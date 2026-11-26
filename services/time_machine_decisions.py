"""The Time Machine's decision per live zone: a small index the dashboard and Telegram messages read.

The Time Machine watcher (dashboard process) writes storage/time_machine_decisions.json when a run finishes:
{zone_id: {verdict, win %, R per trade, Monte Carlo, fill chance, ...}}. It is display and logging only: the bot
never reads it and no trading decision depends on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils import load_json, save_json_atomic

INDEX = "storage/time_machine_decisions.json"
MAX_ZONES = 5000
ICON = {"positive": "✅", "negative": "❌", "few": "⚠️", "mixed": "🟡", "none": "⚪"}
WORD = {"positive": "Positive odds", "negative": "Negative odds", "few": "Too few similar zones", "mixed": "Past positive, paths negative",
        "none": "No history"}


def _head(summary: dict[str, Any], exit_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    block = (summary.get("exits") or {}).get(exit_name) or ({"analogs": summary.get("analogs"), "monte_carlo": summary.get("monte_carlo")} if exit_name == "tp" else {})
    analogs = block.get("analogs") or {}
    head = next((t for t in analogs.get("tiers", []) if t.get("key") == analogs.get("headline")), {}) or {}
    return head, block.get("monte_carlo") or {}


def verdict_code(head: dict[str, Any], mc: dict[str, Any]) -> str:
    if not head.get("trades"):
        return "none"
    if not head.get("trusted"):
        return "few"
    if (head.get("avg_r") or 0) > 0 and (mc.get("expected_r") or 0) > 0:
        return "positive"
    if (head.get("avg_r") or 0) > 0:
        return "mixed"
    return "negative"


def compact(summary: dict[str, Any], run_id: str) -> dict[str, Any]:
    """One line of facts per zone: the verdict for the bot's current exit, and the TP 1R numbers next to it."""
    live = summary.get("live_exit") or "tp"
    head, mc = _head(summary, live)
    tp_head, tp_mc = _head(summary, "tp")
    code = verdict_code(head, mc)
    req = summary.get("request") or {}
    return {"run_id": run_id, "verdict": code, "exit": live, "trades": head.get("trades"), "win_pct": head.get("win_pct"),
            "avg_r": head.get("avg_r"), "mc_win_pct": mc.get("win_pct", mc.get("p_tp_first")), "mc_expected_r": mc.get("expected_r"),
            "tp_win_pct": tp_head.get("win_pct"), "tp_avg_r": tp_head.get("avg_r"), "p_fill": summary.get("p_fill"),
            "limit_time": req.get("limit_time"), "decided_at": summary.get("finished_at"), "data_until": summary.get("data_until"),
            "symbol": (summary.get("zone") or {}).get("symbol"), "timeframe": (summary.get("zone") or {}).get("timeframe"),
            "side": (summary.get("zone") or {}).get("side")}


def line(decision: dict[str, Any] | None) -> str:
    """Short text for Telegram and tables, e.g. '🔮 TM ❌ Negative odds · 44 zones 36% win -0.12R · fill 99%'."""
    if not decision:
        return "🔮 TM: no decision yet"
    code = decision.get("verdict") or "none"
    parts = [f"🔮 TM {ICON.get(code, '')} {WORD.get(code, code)}"]
    if decision.get("trades"):
        r = decision.get("avg_r")
        parts.append(f"{decision['trades']} zones {float(decision.get('win_pct') or 0):.0f}% win {float(r or 0):+.2f}R")
    if decision.get("p_fill") is not None:
        parts.append(f"fill {float(decision['p_fill']):.0f}%")
    return " · ".join(parts)


def load(root: Path) -> dict[str, Any]:
    data = load_json(Path(root) / INDEX, {})
    return data if isinstance(data, dict) else {}


def record(root: Path, zone_ids: list[str], decision: dict[str, Any]) -> None:
    data = load(root)
    for zone_id in zone_ids:
        if zone_id:
            data[str(zone_id)] = decision
    if len(data) > MAX_ZONES:
        data = dict(list(data.items())[-MAX_ZONES:])
    save_json_atomic(Path(root) / INDEX, data)
