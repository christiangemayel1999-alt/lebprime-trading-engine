from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from main import TradingBot
from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.trade_journal import TradeJournalService
from trading_bot.positions.live_lifecycle import LivePositionLifecycleManager
from utils import save_json_atomic, utc_now


class _StubLogger:
    def log_signal_event(self, _row):
        return None

    def log_trade_event(self, _row):
        return None

    def structured(self, *_args, **_kwargs):
        return None

    def signal_console(self, *_args, **_kwargs):
        return None


class _ResolutionConnector:
    def __init__(self, responses: list[dict[str, object]]):
        self.responses = list(responses)
        self._last = dict(responses[-1]) if responses else {
            "resolved": False,
            "status": "CLOSE_HISTORY_PENDING",
            "reason": "history_unavailable",
            "evidence": {"stage": "close_history_pending", "matched_order_ids": [], "matched_deal_ids": [], "confidence": 0.0},
            "orders": [],
            "deals": [],
            "close_deal": None,
        }

    def resolve_trade_close_history(self, *_args, **_kwargs):
        if self.responses:
            self._last = dict(self.responses.pop(0))
        return dict(self._last)

    @staticmethod
    def get_symbol_spec() -> dict[str, float]:
        return {
            "point": 0.01,
            "pip_size": 0.1,
            "pip_value_per_lot": 1.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }


class _LifecycleConnector(_ResolutionConnector):
    pass


class _ExecutionService:
    def __init__(self, open_positions: list[object] | None = None, pending_orders: list[object] | None = None):
        self.open_positions = list(open_positions or [])
        self.pending_orders = list(pending_orders or [])

    def get_open_positions(self):
        return list(self.open_positions)

    def get_pending_orders(self):
        return list(self.pending_orders)

    def cancel_order(self, *_args, **_kwargs):
        return {"ok": True}


class _RiskManager:
    @staticmethod
    def rebase_trade_levels(trade_plan, *_args, **_kwargs):
        return trade_plan

    @staticmethod
    def build_position_state(
        trade_plan,
        ticket,
        symbol,
        volume,
        candidate,
        entry_assessment,
        opened_at,
        mode,
        spread_points,
        requested_volume,
        slippage,
    ):
        return {
            "ticket": ticket,
            "position_id": ticket,
            "symbol": symbol,
            "direction": candidate.get("side", "LONG"),
            "setup_fingerprint": candidate.get("setup_fingerprint", "fp"),
            "setup_family": candidate.get("setup_family", "LEBPRIM_SCALP"),
            "regime_at_entry": candidate.get("regime_name", "TREND"),
            "session_at_entry": candidate.get("session_name", "LONDON"),
            "entry_mode": entry_assessment.get("entry_mode", "limit"),
            "entry_price": trade_plan.get("entry_price", entry_assessment.get("entry_price", 0.0)),
            "sl": trade_plan.get("stop_loss", 0.0),
            "tp2": trade_plan.get("tp2", 0.0),
            "volume": volume,
            "remaining_volume": volume,
            "opened_at": opened_at.isoformat(),
            "mode": mode,
            "spread_points": spread_points,
            "requested_volume": requested_volume,
            "slippage": slippage,
            "metadata": {},
        }


def _journal(tmp_path: Path) -> tuple[DatabaseService, TradeJournalService]:
    db = DatabaseService(tmp_path / "bot.db")
    journal = TradeJournalService(tmp_path, db, _StubLogger(), {})
    return db, journal


def _open_payload(ticket: str, ts: str = "2026-04-20T08:00:00+00:00") -> dict[str, object]:
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
        "volume": 0.5,
        "requested_volume": 0.5,
        "final_volume": 0.5,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "LEBPRIM_SCALP",
        "setup_type": "LEBPRIM_MOMENTUM",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "entry_mode": "lebprim_limit",
        "comment": f"fp-{ticket}",
        "created_at": ts,
        "metadata": {"source": "test"},
    }


def _pending_close_payload(ticket: str, ts: str = "2026-04-20T08:10:00+00:00") -> dict[str, object]:
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
        "volume": 0.5,
        "final_volume": 0.5,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "LEBPRIM_SCALP",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "close_reason": "history_unavailable",
        "status": "PENDING_CLOSE_RESOLUTION",
        "event_type": "TRADE_CLOSED",
        "closed_at": ts,
        "resolution_started_at": ts,
        "metadata": {"source": "test"},
    }


def _build_bot(
    db: DatabaseService,
    journal: TradeJournalService,
    connector: _ResolutionConnector,
) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.config = {
        "mt5": {"symbol": "XAUUSD", "magic_number": 4242},
        "execution": {
            "pending_fill_entry_tolerance_points": 25.0,
            "blocked_setup_replay_suppression_seconds": 45,
            "blocked_setup_replay_price_delta_points": 15.0,
            "blocked_setup_replay_score_delta": 4.0,
            "blocked_setup_replay_spread_improvement_points": 2.0,
        },
        "exit": {"trade_history_resolve_lookback_minutes": 1440},
        "journaling": {
            "unresolved_trade_retry_interval_seconds": 30,
            "unresolved_trade_max_retries": 5,
            "unresolved_trade_min_age_before_fail_seconds": 120,
            "unresolved_trade_max_retry_window_seconds": 600,
            "setup_registry_retention_hours": 72,
            "signal_registry_max_entries": 1500,
        },
    }
    bot.connector = connector
    bot.database = db
    bot.trade_journal = journal
    bot.state = {"setup_registry": {}, "open_position": None}
    bot.logger = _StubLogger()
    bot._record_event = lambda *_args, **_kwargs: None
    bot._journal_trade_close = lambda row: bot.trade_journal.record_trade_close(row)
    return bot


def test_duplicate_trade_open_events_are_suppressed(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)

    journal.record_trade_open(_open_payload("DUP-OPEN"))
    journal.record_trade_open(_open_payload("DUP-OPEN"))

    rows = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND event_type = 'TRADE_OPENED';",
        ("DUP-OPEN",),
    )
    assert len(rows) == 1
    assert db.get_maintenance_counter("trade_event_duplicate_suppressed") == 1


def test_duplicate_trade_close_events_are_suppressed(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    payload = {
        **_pending_close_payload("DUP-CLOSE", ts="2026-04-20T08:12:00+00:00"),
        "status": "FINALIZED",
        "event_type": "TRADE_FINALIZED",
        "exit_price": 101.5,
        "pnl": 1.5,
        "realized_r": 0.75,
        "close_reason": "take_profit",
    }

    journal.record_trade_close(payload)
    journal.record_trade_close(payload)

    rows = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND event_type = 'TRADE_FINALIZED';",
        ("DUP-CLOSE",),
    )
    assert len(rows) == 1
    assert db.get_maintenance_counter("trade_event_duplicate_suppressed") == 1


def test_close_reconciliation_progresses_from_pending_to_finalized(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_open_payload("RCN-1"))
    journal.record_trade_close(_pending_close_payload("RCN-1"))
    close_deal = SimpleNamespace(
        deal="DEAL-RCN-1",
        position_id="RCN-1",
        ticket="DEAL-RCN-1",
        price=101.5,
        volume=0.5,
        profit=1.5,
        swap=0.0,
        commission=0.0,
        time=utc_now(),
        symbol="XAUUSD",
        comment="takeprofit",
    )
    connector = _ResolutionConnector(
        [
            {
                "resolved": False,
                "status": "CLOSE_HISTORY_PENDING",
                "reason": "history_unavailable",
                "orders": [],
                "deals": [],
                "close_deal": None,
                "evidence": {"stage": "close_history_pending", "matched_order_ids": [], "matched_deal_ids": [], "confidence": 0.0},
            },
            {
                "resolved": True,
                "status": "FINALIZED",
                "reason": "matched_close_history",
                "orders": [],
                "deals": [close_deal],
                "close_deal": close_deal,
                "evidence": {
                    "stage": "finalized",
                    "matched_order_ids": ["ORDER-RCN-1"],
                    "matched_deal_ids": ["DEAL-RCN-1"],
                    "matched_volume": 0.5,
                    "confidence": 94.0,
                },
            },
            {
                "resolved": True,
                "status": "FINALIZED",
                "reason": "matched_close_history",
                "orders": [],
                "deals": [close_deal],
                "close_deal": close_deal,
                "evidence": {
                    "stage": "finalized",
                    "matched_order_ids": ["ORDER-RCN-1"],
                    "matched_deal_ids": ["DEAL-RCN-1"],
                    "matched_volume": 0.5,
                    "confidence": 94.0,
                },
            },
        ]
    )
    bot = _build_bot(db, journal, connector)

    result_one = bot._finalize_trade_from_history_row(db.get_trade_by_identity(trade_id="RCN-1"), source="test")
    assert result_one["ok"] is False
    assert result_one["status"] == "CLOSE_HISTORY_PENDING"
    pending_row = db.get_trade_by_identity(trade_id="RCN-1")
    assert pending_row is not None
    assert pending_row["status"] == "CLOSE_HISTORY_PENDING"
    assert pending_row["resolution_attempts"] == 1

    result_two = bot._finalize_trade_from_history_row(db.get_trade_by_identity(trade_id="RCN-1"), source="test")
    assert result_two["ok"] is True
    finalized_row = db.get_trade_by_identity(trade_id="RCN-1")
    assert finalized_row is not None
    assert finalized_row["status"] == "FINALIZED"
    assert finalized_row["close_reason"] == "take_profit"
    assert float(finalized_row["pnl"]) == 1.5
    assert "DEAL-RCN-1" in str(finalized_row["matched_deal_ids"])

    bot._finalize_trade_from_history_row(db.get_trade_by_identity(trade_id="RCN-1"), source="test")
    final_events = db._query_dataframe(
        "SELECT * FROM trade_events WHERE trade_id = ? AND event_type = 'TRADE_FINALIZED';",
        ("RCN-1",),
    )
    assert len(final_events) == 1


def test_close_reconciliation_can_finalize_from_deal_history_without_orders(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_open_payload("RCN-2"))
    journal.record_trade_close(_pending_close_payload("RCN-2"))
    close_deal = SimpleNamespace(
        deal="DEAL-RCN-2",
        position_id="RCN-2",
        ticket="DEAL-RCN-2",
        price=99.5,
        volume=0.5,
        profit=-0.5,
        swap=0.0,
        commission=0.0,
        time=utc_now(),
        symbol="XAUUSD",
        comment="manual close",
    )
    connector = _ResolutionConnector(
        [
            {
                "resolved": True,
                "status": "FINALIZED",
                "reason": "matched_close_history",
                "orders": [],
                "deals": [close_deal],
                "close_deal": close_deal,
                "evidence": {
                    "stage": "finalized",
                    "matched_order_ids": [],
                    "matched_deal_ids": ["DEAL-RCN-2"],
                    "matched_volume": 0.5,
                    "confidence": 88.0,
                },
            }
        ]
    )
    bot = _build_bot(db, journal, connector)

    result = bot._finalize_trade_from_history_row(db.get_trade_by_identity(trade_id="RCN-2"), source="test")
    assert result["ok"] is True
    finalized_row = db.get_trade_by_identity(trade_id="RCN-2")
    assert finalized_row is not None
    assert finalized_row["status"] == "FINALIZED"
    assert finalized_row["close_reason"] == "manual_close"
    assert "DEAL-RCN-2" in str(finalized_row["matched_deal_ids"])


def test_blocked_setup_replay_suppresses_same_setup_without_material_change(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    bot = _build_bot(db, journal, _ResolutionConnector([]))
    now_utc = utc_now()
    bot.state["setup_registry"] = {
        "fp-1": {
            "last_decision": "blocked",
            "last_blocked_at": (now_utc - timedelta(seconds=20)).isoformat(),
            "last_block_reason": "blocked_chasing_entry",
            "last_anchor_time": "2026-04-20T08:00:00+00:00",
            "last_entry_price": 3300.00,
            "last_entry_score": 60.0,
            "last_spread_points": 12.0,
        }
    }

    suppressed = bot._blocked_setup_replay_stage(
        {
            "setup_fingerprint": "fp-1",
            "anchor_time": "2026-04-20T08:00:00+00:00",
        },
        {"entry_price": 3300.03, "entry_score": 62.0},
        {"spread_points": 11.0},
        "blocked_chasing_entry",
        now_utc,
    )
    assert suppressed["suppress"] is True

    allowed = bot._blocked_setup_replay_stage(
        {
            "setup_fingerprint": "fp-1",
            "anchor_time": "2026-04-20T08:01:00+00:00",
        },
        {"entry_price": 3300.03, "entry_score": 62.0},
        {"spread_points": 11.0},
        "blocked_chasing_entry",
        now_utc,
    )
    assert allowed["suppress"] is False
    assert allowed["material_change"] == "new_anchor_time"


def _lifecycle_manager(
    tmp_path: Path,
    *,
    open_positions: list[object] | None = None,
    pending_orders: list[object] | None = None,
) -> tuple[DatabaseService, TradeJournalService, LivePositionLifecycleManager, dict[str, object]]:
    db, journal = _journal(tmp_path)
    state: dict[str, object] = {}
    manager = LivePositionLifecycleManager(
        config={
            "mt5": {"symbol": "XAUUSD", "magic_number": 4242},
            "execution": {"pending_fill_entry_tolerance_points": 25.0},
        },
        state=state,
        connector=_LifecycleConnector([]),
        execution_service=_ExecutionService(open_positions=open_positions, pending_orders=pending_orders),
        risk_manager=_RiskManager(),
        trade_journal=journal,
        logger=_StubLogger(),
    )
    return db, journal, manager, state


def test_pending_fill_matching_prefers_single_high_confidence_candidate(tmp_path: Path) -> None:
    _, _, manager, _ = _lifecycle_manager(tmp_path)
    submitted_at = utc_now()
    pending = {
        "order_id": "ORDER-1",
        "symbol": "XAUUSD",
        "direction": "LONG",
        "volume": 0.5,
        "entry_price": 3300.0,
        "submitted_at": submitted_at.isoformat(),
    }
    position = SimpleNamespace(
        symbol="XAUUSD",
        ticket="POS-1",
        identifier="ORDER-1",
        order="ORDER-1",
        magic=4242,
        type=0,
        volume=0.5,
        price_open=3300.02,
        time=submitted_at,
    )

    result = manager._match_pending_fill(pending, [position], submitted_at)
    assert result["status"] == "matched"
    assert result["position"] is position
    assert result["confidence"] >= 55.0


def test_pending_fill_matching_marks_ambiguous_candidates(tmp_path: Path) -> None:
    db, _, manager, state = _lifecycle_manager(tmp_path)
    submitted_at = utc_now()
    state["pending_order"] = {
        "order_id": "ORDER-2",
        "symbol": "XAUUSD",
        "direction": "LONG",
        "volume": 0.5,
        "entry_price": 3300.0,
        "submitted_at": submitted_at.isoformat(),
        "setup_fingerprint": "fp-order-2",
        "setup_family": "LEBPRIM_SCALP",
        "entry_mode": "limit",
    }
    first = SimpleNamespace(symbol="XAUUSD", ticket="POS-A", identifier="A", order="A", magic=4242, type=0, volume=0.5, price_open=3300.0, time=submitted_at)
    second = SimpleNamespace(symbol="XAUUSD", ticket="POS-B", identifier="B", order="B", magic=4242, type=0, volume=0.5, price_open=3300.0, time=submitted_at)
    manager.execution_service.open_positions = [first, second]

    manager.sync_pending_order({"latest_bar": None}, {"mode": "LIVE"})

    pending = state["pending_order"]
    assert pending["match_status"] == "AMBIGUOUS"
    assert len(pending["match_candidates"]) == 2
    ambiguous_events = db._query_dataframe(
        "SELECT * FROM trade_events WHERE event_type = 'ORDER_FILL_AMBIGUOUS' AND trade_id = ?;",
        ("ORDER-2",),
    )
    assert len(ambiguous_events) == 1


def test_config_source_of_truth_prefers_runtime_and_detects_shadow_drift(tmp_path: Path) -> None:
    source_config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    save_json_atomic(tmp_path / "config.json", source_config)
    manager = ConfigManager(tmp_path)

    saved = manager.save(source_config, actor="test", reason="roundtrip")
    shadow_config = json.loads((tmp_path / "storage" / "config.json").read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert shadow_config["bot"]["trading_mode"] == source_config["bot"]["trading_mode"]

    shadow_config["bot"]["trading_mode"] = "DEMO"
    shadow_config.setdefault("storage", {})["database_path"] = "storage/drifted.db"
    save_json_atomic(tmp_path / "storage" / "config.json", shadow_config)

    snapshot = manager.source_of_truth_snapshot(
        runtime_state={
            "runtime": {"mode": "DRY_RUN"},
            "auto_execution_enabled": False,
            "bot_pid": 9999,
        },
        process_runtime={"mode": "LIVE", "database_path": "storage/live.db"},
    )
    assert snapshot["effective"]["mode"] == "LIVE"
    assert snapshot["sources"]["mode"] == "process_runtime"
    assert snapshot["effective"]["database_path"] == "storage/live.db"
    assert snapshot["sources"]["database_path"] == "process_runtime"
    assert snapshot["effective"]["execution_enabled"] is False
    assert snapshot["sources"]["execution_enabled"] == "persisted_control_state"
    assert snapshot["drift"]["detected"] is True
