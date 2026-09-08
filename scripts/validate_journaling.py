"""Deterministic journaling validation harness."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.database import DatabaseService
from services.trade_journal import TradeJournalService
from utils import ensure_directory


class _StubLogger:
    def log_signal_event(self, row):
        return None

    def log_trade_event(self, row):
        return None

    def structured(self, *args, **kwargs):
        return None


def main() -> int:
    base_root = Path(__file__).resolve().parents[1] / "storage" / "validation_harness"
    root = ensure_directory(base_root / f"run_{uuid.uuid4().hex[:12]}")
    db = DatabaseService(root / "bot.db")
    journal = TradeJournalService(root, db, _StubLogger(), {"exit": {}, "journaling": {}, "bot": {}})

    trade_id = "TEST-001"
    journal.record_signal_event(
        {
            "timestamp": "2026-04-10T00:00:00+00:00",
            "event_type": "signal_observed",
            "symbol": "XAUUSD",
            "side": "LONG",
            "setup_fingerprint": trade_id,
            "setup_family": "TEST_SETUP",
            "regime_name": "TREND_CONTINUATION",
            "session_name": "LONDON",
            "entry_score": 88.5,
        }
    )
    journal.record_trade_open(
        {
            "timestamp": "2026-04-10T00:01:00+00:00",
            "trade_id": trade_id,
            "mt5_ticket": trade_id,
            "position_id": trade_id,
            "event_type": "TRADE_OPENED",
            "status": "OPENED",
            "symbol": "XAUUSD",
            "side": "LONG",
            "setup": "TEST_SETUP",
            "setup_family": "TEST_SETUP",
            "regime": "TREND_CONTINUATION",
            "session": "LONDON",
            "entry_price": 4700.0,
            "stop_loss": 4697.0,
            "take_profit": 4706.0,
            "volume": 0.1,
            "risk_amount": 30.0,
            "risk_percent": 0.5,
            "comment": "validation",
        }
    )
    journal.record_trade_close(
        {
            "timestamp": "2026-04-10T00:15:00+00:00",
            "trade_id": trade_id,
            "mt5_ticket": trade_id,
            "position_id": trade_id,
            "event_type": "TRADE_CLOSED",
            "status": "CLOSED",
            "symbol": "XAUUSD",
            "side": "LONG",
            "setup": "TEST_SETUP",
            "setup_family": "TEST_SETUP",
            "regime": "TREND_CONTINUATION",
            "session": "LONDON",
            "entry_price": 4700.0,
            "stop_loss": 4697.0,
            "take_profit": 4706.0,
            "exit_price": 4706.0,
            "pnl": 60.0,
            "realized_r": 2.0,
            "hold_minutes": 14.0,
            "win_loss": "WIN",
            "outcome_label": "WIN",
            "close_reason": "take_profit",
            "comment": "validation close",
        }
    )

    summary = db.build_daily_analytics("2026-04-10")
    report = db.build_trade_performance_report("2026-04-10")
    print(json.dumps({"summary": summary, "performance_rows": len(report["rows"]), "root": str(root)}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
