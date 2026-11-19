"""Dashboard trade history built from MT5 deals."""

from types import SimpleNamespace as D

from services.lebprime_live.trade_history import build_trades, summarize


def test_entries_and_exits_group_by_position_and_link_to_zone():
    deals = [
        D(position_id=1, order=11, entry=0, type=1, symbol="USTEC", volume=0.1, price=27694.0, profit=0.0, commission=-0.1, swap=0.0, fee=0.0, time=1000, time_msc=1000000, comment="LP340-AB-ENTRY1"),
        D(position_id=1, order=12, entry=1, type=0, symbol="USTEC", volume=0.1, price=27632.6, profit=6.17, commission=-0.1, swap=0.0, fee=0.0, time=2000, time_msc=2000000, comment="[tp 27632.6]"),
        D(position_id=2, order=21, entry=0, type=0, symbol="XAUUSD", volume=0.01, price=3714.0, profit=0.0, commission=0.0, swap=0.0, fee=0.0, time=1500, time_msc=1500000, comment="LP340-CD-ENTRY2"),
    ]
    trades = build_trades(deals, {"11": {"zone_id": "NAS-3M-S-1", "context_key": "NAS100-M3"}}, offset_seconds=0)
    closed = next(t for t in trades if t["position_id"] == 1)
    assert closed["status"] == "CLOSED" and closed["result"] == "WIN" and closed["pnl"] == 5.97
    assert closed["exit_reason"] == "TP (broker)" and closed["zone_id"] == "NAS-3M-S-1" and closed["entry_label"] == "Entry 1"
    open_trade = next(t for t in trades if t["position_id"] == 2)
    assert open_trade["status"] == "OPEN" and open_trade["tracked_by_bot"] is False
    summary = summarize(trades, "1970-01-01")
    assert summary["closed_trades"] == 1 and summary["wins"] == 1 and summary["open_trades"] == 1
