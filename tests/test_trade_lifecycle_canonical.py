from __future__ import annotations

from pathlib import Path

from dashboard.services import DashboardDataService
from services.database import DatabaseService
from services.trade_journal import TradeJournalService


class _StubLogger:
    def log_signal_event(self, _row):
        return None

    def log_trade_event(self, _row):
        return None


def _journal(tmp_path: Path) -> tuple[DatabaseService, TradeJournalService]:
    db = DatabaseService(tmp_path / "bot.db")
    journal = TradeJournalService(tmp_path, db, _StubLogger(), {})
    return db, journal


def _trade_open_payload(ticket: str, ts: str = "2026-04-16T08:00:00+00:00") -> dict[str, object]:
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
        "metadata": {"source": "test"},
    }


def _trade_close_payload(ticket: str, ts: str = "2026-04-16T08:10:00+00:00") -> dict[str, object]:
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
        "exit_price": 102.0,
        "volume": 0.5,
        "final_volume": 0.5,
        "pnl": 2.0,
        "realized_r": 1.0,
        "hold_minutes": 10.0,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "LEBPRIM_SCALP",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "close_reason": "take_profit",
        "status": "FINALIZED",
        "event_type": "TRADE_FINALIZED",
        "closed_at": ts,
        "metadata": {"source": "test"},
    }


def test_open_then_close_updates_same_trade_row(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T100"))
    journal.record_trade_close(_trade_close_payload("T100"))

    rows = db._query_dataframe("SELECT * FROM trades WHERE trade_id = 'T100'")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "FINALIZED"
    assert float(row["entry_price"]) == 100.0
    assert float(row["exit_price"]) == 102.0
    assert float(row["pnl"]) == 2.0
    assert row["setup_family"] == "LEBPRIM_SCALP"


def test_open_pending_resolution_finalized_reuses_same_row(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T101"))
    journal.record_trade_close(
        {
            **_trade_close_payload("T101", ts="2026-04-16T08:07:00+00:00"),
            "status": "PENDING_CLOSE_RESOLUTION",
            "event_type": "TRADE_CLOSED",
            "pnl": None,
            "realized_r": None,
            "exit_price": None,
            "close_reason": "history_unavailable",
        }
    )
    journal.record_trade_close(_trade_close_payload("T101", ts="2026-04-16T08:12:00+00:00"))

    rows = db._query_dataframe("SELECT * FROM trades WHERE trade_id = 'T101'")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "FINALIZED"
    assert row["close_reason"] == "take_profit"
    assert float(row["pnl"]) == 2.0


def test_duplicate_open_payloads_do_not_create_duplicate_trade_rows(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T102"))
    journal.record_trade_open(
        {
            **_trade_open_payload("T102"),
            "comment": "richer-second-open",
            "regime_at_entry": "HIGH_VOLATILITY_BREAKOUT",
            "metadata": {"source": "test", "second": True},
        }
    )

    rows = db._query_dataframe("SELECT * FROM trades WHERE trade_id = 'T102'")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "OPENED"
    assert row["regime"] == "HIGH_VOLATILITY_BREAKOUT"
    assert row["comment"] == "richer-second-open"


def test_partial_payload_then_richer_payload_merges_non_empty_fields(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    partial = _trade_open_payload("T103")
    partial.pop("setup_family")
    partial.pop("regime_at_entry")
    partial.pop("session_at_entry")
    journal.record_trade_open(partial)
    journal.record_trade_managed(
        {
            "timestamp": "2026-04-16T08:03:00+00:00",
            "mode": "LIVE",
            "trade_id": "T103",
            "ticket": "T103",
            "position_id": "T103",
            "symbol": "XAUUSD",
            "side": "LONG",
            "setup_fingerprint": "fp-T103",
            "setup_family": "BREAKOUT_RETEST_CONTINUATION",
            "regime_at_entry": "TREND_CONTINUATION",
            "session_at_entry": "LONDON_OPEN",
            "entry_price": 100.0,
            "volume": 0.5,
            "note": "breakeven_activated",
        }
    )

    row = db.get_trade_by_identity(trade_id="T103")
    assert row is not None
    assert row["setup_family"] == "BREAKOUT_RETEST_CONTINUATION"
    assert row["regime"] == "TREND_CONTINUATION"
    assert row["session"] == "LONDON_OPEN"
    assert float(row["entry_price"]) == 100.0


def test_empty_update_payload_does_not_overwrite_existing_trade_context(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T103B"))
    journal.record_trade_close(
        {
            **_trade_close_payload("T103B", ts="2026-04-16T08:15:00+00:00"),
            "setup_family": "",
            "regime_at_entry": None,
            "session_at_entry": "",
            "comment": "",
            "note": "",
        }
    )

    row = db.get_trade_by_identity(trade_id="T103B")
    assert row is not None
    assert row["setup_family"] == "LEBPRIM_SCALP"
    assert row["regime"] == "TREND_CONTINUATION"
    assert row["session"] == "LONDON"
    assert row["comment"] == "fp-T103B"


def test_daily_performance_uses_canonical_closed_rows(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T104", ts="2026-04-16T07:55:00+00:00"))
    journal.record_trade_close(_trade_close_payload("T104", ts="2026-04-16T08:05:00+00:00"))
    journal.record_trade_open({**_trade_open_payload("T105", ts="2026-04-16T08:10:00+00:00"), "side": "SHORT"})
    journal.record_trade_close(
        {
            **_trade_close_payload("T105", ts="2026-04-16T08:20:00+00:00"),
            "side": "SHORT",
            "pnl": -1.5,
            "realized_r": -0.75,
            "close_reason": "stop_loss",
        }
    )

    summary = db.build_daily_analytics("2026-04-16")
    performance = db.build_trade_performance_report("2026-04-16")

    assert summary["live_trades"] == 2
    assert round(float(summary["total_pnl"]), 2) == 0.5
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert any(row["setup_name"] == "LEBPRIM_SCALP" for row in performance["rows"])


def test_dashboard_views_return_unresolved_and_closed_from_canonical_rows(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("T106"))
    journal.record_trade_close(
        {
            **_trade_close_payload("T106", ts="2026-04-16T08:08:00+00:00"),
            "status": "PENDING_CLOSE_RESOLUTION",
            "event_type": "TRADE_CLOSED",
            "pnl": None,
            "realized_r": None,
            "exit_price": None,
            "close_reason": "history_unavailable",
        }
    )
    journal.record_trade_open(_trade_open_payload("T107"))
    journal.record_trade_close(_trade_close_payload("T107", ts="2026-04-16T08:30:00+00:00"))

    dashboard = DashboardDataService(tmp_path, db)
    unresolved = dashboard.unresolved_trades(limit=10)
    closed = dashboard.recent_closed_trades(limit=10)

    assert len(unresolved) == 1
    assert unresolved[0]["trade_id"] == "T106"
    assert unresolved[0]["status"] == "PENDING_CLOSE_RESOLUTION"
    assert len(closed) == 1
    assert closed[0]["trade_id"] == "T107"
    assert closed[0]["status"] == "FINALIZED"


def test_repair_pass_rebuilds_canonical_trade_from_trade_events(tmp_path: Path) -> None:
    db = DatabaseService(tmp_path / "bot.db")
    db.record_trade_event(
        {
            "timestamp": "2026-04-16T09:00:00+00:00",
            "mode": "LIVE",
            "trade_id": "T108",
            "mt5_ticket": "T108",
            "position_id": "T108",
            "ticket": "T108",
            "symbol": "XAUUSD",
            "side": "LONG",
            "status": "OPENED",
            "event_type": "TRADE_OPENED",
            "setup": "fp-T108",
            "setup_family": "COMPRESSION_RELEASE",
            "regime": "TREND_CONTINUATION",
            "session": "LONDON",
            "entry_price": 100.0,
            "entry": 100.0,
            "volume": 0.3,
        }
    )
    db.record_trade_event(
        {
            "timestamp": "2026-04-16T09:10:00+00:00",
            "mode": "LIVE",
            "trade_id": "T108",
            "mt5_ticket": "T108",
            "position_id": "T108",
            "ticket": "T108",
            "symbol": "XAUUSD",
            "side": "LONG",
            "status": "FINALIZED",
            "event_type": "TRADE_FINALIZED",
            "setup": "fp-T108",
            "setup_family": "COMPRESSION_RELEASE",
            "regime": "TREND_CONTINUATION",
            "session": "LONDON",
            "entry_price": 100.0,
            "entry": 100.0,
            "exit_price": 101.0,
            "pnl": 1.0,
            "realized_r": 0.5,
            "volume": 0.3,
            "closed_at": "2026-04-16T09:10:00+00:00",
        }
    )

    db.repair_trade_lifecycle()
    rows = db._query_dataframe("SELECT * FROM trades WHERE trade_id = 'T108'")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "FINALIZED"
    assert float(row["exit_price"]) == 101.0
    assert float(row["pnl"]) == 1.0
