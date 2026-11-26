"""Automatic Time Machine odds for every new live zone, plus a Telegram message with the result.

Runs as a thread in the dashboard process (like the Telegram trade watcher). Every minute it:
1. reads the live bot's zones table read-only and queues a Time Machine run (request.json, the research-sandbox
   dispatcher does the work) for each new zone that is waiting or active;
2. sends one Telegram message for each Time Machine run that has finished (automatic or started from the panel).
It keeps only its own cursor file. It never talks to MT5, never touches orders, the bot or its config.

Config (optional, in config.json):  "time_machine": {"auto_odds": true, "telegram": true, "timeframes": [...]}
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from dashboard.strategy_tests import StrategyTests
from dashboard.time_machine import KIND, live_zones, queue_run, telegram_message
from services.indicator_lab.time_machine import SYMBOLS, TIMEFRAMES
from utils import load_json, save_json_atomic

POLL_SECONDS = 60
NEW_ZONE_MAX_AGE = timedelta(hours=2)    # older zones (e.g. after a dashboard restart) are not queued
MAX_QUEUE_PER_POLL = 6


def _parse(ts: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TimeMachineWatcher:
    def __init__(self, base_dir: Path, config_reader: Callable[[], dict[str, Any]], db_path: Path,
                 dispatch_state: Path = Path("/var/lib/lebprime-dispatch"), notifier_factory: Callable[..., Any] | None = None) -> None:
        self.root = Path(base_dir)
        self.config_reader = config_reader
        self.db_path = Path(db_path)
        self.tests = StrategyTests(self.root / "reports", dispatch_state, Path("/nonexistent"))
        self.cursor_path = self.root / "storage/time_machine_watcher.json"
        self.cursor: dict[str, Any] = load_json(self.cursor_path, {})
        self.notifier_factory = notifier_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ config
    def settings(self) -> tuple[dict[str, Any], dict[str, Any]]:
        config = self.config_reader() or {}
        return config.get("time_machine") or {}, config.get("telegram") or {}

    def notifier(self, telegram: dict[str, Any]) -> Any:
        if self.notifier_factory is not None:
            return self.notifier_factory(telegram)
        from services.telegram_notifier import TelegramNotifier

        n = TelegramNotifier(telegram.get("bot_token"), telegram.get("chat_id"), enabled=True,
                             display_timezone=telegram.get("display_timezone", "UTC"))
        return n if n.enabled else None

    def save(self) -> None:
        save_json_atomic(self.cursor_path, self.cursor)

    # ------------------------------------------------------------------ 1. queue new zones
    def queue_new_zones(self, cfg: dict[str, Any], now: datetime) -> list[str]:
        allowed = set(cfg.get("timeframes") or TIMEFRAMES)
        queued: dict[str, str] = self.cursor.setdefault("queued", {})
        started = []
        for z in reversed(live_zones(self.db_path, 120)):      # oldest first
            key = f"{z['zone_id']}|{z['limit_time']}"
            if key in queued:
                continue
            created = _parse(z.get("created_at") or z.get("limit_time"))
            fresh = created is not None and now - created <= NEW_ZONE_MAX_AGE
            usable = z["open"] and z["symbol"] in SYMBOLS and z["timeframe"] in TIMEFRAMES and z["timeframe"] in allowed
            if not (fresh and usable):
                queued[key] = ""          # seen, nothing to do
                continue
            twin = f"{z['symbol']}|{z['timeframe']}|{z['side']}|{z['entry1']}|{z['sl']}"
            if twin in self.cursor.setdefault("twins", {}):
                queued[key] = self.cursor["twins"][twin]
                continue
            if len(started) >= MAX_QUEUE_PER_POLL:
                break
            try:
                folder = queue_run(self.tests, {"symbol": z["symbol"], "timeframe": z["timeframe"], "side": z["side"], "entry1": z["entry1"],
                                                "sl": z["sl"], "zone_top": z["zone_top"], "zone_bottom": z["zone_bottom"],
                                                "limit_time": z["limit_time"], "live_zone_id": z["zone_id"], "zone_state": z.get("execution_status"),
                                                "actor": "auto"}, source="auto")
            except HTTPException as exc:
                if exc.status_code == 429:
                    break                 # queue full: try again next minute
                queued[key] = ""          # invalid zone (e.g. bad SL): skip for good
                continue
            queued[key] = self.cursor["twins"][twin] = folder.name
            started.append(folder.name)
        # keep the cursor small
        for name in ("queued", "twins"):
            items = self.cursor.get(name, {})
            if len(items) > 3000:
                self.cursor[name] = dict(list(items.items())[-2000:])
        return started

    # ------------------------------------------------------------------ 2. telegram
    def send_finished(self, telegram: dict[str, Any]) -> list[str]:
        since = self.cursor.setdefault("since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        sent: list[str] = self.cursor.setdefault("sent", [])
        notifier = None
        delivered = []
        for item in reversed(self.tests.list(limit=60, kind=KIND)):
            req = item.get("request") or {}
            if item["id"] in sent or str(req.get("created_at") or "") < since:
                continue
            if item["state"] in {"FAILED", "CANCELED"}:
                sent.append(item["id"])
                continue
            if item["state"] != "COMPLETED":
                continue
            summary = self.tests._json(self.tests.root / item["id"] / "summary.json")
            if not summary:
                continue
            notifier = notifier or self.notifier(telegram)
            if notifier is None:
                return delivered
            title, lines = telegram_message(summary)
            if notifier._send(title, lines):
                sent.append(item["id"])
                delivered.append(item["id"])
            else:
                break                     # Telegram down or rate-limited: retry next minute
        self.cursor["sent"] = sent[-500:]
        return delivered

    def poll_once(self, now: datetime | None = None) -> None:
        cfg, telegram = self.settings()
        now = now or datetime.now(timezone.utc)
        if cfg.get("auto_odds", True):
            self.queue_new_zones(cfg, now)
        if cfg.get("telegram", True) and telegram.get("enabled"):
            self.send_finished(telegram)
        else:
            self.cursor.setdefault("since", now.isoformat(timespec="seconds"))
        self.save()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()

        def loop() -> None:
            while not self.stop_event.is_set():
                try:
                    self.poll_once()
                    self.last_error = None
                except Exception as exc:  # never take the dashboard down
                    self.last_error = f"Time Machine watcher: {type(exc).__name__}: {exc}"
                self.stop_event.wait(POLL_SECONDS)

        self.thread = threading.Thread(target=loop, name="time-machine-watcher", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        cfg, telegram = self.settings()
        return {"auto_odds": bool(cfg.get("auto_odds", True)), "telegram": bool(cfg.get("telegram", True) and telegram.get("enabled")),
                "running": bool(self.thread and self.thread.is_alive()), "last_error": self.last_error,
                "sent": len(self.cursor.get("sent", [])), "since": self.cursor.get("since")}


__all__ = ["TimeMachineWatcher"]
