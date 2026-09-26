"""Campaign orchestration and result persistence for Indicator Lab."""

from __future__ import annotations

import hashlib
import itertools
import csv
import io
import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from services.database import DatabaseService
from services.indicator_lab.dataset_service import IndicatorDatasetService
from services.indicator_lab.engine import IndicatorEngine
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import ZoneRecord
from services.indicator_lab.parameter_registry import ENGINE_VERSION, LEBPRIME_INDICATOR_VERSION, LEBPRIME_PINE_PATH, VSS3_PINE_PATH, default_parameters, normalize_indicator, parameter_schema, validate_parameters
from services.indicator_lab.vss3_engine import VSS3Engine
from trading_bot.core.job_state import JobState
from trading_bot.core.job_types import JobType
from utils import utc_now


CAMPAIGN_STATES = {"DRAFT", "QUEUED", "RUNNING", "PAUSED", "COMPLETED", "PARTIAL", "CANCELED", "FAILED"}
TEST_STATES = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELED"}
MAX_PREVIEW_CONFIGS = 10000

ZONE_EXTRA_COLUMNS: dict[str, str] = {
    "symbol": "TEXT",
    "timeframe": "TEXT",
    "zone_type": "TEXT",
    "indicator_version": "TEXT",
    "flip_generation": "INTEGER",
    "source_open": "REAL",
    "source_high": "REAL",
    "source_low": "REAL",
    "source_close": "REAL",
    "source_body": "REAL",
    "source_range": "REAL",
    "candidate_bar_index": "INTEGER",
    "candidate_time": "TEXT",
    "candidate_open": "REAL",
    "candidate_high": "REAL",
    "candidate_low": "REAL",
    "candidate_close": "REAL",
    "candidate_body": "REAL",
    "candidate_range": "REAL",
    "atr_at_candidate": "REAL",
    "average_body_at_candidate": "REAL",
    "body_threshold": "REAL",
    "bull_count": "INTEGER",
    "bear_count": "INTEGER",
    "compact_base_start_offset": "INTEGER",
    "compact_base_end_offset": "INTEGER",
    "base_start_bar_index": "INTEGER",
    "base_end_bar_index": "INTEGER",
    "base_start_time": "TEXT",
    "base_end_time": "TEXT",
    "base_bar_count": "INTEGER",
    "refinement_target_offset": "INTEGER",
    "refined_bar_index": "INTEGER",
    "refined_time": "TEXT",
    "refined_open": "REAL",
    "refined_high": "REAL",
    "refined_low": "REAL",
    "refined_close": "REAL",
    "refined_range": "REAL",
    "refinement_method": "TEXT",
    "zone_mid": "REAL",
    "zone_width_price": "REAL",
    "zone_width_points": "REAL",
    "zone_width_atr": "REAL",
    "atr_at_source": "REAL",
    "average_body_at_source": "REAL",
    "momentum_body": "REAL",
    "momentum_body_ratio": "REAL",
    "momentum_count": "INTEGER",
    "momentum_span": "INTEGER",
    "compact_body_ratio": "REAL",
    "compact_range_atr_ratio": "REAL",
    "confirmed_bar_index": "INTEGER",
    "confirmed_time": "TEXT",
    "armed_bar_index": "INTEGER",
    "armed_time": "TEXT",
    "entry1_activated_bar_index": "INTEGER",
    "entry1_activated_time": "TEXT",
    "entry2_activated_bar_index": "INTEGER",
    "entry2_activated_time": "TEXT",
    "bars_source_to_candidate": "INTEGER",
    "bars_candidate_to_created": "INTEGER",
    "bars_source_to_created": "INTEGER",
    "bars_source_to_limit": "INTEGER",
    "bars_source_to_activation": "INTEGER",
    "zone_age_at_limit": "INTEGER",
    "zone_age_at_activation": "INTEGER",
    "zone_age_at_terminal": "INTEGER",
    "risk_distance": "REAL",
    "reward_distance": "REAL",
    "rr": "REAL",
    "buffer_used": "REAL",
    "structural_swing_bar_index": "INTEGER",
    "structural_swing_time": "TEXT",
    "structural_swing_price": "REAL",
    "structural_swing_kind": "TEXT",
    "tp1_price": "REAL",
    "tp2_price": "REAL",
    "tp1_hit_bar_index": "INTEGER",
    "tp1_hit_time": "TEXT",
    "tp1_final_bar_index": "INTEGER",
    "tp1_final_time": "TEXT",
    "tp2_hit_bar_index": "INTEGER",
    "tp2_hit_time": "TEXT",
    "departure_price": "REAL",
    "departure_close": "REAL",
    "departure_boundary": "REAL",
    "departure_distance_price": "REAL",
    "departure_distance": "REAL",
    "departure_distance_atr": "REAL",
    "first_retest_bar_index": "INTEGER",
    "first_retest_time": "TEXT",
    "retest_count": "INTEGER",
    "exit_price": "REAL",
    "result_pips": "REAL",
    "mfe_price": "REAL",
    "mae_price": "REAL",
    "mfe_points": "REAL",
    "mae_points": "REAL",
    "mfe_r": "REAL",
    "mae_r": "REAL",
    "holding_bars": "INTEGER",
    "holding_minutes": "REAL",
    "creation_reason": "TEXT",
    "rejection_reason": "TEXT",
    "activation_reason": "TEXT",
    "terminal_reason": "TEXT",
    "parameter_hash": "TEXT",
    "dataset_hash": "TEXT",
    "pine_reference_hash": "TEXT",
    "engine_version": "TEXT",
}

EVENT_EXTRA_COLUMNS: dict[str, str] = {
    "zone_top": "REAL",
    "zone_bottom": "REAL",
    "entry": "REAL",
    "sl": "REAL",
    "tp": "REAL",
}

TEST_EXTRA_COLUMNS: dict[str, str] = {
    "indicator_version": "TEXT",
}

ZONE_CSV_COLUMNS = [
    "campaign_id", "test_id", "dataset_id", "indicator", "symbol", "timeframe", "zone_id", "engine_zone_id",
    "side", "zone_type", "indicator_version", "is_flipped", "parent_zone_id", "flip_generation", "source_bar_index", "source_time",
    "source_open", "source_high", "source_low", "source_close", "source_body", "source_range",
    "candidate_bar_index", "candidate_time", "candidate_open", "candidate_high", "candidate_low",
    "candidate_close", "candidate_body", "candidate_range", "atr_at_candidate", "average_body_at_candidate",
    "body_threshold", "bull_count", "bear_count", "compact_base_start_offset", "compact_base_end_offset",
    "base_start_bar_index", "base_end_bar_index", "base_start_time", "base_end_time", "base_bar_count",
    "refinement_target_offset", "refined_bar_index", "refined_time", "refined_open", "refined_high",
    "refined_low", "refined_close", "refined_range", "refinement_method",
    "created_bar_index", "created_time", "confirmed_bar_index", "confirmed_time", "armed_bar_index", "armed_time",
    "limit_created_bar_index", "limit_created_time", "activated_bar_index", "activated_time",
    "entry1_activated_bar_index", "entry1_activated_time", "entry2_activated_bar_index", "entry2_activated_time", "terminal_bar_index",
    "terminal_time", "zone_top", "zone_bottom", "zone_mid", "zone_width_price", "zone_width_points",
    "zone_width_atr", "atr_at_source", "average_body_at_source", "momentum_body", "momentum_body_ratio",
    "momentum_count", "momentum_span", "compact_body_ratio", "compact_range_atr_ratio",
    "bars_source_to_candidate", "bars_candidate_to_created", "bars_source_to_created", "bars_source_to_limit",
    "bars_source_to_activation", "zone_age_at_limit", "zone_age_at_activation", "zone_age_at_terminal",
    "departure_price", "departure_close", "departure_boundary", "departure_distance_price",
    "departure_distance", "departure_distance_atr", "first_retest_bar_index", "first_retest_time",
    "retest_count", "entry1", "entry2", "entry_price", "sl_price", "structural_swing_bar_index",
    "structural_swing_time", "structural_swing_price", "structural_swing_kind",
    "tp1_price", "tp2_price", "tp1_hit_bar_index", "tp1_hit_time", "tp1_final_bar_index",
    "tp1_final_time", "tp2_hit_bar_index", "tp2_hit_time", "tp_price", "risk_distance",
    "reward_distance", "rr", "buffer_used",
    "terminal_state", "terminal_reason", "exit_price", "result_r", "result_points", "result_pips", "mfe_price",
    "mae_price", "mfe_points", "mae_points", "mfe_r", "mae_r", "holding_bars", "holding_minutes",
    "creation_reason", "rejection_reason", "activation_reason", "parameter_hash", "dataset_hash",
    "pine_reference_hash", "engine_version",
    "metadata_json",
]

EVENT_CSV_COLUMNS = [
    "campaign_id", "test_id", "zone_id", "engine_zone_id", "bar_index", "timestamp", "event_type", "price",
    "zone_top", "zone_bottom", "entry", "sl", "tp", "reason", "metadata_json",
]

CANDIDATE_CSV_COLUMNS = [
    "campaign_id", "test_id", "dataset_id", "indicator", "candidate_id", "side",
    "candidate_bar_index", "candidate_time", "source_bar_index", "source_time",
    "raw_zone_top", "raw_zone_bottom", "clamped_zone_top", "clamped_zone_bottom",
    "zone_width", "ATR", "min_width_required", "max_width_allowed",
    "compact_base_start", "compact_base_end", "compact_base_start_offset", "compact_base_end_offset",
    "refinement_method", "refined_bar_index", "refined_bar_time",
    "same_side_spacing_ok", "initial_overlap_ok", "width_ok",
    "confirmation_due_bar", "confirmation_due_time",
    "outcome", "outcome_reason", "zone_created", "limit_created",
    "limit_created_bar_index", "limit_created_time", "pending_bars_json",
]

RESULT_METRIC_COLUMNS = [
    "zones_created", "zones_rejected", "flipped_zones", "limit_created", "activated", "tp_hit", "sl_hit",
    "be_exit", "canceled", "invalidated", "closed_trades", "pending_limits", "active_trades", "win_rate",
    "net_r", "gross_r_won", "gross_r_lost", "average_r", "profit_factor", "max_drawdown",
    "limits_created", "entry1_activated", "entry2_activated", "tp1_hit", "tp2_hit", "tp1_final",
    "pending", "active",
]


class IndicatorLabService:
    """Build, run, and inspect Indicator Lab campaigns."""

    def __init__(self, base_dir: str | Path, database: DatabaseService, config: dict[str, Any] | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.database = database
        self.config = config or {}
        self.dataset_service = IndicatorDatasetService(base_dir, database, config)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS indicator_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                state TEXT NOT NULL DEFAULT 'DRAFT',
                name TEXT,
                dataset_id INTEGER NOT NULL,
                indicator TEXT NOT NULL,
                fixed_parameters_json TEXT,
                optimization_grid_json TEXT,
                generated_configurations INTEGER DEFAULT 0,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                best_test_id INTEGER,
                failure_reason TEXT,
                notes TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS indicator_campaign_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                dataset_id INTEGER NOT NULL,
                indicator TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                parameter_snapshot_json TEXT NOT NULL,
                parameter_hash TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                pine_reference_hash TEXT,
                dataset_hash TEXT,
                rank_score REAL,
                metrics_json TEXT,
                error TEXT,
                worker_id TEXT,
                UNIQUE(campaign_id, parameter_hash)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS indicator_zones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                dataset_id INTEGER NOT NULL,
                indicator TEXT NOT NULL,
                engine_zone_id TEXT NOT NULL,
                side TEXT NOT NULL,
                source_bar_index INTEGER,
                source_time TEXT,
                zone_top REAL,
                zone_bottom REAL,
                zone_width REAL,
                created_bar_index INTEGER,
                created_time TEXT,
                limit_created_bar_index INTEGER,
                limit_created_time TEXT,
                activated_bar_index INTEGER,
                activated_time TEXT,
                entry1 REAL,
                entry2 REAL,
                entry_price REAL,
                sl_price REAL,
                tp_price REAL,
                is_flipped INTEGER DEFAULT 0,
                parent_zone_id TEXT,
                terminal_state TEXT,
                terminal_bar_index INTEGER,
                terminal_time TEXT,
                result_r REAL,
                result_price REAL,
                result_points REAL,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS indicator_zone_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                engine_zone_id TEXT NOT NULL,
                test_id INTEGER NOT NULL,
                bar_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                price REAL,
                zone_top REAL,
                zone_bottom REAL,
                entry REAL,
                sl REAL,
                tp REAL,
                reason TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS indicator_candidate_diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                test_id INTEGER NOT NULL,
                dataset_id INTEGER NOT NULL,
                indicator TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                side TEXT,
                candidate_bar_index INTEGER,
                candidate_time TEXT,
                source_bar_index INTEGER,
                source_time TEXT,
                raw_zone_top REAL,
                raw_zone_bottom REAL,
                clamped_zone_top REAL,
                clamped_zone_bottom REAL,
                zone_width REAL,
                ATR REAL,
                min_width_required REAL,
                max_width_allowed REAL,
                compact_base_start INTEGER,
                compact_base_end INTEGER,
                compact_base_start_offset INTEGER,
                compact_base_end_offset INTEGER,
                refinement_method TEXT,
                refined_bar_index INTEGER,
                refined_bar_time TEXT,
                same_side_spacing_ok INTEGER,
                initial_overlap_ok INTEGER,
                width_ok INTEGER,
                confirmation_due_bar INTEGER,
                confirmation_due_time TEXT,
                outcome TEXT,
                outcome_reason TEXT,
                zone_created INTEGER,
                limit_created INTEGER,
                limit_created_bar_index INTEGER,
                limit_created_time TEXT,
                pending_bars_json TEXT
            );
            """,
        ]
        for statement in statements:
            self.database._execute(statement, ())
        self._ensure_columns("indicator_zones", ZONE_EXTRA_COLUMNS)
        self._ensure_columns("indicator_zone_events", EVENT_EXTRA_COLUMNS)
        self._ensure_columns("indicator_campaign_tests", TEST_EXTRA_COLUMNS)

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.database._query_dataframe(f"PRAGMA table_info({table});")}
        for name, column_type in columns.items():
            if name not in existing:
                self.database._execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type};", ())

    @staticmethod
    def _json_text(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=True, sort_keys=True, default=str)

    @staticmethod
    def _parse_json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            parsed = json.loads(value)
            return default if parsed is None else parsed
        except Exception:
            return default

    @staticmethod
    def _csv_text(rows: list[dict[str, Any]], columns: list[str]) -> str:
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            clean: dict[str, Any] = {}
            for column in columns:
                value = row.get(column)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
                clean[column] = "" if value is None else value
            writer.writerow(clean)
        return output.getvalue()

    @staticmethod
    def _flatten_metadata(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        for key in keys:
            if key not in row or row.get(key) in (None, ""):
                if key in metadata and not isinstance(metadata[key], (dict, list)):
                    row[key] = metadata[key]
        row["metadata_json"] = json.dumps(metadata or {}, ensure_ascii=True, separators=(",", ":"), default=str)
        return row

    def _engine_cls(self, indicator: str) -> type[IndicatorEngine]:
        return LebPrimeEngine if normalize_indicator(indicator) == "LEBPRIME" else VSS3Engine

    def parameter_schema(self, indicator: str) -> dict[str, Any]:
        return {"indicator": normalize_indicator(indicator), "parameters": parameter_schema(indicator)}

    def pine_hash(self, indicator: str) -> str:
        path = self.base_dir / (LEBPRIME_PINE_PATH if normalize_indicator(indicator) == "LEBPRIME" else VSS3_PINE_PATH)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""

    def preview_campaign(self, *, dataset_id: int, indicator: str, fixed_parameters: dict[str, Any] | None = None, optimization_grid: dict[str, Any] | None = None) -> dict[str, Any]:
        dataset = self.dataset_service.get_dataset(dataset_id)
        if not dataset or dataset.get("status") != "READY":
            raise ValueError("dataset must exist and be READY")
        indicator = normalize_indicator(indicator)
        fixed = validate_parameters(indicator, fixed_parameters or {})
        combos = self._generate_configurations(indicator, fixed, optimization_grid or {})
        return {"dataset_id": int(dataset_id), "indicator": indicator, "generated_configurations": len(combos), "requires_confirmation": len(combos) > MAX_PREVIEW_CONFIGS, "sample": combos[:5]}

    def create_campaign(self, *, dataset_id: int, indicator: str, fixed_parameters: dict[str, Any] | None = None, optimization_grid: dict[str, Any] | None = None, name: str | None = None, notes: str | None = None, confirm_large: bool = False, actor: str = "dashboard") -> dict[str, Any]:
        preview = self.preview_campaign(dataset_id=dataset_id, indicator=indicator, fixed_parameters=fixed_parameters, optimization_grid=optimization_grid)
        if preview["requires_confirmation"] and not confirm_large:
            raise ValueError("Generated configurations exceed 10000; explicit confirmation is required")
        indicator = preview["indicator"]
        dataset = self.dataset_service.get_dataset(dataset_id) or {}
        combos = self._generate_configurations(indicator, validate_parameters(indicator, fixed_parameters or {}), optimization_grid or {})
        now = utc_now().isoformat()
        with self.database._lock:
            with self.database._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO indicator_campaigns (
                        created_at, updated_at, state, name, dataset_id, indicator, fixed_parameters_json,
                        optimization_grid_json, generated_configurations, progress_total, notes, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (now, now, "QUEUED", name or f"{indicator} Campaign", int(dataset_id), indicator, self._json_text(fixed_parameters or {}), self._json_text(optimization_grid or {}), len(combos), len(combos), notes, self._json_text({"actor": actor})),
                )
                campaign_id = int(cursor.lastrowid)
                pine_hash = self.pine_hash(indicator)
                indicator_version = LEBPRIME_INDICATOR_VERSION if indicator == "LEBPRIME" else None
                for combo in combos:
                    snapshot = {"indicator": indicator, "dataset_id": int(dataset_id), "parameters": combo, "indicator_version": indicator_version}
                    parameter_hash = hashlib.sha256(self._json_text(snapshot).encode("utf-8")).hexdigest()
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO indicator_campaign_tests (
                            campaign_id, dataset_id, indicator, state, created_at, updated_at,
                            parameter_snapshot_json, parameter_hash, engine_version, pine_reference_hash, indicator_version,
                            dataset_hash, metrics_json
                        ) VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (campaign_id, int(dataset_id), indicator, now, now, self._json_text(snapshot), parameter_hash, ENGINE_VERSION, pine_hash, indicator_version, dataset.get("content_hash"), "{}"),
                    )
        job_id = self.database.create_job(
            {
                "job_type": JobType.INDICATOR_CAMPAIGN.value,
                "state": JobState.QUEUED.value,
                "payload_json": {"campaign_id": campaign_id, "actor": actor},
                "related_run_type": "indicator_campaign",
                "related_run_id": campaign_id,
                "progress_total": len(combos),
                "metadata_json": {"actor": actor, "indicator": indicator, "dataset_id": int(dataset_id)},
            }
        )
        return {"campaign": self.get_campaign(campaign_id), "job_id": job_id}

    def _generate_configurations(self, indicator: str, fixed: dict[str, Any], grid: dict[str, Any]) -> list[dict[str, Any]]:
        schema = {item["name"]: item for item in parameter_schema(indicator)}
        defaults = default_parameters(indicator)
        base = {**defaults, **fixed}
        axes: list[tuple[str, list[Any]]] = []
        for name, spec in (grid or {}).items():
            if name not in schema:
                raise ValueError(f"Unknown optimization parameter: {name}")
            if not isinstance(spec, dict) or not spec.get("enabled", False):
                continue
            kind = schema[name]["type"]
            values = spec.get("values")
            if values is None and kind in {"int", "float"}:
                start = spec.get("start")
                end = spec.get("end")
                step = spec.get("step")
                if start is None or end is None or step in (None, 0, "0"):
                    raise ValueError(f"{name} optimization requires start/end/step")
                values = self._range_values(float(start), float(end), float(step), kind == "int")
            if values is None:
                raise ValueError(f"{name} optimization requires values")
            axes.append((name, list(values)))
        configs: list[dict[str, Any]] = []
        names = [name for name, _values in axes]
        for values in itertools.product(*[values for _name, values in axes]) if axes else [()]:
            candidate = dict(base)
            for name, value in zip(names, values):
                candidate[name] = value
            configs.append(validate_parameters(indicator, candidate))
        return configs

    @staticmethod
    def _range_values(start: float, end: float, step: float, as_int: bool) -> list[Any]:
        if step <= 0 or end < start:
            raise ValueError("optimization range must have end >= start and positive step")
        values: list[Any] = []
        current = start
        guard = 0
        while current <= end + 1e-9:
            values.append(int(round(current)) if as_int else round(current, 10))
            current += step
            guard += 1
            if guard > MAX_PREVIEW_CONFIGS + 1:
                break
        return values

    def list_campaigns(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT * FROM indicator_campaigns ORDER BY id DESC LIMIT ?;", (int(limit),))
        return [self._parse_campaign(row) for row in rows]

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        rows = self.database._query_dataframe("SELECT * FROM indicator_campaigns WHERE id = ? LIMIT 1;", (int(campaign_id),))
        if not rows:
            return None
        campaign = self._parse_campaign(rows[0])
        campaign["tests"] = self.database._query_dataframe("SELECT id, state, parameter_hash, metrics_json, error FROM indicator_campaign_tests WHERE campaign_id = ? ORDER BY id ASC LIMIT 500;", (int(campaign_id),))
        for test in campaign["tests"]:
            test["metrics_json"] = self._parse_json(test.get("metrics_json"), {})
        return campaign

    def _parse_campaign(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for key in ("fixed_parameters_json", "optimization_grid_json", "metadata_json"):
            item[key] = self._parse_json(item.get(key), {})
        return item

    def set_campaign_state(self, campaign_id: int, state: str) -> dict[str, Any]:
        state = str(state).upper()
        if state not in CAMPAIGN_STATES:
            raise ValueError("unsupported campaign state")
        updates = {"state": state, "updated_at": utc_now().isoformat()}
        if state in {"CANCELED", "COMPLETED", "FAILED", "PARTIAL"}:
            updates["finished_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.database._execute(f"UPDATE indicator_campaigns SET {assignments} WHERE id = ?;", (*updates.values(), int(campaign_id)))
        if state == "CANCELED":
            self.database._execute("UPDATE indicator_campaign_tests SET state = 'CANCELED', updated_at = ? WHERE campaign_id = ? AND state = 'PENDING';", (utc_now().isoformat(), int(campaign_id)))
        return self.get_campaign(campaign_id) or {}

    def execute_campaign(self, campaign_id: int, *, worker_id: str = "", progress_callback: Any | None = None, should_interrupt: Any | None = None) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")
        if campaign["state"] == "CANCELED":
            raise InterruptedError("campaign canceled")
        self.database._execute("UPDATE indicator_campaigns SET state = 'RUNNING', started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?;", (utc_now().isoformat(), utc_now().isoformat(), int(campaign_id)))
        frame_cache = None
        while True:
            if callable(should_interrupt) and should_interrupt():
                self.set_campaign_state(campaign_id, "PAUSED")
                raise InterruptedError("indicator campaign interrupted")
            current = self.get_campaign(campaign_id) or {}
            if current.get("state") == "PAUSED":
                return current
            test = self._claim_next_test(campaign_id, worker_id)
            if not test:
                break
            if frame_cache is None:
                frame_cache = self.dataset_service.load_frame(int(test["dataset_id"]))
            try:
                self._run_test(test, frame_cache)
            except Exception as exc:
                self.database._execute("UPDATE indicator_campaign_tests SET state = 'FAILED', error = ?, finished_at = ?, updated_at = ? WHERE id = ?;", (str(exc), utc_now().isoformat(), utc_now().isoformat(), int(test["id"])))
            self._refresh_campaign_progress(campaign_id)
            progress = self.get_campaign(campaign_id) or {}
            if callable(progress_callback):
                progress_callback(campaign_id, int(progress.get("progress_current", 0) or 0), int(progress.get("progress_total", 0) or 0))
        final = self._refresh_campaign_progress(campaign_id, final=True)
        return final

    def _claim_next_test(self, campaign_id: int, worker_id: str) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self.database._lock:
            with self.database._connect() as connection:
                row = connection.execute("SELECT id FROM indicator_campaign_tests WHERE campaign_id = ? AND state = 'PENDING' ORDER BY id ASC LIMIT 1;", (int(campaign_id),)).fetchone()
                if row is None:
                    return None
                test_id = int(row["id"])
                updated = connection.execute("UPDATE indicator_campaign_tests SET state = 'RUNNING', started_at = COALESCE(started_at, ?), updated_at = ?, worker_id = ? WHERE id = ? AND state = 'PENDING';", (now, now, str(worker_id), test_id))
                if int(updated.rowcount or 0) <= 0:
                    return None
        rows = self.database._query_dataframe("SELECT * FROM indicator_campaign_tests WHERE id = ?;", (test_id,))
        return rows[0] if rows else None

    def _run_test(self, test: dict[str, Any], frame: Any) -> None:
        snapshot = self._parse_json(test.get("parameter_snapshot_json"), {})
        params = validate_parameters(str(test["indicator"]), snapshot.get("parameters") or {})
        dataset = self.dataset_service.get_dataset(int(test["dataset_id"])) or {}
        engine = self._engine_cls(str(test["indicator"]))(params, symbol=dataset.get("symbol") or "XAUUSD", timeframe=dataset.get("timeframe") or "M1")
        result = engine.run(frame)
        self._persist_result(int(test["campaign_id"]), int(test["id"]), int(test["dataset_id"]), str(test["indicator"]), result, test)
        metrics = result.get("metrics") or {}
        score = float(metrics.get("net_r", 0.0) or 0.0)
        self.database._execute(
            "UPDATE indicator_campaign_tests SET state = 'COMPLETED', metrics_json = ?, rank_score = ?, finished_at = ?, updated_at = ? WHERE id = ?;",
            (self._json_text(metrics), score, utc_now().isoformat(), utc_now().isoformat(), int(test["id"])),
        )

    def _persist_result(self, campaign_id: int, test_id: int, dataset_id: int, indicator: str, result: dict[str, Any], test: dict[str, Any] | None = None) -> None:
        zones: list[ZoneRecord] = result.get("zones") or []
        events = result.get("events") or []
        candidates: list[dict[str, Any]] = result.get("candidate_diagnostics") or []
        test = test or self.get_test(test_id) or {}
        zone_field_names = [item.name for item in fields(ZoneRecord) if item.name != "metadata"]
        zone_columns = [
            "campaign_id",
            "test_id",
            "dataset_id",
            "indicator",
            *zone_field_names,
            "zone_width",
            "metadata_json",
        ]
        event_columns = [
            "zone_id",
            "engine_zone_id",
            "test_id",
            "bar_index",
            "timestamp",
            "event_type",
            "price",
            "zone_top",
            "zone_bottom",
            "entry",
            "sl",
            "tp",
            "reason",
            "metadata_json",
        ]
        self.database._execute("DELETE FROM indicator_zone_events WHERE test_id = ?;", (test_id,))
        self.database._execute("DELETE FROM indicator_zones WHERE test_id = ?;", (test_id,))
        self.database._execute("DELETE FROM indicator_candidate_diagnostics WHERE test_id = ?;", (test_id,))
        zone_db_ids: dict[str, int] = {}
        with self.database._lock:
            with self.database._connect() as connection:
                for zone in zones:
                    zone.symbol = zone.symbol or result.get("symbol")
                    zone.timeframe = zone.timeframe or result.get("timeframe")
                    zone.indicator_version = zone.indicator_version or test.get("indicator_version")
                    zone.parameter_hash = zone.parameter_hash or test.get("parameter_hash")
                    zone.dataset_hash = zone.dataset_hash or test.get("dataset_hash")
                    zone.pine_reference_hash = zone.pine_reference_hash or test.get("pine_reference_hash")
                    zone.engine_version = zone.engine_version or test.get("engine_version")
                    zone.zone_mid = zone.zone_mid if zone.zone_mid is not None else (float(zone.zone_top) + float(zone.zone_bottom)) / 2.0
                    zone.zone_width_price = zone.zone_width_price if zone.zone_width_price is not None else zone.zone_width
                    zone.zone_width_points = zone.zone_width_points if zone.zone_width_points is not None else zone.zone_width
                    if zone.limit_created_bar_index is not None:
                        zone.bars_source_to_limit = zone.bars_source_to_limit if zone.bars_source_to_limit is not None else int(zone.limit_created_bar_index) - int(zone.source_bar_index)
                        zone.zone_age_at_limit = zone.zone_age_at_limit if zone.zone_age_at_limit is not None else int(zone.limit_created_bar_index) - int(zone.created_bar_index)
                    if zone.activated_bar_index is not None:
                        zone.bars_source_to_activation = zone.bars_source_to_activation if zone.bars_source_to_activation is not None else int(zone.activated_bar_index) - int(zone.source_bar_index)
                        zone.zone_age_at_activation = zone.zone_age_at_activation if zone.zone_age_at_activation is not None else int(zone.activated_bar_index) - int(zone.created_bar_index)
                    if zone.candidate_bar_index is not None:
                        zone.bars_source_to_candidate = zone.bars_source_to_candidate if zone.bars_source_to_candidate is not None else int(zone.candidate_bar_index) - int(zone.source_bar_index)
                        zone.bars_candidate_to_created = zone.bars_candidate_to_created if zone.bars_candidate_to_created is not None else int(zone.created_bar_index) - int(zone.candidate_bar_index)
                    zone.bars_source_to_created = zone.bars_source_to_created if zone.bars_source_to_created is not None else int(zone.created_bar_index) - int(zone.source_bar_index)
                    if zone.terminal_bar_index is not None:
                        zone.zone_age_at_terminal = zone.zone_age_at_terminal if zone.zone_age_at_terminal is not None else int(zone.terminal_bar_index) - int(zone.created_bar_index)
                    zone.exit_price = zone.exit_price if zone.exit_price is not None else zone.result_price
                    if zone.entry_price is not None and zone.sl_price is not None:
                        zone.risk_distance = zone.risk_distance if zone.risk_distance is not None else abs(float(zone.entry_price) - float(zone.sl_price))
                    if zone.entry_price is not None and zone.tp_price is not None:
                        zone.reward_distance = zone.reward_distance if zone.reward_distance is not None else abs(float(zone.tp_price) - float(zone.entry_price))
                    if zone.risk_distance and zone.reward_distance is not None:
                        zone.rr = zone.rr if zone.rr is not None else float(zone.reward_distance) / float(zone.risk_distance)
                    if zone.terminal_bar_index is not None and zone.activated_bar_index is not None:
                        zone.holding_bars = zone.holding_bars if zone.holding_bars is not None else int(zone.terminal_bar_index) - int(zone.activated_bar_index)
                    if zone.holding_minutes is None and zone.activated_time and zone.terminal_time:
                        try:
                            activated_at = datetime.fromisoformat(str(zone.activated_time).replace("Z", "+00:00"))
                            terminal_at = datetime.fromisoformat(str(zone.terminal_time).replace("Z", "+00:00"))
                            zone.holding_minutes = (terminal_at - activated_at).total_seconds() / 60.0
                        except ValueError:
                            pass
                    values = [campaign_id, test_id, dataset_id, indicator]
                    values.extend(getattr(zone, name, None) for name in zone_field_names)
                    values.extend([zone.zone_width, self._json_text(zone.metadata)])
                    cursor = connection.execute(
                        f"INSERT INTO indicator_zones ({', '.join(zone_columns)}) VALUES ({', '.join(['?'] * len(zone_columns))});",
                        tuple(values),
                    )
                    zone_db_ids[zone.engine_zone_id] = int(cursor.lastrowid)
                for event in events:
                    values = [
                        zone_db_ids.get(event.zone_engine_id),
                        event.zone_engine_id,
                        test_id,
                        event.bar_index,
                        event.timestamp,
                        event.event_type,
                        event.price,
                        event.zone_top,
                        event.zone_bottom,
                        event.entry,
                        event.sl,
                        event.tp,
                        event.reason,
                        self._json_text(event.metadata),
                    ]
                    connection.execute(
                        f"INSERT INTO indicator_zone_events ({', '.join(event_columns)}) VALUES ({', '.join(['?'] * len(event_columns))});",
                        tuple(values),
                    )
                if candidates:
                    candidate_columns = [column for column in CANDIDATE_CSV_COLUMNS if column != "pending_bars_json"]
                    candidate_columns.append("pending_bars_json")
                    for candidate in candidates:
                        item = dict(candidate)
                        item.update({"campaign_id": campaign_id, "test_id": test_id, "dataset_id": dataset_id, "indicator": indicator})
                        item["pending_bars_json"] = json.dumps(item.get("pending_bars_json") or [], ensure_ascii=True, separators=(",", ":"), default=str)
                        values = [item.get(column) for column in candidate_columns]
                        connection.execute(
                            f"INSERT INTO indicator_candidate_diagnostics ({', '.join(candidate_columns)}) VALUES ({', '.join(['?'] * len(candidate_columns))});",
                            tuple(values),
                        )

    def _refresh_campaign_progress(self, campaign_id: int, final: bool = False) -> dict[str, Any]:
        rows = self.database._query_dataframe("SELECT state, COUNT(1) AS count FROM indicator_campaign_tests WHERE campaign_id = ? GROUP BY state;", (int(campaign_id),))
        counts = {row["state"]: int(row["count"]) for row in rows}
        current = counts.get("COMPLETED", 0) + counts.get("FAILED", 0) + counts.get("CANCELED", 0)
        total = sum(counts.values())
        state = "RUNNING"
        if final:
            state = "COMPLETED" if counts.get("FAILED", 0) == 0 and counts.get("CANCELED", 0) == 0 else "PARTIAL"
        best = self.database._query_dataframe("SELECT id FROM indicator_campaign_tests WHERE campaign_id = ? AND state = 'COMPLETED' ORDER BY rank_score DESC, id ASC LIMIT 1;", (int(campaign_id),))
        updates = {"state": state, "progress_current": current, "progress_total": total, "updated_at": utc_now().isoformat(), "best_test_id": best[0]["id"] if best else None}
        if final:
            updates["finished_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.database._execute(f"UPDATE indicator_campaigns SET {assignments} WHERE id = ?;", (*updates.values(), int(campaign_id)))
        return self.get_campaign(campaign_id) or {}

    def campaign_results(self, campaign_id: int, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT * FROM indicator_campaign_tests WHERE campaign_id = ? ORDER BY rank_score DESC, id ASC LIMIT ?;", (int(campaign_id), int(limit)))
        for row in rows:
            row["parameter_snapshot_json"] = self._parse_json(row.get("parameter_snapshot_json"), {})
            row["metrics_json"] = self._parse_json(row.get("metrics_json"), {})
        return rows

    def get_test(self, test_id: int) -> dict[str, Any] | None:
        rows = self.database._query_dataframe("SELECT * FROM indicator_campaign_tests WHERE id = ? LIMIT 1;", (int(test_id),))
        if not rows:
            return None
        row = rows[0]
        row["parameter_snapshot_json"] = self._parse_json(row.get("parameter_snapshot_json"), {})
        row["metrics_json"] = self._parse_json(row.get("metrics_json"), {})
        return row

    def test_zones(self, test_id: int) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT * FROM indicator_zones WHERE test_id = ? ORDER BY source_bar_index ASC, id ASC;", (int(test_id),))
        for row in rows:
            row["metadata_json"] = self._parse_json(row.get("metadata_json"), {})
        return rows

    def test_events(self, test_id: int) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT e.*, z.campaign_id FROM indicator_zone_events e LEFT JOIN indicator_zones z ON z.id = e.zone_id WHERE e.test_id = ? ORDER BY e.timestamp ASC, e.bar_index ASC, e.id ASC;", (int(test_id),))
        for row in rows:
            row["metadata_json"] = self._parse_json(row.get("metadata_json"), {})
        return rows

    def test_candidates(self, test_id: int) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe("SELECT * FROM indicator_candidate_diagnostics WHERE test_id = ? ORDER BY candidate_bar_index ASC, id ASC;", (int(test_id),))
        for row in rows:
            row["pending_bars_json"] = self._parse_json(row.get("pending_bars_json"), [])
        return rows

    def get_zone(self, zone_id: int) -> dict[str, Any] | None:
        rows = self.database._query_dataframe("SELECT * FROM indicator_zones WHERE id = ? LIMIT 1;", (int(zone_id),))
        if not rows:
            return None
        row = rows[0]
        row["metadata_json"] = self._parse_json(row.get("metadata_json"), {})
        test = self.get_test(int(row["test_id"]))
        row["test"] = test
        row["parameter_snapshot_json"] = test.get("parameter_snapshot_json") if test else {}
        return row

    def zone_events(self, zone_id: int) -> list[dict[str, Any]]:
        rows = self.database._query_dataframe(
            "SELECT e.*, z.campaign_id FROM indicator_zone_events e LEFT JOIN indicator_zones z ON z.id = e.zone_id WHERE e.zone_id = ? ORDER BY e.timestamp ASC, e.bar_index ASC, e.id ASC;",
            (int(zone_id),),
        )
        for row in rows:
            row["metadata_json"] = self._parse_json(row.get("metadata_json"), {})
        return rows

    def zones_csv(self, test_id: int) -> str:
        rows = []
        metadata_keys = [
            "momentum_direction", "same_direction_count", "body_mult_threshold", "body_mult_actual",
            "momentum_candles", "compact_base_candles", "base_detection_mode", "compact_count",
            "compact_search_bars_used", "immediate_base_bars_used", "compact_base_min_bars_required",
            "zone_confirm_delay_used", "min_same_side_gap_used", "min_bars_after_source_used",
            "max_zone_width_atr_used", "min_zone_width_atr_used", "allow_overlap_used", "overlap_tolerance_used",
            "overlap_detected", "overlap_decision", "use_refinement", "zone_refinement_method",
            "original_zone_top", "original_zone_bottom", "refined_zone_top", "refined_zone_bottom",
            "invalidation_method", "retest_cooldown_used", "same_bar_priority_used", "tp_rr_used",
            "raw_zone_top", "raw_zone_bottom", "combined_zone", "combined_from_zone_ids",
            "min_distance_between_zones_used", "distance_from_previous_same_side_zone", "max_zone_size_atr_used",
            "momentum_span_used", "flip_created", "flip_parent_zone_id", "flip_distance_percent",
            "min_flipped_order_distance_percent_used", "swing_high", "swing_low", "swing_lookback_used",
            "close_stop_level", "close_stop_triggered", "be_trigger_price", "be_armed_bar", "be_moved",
            "spread_buffer_used",
        ]
        columns = [*ZONE_CSV_COLUMNS[:-1], *metadata_keys, "metadata_json"]
        for row in self.test_zones(test_id):
            item = dict(row)
            item["zone_id"] = item.get("id")
            rows.append(self._flatten_metadata(item, metadata_keys))
        return self._csv_text(rows, columns)

    def events_csv(self, test_id: int) -> str:
        rows = []
        for row in self.test_events(test_id):
            item = dict(row)
            item["zone_id"] = item.get("zone_id")
            item["metadata_json"] = json.dumps(item.get("metadata_json") or {}, ensure_ascii=True, separators=(",", ":"), default=str)
            rows.append(item)
        return self._csv_text(rows, EVENT_CSV_COLUMNS)

    def candidates_csv(self, test_id: int) -> str:
        rows = []
        for row in self.test_candidates(test_id):
            item = dict(row)
            item["pending_bars_json"] = json.dumps(item.get("pending_bars_json") or [], ensure_ascii=True, separators=(",", ":"), default=str)
            rows.append(item)
        return self._csv_text(rows, CANDIDATE_CSV_COLUMNS)

    def campaign_results_csv(self, campaign_id: int) -> str:
        results = self.campaign_results(campaign_id, limit=100000)
        parameter_names = sorted({name for row in results for name in (row.get("parameter_snapshot_json") or {}).get("parameters", {}).keys()})
        columns = [
            "rank", "test_id", "campaign_id", "dataset_id", "indicator", "state", "parameter_hash",
            *parameter_names,
            *RESULT_METRIC_COLUMNS,
            "created_at", "started_at", "finished_at", "dataset_hash", "pine_reference_hash", "engine_version", "indicator_version",
        ]
        rows: list[dict[str, Any]] = []
        for rank, row in enumerate(results, start=1):
            params = (row.get("parameter_snapshot_json") or {}).get("parameters", {})
            metrics = row.get("metrics_json") or {}
            item = {
                "rank": rank,
                "test_id": row.get("id"),
                "campaign_id": row.get("campaign_id"),
                "dataset_id": row.get("dataset_id"),
                "indicator": row.get("indicator"),
                "state": row.get("state"),
                "parameter_hash": row.get("parameter_hash"),
                "created_at": row.get("created_at"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "dataset_hash": row.get("dataset_hash"),
                "pine_reference_hash": row.get("pine_reference_hash"),
                "engine_version": row.get("engine_version"),
                "indicator_version": row.get("indicator_version"),
            }
            item.update({name: params.get(name) for name in parameter_names})
            item.update({name: metrics.get(name) for name in RESULT_METRIC_COLUMNS})
            rows.append(item)
        return self._csv_text(rows, columns)

    def test_replay(self, test_id: int) -> dict[str, Any]:
        test = self.get_test(test_id)
        if not test:
            raise ValueError(f"Test {test_id} not found")
        frame = self.dataset_service.load_frame(int(test["dataset_id"]))
        candles = frame.reset_index(drop=True).reset_index().rename(columns={"index": "bar_index"}).to_dict("records")
        return {"test": test, "candles": candles, "zones": self.test_zones(test_id), "events": self.test_events(test_id)}
