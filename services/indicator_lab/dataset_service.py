"""Frozen historical dataset management for Indicator Lab."""

from __future__ import annotations

import hashlib
import json
import os
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
# Closed M1 candles pulled from the VPS MT5 terminal by lebprime-mt5-pull (hourly), same columns as datasets.
MT5_STORE_DIR = Path(os.environ.get("LEBPRIME_MT5_STORE", "/var/lib/lebprime/mt5_data/M1"))
STORE_SYMBOL_ALIASES = {"NAS100": "USTEC", "US100": "USTEC", "GOLD": "XAUUSD", "SILVER": "XAGUSD"}
TIMEFRAME_MINUTES = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "M45": 45, "H1": 60, "H4": 240, "D1": 1440}


def drop_unclosed_bars(frame: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
    """Remove candles that had not closed yet at ``now`` (MT5 returns the forming candle too)."""
    minutes = TIMEFRAME_MINUTES.get(str(timeframe).upper())
    if not minutes or frame.empty:
        return frame
    opened = pd.to_datetime(frame["timestamp_utc"], utc=True)
    closes = opened + pd.Timedelta(minutes=minutes)
    return frame.loc[closes <= pd.Timestamp(now)].reset_index(drop=True)


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
            store_file = self._store_file(symbol) if connector is None else None
            if store_file is not None:
                # No MT5 in this process (the VPS dashboard and worker): build it from the M1 store instead.
                frozen = self._frame_from_store(store_file, timeframe, start_dt, end_dt)
                return self._finish_dataset(dataset_id, symbol, timeframe, frozen, source_note=f"mt5_data store {store_file.name}")
            if connector is None:
                from logger import BotLogger
                from mt5_connector import MT5Connector

                connector = MT5Connector(self.config, self.logger or BotLogger(self.base_dir, self.config))
                # A fresh connector has no terminal session yet; without this every fetch fails with "No IPC connection".
                if not connector.initialize():
                    raise ValueError("Could not connect to the MT5 terminal for history download")
            loader = HistoricalDataLoader(self.base_dir, self.config, self.database, self.logger or _NullLogger())
            minutes = TIMEFRAME_MINUTES.get(str(timeframe).upper(), 60)
            # Keep each request well under MT5's 100k-bar limit (about 50k bars per chunk).
            chunk_days = max(5, min(120, int(50000 * minutes / 1440)))
            frame = self._load_mt5_in_chunks(loader, connector, symbol, timeframe, start_dt, end_dt, chunk_days=chunk_days)
            if frame.empty:
                raise ValueError("MT5 returned no bars for requested range")
            frozen = drop_unclosed_bars(self.normalize_frame(frame), timeframe, utc_now())
            if frozen.empty:
                raise ValueError("MT5 returned no closed bars for requested range")
            return self._finish_dataset(dataset_id, symbol, timeframe, frozen)
        except Exception as exc:
            self._update_dataset(dataset_id, {"status": "FAILED", "metadata_json": {"error": str(exc)}})
            raise

    def _finish_dataset(self, dataset_id: int, symbol: str, timeframe: str, frozen: pd.DataFrame, source_note: str | None = None) -> dict[str, Any]:
        valid, reason = validate_ohlc_dataframe(frozen.rename(columns={"timestamp_utc": "time"}), min_rows=1)
        if not valid:
            raise ValueError(reason)
        path = self.storage_dir / f"dataset_{dataset_id:06d}_{symbol}_{timeframe}.csv"
        frozen.to_csv(path, index=False, lineterminator="\n")
        digest = self.content_hash(path)
        metadata: dict[str, Any] = {"columns": list(frozen.columns), "immutable": True}
        if source_note:
            metadata["source_detail"] = source_note
        self._update_dataset(
            dataset_id,
            {
                "effective_start": str(frozen.iloc[0]["timestamp_utc"]),
                "effective_end": str(frozen.iloc[-1]["timestamp_utc"]),
                "bar_count": int(len(frozen)),
                "file_path": path.relative_to(self.base_dir).as_posix(),
                "content_hash": digest,
                "status": "READY",
                "metadata_json": metadata,
            },
        )
        return self.get_dataset(dataset_id) or {}

    @staticmethod
    def _store_file(symbol: str) -> Path | None:
        """The M1 store file to use when this process has no MT5 of its own, else None."""
        from mt5_connector import MT5_IMPORT_ERROR

        if MT5_IMPORT_ERROR is None:
            return None
        path = MT5_STORE_DIR / f"{STORE_SYMBOL_ALIASES.get(symbol, symbol)}.csv"
        if not path.is_file():
            raise ValueError(
                f"Could not connect to the MT5 terminal for history download, and there is no stored M1 history for {symbol} in {MT5_STORE_DIR}"
            )
        return path

    @staticmethod
    def _frame_from_store(path: Path, timeframe: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        """Closed ``timeframe`` candles in [start, end] built from the stored closed M1 candles."""
        minutes = TIMEFRAME_MINUTES.get(str(timeframe).upper())
        if not minutes:
            raise ValueError(f"Unsupported timeframe for stored history: {timeframe}")
        start_key = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        end_key = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        parts, last_m1 = [], None
        for chunk in pd.read_csv(path, chunksize=500_000, dtype={"timestamp_utc": str}):
            if len(chunk):
                last_m1 = chunk["timestamp_utc"].iloc[-1]
            keep = chunk[(chunk["timestamp_utc"] >= start_key) & (chunk["timestamp_utc"] <= end_key + "+99")]
            if len(keep):
                parts.append(keep)
        if not parts:
            raise ValueError(f"No stored M1 candles in the requested range (store ends {last_m1})")
        frame = pd.concat(parts, ignore_index=True)
        if minutes > 1:
            opened = pd.to_datetime(frame["timestamp_utc"], utc=True)
            epoch = pd.Timestamp("1970-01-01", tz="UTC")
            frame["bucket"] = (opened - epoch) // pd.Timedelta(minutes=minutes)
            frame = frame.groupby("bucket").agg(
                timestamp_utc=("timestamp_utc", "first"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), tick_volume=("tick_volume", "sum"), spread=("spread", "last"), real_volume=("real_volume", "sum"),
            )
            frame["timestamp_utc"] = pd.to_datetime(frame.index * minutes, unit="m", utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            frame = frame.reset_index(drop=True)
        # A candle counts only once its last M1 candle is in the store.
        store_now = pd.Timestamp(parts[-1]["timestamp_utc"].iloc[-1]) + pd.Timedelta(minutes=1)
        frozen = drop_unclosed_bars(frame[["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]], timeframe, store_now.to_pydatetime())
        if frozen.empty:
            raise ValueError("No closed candles in the requested range")
        return frozen

    @staticmethod
    def _load_mt5_in_chunks(loader: Any, connector: Any, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime, chunk_days: int = 120) -> pd.DataFrame:
        """MT5 refuses very long ranges ("Invalid params" beyond its max bars), so fetch in chunks.

        Chunks before the broker's first available bar simply come back empty.
        """
        from datetime import timedelta

        frames = []
        cursor = start_dt
        while cursor <= end_dt:
            chunk_end = min(end_dt, cursor + timedelta(days=chunk_days) - timedelta(microseconds=1))
            try:
                # Overlap neighbouring chunks by 12h so the broker/UTC offset cannot leave gaps at the seams.
                pad = timedelta(hours=12)
                part = loader._load_mt5_bars(connector, symbol, timeframe, max(start_dt, cursor - pad), min(end_dt, chunk_end + pad), min_rows=1)
            except (ValueError, RuntimeError):
                part = pd.DataFrame()
            if not part.empty:
                frames.append(part)
            cursor = chunk_end + timedelta(microseconds=1)
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True)
        return merged.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)

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

    def _dataset_path(self, dataset: dict[str, Any]) -> Path:
        # Datasets created on the Windows PC store paths like storage\\indicator_datasets\\x.csv.
        return self.base_dir / str(dataset.get("file_path") or "").replace("\\", "/")

    def load_frame(self, dataset_id: int) -> pd.DataFrame:
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")
        if dataset.get("status") != "READY":
            raise ValueError(f"Dataset {dataset_id} is not READY")
        path = self._dataset_path(dataset)
        if not path.exists():
            raise ValueError(f"Dataset file missing: {path}")
        if self.content_hash(path) != dataset.get("content_hash"):
            raise ValueError(f"Dataset {dataset_id} hash verification failed")
        return pd.read_csv(path)

    def verify_hash(self, dataset_id: int) -> dict[str, Any]:
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")
        path = self._dataset_path(dataset)
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
