"""Unified trade journaling, retry, and reconciliation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from logger import BotLogger
from services.database import DatabaseService
from utils import ensure_directory, load_json, save_json_atomic, utc_now


class TradeJournalService:
    """Single journaling path for trade lifecycle records and retries."""

    journal_version = "2.0"

    def __init__(
        self,
        base_dir: str | Path,
        database: DatabaseService,
        logger: BotLogger,
        config: dict[str, Any],
    ) -> None:
        self.base_dir = Path(base_dir)
        self.database = database
        self.logger = logger
        self.config = config
        self._lock = RLock()
        self.outbox_path = ensure_directory(self.base_dir / "storage") / "journal_outbox.json"
        self.reports_dir = ensure_directory(self.base_dir / "reports")
        self._outcome_epsilon = 1e-8

    def _load_outbox(self) -> list[dict[str, Any]]:
        payload = load_json(self.outbox_path, [])
        return payload if isinstance(payload, list) else []

    def _save_outbox(self, payload: list[dict[str, Any]]) -> None:
        save_json_atomic(self.outbox_path, payload)

    def _queue_record(self, record: dict[str, Any]) -> None:
        with self._lock:
            outbox = self._load_outbox()
            outbox.append(record)
            self._save_outbox(outbox)

    def _flush_outbox(self) -> None:
        with self._lock:
            outbox = self._load_outbox()
            if not outbox:
                return
            survivors: list[dict[str, Any]] = []
            for record in outbox:
                pending = list(record.get("pending_sinks") or [])
                if not pending:
                    continue
                failed: list[str] = []
                for sink in pending:
                    try:
                        payload = dict((record.get("payload") or {}).get(sink) or {})
                        self._write_sink(sink, record["kind"], payload)
                    except Exception as exc:
                        failed.append(sink)
                        record["last_error"] = str(exc)
                if failed:
                    record["pending_sinks"] = failed
                    record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
                    record["last_attempt_at"] = utc_now().isoformat()
                    survivors.append(record)
            self._save_outbox(survivors)

    def recover_outbox_on_startup(self, connector: Any | None = None) -> dict[str, Any]:
        """Recover journaling state on bot restart by flushing outbox and reconciling with MT5.
        
        Called during bot startup to:
        1. Flush any pending journal records from previous session
        2. If connector available, scan open/closed positions and reconcile with journal
        3. Report recovery metrics for observability
        
        Args:
            connector: Optional MT5Connector to validate trades exist in MT5.
            
        Returns:
            Dict with recovery metrics: {'recovered_count', 'failed_count', 'verified_count'}
        """
        metrics = {
            "recovered_count": 0,
            "failed_count": 0,
            "verified_count": 0,
            "skipped_no_connector": False,
        }
        
        # First, flush any pending outbox entries from previous session
        try:
            self._flush_outbox()
            metrics["recovered_count"] += 1
        except Exception as exc:
            self.logger.error(f"Failed to flush journal outbox during recovery: {exc}")
            metrics["failed_count"] += 1
        
        # If connector available, reconcile with MT5
        if connector is None:
            metrics["skipped_no_connector"] = True
            self.logger.structured(
                "journal_recovery_startup",
                {
                    "phase": "no_connector",
                    "metrics": metrics,
                },
                level="INFO",
            )
            return metrics
        
        try:
            # Scan open positions in MT5 and verify they're in journal
            try:
                positions = connector._call_mt5("positions_get", __import__("MetaTrader5").positions_get) or []
                for pos in positions:
                    ticket = getattr(pos, "ticket", None)
                    if ticket:
                        metrics["verified_count"] += 1
            except Exception as pos_exc:
                self.logger.warning(f"Could not verify open positions during recovery: {pos_exc}")
                metrics["failed_count"] += 1
        except Exception as exc:
            self.logger.warning(f"Journal recovery partial failure: {exc}")
            metrics["failed_count"] += 1
        
        self.logger.structured(
            "journal_recovery_startup",
            {
                "phase": "complete",
                "metrics": metrics,
            },
            level="INFO",
        )
        
        return metrics

    def _standardize_trade_row(self, row: dict[str, Any], event_type: str, status: str, note: str | None = None) -> dict[str, Any]:
        """Normalize a trade row into the canonical lifecycle schema."""
        now = utc_now().isoformat()
        payload = dict(row)
        trade_id = str(
            payload.get("trade_id")
            or payload.get("mt5_ticket")
            or payload.get("ticket")
            or payload.get("position_id")
            or payload.get("setup_fingerprint")
            or f"{event_type}:{now}"
        )
        mt5_ticket = str(payload.get("mt5_ticket") or payload.get("ticket") or trade_id)
        position_id = str(payload.get("position_id") or payload.get("ticket") or mt5_ticket)
        entry_price = payload.get("entry_price", payload.get("entry"))
        stop_loss = payload.get("stop_loss", payload.get("sl"))
        take_profit = payload.get("take_profit", payload.get("tp"))
        volume = payload.get("volume", payload.get("lot_size"))
        pnl = payload.get("pnl")
        realized_r = payload.get("realized_r")
        outcome_label = payload.get("outcome_label")
        if not outcome_label:
            try:
                pnl_value = float(pnl)
                if abs(pnl_value) <= self._outcome_epsilon:
                    outcome_label = "BREAKEVEN"
                else:
                    outcome_label = "WIN" if pnl_value > 0 else "LOSS"
            except Exception:
                outcome_label = "UNKNOWN"
        return {
            "timestamp": payload.get("timestamp", now),
            "mode": payload.get("mode") or payload.get("bot_mode"),
            "bot_mode": payload.get("bot_mode") or payload.get("mode"),
            "trade_id": trade_id,
            "mt5_ticket": mt5_ticket,
            "position_id": position_id,
            "ticket": str(payload.get("ticket") or mt5_ticket or position_id or trade_id),
            "order_id": str(payload.get("order_id") or payload.get("order") or ""),
            "symbol": payload.get("symbol") or payload.get("symbol_name") or "",
            "side": payload.get("side") or payload.get("direction") or "",
            "status": status,
            "event_type": event_type,
            "setup": payload.get("setup") or payload.get("setup_fingerprint") or payload.get("setup_family"),
            "setup_fingerprint": payload.get("setup_fingerprint") or payload.get("setup"),
            "setup_family": payload.get("setup_family") or payload.get("setup"),
            "setup_variant": payload.get("setup_variant") or payload.get("trigger_type") or payload.get("setup_type"),
            "setup_type": payload.get("setup_type") or payload.get("setup_variant") or payload.get("trigger_type"),
            "regime": payload.get("regime") or payload.get("regime_at_entry") or payload.get("regime_name"),
            "regime_at_entry": payload.get("regime_at_entry") or payload.get("regime") or payload.get("regime_name"),
            "session": payload.get("session") or payload.get("session_at_entry") or payload.get("session_name"),
            "session_at_entry": payload.get("session_at_entry") or payload.get("session") or payload.get("session_name"),
            "entry_mode": payload.get("entry_mode"),
            "execution_reason": payload.get("execution_reason") or payload.get("reason_code") or payload.get("reason"),
            "blocked_reason": payload.get("blocked_reason") or payload.get("execution_blocked_reason"),
            "close_reason": payload.get("close_reason") or payload.get("exit_reason"),
            "signal_score": payload.get("signal_score"),
            "trend_score": payload.get("trend_score"),
            "setup_score": payload.get("setup_score"),
            "trigger_score": payload.get("trigger_score"),
            "entry_score": payload.get("entry_score"),
            "confidence": payload.get("confidence"),
            "entry": entry_price,
            "entry_price": entry_price,
            "sl": stop_loss,
            "stop_loss": stop_loss,
            "tp": take_profit,
            "take_profit": take_profit,
            "risk_amount": payload.get("risk_amount"),
            "risk_percent": payload.get("risk_percent"),
            "volume": volume,
            "requested_volume": payload.get("requested_volume") or volume,
            "final_volume": payload.get("final_volume") or volume,
            "exit_price": payload.get("exit_price"),
            "pnl": pnl,
            "pnl_pips": payload.get("pnl_pips"),
            "fees": payload.get("fees"),
            "swap": payload.get("swap"),
            "commissions": payload.get("commissions"),
            "realized_r": realized_r,
            "hold_minutes": payload.get("hold_minutes"),
            "hold_seconds": payload.get("hold_seconds"),
            "win_loss": payload.get("win_loss"),
            "outcome_label": outcome_label,
            "bot_mode": payload.get("mode") or payload.get("bot_mode"),
            "account_login": payload.get("account_login"),
            "magic_number": payload.get("magic_number"),
            "timeframe_bias": payload.get("timeframe_bias"),
            "timeframe_setup": payload.get("timeframe_setup") or payload.get("timeframe"),
            "timeframe_entry": payload.get("timeframe_entry") or payload.get("timeframe"),
            "comment": payload.get("comment") or payload.get("note"),
            "entry_time": payload.get("entry_time") or payload.get("opened_at") or payload.get("created_at"),
            "exit_time": payload.get("exit_time") or payload.get("closed_at"),
            "created_at": payload.get("created_at", now),
            "updated_at": payload.get("updated_at", now),
            "closed_at": payload.get("closed_at") or payload.get("exit_time"),
            "journal_version": self.journal_version,
            "unresolved_reason": payload.get("unresolved_reason"),
            "resolution_attempts": payload.get("resolution_attempts"),
            "last_resolution_attempt_at": payload.get("last_resolution_attempt_at"),
            "resolution_stage": payload.get("resolution_stage"),
            "resolution_metadata_json": payload.get("resolution_metadata_json") or payload.get("resolution_metadata"),
            "resolution_started_at": payload.get("resolution_started_at"),
            "resolution_last_evidence_at": payload.get("resolution_last_evidence_at"),
            "matched_order_ids": payload.get("matched_order_ids"),
            "matched_deal_ids": payload.get("matched_deal_ids"),
            "matched_volume": payload.get("matched_volume"),
            "reconciliation_confidence": payload.get("reconciliation_confidence"),
            "close_price_source": payload.get("close_price_source"),
            "lifecycle_version": payload.get("lifecycle_version"),
            "event_bucket": payload.get("event_bucket"),
            "event_key": payload.get("event_key"),
            "raw_mt5_json": payload.get("raw_mt5_json"),
            "metadata_json": payload.get("metadata_json") or payload.get("metadata"),
            "note": note or payload.get("note"),
        }

    def _standardize_signal_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Normalize a signal row into the canonical audit schema."""
        payload = dict(row)
        metadata = payload.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        now = utc_now().isoformat()
        trade_id = str(payload.get("trade_id") or payload.get("setup_fingerprint") or f"signal:{now}")
        raw_event = str(payload.get("event_type") or "signal_created").strip().lower()
        executed = bool(payload.get("executed", False))
        event_map = {
            "signal_observed": ("CANDIDATE_CREATED", "CREATED"),
            "signal_created": ("CANDIDATE_CREATED", "CREATED"),
            "candidate_created": ("CANDIDATE_CREATED", "CREATED"),
            "candidate_rejected_strategy": ("CANDIDATE_REJECTED_STRATEGY", "BLOCKED"),
            "candidate_rejected_risk": ("CANDIDATE_REJECTED_RISK", "BLOCKED"),
            "candidate_rejected_system": ("CANDIDATE_REJECTED_SYSTEM", "BLOCKED"),
            "execution_plan_created": ("EXECUTION_PLAN_CREATED", "PLANNED"),
            "order_submitted": ("ORDER_SUBMITTED", "SUBMITTED"),
            "order_resized_and_submitted": ("ORDER_RESIZED_AND_SUBMITTED", "SUBMITTED"),
            "order_expired_unfilled": ("ORDER_EXPIRED_UNFILLED", "MISSED"),
            "order_missed_drift": ("ORDER_MISSED_DRIFT", "MISSED"),
            "order_missed_state_block": ("ORDER_MISSED_STATE_BLOCK", "MISSED"),
            "order_missed_risk_block": ("ORDER_MISSED_RISK_BLOCK", "MISSED"),
        }
        if raw_event in event_map:
            lifecycle_event, lifecycle_status = event_map[raw_event]
        elif raw_event in {"validation_gate", "execution_attempt"} and executed:
            lifecycle_event = "ORDER_SUBMITTED"
            lifecycle_status = "SUBMITTED"
        elif raw_event in {"validation_gate", "execution_attempt"}:
            lifecycle_event = "SIGNAL_BLOCKED"
            lifecycle_status = "BLOCKED"
        else:
            lifecycle_event = raw_event.upper() if raw_event else "CANDIDATE_CREATED"
            lifecycle_status = str(payload.get("status") or ("EXECUTED" if executed else "BLOCKED")).upper()
        return {
            "timestamp": payload.get("timestamp", now),
            "trade_id": trade_id,
            "mt5_ticket": str(payload.get("mt5_ticket") or payload.get("ticket") or payload.get("setup_fingerprint") or trade_id),
            "event_type": lifecycle_event,
            "status": lifecycle_status,
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "setup": payload.get("setup") or payload.get("setup_fingerprint"),
            "regime": payload.get("regime_name") or payload.get("regime"),
            "session": payload.get("session_name") or payload.get("session"),
            "entry_price": payload.get("entry_price") or payload.get("price"),
            "exit_price": payload.get("exit_price"),
            "stop_loss": payload.get("sl"),
            "take_profit": payload.get("tp"),
            "volume": payload.get("volume") or payload.get("final_volume"),
            "pnl": payload.get("pnl"),
            "realized_r": payload.get("realized_r"),
            "execution_reason": payload.get("reason_code") or payload.get("execution_reason"),
            "blocked_reason": payload.get("execution_blocked_reason") or payload.get("blocked_reason"),
            "close_reason": payload.get("close_reason"),
            "outcome_label": payload.get("outcome_label"),
            "note": payload.get("reason") or payload.get("note"),
            "setup_family": payload.get("setup_family"),
            "signal_score": payload.get("entry_score"),
            "trend_score": payload.get("trend_score"),
            "setup_score": payload.get("setup_score"),
            "trigger_score": payload.get("trigger_score"),
            "entry_score": payload.get("entry_score"),
            "confidence": payload.get("confidence"),
            "bot_mode": payload.get("mode"),
            "magic_number": payload.get("magic_number"),
            "comment": payload.get("comment"),
            "created_at": payload.get("created_at", now),
            "updated_at": payload.get("updated_at", now),
            "journal_version": self.journal_version,
            "metadata_json": payload.get("metadata_json") or payload.get("metadata"),
            "attempt_id": payload.get("attempt_id") or metadata_dict.get("attempt_id"),
            "detected_at": payload.get("detected_at") or metadata_dict.get("detected_at"),
            "validated_at": payload.get("validated_at") or metadata_dict.get("validated_at"),
            "final_outcome_at": payload.get("final_outcome_at") or metadata_dict.get("final_outcome_at"),
            "final_outcome_reason": payload.get("final_outcome_reason") or metadata_dict.get("final_outcome_reason") or payload.get("reason_code"),
            "order_send_attempted": payload.get("order_send_attempted", metadata_dict.get("order_send_attempted", False)),
            "order_send_retcode": payload.get("order_send_retcode", metadata_dict.get("order_send_retcode")),
            "order_send_retcode_text": payload.get("order_send_retcode_text", metadata_dict.get("order_send_retcode_text")),
            "spread_at_validation": payload.get("spread_at_validation", metadata_dict.get("spread_at_validation", payload.get("spread_points"))),
            "spread_limit_used": payload.get("spread_limit_used", metadata_dict.get("spread_limit_used")),
            "spread_limit_source": payload.get("spread_limit_source", metadata_dict.get("spread_limit_source")),
            "cooldown_applied": payload.get("cooldown_applied", metadata_dict.get("cooldown_applied", False)),
            "duplicate_guard_applied": payload.get("duplicate_guard_applied", metadata_dict.get("duplicate_guard_applied", False)),
            "failure_stage": payload.get("failure_stage", metadata_dict.get("failure_stage")),
        }

    def _write_sink(self, sink: str, kind: str, payload: dict[str, Any]) -> None:
        """Write a canonical row to one storage sink."""
        if sink == "db":
            if kind == "signal":
                self.database.insert_signal(payload)
            elif kind == "trade_open":
                self.database.insert_trade_open(payload)
            elif kind == "trade_close":
                self.database.record_trade_close(payload)
            elif kind == "trade_event":
                self.database.record_trade_event(payload)
            else:
                raise ValueError(f"Unknown journal kind for db sink: {kind}")
            return
        if sink == "csv":
            if kind == "signal":
                self.logger.log_signal_event(payload)
            else:
                self.logger.log_trade_event(payload)
            return
        raise ValueError(f"Unknown sink: {sink}")

    def _journal(self, kind: str, payload: dict[str, Any], csv_payload: dict[str, Any], db_payload: dict[str, Any]) -> dict[str, Any]:
        """Write to both sinks and queue any failed sink for retry."""
        self._flush_outbox()
        pending_sinks: list[str] = []
        errors: list[str] = []
        for sink, row in [("db", db_payload), ("csv", csv_payload)]:
            try:
                self._write_sink(sink, kind, row)
            except Exception as exc:
                pending_sinks.append(sink)
                errors.append(f"{sink}: {exc}")
        if pending_sinks:
            self._queue_record(
                {
                    "kind": kind,
                    "payload": {
                        "csv": csv_payload,
                        "db": db_payload,
                    },
                    "pending_sinks": pending_sinks,
                    "attempt_count": 0,
                    "created_at": utc_now().isoformat(),
                    "last_attempt_at": None,
                    "last_error": "; ".join(errors),
                }
            )
        return {
            "ok": not pending_sinks,
            "pending_sinks": pending_sinks,
            "errors": errors,
        }

    def record_signal_event(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a signal or execution attempt."""
        payload = dict(row)
        csv_payload = self._standardize_signal_row(payload)
        db_payload = dict(csv_payload)
        result = self._journal("signal", payload, csv_payload, db_payload)
        try:
            self.database.record_trade_event(csv_payload)
        except Exception as exc:
            self._queue_record(
                {
                    "kind": "trade_event",
                    "payload": {"db": csv_payload, "csv": csv_payload},
                    "pending_sinks": ["db"],
                    "attempt_count": 0,
                    "created_at": utc_now().isoformat(),
                    "last_attempt_at": None,
                    "last_error": str(exc),
                }
            )
        if result["ok"]:
            return result
        return result

    def record_trade_open(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a trade-open lifecycle event."""
        payload_row = dict(row)
        payload = self._standardize_trade_row(payload_row, "TRADE_OPENED", "OPENED", note=payload_row.get("note") or "trade_opened")
        result = self._journal("trade_open", payload_row, payload, payload)
        try:
            self.database.record_trade_event({**payload, "event_type": "TRADE_OPENED", "status": "OPENED"})
        except Exception as exc:
            self._queue_record(
                {
                    "kind": "trade_event",
                    "payload": {"db": {**payload, "event_type": "TRADE_OPENED", "status": "OPENED"}, "csv": payload},
                    "pending_sinks": ["db"],
                    "attempt_count": 0,
                    "created_at": utc_now().isoformat(),
                    "last_attempt_at": None,
                    "last_error": str(exc),
                }
            )
        return result

    def record_trade_close(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a trade-close lifecycle event."""
        payload_row = dict(row)
        status = str(payload_row.get("status") or "CLOSED").upper()
        event_type = str(payload_row.get("event_type") or ("TRADE_FINALIZED" if status == "FINALIZED" else "TRADE_CLOSED")).upper()
        payload = self._standardize_trade_row(payload_row, event_type, status, note=payload_row.get("note"))
        result = self._journal("trade_close", payload_row, payload, payload)
        try:
            self.database.record_trade_event(payload)
        except Exception as exc:
            self._queue_record(
                {
                    "kind": "trade_event",
                    "payload": {"db": payload, "csv": payload},
                    "pending_sinks": ["db"],
                    "attempt_count": 0,
                    "created_at": utc_now().isoformat(),
                    "last_attempt_at": None,
                    "last_error": str(exc),
                }
            )
        return result

    def record_trade_managed(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a trade management event."""
        payload_row = dict(row)
        payload = self._standardize_trade_row(payload_row, "TRADE_MANAGED", "OPENED", note=payload_row.get("note") or "trade_managed")
        return self._journal("trade_event", payload_row, payload, payload)

    def record_trade_event(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a generic trade lifecycle event without changing trade-open/close state."""
        payload_row = dict(row)
        event_type = str(payload_row.get("event_type") or "TRADE_EVENT").upper()
        status = str(payload_row.get("status") or "INFO").upper()
        payload = self._standardize_trade_row(payload_row, event_type, status, note=payload_row.get("note"))
        return self._journal("trade_event", payload_row, payload, payload)

    def record_trade_blocked(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a blocked signal/event."""
        payload_row = dict(row)
        payload = self._standardize_signal_row({**payload_row, "event_type": "SIGNAL_BLOCKED", "status": "BLOCKED"})
        result = self._journal("signal", payload_row, payload, payload)
        self.database.record_trade_event(payload)
        return result

    def flush_pending(self) -> None:
        """Retry any queued journal writes."""
        self._flush_outbox()

    def write_daily_reports(self, day: str, summary: dict[str, Any], performance: dict[str, Any]) -> dict[str, Path]:
        """Persist CSV and markdown daily reports."""
        rows = performance.get("rows", []) if isinstance(performance, dict) else []
        summary_path = self.reports_dir / "daily_summary.csv"
        setup_path = self.reports_dir / "setup_performance.csv"
        blocked_path = self.reports_dir / "blocked_reasons.csv"
        close_path = self.reports_dir / "close_reasons.csv"
        execution_path = self.reports_dir / "execution_reasons.csv"
        md_path = self.reports_dir / f"daily_report_{day}.md"

        import pandas as pd

        summary_row = dict(summary)
        for key in ["by_setup_family", "by_regime", "by_session", "by_close_reason", "by_execution_reason", "by_blocked_reason"]:
            if key in summary_row:
                summary_row[f"{key}_json"] = json.dumps(summary_row.pop(key), ensure_ascii=True, sort_keys=True)
        pd.DataFrame([summary_row]).to_csv(summary_path, index=False)
        frame = pd.DataFrame(rows)
        sorted_frame = frame.copy()
        if not sorted_frame.empty and {"total_r", "average_r"}.issubset(sorted_frame.columns):
            sorted_frame = sorted_frame.sort_values(by=["total_r", "average_r", "trade_count"], ascending=[False, False, False])
        if not frame.empty:
            sorted_frame.to_csv(setup_path, index=False)
            if "blocked_reason" in frame.columns:
                frame.groupby("blocked_reason", dropna=False).size().reset_index(name="count").sort_values(["count", "blocked_reason"], ascending=[False, True]).to_csv(blocked_path, index=False)
            else:
                pd.DataFrame([]).to_csv(blocked_path, index=False)
            if "close_reason" in frame.columns:
                frame.groupby("close_reason", dropna=False).size().reset_index(name="count").sort_values(["count", "close_reason"], ascending=[False, True]).to_csv(close_path, index=False)
            else:
                pd.DataFrame([]).to_csv(close_path, index=False)
            if "execution_reason" in frame.columns:
                frame.groupby("execution_reason", dropna=False).size().reset_index(name="count").sort_values(["count", "execution_reason"], ascending=[False, True]).to_csv(execution_path, index=False)
            else:
                pd.DataFrame([]).to_csv(execution_path, index=False)
        else:
            pd.DataFrame([]).to_csv(setup_path, index=False)
            pd.DataFrame([]).to_csv(blocked_path, index=False)
            pd.DataFrame([]).to_csv(close_path, index=False)
            pd.DataFrame([]).to_csv(execution_path, index=False)

        best_rows = sorted_frame.head(5).to_dict(orient="records") if not sorted_frame.empty else []
        worst_rows = sorted_frame.tail(5).sort_values(by=["total_r", "average_r", "trade_count"], ascending=[True, True, True]).to_dict(orient="records") if not sorted_frame.empty else []

        def _reason_lines(title: str, data: dict[str, Any], limit: int = 8) -> list[str]:
            items = sorted((data or {}).items(), key=lambda item: (-int(item[1]), str(item[0])))[:limit]
            lines = [f"### {title}"]
            if not items:
                lines.append("- None")
                return lines
            for label, count in items:
                lines.append(f"- {label}: {count}")
            return lines

        def _table(title: str, records: list[dict[str, Any]]) -> list[str]:
            lines = [f"### {title}"]
            if not records:
                lines.append("- No rows")
                return lines
            lines.append("| Setup | Regime | Session | Trades | Avg PnL | Avg R | Total R | Hold Mins | Close Reason | Blocked Reason |")
            lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |")
            for row in records:
                lines.append(
                    "| {setup} | {regime} | {session} | {trades} | {avg_pnl:.2f} | {avg_r:.2f} | {total_r:.2f} | {hold:.1f} | {close_reason} | {blocked_reason} |".format(
                        setup=str(row.get("setup_name") or row.get("setup_family") or "UNKNOWN"),
                        regime=str(row.get("regime_name") or "UNKNOWN"),
                        session=str(row.get("session_name") or "UNKNOWN"),
                        trades=int(row.get("trade_count", 0) or 0),
                        avg_pnl=float(row.get("average_pnl", 0.0) or 0.0),
                        avg_r=float(row.get("average_r", 0.0) or 0.0),
                        total_r=float(row.get("total_r", 0.0) or 0.0),
                        hold=float(row.get("average_hold_minutes", 0.0) or 0.0),
                        close_reason=str(row.get("close_reason") or ""),
                        blocked_reason=str(row.get("blocked_reason") or ""),
                    )
                )
            return lines

        md_lines = [
            f"# Daily Report {day}",
            "",
            "## Overview",
            f"- Trades: {summary.get('live_trades', 0)}",
            f"- Wins: {summary.get('wins', 0)}",
            f"- Losses: {summary.get('losses', 0)}",
            f"- Breakevens: {summary.get('breakevens', 0)}",
            f"- Win rate: {float(summary.get('win_rate', 0.0)) * 100.0:.2f}%",
            f"- Loss rate: {float(summary.get('loss_rate', 0.0)) * 100.0:.2f}%",
            f"- Profit factor: {float(summary.get('profit_factor', 0.0)):.2f}",
            f"- Total PnL: {float(summary.get('total_pnl', 0.0)):.2f}",
            f"- Total R: {float(summary.get('total_r', 0.0)):.2f}",
            f"- Average R: {float(summary.get('average_r', 0.0)):.2f}",
            f"- Expectancy: {float(summary.get('expectancy', 0.0)):.2f}",
            f"- Average hold minutes: {float(summary.get('average_hold_minutes', 0.0)):.1f}",
            "",
            "## Best And Worst",
        ]
        md_lines.extend(_table("Best Setups", best_rows))
        md_lines.append("")
        md_lines.extend(_table("Worst Setups", worst_rows))
        md_lines.append("")
        md_lines.extend(_reason_lines("Top Close Reasons", summary.get("by_close_reason", {})))
        md_lines.append("")
        md_lines.extend(_reason_lines("Top Execution Reasons", summary.get("by_execution_reason", {})))
        md_lines.append("")
        md_lines.extend(_reason_lines("Top Blocked Reasons", summary.get("by_blocked_reason", {})))
        md_lines.append("")
        md_lines.append("## Notes")
        md_lines.append("- Review setups with strong trigger quality but poor realized R.")
        md_lines.append("- Compare blocked reasons to see whether filters are over-restrictive.")
        md_lines.append("- Investigate repeated close reasons if one exit path dominates losses.")
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        return {
            "summary_csv": summary_path,
            "setup_csv": setup_path,
            "blocked_csv": blocked_path,
            "close_csv": close_path,
            "execution_csv": execution_path,
            "markdown": md_path,
        }
