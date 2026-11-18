"""Frozen historical dataset management for Indicator Lab."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
import pytz

from services.database import DatabaseService
from services.history_loader import HistoricalDataLoader
from utils import ensure_directory, utc_now, validate_ohlc_dataframe


DATASET_SOURCE_MT5 = "MT5"
DATASET_SOURCE_CSV = "CSV"
DATASET_STATES = {"CREATING", "READY", "FAILED"}


class IndicatorDatasetService:
    """Create, list, verify, and load immutable frozen candle datasets."""

    def __init__(self, base_dir: str | Path, database: DatabaseService, config: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.database = database
        self.config = config or {}
        self.logger = logger
        self.storage_dir = self.base_dir / "storage" / "indicator_datasets"
        ensure_directory(self.storage_dir)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.database._execute(
            """
            CREATE TABLE IF NOT EXISTS indicator_datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                source TEXT NOT NULL,
                requested_start TEXT NOT NULL,
                requested_end TEXT NOT NULL,
                effective_start TEXT,
                effective_end TEXT,
                bar_count INTEGER DEFAULT 0,
                timezone TEXT DEFAULT 'UTC',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                file_path TEXT,
                content_hash TEXT,
                status TEXT NOT NULL DEFAULT 'CREATING',
                notes TEXT,
                metadata_json TEXT
            );
            """,
            (),
        )

    @staticmethod
    def _json_text(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=True, default=str, sort_keys=True)

    @staticmethod
    def _parse_date(value: str, end: bool = False) -> datetime:
        day = datetime.fromisoformat(str(value)).date()
        return datetime.combine(day, time.max if end else time.min, tzinfo=pytz.UTC)

    @staticmethod
    def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
        normalized = HistoricalDataLoader._normalize_bar_frame(frame, source_kind="mt5", source_name="mt5")
        normalized = normalized.rename(columns={"time": "timestamp_utc"})
        normalized["timestamp_utc"] = pd.to_datetime(normalized["timestamp_utc"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        normalized["timestamp_utc"] = normalized["timestamp_utc"].str.replace(r"(\+0000)$", "+00:00", regex=True)
        return normalized[["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]]

    @staticmethod
    def content_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _insert_dataset(self, payload: dict[str, Any]) -> int:
        now = utc_now().isoformat()
        with self.database._lock:
            with self.database._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO indicator_datasets (
                        name, symbol, timeframe, source, requested_start, requested_end,
                        effective_start, effective_end, bar_count, timezone, created_at, updated_at,
                        file_path, content_hash, status, notes, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        payload["name"],
                        payload["symbol"],
                        payload["timeframe"],
                        payload["source"],
                        payload["requested_start"],
                        payload["requested_end"],
                        payload.get("effective_start"),
                        payload.get("effective_end"),
                        int(payload.get("bar_count", 0) or 0),
                        payload.get("timezone", "UTC"),
                        payload.get("created_at", now),
                        payload.get("updated_at", now),
                        payload.get("file_path"),
                        payload.get("content_hash"),
                        payload.get("status", "CREATING"),
                        payload.get("notes"),
                        self._json_text(payload.get("metadata_json")),
                    ),
                )
                return int(cursor.lastrowid)

    def _update_dataset(self, dataset_id: int, updates: dict[str, Any]) -> None:
        updates = dict(updates)
        updates.setdefault("updated_at", utc_now().isoformat())
        assignments = []
        values = []
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            values.append(self._json_text(value) if key == "metadata_json" else value)
        values.append(int(dataset_id))
        self.database._execute(f"UPDATE indicator_datasets SET {', '.join(assignments)} WHERE id = ?;", tuple(values))

    def create_mt5_dataset(self, *, name: str, symbol: str, timeframe: str, start_date: str, end_date: str, connector: Any | None = None, notes: str | None = None) -> dict[str, Any]:
        symbol = str(symbol).strip().upper()
        timeframe = HistoricalDataLoader._normalize_timeframe_label(timeframe)
        start_dt = self._parse_date(start_date)
        end_dt = self._parse_date(end_date, end=True)
        if start_dt > end_dt:
            raise ValueError("start_date must be before or equal to end_date")
        dataset_id = self._insert_dataset(
            {
                "name": str(name).strip(),
                "symbol": symbol,
                "timeframe": timeframe,
                "source": DATASET_SOURCE_MT5,
                "requested_start": start_dt.isoformat(),
                "requested_end": end_dt.isoformat(),
                "status": "CREATING",
                "notes": notes,
            }
        )
        try:
            if connector is None:
                from logger import BotLogger
                from mt5_connector import MT5Connector

                connector = MT5Connector(self.config, self.logger or BotLogger(self.base_dir, self.config))
            loader = HistoricalDataLoader(self.base_dir, self.config, self.database, self.logger or _NullLogger())
            frame = loader._load_mt5_bars(connector, symbol, timeframe, start_dt, end_dt, min_rows=1)
            if frame.empty:
                raise ValueError("MT5 returned no bars for requested range")
            frozen = self.normalize_frame(frame)
            valid, reason = validate_ohlc_dataframe(frozen.rename(columns={"timestamp_utc": "time"}), min_rows=1)
            if not valid:
                raise ValueError(reason)
            path = self.storage_dir / f"dataset_{dataset_id:06d}_{symbol}_{timeframe}.csv"
            frozen.to_csv(path, index=False, lineterminator="\n")
            digest = self.content_hash(path)
            self._update_dataset(
                dataset_id,
                {
                    "effective_start": str(frozen.iloc[0]["timestamp_utc"]),
                    "effective_end": str(frozen.iloc[-1]["timestamp_utc"]),
                    "bar_count": int(len(frozen)),
                    "file_path": str(path.relative_to(self.base_dir)),
                    "content_hash": digest,
                    "status": "READY",
                    "metadata_json": {"columns": list(frozen.columns), "immutable": True},
                },
            )
        except Exception as exc:
            self._update_dataset(dataset_id, {"status": "FAILED", "metadata_json": {"error": str(exc)}})
            raise
        return self.get_dataset(dataset_id) or {}

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT * FROM indicator_datasets ORDER BY id DESC;", ())
        return [self._parse(row) for row in rows]

    def get_dataset(self, dataset_id: int) -> dict[str, Any] | None:
        rows = self.database._query_dataframe("SELECT * FROM indicator_datasets WHERE id = ? LIMIT 1;", (int(dataset_id),))
        return self._parse(rows[0]) if rows else None

    def _parse(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try:
            item["metadata_json"] = json.loads(item.get("metadata_json") or "{}")
        except Exception:
            item["metadata_json"] = {}
        return item

    def load_frame(self, dataset_id: int) -> pd.DataFrame:
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")
        if dataset.get("status") != "READY":
            raise ValueError(f"Dataset {dataset_id} is not READY")
        path = self.base_dir / str(dataset.get("file_path") or "")
        if not path.exists():
            raise ValueError(f"Dataset file missing: {path}")
        if self.content_hash(path) != dataset.get("content_hash"):
            raise ValueError(f"Dataset {dataset_id} hash verification failed")
        return pd.read_csv(path)

    def verify_hash(self, dataset_id: int) -> dict[str, Any]:
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")
        path = self.base_dir / str(dataset.get("file_path") or "")
        actual = self.content_hash(path) if path.exists() else ""
        return {"dataset_id": int(dataset_id), "ok": bool(actual and actual == dataset.get("content_hash")), "expected": dataset.get("content_hash"), "actual": actual}

    def delete_dataset(self, dataset_id: int) -> None:
        refs = self.database._query_dataframe(
            """
            SELECT COUNT(1) AS ref_count
            FROM indicator_campaigns
            WHERE dataset_id = ? AND state IN ('QUEUED', 'RUNNING', 'PAUSED', 'DRAFT');
            """,
            (int(dataset_id),),
        )
        if refs and int(refs[0]["ref_count"] or 0) > 0:
            raise ValueError("Dataset is referenced by an active campaign")
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            return
        self.database._execute("DELETE FROM indicator_datasets WHERE id = ?;", (int(dataset_id),))


class _NullLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None
