"""Money simulation for Indicator Lab backtests.

Turns a backtest's closed trades (R multiples) into a dollar account: every trade is sized from the
current balance, so profits and losses compound, and each closed trade moves the balance.

The rules are the ones used by the research money simulation
(reports/phase4/money_sim.py, results in "LebPrime AI Research/02 - Backtest Results/money_sim_1000usd.txt"):

* risk = a % of the current closed balance, recalculated for every trade;
* lots = risk / (stop distance x contract value per lot), rounded DOWN to the broker lot step;
* if that is below the broker minimum lot, the minimum lot is used only while its risk stays within
  a ceiling (default 2% of balance, the same guard the live bot uses), otherwise the trade is skipped;
* a cap on the total risk of all open trades (default 2% of balance);
* a daily loss stop: no new trades once the balance is down a set % from the start of the day (default 3%);
* spread (recorded on the entry candle) and round-trip commission are charged in dollars on the lots filled.

This module never talks to MT5 and is not used by the live trading path.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

# Broker contract specs (same numbers as scripts/phase4_search.py and reports/phase4/money_sim.py).
# value_per_price_per_lot: USD for a 1.0 price move on 1.0 lot. commission_per_lot: round trip, USD per 1.0 lot.
CONTRACTS: dict[str, dict[str, float]] = {
    "XAUUSD": {"value_per_price_per_lot": 100.0, "point": 0.01, "commission_per_lot": 7.0, "min_lot": 0.01, "lot_step": 0.01},
    "XAGUSD": {"value_per_price_per_lot": 5000.0, "point": 0.001, "commission_per_lot": 7.0, "min_lot": 0.01, "lot_step": 0.01},
    "US30": {"value_per_price_per_lot": 1.0, "point": 0.1, "commission_per_lot": 0.70, "min_lot": 0.1, "lot_step": 0.1},
    "USTEC": {"value_per_price_per_lot": 1.0, "point": 0.1, "commission_per_lot": 0.70, "min_lot": 0.1, "lot_step": 0.1},
    "WTI": {"value_per_price_per_lot": 1000.0, "point": 0.001, "commission_per_lot": 7.0, "min_lot": 0.01, "lot_step": 0.01},
    "EURUSD": {"value_per_price_per_lot": 100000.0, "point": 0.00001, "commission_per_lot": 7.0, "min_lot": 0.01, "lot_step": 0.01},
}
SYMBOL_ALIASES = {"NAS100": "USTEC", "XTIUSD": "WTI", "USOIL": "WTI", "DJ30": "US30", "GOLD": "XAUUSD", "SILVER": "XAGUSD"}
CLOSED_STATES = {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}
SINGLE_ENTRY_MODES = {"Close confirmation", "Sweep reclaim", "Rejection candle", "Break of structure"}

DEFAULTS = {
    "starting_capital": 1000.0,
    "risk_pct": 1.0,
    "min_lot_max_risk_pct": 2.0,
    "open_risk_cap_pct": 2.0,
    "daily_loss_stop_pct": 3.0,
    "include_spread": True,
}


def contract_for(symbol: str | None) -> dict[str, float]:
    key = str(symbol or "").upper()
    key = SYMBOL_ALIASES.get(key, key)
    if key in CONTRACTS:
        return dict(CONTRACTS[key])
    for name, spec in CONTRACTS.items():
        if key.startswith(name):  # broker suffixes such as XAUUSD.m
            return dict(spec)
    return {"value_per_price_per_lot": 1.0, "point": 0.0, "commission_per_lot": 0.0, "min_lot": 0.01, "lot_step": 0.01}


def _get(zone: Any, name: str, default: Any = None) -> Any:
    if isinstance(zone, dict):
        return zone.get(name, default)
    return getattr(zone, name, default)


def _metadata(zone: Any) -> dict[str, Any]:
    meta = _get(zone, "metadata_json") if isinstance(zone, dict) else None
    if meta is None:
        meta = _get(zone, "metadata")
    return meta if isinstance(meta, dict) else {}


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace(" ", "T", 1).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def trades_from_zones(zones: Iterable[Any], symbol: str | None = None, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Closed trades of one backtest, with what is needed to size them in lots.

    ``fill_fraction`` is the share of the full planned position that was actually filled
    (two-leg setups put half the size on each entry), matching the research cost model."""
    parameters = parameters or {}
    single = parameters.get("entry_mode") in SINGLE_ENTRY_MODES or parameters.get("entry2_mode") == "Off"
    trades: list[dict[str, Any]] = []
    for zone in zones:
        if _get(zone, "terminal_state") not in CLOSED_STATES:
            continue
        entry_price, sl_price = _get(zone, "entry_price"), _get(zone, "sl_price")
        if entry_price is None or sl_price is None:
            continue
        risk_price = abs(float(entry_price) - float(sl_price))
        entry_time = _parse_time(_get(zone, "entry1_activated_time") or _get(zone, "activated_time"))
        exit_time = _parse_time(_get(zone, "terminal_time"))
        if not risk_price or entry_time is None or exit_time is None:
            continue
        meta = _metadata(zone)
        sym = _get(zone, "symbol") or symbol
        spec = contract_for(sym)
        legs = meta.get("live_legs") or {}
        filled = sum(1 for leg in legs.values() if isinstance(leg, dict) and leg.get("filled")) or 1
        weight = 1.0 if (filled == 1 and single) else 0.5
        side = str(_get(zone, "side") or "").upper()
        trades.append({
            "zone_id": _get(zone, "id") or _get(zone, "engine_zone_id"),
            "symbol": sym,
            "side": "BUY" if side in {"DEMAND", "BUY", "LONG"} else "SELL",
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": float(entry_price),
            "sl_price": float(sl_price),
            "risk_price": risk_price,
            "gross_r": float(_get(zone, "result_r") or 0.0),
            "fill_fraction": weight * filled,
            "spread_price": float(meta.get("entry1_spread") or 0.0) * spec["point"],
            "outcome": _get(zone, "terminal_state"),
        })
    trades.sort(key=lambda t: (t["entry_time"], t["exit_time"]))
    return trades


def _floor_to_step(value: float, step: float) -> float:
    return round(math.floor(value / step + 1e-9) * step, 8)


def simulate(
    trades: list[dict[str, Any]],
    starting_capital: float = DEFAULTS["starting_capital"],
    risk_pct: float = DEFAULTS["risk_pct"],
    min_lot_max_risk_pct: float = DEFAULTS["min_lot_max_risk_pct"],
    open_risk_cap_pct: float = DEFAULTS["open_risk_cap_pct"],
    daily_loss_stop_pct: float = DEFAULTS["daily_loss_stop_pct"],
    include_spread: bool = DEFAULTS["include_spread"],
    min_lot: float | None = None,
    lot_step: float | None = None,
    commission_per_lot: float | None = None,
) -> dict[str, Any]:
    """Run the account through the trades in time order. Overrides of min lot, lot step and
    commission apply to every symbol; when left empty the broker values in CONTRACTS are used.

    A trade may carry ``risk_share`` (default 1.0) to split the risk % between several settings on
    one symbol, as the research portfolio simulation does."""
    capital = float(starting_capital)
    if capital <= 0:
        raise ValueError("Starting capital must be above 0")
    if risk_pct <= 0:
        raise ValueError("Risk per trade must be above 0%")
    balance = capital
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    day, day_start = None, balance
    peak, max_dd_usd, max_dd_pct = balance, 0.0, 0.0
    curve = []

    def close_until(limit: datetime | None) -> None:
        nonlocal balance, peak, max_dd_usd, max_dd_pct
        open_pos.sort(key=lambda p: p["exit_time"])
        while open_pos and (limit is None or open_pos[0]["exit_time"] <= limit):
            pos = open_pos.pop(0)
            balance += pos["net_usd"]
            peak = max(peak, balance)
            max_dd_usd = max(max_dd_usd, peak - balance)
            max_dd_pct = max(max_dd_pct, (peak - balance) / peak * 100 if peak > 0 else 0.0)
            pos["balance_after"] = balance
            pos["drawdown_pct"] = (peak - balance) / peak * 100 if peak > 0 else 0.0
            closed.append(pos)
            curve.append({"time": pos["exit_time"].isoformat(), "balance": round(balance, 2)})

    for trade in sorted(trades, key=lambda t: t["entry_time"]):  # stable: ties keep their given order
        close_until(trade["entry_time"])
        spec = contract_for(trade.get("symbol"))
        step = float(lot_step or spec["lot_step"])
        minimum = float(min_lot or spec["min_lot"])
        commission = spec["commission_per_lot"] if commission_per_lot is None else float(commission_per_lot)
        value = spec["value_per_price_per_lot"]
        base = {"zone_id": trade.get("zone_id"), "symbol": trade.get("symbol"), "side": trade.get("side"),
                "entry_time": trade["entry_time"], "exit_time": trade["exit_time"], "gross_r": trade["gross_r"], "outcome": trade.get("outcome")}
        d = trade["entry_time"].date()
        if d != day:
            day, day_start = d, balance
        if balance <= 0:
            skipped.append({**base, "reason": "Account empty"})
            continue
        if balance <= day_start * (1 - daily_loss_stop_pct / 100):
            skipped.append({**base, "reason": f"Daily loss stop ({daily_loss_stop_pct:g}%) hit"})
            continue
        per_lot = trade["risk_price"] * value
        target = balance * risk_pct / 100 * float(trade.get("risk_share", 1.0))
        lots = _floor_to_step(target / per_lot, step) if per_lot > 0 else 0.0
        if lots < minimum:
            lots = minimum
            if lots * per_lot > balance * min_lot_max_risk_pct / 100:
                skipped.append({**base, "reason": f"Minimum lot {minimum:g} would risk more than {min_lot_max_risk_pct:g}%"})
                continue
        risk_usd = lots * per_lot
        open_risk = sum(p["risk_usd"] for p in open_pos)
        if open_risk + risk_usd > balance * open_risk_cap_pct / 100:
            skipped.append({**base, "reason": f"Open risk cap ({open_risk_cap_pct:g}%) reached"})
            continue
        filled_lots = lots * float(trade.get("fill_fraction", 1.0))
        gross_usd = trade["gross_r"] * risk_usd
        spread_usd = trade.get("spread_price", 0.0) * value * filled_lots if include_spread else 0.0
        commission_usd = commission * filled_lots
        net_usd = gross_usd - spread_usd - commission_usd
        open_pos.append({**base, "lots": round(lots, 8), "risk_usd": risk_usd, "risk_pct": risk_usd / balance * 100, "balance_before": balance,
                         "gross_usd": gross_usd, "spread_usd": spread_usd, "commission_usd": commission_usd, "net_usd": net_usd,
                         "net_r": net_usd / risk_usd if risk_usd else 0.0})
    close_until(None)

    wins = [p for p in closed if p["net_usd"] > 0]
    losses = [p for p in closed if p["net_usd"] <= 0]
    gross_win = sum(p["net_usd"] for p in wins)
    gross_loss = -sum(p["net_usd"] for p in losses)
    by_year: dict[str, float] = {}
    for p in closed:
        by_year[str(p["exit_time"].year)] = by_year.get(str(p["exit_time"].year), 0.0) + p["net_usd"]
    first_time = min((t["entry_time"] for t in trades), default=None)
    if first_time is not None:
        curve.insert(0, {"time": first_time.isoformat(), "balance": round(capital, 2)})

    def row(p: dict[str, Any], number: int) -> dict[str, Any]:
        return {"n": number, **{k: (v.isoformat() if isinstance(v, datetime) else (round(v, 4) if isinstance(v, float) else v)) for k, v in p.items()}}

    summary = {
        "starting_capital": round(capital, 2),
        "ending_balance": round(balance, 2),
        "net_profit": round(balance - capital, 2),
        "return_pct": round((balance - capital) / capital * 100, 2),
        "max_drawdown_usd": round(max_dd_usd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "peak_balance": round(peak, 2),
        "trades_available": len(trades),
        "trades_taken": len(closed),
        "trades_skipped": len(skipped),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "largest_win_usd": round(max((p["net_usd"] for p in closed), default=0.0), 2),
        "largest_loss_usd": round(min((p["net_usd"] for p in closed), default=0.0), 2),
        "total_commission_usd": round(sum(p["commission_usd"] for p in closed), 2),
        "total_spread_usd": round(sum(p["spread_usd"] for p in closed), 2),
        "profit_by_year": {y: round(v, 2) for y, v in sorted(by_year.items())},
    }
    settings = {"starting_capital": capital, "risk_pct": risk_pct, "min_lot_max_risk_pct": min_lot_max_risk_pct, "open_risk_cap_pct": open_risk_cap_pct,
                "daily_loss_stop_pct": daily_loss_stop_pct, "include_spread": include_spread, "min_lot": min_lot, "lot_step": lot_step, "commission_per_lot": commission_per_lot}
    return {
        "summary": summary,
        "settings": settings,
        "trades": [row(p, i + 1) for i, p in enumerate(closed)],
        "skipped": [row(p, i + 1) for i, p in enumerate(skipped)],
        "equity_curve": curve,
    }
