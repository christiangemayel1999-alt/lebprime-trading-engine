from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path
import shutil
import csv
import io

import pandas as pd

from services.database import DatabaseService
from services.indicator_lab import IndicatorDatasetService, IndicatorLabService, LebPrimeEngine, VSS3Engine
from services.indicator_lab.models import Candle, ZoneRecord
from services.indicator_lab.parameter_registry import default_parameters, parameter_schema, validate_parameters
from services.worker import WorkerService


def make_bars(count: int = 90) -> pd.DataFrame:
    rows = []
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
    price = 2400.0
    for i in range(count):
        if 20 <= i <= 25 or 50 <= i <= 55:
            open_price = price
            close = price + 1.8
        elif 35 <= i <= 40 or 65 <= i <= 70:
            open_price = price
            close = price - 1.8
        else:
            open_price = price
            close = price + (0.12 if i % 2 == 0 else -0.12)
        high = max(open_price, close) + 0.15
        low = min(open_price, close) - 0.15
        rows.append(
            {
                "time": ts + timedelta(minutes=i),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": 100 + i,
                "spread": 2,
                "real_volume": 100 + i,
            }
        )
        price = close
    return pd.DataFrame(rows)


class FakeConnector:
    symbol = "XAUUSD"

    def fetch_rates_range(self, _timeframe, _start, _end, min_rows=1):
        return make_bars()


def test_parameter_schema_uses_pine_defaults():
    leb = {item["name"]: item for item in parameter_schema("LEBPRIME")}
    vss = {item["name"]: item for item in parameter_schema("VSS3")}
    assert leb["body_mult"]["default"] == 0.5
    assert leb["zone_refinement"]["default"] == "Smallest Range Candle"
    assert "rr" not in leb
    assert "same_bar_priority" not in leb
    assert leb["gold_buffer"]["default"] == 1.0
    assert leb["nas_buffer"]["default"] == 15.0
    assert leb["fallback_buffer"]["default"] == 1.0
    assert vss["combine_sds"]["default"] is True
    assert vss["tp_rr"]["default"] == 1.0
    assert vss["min_flipped_order_distance_percent"]["default"] == 0.10
    assert validate_parameters("LEBPRIME", {"momentum_count": 3})["momentum_count"] == 3


def test_engines_are_deterministic_on_same_input():
    frame = IndicatorDatasetService.normalize_frame(make_bars())
    leb_params = {**default_parameters("LEBPRIME"), "zone_confirm_delay": 0, "min_zone_width_atr": 0.01, "max_zone_width_atr": 5.0}
    one = LebPrimeEngine(leb_params).run(frame)
    two = LebPrimeEngine(leb_params).run(frame)
    assert [asdict(z) for z in one["zones"]] == [asdict(z) for z in two["zones"]]
    assert [asdict(e) for e in one["events"]] == [asdict(e) for e in two["events"]]
    vss_params = {**default_parameters("VSS3"), "min_zone_size": 1, "min_distance_percent": 0.01}
    assert [asdict(z) for z in VSS3Engine(vss_params).run(frame)["zones"]] == [asdict(z) for z in VSS3Engine(vss_params).run(frame)["zones"]]


def test_lebprime_bounds_preserve_pine_source_anchor_separately_from_refinement_target():
    params = {
        **default_parameters("LEBPRIME"),
        "momentum_count": 2,
        "compact_search_bars": 3,
        "compact_base_min_bars": 1,
        "zone_refinement": "Smallest Range Candle",
        "use_refinement": True,
    }
    engine = LebPrimeEngine(params)
    engine._bars = [
        Candle(i, f"2026-08-01T00:{i:02d}:00+00:00", 100 + i, 101 + i, 99 + i, 100.05 + i)
        for i in range(8)
    ]
    engine._bars[3] = Candle(3, "2026-08-01T00:03:00+00:00", 103.0, 106.0, 102.0, 103.05)
    engine._bars[4] = Candle(4, "2026-08-01T00:04:00+00:00", 104.0, 104.4, 103.9, 104.05)
    engine._bars[5] = Candle(5, "2026-08-01T00:05:00+00:00", 105.0, 106.0, 104.7, 105.05)

    top, bottom, source_offset, target_offset, compact = engine._bounds(True, avg_body=1.0, atr=10.0)

    assert source_offset == 4
    assert target_offset == 3
    assert compact["mode"] == "COMPACT"
    assert top == engine._bars[4].high
    assert bottom == engine._bars[4].low


def test_lebprime_delayed_confirmation_persists_candidate_bar_and_refined_target():
    frame = IndicatorDatasetService.normalize_frame(make_bars())
    params = {
        **default_parameters("LEBPRIME"),
        "zone_confirm_delay": 3,
        "min_zone_width_atr": 0.01,
        "max_zone_width_atr": 5.0,
    }
    result = LebPrimeEngine(params).run(frame)

    assert result["zones"]
    first = result["zones"][0]
    assert first.candidate_bar_index == first.created_bar_index - 3
    assert first.bars_candidate_to_created == 3
    assert first.source_bar_index == first.base_end_bar_index
    assert first.refined_bar_index != first.source_bar_index
    assert first.refinement_target_offset is not None
    assert first.candidate_open is not None
    assert first.momentum_body_ratio is not None
    assert first.metadata["base_detection_mode"] in {"COMPACT", "FALLBACK_IMMEDIATE"}
    assert first.metadata["momentum_candles"]
    assert first.metadata["compact_base_candles"]


def test_lebprime_mfe_mae_prices_are_market_levels_and_metrics_are_unchanged():
    params = default_parameters("LEBPRIME")
    engine = LebPrimeEngine(params)
    demand = ZoneRecord(
        engine_zone_id="demand",
        side="DEMAND",
        source_bar_index=0,
        source_time="2026-08-01T00:00:00+00:00",
        zone_top=100.0,
        zone_bottom=99.0,
        created_bar_index=0,
        created_time="2026-08-01T00:00:00+00:00",
        limit_created_bar_index=0,
        limit_created_time="2026-08-01T00:00:00+00:00",
        activated_bar_index=1,
        activated_time="2026-08-01T00:01:00+00:00",
        entry2=99.0,
        entry2_activated_bar_index=1,
        entry2_activated_time="2026-08-01T00:01:00+00:00",
        entry_price=100.0,
        sl_price=90.0,
        tp_price=150.0,
    )
    supply = ZoneRecord(
        engine_zone_id="supply",
        side="SUPPLY",
        source_bar_index=0,
        source_time="2026-08-01T00:00:00+00:00",
        zone_top=101.0,
        zone_bottom=100.0,
        created_bar_index=0,
        created_time="2026-08-01T00:00:00+00:00",
        limit_created_bar_index=0,
        limit_created_time="2026-08-01T00:00:00+00:00",
        activated_bar_index=1,
        activated_time="2026-08-01T00:01:00+00:00",
        entry2=101.0,
        entry2_activated_bar_index=1,
        entry2_activated_time="2026-08-01T00:01:00+00:00",
        entry_price=100.0,
        sl_price=110.0,
        tp_price=50.0,
    )
    engine._zones = [demand, supply]
    before_metrics = engine.metrics()

    bars = [
        Candle(1, "2026-08-01T00:01:00+00:00", 100.0, 101.0, 99.0, 100.0),
        Candle(2, "2026-08-01T00:02:00+00:00", 100.0, 105.0, 97.0, 101.0),
        Candle(3, "2026-08-01T00:03:00+00:00", 101.0, 103.0, 95.0, 100.0),
        Candle(4, "2026-08-01T00:04:00+00:00", 100.0, 106.0, 98.0, 99.0),
    ]
    for bar in bars:
        engine._bars.append(bar)
        engine._manage_zones(bar)

    assert demand.mfe_price == 106.0
    assert demand.mae_price == 95.0
    assert demand.mfe_points == 6.0
    assert demand.mae_points == 5.0
    assert demand.mfe_r == 0.6
    assert demand.mae_r == 0.5

    assert supply.mfe_price == 95.0
    assert supply.mae_price == 106.0
    assert supply.mfe_points == 5.0
    assert supply.mae_points == 6.0
    assert supply.mfe_r == 0.5
    assert supply.mae_r == 0.6

    assert demand.mfe_price != demand.mfe_points
    assert demand.mae_price != demand.mae_points
    assert supply.mfe_price != supply.mfe_points
    assert supply.mae_price != supply.mae_points
    assert engine.metrics() == before_metrics


def test_lebprime_v34_width_clamp_clean_departure_and_hidden_candidate():
    params = {**default_parameters("LEBPRIME"), "max_zone_width_atr": 1.0, "min_zone_width_atr": 0.1}
    engine = LebPrimeEngine(params)
    top, bottom = engine._clamp_zone_bounds(110.0, 100.0, atr=4.0)
    assert (top, bottom) == (107.0, 103.0)
    assert engine._valid_width(top - bottom, atr=4.0)
    assert LebPrimeEngine._departure_ready(Candle(10, "2026-08-01T00:10:00+00:00", 108.0, 109.0, 107.1, 108.0), True, top, bottom)
    assert not LebPrimeEngine._departure_ready(Candle(11, "2026-08-01T00:11:00+00:00", 107.0, 107.5, 106.8, 107.0), True, top, bottom)

    before_events = len(engine.events())
    assert engine._queue_or_create(True, top, bottom, 0, "hidden", Candle(12, "2026-08-01T00:12:00+00:00", 108.0, 109.0, 107.2, 108.5), 4.0, 1.0)
    assert len(engine.events()) == before_events


def test_lebprime_v34_entry2_tp1_tp2_and_sl_body_close_rules():
    params = default_parameters("LEBPRIME")
    engine = LebPrimeEngine(params)
    demand = ZoneRecord(
        engine_zone_id="demand-v34",
        side="DEMAND",
        source_bar_index=-10,
        source_time="2026-08-01T00:00:00+00:00",
        zone_top=100.0,
        zone_bottom=99.0,
        created_bar_index=0,
        created_time="2026-08-01T00:00:00+00:00",
        limit_created_bar_index=0,
        limit_created_time="2026-08-01T00:00:00+00:00",
        entry1=100.0,
        entry2=99.0,
        entry_price=100.0,
        sl_price=98.0,
        tp1_price=102.0,
        tp2_price=104.0,
        tp_price=104.0,
    )
    engine._zones = [demand]
    activation = Candle(1, "2026-08-01T00:01:00+00:00", 101.0, 101.5, 98.9, 100.5)
    engine._bars = [Candle(0, "2026-08-01T00:00:00+00:00", 102.0, 102.5, 101.0, 101.5), activation]
    engine._manage_zones(activation)
    assert demand.entry1_activated_bar_index == 1
    assert demand.entry2_activated_bar_index == 1
    assert demand.terminal_state is None

    tp1_bar = Candle(2, "2026-08-01T00:02:00+00:00", 100.5, 102.2, 99.5, 101.8)
    engine._bars.append(tp1_bar)
    engine._manage_zones(tp1_bar)
    assert demand.tp1_hit_bar_index == 2
    assert demand.terminal_state is None

    final_bar = Candle(3, "2026-08-01T00:03:00+00:00", 101.8, 102.0, 99.9, 100.1)
    engine._bars.append(final_bar)
    engine._manage_zones(final_bar)
    assert demand.terminal_state == "TP1_FINAL"
    assert demand.result_r == 1.0

    supply = ZoneRecord(
        engine_zone_id="supply-v34",
        side="SUPPLY",
        source_bar_index=0,
        source_time="2026-08-01T00:00:00+00:00",
        zone_top=101.0,
        zone_bottom=100.0,
        created_bar_index=0,
        created_time="2026-08-01T00:00:00+00:00",
        limit_created_bar_index=0,
        limit_created_time="2026-08-01T00:00:00+00:00",
        activated_bar_index=1,
        activated_time="2026-08-01T00:01:00+00:00",
        entry1=100.0,
        entry2=101.0,
        entry_price=100.0,
        sl_price=102.0,
        tp1_price=98.0,
        tp2_price=96.0,
        tp_price=96.0,
    )
    engine._zones = [supply]
    weak_sl_bar = Candle(2, "2026-08-01T00:02:00+00:00", 101.9, 103.0, 101.0, 102.04)
    engine._bars.append(weak_sl_bar)
    engine._manage_zones(weak_sl_bar)
    assert supply.terminal_state is None

    strong_sl_bar = Candle(3, "2026-08-01T00:03:00+00:00", 101.0, 103.1, 100.9, 103.0)
    engine._bars.append(strong_sl_bar)
    engine._manage_zones(strong_sl_bar)
    assert supply.terminal_state == "SL_HIT"


def test_dataset_creation_freezes_hash_and_loads(tmp_path):
    db = DatabaseService(tmp_path / "bot.db")
    service = IndicatorDatasetService(tmp_path, db, {})
    dataset = service.create_mt5_dataset(
        name="XAUUSD_M1_2026_08_01_2026_09_14",
        symbol="XAUUSD",
        timeframe="M1",
        start_date="2026-08-01",
        end_date="2026-08-01",
        connector=FakeConnector(),
    )
    assert dataset["status"] == "READY"
    assert dataset["bar_count"] == 90
    assert service.verify_hash(dataset["id"])["ok"] is True
    loaded = service.load_frame(dataset["id"])
    assert list(loaded.columns) == ["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    assert loaded["timestamp_utc"].str.endswith("+00:00").all()


def test_campaign_matrix_snapshots_and_resume(tmp_path):
    db = DatabaseService(tmp_path / "bot.db")
    lab = IndicatorLabService(tmp_path, db, {})
    dataset = lab.dataset_service.create_mt5_dataset(
        name="frozen",
        symbol="XAUUSD",
        timeframe="M1",
        start_date="2026-08-01",
        end_date="2026-08-01",
        connector=FakeConnector(),
    )
    preview = lab.preview_campaign(
        dataset_id=dataset["id"],
        indicator="LEBPRIME",
        fixed_parameters={"zone_confirm_delay": 0},
        optimization_grid={"momentum_count": {"enabled": True, "start": 2, "end": 3, "step": 1}},
    )
    assert preview["generated_configurations"] == 2
    created = lab.create_campaign(
        dataset_id=dataset["id"],
        indicator="LEBPRIME",
        fixed_parameters={"zone_confirm_delay": 0, "min_zone_width_atr": 0.01, "max_zone_width_atr": 5.0},
        optimization_grid={"momentum_count": {"enabled": True, "start": 2, "end": 3, "step": 1}},
    )
    campaign_id = created["campaign"]["id"]
    result = lab.execute_campaign(campaign_id, worker_id="test-worker")
    assert result["progress_current"] == 2
    rows = lab.campaign_results(campaign_id)
    assert len(rows) == 2
    assert rows[0]["parameter_snapshot_json"]["dataset_id"] == dataset["id"]
    assert db._query_dataframe("SELECT COUNT(1) AS c FROM indicator_campaign_tests WHERE campaign_id = ? AND state = 'PENDING';", (campaign_id,))[0]["c"] == 0


def test_worker_processes_indicator_campaign_job(tmp_path):
    shutil.copyfile(Path(__file__).resolve().parents[1] / "config.json", tmp_path / "config.json")
    db = DatabaseService(tmp_path / "storage" / "bot.db")
    lab = IndicatorLabService(tmp_path, db, {"storage": {"database_path": "storage/bot.db"}})
    dataset = lab.dataset_service.create_mt5_dataset(name="worker_frozen", symbol="XAUUSD", timeframe="M1", start_date="2026-08-01", end_date="2026-08-01", connector=FakeConnector())
    created = lab.create_campaign(dataset_id=dataset["id"], indicator="VSS3", fixed_parameters={"min_zone_size": 1, "min_distance_percent": 0.01}, optimization_grid={})
    worker = WorkerService(tmp_path, poll_interval_seconds=0.01)
    worker.run_once()
    campaign = lab.get_campaign(created["campaign"]["id"])
    assert campaign["state"] in {"COMPLETED", "PARTIAL"}


def test_enhanced_zone_persistence_detail_and_csv_exports(tmp_path):
    db = DatabaseService(tmp_path / "bot.db")
    lab = IndicatorLabService(tmp_path, db, {})
    dataset = lab.dataset_service.create_mt5_dataset(name="enhanced", symbol="XAUUSD", timeframe="M1", start_date="2026-08-01", end_date="2026-08-01", connector=FakeConnector())
    created = lab.create_campaign(
        dataset_id=dataset["id"],
        indicator="LEBPRIME",
        fixed_parameters={"zone_confirm_delay": 0, "min_zone_width_atr": 0.01, "max_zone_width_atr": 5.0},
        optimization_grid={},
    )
    campaign_id = created["campaign"]["id"]
    lab.execute_campaign(campaign_id, worker_id="test-worker")
    results = lab.campaign_results(campaign_id)
    assert results
    test_id = results[0]["id"]
    zones = lab.test_zones(test_id)
    assert zones
    zone = zones[0]
    assert zone["symbol"] == "XAUUSD"
    assert zone["timeframe"] == "M1"
    assert zone["zone_mid"] is not None
    assert zone["parameter_hash"] == results[0]["parameter_hash"]
    assert zone["dataset_hash"] == results[0]["dataset_hash"]
    assert zone["pine_reference_hash"] == results[0]["pine_reference_hash"]
    assert isinstance(zone["metadata_json"], dict)
    assert "zone_confirm_delay_used" in zone["metadata_json"]

    detail = lab.get_zone(zone["id"])
    assert detail is not None
    assert detail["parameter_snapshot_json"]["parameters"]
    events = lab.zone_events(zone["id"])
    assert events == sorted(events, key=lambda row: (row["timestamp"], row["bar_index"], row["id"]))
    assert any(event["event_type"] == "ZONE_CREATED" for event in events)
    assert all("zone_top" in event for event in events)

    zones_csv = lab.zones_csv(test_id)
    assert zones_csv.startswith("\ufeff")
    zone_rows = list(csv.DictReader(io.StringIO(zones_csv.lstrip("\ufeff"))))
    assert zone_rows
    assert {
        "campaign_id", "test_id", "zone_id", "source_open", "candidate_open", "candidate_body",
        "compact_base_start_offset", "compact_base_end_offset", "refinement_target_offset",
        "refined_open", "bars_source_to_candidate", "bars_candidate_to_created", "zone_age_at_terminal",
        "departure_distance_price", "mfe_points", "mae_points", "zone_width_atr", "parameter_hash",
        "metadata_json",
    }.issubset(zone_rows[0])

    candidates = lab.test_candidates(test_id)
    assert candidates
    assert any(row["outcome"] == "LIMIT_CREATED" for row in candidates)
    candidates_csv = lab.candidates_csv(test_id)
    candidate_rows = list(csv.DictReader(io.StringIO(candidates_csv.lstrip("\ufeff"))))
    assert candidate_rows
    assert {
        "candidate_id", "side", "candidate_bar_index", "candidate_time", "source_bar_index",
        "source_time", "raw_zone_top", "raw_zone_bottom", "clamped_zone_top", "clamped_zone_bottom",
        "zone_width", "ATR", "min_width_required", "max_width_allowed", "compact_base_start",
        "compact_base_end", "refinement_method", "refined_bar_index", "refined_bar_time",
        "same_side_spacing_ok", "initial_overlap_ok", "confirmation_due_bar",
        "confirmation_due_time", "outcome", "outcome_reason", "pending_bars_json",
    }.issubset(candidate_rows[0])

    events_csv = lab.events_csv(test_id)
    event_rows = list(csv.DictReader(io.StringIO(events_csv.lstrip("\ufeff"))))
    assert event_rows
    assert {"campaign_id", "test_id", "zone_id", "event_type", "zone_top", "entry", "metadata_json"}.issubset(event_rows[0])

    results_csv = lab.campaign_results_csv(campaign_id)
    result_rows = list(csv.DictReader(io.StringIO(results_csv.lstrip("\ufeff"))))
    assert result_rows
    assert "zone_confirm_delay" in result_rows[0]
    assert {"rank", "test_id", "parameter_hash", "zones_created", "net_r", "engine_version"}.issubset(result_rows[0])


def test_vss3_diagnostics_and_null_safe_old_zone_rows(tmp_path):
    db = DatabaseService(tmp_path / "bot.db")
    lab = IndicatorLabService(tmp_path, db, {})
    dataset = lab.dataset_service.create_mt5_dataset(name="vss_diag", symbol="XAUUSD", timeframe="M1", start_date="2026-08-01", end_date="2026-08-01", connector=FakeConnector())
    created = lab.create_campaign(dataset_id=dataset["id"], indicator="VSS3", fixed_parameters={"min_zone_size": 1, "min_distance_percent": 0.01}, optimization_grid={})
    campaign_id = created["campaign"]["id"]
    lab.execute_campaign(campaign_id, worker_id="test-worker")
    test_id = lab.campaign_results(campaign_id)[0]["id"]
    zones = lab.test_zones(test_id)
    assert zones
    assert any("raw_zone_top" in zone["metadata_json"] for zone in zones)

    db._execute(
        """
        INSERT INTO indicator_zones (
            campaign_id, test_id, dataset_id, indicator, engine_zone_id, side,
            source_bar_index, source_time, zone_top, zone_bottom, zone_width,
            created_bar_index, created_time, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (campaign_id, test_id, dataset["id"], "VSS3", "old-zone", "DEMAND", 1, "2026-08-01T00:01:00+00:00", 2.0, 1.0, 1.0, 1, "2026-08-01T00:01:00+00:00", "{}"),
    )
    old = [zone for zone in lab.test_zones(test_id) if zone["engine_zone_id"] == "old-zone"][0]
    assert old["zone_mid"] is None
    assert old["metadata_json"] == {}
    csv_text = lab.zones_csv(test_id)
    assert "old-zone" in csv_text
