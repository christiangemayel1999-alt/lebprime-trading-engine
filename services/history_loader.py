"""Shared historical market data loader with MT5, cache, and CSV fallback."""

from __future__ import annotations

import json
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from services.database import DatabaseService
from utils import ensure_directory, to_utc, timeframe_duration, utc_now, validate_ohlc_dataframe


CANONICAL_BAR_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
CANONICAL_TICK_COLUMNS = ["time", "bid", "ask", "last", "volume", "spread", "flags"]


@dataclass(slots=True)
class HistoryResolution:
    """Resolved historical dataset bundle for one backtest run."""

    source_kind: str
    bar_frames: dict[str, pd.DataFrame]
    source_details: dict[str, Any]


class HistoricalDataLoader:
    """Resolve historical bars from MT5, local cache, or CSV exports.

    The loader uses a single source tier per resolution attempt so that a run
    does not silently mix MT5, cache, and CSV rows in the same canonical dataset.
    """

    def __init__(self, base_dir: str | Path, config: dict[str, Any], database: DatabaseService, logger: Any) -> None:
        self.base_dir = Path(base_dir)
        self.config = config
        self.database = database
        self.logger = logger

    def _storage_cfg(self) -> dict[str, Any]:
        return dict(self.config.get("storage", {}) or {})

    def _backtest_cfg(self) -> dict[str, Any]:
        return dict(self.config.get("backtest", {}) or {})

    def _cache_db_path(self) -> Path:
        # The cache lives inside the active backtest database so replay and history
        # resolution observe the same canonical store as the running job.
        return Path(self.database.db_path)

    def _configured_cache_db_path(self) -> Path:
        storage_cfg = self._storage_cfg()
        cache_path = storage_cfg.get("history_cache_path") or "storage/history_cache.db"
        return self.base_dir / str(cache_path)

    def _csv_roots(self) -> list[Path]:
        roots: list[Path] = []
        for raw in [
            *list(self._backtest_cfg().get("history_csv_fallback_dirs", []) or []),
            *list(self._storage_cfg().get("history_csv_fallback_dirs", []) or []),
        ]:
            candidate = (self.base_dir / str(raw)).resolve()
            if candidate not in roots:
                roots.append(candidate)
        return roots

    @staticmethod
    def _timeframe_minutes(timeframe_label: str) -> int:
        return int(timeframe_duration(str(timeframe_label)).total_seconds() // 60)

    @staticmethod
    def _normalize_time_index(series: pd.Series) -> pd.Series:
        converted = pd.to_datetime(series, utc=True, errors="coerce")
        if converted.isna().any():
            raise ValueError("Unable to parse one or more historical timestamps")
        return converted

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol).strip().upper()

    @classmethod
    def _normalize_timeframe_label(cls, timeframe_label: str) -> str:
        raw = str(timeframe_label).strip()
        normalized = raw.lower().replace(" ", "")
        canonical_map = {
            "1": "M1",
            "1m": "M1",
            "1min": "M1",
            "m1": "M1",
            "3": "M3",
            "3m": "M3",
            "3min": "M3",
            "m3": "M3",
            "5": "M5",
            "5m": "M5",
            "5min": "M5",
            "m5": "M5",
            "15": "M15",
            "15m": "M15",
            "15min": "M15",
            "m15": "M15",
            "30": "M30",
            "30m": "M30",
            "30min": "M30",
            "m30": "M30",
            "60": "H1",
            "60m": "H1",
            "60min": "H1",
            "1h": "H1",
            "h1": "H1",
            "240": "H4",
            "240m": "H4",
            "240min": "H4",
            "4h": "H4",
            "h4": "H4",
            "1440": "D1",
            "1440m": "D1",
            "1440min": "D1",
            "1d": "D1",
            "d1": "D1",
        }
        if normalized in canonical_map:
            return canonical_map[normalized]
        if normalized.upper() in {"M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"}:
            return normalized.upper()
        return str(timeframe_label).strip().upper()

    @classmethod
    def _timeframe_aliases(cls, timeframe_label: str) -> list[str]:
        canonical = cls._normalize_timeframe_label(timeframe_label)
        raw = str(timeframe_label).strip()
        variants = {
            canonical,
            raw,
            raw.upper(),
            raw.lower(),
        }
        alias_map = {
            "M1": {"1min", "1m", "m1"},
            "M3": {"3min", "3m", "m3"},
            "M5": {"5min", "5m", "m5"},
            "M15": {"15min", "15m", "m15"},
            "M30": {"30min", "30m", "m30"},
            "H1": {"60min", "60m", "1h", "h1"},
            "H4": {"240min", "240m", "4h", "h4"},
            "D1": {"1440min", "1440m", "1d", "d1"},
        }
        variants.update(alias_map.get(canonical, set()))
        ordered: list[str] = []
        seen: set[str] = set()
        for variant in variants:
            if not variant:
                continue
            key = variant.strip()
            if not key:
                continue
            normalized_key = key.lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            ordered.append(key)
        return ordered

    def _log_structured(self, category: str, payload: dict[str, Any], level: str = "INFO") -> None:
        structured = getattr(self.logger, "structured", None)
        if callable(structured):
            structured(category, payload, level=level)
            return
        message = json.dumps({"category": category, **payload}, ensure_ascii=True, default=str, sort_keys=True)
        log_method = getattr(self.logger, level.lower(), None)
        if callable(log_method):
            log_method(message)
        else:
            fallback = getattr(self.logger, "info", None)
            if callable(fallback):
                fallback(message)

    def _timeframe_duration_minutes(self, timeframe_label: str) -> int:
        return self._timeframe_minutes(self._normalize_timeframe_label(timeframe_label))

    @staticmethod
    def _utc_timestamp(value: datetime | str | pd.Timestamp) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            return ts.tz_localize("UTC")
        return ts.tz_convert("UTC")

    @classmethod
    def _normalize_bar_frame(cls, frame: pd.DataFrame, *, source_kind: str, source_name: str) -> pd.DataFrame:
        if frame.empty:
            return frame.copy()
        normalized = frame.copy()
        rename_map = {str(column).strip().lower().replace(" ", "_"): column for column in normalized.columns}

        # Common MT5 export / CSV aliases.
        for canonical, aliases in {
            "time": ["time", "datetime", "timestamp", "date_time", "date"],
            "open": ["open", "o"],
            "high": ["high", "h"],
            "low": ["low", "l"],
            "close": ["close", "c"],
            "tick_volume": ["tick_volume", "tickvolume", "volume_tick", "vol_ticks", "tick vol", "volume"],
            "spread": ["spread", "sp", "spread_points"],
            "real_volume": ["real_volume", "realvolume", "volume_real", "real vol"],
        }.items():
            for alias in aliases:
                source_column = rename_map.get(alias)
                if source_column is not None:
                    if source_column != canonical:
                        normalized[canonical] = normalized[source_column]
                    break

        if "time" not in normalized.columns:
            date_column = next((rename_map.get(alias) for alias in ("date", "day")), None)
            if date_column is not None:
                normalized["time"] = normalized[date_column]
            else:
                raise ValueError("Historical bars are missing a time/date column")

        if {"open", "high", "low", "close"}.difference(normalized.columns):
            missing = sorted({"open", "high", "low", "close"}.difference(normalized.columns))
            raise ValueError(f"Historical bars are missing OHLC columns: {missing}")

        normalized["time"] = cls._normalize_time_index(normalized["time"])
        for column in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume"):
            if column not in normalized.columns:
                if column in {"tick_volume", "spread", "real_volume"}:
                    normalized[column] = 0.0
                else:
                    raise ValueError(f"Historical bars are missing required column: {column}")
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

        normalized["tick_volume"] = normalized["tick_volume"].fillna(0).astype(float)
        normalized["spread"] = normalized["spread"].fillna(0).astype(float)
        normalized["real_volume"] = normalized["real_volume"].fillna(normalized["tick_volume"]).astype(float)
        normalized = normalized[["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]]
        normalized = normalized.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
        normalized["source_kind"] = source_kind
        normalized["source_name"] = source_name
        return normalized

    @staticmethod
    def _range_mask(frame: pd.DataFrame, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
        start = HistoricalDataLoader._utc_timestamp(start_utc)
        end = HistoricalDataLoader._utc_timestamp(end_utc)
        return frame[(frame["time"] >= start) & (frame["time"] <= end)].copy().reset_index(drop=True)

    def _cache_key(self, symbol: str, timeframe_label: str) -> tuple[str, str]:
        return self._normalize_symbol(symbol), self._normalize_timeframe_label(timeframe_label)

    def _upsert_cache(self, symbol: str, timeframe_label: str, frame: pd.DataFrame, source_kind: str) -> None:
        if frame.empty:
            return
        rows: list[dict[str, Any]] = []
        now = utc_now().isoformat()
        symbol_key, timeframe_key = self._cache_key(symbol, timeframe_label)
        for row in frame.to_dict("records"):
            rows.append(
                {
                    "symbol": symbol_key,
                    "timeframe": timeframe_key,
                    "time": (
                        pd.Timestamp(row["time"]).tz_localize("UTC").isoformat()
                        if pd.Timestamp(row["time"]).tzinfo is None
                        else pd.Timestamp(row["time"]).tz_convert("UTC").isoformat()
                    ),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_volume": float(row.get("tick_volume", 0.0) or 0.0),
                    "spread": float(row.get("spread", 0.0) or 0.0),
                    "real_volume": float(row.get("real_volume", row.get("tick_volume", 0.0)) or 0.0),
                    "source_kind": source_kind,
                    "updated_at": now,
                }
            )
        self.database.upsert_history_bars(rows)

    def _load_cached_bars(self, symbol: str, timeframe_label: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
        symbol_key, timeframe_key = self._cache_key(symbol, timeframe_label)
        rows = self.database._query_dataframe(
            """
            SELECT time, open, high, low, close, tick_volume, spread, real_volume, source_kind
            FROM history_cache_bars
            WHERE symbol = ? AND timeframe = ? AND time >= ? AND time <= ?
            ORDER BY time ASC, id ASC;
            """,
            (symbol_key, timeframe_key, start_utc.isoformat(), end_utc.isoformat()),
        )
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["time"] = self._normalize_time_index(frame["time"])
        return self._normalize_bar_frame(frame, source_kind="cache", source_name="sqlite_cache")

    def _candidate_csv_paths(self, symbol: str, timeframe_label: str) -> list[Path]:
        symbol_key = self._normalize_symbol(symbol)
        timeframe_variants = self._timeframe_aliases(timeframe_label)
        patterns: list[str] = []
        for timeframe_variant in timeframe_variants:
            patterns.extend(
                [
                    f"{symbol_key}_{timeframe_variant}.csv",
                    f"{symbol_key}-{timeframe_variant}.csv",
                    f"{timeframe_variant}_{symbol_key}.csv",
                    f"{timeframe_variant}-{symbol_key}.csv",
                    f"{symbol_key}_{timeframe_variant}_*.csv",
                    f"{symbol_key}-{timeframe_variant}-*.csv",
                ]
            )
        candidates: list[Path] = []
        csv_roots = self._csv_roots()
        self._log_structured(
            "history_csv_resolution_roots",
            {
                "symbol": symbol_key,
                "timeframe": self._normalize_timeframe_label(timeframe_label),
                "requested_timeframe": str(timeframe_label),
                "roots": [str(root) for root in csv_roots],
                "patterns": patterns,
            },
        )
        for root in csv_roots:
            if not root.exists():
                continue
            if root.is_file() and root.suffix.lower() == ".csv":
                candidates.append(root)
                continue
            for pattern in patterns:
                candidates.extend(sorted(root.rglob(pattern)))
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        if unique:
            self._log_structured(
                "history_csv_candidate_matches",
                {
                    "symbol": symbol_key,
                    "timeframe": self._normalize_timeframe_label(timeframe_label),
                    "requested_timeframe": str(timeframe_label),
                    "candidates": [str(path) for path in unique],
                },
            )
        else:
            self._log_structured(
                "history_csv_candidate_matches",
                {
                    "symbol": symbol_key,
                    "timeframe": self._normalize_timeframe_label(timeframe_label),
                    "requested_timeframe": str(timeframe_label),
                    "reason": "no_candidate_files_matched",
                    "roots": [str(root) for root in csv_roots],
                    "patterns": patterns,
                },
                level="WARNING",
            )
        return unique

    @staticmethod
    def _read_csv_frame(path: Path) -> pd.DataFrame:
        try:
            frame = pd.read_csv(path, sep=None, engine="python")
        except Exception as exc:
            try:
                frame = pd.read_csv(path, sep="\t")
            except Exception as tab_exc:
                raise ValueError(f"Unable to parse CSV export {path}: {exc}") from tab_exc
        if frame.empty:
            raise ValueError(f"CSV export {path} is empty")
        return frame

    @staticmethod
    def _combine_date_time_columns(frame: pd.DataFrame) -> pd.Series | None:
        lower = {str(column).strip().lower().replace(" ", "_"): column for column in frame.columns}
        if "datetime" in lower:
            return pd.to_datetime(frame[lower["datetime"]], utc=True, errors="coerce")
        if "timestamp" in lower:
            return pd.to_datetime(frame[lower["timestamp"]], utc=True, errors="coerce")
        date_column = lower.get("date")
        time_column = lower.get("time")
        if date_column is not None and time_column is not None:
            return pd.to_datetime(
                frame[date_column].astype(str).str.strip() + " " + frame[time_column].astype(str).str.strip(),
                utc=True,
                errors="coerce",
            )
        if date_column is not None:
            return pd.to_datetime(frame[date_column], utc=True, errors="coerce")
        return None

    def _load_csv_bars(self, symbol: str, timeframe_label: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
        candidates = self._candidate_csv_paths(symbol, timeframe_label)
        if not candidates:
            self._log_structured(
                "history_csv_resolution",
                {
                    "symbol": self._normalize_symbol(symbol),
                    "timeframe": self._normalize_timeframe_label(timeframe_label),
                    "requested_timeframe": str(timeframe_label),
                    "requested_start": start_utc.isoformat(),
                    "requested_end": end_utc.isoformat(),
                    "success": False,
                    "failure_reason": "no_csv_candidate_files_found",
                    "rows_returned": 0,
                },
                level="WARNING",
            )
            raise ValueError(
                f"no_csv_candidate_files_found for {self._normalize_symbol(symbol)} "
                f"{self._normalize_timeframe_label(timeframe_label)} | roots={[str(root) for root in self._csv_roots()]}"
            )
        errors: list[str] = []
        requested_start = self._utc_timestamp(start_utc)
        requested_end = self._utc_timestamp(end_utc)
        for path in candidates:
            try:
                raw = self._read_csv_frame(path)
                if raw.empty:
                    continue
                lower = {str(column).strip().lower().replace(" ", "_"): column for column in raw.columns}
                if "time" not in lower and "timestamp" not in lower and "date" not in lower and "datetime" not in lower:
                    raise ValueError(f"CSV export {path} has no recognizable time/date column")
                parsed_time = self._combine_date_time_columns(raw)
                if parsed_time is None:
                    raise ValueError(f"CSV export {path} has no recognizable time/date column")
                raw = raw.copy()
                raw["time"] = parsed_time
                coverage = raw["time"].dropna()
                coverage_start = coverage.min() if not coverage.empty else None
                coverage_end = coverage.max() if not coverage.empty else None
                if coverage_start is not None and coverage_end is not None:
                    self._log_structured(
                        "history_csv_candidate_coverage",
                        {
                            "symbol": self._normalize_symbol(symbol),
                            "timeframe": self._normalize_timeframe_label(timeframe_label),
                            "requested_timeframe": str(timeframe_label),
                            "candidate_path": str(path),
                            "requested_start": requested_start.isoformat(),
                            "requested_end": requested_end.isoformat(),
                            "coverage_start": self._utc_timestamp(coverage_start).isoformat(),
                            "coverage_end": self._utc_timestamp(coverage_end).isoformat(),
                        },
                    )
                normalized = self._normalize_bar_frame(raw, source_kind="csv", source_name=str(path))
                normalized = self._range_mask(normalized, start_utc, end_utc)
                if normalized.empty:
                    self._log_structured(
                        "history_csv_candidate_skipped",
                        {
                            "symbol": self._normalize_symbol(symbol),
                            "timeframe": self._normalize_timeframe_label(timeframe_label),
                            "requested_timeframe": str(timeframe_label),
                            "candidate_path": str(path),
                            "requested_start": requested_start.isoformat(),
                            "requested_end": requested_end.isoformat(),
                            "failure_reason": "candidate_does_not_overlap_requested_range",
                            "coverage_start": self._utc_timestamp(coverage_start).isoformat() if coverage_start is not None else None,
                            "coverage_end": self._utc_timestamp(coverage_end).isoformat() if coverage_end is not None else None,
                        },
                        level="WARNING",
                    )
                    continue
                valid, reason = validate_ohlc_dataframe(normalized, min_rows=1)
                if not valid:
                    raise ValueError(f"Malformed CSV export {path}: {reason}")
                return normalized
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        if errors:
            raise ValueError(
                "CSV fallback files were found but could not be parsed or did not overlap the requested range:\n"
                + "\n".join(errors[:5])
            )
        failure_reason = "csv_candidates_found_but_none_overlapped_requested_range"
        self._log_structured(
            "history_csv_resolution",
            {
                "symbol": self._normalize_symbol(symbol),
                "timeframe": self._normalize_timeframe_label(timeframe_label),
                "requested_timeframe": str(timeframe_label),
                "requested_start": start_utc.isoformat(),
                "requested_end": end_utc.isoformat(),
                "success": False,
                "failure_reason": failure_reason,
                "rows_returned": 0,
                "candidates": [str(path) for path in candidates],
            },
            level="WARNING",
        )
        raise ValueError(
            f"CSV fallback files matched but none overlapped the requested range for "
            f"{self._normalize_symbol(symbol)} {self._normalize_timeframe_label(timeframe_label)} | "
            f"failure_reason=candidate_does_not_overlap_requested_range | "
            f"requested_start={start_utc.isoformat()} | requested_end={end_utc.isoformat()} | "
            f"candidates={[str(path) for path in candidates]}"
        )

    def _load_mt5_bars(
        self,
        connector: Any | None,
        symbol: str,
        timeframe_label: str,
        start_utc: datetime,
        end_utc: datetime,
        min_rows: int,
    ) -> pd.DataFrame:
        if connector is None:
            return pd.DataFrame()
        fetch = getattr(connector, "fetch_rates_range", None)
        if not callable(fetch):
            return pd.DataFrame()
        original_symbol = getattr(connector, "symbol", None)
        symbol_requested = self._normalize_symbol(symbol)
        timeframe_requested = self._normalize_timeframe_label(timeframe_label)
        try:
            if original_symbol and str(original_symbol).upper() != symbol_requested and hasattr(connector, "symbol"):
                connector.symbol = symbol_requested
            frame = fetch(timeframe_requested, start_utc, end_utc, min_rows=min_rows)
            if frame is None or getattr(frame, "empty", True):
                return pd.DataFrame()
            normalized = self._normalize_bar_frame(pd.DataFrame(frame), source_kind="mt5", source_name="mt5")
            normalized = self._range_mask(normalized, start_utc, end_utc)
            valid, reason = validate_ohlc_dataframe(normalized, min_rows=min_rows)
            if not valid:
                raise ValueError(reason)
            return normalized
        finally:
            if original_symbol and hasattr(connector, "symbol"):
                connector.symbol = original_symbol

    def resolve_bar_bundle(
        self,
        *,
        connector: Any | None,
        symbol: str,
        timeframe_requests: dict[str, tuple[datetime, datetime, int]],
    ) -> HistoryResolution:
        """Resolve a canonical multi-timeframe dataset from one source tier."""
        source_reports: list[dict[str, Any]] = []
        for source_kind in ("mt5", "cache", "csv"):
            bar_frames: dict[str, pd.DataFrame] = {}
            source_details: dict[str, Any] = {
                "source_kind": source_kind,
                "symbol": self._normalize_symbol(symbol),
                "requested_timeframes": {},
                "timeframes": {},
                "cache_db_path": str(self._cache_db_path()),
                "configured_cache_db_path": str(self._configured_cache_db_path()),
            }
            self._log_structured(
                "history_cache_path_report",
                {
                    "symbol": self._normalize_symbol(symbol),
                    "active_cache_db_path": str(self._cache_db_path()),
                    "configured_cache_db_path": str(self._configured_cache_db_path()),
                },
            )
            try:
                for original_timeframe_label, (start_utc, end_utc, min_rows) in timeframe_requests.items():
                    timeframe_label = self._normalize_timeframe_label(original_timeframe_label)
                    source_details["requested_timeframes"][str(original_timeframe_label)] = {
                        "canonical_timeframe": timeframe_label,
                        "requested_timeframe": str(original_timeframe_label),
                        "requested_start": start_utc.isoformat(),
                        "requested_end": end_utc.isoformat(),
                        "min_rows": int(min_rows),
                    }
                    if source_kind == "mt5":
                        frame = self._load_mt5_bars(connector, symbol, timeframe_label, start_utc, end_utc, min_rows)
                    elif source_kind == "cache":
                        frame = self._load_cached_bars(symbol, timeframe_label, start_utc, end_utc)
                    else:
                        frame = self._load_csv_bars(symbol, timeframe_label, start_utc, end_utc)
                    if frame.empty:
                        raise ValueError(f"No {timeframe_label} bars available from {source_kind}")
                    bar_frames[str(original_timeframe_label)] = frame
                    source_details["timeframes"][str(original_timeframe_label)] = {
                        "canonical_timeframe": timeframe_label,
                        "rows": int(len(frame)),
                        "start": frame.iloc[0]["time"].isoformat() if not frame.empty else None,
                        "end": frame.iloc[-1]["time"].isoformat() if not frame.empty else None,
                    }
                if source_kind in {"mt5", "csv"}:
                    for original_timeframe_label, frame in bar_frames.items():
                        self._upsert_cache(symbol, original_timeframe_label, frame, source_kind)
                self.logger.info(
                    f"Historical dataset resolved | source={source_kind.upper()} | "
                    f"symbol={str(symbol).upper()} | timeframes={','.join(sorted(bar_frames))}"
                )
                source_details["success"] = True
                source_details["rows_returned"] = int(sum(len(frame) for frame in bar_frames.values()))
                source_snapshot = {key: value for key, value in source_details.items() if key != "source_attempts"}
                source_details["source_attempts"] = [dict(report) for report in source_reports] + [source_snapshot]
                source_reports.append(source_details)
                self._log_structured(
                    "historical_dataset_source_result",
                    {
                        "symbol": self._normalize_symbol(symbol),
                        "source_kind": source_kind,
                        "success": True,
                        "rows_returned": source_details["rows_returned"],
                        "cache_db_path": source_details["cache_db_path"],
                        "configured_cache_db_path": source_details["configured_cache_db_path"],
                        "requested_timeframes": source_details["requested_timeframes"],
                        "timeframes": source_details["timeframes"],
                    },
                )
                return HistoryResolution(source_kind=source_kind, bar_frames=bar_frames, source_details=source_details)
            except Exception as exc:
                source_details["success"] = False
                source_details["failure_reason"] = str(exc)
                source_details["rows_returned"] = int(sum(len(frame) for frame in bar_frames.values()))
                source_details["timeframes"] = source_details.get("timeframes", {})
                self.logger.warning(
                    f"Historical dataset source failed | source={source_kind.upper()} | symbol={str(symbol).upper()} | error={exc}"
                )
                self._log_structured(
                    "historical_dataset_source_result",
                    {
                        "symbol": self._normalize_symbol(symbol),
                        "source_kind": source_kind,
                        "success": False,
                        "rows_returned": source_details["rows_returned"],
                        "failure_reason": str(exc),
                        "cache_db_path": source_details["cache_db_path"],
                        "configured_cache_db_path": source_details["configured_cache_db_path"],
                        "requested_timeframes": source_details["requested_timeframes"],
                        "timeframes": source_details["timeframes"],
                    },
                    level="WARNING",
                )
                source_reports.append(source_details)
        failure_report = {
            "symbol": self._normalize_symbol(symbol),
            "requested_timeframes": {
                str(timeframe): {
                    "canonical_timeframe": self._normalize_timeframe_label(timeframe),
                    "requested_timeframe": str(timeframe),
                    "requested_start": start_utc.isoformat(),
                    "requested_end": end_utc.isoformat(),
                    "min_rows": int(min_rows),
                }
                for timeframe, (start_utc, end_utc, min_rows) in timeframe_requests.items()
            },
            "source_results": source_reports,
        }
        self._log_structured("historical_dataset_resolution_failed", failure_report, level="ERROR")
        raise RuntimeError(
            "Unable to resolve historical dataset for "
            f"{self._normalize_symbol(symbol)} across MT5, cache, and CSV sources | "
            f"mt5={self._source_failure_summary(source_reports, 'mt5')} | "
            f"cache={self._source_failure_summary(source_reports, 'cache')} | "
            f"csv={self._source_failure_summary(source_reports, 'csv')}"
        )

    @staticmethod
    def _source_failure_summary(source_reports: list[dict[str, Any]], source_kind: str) -> str:
        if not source_reports:
            return f"{source_kind}:unavailable"
        matching = next((report for report in source_reports if report.get("source_kind") == source_kind), None)
        if matching is None:
            return f"{source_kind}:not_attempted"
        if matching.get("success") is True:
            return f"{source_kind}:success rows={matching.get('rows_returned', 0)}"
        if matching.get("source_kind") == source_kind:
            return f"{source_kind}:failure reason={matching.get('failure_reason') or 'unknown'} rows={matching.get('rows_returned', 0)}"
        return f"{source_kind}:not_attempted"

    def load_tick_range(
        self,
        *,
        connector: Any | None,
        symbol: str,
        start_utc: datetime,
        end_utc: datetime,
        source_preference: str | None = None,
    ) -> pd.DataFrame:
        """Load tick data from MT5, cache, or CSV without fetching huge windows at once."""
        # Tick replay is optional in this phase, so we keep the interface ready
        # and bounded while the runner remains bar-based.
        source_preference = str(source_preference or "").lower().strip()
        if source_preference not in {"mt5", "cache", "csv"}:
            source_preference = "mt5"
        symbol_key = self._normalize_symbol(symbol)
        source_order = [source_preference, *[item for item in ("mt5", "cache", "csv") if item != source_preference]]
        for source_kind in source_order:
            try:
                if source_kind == "cache":
                    rows = self.database._query_dataframe(
                        """
                        SELECT time, bid, ask, last, volume, spread, flags, source_kind
                        FROM history_cache_ticks
                        WHERE symbol = ? AND time >= ? AND time <= ?
                        ORDER BY time ASC, id ASC;
                        """,
                        (symbol_key, start_utc.isoformat(), end_utc.isoformat()),
                    )
                    if rows:
                        return pd.DataFrame(rows)
                elif source_kind == "csv":
                    for root in self._csv_roots():
                        if not root.exists():
                            continue
                        for pattern in (f"{symbol_key}_TICKS*.csv", f"{symbol_key}-TICKS*.csv"):
                            for path in root.rglob(pattern):
                                try:
                                    frame = self._read_csv_frame(path)
                                    if frame.empty:
                                        continue
                                    return frame
                                except Exception:
                                    continue
                elif connector is not None:
                    fetch = getattr(connector, "copy_ticks_range", None)
                    if callable(fetch):
                        ticks = fetch(symbol_key, start_utc, end_utc)
                        if ticks is not None:
                            return pd.DataFrame(ticks)
            except Exception:
                continue
        return pd.DataFrame(columns=CANONICAL_TICK_COLUMNS)
