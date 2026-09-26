from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import mt5_connector as mt5_connector_module
from mt5_connector import MT5Connector
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import Candle, ZoneEvent, ZoneRecord
from services.lebprime_live import LebPrimeLiveService, aggregate_m45, resolve_symbol
from services.lebprime_live.service import ACCOUNT_NOT_CONFIRMED_DEMO
from services.telegram_notifier import TelegramNotifier


def _config(*, enabled: bool = False, mode: str = "DRY") -> dict:
    return {
        "lebprime_v3_4_0": {
            "enabled": enabled,
            "execution_mode": mode,
            "symbols": ["XAUUSD", "US30", "NAS100"],
            "timeframes": ["M1", "M3", "M5", "M15", "M30", "M45"],
            "parameters": {"gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0},
            "max_spread_points": 25.0,
        },
        "execution": {"max_spread_points": 25.0},
        "telegram": {
            "enabled": True,
            "signal_alerts": True,
            "execution_success_alerts": True,
            "execution_blocked_alerts": True,
        },
    }


def _service(tmp_path, *, enabled: bool = False, mode: str = "DRY", connector_factory=None, risk_manager=None, notifier=None, logger=None):
    return LebPrimeLiveService(
        _config(enabled=enabled, mode=mode),
        tmp_path / "live.db",
        connector_factory=connector_factory,
        risk_manager=risk_manager,
        risk_state={},
        notifier=notifier,
        logger=logger,
    )


def _zone(zone_id: str, side: str, symbol: str, timeframe: str, bottom: float, top: float) -> ZoneRecord:
    demand = side == "DEMAND"
    entry1 = top if demand else bottom
    entry2 = bottom if demand else top
    stop = bottom - 1.0 if demand else top + 1.0
    risk = abs(entry1 - stop)
    return ZoneRecord(
        engine_zone_id=zone_id,
        side=side,
        source_bar_index=1,
        source_time="2026-09-17T08:00:00+00:00",
        zone_top=top,
        zone_bottom=bottom,
        created_bar_index=5,
        created_time="2026-09-17T08:05:00+00:00",
        symbol=symbol,
        timeframe=timeframe,
        limit_created_bar_index=5,
        limit_created_time="2026-09-17T08:05:00+00:00",
        entry1=entry1,
        entry2=entry2,
        entry_price=entry1,
        sl_price=stop,
        tp1_price=entry1 + risk if demand else entry1 - risk,
        tp2_price=entry1 + 2 * risk if demand else entry1 - 2 * risk,
        atr_at_source=2.0,
        buffer_used=resolve_symbol(symbol).sl_buffer,
        indicator_version="3.4.0",
    )


def _limit_event(zone: ZoneRecord) -> ZoneEvent:
    return ZoneEvent(
        zone.engine_zone_id,
        5,
        "2026-09-17T08:05:00+00:00",
        "LIMIT_CREATED",
        price=zone.entry1,
    )


def test_creates_18_isolated_contexts_and_all_timeframes(tmp_path):
    service = _service(tmp_path)
    assert len(service.contexts) == 18
    assert service.status_snapshot()["context_count"] == 18
    assert {context.timeframe for context in service.contexts.values()} == {"M1", "M3", "M5", "M15", "M30", "M45"}
    assert len({id(context.engine) for context in service.contexts.values()}) == 18
    service.contexts["XAUUSD-M1"].engine._bull_leg = True
    assert not service.contexts["XAUUSD-M3"].engine._bull_leg
    assert not service.contexts["US30-M1"].engine._bull_leg


def test_symbol_mapping_and_production_buffers(tmp_path):
    assert resolve_symbol("XAUUSD").mt5_symbol == "XAUUSD"
    assert resolve_symbol("US30").mt5_symbol == "US30"
    assert resolve_symbol("NAS100").mt5_symbol == "USTEC"
    service = _service(tmp_path)
    assert service.contexts["XAUUSD-M1"].engine._buffer() == 1.0
    assert service.contexts["US30-M1"].engine._buffer() == 30.0
    assert service.contexts["NAS100-M1"].engine._buffer() == 15.0


def test_zone_ids_are_unique_by_symbol_timeframe_and_day(tmp_path):
    service = _service(tmp_path)
    bar = Candle(10, "2026-09-17T08:00:00+00:00", 1, 2, 0, 1)
    ids = {
        context.engine._zone_id(True, bar)
        for context in service.contexts.values()
    }
    assert len(ids) == 18
    assert "XAU-1M-B-0917-LEB01" in ids
    assert "US30-5M-B-0917-LEB01" in ids
    assert "NAS-30M-B-0917-LEB01" in ids


def test_m45_aggregation_emits_only_complete_closed_periods():
    frame = pd.DataFrame(
        [
            {"time": "2026-09-17T00:00:00Z", "open": 10, "high": 12, "low": 9, "close": 11, "tick_volume": 1},
            {"time": "2026-09-17T00:15:00Z", "open": 11, "high": 14, "low": 10, "close": 13, "tick_volume": 2},
            {"time": "2026-09-17T00:30:00Z", "open": 13, "high": 15, "low": 12, "close": 14, "tick_volume": 3},
            {"time": "2026-09-17T00:45:00Z", "open": 14, "high": 16, "low": 13, "close": 15, "tick_volume": 4},
        ]
    )
    result = aggregate_m45(frame, datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc))
    assert len(result) == 1
    assert result.iloc[0]["open"] == 10
    assert result.iloc[0]["high"] == 15
    assert result.iloc[0]["low"] == 9
    assert result.iloc[0]["close"] == 14
    assert result.iloc[0]["tick_volume"] == 6


def test_distinct_zone_ids_are_not_suppressed_by_geometric_overlap(tmp_path):
    service = _service(tmp_path)
    xau_m1 = service.contexts["XAUUSD-M1"]
    first = _zone("XAU-1M-B-0917-LEB01", "DEMAND", "XAUUSD", "M1", 4320, 4322)
    overlap = _zone("XAU-5M-B-0917-LEB01", "DEMAND", "XAUUSD", "M5", 4321, 4323)
    separate = _zone("XAU-15M-B-0917-LEB01", "DEMAND", "XAUUSD", "M15", 4330, 4332)
    opposite = _zone("XAU-30M-S-0917-LEB01", "SUPPLY", "XAUUSD", "M30", 4321, 4323)

    service._register_limit(xau_m1, first, _limit_event(first), execute=False)
    service._register_limit(service.contexts["XAUUSD-M5"], overlap, _limit_event(overlap), execute=False)
    service._register_limit(service.contexts["XAUUSD-M15"], separate, _limit_event(separate), execute=False)
    service._register_limit(service.contexts["XAUUSD-M30"], opposite, _limit_event(opposite), execute=False)

    first_row = service.repository.zone(first.engine_zone_id)
    overlap_row = service.repository.zone(overlap.engine_zone_id)
    separate_row = service.repository.zone(separate.engine_zone_id)
    opposite_row = service.repository.zone(opposite.engine_zone_id)
    assert first_row["execution_primary"] == 1
    assert overlap_row["execution_status"] == "DRY_RUN_PRIMARY"
    assert overlap_row["execution_primary"] == 1
    assert overlap_row["suppressed_by_zone_id"] is None
    assert overlap_row["family_id"] != first_row["family_id"]
    assert separate_row["execution_primary"] == 1
    assert separate_row["family_id"] != first_row["family_id"]
    assert opposite_row["execution_primary"] == 1
    assert opposite_row["family_id"] != first_row["family_id"]


def test_distinct_overlapping_zone_continues_theoretical_lifecycle(tmp_path):
    service = _service(tmp_path)
    first = _zone("XAU-1M-B-0917-LEB01", "DEMAND", "XAUUSD", "M1", 100, 102)
    duplicate = _zone("XAU-5M-B-0917-LEB01", "DEMAND", "XAUUSD", "M5", 101, 103)
    service._register_limit(service.contexts["XAUUSD-M1"], first, _limit_event(first), execute=False)
    service._register_limit(service.contexts["XAUUSD-M5"], duplicate, _limit_event(duplicate), execute=False)
    duplicate.activated_bar_index = 8
    duplicate.activated_time = "2026-09-17T08:08:00+00:00"
    service._track_lifecycle(
        service.contexts["XAUUSD-M5"],
        duplicate,
        ZoneEvent(duplicate.engine_zone_id, 8, duplicate.activated_time, "ENTRY1_ACTIVATED", price=duplicate.entry1),
    )
    row = service.repository.zone(duplicate.engine_zone_id)
    assert row["execution_status"] == "DRY_RUN_PRIMARY"
    assert row["theoretical_state"] == "ACTIVE"
    assert [event["event_type"] for event in service.repository.events_for_zone(duplicate.engine_zone_id)][-1] == "ENTRY1_ACTIVATED"


class _FakeConnector:
    TIMEFRAME_MAP = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30}

    def __init__(self, mapping, *, demo=True):
        self.mapping = mapping
        self.mt5 = SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0, ORDER_STATE_CANCELED=4, ORDER_STATE_EXPIRED=6, ORDER_STATE_REJECTED=5)
        self.demo = demo
        self.sent = []
        self.canceled = []
        self.pending = []
        self.positions = []
        self.orders_history = []
        self.deals = []
        self.partial_closes = []
        self.calls = []
        self.on_precheck = None
        self.spread_check_calls = 0
        self.next_ticket = 1001
        self.tick = SimpleNamespace(ask=110.0, bid=109.8)

    def ensure_initialized(self): return True
    def ensure_symbol_selected(self, symbol, log_details=False): return symbol == self.mapping.mt5_symbol
    def get_account_info(self): return SimpleNamespace(trade_mode=0 if self.demo else 2, trade_allowed=True, balance=10000, equity=10000, margin=0, margin_free=10000)
    def get_terminal_info(self): return SimpleNamespace(trade_allowed=True)
    def is_connected(self): return True
    def ensure_connection(self): return True
    def spread_check_stage(self, limit):
        self.spread_check_calls += 1
        spec = self.get_symbol_spec()
        points = (self.tick.ask - self.tick.bid) / spec["point"]
        ok = points <= float(limit)
        return {"ok": ok, "reason_code": "spread_ok" if ok else "spread_too_wide", "payload": {"spread_points": round(points, 2), "max_spread_points": float(limit), "tick": {"bid": self.tick.bid, "ask": self.tick.ask}}}
    def get_symbol_spec(self, force_refresh=False): return {"pip_size": 1.0, "point": 0.01, "trade_tick_size": 0.01, "stops_level": 0, "digits": 2, "volume_min": 0.01, "volume_max": 10.0, "volume_step": 0.01, "pip_value_per_lot": 1.0}
    def pending_order_type(self, direction): return 2 if direction == "LONG" else 3
    def calculate_margin(self, order_type, volume, price): return volume * 100
    def build_pending_limit_request(self, **kwargs):
        request = {"action": 5, "symbol": self.mapping.mt5_symbol, "volume": kwargs["volume"], "type": self.pending_order_type(kwargs["direction"]), "price": kwargs["entry_price"], "sl": kwargs["sl"], "tp": kwargs["tp"], "comment": kwargs["comment"], "type_filling": 1}
        return request, "", {"entry": request["price"], "sl": request["sl"], "tp": request["tp"], "volume": request["volume"]}
    def precheck_order(self, request):
        self.calls.append(("check", request["comment"]))
        if self.on_precheck: self.on_precheck(request)
        return {"ok": True, "payload": {"retcode": 0, "comment": "Done"}}
    def send_order(self, request, dry_run=False):
        self.calls.append(("send", request["comment"]))
        self.sent.append(request)
        ticket = self.next_ticket
        self.next_ticket += 1
        self.pending.append(SimpleNamespace(ticket=ticket, order=ticket, comment=request["comment"], magic=234000, price_open=request["price"], volume_initial=request["volume"]))
        return {"ok": True, "details": {"result": {"retcode": 10008, "order": ticket}}}
    def cancel_pending_order(self, order_id):
        self.canceled.append(str(order_id))
        self.pending = [item for item in self.pending if str(item.ticket) != str(order_id)]
        return True, SimpleNamespace(retcode=10009)
    def get_tick(self): return self.tick
    def get_pending_orders(self): return list(self.pending)
    def get_bot_positions(self): return list(self.positions)
    def get_bot_order_history(self): return list(self.orders_history)
    def get_bot_deal_history(self): return list(self.deals)
    def find_bot_position(self, order_id=None, comment=None):
        return next((item for item in self.positions if str(getattr(item, "identifier", "")) == str(order_id) or item.comment == comment), None)
    def close_partial(self, ticket, volume, comment=None): self.partial_closes.append((str(ticket), volume, comment)); return True, SimpleNamespace(retcode=10009)
    def close_position(self, ticket, comment=None): return True, SimpleNamespace(retcode=10009)
    def fill(self, ticket, *, deal_ticket, position_ticket, price, entry=0):
        order = next(item for item in self.pending if str(item.ticket) == str(ticket))
        self.pending.remove(order)
        self.orders_history.append(SimpleNamespace(ticket=order.ticket, order=order.ticket, comment=order.comment, magic=234000, state=4))
        self.deals.append(SimpleNamespace(ticket=deal_ticket, order=order.ticket, position_id=position_ticket, comment=order.comment, magic=234000, price=price, volume=order.volume_initial, entry=entry, time=1_789_000_000))
        self.positions.append(SimpleNamespace(ticket=position_ticket, identifier=position_ticket, comment=order.comment, magic=234000, volume=order.volume_initial))
    def shutdown(self): return None
    def fetch_rates(self, timeframe, bars, min_rows=1):
        return pd.DataFrame([
            {"time": "2026-09-17T00:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1},
            {"time": "2026-09-17T00:15:00Z", "open": 1, "high": 3, "low": 1, "close": 2},
            {"time": "2026-09-17T00:30:00Z", "open": 2, "high": 4, "low": 2, "close": 3},
        ])


class _FakeRiskManager:
    def __init__(self): self.adjustments = []
    def evaluate_risk_gate(self, *args, **kwargs): return {"live_allowed": True, "reduced_risk": False, "reasons": []}
    def calculate_position_size(self, *args, **kwargs): self.adjustments.append(kwargs["risk_adjustment"]); return {"valid": True, "volume": 0.2}
    def fit_volume_to_margin(self, requested_volume, *args, **kwargs): return {"valid": True, "volume": requested_volume}


def test_demo_guard_and_m45_fallback(tmp_path):
    live_connector = {}
    service = _service(
        tmp_path,
        enabled=True,
        mode="LIVE",
        connector_factory=lambda mapping: live_connector.setdefault(mapping.logical_symbol, _FakeConnector(mapping, demo=False)),
        risk_manager=_FakeRiskManager(),
    )
    ok, reason = service.start()
    assert not ok
    assert ACCOUNT_NOT_CONFIRMED_DEMO in reason
    assert all(not connector.sent for connector in live_connector.values())

    dry = _service(tmp_path / "dry", enabled=True, mode="DRY", connector_factory=lambda mapping: _FakeConnector(mapping))
    frame = dry._fetch_frame(dry.contexts["XAUUSD-M45"], datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc))
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 3


def test_context_failure_does_not_stop_other_contexts(tmp_path):
    class SelectiveFailureConnector(_FakeConnector):
        def fetch_rates(self, timeframe, bars, min_rows=1):
            if timeframe == "M3":
                raise RuntimeError("M3 feed unavailable")
            return super().fetch_rates(timeframe, bars, min_rows)

    service = _service(
        tmp_path,
        enabled=True,
        mode="DRY",
        connector_factory=lambda mapping: SelectiveFailureConnector(mapping),
    )
    snapshot = service.scan_once(datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc))
    contexts = {row["context_key"]: row for row in snapshot["contexts"]}
    assert contexts["XAUUSD-M3"]["status"] == "ERROR"
    assert contexts["XAUUSD-M1"]["status"] == "HEALTHY"
    assert contexts["US30-M1"]["status"] == "HEALTHY"


def test_realtime_buy_limit_created_places_both_entries_with_half_risk(tmp_path):
    connectors = {}
    risk = _FakeRiskManager()
    service = _service(
        tmp_path,
        enabled=True,
        mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=risk,
    )
    assert service.start()[0]
    first = _zone("XAU-1M-B-0917-LEB01", "DEMAND", "XAUUSD", "M1", 100, 102)
    duplicate = _zone("XAU-5M-B-0917-LEB01", "DEMAND", "XAUUSD", "M5", 101, 103)
    service._register_limit(service.contexts["XAUUSD-M1"], first, _limit_event(first), execute=True)
    service._register_limit(service.contexts["XAUUSD-M5"], duplicate, _limit_event(duplicate), execute=True)
    connector = connectors["XAUUSD"]
    assert len(connector.sent) == 4
    assert [request["type"] for request in connector.sent] == [2, 2, 2, 2]
    assert [request["price"] for request in connector.sent[:2]] == [first.entry1, first.entry2]
    assert all(request["sl"] == first.sl_price and request["tp"] == first.tp2_price for request in connector.sent[:2])
    assert [kind for kind, _ in connector.calls] == ["check", "send"] * 4
    assert risk.adjustments == [0.5, 0.5, 0.5, 0.5]
    row = service.repository.zone(first.engine_zone_id)
    assert row["entry1_order_id"] == "1001"
    assert row["entry2_order_id"] == "1002"
    assert row["execution_status"] == "BROKER_PENDING"
    assert service.repository.zone(duplicate.engine_zone_id)["execution_status"] == "BROKER_PENDING"


class _CaptureLogger:
    def __init__(self): self.events = []
    def structured(self, category, payload, level="INFO"): self.events.append((category, payload, level))


class _FakeNotifier:
    def __init__(self, *, created_ok=True):
        self.created_ok = created_ok
        self.created = []
        self.accepted = []
        self.failed = []
        self.last_error = None

    def send_lebprime_zone_created(self, payload):
        self.created.append(dict(payload))
        if not self.created_ok:
            self.last_error = "telegram unavailable"
        return self.created_ok

    def send_lebprime_pending_accepted(self, payload):
        self.accepted.append(dict(payload))
        return True

    def send_lebprime_pending_failed(self, payload):
        self.failed.append(dict(payload))
        return True


def _activate(service, zone, *, execute=True, historical=False, reason=None):
    zone.activated_bar_index = 6
    zone.activated_time = "2026-09-17T08:06:00+00:00"
    service._track_lifecycle(
        service.contexts["XAUUSD-M1"],
        zone,
        ZoneEvent(zone.engine_zone_id, 6, zone.activated_time, "ENTRY1_ACTIVATED", price=zone.entry1),
        execute=execute,
        historical=historical,
        execution_block_reason=reason,
    )


def test_historical_activation_is_reconstructed_but_never_sent(tmp_path):
    connectors = {}
    logger = _CaptureLogger()
    service = LebPrimeLiveService(
        _config(enabled=True, mode="LIVE"), tmp_path / "live.db", logger=logger,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(), risk_state={},
    )
    zone = _zone("XAU-1M-B-0917-HIST", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=False, historical=True, execution_block_reason="HISTORICAL_REPLAY")
    _activate(service, zone, execute=False, historical=True, reason="HISTORICAL_REPLAY")
    assert connectors.get("XAUUSD") is None or connectors["XAUUSD"].sent == []
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "HISTORICAL_REPLAY"
    activation = next(payload for category, payload, _ in logger.events if category == "lebprime_entry1_activated")
    assert activation["historical"] is True
    assert activation["execution_eligible"] is False


def test_historical_pending_zone_never_executes_on_later_candle_touch(tmp_path):
    connectors = {}
    service = _service(
        tmp_path, enabled=True, mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    zone = _zone("XAU-1M-B-0917-REALTIME", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=False, historical=True, execution_block_reason="HISTORICAL_REPLAY")
    _activate(service, zone, execute=True, historical=False)
    _activate(service, zone, execute=True, historical=False)
    assert connectors.get("XAUUSD") is None or connectors["XAUUSD"].sent == []
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "HISTORICAL_REPLAY"


def test_dry_limit_created_blocks_order_send(tmp_path):
    connectors = {}
    service = _service(
        tmp_path, enabled=True, mode="DRY",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    zone = _zone("XAU-1M-B-0917-DRY", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert connectors.get("XAUUSD") is None or connectors["XAUUSD"].sent == []
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "DRY_RUN_PRIMARY"


def test_non_demo_limit_created_is_blocked(tmp_path):
    connectors = {}
    service = _service(
        tmp_path, enabled=True, mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping, demo=False)),
        risk_manager=_FakeRiskManager(),
    )
    zone = _zone("XAU-1M-B-0917-REAL", "DEMAND", "XAUUSD", "M1", 100, 102)
    assert not service.start()[0]
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert connectors["XAUUSD"].sent == []
    assert service.contexts["XAUUSD-M1"].last_execution_error == "RUNTIME_NOT_STARTED"


def test_runtime_control_reasons_block_order_send(tmp_path):
    for reason in ("KILL_SWITCH_ACTIVE", "EXECUTION_PAUSED", "AUTO_EXECUTION_DISABLED", "SIGNAL_GENERATION_DISABLED"):
        connectors = {}
        service = _service(
            tmp_path / reason, enabled=True, mode="LIVE",
            connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
            risk_manager=_FakeRiskManager(),
        )
        zone = _zone(f"XAU-1M-B-0917-{reason}", "DEMAND", "XAUUSD", "M1", 100, 102)
        service.started = True
        service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=False, execution_block_reason=reason)
        assert connectors.get("XAUUSD") is None or connectors["XAUUSD"].sent == []
        assert service.contexts["XAUUSD-M1"].last_execution_error == reason


def test_started_status_comes_only_from_fresh_operational_heartbeat(tmp_path):
    writer = _service(tmp_path, enabled=True, mode="LIVE")
    writer.repository.upsert_runtime_state(
        {"started": 1, "pid": 123, "execution_mode": "LIVE", "startup_block_reason": None,
         "heartbeat": "2026-09-17T08:00:00+00:00", "updated_at": "2026-09-17T08:00:00+00:00"}
    )
    reader = _service(tmp_path, enabled=True, mode="LIVE")
    assert reader.status_snapshot()["started"] is False
    now = datetime.now(timezone.utc).isoformat()
    writer.repository.upsert_runtime_state(
        {"started": 1, "pid": 123, "execution_mode": "LIVE", "startup_block_reason": None,
         "heartbeat": now, "updated_at": now}
    )
    assert reader.status_snapshot()["started"] is True


def test_execution_failure_is_visible_in_status_and_structured_logs(tmp_path):
    class RejectingConnector(_FakeConnector):
        def precheck_order(self, request):
            return {"ok": False, "reason_code": "invalid_stops", "payload": {"retcode": 10016}}

    connectors = {}
    logger = _CaptureLogger()
    service = LebPrimeLiveService(
        _config(enabled=True, mode="LIVE"), tmp_path / "live.db", logger=logger,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, RejectingConnector(mapping)),
        risk_manager=_FakeRiskManager(), risk_state={},
    )
    zone = _zone("XAU-1M-B-0917-FAIL", "DEMAND", "XAUUSD", "M1", 100, 102)
    assert service.start()[0]
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    status = {row["context_key"]: row for row in service.status_snapshot()["contexts"]}["XAUUSD-M1"]
    assert status["last_error"] == "invalid_stops"
    categories = [category for category, _, _ in logger.events]
    assert "lebprime_pending_order_check" in categories
    assert "lebprime_pending_order_failed" in categories
    assert connectors["XAUUSD"].sent == []


def test_realtime_sell_limit_created_places_sell_limits(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    zone = _zone("XAU-1M-S-0917-SELL", "SUPPLY", "XAUUSD", "M1", 120, 122)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(connectors["XAUUSD"].sent) == 2
    assert [request["type"] for request in connectors["XAUUSD"].sent] == [3, 3]


def test_duplicate_limit_created_sends_each_entry_once(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0917-DUP", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(connectors["XAUUSD"].sent) == 2
    assert len(service.repository.broker_entries_for_zone(zone.engine_zone_id)) == 2


def test_historical_overlap_does_not_suppress_new_realtime_zone(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    historical = _zone("XAU-1M-B-0917-HISTORY", "DEMAND", "XAUUSD", "M1", 100, 102)
    realtime = _zone("XAU-5M-B-0921-NEW", "DEMAND", "XAUUSD", "M5", 101, 103)
    service._register_limit(service.contexts["XAUUSD-M1"], historical, _limit_event(historical), execute=False, historical=True, execution_block_reason="HISTORICAL_REPLAY")
    service._register_limit(service.contexts["XAUUSD-M5"], realtime, _limit_event(realtime), execute=True)
    assert len(connectors["XAUUSD"].sent) == 2
    assert service.repository.zone(realtime.engine_zone_id)["execution_status"] == "BROKER_PENDING"


def test_duplicate_check_logs_exact_idempotency_decision(tmp_path):
    connectors = {}
    logger = _CaptureLogger()
    service = LebPrimeLiveService(_config(enabled=True, mode="LIVE"), tmp_path / "live.db", logger=logger, connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager(), risk_state={})
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-IDEMP", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    checks = [payload for category, payload, _ in logger.events if category == "lebprime_duplicate_check"]
    assert any(row["decision"] == "PASS" and row["matched_existing_key"] is None for row in checks)
    assert any(row["decision"] == "SUPPRESS" and row["matched_ticket"] in {"1001", "1002"} for row in checks)


def test_restart_reconciliation_recovers_pending_ticket_without_resend(tmp_path):
    connectors = {}
    factory = lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping))
    first = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=factory, risk_manager=_FakeRiskManager())
    assert first.start()[0]
    zone = _zone("XAU-1M-B-0917-RESTART", "DEMAND", "XAUUSD", "M1", 100, 102)
    first._register_limit(first.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    entry = first.repository.broker_entry(zone.engine_zone_id, 1)
    entry.update({"broker_status": "PLACING", "mt5_order_ticket": None})
    first.repository.upsert_broker_entry(entry)
    second = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=factory, risk_manager=_FakeRiskManager())
    assert second.start()[0]
    second._reconcile_broker_execution(datetime.now(timezone.utc))
    recovered = second.repository.broker_entry(zone.engine_zone_id, 1)
    assert recovered["broker_status"] == "PENDING"
    assert recovered["mt5_order_ticket"] == "1001"
    assert len(connectors["XAUUSD"].sent) == 2


def test_broker_entry1_and_entry2_fills_are_reconciled_independently(tmp_path):
    connectors = {}
    logger = _CaptureLogger()
    service = LebPrimeLiveService(_config(enabled=True, mode="LIVE"), tmp_path / "live.db", logger=logger, connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager(), risk_state={})
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0917-FILLS", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    connector = connectors["XAUUSD"]
    connector.fill(1001, deal_ticket=2001, position_ticket=3001, price=102.0)
    service._reconcile_broker_execution(datetime.now(timezone.utc))
    assert service.repository.broker_entry(zone.engine_zone_id, 1)["mt5_deal_ticket"] == "2001"
    assert service.repository.broker_entry(zone.engine_zone_id, 2)["broker_status"] == "PENDING"
    connector.fill(1002, deal_ticket=2002, position_ticket=3002, price=100.0)
    service._reconcile_broker_execution(datetime.now(timezone.utc))
    assert service.repository.broker_entry(zone.engine_zone_id, 2)["mt5_position_ticket"] == "3002"
    assert service.contexts["XAUUSD-M1"].realtime_activations == 2
    assert [category for category, _, _ in logger.events].count("lebprime_pending_order_filled") == 2


def test_cancel_and_expire_remove_only_unfilled_pending_entries(tmp_path):
    for event_type in ("CANCELED", "EXPIRED"):
        connectors = {}
        service = _service(tmp_path / event_type, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
        assert service.start()[0]
        zone = _zone(f"XAU-1M-B-0917-{event_type}", "DEMAND", "XAUUSD", "M1", 100, 102)
        service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
        service._track_lifecycle(service.contexts["XAUUSD-M1"], zone, ZoneEvent(zone.engine_zone_id, 7, "2026-09-17T08:07:00+00:00", event_type), execute=True)
        assert connectors["XAUUSD"].canceled == ["1001", "1002"]
        assert all(row["broker_status"] == "CANCELLED" for row in service.repository.broker_entries_for_zone(zone.engine_zone_id))


def test_cancel_never_removes_filled_entry1_and_tp1_cancels_entry2(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0917-TP1", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    connector = connectors["XAUUSD"]
    connector.fill(1001, deal_ticket=2001, position_ticket=3001, price=102.0)
    service._reconcile_broker_execution(datetime.now(timezone.utc))
    service._track_lifecycle(service.contexts["XAUUSD-M1"], zone, ZoneEvent(zone.engine_zone_id, 8, "2026-09-17T08:08:00+00:00", "TP1_HIT", price=zone.tp1_price), execute=True)
    assert connector.canceled == ["1002"]
    assert service.repository.broker_entry(zone.engine_zone_id, 1)["broker_status"] == "FILLED"
    assert service.repository.broker_entry(zone.engine_zone_id, 2)["broker_status"] == "CANCELLED"


def test_already_crossed_entry_is_not_chased_with_market_order(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    connector = connectors["XAUUSD"]
    connector.tick = SimpleNamespace(ask=101.0, bid=100.8)
    zone = _zone("XAU-1M-B-0917-CROSSED", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(connector.sent) == 1
    assert connector.sent[0]["price"] == zone.entry2
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "PARTIAL_PENDING_FAILED"
    assert service.repository.broker_entry(zone.engine_zone_id, 1)["failure_reason"] == "ENTRY_ALREADY_CROSSED"


def test_connector_pending_request_attaches_normalized_sl_and_tp():
    connector = SimpleNamespace(
        symbol="XAUUSD",
        mt5_config={"deviation": 10, "magic_number": 234000},
        get_symbol_spec=lambda: {"digits": 2, "trade_tick_size": 0.01, "point": 0.01, "stops_level": 10, "volume_min": 0.01},
        normalize_volume=lambda volume, spec: volume,
        pending_order_type=lambda direction: 2 if direction == "LONG" else 3,
        choose_filling_mode=lambda value: 1,
    )
    request, reason, normalized = MT5Connector.build_pending_limit_request(
        connector, direction="LONG", volume=0.2, entry_price=102.003, sl=98.997,
        tp=108.004, comment="LP340-ABC-ENTRY1",
    )
    assert reason == ""
    assert request["action"] == mt5_connector_module.mt5.TRADE_ACTION_PENDING
    assert request["price"] == 102.0
    assert request["sl"] == normalized["sl"] == 99.0
    assert request["tp"] == normalized["tp"] == 108.0


def test_spread_calculation_uses_mt5_points_for_all_broker_symbols():
    cases = {
        "XAUUSD": (4348.83, 4349.07, 0.01, 24.0),
        "US30": (52080.65, 52081.85, 0.01, 120.0),
        "USTEC": (29958.53, 29959.33, 0.01, 80.0),
    }
    for symbol, (bid, ask, point, expected) in cases.items():
        tick = SimpleNamespace(bid=bid, ask=ask)
        connector = SimpleNamespace(
            get_tick=lambda tick=tick: tick,
            get_symbol_spec=lambda point=point: {"point": point},
            _tick_snapshot=lambda value: {"bid": value.bid, "ask": value.ask},
            _stage_result=lambda stage, ok, reason, message, payload: {"stage": stage, "ok": ok, "reason_code": reason, "human_reason": message, "payload": payload},
        )
        result = MT5Connector.spread_check_stage(connector, expected, tick=tick)
        assert result["ok"], symbol
        assert result["payload"]["spread_points"] == expected


def test_per_symbol_spread_thresholds_and_global_fallback(tmp_path):
    config = _config(enabled=True, mode="LIVE")
    config["lebprime_v3_4_0"]["max_spread_points_by_symbol"] = {"XAUUSD": 25, "US30": 120, "NAS100": 80}
    service = LebPrimeLiveService(config, tmp_path / "live.db")
    assert service._spread_limit("XAUUSD", "XAUUSD") == 25
    assert service._spread_limit("US30", "US30") == 120
    assert service._spread_limit("NAS100", "USTEC") == 80
    del config["lebprime_v3_4_0"]["max_spread_points_by_symbol"]
    fallback = LebPrimeLiveService(config, tmp_path / "fallback.db")
    assert fallback._spread_limit("US30", "US30") == config["lebprime_v3_4_0"]["max_spread_points"]


def test_wide_spread_does_not_block_lebprime_pending_placement(tmp_path):
    connectors = {}
    risk = _FakeRiskManager()
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=risk)
    assert service.start()[0]
    connectors["XAUUSD"].tick = SimpleNamespace(bid=109.0, ask=110.0)
    zone = _zone("XAU-1M-B-0921-SPREAD", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(connectors["XAUUSD"].sent) == 2
    assert risk.adjustments == [0.5, 0.5]
    assert connectors["XAUUSD"].spread_check_calls == 0
    assert service.repository.zone(zone.engine_zone_id)["execution_status"] == "BROKER_PENDING"


def test_realtime_limit_created_sends_native_telegram_and_mt5_independently(tmp_path):
    connectors = {}
    notifier = _FakeNotifier()
    logger = _CaptureLogger()
    service = _service(
        tmp_path,
        enabled=True,
        mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
        notifier=notifier,
        logger=logger,
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-TG01", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(notifier.created) == 1
    assert notifier.created[0] == {
        "symbol": "XAUUSD",
        "timeframe": "M1",
        "order_type": "BUY LIMIT",
        "entry1": zone.entry1,
        "entry2": zone.entry2,
        "sl": zone.sl_price,
        "tp1": zone.tp1_price,
        "tp2": zone.tp2_price,
        "zone_id": zone.engine_zone_id,
    }
    assert len(connectors["XAUUSD"].sent) == 2
    assert len(notifier.accepted) == 2
    categories = [category for category, _, _ in logger.events]
    for expected in ("lebprime_limit_created", "lebprime_telegram_created_sent", "lebprime_pending_order_attempt", "lebprime_pending_order_check", "lebprime_pending_order_placed"):
        assert expected in categories
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(notifier.created) == 1
    assert len(connectors["XAUUSD"].sent) == 2


def test_telegram_created_deduplication_survives_restart(tmp_path):
    connectors = {}
    notifier = _FakeNotifier()
    factory = lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping))
    first = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=factory, risk_manager=_FakeRiskManager(), notifier=notifier)
    assert first.start()[0]
    zone = _zone("XAU-1M-B-0921-TGRESTART", "DEMAND", "XAUUSD", "M1", 100, 102)
    first._register_limit(first.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    second = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=factory, risk_manager=_FakeRiskManager(), notifier=notifier)
    assert second.start()[0]
    second._register_limit(second.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(notifier.created) == 1
    assert len(connectors["XAUUSD"].sent) == 2


def test_signal_alert_setting_disables_created_delivery_without_affecting_mt5(tmp_path):
    connectors = {}
    notifier = _FakeNotifier()
    config = _config(enabled=True, mode="LIVE")
    config["telegram"]["signal_alerts"] = False
    service = LebPrimeLiveService(
        config, tmp_path / "live.db", notifier=notifier,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(), risk_state={},
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-TGOFF", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert notifier.created == []
    assert len(connectors["XAUUSD"].sent) == 2


def test_telegram_created_message_contains_two_entry_zone_fields():
    notifier = TelegramNotifier("token", "chat", enabled=True)
    captured = {}
    notifier._send = lambda title, lines: captured.update({"title": title, "lines": lines}) or True
    assert notifier.send_lebprime_zone_created(
        {
            "symbol": "XAUUSD", "timeframe": "M3", "order_type": "SELL LIMIT",
            "entry1": 4352.4, "entry2": 4353.83, "sl": 4355.89,
            "tp1": 4349.0, "tp2": 4345.42, "zone_id": "XAU-3M-S-0921-LEB09",
        }
    )
    message = "\n".join(captured["lines"])
    assert captured["title"] == "LEBPRIME CREATED"
    for expected in ("XAUUSD", "M3", "SELL LIMIT", "4352.40000", "4353.83000", "4355.89000", "4349.00000", "4345.42000", "XAU-3M-S-0921-LEB09", "Status: CREATED"):
        assert expected in message


def test_telegram_failure_does_not_prevent_pending_orders(tmp_path):
    connectors = {}
    notifier = _FakeNotifier(created_ok=False)
    logger = _CaptureLogger()
    service = _service(
        tmp_path, enabled=True, mode="LIVE", notifier=notifier, logger=logger,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-TGFAIL", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(notifier.created) == 1
    assert len(connectors["XAUUSD"].sent) == 2
    assert "lebprime_telegram_created_failed" in [category for category, _, _ in logger.events]


def test_mt5_failure_does_not_prevent_telegram_created(tmp_path):
    class PermanentRejectConnector(_FakeConnector):
        def precheck_order(self, request):
            return {"ok": False, "reason_code": "invalid_stops", "payload": {"retcode": 10016, "comment": "Invalid stops"}}

    connectors = {}
    notifier = _FakeNotifier()
    service = _service(
        tmp_path, enabled=True, mode="LIVE", notifier=notifier,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, PermanentRejectConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-MT5FAIL", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(notifier.created) == 1
    assert connectors["XAUUSD"].sent == []
    assert len(notifier.failed) == 2
    assert all(row["broker_status"] == "PERMANENT_FAILURE" for row in service.repository.broker_entries_for_zone(zone.engine_zone_id))


def test_telegram_notifier_cannot_initiate_an_order(tmp_path):
    connectors = {}
    notifier = _FakeNotifier()
    service = _service(
        tmp_path, enabled=True, mode="LIVE", notifier=notifier,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    notifier.send_lebprime_zone_created({"zone_id": "NOT-A-STRATEGY-EVENT"})
    assert connectors == {}
    assert service.repository.broker_entries_for_zone("NOT-A-STRATEGY-EVENT") == []


def test_historical_limit_created_sends_neither_telegram_nor_mt5(tmp_path):
    connectors = {}
    notifier = _FakeNotifier()
    service = _service(
        tmp_path, enabled=True, mode="LIVE", notifier=notifier,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    zone = _zone("XAU-1M-B-0921-HISTTG", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=False, historical=True, execution_block_reason="HISTORICAL_REPLAY")
    assert notifier.created == []
    assert connectors == {}


def test_transient_unplaced_entries_retry_and_store_tickets(tmp_path):
    class TransientConnector(_FakeConnector):
        def __init__(self, mapping):
            super().__init__(mapping)
            self.transient_failure = True

        def precheck_order(self, request):
            self.calls.append(("check", request["comment"]))
            if self.transient_failure:
                return {"ok": False, "reason_code": "order_check_none", "payload": {"retcode": None, "comment": "terminal busy"}}
            return {"ok": True, "payload": {"retcode": 0, "comment": "Done"}}

    connectors = {}
    logger = _CaptureLogger()
    service = _service(
        tmp_path, enabled=True, mode="LIVE", logger=logger,
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, TransientConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-RETRY", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    rows = service.repository.broker_entries_for_zone(zone.engine_zone_id)
    assert all(row["broker_status"] == "RETRY_SCHEDULED" and row["retryable"] == 1 and row["mt5_order_ticket"] is None for row in rows)
    categories = [category for category, _, _ in logger.events]
    assert "lebprime_pending_order_failed" in categories
    assert "lebprime_pending_order_retry_scheduled" in categories
    checks_before = len(connectors["XAUUSD"].calls)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(connectors["XAUUSD"].calls) == checks_before
    connectors["XAUUSD"].transient_failure = False
    for row in rows:
        row["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        service.repository.upsert_broker_entry(row)
    service._retry_due_placements(datetime.now(timezone.utc))
    retried = service.repository.broker_entries_for_zone(zone.engine_zone_id)
    assert [row["mt5_order_ticket"] for row in retried] == ["1001", "1002"]
    assert all(row["attempt_count"] == 2 and row["retryable"] == 0 for row in retried)


def test_permanent_invalid_entries_do_not_retry_forever(tmp_path):
    class PermanentConnector(_FakeConnector):
        def precheck_order(self, request):
            self.calls.append(("check", request["comment"]))
            return {"ok": False, "reason_code": "invalid_stops", "payload": {"retcode": 10016, "comment": "Invalid stops"}}

    connectors = {}
    service = _service(
        tmp_path, enabled=True, mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, PermanentConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-PERM", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    checks = len(connectors["XAUUSD"].calls)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    service._retry_due_placements(datetime.now(timezone.utc) + timedelta(days=1))
    assert len(connectors["XAUUSD"].calls) == checks
    assert all(row["retryable"] == 0 and row["broker_status"] == "PERMANENT_FAILURE" for row in service.repository.broker_entries_for_zone(zone.engine_zone_id))


def test_volume_and_normalized_price_are_persisted_before_order_check(tmp_path):
    connectors = {}
    service = _service(tmp_path, enabled=True, mode="LIVE", connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)), risk_manager=_FakeRiskManager())
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-PERSIST", "DEMAND", "XAUUSD", "M1", 100, 102)
    observed = []
    connectors["XAUUSD"].on_precheck = lambda request: observed.append(service.repository.broker_entry(zone.engine_zone_id, 1 if request["price"] == zone.entry1 else 2))
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert len(observed) == 2
    assert all(row["volume"] == 0.2 and row["normalized_price"] is not None for row in observed)
    assert [row["mt5_order_ticket"] for row in service.repository.broker_entries_for_zone(zone.engine_zone_id)] == ["1001", "1002"]


def test_genuine_margin_failure_never_reaches_order_check_or_send(tmp_path):
    class MarginFailureRisk(_FakeRiskManager):
        def fit_volume_to_margin(self, requested_volume, account, symbol_spec, callback, **kwargs):
            return {
                "valid": False,
                "volume": 0.0,
                "requested_volume": requested_volume,
                "requested_margin": 500.0,
                "required_margin": 500.0,
                "projected_margin": 500.0,
                "projected_margin_level": 2000.0,
                "free_margin_after": -400.0,
                "effective_minimum_volume": 0.05,
                "min_margin_level": 300.0,
                "min_free_margin_buffer": 0.0,
                "min_fitted_volume_ratio": 0.0,
                "fit_candidates": [{
                    "candidate_volume": requested_volume,
                    "order_calc_margin_result": 500.0,
                    "projected_margin": 500.0,
                    "projected_margin_level": 2000.0,
                    "free_margin_after": -400.0,
                    "accepted": False,
                    "rejection_reason": "MARGIN_FREE_INSUFFICIENT",
                }],
                "reason": "Free margin is insufficient",
                "reason_code": "MARGIN_FREE_INSUFFICIENT",
            }

    connectors = {}
    service = _service(
        tmp_path,
        enabled=True,
        mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=MarginFailureRisk(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-MARGINFAIL", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    assert connectors["XAUUSD"].calls == []
    assert connectors["XAUUSD"].sent == []
    rows = service.repository.broker_entries_for_zone(zone.engine_zone_id)
    assert all(row["last_failure"] == "MARGIN_FREE_INSUFFICIENT" for row in rows)
    assert all(row["retryable"] == 1 for row in rows)
    assert all(row["requested_volume"] == 0.2 and row["minimum_volume"] == 0.05 for row in rows)


def test_successful_margin_reaches_order_check_before_order_send(tmp_path):
    connectors = {}
    service = _service(
        tmp_path,
        enabled=True,
        mode="LIVE",
        connector_factory=lambda mapping: connectors.setdefault(mapping.logical_symbol, _FakeConnector(mapping)),
        risk_manager=_FakeRiskManager(),
    )
    assert service.start()[0]
    zone = _zone("XAU-1M-B-0921-MARGINPASS", "DEMAND", "XAUUSD", "M1", 100, 102)
    service._register_limit(service.contexts["XAUUSD-M1"], zone, _limit_event(zone), execute=True)
    calls = connectors["XAUUSD"].calls
    assert [name for name, _ in calls] == ["check", "send", "check", "send"]
    assert [row["mt5_order_ticket"] for row in service.repository.broker_entries_for_zone(zone.engine_zone_id)] == ["1001", "1002"]


def test_all_18_contexts_remain_healthy_on_successful_scan(tmp_path):
    service = _service(tmp_path, enabled=True, mode="DRY", connector_factory=lambda mapping: _FakeConnector(mapping), risk_manager=_FakeRiskManager())
    snapshot = service.scan_once(datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc))
    assert len(snapshot["contexts"]) == 18
    assert all(row["status"] == "HEALTHY" for row in snapshot["contexts"])


def test_legacy_primary_pending_rows_migrate_to_non_executable_history(tmp_path):
    service = _service(tmp_path)
    zone = _zone("XAU-1M-B-0917-LEGACY", "DEMAND", "XAUUSD", "M1", 100, 102)
    payload = service._zone_payload(service.contexts["XAUUSD-M1"], zone, zone.limit_created_time)
    payload.update({"family_id": "LEGACY", "execution_primary": 1, "execution_status": "PRIMARY_PENDING"})
    service.repository.upsert_zone(payload)
    reloaded = _service(tmp_path)
    assert reloaded.repository.zone(zone.engine_zone_id)["execution_status"] == "HISTORICAL_REPLAY"
    entries = reloaded.repository.broker_entries_for_zone(zone.engine_zone_id)
    assert [row["entry_number"] for row in entries] == [1, 2]
    assert all(row["historical"] == 1 and row["execution_eligible"] == 0 for row in entries)


def test_legacy_local_spread_rejection_migrates_to_blocked_precheck(tmp_path):
    service = _service(tmp_path)
    zone = _zone("US30-3M-B-0921-SPREAD", "DEMAND", "US30", "M3", 52000, 52020)
    context = service.contexts["US30-M3"]
    payload = service._zone_payload(context, zone, zone.limit_created_time)
    payload.update({"family_id": "SPREAD", "execution_primary": 1, "execution_status": "ORDER_REJECTED"})
    service.repository.upsert_zone(payload)
    service.repository.upsert_broker_entry(service._broker_entry_payload(context, zone, 1, float(zone.entry1), status="REJECTED", failure_reason="spread_too_wide"))
    reloaded = _service(tmp_path)
    assert reloaded.repository.zone(zone.engine_zone_id)["execution_status"] == "BLOCKED_PRECHECK"
    assert reloaded.repository.broker_entry(zone.engine_zone_id, 1)["broker_status"] == "BLOCKED_PRECHECK"


def test_engine_never_emits_sl_terminal_after_tp1():
    engine = LebPrimeEngine(symbol="XAUUSD", timeframe="M1")
    zone = _zone("XAU-1M-B-0917-LEB01", "DEMAND", "XAUUSD", "M1", 100, 102)
    zone.activated_bar_index = 5
    zone.activated_time = "2026-09-17T08:05:00+00:00"
    zone.tp1_hit_bar_index = 6
    zone.tp1_hit_time = "2026-09-17T08:06:00+00:00"
    engine._zones.append(zone)
    engine._bars.append(Candle(7, "2026-09-17T08:07:00+00:00", 95, 96, 89, 90))
    engine._manage_zones(engine._bars[-1])
    assert zone.terminal_state == "TP1_FINAL"
    assert engine.events()[-1].event_type == "TP1_FINAL"
    assert all(event.event_type != "SL_HIT" for event in engine.events())


def test_engine_tp1_then_tp2_lifecycle():
    engine = LebPrimeEngine(symbol="XAUUSD", timeframe="M1")
    zone = _zone("XAU-1M-B-0917-LEB01", "DEMAND", "XAUUSD", "M1", 100, 102)
    zone.activated_bar_index = 5
    zone.activated_time = "2026-09-17T08:05:00+00:00"
    engine._zones.append(zone)
    tp1_bar = Candle(6, "2026-09-17T08:06:00+00:00", 102, float(zone.tp1_price), 101.5, float(zone.tp1_price))
    engine._bars.append(tp1_bar)
    engine._manage_zones(tp1_bar)
    assert zone.tp1_hit_bar_index == 6
    assert zone.terminal_state is None
    tp2_bar = Candle(7, "2026-09-17T08:07:00+00:00", float(zone.tp1_price), float(zone.tp2_price), 103, float(zone.tp2_price))
    engine._bars.append(tp2_bar)
    engine._manage_zones(tp2_bar)
    assert zone.terminal_state == "TP2_HIT"
    assert [event.event_type for event in engine.events()] == ["TP1_HIT", "TP2_HIT"]


def test_strategy_runtime_controls_are_present_and_configured():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    strategy = config["lebprime_v3_4_0"]
    assert strategy["enabled"] is True
    assert strategy["execution_mode"] in {"DRY", "LIVE"}
    source = (root / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "lebprime_v3_4_0" in source
    assert "Live (demo only)" in source
    assert "Active zone families" in source
    assert "Duplicate suppressed" in source
    assert "/api/strategies/lebprime-v3-4-0/status" in source
