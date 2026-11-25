"""TradingView zone parity (Pine zone lifecycle next to the trade, Pine pending retry, chart window) and the
break-even + trailing-stop exit of the Live broker model."""

from __future__ import annotations

import numpy as np
import pandas as pd

from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import Candle, ZoneRecord
from services.indicator_lab.parameter_registry import default_parameters

PINE = "Pine indicator"
TRAIL = {"execution_model": "Live broker", "exit_mode": "Trail", "be_trigger_r": 0.5, "be_lock_r": 0.0,
         "trail_start_r": 1.0, "trail_mode": "Distance R", "trail_distance_r": 0.5}


def _walk(bars: int = 6000, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 2000 + rng.normal(0, 1.2, bars).cumsum()
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.random(bars) * 0.8
    low = np.minimum(open_, close) - rng.random(bars) * 0.8
    times = pd.date_range("2025-01-02", periods=bars, freq="5min", tz="UTC")
    return pd.DataFrame({"timestamp_utc": times.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": open_.round(2), "high": high.round(2), "low": low.round(2), "close": close.round(2)})


def _limits(params: dict, frame: pd.DataFrame) -> list[tuple]:
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), **params}, symbol="XAUUSD", timeframe="M5")
    engine.run(frame)
    return sorted((z.engine_zone_id, z.limit_created_bar_index, z.side, z.zone_top, z.zone_bottom) for z in engine.zones() if z.limit_created_bar_index is not None)


def test_live_model_with_parity_creates_exactly_the_pine_zones():
    frame = _walk()
    for extra in ({}, {"zone_window_bars": 500}, {"max_zones": 3}):
        pine = _limits({"execution_model": PINE, **extra}, frame)
        live = _limits({**TRAIL, "tv_zone_parity": True, **extra}, frame)
        assert len(pine) > 50
        assert live == pine, extra


def test_pine_keeps_hidden_candidate_while_side_is_full():
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), "execution_model": PINE, "max_zones": 1}, symbol="XAUUSD")
    blocker = ZoneRecord(engine_zone_id="old", side="DEMAND", source_bar_index=0, source_time="t", zone_top=90.0, zone_bottom=89.0,
                         created_bar_index=0, created_time="t", limit_created_bar_index=0, limit_created_time="t", entry1=90.0, entry2=89.0,
                         entry_price=90.0, sl_price=88.0, tp1_price=92.0, tp2_price=94.0, tp_price=94.0)
    engine._zones = [blocker]
    engine._bars = [Candle(i, f"t{i}", 100.0, 100.5, 99.9, 100.2) for i in range(20)]
    engine._bars.append(Candle(20, "t20", 101.0, 101.5, 100.9, 101.2))
    candidate = engine._bars[5]
    engine._pending = [{"demand": True, "top": 100.0, "bottom": 99.5, "source_index": 2, "due": 7, "id": "new", "avg_body": 0.2, "context": {}, "candidate": candidate, "diagnostic": None}]
    engine._process_pending(engine._bars[-1], atr=1.0)
    assert [z.engine_zone_id for z in engine._zones] == ["old"] and len(engine._pending) == 1   # side full: wait, as Pine does
    blocker.terminal_state = "CANCELED"
    engine._process_pending(engine._bars[-1], atr=1.0)
    assert [z.engine_zone_id for z in engine._zones] == ["old", "new"] and engine._pending == []


def test_zone_window_drops_old_zones_and_cancels_unfilled_limit():
    frame = _walk(3000)
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), **TRAIL, "tv_zone_parity": True, "zone_window_bars": 300}, symbol="XAUUSD", timeframe="M5")
    engine.run(frame)
    for zone in engine.zones():
        if zone.limit_created_bar_index is None:
            continue
        if zone.metadata.get("pine_state") == "EXPIRED":
            assert int(zone.metadata["pine_state_bar_index"]) - int(zone.candidate_bar_index) == 300
        if zone.terminal_state == "EXPIRED":
            assert zone.activated_bar_index is None
    assert any(z.terminal_state == "EXPIRED" for z in engine.zones())


def _trail_zone() -> ZoneRecord:
    # Entry 1 = 100, SL = 98 (risk 2).
    return ZoneRecord(engine_zone_id="trail", side="DEMAND", source_bar_index=-20, source_time="t", zone_top=100.0, zone_bottom=99.0,
                      created_bar_index=0, created_time="t", limit_created_bar_index=0, limit_created_time="t", entry1=100.0, entry2=99.0,
                      entry_price=100.0, sl_price=98.0, tp1_price=102.0, tp2_price=104.0, tp_price=104.0)


def _drive(params: dict, bars: list[tuple]) -> tuple[ZoneRecord, list]:
    engine = LebPrimeEngine({**default_parameters("LEBPRIME"), **params}, symbol="XAUUSD")
    zone = _trail_zone()
    engine._zones = [zone]
    engine._bars = [Candle(0, "t0", 101.0, 101.5, 100.5, 101.0)]
    for i, (o, h, l, c) in enumerate(bars, start=1):
        bar = Candle(i, f"t{i}", o, h, l, c)
        engine._bars.append(bar)
        engine._manage_zones(bar)
    return zone, engine.events()


def test_trail_exit_break_even_then_trailing_stop():
    zone, events = _drive(TRAIL, [
        (101.0, 101.0, 99.8, 100.2),   # fill at 100
        (100.2, 101.2, 100.1, 101.0),  # best 101.2 = +0.6R -> stop to 100 (break-even) from the next candle
        (101.0, 103.0, 100.8, 102.8),  # best 103 = +1.5R -> stop 103 - 0.5R = 102
        (102.8, 104.0, 102.5, 103.9),  # best 104 = +2R -> stop 103
        (103.9, 103.95, 102.9, 103.0), # touches 103 -> trailing exit at 103 = +1.5R
    ])
    stops = [round(e.price, 4) for e in events if e.event_type == "STOP_MOVED"]
    assert stops == [100.0, 102.0, 103.0]
    assert zone.terminal_state == "TRAIL_EXIT"
    assert abs(zone.result_r - 1.5) < 1e-9
    assert not any(e.event_type in {"TP1_HIT", "TP2_HIT"} for e in events)   # no take profit


def test_trail_exit_break_even_exit_and_plain_stop():
    zone, _ = _drive(TRAIL, [(101.0, 101.0, 99.8, 100.2), (100.2, 101.2, 100.1, 101.0), (101.0, 101.1, 99.9, 100.0)])
    assert zone.terminal_state == "BE_EXIT" and abs(zone.result_r) < 1e-9
    zone, _ = _drive(TRAIL, [(101.0, 101.0, 99.8, 100.2), (100.2, 100.6, 97.5, 97.8)])
    assert zone.terminal_state == "SL_HIT" and abs(zone.result_r + 1.0) < 1e-9   # touch SL at the zone SL = -1R
    # SL touched on the fill candle itself
    zone, _ = _drive(TRAIL, [(101.0, 101.0, 97.9, 98.5)])
    assert zone.terminal_state == "SL_HIT"


def test_trail_exit_is_single_entry():
    zone, events = _drive(TRAIL, [(101.0, 101.0, 98.9, 99.2), (99.2, 99.5, 98.6, 99.0)])
    assert not any(e.event_type == "ENTRY2_ACTIVATED" for e in events)


# ---------------------------------------------------------------- live service with the Trail exit
from datetime import datetime, timezone  # noqa: E402

from services.indicator_lab.models import ZoneEvent  # noqa: E402
from services.lebprime_live import LebPrimeLiveService  # noqa: E402
from tests.test_lebprime_live_v3_4_0 import _FakeConnector, _FakeRiskManager, _config, _limit_event, _zone  # noqa: E402


class _TrailConnector(_FakeConnector):
    def __init__(self, mapping, *, demo=True):
        super().__init__(mapping, demo=demo)
        self.modified = []

    def modify_position_sltp(self, ticket, sl, tp, comment=None):
        self.modified.append((str(ticket), sl, tp, comment))
        return True, None


def test_service_places_one_entry_without_tp_and_moves_the_broker_stop(tmp_path):
    config = _config(enabled=True, mode="LIVE")
    config["lebprime_v3_4_0"]["parameters"].update({**TRAIL, "tv_zone_parity": True, "zone_window_bars": 500})
    connectors = {}
    risk = _FakeRiskManager()
    service = LebPrimeLiveService(config, tmp_path / "live.db", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _TrailConnector(mapping)), risk_manager=risk, risk_state={})
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0917-TRAIL", "DEMAND", "XAUUSD", "M1", 100, 102)
    context = service.contexts["XAUUSD-M1"]
    service._register_limit(context, zone, _limit_event(zone), execute=True)
    connector = connectors["XAUUSD"]
    assert len(connector.sent) == 1                          # Entry 1 only
    request = connector.sent[0]
    assert request["price"] == zone.entry1 and request["sl"] == zone.sl_price and request["tp"] == 0.0   # touch SL at the zone SL, no TP
    assert risk.adjustments == [1.0]                         # the single entry carries the full risk
    connector.fill(1001, deal_ticket=2001, position_ticket=3001, price=102.0)
    service._reconcile_broker_execution(datetime.now(timezone.utc))
    service._track_lifecycle(context, zone, ZoneEvent(zone.engine_zone_id, 9, "2026-09-17T08:09:00+00:00", "STOP_MOVED", price=102.004), execute=True)
    assert connector.modified == [("3001", 102.0, None, "LP340-TRAIL")]
    service._track_lifecycle(context, zone, ZoneEvent(zone.engine_zone_id, 12, "2026-09-17T08:12:00+00:00", "TRAIL_EXIT", price=104.0), execute=True)
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "CLOSED"


def test_bootstrap_only_searches_the_chart_window(tmp_path):
    config = _config(enabled=True, mode="DRY")
    config["lebprime_v3_4_0"]["parameters"].update({**TRAIL, "tv_zone_parity": True, "zone_window_bars": 500})
    service = LebPrimeLiveService(config, tmp_path / "live.db", connector_factory=lambda mapping: _FakeConnector(mapping), risk_state={})
    frame = _walk(1500)
    frame = frame.rename(columns={"timestamp_utc": "time"})
    context = service.contexts["XAUUSD-M5"]
    service.process_frame(context.key, frame, execute_new=False)
    found = [z.candidate_bar_index for z in context.engine.zones()]
    assert found and min(found) >= 1000
