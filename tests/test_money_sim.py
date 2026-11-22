from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.indicator_lab import money_sim

T0 = datetime(2025, 1, 6, 8, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def trade(hours: float, r: float, risk_price: float = 5.0, hold: float = 1.0, symbol: str = "XAUUSD", **extra):
    entry = T0 + timedelta(hours=hours)
    return {"symbol": symbol, "entry_time": entry, "exit_time": entry + timedelta(hours=hold), "risk_price": risk_price,
            "gross_r": r, "fill_fraction": 1.0, "spread_price": 0.0, **extra}


def test_balance_compounds_trade_by_trade():
    # Gold: $5 stop x $100 per lot = $500 per lot. 1% of $1,000 = $10 -> 0.02 lots, risk $10.
    result = money_sim.simulate([trade(0, 1.0), trade(2, -1.0), trade(4, 2.0)], starting_capital=1000, risk_pct=1.0,
                                commission_per_lot=0, include_spread=False)
    rows = result["trades"]
    assert [row["lots"] for row in rows] == [0.02, 0.02, 0.02]
    assert [row["balance_after"] for row in rows] == [1010.0, 1000.0, 1020.0]
    s = result["summary"]
    assert s["ending_balance"] == 1020.0 and s["net_profit"] == 20.0 and s["return_pct"] == 2.0
    assert s["max_drawdown_usd"] == 10.0 and s["max_drawdown_pct"] == pytest.approx(0.99, abs=0.01)
    assert [p["balance"] for p in result["equity_curve"]] == [1000.0, 1010.0, 1000.0, 1020.0]


def test_risk_is_recalculated_from_current_balance():
    # Starting at $10,000 the 1% risk is $100 = 0.20 lots; after a +3R win ($300) it is 1% of $10,300 = 0.20 lots (floor).
    result = money_sim.simulate([trade(0, 3.0), trade(2, 1.0, risk_price=1.0)], starting_capital=10000, risk_pct=1.0,
                                commission_per_lot=0, include_spread=False)
    first, second = result["trades"]
    assert first["lots"] == 0.2
    # $1 stop = $100 per lot; 1% of $10,300 = $103 -> 1.03 lots
    assert second["lots"] == 1.03 and second["balance_before"] == 10300.0


def test_min_lot_guard_open_risk_cap_and_daily_stop():
    # Huge stop: min lot 0.01 x $50 x $100 = $50 = 5% of $1,000 -> above the 2% ceiling -> skipped.
    result = money_sim.simulate([trade(0, 1.0, risk_price=50.0)], starting_capital=1000)
    assert result["summary"]["trades_taken"] == 0 and "Minimum lot" in result["skipped"][0]["reason"]
    # Two overlapping 1% trades fit a 2% cap, a third is refused.
    overlapping = [trade(0, 1.0, hold=10), trade(1, 1.0, hold=10), trade(2, 1.0, hold=10)]
    result = money_sim.simulate(overlapping, starting_capital=1000, commission_per_lot=0, include_spread=False)
    assert result["summary"]["trades_taken"] == 2 and "Open risk cap" in result["skipped"][0]["reason"]
    # -$20 then -$10 (lots shrink with the balance) = -3% that day, past a 2.5% daily stop -> the rest of the day is skipped.
    losers = [trade(h, -2.0, hold=0.5) for h in (0, 1, 2, 3)]
    result = money_sim.simulate(losers, starting_capital=1000, daily_loss_stop_pct=2.5, commission_per_lot=0, include_spread=False)
    assert [row["balance_after"] for row in result["trades"]] == [980.0, 970.0]
    assert result["summary"]["trades_skipped"] == 2 and "Daily loss stop" in result["skipped"][0]["reason"]
    # The next day trading resumes.
    result = money_sim.simulate(losers + [trade(30, 1.0)], starting_capital=1000, daily_loss_stop_pct=2.5, commission_per_lot=0, include_spread=False)
    assert result["summary"]["trades_taken"] == 3


def test_spread_and_commission_are_charged_on_filled_lots():
    t = trade(0, 1.0, spread_price=0.20)
    result = money_sim.simulate([t], starting_capital=1000, risk_pct=1.0)  # broker: $7 per lot round trip
    row = result["trades"][0]
    assert row["lots"] == 0.02
    assert row["commission_usd"] == pytest.approx(0.14)
    assert row["spread_usd"] == pytest.approx(0.40)  # 0.20 price x $100 x 0.02 lots
    assert row["net_usd"] == pytest.approx(10.0 - 0.54)


def test_trades_from_zones_uses_entry_and_exit_and_leg_fills():
    zones = [
        {"id": 1, "symbol": "XAUUSD", "side": "DEMAND", "terminal_state": "TP2_HIT", "entry_price": 2000.0, "sl_price": 1995.0,
         "entry1_activated_time": "2025-01-06T08:00:00+00:00", "terminal_time": "2025-01-06T10:00:00+00:00", "result_r": 1.5,
         "metadata_json": {"entry1_spread": 15, "live_legs": {"1": {"filled": True}, "2": {"filled": False}}}},
        {"id": 2, "symbol": "XAUUSD", "side": "SUPPLY", "terminal_state": "CANCELED", "entry_price": 2000.0, "sl_price": 2005.0},
    ]
    trades = money_sim.trades_from_zones(zones)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "BUY" and t["risk_price"] == 5.0 and t["fill_fraction"] == 0.5
    assert t["spread_price"] == pytest.approx(0.15)


def test_matches_research_money_simulation():
    """Same rules as reports/phase4/money_sim.py: $1,000, 1% risk, min lot allowed up to 2% -> $21,887."""
    pd = pytest.importorskip("pandas")
    path = ROOT / "LebPrime AI Research" / "02 - Backtest Results" / "money_sim_trades.csv"
    if not path.exists():
        pytest.skip("research trade list not on this machine")
    t = pd.read_csv(path)
    t["entry_time"] = pd.to_datetime(t.entry_time, utc=True, format="mixed")
    t["exit_time"] = pd.to_datetime(t.exit_time, utc=True, format="mixed")
    nset = t.groupby("symbol").setting.nunique()
    trades = [{"symbol": r.symbol, "entry_time": r.entry_time.to_pydatetime(), "exit_time": r.exit_time.to_pydatetime(), "risk_price": r.risk_price,
               "gross_r": r.net_r, "risk_share": 1 / nset[r.symbol], "fill_fraction": 0.0} for r in t.itertuples()]
    s = money_sim.simulate(trades, 1000, 1.0, 2.0, 2.0, 3.0, include_spread=False, commission_per_lot=0)["summary"]
    assert round(s["ending_balance"]) == 21887 and s["trades_taken"] == 6421


def test_dashboard_has_money_simulation_panel():
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "${renderMoneyPanel()}" in source and "bindMoneyPanel();" in source
    assert "/api/indicator-lab/tests/${testId}/money?" in source
    assert "Starting capital ($)" in source and "Balance after" in source


def test_service_money_simulation_for_completed_test(tmp_path):
    from services.database import DatabaseService
    from services.indicator_lab import IndicatorLabService
    from tests.test_indicator_lab import FakeConnector

    db = DatabaseService(tmp_path / "bot.db")
    lab = IndicatorLabService(tmp_path, db, {})
    dataset = lab.dataset_service.create_mt5_dataset(name="frozen", symbol="XAUUSD", timeframe="M1", start_date="2026-08-01", end_date="2026-08-01", connector=FakeConnector())
    created = lab.create_campaign(dataset_id=dataset["id"], indicator="LEBPRIME", fixed_parameters={"zone_confirm_delay": 0, "min_zone_width_atr": 0.01, "max_zone_width_atr": 5.0})
    lab.execute_campaign(created["campaign"]["id"], worker_id="test-worker")
    test_id = lab.campaign_results(created["campaign"]["id"])[0]["id"]
    result = lab.test_money_simulation(test_id, starting_capital=2500, risk_pct=0.5)
    assert result["summary"]["starting_capital"] == 2500.0
    assert result["symbol"] == "XAUUSD" and result["contract"]["min_lot"] == 0.01
    assert result["summary"]["trades_available"] == result["summary"]["trades_taken"] + result["summary"]["trades_skipped"]
    if result["trades"]:
        assert result["trades"][-1]["balance_after"] == result["summary"]["ending_balance"]
    with pytest.raises(ValueError):
        lab.test_money_simulation(999999)
