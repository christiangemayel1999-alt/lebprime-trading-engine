from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TradingBot
from services.database import DatabaseService
from services.trade_journal import TradeJournalService
from trading_bot.positions.live_lifecycle import LivePositionLifecycleManager


class _StubLogger:
    def log_signal_event(self, _row):
        return None

    def log_trade_event(self, _row):
        return None

    def structured(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def signal_console(self, *_args, **_kwargs):
        return None


class _ResolutionConnector:
    def __init__(self, resolutions: list[dict[str, object]]) -> None:
        self.resolutions = list(resolutions)

    def resolve_trade_close_history(self, *_args, **_kwargs):
        if not self.resolutions:
            raise AssertionError("No more resolution payloads queued")
        return self.resolutions.pop(0)

    def get_symbol_spec(self):
        return {"pip_size": 0.1, "pip_value_per_lot": 1.0, "point": 0.01}


def _trade_open_payload(ticket: str, ts: str = "2026-04-20T10:00:00+00:00") -> dict[str, object]:
    return {
        "timestamp": ts,
        "mode": "LIVE",
        "symbol": "XAUUSD",
        "side": "LONG",
        "ticket": ticket,
        "position_id": ticket,
        "trade_id": ticket,
        "entry_price": 100.0,
        "entry": 100.0,
        "volume": 1.0,
        "requested_volume": 1.0,
        "final_volume": 1.0,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "COMPRESSION_RELEASE",
        "setup_type": "COMPRESSION_RELEASE",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "entry_mode": "market",
        "comment": f"fp-{ticket}",
        "metadata": {"source": "test"},
    }


def _pending_close_payload(ticket: str, ts: str = "2026-04-20T10:05:00+00:00") -> dict[str, object]:
    return {
        **_trade_open_payload(ticket),
        "timestamp": ts,
        "closed_at": ts,
        "updated_at": ts,
        "status": "PENDING_CLOSE_RESOLUTION",
        "event_type": "TRADE_CLOSED",
        "close_reason": "momentum_failure_exit",
        "unresolved_reason": "momentum_failure_exit",
        "note": "momentum_failure_exit",
    }


def _resolved_history(
    *,
    price: float = 101.5,
    profit: float = 15.0,
    volume: float = 1.0,
    confidence: float = 95.0,
    order_id: str = "O-1",
    deal_id: str = "D-1",
    ts: str = "2026-04-20T10:06:00+00:00",
) -> dict[str, object]:
    deal = SimpleNamespace(
        position_id=123456,
        order=order_id,
        deal=deal_id,
        ticket=deal_id,
        symbol="XAUUSD",
        price=price,
        profit=profit,
        swap=0.0,
        commission=0.0,
        volume=volume,
        time=ts,
        comment="takeprofit",
    )
    return {
        "resolved": True,
        "status": "FINALIZED",
        "reason": "matched_close_history",
        "confidence": confidence,
        "orders": [],
        "deals": [deal],
        "close_deal": deal,
        "evidence": {
            "ticket": "123456",
            "matched_order_ids": [order_id],
            "matched_deal_ids": [deal_id],
            "matched_volume": volume,
            "close_price": price,
            "close_time": ts,
            "confidence": confidence,
            "stage": "FINALIZED",
            "orders": [],
            "deals": [{"deal": deal_id}],
        },
    }


def _pending_history(stage: str = "CLOSE_HISTORY_PENDING") -> dict[str, object]:
    return {
        "resolved": False,
        "status": stage,
        "reason": "history_unavailable" if stage == "CLOSE_HISTORY_PENDING" else "partial_history_match",
        "confidence": 35.0 if stage == "CLOSE_HISTORY_PARTIAL" else 0.0,
        "orders": [],
        "deals": [],
        "close_deal": None,
        "evidence": {
            "ticket": "123456",
            "matched_order_ids": [],
            "matched_deal_ids": [],
            "matched_volume": 0.0,
            "confidence": 35.0 if stage == "CLOSE_HISTORY_PARTIAL" else 0.0,
            "stage": stage,
            "orders": [],
            "deals": [],
        },
    }


def _journal(tmp_path: Path) -> tuple[DatabaseService, TradeJournalService]:
    db = DatabaseService(tmp_path / "bot.db")
    journal = TradeJournalService(tmp_path, db, _StubLogger(), {})
    return db, journal


def _build_reconciliation_bot(tmp_path: Path, resolutions: list[dict[str, object]]) -> tuple[TradingBot, DatabaseService, TradeJournalService]:
    db, journal = _journal(tmp_path)
    bot = TradingBot.__new__(TradingBot)
    bot.database = db
    bot.trade_journal = journal
    bot.logger = _StubLogger()
    bot.connector = _ResolutionConnector(resolutions)
    bot.state = {}
    bot.config = {
        "mt5": {"symbol": "XAUUSD", "magic_number": 7},
        "exit": {"trade_history_resolve_lookback_minutes": 1440},
        "execution": {"pending_fill_entry_tolerance_points": 25.0},
        "journaling": {
            "unresolved_trade_retry_interval_seconds": 1,
            "unresolved_trade_max_retries": 3,
            "unresolved_trade_min_age_before_fail_seconds": 0,
            "unresolved_trade_max_retry_window_seconds": 3600,
        },
    }
    bot._record_event = lambda *_args, **_kwargs: None
    return bot, db, journal


def _build_replay_guard_bot() -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.state = {}
    bot.connector = SimpleNamespace(get_symbol_spec=lambda: {"point": 0.01})
    bot.config = {
        "mt5": {"symbol": "XAUUSD"},
        "execution": {
            "blocked_setup_replay_suppression_seconds": 45,
            "blocked_setup_replay_price_delta_points": 15.0,
            "blocked_setup_replay_score_delta": 4.0,
            "blocked_setup_replay_spread_improvement_points": 2.0,
        },
    }
    return bot


def test_delayed_mt5_history_availability_transitions_to_finalized(tmp_path: Path) -> None:
    bot, db, journal = _build_reconciliation_bot(tmp_path, [_pending_history(), _resolved_history()])
    journal.record_trade_open(_trade_open_payload("123456"))
    journal.record_trade_close(_pending_close_payload("123456"))

    first = TradingBot._finalize_trade_from_history_row(bot, db.get_trade_by_identity(trade_id="123456"), source="test")
    row_after_first = db.get_trade_by_identity(trade_id="123456")
    second = TradingBot._finalize_trade_from_history_row(bot, row_after_first, source="test")
    row_after_second = db.get_trade_by_identity(trade_id="123456")

    assert first["ok"] is False
    assert row_after_first["status"] == "CLOSE_HISTORY_PENDING"
    assert second["ok"] is True
    assert row_after_second["status"] == "FINALIZED"
    assert float(row_after_second["exit_price"]) == 101.5
    assert row_after_second["matched_deal_ids"] == "D-1"


def test_deal_history_without_order_history_still_finalizes(tmp_path: Path) -> None:
    bot, db, journal = _build_reconciliation_bot(tmp_path, [_resolved_history(order_id="", deal_id="DEAL_ONLY")])
    journal.record_trade_open(_trade_open_payload("123456"))
    journal.record_trade_close(_pending_close_payload("123456"))

    result = TradingBot._finalize_trade_from_history_row(bot, db.get_trade_by_identity(trade_id="123456"), source="test")
    row = db.get_trade_by_identity(trade_id="123456")

    assert result["ok"] is True
    assert row["status"] == "FINALIZED"
    assert row["matched_deal_ids"] == "DEAL_ONLY"


def test_repeated_close_reconciliation_stage_does_not_duplicate_trade_events(tmp_path: Path) -> None:
    bot, db, journal = _build_reconciliation_bot(tmp_path, [_pending_history(), _pending_history()])
    journal.record_trade_open(_trade_open_payload("123456"))
    journal.record_trade_close(_pending_close_payload("123456"))

    TradingBot._finalize_trade_from_history_row(bot, db.get_trade_by_identity(trade_id="123456"), source="test")
    TradingBot._finalize_trade_from_history_row(bot, db.get_trade_by_identity(trade_id="123456"), source="test")

    rows = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND status = 'CLOSE_HISTORY_PENDING';",
        ("123456",),
    )
    assert len(rows) == 1


def test_duplicate_trade_open_event_is_idempotent(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    payload = _trade_open_payload("T-OPEN")

    journal.record_trade_open(payload)
    journal.record_trade_open(payload)
    journal.record_trade_open(payload)

    rows = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND event_type = 'TRADE_OPENED';",
        ("T-OPEN",),
    )
    assert len(rows) == 1


def test_duplicate_trade_close_event_is_idempotent(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T-CLOSE"))
    payload = {
        **_pending_close_payload("T-CLOSE"),
        "status": "FINALIZED",
        "event_type": "TRADE_FINALIZED",
        "exit_price": 102.0,
        "pnl": 2.0,
        "realized_r": 1.0,
    }

    journal.record_trade_close(payload)
    journal.record_trade_close(payload)

    rows = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND event_type = 'TRADE_FINALIZED';",
        ("T-CLOSE",),
    )
    assert len(rows) == 1


def test_blocked_setup_replay_stage_suppresses_same_blocked_fingerprint() -> None:
    bot = _build_replay_guard_bot()
    now = TradingBot._close_resolution_policy.__globals__["utc_now"]()
    TradingBot._update_setup_registry(
        bot,
        "fp-1",
        "blocked",
        now,
        {
            "last_decision": "blocked",
            "last_block_reason": "blocked_chasing_entry",
            "last_blocked_at": now.isoformat(),
            "last_anchor_time": "2026-04-20T10:00:00+00:00",
            "last_entry_price": 100.0,
            "last_entry_score": 60.0,
            "last_spread_points": 18.0,
        },
    )

    stage = TradingBot._blocked_setup_replay_stage(
        bot,
        {"setup_fingerprint": "fp-1", "anchor_time": "2026-04-20T10:00:00+00:00"},
        {"entry_price": 100.01, "entry_score": 60.5},
        {"spread_points": 17.5},
        "blocked_chasing_entry",
        now,
    )

    assert stage["suppress"] is True


def test_blocked_setup_replay_stage_allows_new_anchor_time() -> None:
    bot = _build_replay_guard_bot()
    now = TradingBot._close_resolution_policy.__globals__["utc_now"]()
    TradingBot._update_setup_registry(
        bot,
        "fp-1",
        "blocked",
        now,
        {
            "last_decision": "blocked",
            "last_block_reason": "blocked_chasing_entry",
            "last_blocked_at": now.isoformat(),
            "last_anchor_time": "2026-04-20T10:00:00+00:00",
            "last_entry_price": 100.0,
            "last_entry_score": 60.0,
            "last_spread_points": 18.0,
        },
    )

    stage = TradingBot._blocked_setup_replay_stage(
        bot,
        {"setup_fingerprint": "fp-1", "anchor_time": "2026-04-20T10:01:00+00:00"},
        {"entry_price": 100.01, "entry_score": 60.5},
        {"spread_points": 17.5},
        "blocked_chasing_entry",
        now,
    )

    assert stage["suppress"] is False


def test_blocked_setup_replay_stage_allows_material_price_improvement() -> None:
    bot = _build_replay_guard_bot()
    now = TradingBot._close_resolution_policy.__globals__["utc_now"]()
    TradingBot._update_setup_registry(
        bot,
        "fp-1",
        "blocked",
        now,
        {
            "last_decision": "blocked",
            "last_block_reason": "blocked_chasing_entry",
            "last_blocked_at": now.isoformat(),
            "last_anchor_time": "2026-04-20T10:00:00+00:00",
            "last_entry_price": 100.0,
            "last_entry_score": 60.0,
            "last_spread_points": 18.0,
        },
    )

    stage = TradingBot._blocked_setup_replay_stage(
        bot,
        {"setup_fingerprint": "fp-1", "anchor_time": "2026-04-20T10:00:00+00:00"},
        {"entry_price": 100.40, "entry_score": 60.5},
        {"spread_points": 17.5},
        "blocked_chasing_entry",
        now,
    )

    assert stage["suppress"] is False


def test_pending_fill_match_prefers_high_confidence_position() -> None:
    manager = LivePositionLifecycleManager(
        config={"mt5": {"symbol": "XAUUSD", "magic_number": 7}, "execution": {"pending_fill_entry_tolerance_points": 25.0}},
        state={},
        connector=SimpleNamespace(get_symbol_spec=lambda: {"point": 0.01}),
        execution_service=SimpleNamespace(),
        risk_manager=SimpleNamespace(),
        trade_journal=SimpleNamespace(),
        logger=_StubLogger(),
    )
    pending = {
        "order_id": "9001",
        "symbol": "XAUUSD",
        "direction": "LONG",
        "volume": 1.0,
        "entry_price": 100.0,
        "submitted_at": "2026-04-20T10:00:00+00:00",
    }
    positions = [
        SimpleNamespace(ticket="P1", identifier="9001", symbol="XAUUSD", type=0, volume=1.0, price_open=100.02, time="2026-04-20T10:00:30+00:00", magic=7),
        SimpleNamespace(ticket="P2", identifier="X", symbol="XAUUSD", type=0, volume=1.0, price_open=101.50, time="2026-04-20T10:20:00+00:00", magic=7),
    ]

    match = manager._match_pending_fill(pending, positions, SimpleNamespace())

    assert match["status"] == "matched"
    assert float(match["confidence"]) >= 55.0


def test_pending_fill_match_marks_ambiguous_same_symbol_positions() -> None:
    manager = LivePositionLifecycleManager(
        config={"mt5": {"symbol": "XAUUSD", "magic_number": 7}, "execution": {"pending_fill_entry_tolerance_points": 25.0}},
        state={},
        connector=SimpleNamespace(get_symbol_spec=lambda: {"point": 0.01}),
        execution_service=SimpleNamespace(),
        risk_manager=SimpleNamespace(),
        trade_journal=SimpleNamespace(),
        logger=_StubLogger(),
    )
    pending = {
        "order_id": "",
        "symbol": "XAUUSD",
        "direction": "LONG",
        "volume": 1.0,
        "entry_price": 100.0,
        "submitted_at": "2026-04-20T10:00:00+00:00",
    }
    positions = [
        SimpleNamespace(ticket="P1", identifier="", symbol="XAUUSD", type=0, volume=1.0, price_open=100.02, time="2026-04-20T10:00:30+00:00", magic=7),
        SimpleNamespace(ticket="P2", identifier="", symbol="XAUUSD", type=0, volume=1.0, price_open=100.01, time="2026-04-20T10:00:40+00:00", magic=7),
    ]

    match = manager._match_pending_fill(pending, positions, SimpleNamespace())

    assert match["status"] == "ambiguous"


def test_partial_fill_match_still_links_when_other_fields_align() -> None:
    manager = LivePositionLifecycleManager(
        config={"mt5": {"symbol": "XAUUSD", "magic_number": 7}, "execution": {"pending_fill_entry_tolerance_points": 25.0}},
        state={},
        connector=SimpleNamespace(get_symbol_spec=lambda: {"point": 0.01}),
        execution_service=SimpleNamespace(),
        risk_manager=SimpleNamespace(),
        trade_journal=SimpleNamespace(),
        logger=_StubLogger(),
    )
    pending = {
        "order_id": "9001",
        "symbol": "XAUUSD",
        "direction": "LONG",
        "volume": 1.0,
        "entry_price": 100.0,
        "submitted_at": "2026-04-20T10:00:00+00:00",
    }
    positions = [
        SimpleNamespace(ticket="P1", identifier="9001", symbol="XAUUSD", type=0, volume=0.5, price_open=100.02, time="2026-04-20T10:00:30+00:00", magic=7),
    ]

    match = manager._match_pending_fill(pending, positions, SimpleNamespace())

    assert match["status"] == "matched"
