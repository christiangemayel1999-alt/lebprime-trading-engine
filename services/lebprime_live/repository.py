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
                broker_status TEXT NOT NULL,
                broker_created_at TEXT,
                broker_filled_at TEXT,
                broker_closed_at TEXT,
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
        fields = (
            "idempotency_key", "zone_id", "context_key", "entry_number", "logical_symbol",
            "broker_symbol", "side", "order_type", "entry_price", "normalized_price",
            "volume", "raw_calculated_volume", "requested_volume", "minimum_volume",
            "required_margin", "free_margin", "projected_margin_level", "required_margin_level",
            "margin_diagnostics_json", "sl", "tp", "mt5_order_ticket", "mt5_deal_ticket",
            "mt5_position_ticket", "broker_status", "broker_created_at", "broker_filled_at",
            "broker_closed_at", "failure_reason", "historical", "execution_eligible", "comment",
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
                  AND zone.execution_status NOT IN ('CLOSED','CANCELLED','EXPIRED','HISTORICAL_REPLAY')
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
