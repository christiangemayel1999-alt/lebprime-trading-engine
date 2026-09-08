"""Logging utilities for operational, signal, analytics, and trade journals."""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any

from utils import UTC, ensure_directory


SIGNAL_COLUMNS = [
    "timestamp",
    "event_type",
    "mode",
    "live_or_dry",
    "symbol",
    "side",
    "timeframe",
    "signal_type",
    "setup_fingerprint",
    "setup_family",
    "setup_anchor_time",
    "trigger_type",
    "price",
    "setup_price",
    "value_price",
    "entry_price",
    "sl",
    "tp",
    "spread_points",
    "requested_volume",
    "final_volume",
    "trend_ok",
    "setup_ok",
    "entry_ok",
    "executed",
    "entry_mode",
    "regime_name",
    "regime_confidence",
    "session_name",
    "session_live_allowed",
    "reason_code",
    "reason",
    "trend_score",
    "setup_score",
    "trigger_score",
    "entry_score",
    "attempt_id",
    "detected_at",
    "validated_at",
    "final_outcome_at",
    "final_outcome_reason",
    "order_send_attempted",
    "order_send_retcode",
    "order_send_retcode_text",
    "spread_at_validation",
    "spread_limit_used",
    "spread_limit_source",
    "cooldown_applied",
    "duplicate_guard_applied",
    "failure_stage",
    "metadata_json",
]

TRADE_COLUMNS = [
    "timestamp",
    "trade_id",
    "mt5_ticket",
    "position_id",
    "order_id",
    "event_type",
    "status",
    "symbol",
    "side",
    "setup",
    "setup_family",
    "setup_variant",
    "regime",
    "session",
    "entry_mode",
    "execution_reason",
    "blocked_reason",
    "close_reason",
    "signal_score",
    "trend_score",
    "setup_score",
    "trigger_score",
    "entry_score",
    "confidence",
    "entry_price",
    "stop_loss",
    "take_profit",
    "risk_amount",
    "risk_percent",
    "volume",
    "exit_price",
    "pnl",
    "pnl_pips",
    "fees",
    "swap",
    "commissions",
    "realized_r",
    "hold_minutes",
    "hold_seconds",
    "win_loss",
    "outcome_label",
    "bot_mode",
    "account_login",
    "magic_number",
    "timeframe_bias",
    "timeframe_setup",
    "timeframe_entry",
    "comment",
    "created_at",
    "updated_at",
    "closed_at",
    "journal_version",
    "unresolved_reason",
    "resolution_attempts",
    "last_resolution_attempt_at",
    "raw_mt5_json",
    "metadata_json",
    "note",
]

ANALYTICS_COLUMNS = [
    "timestamp",
    "day",
    "mode",
    "setups",
    "live_trades",
    "win_rate",
    "average_r",
    "average_hold_minutes",
    "long_trades",
    "short_trades",
    "by_setup_family_json",
    "by_regime_json",
    "by_session_json",
]

VALIDATION_REPORT_COLUMNS = [
    "timestamp",
    "day",
    "scope_type",
    "scope_name",
    "mode",
    "rollout_phase",
    "setups_by_family_json",
    "blocked_by_regime",
    "blocked_by_session",
    "blocked_by_anti_chase",
    "duplicate_entries_prevented",
    "live_eligible_setups",
    "paper_validated_setups",
    "journaling_errors",
    "top_block_reasons_json",
    "consistency_ok",
    "consistency_summary_json",
]


class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotating handler hardened for transient Windows file-lock rollover errors."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except PermissionError:
            # Root cause: Windows can briefly lock the file during rename/scan.
            # Mitigation: keep runtime alive, skip this rollover attempt, and write later.
            self.handleError(record)
        except OSError:
            self.handleError(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            if self.stream:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            try:
                # Reopen current file and continue without rotating this cycle.
                self.stream = self._open()
            except Exception:
                self.stream = None
            return


class BotLogger:
    """Centralized logger that writes console, rotating files, and CSV journals."""

    def __init__(self, base_path: str | Path, config: dict[str, Any]) -> None:
        self.base_path = Path(base_path)
        self.logs_dir = ensure_directory(self.base_path / "logs")
        self.config = config
        self.journaling_cfg = config.get("journaling", {})
        self._logger = logging.getLogger("mtf_sniper_bot")
        self._logger.setLevel(logging.INFO)
        self._logger.handlers.clear()
        self._logger.propagate = False
        self._setup_handlers()
        self.trades_path = self.logs_dir / "trades.csv"
        self.signals_path = self.logs_dir / "signals.csv"
        self.analytics_path = self.logs_dir / "analytics_daily.csv"
        self.validation_reports_path = self.logs_dir / "validation_daily.csv"
        self._csv_lock = RLock()
        self._ensure_csv_files()

    def _setup_handlers(self) -> None:
        """Configure file and console log handlers."""
        formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
        date_format = "%Y-%m-%d %H:%M:%S"
        logging_cfg = self.config.get("logging", {})
        max_bytes = int(logging_cfg.get("max_bytes", 2_000_000))
        backup_count = int(logging_cfg.get("backup_count", 5))

        bot_handler = SafeRotatingFileHandler(
            self.logs_dir / "bot.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        bot_handler.setLevel(logging.INFO)
        bot_handler.setFormatter(formatter)
        bot_handler.formatter.default_time_format = date_format  # type: ignore[attr-defined]
        bot_handler.formatter.converter = lambda *args: datetime.now(tz=UTC).timetuple()
        self._logger.addHandler(bot_handler)

        error_handler = SafeRotatingFileHandler(
            self.logs_dir / "errors.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        error_handler.formatter.default_time_format = date_format  # type: ignore[attr-defined]
        error_handler.formatter.converter = lambda *args: datetime.now(tz=UTC).timetuple()
        self._logger.addHandler(error_handler)

        if logging_cfg.get("console_output", True):
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            console_handler.formatter.default_time_format = date_format  # type: ignore[attr-defined]
            console_handler.formatter.converter = lambda *args: datetime.now(tz=UTC).timetuple()
            self._logger.addHandler(console_handler)

    def _csv_retry_settings(self) -> tuple[int, float]:
        """Return write retry settings for CSV journaling."""
        retry_count = max(1, int(self.journaling_cfg.get("csv_retry_count", 3)))
        retry_delay = max(0.0, float(self.journaling_cfg.get("csv_retry_delay_seconds", 0.25)))
        return retry_count, retry_delay

    def _read_existing_rows(self, path: Path) -> list[dict[str, Any]]:
        """Read all existing CSV rows if the file exists and is well-formed."""
        if not path.exists() or path.stat().st_size == 0:
            return []
        try:
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                return list(reader)
        except Exception:
            return []

    def _rewrite_csv(self, path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
        """Rewrite a CSV file using a canonical header."""
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                normalized = {column: row.get(column, "") for column in header}
                writer.writerow(normalized)

    def _migrate_csv_file(self, path: Path, header: list[str]) -> None:
        """Rewrite legacy CSVs into the current canonical schema when needed."""
        if not path.exists():
            self._rewrite_csv(path, header, [])
            return
        rows = self._read_existing_rows(path)
        existing_header = rows[0].keys() if rows else None
        if existing_header is not None and list(existing_header) == header:
            return
        self._rewrite_csv(path, header, rows)

    def _ensure_csv_files(self) -> None:
        """Create or migrate journal CSV files."""
        self._migrate_csv_file(self.trades_path, TRADE_COLUMNS)
        self._migrate_csv_file(self.signals_path, SIGNAL_COLUMNS)
        self._migrate_csv_file(self.analytics_path, ANALYTICS_COLUMNS)
        self._migrate_csv_file(self.validation_reports_path, VALIDATION_REPORT_COLUMNS)

    def _append_csv_row(self, path: Path, header: list[str], row: dict[str, Any]) -> None:
        """Append a CSV row with retry protection for transient Windows file locks."""
        retry_count, retry_delay = self._csv_retry_settings()
        normalized = {column: row.get(column, "") for column in header}
        last_error: OSError | None = None

        with self._csv_lock:
            for attempt in range(1, retry_count + 1):
                try:
                    file_exists = path.exists()
                    with path.open("a", newline="", encoding="utf-8") as handle:
                        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
                        if not file_exists or path.stat().st_size == 0:
                            writer.writeheader()
                        writer.writerow(normalized)
                    return
                except OSError as exc:
                    last_error = exc
                    if attempt < retry_count:
                        time.sleep(retry_delay * attempt)

        if last_error is not None:
            raise last_error

    @staticmethod
    def _jsonify(value: Any) -> str:
        """Serialize nested metadata into a stable JSON string."""
        if value in (None, "", {}):
            return ""
        return json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)

    def structured(self, category: str, payload: dict[str, Any], level: str = "INFO") -> None:
        """Write a JSON-style structured log record."""
        message = json.dumps({"category": category, **payload}, ensure_ascii=True, default=str, sort_keys=True)
        log_level = level.upper()
        if log_level == "WARNING":
            self.warning(message)
        elif log_level in {"ERROR", "CRITICAL"}:
            self.error(message)
        else:
            self.info(message)

    def info(self, message: str) -> None:
        """Log an informational message."""
        self._logger.info(message)

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self._logger.warning(message)

    def error(self, message: str) -> None:
        """Log an error message."""
        self._logger.error(message)

    def critical(self, message: str) -> None:
        """Log a critical message."""
        self._logger.critical(message)

    def trade_console(self, message: str) -> None:
        """Log a trade-prefixed message to the general logger."""
        self._logger.info(f"[TRADE] {message}")

    def signal_console(self, message: str) -> None:
        """Log a signal-prefixed message to the general logger."""
        self._logger.info(f"[SIGNAL] {message}")

    def log_signal_event(self, row: dict[str, Any]) -> None:
        """Append a structured signal or execution-attempt row to signals.csv."""
        if not self.config.get("logging", {}).get("log_signals", True):
            return
        payload = dict(row)
        payload["metadata_json"] = self._jsonify(payload.get("metadata_json") or payload.get("metadata"))
        self._append_csv_row(self.signals_path, SIGNAL_COLUMNS, payload)

    def log_trade_event(self, row: dict[str, Any]) -> None:
        """Append a structured trade lifecycle row to trades.csv."""
        if not self.config.get("logging", {}).get("log_trades", True):
            return
        payload = dict(row)
        payload["metadata_json"] = self._jsonify(payload.get("metadata_json") or payload.get("metadata"))
        self._append_csv_row(self.trades_path, TRADE_COLUMNS, payload)

    def log_analytics_summary(self, row: dict[str, Any]) -> None:
        """Append a daily analytics row."""
        if not self.config.get("logging", {}).get("daily_summary", True):
            return
        payload = dict(row)
        payload["by_setup_family_json"] = self._jsonify(payload.get("by_setup_family_json"))
        payload["by_regime_json"] = self._jsonify(payload.get("by_regime_json"))
        payload["by_session_json"] = self._jsonify(payload.get("by_session_json"))
        self._append_csv_row(self.analytics_path, ANALYTICS_COLUMNS, payload)

    def log_validation_summary(self, row: dict[str, Any]) -> None:
        """Append a daily/session validation report row."""
        if not self.config.get("validation", {}).get("enabled", True):
            return
        payload = dict(row)
        payload["setups_by_family_json"] = self._jsonify(payload.get("setups_by_family_json"))
        payload["top_block_reasons_json"] = self._jsonify(payload.get("top_block_reasons_json"))
        payload["consistency_summary_json"] = self._jsonify(payload.get("consistency_summary_json"))
        self._append_csv_row(self.validation_reports_path, VALIDATION_REPORT_COLUMNS, payload)

    def log_signal(
        self,
        timestamp: str,
        timeframe: str,
        signal_type: str,
        price: float,
        reason: str,
        executed: bool,
    ) -> None:
        """Backward-compatible wrapper for older signal logging calls."""
        self.log_signal_event(
            {
                "timestamp": timestamp,
                "event_type": "legacy_signal",
                "mode": "",
                "live_or_dry": "",
                "symbol": self.config.get("mt5", {}).get("symbol", ""),
                "side": signal_type,
                "timeframe": timeframe,
                "signal_type": signal_type,
                "price": f"{price:.5f}",
                "executed": int(executed),
                "reason": reason,
            }
        )

    def log_trade(self, row: dict[str, Any]) -> None:
        """Backward-compatible wrapper for older trade logging calls."""
        payload = dict(row)
        legacy_row = {
            "timestamp": payload.get("timestamp", ""),
            "trade_id": payload.get("trade_id") or payload.get("ticket", ""),
            "mt5_ticket": payload.get("mt5_ticket") or payload.get("ticket", ""),
            "position_id": payload.get("position_id") or payload.get("ticket", ""),
            "event_type": str(payload.get("event_type", "TRADE_EVENT")).upper(),
            "status": payload.get("status", ""),
            "symbol": payload.get("symbol", ""),
            "side": payload.get("type", ""),
            "setup": payload.get("setup", payload.get("setup_type", "")),
            "setup_family": payload.get("setup_family", payload.get("setup_type", "")),
            "setup_variant": payload.get("setup_variant", payload.get("trigger_type", "")),
            "regime": payload.get("regime", payload.get("regime_at_entry", "")),
            "session": payload.get("session", payload.get("session_at_entry", "")),
            "entry_mode": payload.get("entry_mode", ""),
            "execution_reason": payload.get("execution_reason", ""),
            "blocked_reason": payload.get("blocked_reason", ""),
            "close_reason": payload.get("close_reason", ""),
            "entry_price": payload.get("entry_price", payload.get("entry", "")),
            "stop_loss": payload.get("stop_loss", payload.get("sl", "")),
            "take_profit": payload.get("take_profit", payload.get("tp", "")),
            "volume": payload.get("volume", payload.get("lot_size", "")),
            "pnl": payload.get("pnl", ""),
            "pnl_pips": payload.get("pnl_pips", ""),
            "realized_r": payload.get("realized_r", ""),
            "win_loss": payload.get("win_loss", ""),
            "outcome_label": payload.get("outcome_label", ""),
            "comment": payload.get("comment", ""),
            "closed_at": payload.get("closed_at", ""),
            "metadata_json": payload.get("metadata_json", payload),
            "note": payload.get("note", ""),
        }
        self.log_trade_event(legacy_row)

    def log_daily_summary(self, state: dict[str, Any]) -> None:
        """Log a compact day summary to the main bot log."""
        total = int(state.get("daily_trade_count", 0))
        wins = int(state.get("winning_trades", 0))
        losses = int(state.get("losing_trades", 0))
        total_closed = int(state.get("total_trades", 0))
        win_rate = (wins / total_closed * 100) if total_closed else 0.0
        self.info(
            "Daily Summary | "
            f"Trades: {total} | Wins: {wins} ({win_rate:.2f}%) | "
            f"Losses: {losses} | "
            f"Daily PnL: {float(state.get('daily_pnl', 0.0)):.2f} | "
            f"Total PnL: {float(state.get('total_pnl', 0.0)):.2f}"
        )
