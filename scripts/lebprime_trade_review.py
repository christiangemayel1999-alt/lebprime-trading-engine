"""Daily take/skip review of LebPrime v3.4.0 trades: one row per zone that reached LIMIT CREATED.

Read-only: opens the bot database in SQLite read-only mode and never talks to MT5.
Each row has the zone, Entry 1 / Entry 2 / SL / TP1 / TP2, the broker orders and their status, the result,
and RSI(14), MACD (line, signal, histogram), ADX(14) with +DI/-DI, ATR(14) and its percentile, EMA distances,
H1/H4 trend and session at the LIMIT CREATED candle and at the Entry 1 / Entry 2 fills (closed candles only).

    python scripts/lebprime_trade_review.py --db storage/bot.db --since 2026-11-25T10:21:00+00:00 --out review.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

INDICATORS = ("rsi14", "macd", "macd_signal", "macd_hist", "adx14", "plus_di14", "minus_di14", "atr14", "atr14_percentile",
              "ema50_dist_atr", "ema200_dist_atr", "h1_trend", "h4_trend", "session")
MOMENTS = ("limit", "entry1", "entry2")
ZONE_COLUMNS = ("zone_id", "logical_symbol", "mt5_symbol", "timeframe", "side", "zone_bottom", "zone_top", "entry1", "entry2",
                "stop_loss", "tp1", "tp2", "buffer_used", "execution_status", "theoretical_state", "created_at", "updated_at")


def _connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def review_rows(db: str, since: str = "", until: str = "") -> list[dict]:
    con = _connect(db)
    sql = "SELECT * FROM lebprime_live_zones WHERE 1=1"
    args: list[str] = []
    if since:
        sql += " AND created_at >= ?"; args.append(since)
    if until:
        sql += " AND created_at < ?"; args.append(until)
    zones = con.execute(sql + " ORDER BY created_at", args).fetchall()
    rows = []
    for zone in zones:
        meta = json.loads(zone["metadata_json"] or "{}")
        row = {key: zone[key] for key in ZONE_COLUMNS}
        top, bottom = float(zone["zone_top"]), float(zone["zone_bottom"])
        risk = abs(float(zone["entry1"]) - float(zone["stop_loss"])) if zone["entry1"] is not None and zone["stop_loss"] is not None else None
        atr = meta.get("limit_atr14")
        row.update({
            "zone_width": top - bottom,
            "zone_width_atr": (top - bottom) / float(atr) if atr else None,
            "risk_price": risk,
            "risk_atr": risk / float(atr) if atr and risk else None,
            "limit_time": meta.get("limit_time"),
            "entry1_time": meta.get("entry1_time"),
            "entry2_time": meta.get("entry2_time"),
            "exit_reason": meta.get("exit_reason"),
            "result_r": meta.get("result_r"),
            "entry1_result_r": meta.get("entry1_result_r"),
            "entry2_result_r": meta.get("entry2_result_r"),
            "mfe_r": meta.get("mfe_r"),
            "mae_r": meta.get("mae_r"),
        })
        for moment in MOMENTS:
            for key in INDICATORS:
                row[f"{moment}_{key}"] = meta.get(f"{moment}_{key}")
        entries = con.execute(
            "SELECT entry_number, broker_status, volume, sl, tp, mt5_order_ticket, mt5_position_ticket, failure_reason "
            "FROM lebprime_broker_entries WHERE zone_id = ? ORDER BY entry_number", (zone["zone_id"],)
        ).fetchall()
        for entry in entries:
            n = entry["entry_number"]
            for key in ("broker_status", "volume", "sl", "tp", "mt5_order_ticket", "mt5_position_ticket", "failure_reason"):
                row[f"broker{n}_{key}"] = entry[key]
        events = con.execute("SELECT event_type FROM lebprime_live_events WHERE zone_id = ? ORDER BY id", (zone["zone_id"],)).fetchall()
        row["events"] = " > ".join(dict.fromkeys(e["event_type"] for e in events if e["event_type"] != "CANDIDATE"))
        row["decision"] = ""  # filled in by hand during the review: TAKE / SKIP + reason
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="storage/bot.db")
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)
    rows = review_rows(args.db, args.since, args.until)
    fields: list[str] = []
    for row in rows:
        fields.extend(key for key in row if key not in fields)
    out = sys.stdout if args.out == "-" else open(args.out, "w", newline="")
    writer = csv.DictWriter(out, fieldnames=fields or list(ZONE_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    if out is not sys.stdout:
        out.close()
        print(f"{len(rows)} zones -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
