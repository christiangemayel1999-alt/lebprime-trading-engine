"""SQLite persistence for LebPrime live contexts, families, zones, and events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class LebPrimeLiveRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS lebprime_live_contexts (
                context_key TEXT PRIMARY KEY,
                logical_symbol TEXT NOT NULL,
                mt5_symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                status TEXT NOT NULL,
                last_closed_candle TEXT,
                last_scan_time TEXT,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                live_zones INTEGER NOT NULL DEFAULT 0,
                pending_limits INTEGER NOT NULL DEFAULT 0,
                active_trades INTEGER NOT NULL DEFAULT 0,
                historical_active_trades INTEGER NOT NULL DEFAULT 0,
                realtime_activations INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                heartbeat TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_runtime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                execution_mode TEXT NOT NULL DEFAULT 'DRY',
                startup_block_reason TEXT,
                heartbeat TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_zone_families (
                family_id TEXT PRIMARY KEY,
                logical_symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                zone_bottom REAL NOT NULL,
                zone_top REAL NOT NULL,
                primary_zone_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_live_zones (
                zone_id TEXT PRIMARY KEY,
                logical_symbol TEXT NOT NULL,
                mt5_symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                side TEXT NOT NULL,
                zone_bottom REAL NOT NULL,
                zone_top REAL NOT NULL,
                entry1 REAL,
                entry2 REAL,
                stop_loss REAL,
                tp1 REAL,
                tp2 REAL,
                buffer_used REAL,
                family_id TEXT,
                execution_primary INTEGER NOT NULL DEFAULT 0,
                execution_status TEXT NOT NULL,
                suppressed_by_zone_id TEXT,
                duplicate_reason TEXT,
                entry1_order_id TEXT,
                entry2_order_id TEXT,
                theoretical_state TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_live_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                logical_symbol TEXT NOT NULL,
                mt5_symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                family_id TEXT,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                price REAL,
                broker_order_id TEXT,
                metadata_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_broker_entries (
                idempotency_key TEXT PRIMARY KEY,
                zone_id TEXT NOT NULL,
                context_key TEXT NOT NULL,
                entry_number INTEGER NOT NULL CHECK (entry_number IN (1, 2)),
                logical_symbol TEXT NOT NULL,
                broker_symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                entry_price REAL NOT NULL,
                normalized_price REAL,
                volume REAL,
                raw_calculated_volume REAL,
                requested_volume REAL,
                minimum_volume REAL,
                required_margin REAL,
                free_margin REAL,
                projected_margin_level REAL,
                required_margin_level REAL,
                margin_diagnostics_json TEXT,
                sl REAL,
                tp REAL,
                mt5_order_ticket TEXT,
                mt5_deal_ticket TEXT,
                mt5_position_ticket TEXT,
                fill_price REAL,
                broker_status TEXT NOT NULL,
                broker_created_at TEXT,
                broker_filled_at TEXT,
                broker_closed_at TEXT,
                close_price REAL,
                realized_pnl REAL,
                partial_realized_pnl REAL,
                broker_profit REAL,
                broker_commission REAL,
                broker_swap REAL,
                broker_fee REAL,
                final_exit_reason TEXT,
                broker_retcode TEXT,
                broker_submitted_at TEXT,
                canceled_at TEXT,
                expired_at TEXT,
                filled_volume REAL,
                closed_volume REAL,
                remaining_volume REAL,
                partial_close_count INTEGER NOT NULL DEFAULT 0,
                outcome_complete INTEGER NOT NULL DEFAULT 0,
                position_model TEXT,
                close_deal_tickets_json TEXT,
                lifecycle_details_json TEXT,
                failure_reason TEXT,
                historical INTEGER NOT NULL DEFAULT 0,
                execution_eligible INTEGER NOT NULL DEFAULT 0,
                comment TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_failure TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(zone_id, entry_number)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_notifications (
                notification_key TEXT PRIMARY KEY,
                context_key TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                status TEXT NOT NULL,
                failure_reason TEXT,
                attempted_at TEXT NOT NULL,
                sent_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS lebprime_spread_status (
                logical_symbol TEXT PRIMARY KEY,
                broker_symbol TEXT NOT NULL,
                bid REAL,
                ask REAL,
                point REAL,
                digits INTEGER,
                spread_price REAL,
                spread_points REAL,
                max_spread_points REAL,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_lebprime_family_lookup ON lebprime_zone_families(logical_symbol, side, status)",
            "CREATE INDEX IF NOT EXISTS idx_lebprime_event_zone ON lebprime_live_events(zone_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_lebprime_broker_entry_status ON lebprime_broker_entries(logical_symbol, broker_status)",
            "CREATE INDEX IF NOT EXISTS idx_lebprime_broker_entry_ticket ON lebprime_broker_entries(mt5_order_ticket)",
        ]
        with self._lock, self._connect() as connection:
            for statement in statements:
                connection.execute(statement)
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(lebprime_live_contexts)").fetchall()}
            for column, definition in {
                "historical_active_trades": "INTEGER NOT NULL DEFAULT 0",
                "realtime_activations": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE lebprime_live_contexts ADD COLUMN {column} {definition}")
            broker_columns = {row[1] for row in connection.execute("PRAGMA table_info(lebprime_broker_entries)").fetchall()}
            for column, definition in {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "last_attempt_at": "TEXT",
                "last_failure": "TEXT",
                "retryable": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT",
                "raw_calculated_volume": "REAL",
                "requested_volume": "REAL",
                "minimum_volume": "REAL",
                "required_margin": "REAL",
                "free_margin": "REAL",
                "projected_margin_level": "REAL",
                "required_margin_level": "REAL",
                "margin_diagnostics_json": "TEXT",
                "fill_price": "REAL",
                "close_price": "REAL",
                "realized_pnl": "REAL",
                "partial_realized_pnl": "REAL",
                "broker_profit": "REAL",
                "broker_commission": "REAL",
                "broker_swap": "REAL",
                "broker_fee": "REAL",
                "final_exit_reason": "TEXT",
                "broker_retcode": "TEXT",
                "broker_submitted_at": "TEXT",
                "canceled_at": "TEXT",
                "expired_at": "TEXT",
                "filled_volume": "REAL",
                "closed_volume": "REAL",
                "remaining_volume": "REAL",
                "partial_close_count": "INTEGER NOT NULL DEFAULT 0",
                "outcome_complete": "INTEGER NOT NULL DEFAULT 0",
                "position_model": "TEXT",
                "close_deal_tickets_json": "TEXT",
                "lifecycle_details_json": "TEXT",
            }.items():
                if column not in broker_columns:
                    connection.execute(f"ALTER TABLE lebprime_broker_entries ADD COLUMN {column} {definition}")
            # Before broker-pending execution existed, PRIMARY_PENDING rows were
            # reconstructed from warm-up history. They are not realtime-new
            # events and must never become executable after this migration.
            for entry_number, entry_column in ((1, "entry1"), (2, "entry2")):
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO lebprime_broker_entries (
                        idempotency_key, zone_id, context_key, entry_number, logical_symbol,
                        broker_symbol, side, order_type, entry_price, sl, tp, broker_status,
                        failure_reason, historical, execution_eligible, updated_at
                    )
                    SELECT
                        'lebprime_v3_4_0:' || logical_symbol || '-' || timeframe || ':' || zone_id || ':{entry_number}',
                        zone_id, logical_symbol || '-' || timeframe, {entry_number}, logical_symbol,
                        mt5_symbol, side, CASE WHEN side = 'BUY' THEN 'BUY_LIMIT' ELSE 'SELL_LIMIT' END,
                        {entry_column}, stop_loss, tp2, 'HISTORICAL_REPLAY', 'LEGACY_PRE_BROKER_PENDING',
                        1, 0, updated_at
                    FROM lebprime_live_zones AS zone
                    WHERE execution_status = 'PRIMARY_PENDING' AND {entry_column} IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM lebprime_broker_entries AS broker
                          WHERE broker.zone_id = zone.zone_id AND broker.entry_number = {entry_number}
                      )
                    """
                )
            connection.execute(
                """
                UPDATE lebprime_live_zones
                SET execution_status = 'HISTORICAL_REPLAY'
                WHERE execution_status = 'PRIMARY_PENDING'
                  AND EXISTS (
                      SELECT 1 FROM lebprime_broker_entries AS broker
                      WHERE broker.zone_id = lebprime_live_zones.zone_id
                        AND broker.historical = 1
                  )
                """
            )
            connection.execute(
                """
                UPDATE lebprime_broker_entries
                SET broker_status = 'BLOCKED_PRECHECK'
                WHERE broker_status = 'REJECTED'
                  AND failure_reason IN ('spread_too_wide', 'SPREAD_CHECK_FAILED')
                """
            )
            connection.execute(
                """
                UPDATE lebprime_live_zones
                SET execution_status = 'BLOCKED_PRECHECK'
                WHERE execution_status = 'ORDER_REJECTED'
                  AND EXISTS (
                      SELECT 1 FROM lebprime_broker_entries AS broker
                      WHERE broker.zone_id = lebprime_live_zones.zone_id
                        AND broker.broker_status = 'BLOCKED_PRECHECK'
                  )
                """
            )
            # Releases only the live entries incorrectly made permanent by the
            # former pending-enum margin calculation. Terminal zones remain
            # excluded by retryable_broker_entries() and are never resurrected.
            connection.execute(
                """
                UPDATE lebprime_broker_entries
                SET broker_status = 'RETRY_SCHEDULED',
                    failure_reason = 'ORDER_CALC_MARGIN_FAILED',
                    last_failure = 'ORDER_CALC_MARGIN_FAILED',
                    retryable = 1,
                    execution_eligible = 1,
                    next_retry_at = NULL
                WHERE historical = 0
                  AND broker_status = 'PERMANENT_FAILURE'
                  AND failure_reason IN ('margin_constraint', 'BLOCKED_MARGIN_CONSTRAINT')
                  AND mt5_order_ticket IS NULL
                  AND mt5_deal_ticket IS NULL
                """
            )
            connection.execute(
                """
                UPDATE lebprime_broker_entries
                SET broker_status = 'PERMANENT_FAILURE',
                    retryable = 0,
                    execution_eligible = 0,
                    next_retry_at = NULL
                WHERE historical = 0
                  AND broker_status = 'RETRY_SCHEDULED'
                  AND UPPER(COALESCE(last_failure, failure_reason, '')) LIKE '%SL TOO CLOSE%'
                  AND mt5_order_ticket IS NULL
                  AND mt5_deal_ticket IS NULL
                """
            )
            connection.commit()

    def upsert_context(self, payload: dict[str, Any]) -> None:
        fields = (
            "context_key", "logical_symbol", "mt5_symbol", "timeframe", "status",
            "last_closed_candle", "last_scan_time", "candidate_count", "live_zones",
            "pending_limits", "active_trades", "historical_active_trades", "realtime_activations",
            "last_error", "heartbeat", "updated_at",
        )
        values = tuple(payload.get(field) for field in fields)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_live_contexts ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(context_key) DO UPDATE SET "
                + ",".join(f"{field}=excluded.{field}" for field in fields[1:]),
                values,
            )
            connection.commit()

    def find_live_family(self, logical_symbol: str, side: str, bottom: float, top: float) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM lebprime_zone_families
                WHERE logical_symbol = ? AND side = ? AND status IN ('LIVE', 'PENDING', 'ACTIVE')
                  AND zone_top >= ? AND zone_bottom <= ?
                ORDER BY created_at, family_id LIMIT 1
                """,
                (logical_symbol, side, float(bottom), float(top)),
            ).fetchone()
        return dict(row) if row else None

    def upsert_family(self, payload: dict[str, Any]) -> None:
        fields = ("family_id", "logical_symbol", "side", "zone_bottom", "zone_top", "primary_zone_id", "status", "created_at", "updated_at")
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_zone_families ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(family_id) DO UPDATE SET zone_bottom=MIN(zone_bottom, excluded.zone_bottom), "
                "zone_top=MAX(zone_top, excluded.zone_top), status=excluded.status, updated_at=excluded.updated_at",
                tuple(payload.get(field) for field in fields),
            )
            connection.commit()

    def upsert_zone(self, payload: dict[str, Any]) -> None:
        serial = dict(payload)
        serial["metadata_json"] = json.dumps(serial.get("metadata") or {}, sort_keys=True, default=str)
        fields = (
            "zone_id", "logical_symbol", "mt5_symbol", "timeframe", "side", "zone_bottom", "zone_top",
            "entry1", "entry2", "stop_loss", "tp1", "tp2", "buffer_used", "family_id",
            "execution_primary", "execution_status", "suppressed_by_zone_id", "duplicate_reason",
            "entry1_order_id", "entry2_order_id", "theoretical_state", "created_at", "updated_at", "metadata_json",
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_live_zones ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(zone_id) DO UPDATE SET "
                + ",".join(f"{field}=excluded.{field}" for field in fields[1:]),
                tuple(serial.get(field) for field in fields),
            )
            connection.commit()

    def zone(self, zone_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM lebprime_live_zones WHERE zone_id = ?", (zone_id,)).fetchone()
        return dict(row) if row else None

    def zones_for_symbol(self, logical_symbol: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM lebprime_live_zones WHERE logical_symbol = ? AND execution_status IN ({placeholders})",
                (logical_symbol, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_zone_execution_status(self, zone_id: str, status: str, updated_at: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE lebprime_live_zones SET execution_status = ?, updated_at = ? WHERE zone_id = ?",
                (status, updated_at, zone_id),
            )
            connection.commit()

    def upsert_runtime_state(self, payload: dict[str, Any]) -> None:
        fields = ("id", "started", "pid", "execution_mode", "startup_block_reason", "heartbeat", "updated_at")
        values = {"id": 1, **payload}
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_runtime_state ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{field}=excluded.{field}" for field in fields[1:]),
                tuple(values.get(field) for field in fields),
            )
            connection.commit()

    def runtime_state(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM lebprime_runtime_state WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def append_event(self, payload: dict[str, Any]) -> None:
        fields = ("logical_symbol", "mt5_symbol", "timeframe", "zone_id", "family_id", "event_type", "event_time", "price", "broker_order_id", "metadata_json")
        values = dict(payload)
        values["metadata_json"] = json.dumps(values.get("metadata") or {}, sort_keys=True, default=str)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_live_events ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(values.get(field) for field in fields),
            )
            connection.commit()

    def upsert_broker_entry(self, payload: dict[str, Any]) -> None:
        serial = dict(payload)
        diagnostics = serial.get("margin_diagnostics")
        if diagnostics is not None:
            serial["margin_diagnostics_json"] = json.dumps(diagnostics, sort_keys=True, default=str)
        for source_key, storage_key in (
            ("close_deal_tickets", "close_deal_tickets_json"),
            ("lifecycle_details", "lifecycle_details_json"),
        ):
            if source_key in serial:
                serial[storage_key] = json.dumps(serial.get(source_key), sort_keys=True, default=str)
        fields = (
            "idempotency_key", "zone_id", "context_key", "entry_number", "logical_symbol",
            "broker_symbol", "side", "order_type", "entry_price", "normalized_price",
            "volume", "raw_calculated_volume", "requested_volume", "minimum_volume",
            "required_margin", "free_margin", "projected_margin_level", "required_margin_level",
            "margin_diagnostics_json", "sl", "tp", "mt5_order_ticket", "mt5_deal_ticket",
            "mt5_position_ticket", "fill_price", "broker_status", "broker_created_at", "broker_filled_at",
            "broker_closed_at", "close_price", "realized_pnl", "partial_realized_pnl",
            "broker_profit", "broker_commission", "broker_swap", "broker_fee",
            "final_exit_reason", "broker_retcode", "broker_submitted_at", "canceled_at", "expired_at",
            "filled_volume", "closed_volume", "remaining_volume", "partial_close_count",
            "outcome_complete", "position_model", "close_deal_tickets_json", "lifecycle_details_json",
            "failure_reason", "historical", "execution_eligible", "comment",
            "attempt_count", "last_attempt_at", "last_failure", "retryable", "next_retry_at",
            "updated_at",
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_broker_entries ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                + ",".join(f"{field}=excluded.{field}" for field in fields[1:]),
                tuple(serial.get(field) for field in fields),
            )
            connection.commit()

    def broker_entry(self, zone_id: str, entry_number: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM lebprime_broker_entries WHERE zone_id = ? AND entry_number = ?",
                (zone_id, int(entry_number)),
            ).fetchone()
        return dict(row) if row else None

    def broker_entries_for_zone(self, zone_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lebprime_broker_entries WHERE zone_id = ? ORDER BY entry_number",
                (zone_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def broker_entries_for_symbol(self, logical_symbol: str, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM lebprime_broker_entries WHERE logical_symbol = ?"
        params: list[Any] = [logical_symbol]
        if statuses:
            sql += f" AND broker_status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY zone_id, entry_number"
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def broker_entries_for_reconciliation(self, logical_symbol: str) -> list[dict[str, Any]]:
        """Return live rows whose broker lifecycle may still need reconciliation."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lebprime_broker_entries
                WHERE logical_symbol = ?
                  AND historical = 0
                  AND (
                      broker_status IN ('PLACING','RETRY_SCHEDULED','PENDING','ORDER_HISTORY','FILLED')
                      OR (
                          broker_status = 'CLOSED'
                          AND outcome_complete = 0
                          AND COALESCE(position_model, 'UNKNOWN') != 'NETTING'
                      )
                  )
                ORDER BY updated_at
                """,
                (logical_symbol,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_trade_snapshot(self, limit_per_category: int = 200) -> dict[str, Any]:
        """Return a read-only, broker-authoritative dashboard view of live entries."""
        limit = max(1, min(int(limit_per_category), 1000))
        pending_statuses = ("PENDING",)
        open_statuses = ("FILLED",)
        closed_statuses = ("CLOSED",)
        canceled_statuses = ("CANCELLED", "CANCELED", "CANCEL_FAILED")
        expired_statuses = ("EXPIRED",)
        rejected_statuses = ("REJECTED", "REJECTED_BY_BROKER")
        failed_statuses = (
            "PLACING", "RETRY_SCHEDULED", "ORDER_HISTORY", "PERMANENT_FAILURE",
            "BLOCKED_PRECHECK", "RISK_BLOCKED", "NOT_PLACED",
        )

        def placeholders(values: tuple[str, ...]) -> str:
            return ",".join("?" for _ in values)

        select_sql = """
            SELECT
                broker.idempotency_key,
                broker.zone_id,
                broker.context_key,
                broker.entry_number,
                broker.logical_symbol AS symbol,
                zone.timeframe,
                broker.broker_symbol,
                broker.side,
                broker.order_type,
                COALESCE(broker.normalized_price, broker.entry_price) AS entry,
                broker.entry_price AS requested_entry,
                broker.normalized_price AS normalized_entry,
                broker.fill_price,
                broker.volume,
                broker.filled_volume,
                broker.closed_volume,
                broker.remaining_volume,
                broker.partial_close_count,
                zone.stop_loss AS strategy_sl,
                broker.sl AS broker_sl,
                zone.tp1,
                zone.tp2,
                broker.mt5_order_ticket AS order_ticket,
                broker.mt5_deal_ticket AS deal_ticket,
                broker.mt5_position_ticket AS position_ticket,
                broker.mt5_order_ticket,
                broker.mt5_deal_ticket,
                broker.mt5_position_ticket,
                broker.broker_status AS status,
                COALESCE(broker.broker_created_at, zone.created_at, broker.updated_at) AS created_at,
                broker.broker_submitted_at AS submitted_at,
                broker.broker_filled_at AS filled_at,
                broker.broker_closed_at AS closed_at,
                broker.canceled_at,
                broker.expired_at,
                broker.close_price,
                broker.realized_pnl,
                broker.partial_realized_pnl,
                broker.broker_profit,
                broker.broker_commission,
                broker.broker_swap,
                broker.broker_fee,
                broker.final_exit_reason,
                broker.broker_retcode,
                broker.outcome_complete,
                broker.position_model,
                broker.close_deal_tickets_json,
                COALESCE(broker.last_failure, broker.failure_reason) AS failure_reason,
                broker.attempt_count AS retry_count,
                broker.retryable,
                broker.updated_at
            FROM lebprime_broker_entries AS broker
            LEFT JOIN lebprime_live_zones AS zone ON zone.zone_id = broker.zone_id
            WHERE broker.historical = 0 AND broker.broker_status IN ({statuses})
            ORDER BY COALESCE(broker.broker_closed_at, broker.broker_filled_at,
                              broker.broker_created_at, zone.created_at, broker.updated_at) DESC
            LIMIT ?
        """

        def fetch_category(connection: sqlite3.Connection, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
            rows = connection.execute(
                select_sql.format(statuses=placeholders(statuses)),
                (*statuses, limit),
            ).fetchall()
            payload = [dict(row) for row in rows]
            for row in payload:
                row["outcome_complete"] = bool(row.get("outcome_complete"))
                try:
                    row["close_deal_tickets"] = json.loads(row.pop("close_deal_tickets_json") or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    row["close_deal_tickets"] = []
            return payload

        with self._lock, self._connect() as connection:
            pending = fetch_category(connection, pending_statuses)
            open_entries = fetch_category(connection, open_statuses)
            closed = fetch_category(connection, closed_statuses)
            failed = fetch_category(connection, failed_statuses + rejected_statuses + canceled_statuses + expired_statuses)
            status_rows = connection.execute(
                """
                SELECT broker_status, COUNT(*) AS count
                FROM lebprime_broker_entries
                WHERE historical = 0
                GROUP BY broker_status
                """
            ).fetchall()
            historical_excluded = connection.execute(
                "SELECT COUNT(*) FROM lebprime_broker_entries WHERE historical = 1"
            ).fetchone()[0]
            complete_outcomes = connection.execute(
                """
                SELECT COUNT(*) FROM lebprime_broker_entries
                WHERE historical = 0 AND broker_status = 'CLOSED'
                  AND outcome_complete = 1 AND realized_pnl IS NOT NULL
                """
            ).fetchone()[0]

        status_counts = {str(row["broker_status"]): int(row["count"] or 0) for row in status_rows}
        return {
            "pending": pending,
            "open": open_entries,
            "closed": closed,
            "failed": failed,
            "summary": {
                "pending": sum(status_counts.get(status, 0) for status in pending_statuses),
                "open": sum(status_counts.get(status, 0) for status in open_statuses),
                "closed": sum(status_counts.get(status, 0) for status in closed_statuses),
                "failed": sum(status_counts.get(status, 0) for status in failed_statuses),
                "rejected": sum(status_counts.get(status, 0) for status in rejected_statuses),
                "canceled": sum(status_counts.get(status, 0) for status in canceled_statuses),
                "expired": sum(status_counts.get(status, 0) for status in expired_statuses),
                "complete_outcomes": int(complete_outcomes or 0),
                "historical_replay_excluded": int(historical_excluded or 0),
                "realized_pnl_available": bool(complete_outcomes),
            },
        }

    def dashboard_performance_snapshot(self) -> dict[str, Any]:
        """Aggregate reliable persisted broker outcomes without mixing incomplete rows."""
        with self._lock, self._connect() as connection:
            zones_created = connection.execute(
                """
                SELECT COUNT(*) FROM lebprime_live_zones AS zone
                WHERE zone.execution_status != 'HISTORICAL_REPLAY'
                  AND (zone.execution_primary = 1 OR EXISTS (
                      SELECT 1 FROM lebprime_broker_entries AS broker
                      WHERE broker.zone_id = zone.zone_id AND broker.historical = 0
                  ))
                """
            ).fetchone()[0]
            status_rows = connection.execute(
                """
                SELECT broker_status, COUNT(*) AS count
                FROM lebprime_broker_entries
                WHERE historical = 0
                GROUP BY broker_status
                """
            ).fetchall()
            lifecycle_counts = dict(connection.execute(
                """
                SELECT
                    SUM(CASE WHEN broker_submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS broker_orders_submitted,
                    SUM(CASE WHEN NULLIF(mt5_order_ticket, '') IS NOT NULL
                                  AND broker_status IN ('PENDING','ORDER_HISTORY','FILLED','CLOSED','CANCELLED','CANCELED','EXPIRED')
                             THEN 1 ELSE 0 END) AS broker_orders_accepted,
                    SUM(CASE WHEN NULLIF(mt5_deal_ticket, '') IS NOT NULL THEN 1 ELSE 0 END) AS fills,
                    SUM(CASE WHEN broker_status = 'FILLED' THEN 1 ELSE 0 END) AS open_positions,
                    SUM(CASE WHEN broker_status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_entries,
                    SUM(CASE WHEN broker_status IN ('CANCELLED','CANCELED') THEN 1 ELSE 0 END) AS canceled,
                    SUM(CASE WHEN broker_status = 'EXPIRED' THEN 1 ELSE 0 END) AS expired,
                    SUM(CASE WHEN broker_status IN ('REJECTED','REJECTED_BY_BROKER')
                                  OR final_exit_reason = 'REJECTED' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN NULLIF(mt5_order_ticket, '') IS NULL
                                  AND NULLIF(mt5_deal_ticket, '') IS NULL
                                  AND COALESCE(final_exit_reason, '') != 'REJECTED'
                                  AND broker_status IN ('PERMANENT_FAILURE','BLOCKED_PRECHECK','RISK_BLOCKED','NOT_PLACED')
                             THEN 1 ELSE 0 END) AS failed_precheck
                FROM lebprime_broker_entries
                WHERE historical = 0
                """
            ).fetchone())
            outcome_rows = [
                dict(row) for row in connection.execute(
                    """
                    SELECT broker.logical_symbol AS symbol, zone.timeframe, broker.entry_number,
                           broker.side, broker.realized_pnl, broker.final_exit_reason,
                           broker.outcome_complete
                    FROM lebprime_broker_entries AS broker
                    LEFT JOIN lebprime_live_zones AS zone ON zone.zone_id = broker.zone_id
                    WHERE broker.historical = 0 AND broker.broker_status = 'CLOSED'
                    ORDER BY broker.logical_symbol, zone.timeframe, broker.entry_number
                    """
                ).fetchall()
            ]
            breakdown_rows = connection.execute(
                """
                SELECT broker.logical_symbol AS symbol, zone.timeframe, broker.entry_number,
                       broker.side, broker.broker_status AS status, COUNT(*) AS count
                FROM lebprime_broker_entries AS broker
                LEFT JOIN lebprime_live_zones AS zone ON zone.zone_id = broker.zone_id
                WHERE broker.historical = 0
                GROUP BY broker.logical_symbol, zone.timeframe, broker.entry_number,
                         broker.side, broker.broker_status
                ORDER BY broker.logical_symbol, zone.timeframe, broker.entry_number,
                         broker.side, broker.broker_status
                """
            ).fetchall()

        status_counts = {str(row["broker_status"]): int(row["count"] or 0) for row in status_rows}
        complete_rows = [
            row for row in outcome_rows
            if bool(row.get("outcome_complete")) and row.get("realized_pnl") is not None
        ]
        incomplete_outcomes = len(outcome_rows) - len(complete_rows)
        pnl_values = [float(row["realized_pnl"]) for row in complete_rows]
        winners = [value for value in pnl_values if value > 0]
        losers = [value for value in pnl_values if value < 0]
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        complete_count = len(complete_rows)

        def aggregate_dimension(field: str) -> list[dict[str, Any]]:
            grouped: dict[str, dict[str, Any]] = {}
            for row in outcome_rows:
                key = str(row.get(field) if row.get(field) not in (None, "") else "UNKNOWN")
                item = grouped.setdefault(
                    key,
                    {
                        field: row.get(field),
                        "closed_entries": 0,
                        "complete_outcomes": 0,
                        "incomplete_outcomes": 0,
                        "wins": 0,
                        "losses": 0,
                        "net_realized_pnl": 0.0,
                    },
                )
                item["closed_entries"] += 1
                if bool(row.get("outcome_complete")) and row.get("realized_pnl") is not None:
                    pnl = float(row["realized_pnl"])
                    item["complete_outcomes"] += 1
                    item["net_realized_pnl"] += pnl
                    item["wins"] += int(pnl > 0)
                    item["losses"] += int(pnl < 0)
                else:
                    item["incomplete_outcomes"] += 1
            return list(grouped.values())

        summary = {
            "zones_created": int(zones_created or 0),
            "broker_orders_submitted": int(lifecycle_counts.get("broker_orders_submitted") or 0),
            "broker_orders_accepted": int(lifecycle_counts.get("broker_orders_accepted") or 0),
            "fills": int(lifecycle_counts.get("fills") or 0),
            "open_positions": int(lifecycle_counts.get("open_positions") or 0),
            "closed_entries": int(lifecycle_counts.get("closed_entries") or 0),
            "canceled": int(lifecycle_counts.get("canceled") or 0),
            "expired": int(lifecycle_counts.get("expired") or 0),
            "rejected": int(lifecycle_counts.get("rejected") or 0),
            "failed_precheck": int(lifecycle_counts.get("failed_precheck") or 0),
            "complete_outcomes": complete_count,
            "incomplete_outcomes": incomplete_outcomes,
            "tp_closes": sum(1 for row in complete_rows if row.get("final_exit_reason") == "TP"),
            "sl_closes": sum(1 for row in complete_rows if row.get("final_exit_reason") == "SL"),
            "manual_closes": sum(1 for row in complete_rows if row.get("final_exit_reason") == "MANUAL_CLOSE"),
            "other_closes": sum(1 for row in complete_rows if row.get("final_exit_reason") not in {"TP", "SL", "MANUAL_CLOSE"}),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_realized_pnl": sum(pnl_values),
            "average_winner": (gross_profit / len(winners)) if winners else None,
            "average_loser": (sum(losers) / len(losers)) if losers else None,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
            "win_rate": (len(winners) / complete_count) if complete_count else None,
            "average_r": None,
        }
        return {
            "summary": summary,
            "by_status": [{"status": key, "count": value} for key, value in sorted(status_counts.items())],
            "breakdown": [dict(row) for row in breakdown_rows],
            "results_by_symbol": aggregate_dimension("symbol"),
            "results_by_timeframe": aggregate_dimension("timeframe"),
            "results_by_entry_number": aggregate_dimension("entry_number"),
            "notes": [
                "Broker lifecycle records are authoritative; historical replay entries are excluded.",
                "PnL metrics include only CLOSED rows with outcome_complete=1.",
                "Realized PnL is MT5 deal profit plus commission, swap, and fee.",
                "Netting or unknown position-model rows remain incomplete to prevent duplicate per-entry attribution.",
                "Average R remains unknown because a reliable per-entry risk basis is not persisted.",
            ],
        }



    def retryable_broker_entries(self, now_iso: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT broker.* FROM lebprime_broker_entries AS broker
                JOIN lebprime_live_zones AS zone ON zone.zone_id = broker.zone_id
                WHERE broker.historical = 0
                  AND broker.retryable = 1
                  AND broker.mt5_order_ticket IS NULL
                  AND broker.mt5_deal_ticket IS NULL
                  AND (broker.next_retry_at IS NULL OR broker.next_retry_at <= ?)
                  AND zone.execution_status NOT IN ('CLOSED','CANCELLED','CANCELED','EXPIRED','HISTORICAL_REPLAY')
                ORDER BY broker.next_retry_at, broker.updated_at
                """,
                (now_iso,),
            ).fetchall()
        return [dict(row) for row in rows]

    def notification(self, notification_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM lebprime_notifications WHERE notification_key = ?",
                (notification_key,),
            ).fetchone()
        return dict(row) if row else None

    def record_notification(self, payload: dict[str, Any]) -> bool:
        """Persist the first notification outcome; false means it was already attempted."""
        fields = (
            "notification_key", "context_key", "zone_id", "notification_type",
            "status", "failure_reason", "attempted_at", "sent_at",
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO lebprime_notifications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(payload.get(field) for field in fields),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def update_notification_outcome(
        self,
        notification_key: str,
        *,
        status: str,
        failure_reason: str | None,
        sent_at: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE lebprime_notifications SET status = ?, failure_reason = ?, sent_at = ? WHERE notification_key = ?",
                (status, failure_reason, sent_at, notification_key),
            )
            connection.commit()

    def upsert_spread_status(self, payload: dict[str, Any]) -> None:
        fields = (
            "logical_symbol", "broker_symbol", "bid", "ask", "point", "digits",
            "spread_price", "spread_points", "max_spread_points", "status",
            "rejection_reason", "updated_at",
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO lebprime_spread_status ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                "ON CONFLICT(logical_symbol) DO UPDATE SET "
                + ",".join(f"{field}=excluded.{field}" for field in fields[1:]),
                tuple(payload.get(field) for field in fields),
            )
            connection.commit()

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            contexts = [dict(row) for row in connection.execute("SELECT * FROM lebprime_live_contexts ORDER BY logical_symbol, timeframe").fetchall()]
            counts = {
                "active_zone_families": connection.execute("SELECT COUNT(*) FROM lebprime_zone_families WHERE status IN ('LIVE','PENDING','ACTIVE')").fetchone()[0],
                "pending_orders": connection.execute("SELECT COUNT(*) FROM lebprime_live_zones WHERE execution_status IN ('PRIMARY_PENDING','DRY_RUN_PRIMARY')").fetchone()[0],
                "broker_order_count": connection.execute("SELECT COUNT(*) FROM lebprime_broker_entries WHERE broker_status IN ('PLACING','PENDING')").fetchone()[0],
                "placement_retries": connection.execute("SELECT COUNT(*) FROM lebprime_broker_entries WHERE retryable = 1 AND mt5_order_ticket IS NULL").fetchone()[0],
                "active_positions": connection.execute("SELECT COUNT(*) FROM lebprime_broker_entries WHERE broker_status = 'FILLED'").fetchone()[0],
                "historical_active_states": connection.execute("SELECT COALESCE(SUM(historical_active_trades), 0) FROM lebprime_live_contexts").fetchone()[0],
                "realtime_activated_trades": connection.execute("SELECT COUNT(*) FROM lebprime_broker_entries WHERE broker_status = 'FILLED' AND historical = 0").fetchone()[0],
                "duplicate_suppressed_zones": connection.execute("SELECT COUNT(*) FROM lebprime_live_zones WHERE execution_status = 'DUPLICATE_SUPPRESSED'").fetchone()[0],
            }
            broker_entries = [dict(row) for row in connection.execute(
                "SELECT * FROM lebprime_broker_entries WHERE broker_status IN ('PLACING','RETRY_SCHEDULED','PERMANENT_FAILURE','PENDING','FILLED','REJECTED_BY_BROKER','BLOCKED_PRECHECK','RISK_BLOCKED','NOT_PLACED') ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()]
            for row in broker_entries:
                try:
                    row["margin_diagnostics"] = json.loads(row.get("margin_diagnostics_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    row["margin_diagnostics"] = {}
            spread_status = [dict(row) for row in connection.execute(
                "SELECT * FROM lebprime_spread_status ORDER BY logical_symbol"
            ).fetchall()]
        return {"contexts": contexts, "broker_entries": broker_entries, "spread_status": spread_status, "runtime_state": self.runtime_state(), **counts}

    def events_for_zone(self, zone_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lebprime_live_events WHERE zone_id = ? ORDER BY id",
                (zone_id,),
            ).fetchall()
        return [dict(row) for row in rows]
