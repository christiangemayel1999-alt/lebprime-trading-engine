"""SQLite persistence layer for bot telemetry, signals, trades, and analytics."""

from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from trading_bot.core.job_state import JobState, normalize_job_state
from trading_bot.core.job_types import JobType, normalize_job_type
from utils import ensure_directory, utc_now


def row_to_dict(row: Any) -> dict[str, Any] | None:
    """Convert a SQLite row-like object into a plain dictionary."""
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Any) -> list[dict[str, Any]]:
    """Convert an iterable of SQLite row-like objects into dictionaries."""
    return [dict(r) for r in rows] if rows else []


class DatabaseService:
    """SQLite service with lightweight schema migration support."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        ensure_directory(self.db_path.parent)
        self._lock = RLock()
        self._runtime_logger = logging.getLogger("mtf_sniper_bot")
        self._initialize()
        self.repair_trade_lifecycle_if_needed()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Create a SQLite connection with row support and close it deterministically."""
        connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA synchronous=NORMAL;")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _table_columns(self, connection: sqlite3.Connection, table_name: str) -> set[str]:
        """Return the existing column names for a table."""
        rows = connection.execute(f"PRAGMA table_info({table_name});").fetchall()
        return {str(row["name"]) for row in rows}

    def _table_info(self, connection: sqlite3.Connection, table_name: str) -> dict[str, dict[str, Any]]:
        """Return PRAGMA table info keyed by column name."""
        rows = connection.execute(f"PRAGMA table_info({table_name});").fetchall()
        return {str(row["name"]): dict(row) for row in rows}

    def _ensure_columns(self, connection: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
        """Add missing columns to an existing table."""
        existing = self._table_columns(connection, table_name)
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition};")

    @staticmethod
    def _scalar(connection: sqlite3.Connection, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        """Return the first column from a scalar query."""
        row = connection.execute(statement, parameters).fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return row[0]
        return row[0] if isinstance(row, (tuple, list)) else row

    def _get_maintenance_value(self, connection: sqlite3.Connection, key: str, default: str = "") -> str:
        """Read a maintenance-state value."""
        value = self._scalar(
            connection,
            "SELECT value FROM maintenance_state WHERE key = ? LIMIT 1;",
            (str(key),),
        )
        return str(value) if value not in (None, "") else default

    def _set_maintenance_value(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        """Persist a maintenance-state value."""
        connection.execute(
            """
            INSERT INTO maintenance_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
            """,
            (str(key), "" if value is None else str(value), utc_now().isoformat()),
        )

    def _initialize(self) -> None:
        """Create tables and indexes when the database is first used."""
        base_statements = [
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trade_id TEXT,
                mt5_ticket TEXT,
                position_id TEXT,
                order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                event_type TEXT DEFAULT 'TRADE_EVENT',
                setup TEXT,
                setup_family TEXT,
                setup_variant TEXT,
                regime TEXT,
                session TEXT,
                entry_mode TEXT,
                execution_reason TEXT,
                blocked_reason TEXT,
                close_reason TEXT,
                signal_score REAL,
                trend_score REAL,
                setup_score REAL,
                trigger_score REAL,
                entry_score REAL,
                confidence REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                risk_amount REAL,
                risk_percent REAL,
                volume REAL,
                exit_price REAL,
                pnl REAL,
                pnl_pips REAL,
                fees REAL,
                swap REAL,
                commissions REAL,
                realized_r REAL,
                hold_minutes REAL,
                hold_seconds REAL,
                win_loss TEXT,
                outcome_label TEXT,
                bot_mode TEXT,
                account_login TEXT,
                magic_number INTEGER,
                timeframe_bias TEXT,
                timeframe_setup TEXT,
                timeframe_entry TEXT,
                comment TEXT,
                created_at TEXT,
                updated_at TEXT,
                closed_at TEXT,
                journal_version TEXT,
                unresolved_reason TEXT,
                resolution_attempts INTEGER,
                last_resolution_attempt_at TEXT,
                raw_mt5_json TEXT,
                metadata_json TEXT,
                note TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trade_id TEXT,
                mt5_ticket TEXT,
                position_id TEXT,
                order_id TEXT,
                event_type TEXT NOT NULL,
                status TEXT,
                symbol TEXT,
                side TEXT,
                setup TEXT,
                setup_family TEXT,
                regime TEXT,
                session TEXT,
                entry_price REAL,
                exit_price REAL,
                stop_loss REAL,
                take_profit REAL,
                volume REAL,
                pnl REAL,
                realized_r REAL,
                execution_reason TEXT,
                blocked_reason TEXT,
                close_reason TEXT,
                outcome_label TEXT,
                comment TEXT,
                journal_version TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trend_ok INTEGER NOT NULL,
                setup_ok INTEGER NOT NULL,
                entry_ok INTEGER NOT NULL,
                executed INTEGER NOT NULL,
                reason TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS heartbeat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                symbol TEXT NOT NULL,
                spread REAL,
                trend TEXT,
                setup TEXT,
                balance REAL,
                equity REAL,
                open_positions INTEGER NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                day TEXT NOT NULL,
                mode TEXT NOT NULL,
                setups INTEGER NOT NULL,
                live_trades INTEGER NOT NULL,
                win_rate REAL,
                average_r REAL,
                average_hold_minutes REAL,
                long_trades INTEGER NOT NULL,
                short_trades INTEGER NOT NULL,
                by_setup_family_json TEXT,
                by_regime_json TEXT,
                by_session_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS validation_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                day TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_name TEXT NOT NULL,
                mode TEXT NOT NULL,
                rollout_phase INTEGER NOT NULL,
                setups_by_family_json TEXT,
                blocked_by_regime INTEGER NOT NULL,
                blocked_by_session INTEGER NOT NULL,
                blocked_by_anti_chase INTEGER NOT NULL,
                duplicate_entries_prevented INTEGER NOT NULL,
                live_eligible_setups INTEGER NOT NULL,
                paper_validated_setups INTEGER NOT NULL,
                journaling_errors INTEGER NOT NULL,
                top_block_reasons_json TEXT,
                consistency_ok INTEGER NOT NULL,
                consistency_summary_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                level TEXT NOT NULL,
                details_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS maintenance_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                enabled_strategies_json TEXT,
                session_filter_json TEXT,
                initial_balance REAL NOT NULL,
                risk_percent REAL NOT NULL,
                spread_model TEXT,
                slippage_model TEXT,
                execution_model TEXT,
                notes TEXT,
                state TEXT DEFAULT 'QUEUED',
                started_at TEXT,
                updated_at TEXT,
                finished_at TEXT,
                last_heartbeat_at TEXT,
                failure_reason TEXT,
                interrupted_reason TEXT,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                artifacts_json TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                signal_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                setup TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                score REAL,
                executed INTEGER NOT NULL,
                execution_reason TEXT,
                blocked_reason TEXT,
                raw_signal_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trade_id TEXT,
                position_id TEXT,
                order_id TEXT,
                setup_family TEXT,
                setup_fingerprint TEXT,
                open_time TEXT NOT NULL,
                close_time TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                pnl_r REAL NOT NULL,
                exit_reason TEXT,
                exit_reason_code TEXT,
                mfe REAL,
                mae REAL,
                duration_seconds REAL,
                volume REAL,
                volume_initial REAL,
                volume_remaining_at_close REAL,
                realized_pnl_total REAL,
                partial_realized_pnl REAL,
                final_realized_pnl REAL,
                commission REAL,
                swap REAL,
                balance_before REAL,
                balance_after REAL,
                equity_before REAL,
                equity_after REAL,
                trade_metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                equity REAL NOT NULL,
                balance REAL NOT NULL,
                floating_pnl REAL NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS history_cache_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                tick_volume REAL NOT NULL DEFAULT 0,
                spread REAL NOT NULL DEFAULT 0,
                real_volume REAL NOT NULL DEFAULT 0,
                source_kind TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(symbol, timeframe, time)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS history_cache_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                time TEXT NOT NULL,
                bid REAL,
                ask REAL,
                last REAL,
                volume REAL,
                spread REAL,
                flags INTEGER,
                source_kind TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(symbol, time)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_playback_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                bar_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                tick_volume REAL,
                spread REAL,
                equity REAL,
                balance REAL,
                floating_pnl REAL,
                open_positions_json TEXT,
                pending_orders_json TEXT,
                selected_candidate_json TEXT,
                candidate_summary_json TEXT,
                state_json TEXT,
                UNIQUE(run_id, bar_index)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_playback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                bar_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                position_id TEXT,
                signal_id TEXT,
                strategy_name TEXT,
                setup_family TEXT,
                side TEXT,
                price REAL,
                reason_code TEXT,
                event_payload_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS oos_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                state TEXT NOT NULL DEFAULT 'QUEUED',
                last_heartbeat_at TEXT,
                title TEXT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                request_json TEXT,
                baseline_run_id INTEGER,
                baseline_bundle_path TEXT,
                baseline_database_path TEXT,
                output_dir TEXT,
                report_status TEXT DEFAULT 'pending',
                report_path TEXT,
                report_error TEXT,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                failure_reason TEXT,
                interrupted_reason TEXT,
                notes TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS oos_run_scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                oos_run_id INTEGER NOT NULL,
                scenario_index INTEGER NOT NULL,
                scenario_key TEXT NOT NULL,
                scenario_label TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                run_id INTEGER,
                export_path TEXT,
                bundle_path TEXT,
                report_path TEXT,
                warning_json TEXT,
                error TEXT,
                metadata_json TEXT,
                UNIQUE(oos_run_id, scenario_key)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                last_heartbeat_at TEXT,
                job_type TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'QUEUED',
                payload_json TEXT,
                result_json TEXT,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                failure_reason TEXT,
                interrupted_reason TEXT,
                cancel_requested INTEGER DEFAULT 0,
                worker_id TEXT,
                parent_job_id INTEGER,
                related_run_type TEXT,
                related_run_id INTEGER,
                claim_token TEXT,
                claim_expires_at TEXT,
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                metadata_json TEXT
            );
            """,
        ]
        trade_columns = {
            "trade_id": "TEXT",
            "mt5_ticket": "TEXT",
            "position_id": "TEXT",
            "ticket": "TEXT",
            "order_id": "TEXT",
            "setup": "TEXT",
            "setup_fingerprint": "TEXT",
            "setup_family": "TEXT",
            "setup_variant": "TEXT",
            "setup_type": "TEXT",
            "regime": "TEXT",
            "regime_at_entry": "TEXT",
            "session": "TEXT",
            "session_at_entry": "TEXT",
            "entry_mode": "TEXT",
            "execution_reason": "TEXT",
            "blocked_reason": "TEXT",
            "close_reason": "TEXT",
            "signal_score": "REAL",
            "trend_score": "REAL",
            "setup_score": "REAL",
            "trigger_score": "REAL",
            "entry_score": "REAL",
            "confidence": "REAL",
            "entry": "REAL",
            "entry_price": "REAL",
            "sl": "REAL",
            "stop_loss": "REAL",
            "tp": "REAL",
            "take_profit": "REAL",
            "risk_amount": "REAL",
            "risk_percent": "REAL",
            "volume": "REAL",
            "requested_volume": "REAL",
            "final_volume": "REAL",
            "exit_price": "REAL",
            "pnl": "REAL",
            "exit_reason_code": "TEXT",
            "pnl_pips": "REAL",
            "fees": "REAL",
            "swap": "REAL",
            "commissions": "REAL",
            "realized_r": "REAL",
            "hold_minutes": "REAL",
            "hold_seconds": "REAL",
            "win_loss": "TEXT",
            "outcome_label": "TEXT",
            "bot_mode": "TEXT",
            "account_login": "TEXT",
            "magic_number": "INTEGER",
            "timeframe_bias": "TEXT",
            "timeframe_setup": "TEXT",
            "timeframe_entry": "TEXT",
            "comment": "TEXT",
            "entry_time": "TEXT",
            "exit_time": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
            "closed_at": "TEXT",
            "mode": "TEXT",
            "journal_version": "TEXT",
            "unresolved_reason": "TEXT",
            "resolution_attempts": "INTEGER",
            "last_resolution_attempt_at": "TEXT",
            "raw_mt5_json": "TEXT",
            "resolution_stage": "TEXT",
            "resolution_metadata_json": "TEXT",
            "resolution_started_at": "TEXT",
            "resolution_last_evidence_at": "TEXT",
            "matched_order_ids": "TEXT",
            "matched_deal_ids": "TEXT",
            "matched_volume": "REAL",
            "reconciliation_confidence": "REAL",
            "close_price_source": "TEXT",
            "lifecycle_version": "INTEGER DEFAULT 0",
            "metadata_json": "TEXT",
            "event_type": "TEXT DEFAULT 'TRADE_EVENT'",
            "status": "TEXT",
            "timestamp": "TEXT",
            "side": "TEXT",
            "symbol": "TEXT",
            "note": "TEXT",
        }
        signal_columns = {
            "event_type": "TEXT DEFAULT 'signal'",
            "mode": "TEXT",
            "live_or_dry": "TEXT",
            "timeframe": "TEXT",
            "signal_type": "TEXT",
            "setup_fingerprint": "TEXT",
            "setup_family": "TEXT",
            "setup_anchor_time": "TEXT",
            "trigger_type": "TEXT",
            "price": "REAL",
            "setup_price": "REAL",
            "value_price": "REAL",
            "entry_price": "REAL",
            "sl": "REAL",
            "tp": "REAL",
            "spread_points": "REAL",
            "requested_volume": "REAL",
            "final_volume": "REAL",
            "entry_mode": "TEXT",
            "regime_name": "TEXT",
            "regime_confidence": "REAL",
            "session_name": "TEXT",
            "session_live_allowed": "INTEGER",
            "reason_code": "TEXT",
            "trend_score": "REAL",
            "setup_score": "REAL",
            "trigger_score": "REAL",
            "entry_score": "REAL",
            "metadata_json": "TEXT",
            "attempt_id": "TEXT",
            "detected_at": "TEXT",
            "validated_at": "TEXT",
            "final_outcome_at": "TEXT",
            "final_outcome_reason": "TEXT",
            "order_send_attempted": "INTEGER",
            "order_send_retcode": "INTEGER",
            "order_send_retcode_text": "TEXT",
            "spread_at_validation": "REAL",
            "spread_limit_used": "REAL",
            "spread_limit_source": "TEXT",
            "cooldown_applied": "INTEGER",
            "duplicate_guard_applied": "INTEGER",
            "failure_stage": "TEXT",
        }
        event_columns = {
            "reason_code": "TEXT",
            "details_json": "TEXT",
        }
        heartbeat_columns = {
            "regime": "TEXT",
            "session": "TEXT",
            "reason_code": "TEXT",
            "mode": "TEXT",
        }

        with self._connect() as connection:
            for statement in base_statements:
                connection.execute(statement)
            self._ensure_columns(connection, "trades", trade_columns)
            self._ensure_columns(connection, "trade_events", {
                "trade_id": "TEXT",
                "mt5_ticket": "TEXT",
                "position_id": "TEXT",
                "ticket": "TEXT",
                "order_id": "TEXT",
                "status": "TEXT",
                "symbol": "TEXT",
                "side": "TEXT",
                "setup": "TEXT",
                "setup_fingerprint": "TEXT",
                "setup_family": "TEXT",
                "setup_type": "TEXT",
                "regime": "TEXT",
                "regime_at_entry": "TEXT",
                "session": "TEXT",
                "session_at_entry": "TEXT",
                "entry": "REAL",
                "entry_price": "REAL",
                "exit_price": "REAL",
                "sl": "REAL",
                "stop_loss": "REAL",
                "tp": "REAL",
                "take_profit": "REAL",
                "volume": "REAL",
                "requested_volume": "REAL",
                "final_volume": "REAL",
                "pnl": "REAL",
                "realized_r": "REAL",
                "execution_reason": "TEXT",
                "blocked_reason": "TEXT",
                "close_reason": "TEXT",
                "outcome_label": "TEXT",
                "comment": "TEXT",
                "entry_time": "TEXT",
                "exit_time": "TEXT",
                "mode": "TEXT",
                "journal_version": "TEXT",
                "metadata_json": "TEXT",
                "event_key": "TEXT",
                "event_bucket": "TEXT",
                "lifecycle_version": "INTEGER DEFAULT 0",
                "resolution_stage": "TEXT",
                "matched_order_ids": "TEXT",
                "matched_deal_ids": "TEXT",
                "matched_volume": "REAL",
                "reconciliation_confidence": "REAL",
                "resolution_metadata_json": "TEXT",
            })
            self._ensure_columns(connection, "signals", signal_columns)
            self._ensure_columns(connection, "bot_events", event_columns)
            self._ensure_columns(connection, "heartbeat", heartbeat_columns)
            self._ensure_columns(connection, "backtest_trades", {
                "trade_id": "TEXT",
                "position_id": "TEXT",
                "order_id": "TEXT",
                "setup_family": "TEXT",
                "setup_fingerprint": "TEXT",
                "exit_reason_code": "TEXT",
                "volume": "REAL",
                "volume_initial": "REAL",
                "volume_remaining_at_close": "REAL",
                "realized_pnl_total": "REAL",
                "partial_realized_pnl": "REAL",
                "final_realized_pnl": "REAL",
                "commission": "REAL",
                "swap": "REAL",
                "balance_before": "REAL",
                "balance_after": "REAL",
                "equity_before": "REAL",
                "equity_after": "REAL",
            })
            self._ensure_columns(connection, "backtest_runs", {
                "state": "TEXT DEFAULT 'QUEUED'",
                "started_at": "TEXT",
                "updated_at": "TEXT",
                "finished_at": "TEXT",
                "last_heartbeat_at": "TEXT",
                "failure_reason": "TEXT",
                "interrupted_reason": "TEXT",
                "progress_current": "INTEGER DEFAULT 0",
                "progress_total": "INTEGER DEFAULT 0",
                "artifacts_json": "TEXT",
                "metadata_json": "TEXT",
            })
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_mt5_ticket ON trades(mt5_ticket);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_event_type ON trades(event_type);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades(setup);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_timestamp ON trade_events(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_trade_id ON trade_events(trade_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_mt5_ticket ON trade_events(mt5_ticket);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_event_type ON trade_events(event_type);")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_events_event_key ON trade_events(event_key) WHERE event_key IS NOT NULL AND event_key != '';")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_setup_fingerprint ON signals(setup_fingerprint);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON bot_events(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat_timestamp ON heartbeat(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_daily_analytics_day ON daily_analytics(day);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_validation_reports_day ON validation_reports(day, scope_type, scope_name);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at ON backtest_runs(created_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_signals_run_id ON backtest_signals(run_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_signals_time ON backtest_signals(signal_time);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_id ON backtest_trades(run_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_trades_open_close ON backtest_trades(open_time, close_time);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_equity_run_id ON backtest_equity_curve(run_id);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_cache_bars_symbol_timeframe_time ON history_cache_bars(symbol, timeframe, time);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_cache_ticks_symbol_time ON history_cache_ticks(symbol, time);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_playback_frames_run_bar ON backtest_playback_frames(run_id, bar_index);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_playback_events_run_bar ON backtest_playback_events(run_id, bar_index);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_state ON backtest_runs(state, updated_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_oos_runs_created_at ON oos_runs(created_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_oos_runs_state ON oos_runs(state, updated_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_oos_scenarios_run_id ON oos_run_scenarios(oos_run_id, scenario_index);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_oos_scenarios_status ON oos_run_scenarios(status, updated_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state_created_at ON jobs(state, created_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_type_state ON jobs(job_type, state, updated_at);")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_related_run ON jobs(related_run_type, related_run_id);")
            connection.commit()

    def _execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        """Run a write statement safely under a lock."""
        with self._lock:
            with self._connect() as connection:
                connection.execute(statement, parameters)
                connection.commit()

    def _query_dataframe(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a query and return rows."""
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(statement, parameters)
                return rows_to_dicts(cursor.fetchall())

    @staticmethod
    def _json_text(value: Any) -> str:
        """Serialize a JSON payload for storage."""
        if value in (None, "", {}, []):
            return ""
        return json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)

    @staticmethod
    def _trade_outcome(pnl: Any, win_loss: Any | None = None) -> str:
        """Map PnL into a normalized outcome label."""
        if win_loss:
            normalized = str(win_loss).strip().upper()
            if normalized in {"WIN", "LOSS", "BREAKEVEN", "UNKNOWN"}:
                return normalized
        try:
            pnl_value = float(pnl)
        except Exception:
            return "UNKNOWN"
        if abs(pnl_value) <= 1e-8:
            return "BREAKEVEN"
        return "WIN" if pnl_value > 0 else "LOSS"

    @staticmethod
    def _safe_iso_timestamp(*candidates: Any) -> str:
        """Return the first non-empty timestamp candidate, else UTC now."""
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            text = str(candidate).strip()
            if text:
                return text
        return utc_now().isoformat()

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Return stripped text or None."""
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @classmethod
    def _is_placeholder_text(cls, value: Any) -> bool:
        """Return whether a textual value is blank or only a placeholder."""
        text = cls._clean_text(value)
        return text is None or text.lower() in {"unknown", "n/a", "none", "null", "pending", "trade", "info"}

    @classmethod
    def _has_value(cls, value: Any) -> bool:
        """Return whether a value should be treated as meaningful."""
        if value is None:
            return False
        if isinstance(value, str):
            return not cls._is_placeholder_text(value)
        if isinstance(value, (dict, list, tuple, set)):
            return bool(value)
        return True

    @staticmethod
    def _json_object(value: Any) -> Any:
        """Return a JSON-friendly object from dict/list/string input."""
        if value in (None, "", {}, []):
            return {}
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                return json.loads(text)
            except Exception:
                return {"raw": value}
        return {"raw": value}

    @classmethod
    def _merge_json_values(cls, existing: Any, incoming: Any) -> str:
        """Merge JSON-ish values conservatively and serialize them."""
        left = cls._json_object(existing)
        right = cls._json_object(incoming)
        if isinstance(left, dict) and isinstance(right, dict):
            merged = dict(left)
            for key, value in right.items():
                if cls._has_value(value):
                    merged[key] = value
            return cls._json_text(merged)
        if isinstance(right, list) and right:
            return cls._json_text(right)
        if isinstance(left, list) and left:
            return cls._json_text(left)
        if cls._has_value(right):
            return cls._json_text(right)
        return cls._json_text(left)

    @classmethod
    def _canonical_trade_status(cls, status: Any, event_type: Any | None = None) -> str:
        """Normalize any lifecycle status into the canonical trade-row vocabulary."""
        raw_status = str(status or "").strip().upper()
        raw_event = str(event_type or "").strip().upper()
        if raw_status in {"MERGED_DUPLICATE", "IGNORED_ORPHAN"}:
            return raw_status
        mapping = {
            "OPEN": "OPENED",
            "OPENED": "OPENED",
            "TRADE_OPENED": "OPENED",
            "TRADE_MANAGED": "OPENED",
            "MANAGED": "OPENED",
            "PENDING_CLOSE_RESOLUTION": "PENDING_CLOSE_RESOLUTION",
            "CLOSE_HISTORY_PENDING": "CLOSE_HISTORY_PENDING",
            "CLOSE_HISTORY_PARTIAL": "CLOSE_HISTORY_PARTIAL",
            "FAILED_CLOSE_RESOLUTION": "FAILED_CLOSE_RESOLUTION",
            "CLOSE": "CLOSED",
            "CLOSED": "CLOSED",
            "TRADE_CLOSED": "CLOSED",
            "TRADE_FINALIZED": "FINALIZED",
            "FINALIZED": "FINALIZED",
            "CLOSED_UNVERIFIED": "CLOSED_UNVERIFIED",
        }
        if raw_status in mapping:
            return mapping[raw_status]
        if raw_event in mapping:
            return mapping[raw_event]
        if raw_event == "TRADE_FINALIZED":
            return "FINALIZED"
        if raw_event in {"TRADE_CLOSED", "CLOSE"}:
            return "CLOSED"
        return raw_status or "OPENED"

    @classmethod
    def _canonical_trade_event(cls, event_type: Any, status: Any | None = None) -> str:
        """Normalize lifecycle event names for the canonical trade row."""
        raw_event = str(event_type or "").strip().upper()
        normalized_status = cls._canonical_trade_status(status, raw_event)
        mapping = {
            "OPEN": "TRADE_OPENED",
            "TRADE_OPENED": "TRADE_OPENED",
            "TRADE_MANAGED": "TRADE_MANAGED",
            "CLOSE": "TRADE_CLOSED",
            "TRADE_CLOSED": "TRADE_CLOSED",
            "TRADE_FINALIZED": "TRADE_FINALIZED",
        }
        if raw_event in mapping:
            return mapping[raw_event]
        if normalized_status == "FINALIZED":
            return "TRADE_FINALIZED"
        if normalized_status in {"CLOSED", "CLOSED_UNVERIFIED", "FAILED_CLOSE_RESOLUTION", "PENDING_CLOSE_RESOLUTION"}:
            return "TRADE_CLOSED"
        if normalized_status == "OPENED":
            return "TRADE_OPENED"
        return raw_event or "TRADE_EVENT"

    @staticmethod
    def _trade_status_rank(status: Any) -> int:
        """Rank statuses so richer lifecycle states win during merges."""
        normalized = str(status or "").strip().upper()
        return {
            "IGNORED_ORPHAN": -10,
            "MERGED_DUPLICATE": -5,
            "OPENED": 10,
            "PENDING_CLOSE_RESOLUTION": 20,
            "CLOSE_HISTORY_PENDING": 22,
            "CLOSE_HISTORY_PARTIAL": 25,
            "FAILED_CLOSE_RESOLUTION": 30,
            "CLOSED_UNVERIFIED": 35,
            "CLOSED": 40,
            "FINALIZED": 50,
        }.get(normalized, 0)

    @classmethod
    def _merge_trade_status(cls, existing: Any, incoming: Any) -> str:
        """Prefer the more advanced lifecycle status."""
        existing_status = cls._canonical_trade_status(existing)
        incoming_status = cls._canonical_trade_status(incoming)
        return incoming_status if cls._trade_status_rank(incoming_status) >= cls._trade_status_rank(existing_status) else existing_status

    @classmethod
    def _generate_trade_identity(cls, row: dict[str, Any]) -> str:
        """Generate a deterministic fallback trade identity when no stable ids exist."""
        basis = [
            cls._clean_text(row.get("setup_fingerprint") or row.get("setup")),
            cls._clean_text(row.get("setup_family")),
            cls._clean_text(row.get("symbol")),
            cls._clean_text(row.get("side")),
            cls._clean_text(row.get("entry_time") or row.get("created_at") or row.get("timestamp")),
            cls._clean_text(row.get("entry_price") or row.get("entry")),
        ]
        seed = "|".join(item or "" for item in basis)
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        return f"GEN-{digest}"

    @classmethod
    def _lifecycle_version(cls, status: Any, event_type: Any | None = None) -> int:
        """Map lifecycle state to a monotonic version for idempotent transitions."""
        normalized_status = cls._canonical_trade_status(status, event_type)
        normalized_event = cls._canonical_trade_event(event_type, normalized_status)
        if normalized_event == "ORDER_SUBMITTED":
            return 5
        if normalized_event == "TRADE_OPENED":
            return 10
        if normalized_event == "TRADE_MANAGED":
            return 15
        return {
            "OPENED": 10,
            "PENDING_CLOSE_RESOLUTION": 20,
            "CLOSE_HISTORY_PENDING": 22,
            "CLOSE_HISTORY_PARTIAL": 25,
            "FAILED_CLOSE_RESOLUTION": 30,
            "CLOSED_UNVERIFIED": 35,
            "CLOSED": 40,
            "FINALIZED": 50,
        }.get(normalized_status, 0)

    @classmethod
    def _event_identity_key(cls, row: dict[str, Any]) -> str:
        """Generate a stable idempotency key for append-only lifecycle events."""
        event_type = cls._canonical_trade_event(row.get("event_type"), row.get("status"))
        lifecycle_version = cls._lifecycle_version(row.get("status"), event_type)
        stable_identity = cls._clean_text(
            row.get("trade_id")
            or row.get("mt5_ticket")
            or row.get("position_id")
            or row.get("ticket")
            or row.get("order_id")
            or row.get("setup_fingerprint")
        )
        if not stable_identity:
            return ""
        bucket = cls._clean_text(row.get("event_bucket"))
        if not bucket:
            bucket = f"v{lifecycle_version}"
        return f"{event_type}|{stable_identity}|{bucket}"

    @classmethod
    def _trade_identity_candidates(cls, row: dict[str, Any], include_generated: bool = True) -> list[str]:
        """Return identity candidates in canonical priority order."""
        candidates: list[str] = []
        for key in ("trade_id", "position_id", "mt5_ticket", "ticket", "order_id"):
            value = cls._clean_text(row.get(key))
            if value and value not in candidates:
                candidates.append(value)
        if include_generated and not candidates:
            generated = cls._generate_trade_identity(row)
            if generated:
                candidates.append(generated)
        return candidates

    @classmethod
    def _trade_row_quality(cls, row: dict[str, Any]) -> int:
        """Score how useful a row is so duplicate clusters can pick one canonical record."""
        score = 0
        for key in (
            "trade_id", "position_id", "mt5_ticket", "ticket", "order_id", "setup", "setup_family", "regime", "session",
            "entry_price", "exit_price", "pnl", "realized_r", "closed_at",
        ):
            if cls._has_value(row.get(key)):
                score += 3
        score += cls._trade_status_rank(row.get("status"))
        return score

    def _find_trade_matches(self, connection: sqlite3.Connection, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Return every trade row matching any known identity field."""
        values = self._trade_identity_candidates(row, include_generated=False)
        if not values:
            generated = self._clean_text(row.get("trade_id"))
            if generated:
                values = [generated]
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        statement = f"""
            SELECT *
            FROM trades
            WHERE trade_id IN ({placeholders})
               OR position_id IN ({placeholders})
               OR mt5_ticket IN ({placeholders})
               OR ticket IN ({placeholders})
               OR order_id IN ({placeholders})
            ORDER BY id ASC;
        """
        parameters = tuple(values * 5)
        return rows_to_dicts(connection.execute(statement, parameters).fetchall())

    def _select_canonical_trade_row(self, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the best existing row from a duplicate cluster."""
        if not rows:
            return None
        return sorted(
            rows,
            key=lambda item: (
                self._trade_row_quality(item),
                self._trade_status_rank(item.get("status")),
                self._safe_iso_timestamp(item.get("updated_at"), item.get("closed_at"), item.get("timestamp")),
                int(item.get("id", 0) or 0),
            ),
            reverse=True,
        )[0]

    def _normalize_trade_identity_fields(self, row: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        """Populate canonical and legacy identity aliases consistently."""
        normalized = dict(row)
        current = existing or {}

        trade_id = self._clean_text(normalized.get("trade_id")) or self._clean_text(current.get("trade_id"))
        position_id = self._clean_text(normalized.get("position_id")) or self._clean_text(current.get("position_id"))
        mt5_ticket = self._clean_text(normalized.get("mt5_ticket")) or self._clean_text(current.get("mt5_ticket"))
        ticket = self._clean_text(normalized.get("ticket")) or self._clean_text(current.get("ticket"))
        order_id = self._clean_text(normalized.get("order_id")) or self._clean_text(current.get("order_id"))

        stable_identity = trade_id or position_id or mt5_ticket or ticket or order_id
        if not stable_identity:
            stable_identity = self._generate_trade_identity({**current, **normalized})
        if not trade_id:
            trade_id = stable_identity
        if not position_id:
            position_id = ticket or mt5_ticket or current.get("position_id") or stable_identity
        if not mt5_ticket:
            mt5_ticket = ticket or current.get("mt5_ticket") or ""
        if not ticket:
            ticket = mt5_ticket or position_id or trade_id

        normalized["trade_id"] = trade_id
        normalized["position_id"] = self._clean_text(position_id) or ""
        normalized["mt5_ticket"] = self._clean_text(mt5_ticket) or ""
        normalized["ticket"] = self._clean_text(ticket) or trade_id
        normalized["order_id"] = self._clean_text(order_id) or ""
        return normalized

    def _normalize_trade_row(self, row: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        """Normalize a trade payload across legacy and canonical column names."""
        normalized = dict(row)
        current = existing or {}
        normalized = self._normalize_trade_identity_fields(normalized, current)

        normalized_status = self._merge_trade_status(current.get("status"), normalized.get("status") or normalized.get("event_type"))
        normalized_event = self._canonical_trade_event(normalized.get("event_type"), normalized_status)

        normalized["timestamp"] = self._safe_iso_timestamp(
            normalized.get("timestamp"),
            normalized.get("created_at"),
            normalized.get("entry_time"),
            normalized.get("updated_at"),
            normalized.get("closed_at"),
            current.get("timestamp"),
        )
        normalized["created_at"] = self._safe_iso_timestamp(
            current.get("created_at"),
            normalized.get("created_at"),
            normalized.get("entry_time"),
            normalized.get("timestamp"),
        )
        normalized["updated_at"] = self._safe_iso_timestamp(
            normalized.get("updated_at"),
            normalized.get("closed_at"),
            normalized.get("timestamp"),
            current.get("updated_at"),
        )
        if self._trade_status_rank(normalized_status) >= self._trade_status_rank("CLOSED"):
            normalized["closed_at"] = self._safe_iso_timestamp(
                normalized.get("closed_at"),
                normalized.get("exit_time"),
                normalized.get("updated_at"),
                current.get("closed_at"),
            )
        else:
            normalized["closed_at"] = self._clean_text(normalized.get("closed_at")) or self._clean_text(current.get("closed_at")) or ""

        normalized["status"] = normalized_status
        normalized["event_type"] = normalized_event
        normalized["symbol"] = self._clean_text(normalized.get("symbol")) or self._clean_text(current.get("symbol")) or "UNKNOWN"
        normalized["side"] = self._clean_text(normalized.get("side") or normalized.get("direction")) or self._clean_text(current.get("side")) or "UNKNOWN"
        normalized["mode"] = self._clean_text(normalized.get("mode") or normalized.get("bot_mode")) or self._clean_text(current.get("mode") or current.get("bot_mode")) or "UNKNOWN"
        normalized["bot_mode"] = self._clean_text(normalized.get("bot_mode") or normalized.get("mode")) or self._clean_text(current.get("bot_mode") or current.get("mode")) or normalized["mode"]

        normalized["setup"] = self._clean_text(normalized.get("setup") or normalized.get("setup_fingerprint")) or self._clean_text(current.get("setup")) or ""
        normalized["setup_fingerprint"] = self._clean_text(normalized.get("setup_fingerprint") or normalized.get("setup")) or self._clean_text(current.get("setup_fingerprint")) or normalized["setup"]
        normalized["setup_family"] = self._clean_text(normalized.get("setup_family")) or self._clean_text(current.get("setup_family")) or self._clean_text(normalized.get("setup")) or ""
        normalized["setup_variant"] = self._clean_text(normalized.get("setup_variant") or normalized.get("setup_type") or normalized.get("trigger_type")) or self._clean_text(current.get("setup_variant")) or ""
        normalized["setup_type"] = self._clean_text(normalized.get("setup_type") or normalized.get("setup_variant") or normalized.get("trigger_type")) or self._clean_text(current.get("setup_type")) or normalized["setup_variant"]
        normalized["regime"] = self._clean_text(normalized.get("regime") or normalized.get("regime_at_entry") or normalized.get("regime_name")) or self._clean_text(current.get("regime")) or ""
        normalized["regime_at_entry"] = self._clean_text(normalized.get("regime_at_entry") or normalized.get("regime")) or self._clean_text(current.get("regime_at_entry")) or normalized["regime"]
        normalized["session"] = self._clean_text(normalized.get("session") or normalized.get("session_at_entry") or normalized.get("session_name")) or self._clean_text(current.get("session")) or ""
        normalized["session_at_entry"] = self._clean_text(normalized.get("session_at_entry") or normalized.get("session")) or self._clean_text(current.get("session_at_entry")) or normalized["session"]
        normalized["entry_mode"] = self._clean_text(normalized.get("entry_mode")) or self._clean_text(current.get("entry_mode")) or ""
        normalized["execution_reason"] = self._clean_text(normalized.get("execution_reason") or normalized.get("reason_code")) or self._clean_text(current.get("execution_reason")) or ""
        normalized["blocked_reason"] = self._clean_text(normalized.get("blocked_reason")) or self._clean_text(current.get("blocked_reason")) or ""
        normalized["close_reason"] = self._clean_text(normalized.get("close_reason") or normalized.get("exit_reason")) or self._clean_text(current.get("close_reason")) or ""

        normalized["entry_price"] = normalized.get("entry_price", normalized.get("entry", current.get("entry_price")))
        normalized["stop_loss"] = normalized.get("stop_loss", normalized.get("sl", current.get("stop_loss")))
        normalized["take_profit"] = normalized.get("take_profit", normalized.get("tp", current.get("take_profit")))
        normalized["entry"] = normalized.get("entry", normalized.get("entry_price", current.get("entry")))
        normalized["sl"] = normalized.get("sl", normalized.get("stop_loss", current.get("sl")))
        normalized["tp"] = normalized.get("tp", normalized.get("take_profit", current.get("tp")))
        normalized["volume"] = normalized.get("volume", normalized.get("final_volume", normalized.get("requested_volume", current.get("volume", 0.0))))
        normalized["requested_volume"] = normalized.get("requested_volume", current.get("requested_volume", normalized.get("volume")))
        normalized["final_volume"] = normalized.get("final_volume", current.get("final_volume", normalized.get("volume")))

        normalized["entry_time"] = self._clean_text(normalized.get("entry_time") or normalized.get("created_at")) or self._clean_text(current.get("entry_time")) or normalized["created_at"]
        normalized["exit_time"] = self._clean_text(normalized.get("exit_time") or normalized.get("closed_at")) or self._clean_text(current.get("exit_time")) or normalized.get("closed_at")
        normalized["comment"] = self._clean_text(normalized.get("comment") or normalized.get("note")) or self._clean_text(current.get("comment")) or ""
        normalized["note"] = self._clean_text(normalized.get("note")) or self._clean_text(current.get("note")) or ""

        if not self._has_value(normalized.get("outcome_label")):
            normalized["outcome_label"] = self._trade_outcome(
                normalized.get("pnl", current.get("pnl")),
                normalized.get("win_loss", current.get("win_loss")),
            )
        if not self._has_value(normalized.get("win_loss")) and self._has_value(normalized.get("outcome_label")):
            normalized["win_loss"] = normalized.get("outcome_label")

        normalized["metadata_json"] = self._merge_json_values(current.get("metadata_json"), normalized.get("metadata_json") or normalized.get("metadata"))
        normalized["raw_mt5_json"] = self._merge_json_values(current.get("raw_mt5_json"), normalized.get("raw_mt5_json") or normalized.get("raw_mt5"))
        normalized["resolution_metadata_json"] = self._merge_json_values(
            current.get("resolution_metadata_json"),
            normalized.get("resolution_metadata_json") or normalized.get("resolution_metadata"),
        )
        normalized["journal_version"] = self._clean_text(normalized.get("journal_version")) or self._clean_text(current.get("journal_version")) or "2.0"
        normalized["resolution_stage"] = self._clean_text(normalized.get("resolution_stage")) or self._clean_text(current.get("resolution_stage")) or ""
        normalized["resolution_started_at"] = self._clean_text(normalized.get("resolution_started_at")) or self._clean_text(current.get("resolution_started_at")) or ""
        normalized["resolution_last_evidence_at"] = self._clean_text(normalized.get("resolution_last_evidence_at")) or self._clean_text(current.get("resolution_last_evidence_at")) or ""
        normalized["matched_order_ids"] = self._clean_text(normalized.get("matched_order_ids")) or self._clean_text(current.get("matched_order_ids")) or ""
        normalized["matched_deal_ids"] = self._clean_text(normalized.get("matched_deal_ids")) or self._clean_text(current.get("matched_deal_ids")) or ""
        normalized["matched_volume"] = normalized.get("matched_volume", current.get("matched_volume"))
        normalized["reconciliation_confidence"] = normalized.get("reconciliation_confidence", current.get("reconciliation_confidence"))
        normalized["close_price_source"] = self._clean_text(normalized.get("close_price_source")) or self._clean_text(current.get("close_price_source")) or ""
        normalized["lifecycle_version"] = int(
            normalized.get("lifecycle_version")
            or current.get("lifecycle_version")
            or self._lifecycle_version(normalized_status, normalized_event)
            or 0
        )
        return normalized

    def _prefer_non_empty(self, existing: Any, incoming: Any, *, prefer_incoming: bool = False) -> Any:
        """Keep the richer non-empty value, never downgrading to blank placeholders."""
        if prefer_incoming and self._has_value(incoming):
            return incoming
        if self._has_value(existing) and not self._has_value(incoming):
            return existing
        if not self._has_value(existing) and self._has_value(incoming):
            return incoming
        if prefer_incoming and self._has_value(incoming):
            return incoming
        return existing if self._has_value(existing) else incoming

    def _merge_trade_rows(self, existing: dict[str, Any], incoming: dict[str, Any], columns: set[str]) -> dict[str, Any]:
        """Merge a new payload into an existing trade row without losing good data."""
        merged = dict(existing)
        incoming = self._normalize_trade_row(incoming, existing)
        always_use_incoming = {
            "updated_at", "closed_at", "exit_time", "exit_price", "pnl", "pnl_pips", "realized_r", "hold_minutes", "hold_seconds",
            "fees", "swap", "commissions", "close_reason", "blocked_reason", "execution_reason", "outcome_label", "win_loss",
            "unresolved_reason", "resolution_attempts", "last_resolution_attempt_at", "raw_mt5_json",
            "resolution_stage", "resolution_started_at", "resolution_last_evidence_at", "matched_order_ids", "matched_deal_ids",
            "matched_volume", "reconciliation_confidence", "close_price_source", "lifecycle_version", "resolution_metadata_json",
        }
        prefer_latest_context = {
            "setup", "setup_fingerprint", "setup_family", "setup_variant", "setup_type",
            "regime", "regime_at_entry", "session", "session_at_entry", "entry_mode",
            "comment", "note",
        }
        for column in columns:
            existing_value = merged.get(column)
            incoming_value = incoming.get(column)
            if column == "status":
                merged[column] = self._merge_trade_status(existing_value, incoming_value)
                continue
            if column == "event_type":
                merged[column] = self._canonical_trade_event(incoming_value or existing_value, merged.get("status"))
                continue
            if column in {"metadata_json", "raw_mt5_json"}:
                merged[column] = self._merge_json_values(existing_value, incoming_value)
                continue
            if column == "created_at":
                merged[column] = self._safe_iso_timestamp(existing_value, incoming_value)
                continue
            if column in always_use_incoming:
                merged[column] = self._prefer_non_empty(existing_value, incoming_value, prefer_incoming=True)
                continue
            if column in prefer_latest_context:
                merged[column] = self._prefer_non_empty(existing_value, incoming_value, prefer_incoming=True)
                continue
            merged[column] = self._prefer_non_empty(existing_value, incoming_value)
        merged["status"] = self._merge_trade_status(existing.get("status"), incoming.get("status"))
        merged["event_type"] = self._canonical_trade_event(incoming.get("event_type") or existing.get("event_type"), merged["status"])
        return self._normalize_trade_row(merged, existing)

    def _write_trade_row(self, connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        """Insert or update a canonical trade row by trade identity."""
        row = dict(row)
        raw_identity_candidates = self._trade_identity_candidates(row, include_generated=False)
        matches = self._find_trade_matches(connection, row)
        existing = self._select_canonical_trade_row(matches)
        normalized = self._normalize_trade_row(row, existing)
        table_info = self._table_info(connection, "trades")
        columns = set(table_info)

        if existing:
            merged = self._merge_trade_rows(existing, normalized, columns)
        else:
            merged = self._normalize_trade_row(normalized)

        # Fill required legacy columns so old databases accept the canonical row.
        entry_default = merged.get("entry")
        if not self._has_value(entry_default):
            entry_default = merged.get("entry_price")
        if not self._has_value(entry_default):
            entry_default = 0.0
        volume_default = merged.get("volume")
        if not self._has_value(volume_default):
            volume_default = merged.get("final_volume")
        if not self._has_value(volume_default):
            volume_default = merged.get("requested_volume")
        if not self._has_value(volume_default):
            volume_default = 0.0
        required_defaults = {
            "timestamp": merged.get("timestamp"),
            "mode": self._clean_text(merged.get("mode") or merged.get("bot_mode")) or "UNKNOWN",
            "symbol": self._clean_text(merged.get("symbol")) or "UNKNOWN",
            "side": self._clean_text(merged.get("side")) or "UNKNOWN",
            "entry": entry_default,
            "volume": volume_default,
            "ticket": self._clean_text(merged.get("ticket") or merged.get("mt5_ticket") or merged.get("position_id") or merged.get("trade_id")) or self._generate_trade_identity(merged),
            "status": self._canonical_trade_status(merged.get("status"), merged.get("event_type")),
        }
        for column, info in table_info.items():
            if int(info.get("notnull", 0)) and column not in merged:
                merged[column] = required_defaults.get(column)
            elif int(info.get("notnull", 0)) and not self._has_value(merged.get(column)):
                merged[column] = required_defaults.get(column)
        if not self._has_value(merged.get("trade_id")):
            raise ValueError("Canonical trade row missing trade_id after normalization")
        if not self._has_value(merged.get("ticket")):
            merged["ticket"] = required_defaults["ticket"]

        normalized_columns: dict[str, Any] = {}
        for column in columns:
            if column not in merged:
                continue
            value = merged.get(column)
            if column in {"metadata_json", "raw_mt5_json", "details_json"}:
                normalized_columns[column] = self._json_text(self._json_object(value))
            else:
                normalized_columns[column] = value

        if existing:
            changed_fields = [
                column
                for column, value in normalized_columns.items()
                if existing.get(column) != value
            ]
            assignments = ", ".join(f"{column} = ?" for column in normalized_columns.keys())
            values = list(normalized_columns.values()) + [int(existing["id"])]
            connection.execute(f"UPDATE trades SET {assignments} WHERE id = ?;", tuple(values))
            if changed_fields:
                self._runtime_logger.info(
                    "trade_row_merged trade_id=%s row_id=%s matched_rows=%s status=%s event_type=%s changed_fields=%s",
                    merged.get("trade_id"),
                    existing.get("id"),
                    len(matches),
                    merged.get("status"),
                    merged.get("event_type"),
                    ",".join(changed_fields[:12]),
                )
            return

        insert_columns = list(normalized_columns.keys())
        placeholders = ", ".join("?" for _ in insert_columns)
        values = [normalized_columns[column] for column in insert_columns]
        connection.execute(
            f"INSERT INTO trades ({', '.join(insert_columns)}) VALUES ({placeholders});",
            tuple(values),
        )
        self._runtime_logger.info(
            "trade_row_created trade_id=%s status=%s event_type=%s symbol=%s side=%s generated_identity=%s",
            normalized_columns.get("trade_id"),
            normalized_columns.get("status"),
            normalized_columns.get("event_type"),
            normalized_columns.get("symbol"),
            normalized_columns.get("side"),
            "yes" if not raw_identity_candidates else "no",
        )

    def _write_trade_event_row(self, connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        """Insert a lifecycle event row."""
        raw_row = dict(row)
        row = self._normalize_trade_identity_fields(raw_row)
        raw_event_type = str(raw_row.get("event_type") or "").strip().upper()
        row["event_type"] = raw_event_type or self._canonical_trade_event(row.get("event_type"), row.get("status"))
        row["status"] = str(row.get("status") or self._canonical_trade_status(None, row.get("event_type"))).strip().upper()
        row["timestamp"] = self._safe_iso_timestamp(row.get("timestamp"), row.get("updated_at"), row.get("created_at"))
        if not row.get("lifecycle_version"):
            row["lifecycle_version"] = self._lifecycle_version(row.get("status"), row.get("event_type"))
        if not row.get("event_bucket"):
            row["event_bucket"] = f"v{int(row.get('lifecycle_version', 0) or 0)}"
        if not row.get("event_key"):
            row["event_key"] = self._event_identity_key(row)
        columns = self._table_columns(connection, "trade_events")
        normalized = {column: row.get(column) for column in columns if column in row}
        if "metadata_json" in columns:
            normalized["metadata_json"] = self._json_text(row.get("metadata_json") or row.get("metadata"))
        if "resolution_metadata_json" in columns:
            normalized["resolution_metadata_json"] = self._json_text(row.get("resolution_metadata_json") or row.get("resolution_metadata"))
        if "journal_version" in columns and not normalized.get("journal_version"):
            normalized["journal_version"] = "2.0"
        event_key = str(normalized.get("event_key") or "").strip()
        if event_key:
            existing = connection.execute(
                "SELECT id FROM trade_events WHERE event_key = ? LIMIT 1;",
                (event_key,),
            ).fetchone()
            if existing is not None:
                current = int(self._get_maintenance_value(connection, "trade_event_duplicate_suppressed", "0") or 0)
                self._set_maintenance_value(connection, "trade_event_duplicate_suppressed", current + 1)
                return
        insert_columns = list(normalized.keys())
        placeholders = ", ".join("?" for _ in insert_columns)
        values = [normalized[column] for column in insert_columns]
        connection.execute(
            f"INSERT INTO trade_events ({', '.join(insert_columns)}) VALUES ({placeholders});",
            tuple(values),
        )

    def _update_trade_row_by_id(self, connection: sqlite3.Connection, row_id: int, row: dict[str, Any]) -> None:
        """Update a trade row by primary key."""
        columns = self._table_columns(connection, "trades")
        payload = {column: row.get(column) for column in columns if column in row}
        if "metadata_json" in payload:
            payload["metadata_json"] = self._json_text(self._json_object(payload.get("metadata_json")))
        if "raw_mt5_json" in payload:
            payload["raw_mt5_json"] = self._json_text(self._json_object(payload.get("raw_mt5_json")))
        assignments = ", ".join(f"{column} = ?" for column in payload.keys())
        values = list(payload.values()) + [int(row_id)]
        connection.execute(f"UPDATE trades SET {assignments} WHERE id = ?;", tuple(values))

    def _mark_trade_row_status(
        self,
        connection: sqlite3.Connection,
        row_id: int,
        status: str,
        note: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Soft-mark a row as merged/orphaned without deleting it."""
        existing = row_to_dict(connection.execute("SELECT * FROM trades WHERE id = ? LIMIT 1;", (int(row_id),)).fetchone()) or {}
        merged_metadata = self._merge_json_values(existing.get("metadata_json"), metadata or {})
        self._update_trade_row_by_id(
            connection,
            row_id,
            {
                "status": status,
                "event_type": existing.get("event_type") or ("TRADE_CLOSED" if "CLOSE" in status else "TRADE_EVENT"),
                "updated_at": utc_now().isoformat(),
                "note": note,
                "metadata_json": merged_metadata,
            },
        )
        self._runtime_logger.info(
            "trade_row_suppressed row_id=%s status=%s note=%s trade_id=%s",
            row_id,
            status,
            note,
            existing.get("trade_id") or existing.get("mt5_ticket") or existing.get("position_id") or existing.get("ticket") or existing.get("order_id") or "",
        )

    def _collect_duplicate_trade_groups(self, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Return connected groups of trade rows that share any stable identity value."""
        rows_by_id = {int(row["id"]): row for row in rows if row.get("id") is not None}
        adjacency: dict[int, set[int]] = defaultdict(set)
        identity_map: dict[str, set[int]] = defaultdict(set)

        for row in rows_by_id.values():
            for key in ("trade_id", "position_id", "mt5_ticket", "ticket", "order_id"):
                value = self._clean_text(row.get(key))
                if value:
                    identity_map[f"{key}:{value}"].add(int(row["id"]))

        for ids in identity_map.values():
            if len(ids) < 2:
                continue
            id_list = list(ids)
            for source in id_list:
                adjacency[source].update(ids - {source})

        groups: list[list[dict[str, Any]]] = []
        seen: set[int] = set()
        for row_id in rows_by_id:
            if row_id in seen or row_id not in adjacency:
                continue
            stack = [row_id]
            component: set[int] = set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(item for item in adjacency.get(current, set()) if item not in component)
            seen.update(component)
            if len(component) > 1:
                groups.append([rows_by_id[item] for item in sorted(component)])
        return groups

    def _trade_repair_consistency(self, connection: sqlite3.Connection) -> dict[str, int]:
        """Return lightweight signals about whether lifecycle repair should run."""
        relevant_event_types = ("TRADE_OPENED", "TRADE_MANAGED", "TRADE_CLOSED", "TRADE_FINALIZED", "OPEN", "CLOSE")
        placeholders = ", ".join("?" for _ in relevant_event_types)
        relevant_event_max_id = int(
            self._scalar(
                connection,
                f"""
                SELECT COALESCE(MAX(id), 0)
                FROM trade_events
                WHERE UPPER(COALESCE(event_type, '')) IN ({placeholders});
                """,
                relevant_event_types,
            )
            or 0
        )
        malformed_trade_rows = int(
            self._scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM trades
                WHERE status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
                  AND (
                        trade_id IS NULL
                        OR TRIM(trade_id) = ''
                        OR status IS NULL
                        OR TRIM(status) = ''
                        OR UPPER(status) IN ('OPEN', 'TRADE_MANAGED', 'MANAGED')
                      );
                """,
            )
            or 0
        )
        duplicate_rows = int(
            self._scalar(
                connection,
                "SELECT COUNT(*) FROM trades WHERE status = 'MERGED_DUPLICATE';",
            )
            or 0
        )
        orphan_rows = int(
            self._scalar(
                connection,
                "SELECT COUNT(*) FROM trades WHERE status = 'IGNORED_ORPHAN';",
            )
            or 0
        )
        total_trades = int(self._scalar(connection, "SELECT COUNT(*) FROM trades;") or 0)
        last_repaired_event_id = int(self._get_maintenance_value(connection, "trade_lifecycle_last_repaired_event_id", "0") or 0)
        return {
            "relevant_event_max_id": relevant_event_max_id,
            "last_repaired_event_id": last_repaired_event_id,
            "malformed_trade_rows": malformed_trade_rows,
            "duplicate_rows": duplicate_rows,
            "orphan_rows": orphan_rows,
            "total_trades": total_trades,
        }

    def repair_trade_lifecycle_if_needed(self, force_full: bool = False) -> dict[str, Any]:
        """Run lifecycle repair only when the database shape suggests it is needed."""
        started = time.perf_counter()
        with self._lock:
            with self._connect() as connection:
                consistency = self._trade_repair_consistency(connection)
                should_repair = bool(force_full) or consistency["malformed_trade_rows"] > 0 or consistency["relevant_event_max_id"] > consistency["last_repaired_event_id"]
                if not should_repair:
                    self._runtime_logger.info(
                        "trade_lifecycle_repair_skipped relevant_event_max_id=%s last_repaired_event_id=%s malformed_trade_rows=%s total_trades=%s",
                        consistency["relevant_event_max_id"],
                        consistency["last_repaired_event_id"],
                        consistency["malformed_trade_rows"],
                        consistency["total_trades"],
                    )
                    return {
                        "skipped": True,
                        "full_scan": False,
                        "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                        **consistency,
                    }
                full_scan = bool(force_full) or consistency["malformed_trade_rows"] > 0 or consistency["last_repaired_event_id"] == 0
                start_event_id = 0 if full_scan else consistency["last_repaired_event_id"]
        summary = self.repair_trade_lifecycle(start_event_id=start_event_id, full_scan=full_scan)
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        with self._lock:
            with self._connect() as connection:
                latest = self._trade_repair_consistency(connection)
                self._set_maintenance_value(connection, "trade_lifecycle_last_repaired_event_id", latest["relevant_event_max_id"])
                self._set_maintenance_value(connection, "trade_lifecycle_last_repair_summary", json.dumps(summary, ensure_ascii=True, sort_keys=True))
                self._set_maintenance_value(connection, "trade_lifecycle_last_repaired_at", utc_now().isoformat())
                connection.commit()
        self._runtime_logger.info(
            "trade_lifecycle_repair_complete full_scan=%s start_event_id=%s normalized=%s events=%s merged_duplicates=%s ignored_orphans=%s duration_ms=%s",
            full_scan,
            start_event_id,
            summary.get("normalized_trade_rows", 0),
            summary.get("backfilled_from_events", 0),
            summary.get("merged_duplicates", 0),
            summary.get("ignored_orphans", 0),
            duration_ms,
        )
        return {"skipped": False, "full_scan": full_scan, "duration_ms": duration_ms, **summary}

    def repair_trade_lifecycle(self, start_event_id: int = 0, full_scan: bool = True) -> dict[str, int]:
        """Repair canonical trades from existing rows and lifecycle events."""
        summary = {
            "normalized_trade_rows": 0,
            "backfilled_from_events": 0,
            "merged_duplicates": 0,
            "ignored_orphans": 0,
        }
        with self._lock:
            with self._connect() as connection:
                if full_scan:
                    existing_rows = rows_to_dicts(
                        connection.execute("SELECT * FROM trades ORDER BY id ASC;").fetchall()
                    )
                    for row in existing_rows:
                        normalized = self._normalize_trade_row(row, row)
                        if row.get("id") is not None:
                            self._update_trade_row_by_id(connection, int(row["id"]), normalized)
                            summary["normalized_trade_rows"] += 1

                event_query = """
                    SELECT *
                    FROM trade_events
                    WHERE UPPER(COALESCE(event_type, '')) IN (
                        'TRADE_OPENED', 'TRADE_MANAGED', 'TRADE_CLOSED', 'TRADE_FINALIZED', 'OPEN', 'CLOSE'
                    )
                """
                event_parameters: tuple[Any, ...] = ()
                if int(start_event_id) > 0:
                    event_query += " AND id > ?"
                    event_parameters = (int(start_event_id),)
                event_query += " ORDER BY id ASC;"
                event_rows = rows_to_dicts(
                    connection.execute(event_query, event_parameters).fetchall()
                )
                for event in event_rows:
                    self._write_trade_row(connection, dict(event))
                    summary["backfilled_from_events"] += 1

                if full_scan:
                    repaired_rows = rows_to_dicts(
                        connection.execute(
                            """
                            SELECT *
                            FROM trades
                            WHERE status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
                            ORDER BY id ASC;
                            """
                        ).fetchall()
                    )
                    trade_columns = self._table_columns(connection, "trades")
                    for group in self._collect_duplicate_trade_groups(repaired_rows):
                        canonical = self._select_canonical_trade_row(group)
                        if canonical is None:
                            continue
                        merged = dict(canonical)
                        for row in group:
                            if int(row["id"]) == int(canonical["id"]):
                                continue
                            merged = self._merge_trade_rows(merged, row, trade_columns)
                        self._update_trade_row_by_id(connection, int(canonical["id"]), merged)
                        for row in group:
                            if int(row["id"]) == int(canonical["id"]):
                                continue
                            self._mark_trade_row_status(
                                connection,
                                int(row["id"]),
                                "MERGED_DUPLICATE",
                                f"merged_into:{merged.get('trade_id')}",
                                {"merged_into_trade_id": merged.get("trade_id"), "merged_into_row_id": canonical["id"]},
                            )
                            summary["merged_duplicates"] += 1

                    orphan_rows = rows_to_dicts(
                        connection.execute(
                            """
                            SELECT *
                            FROM trades
                            WHERE status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
                            ORDER BY id ASC;
                            """
                        ).fetchall()
                    )
                    for row in orphan_rows:
                        identities = self._trade_identity_candidates(row, include_generated=False)
                        if identities:
                            continue
                        if self._trade_row_quality(row) >= 18:
                            generated = self._generate_trade_identity(row)
                            patched = self._normalize_trade_row({**row, "trade_id": generated}, row)
                            self._update_trade_row_by_id(connection, int(row["id"]), patched)
                            summary["normalized_trade_rows"] += 1
                            continue
                        self._mark_trade_row_status(
                            connection,
                            int(row["id"]),
                            "IGNORED_ORPHAN",
                            "ignored_orphan_trade_row",
                            {"repair_reason": "missing_identity_and_insufficient_context"},
                        )
                        summary["ignored_orphans"] += 1

                connection.commit()
        return summary

    def insert_trade_open(self, trade: dict[str, Any]) -> None:
        """Insert a newly opened trade row."""
        trade_row = dict(trade)
        trade_row.setdefault("event_type", "TRADE_OPENED")
        trade_row.setdefault("status", "OPENED")
        trade_row.setdefault("created_at", trade_row.get("timestamp"))
        trade_row.setdefault("updated_at", trade_row.get("timestamp"))
        trade_row.setdefault("bot_mode", trade_row.get("mode"))
        trade_row.setdefault("mode", trade_row.get("bot_mode"))
        trade_row.setdefault("entry_price", trade_row.get("entry") or trade_row.get("entry_price"))
        trade_row.setdefault("stop_loss", trade_row.get("sl") or trade_row.get("stop_loss"))
        trade_row.setdefault("take_profit", trade_row.get("tp") or trade_row.get("take_profit"))
        trade_row.setdefault("timeframe_setup", trade_row.get("timeframe_setup") or trade_row.get("timeframe"))
        trade_row.setdefault("timeframe_entry", trade_row.get("timeframe_entry") or trade_row.get("timeframe"))
        trade_row.setdefault("comment", trade_row.get("note") or trade_row.get("comment"))
        trade_row.setdefault("setup", trade_row.get("setup") or trade_row.get("setup_fingerprint"))
        trade_row.setdefault("regime", trade_row.get("regime") or trade_row.get("regime_at_entry"))
        trade_row.setdefault("session", trade_row.get("session") or trade_row.get("session_at_entry"))
        with self._lock:
            with self._connect() as connection:
                self._write_trade_row(connection, trade_row)
                connection.commit()

    def record_trade_close(self, trade: dict[str, Any]) -> None:
        """Update an existing trade row with close details, or insert if missing."""
        trade_row = dict(trade)
        trade_row["timestamp"] = self._safe_iso_timestamp(
            trade_row.get("timestamp"),
            trade_row.get("closed_at"),
            trade_row.get("exit_time"),
            trade_row.get("updated_at"),
            trade_row.get("created_at"),
        )
        trade_row.setdefault("event_type", "TRADE_CLOSED")
        trade_row.setdefault("status", "CLOSED")
        trade_row.setdefault("closed_at", trade_row.get("closed_at") or trade_row.get("exit_time") or trade_row.get("timestamp"))
        trade_row.setdefault("updated_at", trade_row.get("timestamp"))
        trade_row.setdefault("bot_mode", trade_row.get("mode") or trade_row.get("bot_mode"))
        trade_row.setdefault("mode", trade_row.get("bot_mode"))
        trade_row.setdefault("entry_price", trade_row.get("entry") or trade_row.get("entry_price"))
        trade_row.setdefault("stop_loss", trade_row.get("sl") or trade_row.get("stop_loss"))
        trade_row.setdefault("take_profit", trade_row.get("tp") or trade_row.get("take_profit"))
        trade_row.setdefault("comment", trade_row.get("note") or trade_row.get("comment"))
        trade_row.setdefault("setup", trade_row.get("setup") or trade_row.get("setup_fingerprint"))
        trade_row.setdefault("regime", trade_row.get("regime") or trade_row.get("regime_at_entry"))
        trade_row.setdefault("session", trade_row.get("session") or trade_row.get("session_at_entry"))
        trade_row["outcome_label"] = self._trade_outcome(trade_row.get("pnl"), trade_row.get("win_loss"))
        with self._lock:
            with self._connect() as connection:
                self._write_trade_row(connection, trade_row)
                connection.commit()

    def record_trade_event(self, event: dict[str, Any]) -> None:
        """Insert a lifecycle event row into trade_events."""
        event_row = dict(event)
        event_row["timestamp"] = self._safe_iso_timestamp(
            event_row.get("timestamp"),
            event_row.get("created_at"),
            event_row.get("updated_at"),
        )
        event_row = self._normalize_trade_identity_fields(event_row)
        event_row.setdefault("journal_version", "2.0")
        trade_row_event_types = {"TRADE_OPENED", "TRADE_MANAGED", "TRADE_CLOSED", "TRADE_FINALIZED", "OPEN", "CLOSE"}
        should_update_trade_row = str(event_row.get("event_type") or "").upper() in trade_row_event_types
        with self._lock:
            with self._connect() as connection:
                self._write_trade_event_row(connection, event_row)
                if should_update_trade_row:
                    self._write_trade_row(connection, event_row)
                connection.commit()

    def mark_trade_pending_resolution(self, trade: dict[str, Any]) -> None:
        """Mark a trade as waiting for close resolution."""
        pending_row = dict(trade)
        pending_row["timestamp"] = self._safe_iso_timestamp(
            pending_row.get("timestamp"),
            pending_row.get("closed_at"),
            pending_row.get("updated_at"),
            pending_row.get("created_at"),
        )
        pending_row.setdefault("status", "PENDING_CLOSE_RESOLUTION")
        pending_row.setdefault("event_type", "TRADE_CLOSED")
        pending_row.setdefault("resolution_attempts", int(pending_row.get("resolution_attempts", 0) or 0))
        pending_row.setdefault("unresolved_reason", pending_row.get("unresolved_reason") or "history_unavailable")
        pending_row.setdefault("closed_at", pending_row.get("timestamp"))
        pending_row.setdefault("updated_at", pending_row.get("timestamp"))
        self.record_trade_close(pending_row)
        self.record_trade_event({
            **pending_row,
            "event_type": "TRADE_CLOSED",
            "status": "PENDING_CLOSE_RESOLUTION",
            "note": pending_row.get("note") or "pending_close_resolution",
        })

    def finalize_trade_resolution(self, trade: dict[str, Any]) -> None:
        """Mark a trade as fully finalized after close reconciliation."""
        final_row = dict(trade)
        final_row.setdefault("status", "FINALIZED")
        final_row.setdefault("event_type", "TRADE_FINALIZED")
        final_row.setdefault("closed_at", final_row.get("closed_at") or final_row.get("exit_time") or final_row.get("timestamp"))
        final_row.setdefault("updated_at", final_row.get("updated_at") or final_row.get("timestamp"))
        self.record_trade_close(final_row)
        self.record_trade_event(final_row)

    def insert_signal(self, signal: dict[str, Any]) -> None:
        """Insert a signal or execution-attempt record."""
        columns = [
            "timestamp", "event_type", "mode", "live_or_dry", "symbol", "side", "timeframe", "signal_type",
            "setup_fingerprint", "setup_family", "setup_anchor_time", "trigger_type", "price", "setup_price",
            "value_price", "entry_price", "sl", "tp", "spread_points", "requested_volume", "final_volume", "trend_ok",
            "setup_ok", "entry_ok", "executed", "entry_mode", "regime_name", "regime_confidence", "session_name",
            "session_live_allowed", "reason_code", "reason", "trend_score", "setup_score", "trigger_score", "entry_score",
            "metadata_json", "attempt_id", "detected_at", "validated_at", "final_outcome_at", "final_outcome_reason",
            "order_send_attempted", "order_send_retcode", "order_send_retcode_text", "spread_at_validation",
            "spread_limit_used", "spread_limit_source", "cooldown_applied", "duplicate_guard_applied", "failure_stage",
        ]
        values = (
            signal["timestamp"],
            signal.get("event_type", "signal"),
            signal.get("mode"),
            signal.get("live_or_dry"),
            signal["symbol"],
            signal["side"],
            signal.get("timeframe"),
            signal.get("signal_type"),
            signal.get("setup_fingerprint"),
            signal.get("setup_family"),
            signal.get("setup_anchor_time"),
            signal.get("trigger_type"),
            signal.get("price"),
            signal.get("setup_price"),
            signal.get("value_price"),
            signal.get("entry_price"),
            signal.get("sl"),
            signal.get("tp"),
            signal.get("spread_points"),
            signal.get("requested_volume"),
            signal.get("final_volume"),
            int(signal.get("trend_ok", True)),
            int(signal.get("setup_ok", True)),
            int(signal.get("entry_ok", False)),
            int(signal.get("executed", False)),
            signal.get("entry_mode"),
            signal.get("regime_name"),
            signal.get("regime_confidence"),
            signal.get("session_name"),
            int(bool(signal.get("session_live_allowed", False))),
            signal.get("reason_code"),
            signal.get("reason"),
            signal.get("trend_score"),
            signal.get("setup_score"),
            signal.get("trigger_score"),
            signal.get("entry_score"),
            self._json_text(self._json_object(signal.get("metadata_json") or signal.get("metadata"))),
            signal.get("attempt_id"),
            signal.get("detected_at"),
            signal.get("validated_at"),
            signal.get("final_outcome_at"),
            signal.get("final_outcome_reason"),
            int(bool(signal.get("order_send_attempted", False))),
            signal.get("order_send_retcode"),
            signal.get("order_send_retcode_text"),
            signal.get("spread_at_validation"),
            signal.get("spread_limit_used"),
            signal.get("spread_limit_source"),
            int(bool(signal.get("cooldown_applied", False))),
            int(bool(signal.get("duplicate_guard_applied", False))),
            signal.get("failure_stage"),
        )
        placeholders = ", ".join("?" for _ in columns)
        statement = f"INSERT INTO signals ({', '.join(columns)}) VALUES ({placeholders});"
        self._execute(statement, values)

    def insert_event(self, event: dict[str, Any]) -> None:
        """Insert a bot event row."""
        self._execute(
            """
            INSERT INTO bot_events (timestamp, level, event_type, message, reason_code, details_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                event["timestamp"],
                event["level"],
                event["event_type"],
                event["message"],
                event.get("reason_code"),
                self._json_text(self._json_object(event.get("details_json") or event.get("details"))),
            ),
        )

    def insert_heartbeat(self, heartbeat: dict[str, Any]) -> None:
        """Insert a heartbeat row."""
        self._execute(
            """
            INSERT INTO heartbeat (
                timestamp, status, symbol, spread, trend, setup, balance, equity, open_positions, regime, session, reason_code, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                heartbeat["timestamp"],
                heartbeat["status"],
                heartbeat["symbol"],
                heartbeat.get("spread"),
                heartbeat.get("trend"),
                heartbeat.get("setup"),
                heartbeat.get("balance"),
                heartbeat.get("equity"),
                int(heartbeat["open_positions"]),
                heartbeat.get("regime"),
                heartbeat.get("session"),
                heartbeat.get("reason_code"),
                heartbeat.get("mode"),
            ),
        )

    def insert_daily_analytics(self, summary: dict[str, Any]) -> None:
        """Insert a daily analytics summary."""
        self._execute(
            """
            INSERT INTO daily_analytics (
                timestamp, day, mode, setups, live_trades, win_rate, average_r, average_hold_minutes,
                long_trades, short_trades, by_setup_family_json, by_regime_json, by_session_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                summary["timestamp"],
                summary["day"],
                summary["mode"],
                int(summary.get("setups", 0)),
                int(summary.get("live_trades", 0)),
                summary.get("win_rate"),
                summary.get("average_r"),
                summary.get("average_hold_minutes"),
                int(summary.get("long_trades", 0)),
                int(summary.get("short_trades", 0)),
                summary.get("by_setup_family_json"),
                summary.get("by_regime_json"),
                summary.get("by_session_json"),
            ),
        )

    def insert_validation_report(self, summary: dict[str, Any]) -> None:
        """Insert a validation report summary."""
        self._execute(
            """
            INSERT INTO validation_reports (
                timestamp, day, scope_type, scope_name, mode, rollout_phase, setups_by_family_json,
                blocked_by_regime, blocked_by_session, blocked_by_anti_chase, duplicate_entries_prevented,
                live_eligible_setups, paper_validated_setups, journaling_errors, top_block_reasons_json,
                consistency_ok, consistency_summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                summary["timestamp"],
                summary["day"],
                summary["scope_type"],
                summary["scope_name"],
                summary["mode"],
                int(summary.get("rollout_phase", 0)),
                summary.get("setups_by_family_json"),
                int(summary.get("blocked_by_regime", 0)),
                int(summary.get("blocked_by_session", 0)),
                int(summary.get("blocked_by_anti_chase", 0)),
                int(summary.get("duplicate_entries_prevented", 0)),
                int(summary.get("live_eligible_setups", 0)),
                int(summary.get("paper_validated_setups", 0)),
                int(summary.get("journaling_errors", 0)),
                summary.get("top_block_reasons_json"),
                int(bool(summary.get("consistency_ok", False))),
                summary.get("consistency_summary_json"),
            ),
        )

    def insert_audit_event(self, event: dict[str, Any]) -> None:
        """Insert an audit trail row."""
        self._execute(
            """
            INSERT INTO audit_log (timestamp, actor, action, target, level, details_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                event["timestamp"],
                event["actor"],
                event["action"],
                event["target"],
                event.get("level", "INFO"),
                event.get("details_json"),
            ),
        )

    def get_recent_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent audit log rows."""
        return self._query_dataframe(
            """
            SELECT timestamp, actor, action, target, level, details_json
            FROM audit_log
            ORDER BY id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )

    def create_backtest_run(self, payload: dict[str, Any]) -> int:
        """Create a backtest run row and return its id."""
        created_at = str(payload.get("created_at") or utc_now().isoformat())
        state = normalize_job_state(payload.get("state"), JobState.QUEUED.value)
        started_at = payload.get("started_at") or (created_at if state in {JobState.RUNNING.value, JobState.COMPLETED.value} else None)
        updated_at = payload.get("updated_at") or created_at
        finished_at = payload.get("finished_at")
        last_heartbeat_at = payload.get("last_heartbeat_at") or updated_at
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO backtest_runs (
                        created_at, symbol, timeframe, start_date, end_date, enabled_strategies_json,
                        session_filter_json, initial_balance, risk_percent, spread_model, slippage_model,
                        execution_model, notes, state, started_at, updated_at, finished_at,
                        last_heartbeat_at, failure_reason, interrupted_reason, progress_current,
                        progress_total, artifacts_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        created_at,
                        payload["symbol"],
                        payload["timeframe"],
                        payload["start_date"],
                        payload["end_date"],
                        self._json_text(payload.get("enabled_strategies_json")),
                        self._json_text(payload.get("session_filter_json")),
                        float(payload.get("initial_balance", 0.0)),
                        float(payload.get("risk_percent", 0.0)),
                        self._json_text(payload.get("spread_model")),
                        self._json_text(payload.get("slippage_model")),
                        str(payload.get("execution_model") or "next_bar_open"),
                        payload.get("notes"),
                        state,
                        started_at,
                        updated_at,
                        finished_at,
                        last_heartbeat_at,
                        payload.get("failure_reason"),
                        payload.get("interrupted_reason"),
                        int(payload.get("progress_current", 0) or 0),
                        int(payload.get("progress_total", 0) or 0),
                        self._json_text(payload.get("artifacts_json")),
                        self._json_text(payload.get("metadata_json")),
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)

    def update_backtest_run(self, run_id: int, updates: dict[str, Any]) -> None:
        """Update one backtest run row with null-safe JSON handling."""
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        json_fields = {"artifacts_json", "enabled_strategies_json", "session_filter_json", "spread_model", "slippage_model", "metadata_json"}
        text_fields = {"failure_reason", "interrupted_reason", "notes", "started_at", "updated_at", "finished_at", "last_heartbeat_at", "state"}
        int_fields = {"progress_current", "progress_total"}
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            if key == "state":
                values.append(normalize_job_state(value))
            elif key in json_fields:
                values.append(self._json_text(value))
            elif key in int_fields:
                values.append(int(value or 0))
            elif key in text_fields:
                values.append(None if value in ("", None) else str(value))
            else:
                values.append(value)
        if "updated_at" not in updates:
            assignments.append("updated_at = ?")
            values.append(utc_now().isoformat())
        values.append(int(run_id))
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE backtest_runs SET {', '.join(assignments)} WHERE id = ?;",
                    tuple(values),
                )
                connection.commit()

    def update_backtest_run_state(
        self,
        run_id: int,
        state: str,
        *,
        progress_current: int | None = None,
        progress_total: int | None = None,
        failure_reason: str | None = None,
        interrupted_reason: str | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> None:
        """Persist a standardized lifecycle state for one backtest run."""
        normalized = normalize_job_state(state)
        now = utc_now().isoformat()
        updates: dict[str, Any] = {
            "state": normalized,
            "updated_at": now,
            "last_heartbeat_at": now,
        }
        if normalized in {JobState.RUNNING.value, JobState.COMPLETED.value, JobState.PARTIAL.value, JobState.FAILED.value, JobState.INTERRUPTED.value}:
            run = self.get_backtest_run(run_id) or {}
            if not run.get("started_at"):
                updates["started_at"] = now
        if normalized in {JobState.COMPLETED.value, JobState.FAILED.value, JobState.INTERRUPTED.value, JobState.PARTIAL.value}:
            updates["finished_at"] = now
        if progress_current is not None:
            updates["progress_current"] = int(progress_current)
        if progress_total is not None:
            updates["progress_total"] = int(progress_total)
        if failure_reason is not None:
            updates["failure_reason"] = failure_reason
        if interrupted_reason is not None:
            updates["interrupted_reason"] = interrupted_reason
        if artifacts is not None:
            updates["artifacts_json"] = artifacts
        self.update_backtest_run(run_id, updates)

    def insert_backtest_signal(self, payload: dict[str, Any]) -> None:
        """Persist one backtest signal row."""
        self._execute(
            """
            INSERT INTO backtest_signals (
                run_id, signal_time, strategy_name, symbol, side, setup, entry, sl, tp, score,
                executed, execution_reason, blocked_reason, raw_signal_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(payload["run_id"]),
                payload["signal_time"],
                payload["strategy_name"],
                payload["symbol"],
                payload["side"],
                payload.get("setup"),
                payload.get("entry"),
                payload.get("sl"),
                payload.get("tp"),
                payload.get("score"),
                int(bool(payload.get("executed", False))),
                payload.get("execution_reason"),
                payload.get("blocked_reason"),
                self._json_text(payload.get("raw_signal_json")),
            ),
        )

    def insert_backtest_trade(self, payload: dict[str, Any]) -> None:
        """Persist one backtest trade row."""
        self._execute(
            """
            INSERT INTO backtest_trades (
                run_id, strategy_name, symbol, side, trade_id, position_id, order_id, setup_family,
                setup_fingerprint, open_time, close_time, entry, sl, tp, exit_price, pnl, pnl_r,
                exit_reason, exit_reason_code, mfe, mae, duration_seconds, volume, volume_initial,
                volume_remaining_at_close, realized_pnl_total, partial_realized_pnl, final_realized_pnl,
                commission, swap, balance_before, balance_after, equity_before, equity_after, trade_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(payload["run_id"]),
                payload["strategy_name"],
                payload["symbol"],
                payload["side"],
                payload.get("trade_id"),
                payload.get("position_id"),
                payload.get("order_id"),
                payload.get("setup_family"),
                payload.get("setup_fingerprint"),
                payload["open_time"],
                payload["close_time"],
                float(payload["entry"]),
                float(payload["sl"]),
                float(payload["tp"]),
                float(payload["exit_price"]),
                float(payload["pnl"]),
                float(payload["pnl_r"]),
                payload.get("exit_reason"),
                payload.get("exit_reason_code"),
                payload.get("mfe"),
                payload.get("mae"),
                payload.get("duration_seconds"),
                payload.get("volume"),
                payload.get("volume_initial"),
                payload.get("volume_remaining_at_close"),
                payload.get("realized_pnl_total", payload.get("pnl")),
                payload.get("partial_realized_pnl", 0.0),
                payload.get("final_realized_pnl", payload.get("pnl")),
                payload.get("commission", 0.0),
                payload.get("swap", 0.0),
                payload.get("balance_before"),
                payload.get("balance_after"),
                payload.get("equity_before"),
                payload.get("equity_after"),
                self._json_text(payload.get("trade_metadata_json")),
            ),
        )

    def insert_backtest_equity_point(self, payload: dict[str, Any]) -> None:
        """Persist one backtest equity curve point."""
        self._execute(
            """
            INSERT INTO backtest_equity_curve (run_id, ts, equity, balance, floating_pnl)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                int(payload["run_id"]),
                payload["ts"],
                float(payload["equity"]),
                float(payload["balance"]),
                float(payload["floating_pnl"]),
            ),
        )

    def upsert_history_bars(self, rows: list[dict[str, Any]]) -> None:
        """Persist a batch of normalized history bars into the local cache."""
        if not rows:
            return
        with self._lock:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO history_cache_bars (
                        symbol, timeframe, time, open, high, low, close, tick_volume, spread, real_volume, source_kind, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timeframe, time) DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        tick_volume = excluded.tick_volume,
                        spread = excluded.spread,
                        real_volume = excluded.real_volume,
                        source_kind = excluded.source_kind,
                        updated_at = excluded.updated_at;
                    """,
                    [
                        (
                            str(row["symbol"]).upper(),
                            str(row["timeframe"]).upper(),
                            str(row["time"]),
                            float(row["open"]),
                            float(row["high"]),
                            float(row["low"]),
                            float(row["close"]),
                            float(row.get("tick_volume", 0.0) or 0.0),
                            float(row.get("spread", 0.0) or 0.0),
                            float(row.get("real_volume", row.get("tick_volume", 0.0)) or 0.0),
                            str(row.get("source_kind") or "mt5"),
                            str(row.get("updated_at") or utc_now().isoformat()),
                        )
                        for row in rows
                    ],
                )
                connection.commit()

    def upsert_history_ticks(self, rows: list[dict[str, Any]]) -> None:
        """Persist a batch of normalized tick rows into the local cache."""
        if not rows:
            return
        with self._lock:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO history_cache_ticks (
                        symbol, time, bid, ask, last, volume, spread, flags, source_kind, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, time) DO UPDATE SET
                        bid = excluded.bid,
                        ask = excluded.ask,
                        last = excluded.last,
                        volume = excluded.volume,
                        spread = excluded.spread,
                        flags = excluded.flags,
                        source_kind = excluded.source_kind,
                        updated_at = excluded.updated_at;
                    """,
                    [
                        (
                            str(row["symbol"]).upper(),
                            str(row["time"]),
                            None if row.get("bid") in (None, "") else float(row.get("bid")),
                            None if row.get("ask") in (None, "") else float(row.get("ask")),
                            None if row.get("last") in (None, "") else float(row.get("last")),
                            None if row.get("volume") in (None, "") else float(row.get("volume")),
                            None if row.get("spread") in (None, "") else float(row.get("spread")),
                            None if row.get("flags") in (None, "") else int(row.get("flags")),
                            str(row.get("source_kind") or "mt5"),
                            str(row.get("updated_at") or utc_now().isoformat()),
                        )
                        for row in rows
                    ],
                )
                connection.commit()

    def insert_backtest_playback_frame(self, payload: dict[str, Any]) -> None:
        """Persist one replay frame."""
        self._execute(
            """
            INSERT INTO backtest_playback_frames (
                run_id, bar_index, timestamp, open, high, low, close, volume, tick_volume, spread,
                equity, balance, floating_pnl, open_positions_json, pending_orders_json,
                selected_candidate_json, candidate_summary_json, state_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, bar_index) DO UPDATE SET
                timestamp = excluded.timestamp,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                tick_volume = excluded.tick_volume,
                spread = excluded.spread,
                equity = excluded.equity,
                balance = excluded.balance,
                floating_pnl = excluded.floating_pnl,
                open_positions_json = excluded.open_positions_json,
                pending_orders_json = excluded.pending_orders_json,
                selected_candidate_json = excluded.selected_candidate_json,
                candidate_summary_json = excluded.candidate_summary_json,
                state_json = excluded.state_json;
            """,
            (
                int(payload["run_id"]),
                int(payload["bar_index"]),
                str(payload["timestamp"]),
                payload.get("open"),
                payload.get("high"),
                payload.get("low"),
                payload.get("close"),
                payload.get("volume"),
                payload.get("tick_volume"),
                payload.get("spread"),
                float(payload.get("equity", 0.0) or 0.0),
                float(payload.get("balance", 0.0) or 0.0),
                float(payload.get("floating_pnl", 0.0) or 0.0),
                self._json_text(payload.get("open_positions_json")),
                self._json_text(payload.get("pending_orders_json")),
                self._json_text(payload.get("selected_candidate_json")),
                self._json_text(payload.get("candidate_summary_json")),
                self._json_text(payload.get("state_json")),
            ),
        )

    def insert_backtest_playback_event(self, payload: dict[str, Any]) -> None:
        """Persist one replay event."""
        self._execute(
            """
            INSERT INTO backtest_playback_events (
                run_id, bar_index, timestamp, event_type, position_id, signal_id, strategy_name,
                setup_family, side, price, reason_code, event_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(payload["run_id"]),
                int(payload["bar_index"]),
                str(payload["timestamp"]),
                str(payload["event_type"]),
                payload.get("position_id"),
                payload.get("signal_id"),
                payload.get("strategy_name"),
                payload.get("setup_family"),
                payload.get("side"),
                payload.get("price"),
                payload.get("reason_code"),
                self._json_text(payload.get("event_payload_json")),
            ),
        )

    def get_backtest_playback_frames(
        self,
        run_id: int,
        *,
        start_bar: int | None = None,
        end_bar: int | None = None,
        after_bar: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return replay frames for a run with optional cursor/window filtering."""
        statement = "SELECT * FROM backtest_playback_frames WHERE run_id = ?"
        parameters: list[Any] = [int(run_id)]
        if after_bar is not None:
            statement += " AND bar_index > ?"
            parameters.append(int(after_bar))
        if start_bar is not None:
            statement += " AND bar_index >= ?"
            parameters.append(int(start_bar))
        if end_bar is not None:
            statement += " AND bar_index <= ?"
            parameters.append(int(end_bar))
        statement += " ORDER BY bar_index ASC, id ASC"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(int(limit))
        return self._query_dataframe(statement, tuple(parameters))

    def get_backtest_playback_frame(self, run_id: int, bar_index: int) -> dict[str, Any] | None:
        """Return one replay frame by bar index."""
        rows = self._query_dataframe(
            """
            SELECT *
            FROM backtest_playback_frames
            WHERE run_id = ? AND bar_index = ?
            LIMIT 1;
            """,
            (int(run_id), int(bar_index)),
        )
        return rows[0] if rows else None

    def get_backtest_playback_events(
        self,
        run_id: int,
        *,
        start_bar: int | None = None,
        end_bar: int | None = None,
        after_bar: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return replay events for a run with optional cursor/window filtering."""
        statement = "SELECT * FROM backtest_playback_events WHERE run_id = ?"
        parameters: list[Any] = [int(run_id)]
        if after_bar is not None:
            statement += " AND bar_index > ?"
            parameters.append(int(after_bar))
        if start_bar is not None:
            statement += " AND bar_index >= ?"
            parameters.append(int(start_bar))
        if end_bar is not None:
            statement += " AND bar_index <= ?"
            parameters.append(int(end_bar))
        statement += " ORDER BY bar_index ASC, id ASC"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(int(limit))
        return self._query_dataframe(statement, tuple(parameters))

    def get_backtest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent backtest runs."""
        return self._query_dataframe(
            """
            SELECT *
            FROM backtest_runs
            ORDER BY id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )

    def get_active_backtest_runs(self) -> list[dict[str, Any]]:
        """Return backtest runs still marked queued/running."""
        return self._query_dataframe(
            """
            SELECT *
            FROM backtest_runs
            WHERE state IN (?, ?)
            ORDER BY id ASC;
            """,
            (JobState.QUEUED.value, JobState.RUNNING.value),
        )

    def get_backtest_run(self, run_id: int) -> dict[str, Any] | None:
        """Return one backtest run by id."""
        rows = self._query_dataframe(
            """
            SELECT *
            FROM backtest_runs
            WHERE id = ?
            LIMIT 1;
            """,
            (int(run_id),),
        )
        return rows[0] if rows else None

    def create_oos_run(self, payload: dict[str, Any]) -> int:
        """Create an OOS matrix run manifest row and return its id."""
        created_at = str(payload.get("created_at") or utc_now().isoformat())
        state = normalize_job_state(payload.get("state"), JobState.QUEUED.value)
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO oos_runs (
                        created_at, updated_at, started_at, finished_at, state, last_heartbeat_at,
                        title, symbol, timeframe, start_date, end_date, request_json,
                        baseline_run_id, baseline_bundle_path, baseline_database_path, output_dir,
                        report_status, report_path, report_error, progress_current, progress_total,
                        failure_reason, interrupted_reason, notes, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        created_at,
                        payload.get("updated_at") or created_at,
                        payload.get("started_at"),
                        payload.get("finished_at"),
                        state,
                        payload.get("last_heartbeat_at") or created_at,
                        payload.get("title"),
                        payload["symbol"],
                        payload["timeframe"],
                        payload["start_date"],
                        payload["end_date"],
                        self._json_text(payload.get("request_json")),
                        payload.get("baseline_run_id"),
                        payload.get("baseline_bundle_path"),
                        payload.get("baseline_database_path"),
                        payload.get("output_dir"),
                        str(payload.get("report_status") or "pending"),
                        payload.get("report_path"),
                        payload.get("report_error"),
                        int(payload.get("progress_current", 0) or 0),
                        int(payload.get("progress_total", 0) or 0),
                        payload.get("failure_reason"),
                        payload.get("interrupted_reason"),
                        payload.get("notes"),
                        self._json_text(payload.get("metadata_json")),
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)

    def update_oos_run(self, oos_run_id: int, updates: dict[str, Any]) -> None:
        """Update one OOS run row."""
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        json_fields = {"request_json", "metadata_json"}
        int_fields = {"progress_current", "progress_total", "baseline_run_id"}
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            if key == "state":
                values.append(normalize_job_state(value))
            elif key in json_fields:
                values.append(self._json_text(value))
            elif key in int_fields:
                values.append(None if value is None and key == "baseline_run_id" else int(value or 0))
            elif key in {"report_status"}:
                values.append(str(value or "pending"))
            else:
                values.append(None if value in ("", None) else value)
        if "updated_at" not in updates:
            assignments.append("updated_at = ?")
            values.append(utc_now().isoformat())
        values.append(int(oos_run_id))
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE oos_runs SET {', '.join(assignments)} WHERE id = ?;",
                    tuple(values),
                )
                connection.commit()

    def get_oos_run(self, oos_run_id: int) -> dict[str, Any] | None:
        rows = self._query_dataframe(
            """
            SELECT *
            FROM oos_runs
            WHERE id = ?
            LIMIT 1;
            """,
            (int(oos_run_id),),
        )
        return rows[0] if rows else None

    def get_oos_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query_dataframe(
            """
            SELECT *
            FROM oos_runs
            ORDER BY id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )

    def get_active_oos_runs(self) -> list[dict[str, Any]]:
        return self._query_dataframe(
            """
            SELECT *
            FROM oos_runs
            WHERE state IN (?, ?)
            ORDER BY id ASC;
            """,
            (JobState.QUEUED.value, JobState.RUNNING.value),
        )

    def upsert_oos_scenario(self, payload: dict[str, Any]) -> int:
        """Insert or update one OOS scenario manifest row and return its id."""
        now = utc_now().isoformat()
        with self._lock:
            with self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM oos_run_scenarios
                    WHERE oos_run_id = ? AND scenario_key = ?
                    LIMIT 1;
                    """,
                    (int(payload["oos_run_id"]), str(payload["scenario_key"])),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO oos_run_scenarios (
                            oos_run_id, scenario_index, scenario_key, scenario_label, source, status,
                            created_at, updated_at, started_at, finished_at, run_id, export_path,
                            bundle_path, report_path, warning_json, error, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            int(payload["oos_run_id"]),
                            int(payload.get("scenario_index", 0) or 0),
                            str(payload["scenario_key"]),
                            str(payload["scenario_label"]),
                            payload.get("source"),
                            normalize_job_state(payload.get("status"), JobState.QUEUED.value),
                            payload.get("created_at") or now,
                            payload.get("updated_at") or now,
                            payload.get("started_at"),
                            payload.get("finished_at"),
                            payload.get("run_id"),
                            payload.get("export_path"),
                            payload.get("bundle_path"),
                            payload.get("report_path"),
                            self._json_text(payload.get("warning_json")),
                            payload.get("error"),
                            self._json_text(payload.get("metadata_json")),
                        ),
                    )
                    connection.commit()
                    return int(cursor.lastrowid)
                scenario_id = int(existing["id"])
                updates = dict(payload)
                updates.pop("oos_run_id", None)
                self.update_oos_scenario(scenario_id, updates)
                return scenario_id

    def update_oos_scenario(self, scenario_id: int, updates: dict[str, Any]) -> None:
        """Update one OOS scenario row."""
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "id":
                continue
            assignments.append(f"{key} = ?")
            if key == "status":
                values.append(normalize_job_state(value))
            elif key in {"warning_json", "metadata_json"}:
                values.append(self._json_text(value))
            elif key in {"scenario_index", "run_id"}:
                values.append(None if value is None else int(value))
            else:
                values.append(None if value in ("", None) else value)
        if "updated_at" not in updates:
            assignments.append("updated_at = ?")
            values.append(utc_now().isoformat())
        values.append(int(scenario_id))
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE oos_run_scenarios SET {', '.join(assignments)} WHERE id = ?;",
                    tuple(values),
                )
                connection.commit()

    def get_oos_scenarios(self, oos_run_id: int) -> list[dict[str, Any]]:
        return self._query_dataframe(
            """
            SELECT *
            FROM oos_run_scenarios
            WHERE oos_run_id = ?
            ORDER BY scenario_index ASC, id ASC;
            """,
            (int(oos_run_id),),
        )

    def create_job(self, payload: dict[str, Any]) -> int:
        """Insert one queued job row and return its id."""
        created_at = str(payload.get("created_at") or utc_now().isoformat())
        state = normalize_job_state(payload.get("state"), JobState.QUEUED.value)
        job_type = normalize_job_type(payload.get("job_type"), JobType.BACKTEST.value)
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (
                        created_at, updated_at, started_at, finished_at, last_heartbeat_at, job_type, state,
                        payload_json, result_json, progress_current, progress_total, failure_reason,
                        interrupted_reason, cancel_requested, worker_id, parent_job_id, related_run_type,
                        related_run_id, claim_token, claim_expires_at, attempts, max_attempts, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        created_at,
                        payload.get("updated_at") or created_at,
                        payload.get("started_at"),
                        payload.get("finished_at"),
                        payload.get("last_heartbeat_at"),
                        job_type,
                        state,
                        self._json_text(payload.get("payload_json")),
                        self._json_text(payload.get("result_json")),
                        int(payload.get("progress_current", 0) or 0),
                        int(payload.get("progress_total", 0) or 0),
                        payload.get("failure_reason"),
                        payload.get("interrupted_reason"),
                        int(bool(payload.get("cancel_requested", False))),
                        payload.get("worker_id"),
                        payload.get("parent_job_id"),
                        payload.get("related_run_type"),
                        payload.get("related_run_id"),
                        payload.get("claim_token"),
                        payload.get("claim_expires_at"),
                        int(payload.get("attempts", 0) or 0),
                        int(payload.get("max_attempts", 3) or 3),
                        self._json_text(payload.get("metadata_json")),
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)

    def update_job(self, job_id: int, updates: dict[str, Any]) -> None:
        """Update one job row."""
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        json_fields = {"payload_json", "result_json", "metadata_json"}
        int_fields = {"progress_current", "progress_total", "cancel_requested", "parent_job_id", "related_run_id", "attempts", "max_attempts"}
        text_fields = {
            "updated_at", "started_at", "finished_at", "last_heartbeat_at", "failure_reason",
            "interrupted_reason", "worker_id", "related_run_type", "claim_token", "claim_expires_at",
        }
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            if key == "state":
                values.append(normalize_job_state(value))
            elif key == "job_type":
                values.append(normalize_job_type(value))
            elif key in json_fields:
                values.append(self._json_text(value))
            elif key in int_fields:
                values.append(int(value or 0) if value is not None else None)
            elif key in text_fields:
                values.append(None if value in ("", None) else str(value))
            else:
                values.append(value)
        if "updated_at" not in updates:
            assignments.append("updated_at = ?")
            values.append(utc_now().isoformat())
        values.append(int(job_id))
        with self._lock:
            with self._connect() as connection:
                connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?;", tuple(values))
                connection.commit()

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        rows = self._query_dataframe(
            """
            SELECT *
            FROM jobs
            WHERE id = ?
            LIMIT 1;
            """,
            (int(job_id),),
        )
        return rows[0] if rows else None

    def get_jobs(self, limit: int = 50, job_type: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        statement = "SELECT * FROM jobs WHERE 1=1"
        parameters: list[Any] = []
        if job_type:
            statement += " AND job_type = ?"
            parameters.append(normalize_job_type(job_type))
        if state:
            statement += " AND state = ?"
            parameters.append(normalize_job_state(state))
        statement += " ORDER BY id DESC LIMIT ?;"
        parameters.append(int(limit))
        return self._query_dataframe(statement, tuple(parameters))

    def request_job_cancel(self, job_id: int, reason: str | None = None) -> None:
        """Mark a job as cancel-requested and store an interruption reason when present."""
        updates: dict[str, Any] = {"cancel_requested": 1}
        if reason is not None:
            updates["interrupted_reason"] = reason
        self.update_job(job_id, updates)

    def claim_next_job(self, worker_id: str, *, heartbeat_timeout_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically claim the next queued job for one worker."""
        now = utc_now().isoformat()
        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT id
                    FROM jobs
                    WHERE state = ?
                      AND COALESCE(cancel_requested, 0) = 0
                    ORDER BY id ASC
                    LIMIT 1;
                    """,
                    (JobState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    return None
                job_id = int(row["id"])
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, worker_id = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                        last_heartbeat_at = ?, attempts = COALESCE(attempts, 0) + 1,
                        claim_expires_at = ?, failure_reason = NULL, interrupted_reason = NULL
                    WHERE id = ? AND state = ?;
                    """,
                    (
                        JobState.RUNNING.value,
                        str(worker_id),
                        now,
                        now,
                        now,
                        utc_now().isoformat(),
                        job_id,
                        JobState.QUEUED.value,
                    ),
                )
                connection.commit()
                if int(updated.rowcount or 0) <= 0:
                    return None
        return self.get_job(job_id)

    def heartbeat_job(
        self,
        job_id: int,
        *,
        worker_id: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Update heartbeat/progress for a running job."""
        updates: dict[str, Any] = {"last_heartbeat_at": utc_now().isoformat(), "state": JobState.RUNNING.value}
        if worker_id is not None:
            updates["worker_id"] = worker_id
        if progress_current is not None:
            updates["progress_current"] = progress_current
        if progress_total is not None:
            updates["progress_total"] = progress_total
        if result is not None:
            updates["result_json"] = result
        self.update_job(job_id, updates)

    def finalize_job(
        self,
        job_id: int,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        failure_reason: str | None = None,
        interrupted_reason: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        """Write a terminal job state."""
        updates: dict[str, Any] = {
            "state": state,
            "finished_at": utc_now().isoformat(),
            "last_heartbeat_at": utc_now().isoformat(),
        }
        if result is not None:
            updates["result_json"] = result
        if failure_reason is not None:
            updates["failure_reason"] = failure_reason
        if interrupted_reason is not None:
            updates["interrupted_reason"] = interrupted_reason
        if progress_current is not None:
            updates["progress_current"] = progress_current
        if progress_total is not None:
            updates["progress_total"] = progress_total
        self.update_job(job_id, updates)

    def get_active_jobs(self) -> list[dict[str, Any]]:
        """Return jobs still queued/running."""
        return self._query_dataframe(
            """
            SELECT *
            FROM jobs
            WHERE state IN (?, ?)
            ORDER BY id ASC;
            """,
            (JobState.QUEUED.value, JobState.RUNNING.value),
        )

    def get_backtest_signals(self, run_id: int, limit: int = 5000) -> list[dict[str, Any]]:
        """Return backtest signals for a run."""
        return self._query_dataframe(
            """
            SELECT *
            FROM backtest_signals
            WHERE run_id = ?
            ORDER BY id ASC
            LIMIT ?;
            """,
            (int(run_id), int(limit)),
        )

    def get_backtest_trades(self, run_id: int, limit: int = 5000) -> list[dict[str, Any]]:
        """Return backtest trades for a run."""
        return self._query_dataframe(
            """
            SELECT *
            FROM backtest_trades
            WHERE run_id = ?
            ORDER BY id ASC
            LIMIT ?;
            """,
            (int(run_id), int(limit)),
        )

    def get_backtest_equity_curve(self, run_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Return equity curve points for a run."""
        statement = """
            SELECT *
            FROM backtest_equity_curve
            WHERE run_id = ?
            ORDER BY id ASC
        """
        parameters: tuple[Any, ...]
        if limit is None:
            parameters = (int(run_id),)
        else:
            statement += " LIMIT ?"
            parameters = (int(run_id), int(limit))
        return self._query_dataframe(statement, parameters)

    def count_events(
        self,
        day: str | None = None,
        reason_codes: list[str] | None = None,
        event_types: list[str] | None = None,
    ) -> int:
        """Count bot events for a day and optional reason/event filters."""
        statement = "SELECT COUNT(1) AS event_count FROM bot_events WHERE 1=1"
        parameters: list[Any] = []
        if day:
            statement += " AND timestamp LIKE ?"
            parameters.append(f"{day}%")
        if reason_codes:
            placeholders = ", ".join("?" for _ in reason_codes)
            statement += f" AND reason_code IN ({placeholders})"
            parameters.extend(reason_codes)
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            statement += f" AND event_type IN ({placeholders})"
            parameters.extend(event_types)
        rows = self._query_dataframe(statement, tuple(parameters))
        return int(rows[0]["event_count"]) if rows else 0

    def get_trade_by_identity(self, trade_id: str | None = None, mt5_ticket: str | None = None, position_id: str | None = None) -> dict[str, Any] | None:
        """Return a single trade row by identity fields."""
        values = [str(value) for value in [trade_id, mt5_ticket, position_id] if value]
        if not values:
            return None
        statement = """
            SELECT *
            FROM trades
            WHERE trade_id IN ({placeholders})
               OR position_id IN ({placeholders})
               OR mt5_ticket IN ({placeholders})
               OR ticket IN ({placeholders})
               OR order_id IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1;
        """.format(placeholders=", ".join("?" for _ in values))
        parameters = tuple(values * 5)
        rows = self._query_dataframe(statement, parameters)
        return row_to_dict(rows[0]) if rows else None

    def get_open_trades(self) -> list[dict[str, Any]]:
        """Return unresolved open-trade rows."""
        return self._query_dataframe(
            """
            SELECT *
            FROM trades
            WHERE status IN ('OPENED', 'PENDING_CLOSE_RESOLUTION', 'CLOSE_HISTORY_PENDING', 'CLOSE_HISTORY_PARTIAL')
              AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
            ORDER BY COALESCE(created_at, timestamp, updated_at) ASC, id ASC;
            """,
            (),
        )

    def get_pending_resolution_trades(self) -> list[dict[str, Any]]:
        """Return trades waiting for history-based finalization."""
        return self._query_dataframe(
            """
            SELECT *
            FROM trades
            WHERE status IN ('PENDING_CLOSE_RESOLUTION', 'CLOSE_HISTORY_PENDING', 'CLOSE_HISTORY_PARTIAL')
              AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
            ORDER BY COALESCE(last_resolution_attempt_at, closed_at, updated_at, timestamp) ASC, id ASC;
            """,
            (),
        )

    def get_recent_closed_trades(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return recently closed or finalized trades."""
        return self._query_dataframe(
            """
            SELECT *
            FROM trades
            WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED', 'FAILED_CLOSE_RESOLUTION')
              AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
            ORDER BY COALESCE(closed_at, exit_time, updated_at, timestamp) DESC, id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )

    def get_trade_lifecycle_debug_snapshot(self, day: str | None = None, recent_closed_limit: int = 10) -> dict[str, Any]:
        """Return a compact snapshot useful for manual dashboard/lifecycle debugging."""
        target_day = day or utc_now().date().isoformat()
        status_counts = {
            row["status"]: int(row["count"])
            for row in self._query_dataframe(
                """
                SELECT status, COUNT(*) AS count
                FROM trades
                GROUP BY status
                ORDER BY status ASC;
                """,
                (),
            )
        }
        consistency = {}
        with self._lock:
            with self._connect() as connection:
                consistency = self._trade_repair_consistency(connection)
        return {
            "day": target_day,
            "open_trades": self.get_open_trades(),
            "unresolved_trades": self.get_pending_resolution_trades(),
            "recent_closed_trades": self.get_recent_closed_trades(limit=recent_closed_limit),
            "daily_performance_summary": self.build_daily_analytics(target_day),
            "trade_status_counts": status_counts,
            "duplicate_row_count": int(status_counts.get("MERGED_DUPLICATE", 0)),
            "junk_row_count": int(status_counts.get("IGNORED_ORPHAN", 0)),
            "repair_consistency": consistency,
        }

    def get_recent_trade_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest trade lifecycle events."""
        return self._query_dataframe(
            """
            SELECT *
            FROM trade_events
            ORDER BY id DESC
            LIMIT ?;
            """,
            (int(limit),),
        )

    def update_trade_resolution_attempt(
        self,
        trade_id: str,
        status: str,
        attempt_count: int,
        reason: str | None = None,
        raw_mt5_json: Any | None = None,
        *,
        resolution_stage: str | None = None,
        resolution_metadata: Any | None = None,
        matched_order_ids: list[str] | None = None,
        matched_deal_ids: list[str] | None = None,
        matched_volume: float | None = None,
        reconciliation_confidence: float | None = None,
    ) -> None:
        """Persist a retry attempt for unresolved trade finalization."""
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE trades
                    SET status = ?, resolution_attempts = ?, last_resolution_attempt_at = ?, unresolved_reason = COALESCE(?, unresolved_reason),
                        raw_mt5_json = COALESCE(?, raw_mt5_json), resolution_stage = COALESCE(?, resolution_stage),
                        resolution_metadata_json = COALESCE(?, resolution_metadata_json), matched_order_ids = COALESCE(?, matched_order_ids),
                        matched_deal_ids = COALESCE(?, matched_deal_ids), matched_volume = COALESCE(?, matched_volume),
                        reconciliation_confidence = COALESCE(?, reconciliation_confidence),
                        resolution_started_at = COALESCE(resolution_started_at, ?), resolution_last_evidence_at = ?, lifecycle_version = ?,
                        updated_at = ?
                    WHERE trade_id = ? OR mt5_ticket = ? OR position_id = ? OR ticket = ? OR order_id = ?;
                    """,
                    (
                        status,
                        int(attempt_count),
                        utc_now().isoformat(),
                        reason,
                        self._json_text(raw_mt5_json) if raw_mt5_json is not None else None,
                        resolution_stage,
                        self._json_text(resolution_metadata) if resolution_metadata is not None else None,
                        ",".join(str(item) for item in (matched_order_ids or []) if str(item).strip()) or None,
                        ",".join(str(item) for item in (matched_deal_ids or []) if str(item).strip()) or None,
                        matched_volume,
                        reconciliation_confidence,
                        utc_now().isoformat(),
                        utc_now().isoformat(),
                        self._lifecycle_version(status, "TRADE_CLOSED"),
                        utc_now().isoformat(),
                        trade_id,
                        trade_id,
                        trade_id,
                        trade_id,
                        trade_id,
                    ),
                )
                connection.commit()

    def get_maintenance_counter(self, key: str) -> int:
        """Return an integer maintenance counter value."""
        with self._lock:
            with self._connect() as connection:
                return int(self._get_maintenance_value(connection, key, "0") or 0)

    def build_daily_analytics(self, day: str) -> dict[str, Any]:
        """Return aggregated performance analytics for a given UTC day."""
        closed = self._query_dataframe(
            """
            SELECT *
            FROM trades
            WHERE substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10) = ?
              AND status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
              AND trade_id IS NOT NULL AND TRIM(trade_id) != ''
              AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
            ORDER BY COALESCE(closed_at, exit_time, updated_at, timestamp) ASC, id ASC;
            """,
            (day,),
        )
        setups = self._query_dataframe(
            """
            SELECT COUNT(*) AS setup_count
            FROM signals
            WHERE substr(timestamp, 1, 10) = ? AND UPPER(event_type) IN ('SIGNAL_OBSERVED', 'SIGNAL_CREATED', 'CANDIDATE_CREATED');
            """,
            (day,),
        )
        summary = {
            "day": day,
            "setups": int(setups[0]["setup_count"]) if setups else 0,
            "live_trades": len(closed),
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "total_pnl": 0.0,
            "total_r": 0.0,
            "average_r": 0.0,
            "average_pnl": 0.0,
            "average_hold_minutes": 0.0,
            "average_hold_seconds": 0.0,
            "long_trades": 0,
            "short_trades": 0,
            "by_setup_family": {},
            "by_regime": {},
            "by_session": {},
            "by_close_reason": {},
            "by_execution_reason": {},
            "by_blocked_reason": {},
        }
        if not closed:
            return summary

        total_r_values: list[float] = []
        pnl_values: list[float] = []
        positive_pnl = 0.0
        negative_pnl = 0.0
        for row in closed:
            pnl = float(row["pnl"] or 0.0)
            realized_r = float(row["realized_r"] or 0.0)
            hold_minutes = float(row["hold_minutes"] or 0.0)
            hold_seconds = float(row["hold_seconds"] or (hold_minutes * 60.0))
            outcome = str(row["outcome_label"] or self._trade_outcome(pnl, row["win_loss"]))
            summary["total_pnl"] += pnl
            summary["total_r"] += realized_r
            pnl_values.append(pnl)
            total_r_values.append(realized_r)
            summary["average_hold_minutes"] += hold_minutes
            summary["average_hold_seconds"] += hold_seconds
            if outcome == "WIN":
                summary["wins"] += 1
                positive_pnl += pnl
            elif outcome == "LOSS":
                summary["losses"] += 1
                negative_pnl += abs(pnl)
            else:
                summary["breakevens"] += 1

            side = str(row["side"] or "UNKNOWN")
            if side == "LONG":
                summary["long_trades"] += 1
            elif side == "SHORT":
                summary["short_trades"] += 1
            family = str(row["setup_family"] or row["setup"] or row.get("setup_type") or "UNKNOWN")
            regime = str(row["regime"] or row.get("regime_at_entry") or "UNKNOWN")
            session = str(row["session"] or row.get("session_at_entry") or "UNKNOWN")
            summary["by_setup_family"][family] = summary["by_setup_family"].get(family, 0) + 1
            summary["by_regime"][regime] = summary["by_regime"].get(regime, 0) + 1
            summary["by_session"][session] = summary["by_session"].get(session, 0) + 1
            close_reason = str(row["close_reason"] or "unknown")
            execution_reason = str(row["execution_reason"] or "unknown")
            blocked_reason = str(row["blocked_reason"] or "none")
            summary["by_close_reason"][close_reason] = summary["by_close_reason"].get(close_reason, 0) + 1
            summary["by_execution_reason"][execution_reason] = summary["by_execution_reason"].get(execution_reason, 0) + 1
            summary["by_blocked_reason"][blocked_reason] = summary["by_blocked_reason"].get(blocked_reason, 0) + 1

        closed_count = max(len(closed), 1)
        summary["average_r"] = summary["total_r"] / closed_count
        summary["average_pnl"] = summary["total_pnl"] / closed_count
        summary["average_hold_minutes"] = summary["average_hold_minutes"] / closed_count
        summary["average_hold_seconds"] = summary["average_hold_seconds"] / closed_count
        summary["win_rate"] = summary["wins"] / closed_count
        summary["loss_rate"] = summary["losses"] / closed_count
        summary["profit_factor"] = (positive_pnl / negative_pnl) if negative_pnl > 0 else positive_pnl
        summary["expectancy"] = summary["average_pnl"]
        return summary

    def build_trade_performance_report(self, day: str) -> dict[str, Any]:
        """Return detailed performance breakdowns for setup, regime, and session."""
        rows = self._query_dataframe(
            """
            SELECT
                COALESCE(NULLIF(setup_family, ''), NULLIF(setup, ''), NULLIF(setup_type, ''), 'UNKNOWN') AS setup_name,
                COALESCE(NULLIF(regime, ''), NULLIF(regime_at_entry, ''), 'UNKNOWN') AS regime_name,
                COALESCE(NULLIF(session, ''), NULLIF(session_at_entry, ''), 'UNKNOWN') AS session_name,
                COALESCE(close_reason, 'unknown') AS close_reason,
                COALESCE(blocked_reason, 'none') AS blocked_reason,
                COALESCE(NULLIF(outcome_label, ''), CASE WHEN ABS(COALESCE(pnl, 0)) <= 1e-8 THEN 'BREAKEVEN' WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' ELSE 'LOSS' END, 'UNKNOWN') AS outcome_label,
                COUNT(*) AS trade_count,
                AVG(COALESCE(pnl, 0)) AS average_pnl,
                AVG(COALESCE(realized_r, 0)) AS average_r,
                SUM(COALESCE(realized_r, 0)) AS total_r,
                AVG(COALESCE(hold_minutes, 0)) AS average_hold_minutes,
                SUM(CASE WHEN COALESCE(NULLIF(outcome_label, ''), CASE WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' WHEN COALESCE(pnl, 0) < 0 THEN 'LOSS' ELSE 'BREAKEVEN' END) = 'WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN COALESCE(NULLIF(outcome_label, ''), CASE WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' WHEN COALESCE(pnl, 0) < 0 THEN 'LOSS' ELSE 'BREAKEVEN' END) = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN COALESCE(NULLIF(outcome_label, ''), CASE WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' WHEN COALESCE(pnl, 0) < 0 THEN 'LOSS' ELSE 'BREAKEVEN' END) = 'BREAKEVEN' THEN 1 ELSE 0 END) AS breakevens
            FROM trades
            WHERE substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10) = ?
              AND status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
              AND trade_id IS NOT NULL AND TRIM(trade_id) != ''
              AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
            GROUP BY setup_name, regime_name, session_name, close_reason, blocked_reason, outcome_label
            ORDER BY trade_count DESC, total_r DESC;
            """,
            (day,),
        )
        return {"rows": rows_to_dicts(rows)}
