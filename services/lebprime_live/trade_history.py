"""LebPrime trade history built from MT5 deals (the broker is the source of truth for P&L).

Each MT5 position is one trade: its IN deal is the entry (Entry 1 or Entry 2 of a zone), its OUT
deals are the exits (TP, SL, the bot's TP1 half-close or its own close). Trades are linked to their
zone through the order ticket stored in lebprime_broker_entries.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

DEAL_BUY, DEAL_SELL = 0, 1
ENTRY_IN = 0


def _utc(epoch: float | int, offset_seconds: int) -> str:
    return datetime.fromtimestamp(float(epoch) - offset_seconds, tz=timezone.utc).isoformat()


def _exit_reason(comment: str) -> str:
    text = str(comment or "")
    if text.startswith("[tp"):
        return "TP (broker)"
    if text.startswith("[sl"):
        return "Emergency stop (broker)"
    match = re.match(r"LP340-(\w+)", text)
    if match and match.group(1) in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "TP1"}:
        return {"SL_HIT": "Candle closed beyond SL", "TP1_FINAL": "Back to Entry 1 after TP1", "TP2_HIT": "TP2", "TP1": "TP1 half close"}[match.group(1)]
    return text or "Closed"


def build_trades(deals: Iterable[Any], entries_by_order: dict[str, dict], offset_seconds: int = 0) -> list[dict[str, Any]]:
    by_position: dict[int, list[Any]] = defaultdict(list)
    for deal in deals:
        position_id = int(getattr(deal, "position_id", 0) or 0)
        if position_id:
            by_position[position_id].append(deal)
    trades = []
    for position_id, group in by_position.items():
        group.sort(key=lambda d: (getattr(d, "time_msc", 0) or getattr(d, "time", 0)))
        ins = [d for d in group if int(getattr(d, "entry", -1)) == ENTRY_IN]
        outs = [d for d in group if int(getattr(d, "entry", -1)) != ENTRY_IN]
        if not ins:
            continue  # position opened before the history window
        first = ins[0]
        volume_in = sum(float(d.volume) for d in ins)
        volume_out = sum(float(d.volume) for d in outs)
        pnl = sum(float(getattr(d, "profit", 0.0)) + float(getattr(d, "commission", 0.0)) + float(getattr(d, "swap", 0.0)) + float(getattr(d, "fee", 0.0)) for d in group)
        close_price = (sum(float(d.price) * float(d.volume) for d in outs) / volume_out) if volume_out else None
        entry = entries_by_order.get(str(int(getattr(first, "order", 0) or 0)), {})
        closed = volume_out + 1e-9 >= volume_in
        label_match = re.search(r"ENTRY(\d)", str(getattr(first, "comment", "")))
        trades.append({
            "position_id": position_id,
            "symbol": first.symbol,
            "side": "BUY" if int(first.type) == DEAL_BUY else "SELL",
            "entry_label": f"Entry {label_match.group(1)}" if label_match else "",
            "volume": round(volume_in, 2),
            "open_time": _utc(first.time, offset_seconds),
            "open_price": float(first.price),
            "close_time": _utc(outs[-1].time, offset_seconds) if outs else None,
            "close_price": close_price,
            "pnl": round(pnl, 2),
            "status": "CLOSED" if closed else ("PARTIAL" if outs else "OPEN"),
            "result": ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT") if closed else None,
            "exit_reason": _exit_reason(getattr(outs[-1], "comment", "")) if outs else None,
            "zone_id": entry.get("zone_id"),
            "context": entry.get("context_key"),
            "tracked_by_bot": bool(entry),
        })
    trades.sort(key=lambda t: t["open_time"], reverse=True)
    return trades


def summarize(trades: list[dict[str, Any]], today: str) -> dict[str, Any]:
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    by_symbol: dict[str, dict[str, Any]] = {}
    for trade in closed:
        row = by_symbol.setdefault(trade["symbol"], {"symbol": trade["symbol"], "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        row["trades"] += 1
        row["wins"] += int(trade["pnl"] > 0)
        row["losses"] += int(trade["pnl"] < 0)
        row["pnl"] = round(row["pnl"] + trade["pnl"], 2)
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "net_pnl": round(sum(t["pnl"] for t in closed), 2),
        "today_pnl": round(sum(t["pnl"] for t in closed if str(t["close_time"] or "")[:10] == today), 2),
        "open_trades": sum(1 for t in trades if t["status"] != "CLOSED"),
        "by_symbol": sorted(by_symbol.values(), key=lambda r: r["symbol"]),
    }
