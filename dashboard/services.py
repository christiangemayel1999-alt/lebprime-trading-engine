"""Dashboard data services and API helpers."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from dashboard.queries import (
    get_daily_pnl_curve,
    get_last_error,
    get_last_signal,
    get_recent_events,
    get_recent_signals,
    get_recent_trades,
    get_top_metrics,
)
from services.database import DatabaseService
from services.backtest_storage import BacktestStorage
from services.worker_control import DEFAULT_WORKER_STALE_SECONDS, read_worker_status


class DashboardDataService:
    """Read dashboard status, logs, and analytics from bot storage."""

    def __init__(self, base_dir: str | Path, database: DatabaseService) -> None:
        self.base_dir = Path(base_dir)
        self.database = database
        self.backtest_storage = BacktestStorage(database)
        self._runtime_logger = logging.getLogger("mtf_sniper_bot")
        self._last_status_count_log: tuple[tuple[str, int], ...] | None = None
        self._last_status_count_log_at = 0.0

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        """Return a float with a safe default."""
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        """Return an int with a safe default."""
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _as_text(value: Any, default: str = "") -> str:
        """Return a display-safe string."""
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _normalize_trade_rows(self, rows: list[dict[str, Any]], *, closed: bool = False) -> list[dict[str, Any]]:
        """Normalize trade payloads for dashboard rendering."""
        payload: list[dict[str, Any]] = []
        for row in rows or []:
            normalized = {
                "trade_id": self._as_text(row.get("trade_id") or row.get("mt5_ticket") or row.get("position_id") or row.get("order_id"), "UNKNOWN"),
                "mt5_ticket": self._as_text(row.get("mt5_ticket")),
                "position_id": self._as_text(row.get("position_id")),
                "order_id": self._as_text(row.get("order_id")),
                "symbol": self._as_text(row.get("symbol"), "UNKNOWN"),
                "side": self._as_text(row.get("side"), "UNKNOWN"),
                "status": self._as_text(row.get("status"), "UNKNOWN"),
                "event_type": self._as_text(row.get("event_type"), "UNKNOWN"),
                "setup": self._as_text(row.get("setup"), "UNKNOWN"),
                "setup_family": self._as_text(row.get("setup_family"), self._as_text(row.get("setup"), "UNKNOWN")),
                "regime": self._as_text(row.get("regime"), "UNKNOWN"),
                "session": self._as_text(row.get("session"), "UNKNOWN"),
                "entry_price": self._as_float(row.get("entry_price")),
                "stop_loss": self._as_float(row.get("stop_loss")),
                "take_profit": self._as_float(row.get("take_profit")),
                "volume": self._as_float(row.get("volume")),
                "close_reason": self._as_text(row.get("close_reason")),
                "comment": self._as_text(row.get("comment")),
                "note": self._as_text(row.get("note")),
                "created_at": self._as_text(row.get("created_at")),
                "updated_at": self._as_text(row.get("updated_at")),
                "closed_at": self._as_text(row.get("closed_at")),
            }
            if not closed:
                normalized.update(
                    {
                        "unresolved_reason": self._as_text(row.get("unresolved_reason")),
                        "resolution_attempts": self._as_int(row.get("resolution_attempts")),
                        "last_resolution_attempt_at": self._as_text(row.get("last_resolution_attempt_at")),
                    }
                )
            else:
                normalized.update(
                    {
                        "exit_price": self._as_float(row.get("exit_price")),
                        "pnl": self._as_float(row.get("pnl")),
                        "pnl_pips": self._as_float(row.get("pnl_pips")),
                        "realized_r": self._as_float(row.get("realized_r")),
                        "hold_minutes": self._as_float(row.get("hold_minutes")),
                        "hold_seconds": self._as_float(row.get("hold_seconds")),
                        "win_loss": self._as_text(row.get("win_loss"), "UNKNOWN"),
                        "outcome_label": self._as_text(row.get("outcome_label"), "UNKNOWN"),
                        "execution_reason": self._as_text(row.get("execution_reason")),
                        "blocked_reason": self._as_text(row.get("blocked_reason")),
                    }
                )
            payload.append(normalized)
        return payload

    def _normalize_daily_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize the daily performance payload for JS consumers."""
        summary = dict(payload.get("summary") or {})
        summary.setdefault("day", self._as_text(payload.get("day")))
        for key in ("setups", "live_trades", "wins", "losses", "breakevens", "long_trades", "short_trades"):
            summary[key] = self._as_int(summary.get(key))
        for key in ("win_rate", "loss_rate", "profit_factor", "expectancy", "total_pnl", "total_r", "average_r", "average_pnl", "average_hold_minutes", "average_hold_seconds"):
            summary[key] = self._as_float(summary.get(key))
        for key in ("by_setup_family", "by_regime", "by_session", "by_close_reason", "by_execution_reason", "by_blocked_reason"):
            value = summary.get(key)
            summary[key] = value if isinstance(value, dict) else {}
        rows: list[dict[str, Any]] = []
        for row in payload.get("performance_rows") or []:
            rows.append(
                {
                    "setup_name": self._as_text(row.get("setup_name"), "UNKNOWN"),
                    "regime_name": self._as_text(row.get("regime_name"), "UNKNOWN"),
                    "session_name": self._as_text(row.get("session_name"), "UNKNOWN"),
                    "close_reason": self._as_text(row.get("close_reason"), "unknown"),
                    "blocked_reason": self._as_text(row.get("blocked_reason"), "none"),
                    "outcome_label": self._as_text(row.get("outcome_label"), "UNKNOWN"),
                    "trade_count": self._as_int(row.get("trade_count")),
                    "average_pnl": self._as_float(row.get("average_pnl")),
                    "average_r": self._as_float(row.get("average_r")),
                    "total_r": self._as_float(row.get("total_r")),
                    "average_hold_minutes": self._as_float(row.get("average_hold_minutes")),
                    "wins": self._as_int(row.get("wins")),
                    "losses": self._as_int(row.get("losses")),
                    "breakevens": self._as_int(row.get("breakevens")),
                }
            )
        return {"day": self._as_text(payload.get("day") or summary.get("day")), "summary": summary, "performance_rows": rows}

    def _log_status_counts(self, payload: dict[str, Any]) -> None:
        """Log key dashboard query counts when they change or periodically."""
        counts = (
            ("recent_trades", len(payload.get("recent_trades") or [])),
            ("recent_signals", len(payload.get("recent_signals") or [])),
            ("recent_events", len(payload.get("recent_events") or [])),
            ("blocked_setups", len(payload.get("blocked_setups") or [])),
            ("valid_not_executed", len(payload.get("valid_not_executed") or [])),
            ("unresolved_trades", len(payload.get("unresolved_trades") or [])),
            ("recent_closed_trades", len(payload.get("recent_closed_trades") or [])),
            ("performance_rows", len((payload.get("daily_performance") or {}).get("performance_rows") or [])),
        )
        now = time.time()
        if counts == self._last_status_count_log and (now - self._last_status_count_log_at) < 60.0:
            return
        self._runtime_logger.info(
            "dashboard_status_snapshot_counts %s",
            " ".join(f"{name}={value}" for name, value in counts),
        )
        self._last_status_count_log = counts
        self._last_status_count_log_at = now

    def recent_logs(
        self,
        level: str | None = None,
        source: str | None = None,
        search: str | None = None,
        date_from: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read recent structured bot events for the logs page."""
        statement = "SELECT timestamp, level, event_type, message, reason_code, details_json FROM bot_events WHERE 1=1"
        parameters: list[Any] = []
        if level:
            statement += " AND level = ?"
            parameters.append(level.upper())
        if source:
            statement += " AND event_type LIKE ?"
            parameters.append(f"%{source}%")
        if search:
            statement += " AND (message LIKE ? OR details_json LIKE ?)"
            parameters.extend([f"%{search}%", f"%{search}%"])
        if date_from:
            statement += " AND substr(timestamp, 1, 10) >= ?"
            parameters.append(date_from)
        statement += " ORDER BY id DESC LIMIT ?"
        parameters.append(int(limit))
        rows = self.database._query_dataframe(statement, tuple(parameters))
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "timestamp": row["timestamp"],
                    "level": row["level"],
                    "event_type": row["event_type"],
                    "message": row["message"],
                    "reason_code": row["reason_code"],
                    "details": json.loads(row["details_json"]) if row["details_json"] else {},
                }
            )
        return payload

    def rejection_summary(self, hours: int = 24, limit: int = 8) -> list[dict[str, Any]]:
        """Return top rejection reasons from recent signal/execution rows."""
        rows = self.database._query_dataframe(
            """
            SELECT COALESCE(final_outcome_reason, reason_code) AS reason_code, COUNT(*) AS rejection_count
            FROM signals
            WHERE timestamp >= datetime('now', ?)
              AND COALESCE(final_outcome_reason, reason_code, '') NOT IN ('', 'entry_valid', 'live_validation_passed', 'live_eligible_setup', 'dry_run_validated', 'paper_validated_test_mode', 'executed', 'executed_live', 'setup_valid')
            GROUP BY reason_code
            ORDER BY rejection_count DESC, reason_code ASC
            LIMIT ?;
            """,
            (f"-{int(hours)} hours", int(limit)),
        )
        return [{"reason_code": row["reason_code"], "count": int(row["rejection_count"])} for row in rows]

    def blocked_setups_snapshot(self, hours: int = 12, limit: int = 12) -> list[dict[str, Any]]:
        """Return recent blocked execution attempts with enough context for the dashboard."""
        rows = self.database._query_dataframe(
            """
            SELECT
                timestamp,
                setup_family,
                side,
                regime_name,
                session_name,
                reason_code,
                reason,
                trend_score,
                setup_score,
                trigger_score,
                entry_score,
                spread_points,
                final_outcome_reason,
                order_send_attempted,
                order_send_retcode,
                order_send_retcode_text,
                spread_limit_used,
                spread_limit_source,
                cooldown_applied,
                duplicate_guard_applied,
                failure_stage
            FROM signals
            WHERE timestamp >= datetime('now', ?)
              AND UPPER(event_type) IN ('VALIDATION_GATE', 'EXECUTION_ATTEMPT', 'CANDIDATE_REJECTED_STRATEGY', 'CANDIDATE_REJECTED_RISK', 'CANDIDATE_REJECTED_SYSTEM', 'ORDER_MISSED_DRIFT', 'ORDER_MISSED_STATE_BLOCK', 'ORDER_MISSED_RISK_BLOCK')
              AND executed = 0
              AND COALESCE(final_outcome_reason, reason_code, '') NOT IN ('', 'entry_valid', 'live_validation_passed', 'live_eligible_setup', 'dry_run_validated', 'paper_validated_test_mode', 'executed', 'executed_live')
            ORDER BY id DESC
            LIMIT ?;
            """,
            (f"-{int(hours)} hours", int(limit)),
        )
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "timestamp": row["timestamp"],
                    "setup_family": row["setup_family"],
                    "side": row["side"],
                    "regime_name": row["regime_name"],
                    "session_name": row["session_name"],
                    "reason_code": row["final_outcome_reason"] or row["reason_code"],
                    "reason": row["reason"],
                    "trend_score": row["trend_score"],
                    "setup_score": row["setup_score"],
                    "trigger_score": row["trigger_score"],
                    "entry_score": row["entry_score"],
                    "spread_points": row["spread_points"],
                    "order_send_attempted": bool(row["order_send_attempted"]),
                    "order_send_retcode": row["order_send_retcode"],
                    "order_send_retcode_text": row["order_send_retcode_text"],
                    "spread_limit_used": row["spread_limit_used"],
                    "spread_limit_source": row["spread_limit_source"],
                    "cooldown_applied": bool(row["cooldown_applied"]),
                    "duplicate_guard_applied": bool(row["duplicate_guard_applied"]),
                    "failure_stage": row["failure_stage"],
                }
            )
        return payload

    def valid_not_executed_snapshot(self, hours: int = 12, limit: int = 20) -> list[dict[str, Any]]:
        """Return entry-valid attempts that ended with a non-executed final outcome."""
        rows = self.database._query_dataframe(
            """
            SELECT
                timestamp,
                setup_family,
                side,
                regime_name,
                session_name,
                COALESCE(final_outcome_reason, reason_code) AS final_outcome_reason,
                reason,
                order_send_attempted,
                order_send_retcode,
                order_send_retcode_text,
                spread_points,
                spread_limit_used,
                spread_limit_source,
                cooldown_applied,
                duplicate_guard_applied
            FROM signals
            WHERE timestamp >= datetime('now', ?)
              AND UPPER(event_type) IN ('EXECUTION_ATTEMPT', 'ORDER_MISSED_DRIFT', 'ORDER_MISSED_STATE_BLOCK', 'ORDER_MISSED_RISK_BLOCK', 'ORDER_SUBMITTED', 'ORDER_RESIZED_AND_SUBMITTED')
              AND executed = 0
              AND COALESCE(final_outcome_reason, reason_code, '') NOT IN ('', 'executed', 'executed_live')
            ORDER BY id DESC
            LIMIT ?;
            """,
            (f"-{int(hours)} hours", int(limit)),
        )
        return [
            {
                "timestamp": self._as_text(row.get("timestamp")),
                "setup_family": self._as_text(row.get("setup_family"), "UNKNOWN"),
                "side": self._as_text(row.get("side"), "UNKNOWN"),
                "regime_name": self._as_text(row.get("regime_name"), "UNKNOWN"),
                "session_name": self._as_text(row.get("session_name"), "UNKNOWN"),
                "final_outcome_reason": self._as_text(row.get("final_outcome_reason")),
                "reason": self._as_text(row.get("reason")),
                "order_send_attempted": bool(row.get("order_send_attempted")),
                "order_send_retcode": self._as_text(row.get("order_send_retcode")),
                "order_send_retcode_text": self._as_text(row.get("order_send_retcode_text")),
                "spread_points": self._as_float(row.get("spread_points")),
                "spread_limit_used": self._as_float(row.get("spread_limit_used")),
                "spread_limit_source": self._as_text(row.get("spread_limit_source"), "default"),
                "cooldown_applied": bool(row.get("cooldown_applied")),
                "duplicate_guard_applied": bool(row.get("duplicate_guard_applied")),
            }
            for row in rows
        ]

    def blocked_reason_matrix(self, hours: int = 24, dimension: str = "setup_family", limit: int = 50) -> list[dict[str, Any]]:
        """Return grouped blocked-reason counts by family or strategy."""
        allowed_dimensions = {"setup_family", "signal_type"}
        column = dimension if dimension in allowed_dimensions else "setup_family"
        rows = self.database._query_dataframe(
            f"""
            SELECT
                COALESCE({column}, 'UNKNOWN') AS group_name,
                COALESCE(final_outcome_reason, reason_code, 'unknown') AS reason_code,
                COUNT(*) AS blocked_count
            FROM signals
            WHERE timestamp >= datetime('now', ?)
              AND UPPER(event_type) IN ('VALIDATION_GATE', 'EXECUTION_ATTEMPT', 'CANDIDATE_REJECTED_STRATEGY', 'CANDIDATE_REJECTED_RISK', 'CANDIDATE_REJECTED_SYSTEM', 'ORDER_MISSED_DRIFT', 'ORDER_MISSED_STATE_BLOCK', 'ORDER_MISSED_RISK_BLOCK')
              AND executed = 0
              AND COALESCE(final_outcome_reason, reason_code, '') NOT IN ('', 'entry_valid', 'live_validation_passed', 'live_eligible_setup', 'dry_run_validated', 'paper_validated_test_mode', 'executed', 'executed_live')
            GROUP BY group_name, reason_code
            ORDER BY blocked_count DESC, group_name ASC, reason_code ASC
            LIMIT ?;
            """,
            (f"-{int(hours)} hours", int(limit)),
        )
        return [
            {
                "group_name": row["group_name"],
                "reason_code": row["reason_code"],
                "count": int(row["blocked_count"]),
            }
            for row in rows
        ]

    def unresolved_trades(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return trades waiting for close resolution."""
        rows = self.database._query_dataframe(
            """
            SELECT
                   COALESCE(trade_id, position_id, mt5_ticket, ticket, order_id) AS trade_id,
                   mt5_ticket,
                   position_id,
                   order_id,
                   symbol,
                   side,
                   status,
                   event_type,
                   COALESCE(NULLIF(setup, ''), NULLIF(setup_fingerprint, ''), NULLIF(setup_family, '')) AS setup,
                   COALESCE(NULLIF(setup_family, ''), NULLIF(setup, ''), NULLIF(setup_type, '')) AS setup_family,
                   COALESCE(NULLIF(regime, ''), NULLIF(regime_at_entry, ''), 'UNKNOWN') AS regime,
                   COALESCE(NULLIF(session, ''), NULLIF(session_at_entry, ''), 'UNKNOWN') AS session,
                   entry_price,
                   stop_loss,
                   take_profit,
                   volume,
                   close_reason,
                   unresolved_reason,
                   resolution_attempts,
                   last_resolution_attempt_at,
                   created_at,
                   updated_at,
                   closed_at,
                   comment,
                   note
            FROM trades
            WHERE status IN ('PENDING_CLOSE_RESOLUTION', 'CLOSE_HISTORY_PENDING', 'CLOSE_HISTORY_PARTIAL')
            ORDER BY COALESCE(last_resolution_attempt_at, closed_at, updated_at, created_at, timestamp) ASC, id ASC
            LIMIT ?;
            """,
            (int(limit),),
        )
        return self._normalize_trade_rows([dict(row) for row in rows], closed=False)

    def recent_closed_trades(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return recently finalized trades."""
        rows = self.database._query_dataframe(
            """
            SELECT
                   COALESCE(trade_id, position_id, mt5_ticket, ticket, order_id) AS trade_id,
                   mt5_ticket,
                   position_id,
                   symbol,
                   side,
                   status,
                   event_type,
                   COALESCE(NULLIF(setup, ''), NULLIF(setup_fingerprint, ''), NULLIF(setup_family, '')) AS setup,
                   COALESCE(NULLIF(setup_family, ''), NULLIF(setup, ''), NULLIF(setup_type, '')) AS setup_family,
                   COALESCE(NULLIF(regime, ''), NULLIF(regime_at_entry, ''), 'UNKNOWN') AS regime,
                   COALESCE(NULLIF(session, ''), NULLIF(session_at_entry, ''), 'UNKNOWN') AS session,
                   entry_price,
                   stop_loss,
                   take_profit,
                   exit_price,
                   pnl,
                   pnl_pips,
                   realized_r,
                   hold_minutes,
                   hold_seconds,
                   COALESCE(NULLIF(win_loss, ''), NULLIF(outcome_label, '')) AS win_loss,
                   COALESCE(NULLIF(outcome_label, ''), CASE WHEN pnl IS NULL THEN 'UNKNOWN' WHEN ABS(COALESCE(pnl, 0)) <= 1e-8 THEN 'BREAKEVEN' WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' ELSE 'LOSS' END) AS outcome_label,
                   execution_reason,
                   blocked_reason,
                   close_reason,
                   comment,
                   closed_at,
                   updated_at
            FROM trades
            WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED', 'FAILED_CLOSE_RESOLUTION')
            ORDER BY COALESCE(closed_at, updated_at, timestamp) DESC, id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )
        return self._normalize_trade_rows([dict(row) for row in rows], closed=True)

    def daily_performance_snapshot(self, day: str | None = None) -> dict[str, Any]:
        """Return a performance snapshot for the requested UTC day."""
        target_day = day or __import__("datetime").datetime.utcnow().date().isoformat()
        summary = self.database.build_daily_analytics(target_day)
        performance = self.database.build_trade_performance_report(target_day)
        return self._normalize_daily_performance({
            "day": target_day,
            "summary": summary,
            "performance_rows": performance.get("rows", []),
        })

    def live_integrity_snapshot(self, runtime_config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return compact live integrity counters and recent reconciliation context."""
        unresolved = self.unresolved_trades(limit=50)
        failed_rows = self.database._query_dataframe(
            """
            SELECT COALESCE(trade_id, mt5_ticket, position_id, order_id) AS trade_id, status, close_reason, unresolved_reason, updated_at
            FROM trades
            WHERE status = 'FAILED_CLOSE_RESOLUTION'
            ORDER BY COALESCE(updated_at, closed_at, timestamp) DESC, id DESC
            LIMIT 10;
            """,
            (),
        )
        ambiguous_rows = self.database._query_dataframe(
            """
            SELECT COUNT(*) AS count
            FROM trade_events
            WHERE event_type = 'ORDER_FILL_AMBIGUOUS';
            """,
            (),
        )
        replay_rows = self.database._query_dataframe(
            """
            SELECT COUNT(*) AS count
            FROM signals
            WHERE COALESCE(final_outcome_reason, reason_code, '') = 'blocked_setup_replay_suppressed';
            """,
            (),
        )
        return {
            "current_db_path": str((runtime_config or {}).get("storage", {}).get("database_path", "")),
            "active_mode": str((runtime_config or {}).get("bot", {}).get("trading_mode", "UNKNOWN")),
            "execution_enabled": bool((runtime_config or {}).get("bot", {}).get("allow_live_execution", False)),
            "unresolved_trades_count": len(unresolved),
            "unresolved_trade_ids": [row["trade_id"] for row in unresolved[:10]],
            "failed_resolution_count": len(failed_rows),
            "recent_reconciliation_failures": [dict(row) for row in failed_rows],
            "duplicate_event_suppression_count": int(self.database.get_maintenance_counter("trade_event_duplicate_suppressed")),
            "blocked_replay_suppression_count": int((replay_rows[0]["count"] if replay_rows else 0) or 0),
            "ambiguous_pending_fill_count": int((ambiguous_rows[0]["count"] if ambiguous_rows else 0) or 0),
        }

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent audit log rows."""
        rows = self.database.get_recent_audit_events(limit=limit)
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "timestamp": row["timestamp"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "target": row["target"],
                    "level": row["level"],
                    "details": json.loads(row["details_json"]) if row["details_json"] else {},
                }
            )
        return payload

    def status_snapshot(self, db_path: str | Path, stale_seconds: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate dashboard status from the existing SQLite helpers."""
        runtime_config = config or {}
        strategy_cfg = runtime_config.get("strategy", {}) if isinstance(runtime_config, dict) else {}
        risk_cfg = runtime_config.get("risk", {}) if isinstance(runtime_config, dict) else {}
        setup_controls = strategy_cfg.get("setup_controls", {}) if isinstance(strategy_cfg.get("setup_controls", {}), dict) else {}
        setup_families = strategy_cfg.get("setup_families", {}) if isinstance(strategy_cfg.get("setup_families", {}), dict) else {}
        enabled_strategy_count = sum(1 for item in setup_controls.values() if isinstance(item, dict) and bool(item.get("enabled", True)))
        enabled_family_count = sum(1 for item in setup_families.values() if isinstance(item, dict) and bool(item.get("enabled", True)))
        metrics = get_top_metrics(db_path, stale_seconds)
        max_daily_loss_pct = float(risk_cfg.get("max_daily_drawdown_pct", 0.0) or 0.0)
        balance = float(metrics.get("balance", 0.0) or 0.0)
        daily_loss_limit_value = balance * (max_daily_loss_pct / 100.0) if balance > 0 and max_daily_loss_pct > 0 else 0.0
        today_pnl = float(metrics.get("today_pnl", 0.0) or 0.0)
        daily_loss_used_pct = 0.0
        if daily_loss_limit_value > 0 and today_pnl < 0:
            daily_loss_used_pct = min(100.0, abs(today_pnl) / daily_loss_limit_value * 100.0)
        payload = {
            "metrics": {
                **metrics,
                "enabled_strategy_count": enabled_strategy_count,
                "enabled_family_count": enabled_family_count,
                "daily_loss_used_pct": round(daily_loss_used_pct, 2),
            },
            "resolved_runtime": runtime_config.get("resolved_runtime", {}),
            "last_signal": get_last_signal(db_path),
            "last_error": get_last_error(db_path),
            "recent_trades": get_recent_trades(db_path, limit=10).to_dict("records"),
            "recent_signals": get_recent_signals(db_path, limit=10).to_dict("records"),
            "recent_events": get_recent_events(db_path, limit=10).to_dict("records"),
            "daily_pnl_curve": get_daily_pnl_curve(db_path).to_dict("records"),
            "rejection_summary": self.rejection_summary(hours=24, limit=8),
            "blocked_setups": self.blocked_setups_snapshot(hours=12, limit=12),
            "valid_not_executed": self.valid_not_executed_snapshot(hours=12, limit=20),
            "family_block_matrix": self.blocked_reason_matrix(hours=24, dimension="setup_family", limit=30),
            "strategy_block_matrix": self.blocked_reason_matrix(hours=24, dimension="signal_type", limit=30),
            "unresolved_trades": self.unresolved_trades(limit=15),
            "recent_closed_trades": self.recent_closed_trades(limit=15),
            "daily_performance": self.daily_performance_snapshot(),
            "backtest_runs": self.list_backtest_runs(limit=10),
            "live_integrity": self.live_integrity_snapshot(runtime_config),
        }
        self._log_status_counts(payload)
        return payload

    def list_backtest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent backtest runs for dashboard listing."""
        return self.backtest_storage.list_runs(limit=limit)

    def get_backtest_run_bundle(self, run_id: int) -> dict[str, Any]:
        """Return full backtest bundle for a run id."""
        return self.backtest_storage.get_run_bundle(run_id)

    @staticmethod
    def _parse_json_field(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

    def _normalize_playback_frame(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": self._as_int(row.get("run_id")),
            "bar_index": self._as_int(row.get("bar_index")),
            "timestamp": self._as_text(row.get("timestamp")),
            "open": self._as_float(row.get("open")),
            "high": self._as_float(row.get("high")),
            "low": self._as_float(row.get("low")),
            "close": self._as_float(row.get("close")),
            "volume": self._as_float(row.get("volume")),
            "tick_volume": self._as_float(row.get("tick_volume")),
            "spread": self._as_float(row.get("spread")),
            "equity": self._as_float(row.get("equity")),
            "balance": self._as_float(row.get("balance")),
            "floating_pnl": self._as_float(row.get("floating_pnl")),
            "open_positions": self._parse_json_field(row.get("open_positions_json"), []),
            "pending_orders": self._parse_json_field(row.get("pending_orders_json"), []),
            "selected_candidate": self._parse_json_field(row.get("selected_candidate_json"), {}),
            "candidate_summary": self._parse_json_field(row.get("candidate_summary_json"), {}),
            "state": self._parse_json_field(row.get("state_json"), {}),
        }

    def _normalize_playback_event(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": self._as_int(row.get("run_id")),
            "bar_index": self._as_int(row.get("bar_index")),
            "timestamp": self._as_text(row.get("timestamp")),
            "event_type": self._as_text(row.get("event_type"), "UNKNOWN"),
            "position_id": self._as_text(row.get("position_id")),
            "signal_id": self._as_text(row.get("signal_id")),
            "strategy_name": self._as_text(row.get("strategy_name")),
            "setup_family": self._as_text(row.get("setup_family")),
            "side": self._as_text(row.get("side")),
            "price": self._as_float(row.get("price")),
            "reason_code": self._as_text(row.get("reason_code")),
            "payload": self._parse_json_field(row.get("event_payload_json"), {}),
        }

    def _normalize_run_record(self, row: dict[str, Any]) -> dict[str, Any]:
        run = dict(row or {})
        run["enabled_strategies_json"] = self._parse_json_field(run.get("enabled_strategies_json"), [])
        run["session_filter_json"] = self._parse_json_field(run.get("session_filter_json"), {})
        run["spread_model"] = self._parse_json_field(run.get("spread_model"), {})
        run["slippage_model"] = self._parse_json_field(run.get("slippage_model"), {})
        run["artifacts_json"] = self._parse_json_field(run.get("artifacts_json"), {})
        run["metadata_json"] = self._parse_json_field(run.get("metadata_json"), {})
        return run

    def get_backtest_replay_summary(self, run_id: int) -> dict[str, Any]:
        """Return a front-end friendly summary for a finished backtest replay."""
        bundle = self.backtest_storage.get_run_bundle(run_id)
        frames = [self._normalize_playback_frame(row) for row in bundle.get("playback_frames", [])]
        events = [self._normalize_playback_event(row) for row in bundle.get("playback_events", [])]
        run = self._normalize_run_record(bundle.get("run") or {})
        return {
            "run": run,
            "summary": bundle.get("summary") or {},
            "frames_count": len(frames),
            "events_count": len(events),
            "last_frame": frames[-1] if frames else None,
            "last_event": events[-1] if events else None,
            "playback_ready": bool(run.get("state") in {"COMPLETED", "PARTIAL", "FAILED", "INTERRUPTED"}),
            "history_resolution": (run.get("metadata_json") or {}).get("history_resolution", {}),
        }

    def get_backtest_replay_window(self, run_id: int, start_bar: int, end_bar: int) -> dict[str, Any]:
        """Return a playback window with frames and events for a bar range."""
        frames = self.database.get_backtest_playback_frames(run_id, start_bar=int(start_bar), end_bar=int(end_bar))
        events = self.database.get_backtest_playback_events(run_id, start_bar=int(start_bar), end_bar=int(end_bar))
        return {
            "run_id": int(run_id),
            "start_bar": int(start_bar),
            "end_bar": int(end_bar),
            "frames": [self._normalize_playback_frame(row) for row in frames],
            "events": [self._normalize_playback_event(row) for row in events],
        }

    def get_backtest_replay_events(self, run_id: int, start_bar: int, end_bar: int) -> dict[str, Any]:
        """Return replay events for a specific bar window."""
        events = self.database.get_backtest_playback_events(run_id, start_bar=int(start_bar), end_bar=int(end_bar))
        return {
            "run_id": int(run_id),
            "start_bar": int(start_bar),
            "end_bar": int(end_bar),
            "events": [self._normalize_playback_event(row) for row in events],
        }

    def get_backtest_replay_frame(self, run_id: int, bar_index: int) -> dict[str, Any] | None:
        """Return one replay frame by bar index."""
        row = self.database.get_backtest_playback_frame(run_id, int(bar_index))
        return self._normalize_playback_frame(row) if row else None

    def get_backtest_stream_status(self, run_id: int) -> dict[str, Any]:
        """Return polling-friendly status for a running replay."""
        run = self._normalize_run_record(self.database.get_backtest_run(run_id) or {})
        if not run:
            raise ValueError(f"Backtest run {run_id} not found")
        latest_frame = self.database._query_dataframe(
            """
            SELECT *
            FROM backtest_playback_frames
            WHERE run_id = ?
            ORDER BY bar_index DESC, id DESC
            LIMIT 1;
            """,
            (int(run_id),),
        )
        latest_event = self.database._query_dataframe(
            """
            SELECT *
            FROM backtest_playback_events
            WHERE run_id = ?
            ORDER BY bar_index DESC, id DESC
            LIMIT 1;
            """,
            (int(run_id),),
        )
        return {
            "run": run,
            "state": self._as_text(run.get("state"), "UNKNOWN"),
            "progress_current": self._as_int(run.get("progress_current")),
            "progress_total": self._as_int(run.get("progress_total")),
            "latest_frame": self._normalize_playback_frame(latest_frame[-1]) if latest_frame else None,
            "latest_event": self._normalize_playback_event(latest_event[-1]) if latest_event else None,
            "streaming": self._as_text(run.get("state")) in {"QUEUED", "RUNNING"},
        }

    def get_backtest_stream_window(self, run_id: int, after_bar: int) -> dict[str, Any]:
        """Return incremental frames/events after a cursor bar index."""
        frames = self.database.get_backtest_playback_frames(run_id, after_bar=int(after_bar))
        events = self.database.get_backtest_playback_events(run_id, after_bar=int(after_bar))
        return {
            "run_id": int(run_id),
            "after_bar": int(after_bar),
            "frames": [self._normalize_playback_frame(row) for row in frames],
            "events": [self._normalize_playback_event(row) for row in events],
            "cursor": frames[-1]["bar_index"] if frames else int(after_bar),
        }

    def worker_status(self, stale_seconds: int = DEFAULT_WORKER_STALE_SECONDS) -> dict[str, Any]:
        """Return dashboard-friendly worker heartbeat status."""
        return read_worker_status(self.base_dir, stale_seconds=stale_seconds)

    def list_oos_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent OOS runs with parsed manifest fields."""
        rows = self.database.get_oos_runs(limit=limit)
        payload: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["request_json"] = json.loads(item["request_json"]) if item.get("request_json") else {}
            item["metadata_json"] = json.loads(item["metadata_json"]) if item.get("metadata_json") else {}
            item["scenarios"] = []
            payload.append(item)
        return payload
