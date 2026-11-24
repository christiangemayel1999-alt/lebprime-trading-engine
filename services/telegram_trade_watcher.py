"""Read-only observer of LebPrime records. Writes only its own delivery cursor/cache.

No order, execution, risk or configuration mutators are imported or called. The
injected history reader is the existing read-only MT5 deal-history service.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Any

from services.telegram_notifier import TelegramNotifier, price
from utils import load_json, save_json_atomic, utc_now


def daily_summary(trades: list[dict], day: str) -> dict:
    closed = [r for r in trades if r.get("status") == "CLOSED" and str(r.get("close_time") or "")[:10] == day and r.get("pnl") is not None]
    wins = sum(float(r["pnl"]) > 0 for r in closed)
    losses = sum(float(r["pnl"]) < 0 for r in closed)
    return {"source":"mt5_deals", "day":day, "trades":len(closed), "wins":wins, "losses":losses,
            "breakevens":len(closed)-wins-losses, "pnl":sum(float(r["pnl"]) for r in closed),
            "win_rate":100*wins/len(closed) if closed else 0}


class TelegramTradeWatcher:
    def __init__(self, base_dir: Path, config_reader: Callable, history_reader: Callable) -> None:
        self.root = Path(base_dir)
        self.config_reader, self.history_reader = config_reader, history_reader
        self.cursor_path = self.root / "storage/telegram_watcher_cursor.json"
        self.cursor = load_json(self.cursor_path, {})
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def read_entries(self, db_path: Path) -> list[dict]:
        if not db_path.is_file(): return []
        with sqlite3.connect(db_path.resolve().as_uri()+"?mode=ro", uri=True, timeout=3) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lebprime_broker_entries'").fetchone(): return []
            return [dict(r) for r in db.execute("SELECT * FROM lebprime_broker_entries WHERE historical=0 ORDER BY updated_at")]

    def poll_once(self) -> None:
        config = self.config_reader()
        cfg = config.get("telegram", {})
        if not cfg.get("enabled") or not cfg.get("lebprime_alerts_enabled", True): return
        notifier = TelegramNotifier(cfg.get("bot_token"), cfg.get("chat_id"), enabled=True,
                                    display_timezone=cfg.get("display_timezone", "UTC"))
        if not notifier.enabled: return
        rows = self.read_entries(self.root / config["storage"]["database_path"])
        # Baseline once, so enabling the observer doesn't announce historical orders.
        baseline = not self.cursor.get("initialized")
        seen = self.cursor.setdefault("entries", {})
        history = None
        try:
            history = self.history_reader(30)
        except Exception as exc:
            self.last_error = f"Deal history unavailable ({type(exc).__name__})"
        trades = history.get("trades", []) if history and history.get("ok") else []
        by_position = {str(t.get("position_id")):t for t in trades}
        if history and history.get("ok"):
            today = utc_now().date().isoformat()
            summary = daily_summary(trades, today)
            control = load_json(self.root/"storage/control_state.json", {})
            summary["execution_state"] = "PAUSED" if control.get("execution_paused") else "not supplied"
            save_json_atomic(self.root/"storage/telegram_trade_summary.json", {**summary,"updated_at":utc_now().isoformat()})
        for row in rows:
            key, status = row["idempotency_key"], row["broker_status"]
            if baseline or status not in {"PENDING","FILLED","CLOSED"}:
                seen[key] = status
                continue
            if seen.get(key) == status: continue
            data = {"symbol":row.get("broker_symbol"), "side":row.get("side"), "zone_id":row.get("zone_id"),
                    "entry_label":f"Entry {row.get('entry_number')} of 2", "ticket":row.get("mt5_position_ticket") or row.get("mt5_order_ticket"),
                    "entry":row.get("normalized_price") or row.get("entry_price"), "sl":row.get("sl"), "tp":row.get("tp"), "volume":row.get("volume")}
            deal = by_position.get(str(row.get("mt5_position_ticket")), {})
            if status == "PENDING":
                sent = notifier._send(f"📍 LIMIT placed · {data['symbol']} {data['side']}", [
                    f"Price {price(data['entry'],data)} · SL {price(data['sl'],data)} · TP {price(data['tp'],data)}",
                    f"Zone {data['zone_id']} · {data['entry_label']} · #{data['ticket']}"])
            elif status == "FILLED":
                data["entry"] = deal.get("open_price", data["entry"])
                sent = notifier.send_trade_open(data)
            else:
                # Closed status alone does not reveal P&L, R, or exit price. Never invent them.
                sent = notifier.send_trade_close({**data,"pnl":deal.get("pnl"),"exit_price":deal.get("close_price"),"close_reason":deal.get("exit_reason")})
            if sent:
                seen[key] = status
                save_json_atomic(self.cursor_path, {**self.cursor,"initialized":True})
        self.cursor["initialized"] = True
        save_json_atomic(self.cursor_path, self.cursor)

    def start(self) -> None:
        if self.thread and self.thread.is_alive(): return
        if not self.config_reader().get("telegram", {}).get("enabled"): return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                try: self.poll_once(); self.last_error = None
                except Exception as exc: self.last_error = f"Observer unavailable ({type(exc).__name__})"
                self.stop_event.wait(30)
        self.thread = threading.Thread(target=loop, name="telegram-trade-observer", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=5)
