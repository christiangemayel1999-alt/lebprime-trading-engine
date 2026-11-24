"""Secure, bounded Telegram presentation. No broker or order operations."""
from __future__ import annotations

import hashlib
import html
import json
import math
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from services.alert_throttle import AlertThrottle
from utils import UTC, utc_now, load_json, to_utc


def number(value: Any, digits: int = 2, signed: bool = False) -> str:
    try:
        n = float(value)
        if value is None or isinstance(value, bool) or not math.isfinite(n):
            return "—"
        return format(n, f"{'+' if signed else ''},.{digits}f")
    except (TypeError, ValueError):
        return "—"


def money(value: Any) -> str:
    text = number(value, signed=True)
    return "—" if text == "—" else text[0] + "$" + text[1:]


def price(value: Any, data: dict[str, Any]) -> str:
    spec = data.get("symbol_info") or {}
    digits = data.get("digits", spec.get("digits") if isinstance(spec, dict) else getattr(spec, "digits", None))
    if digits is None:
        digits = {"XAUUSD":2, "XAGUSD":3, "US30":1, "USTEC":1, "NAS100":1, "WTI":3, "EURUSD":5}.get(str(data.get("symbol", "")).upper(), 5)
    try:
        digits = max(0, min(10, int(digits)))
    except (ValueError, TypeError):
        digits = 5
    return number(value, digits)


def bounded_html(text: str, limit: int = 4000) -> str:
    """Bound UTF-16 length and escaped payload without splitting HTML entities."""
    parts, size = [], 0
    for char in text:
        escaped = html.escape(char, quote=True)
        cost = len(escaped.encode("utf-16-le")) // 2
        if size + cost > limit - 1:
            return "".join(parts) + "…"
        parts.append(escaped)
        size += cost
    return "".join(parts)


class TelegramNotifier:
    _throttle = AlertThrottle()
    _repeats: dict[str, int] = {}
    _retry_at: dict[str, float] = {}
    _lock = threading.Lock()

    def __init__(self, token: str | None, chat_id: str | None, timeout_seconds: int = 5,
                 enabled: bool = False, display_timezone: str | None = None, dry_print: bool = False) -> None:
        self.token, self.chat_id = token or "", chat_id or ""
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled and bool(self.token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.last_error: str | None = None
        self.last_message = ""
        self.dry_print = dry_print
        # Existing main.py caller stays untouched. Only read the presentation key.
        if display_timezone is None:
            try:
                cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
                display_timezone = cfg.get("telegram", {}).get("display_timezone", "UTC")
            except (OSError, ValueError, AttributeError):
                display_timezone = "UTC"
        try:
            self.timezone = ZoneInfo(display_timezone or "UTC")
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            self.timezone = ZoneInfo("UTC")

    def _timestamp(self) -> str:
        now = utc_now()
        return f"{now.astimezone(self.timezone):%d %b %H:%M} ({self.timezone.key}) · {now.astimezone(UTC):%H:%M} UTC"

    def _send(self, title: str, lines: list[str]) -> bool:
        text = "\n".join([title + " · DEMO", *[str(s).replace("\n", " · ") for s in lines[:4]], self._timestamp()])
        self.last_message = bounded_html(text)
        if self.dry_print:
            print(self.last_message)
            return True
        if not self.enabled or time.monotonic() < self._retry_at.get(self.token, 0):
            return False
        try:
            response = requests.post(self.base_url, data={"chat_id":self.chat_id, "text":self.last_message,
                "parse_mode":"HTML", "disable_web_page_preview":True}, timeout=self.timeout_seconds, verify=True)
            if response.ok:
                self.last_error = None
                return True
            if response.status_code == 429:
                try:
                    delay = max(1, float(response.json().get("parameters", {}).get("retry_after", 60)))
                    if not math.isfinite(delay): delay = 60
                except (ValueError, TypeError, AttributeError):
                    delay = 60
                self._retry_at[self.token] = time.monotonic() + delay
            self.last_error = f"Telegram HTTP {response.status_code}"
        except requests.RequestException as exc:
            self.last_error = f"Telegram transport unavailable ({type(exc).__name__})"
        return False

    def _alert(self, severity: str, message: str) -> bool:
        fingerprint = hashlib.sha256(str(message).encode()).hexdigest()
        key = hashlib.sha256(f"{self.token}:{self.chat_id}:{severity}:{fingerprint}".encode()).hexdigest()
        with self._lock:
            if not self._throttle.should_emit(key, fingerprint=fingerprint, severity=severity, cooldown_seconds=900):
                self._repeats[key] = self._repeats.get(key, 0) + 1
                return False
            repeats = self._repeats.pop(key, 0)
        lines = [str(message)] + ([f"Repeated {repeats} additional times since the last alert"] if repeats else [])
        sent = self._send("⚠️ Warning" if severity == "WARNING" else "❌ Error", lines)
        if not sent:
            with self._lock:
                self._throttle.clear(key)
                self._repeats[key] = repeats
        return sent

    def send_info(self, message: str) -> bool:
        if str(message).startswith("Bot started |"):
            fields = dict(part.split("=", 1) for part in str(message).split(" | ")[1:] if "=" in part)
            cfg = load_json(Path(__file__).resolve().parents[1]/"config.json", {})
            symbols = cfg.get("lebprime_v3_4_0", {}).get("symbols", [])
            if isinstance(symbols, dict): symbols = [s for s,v in symbols.items() if v]
            return self._send("ℹ️ Bot started", [
                "Symbols: " + (", ".join(map(str,symbols)) or fields.get("symbol", "not supplied")),
                "MT5: " + ("connected" if fields.get("connection_ok") == "True" else "unavailable"),
                "Trading permission: " + ("available" if fields.get("trade_allowed") == "True" else "unavailable"),
                "Execution pause remains controlled by the operator"])
        return self._send("ℹ️ Info", [str(message).replace(" | ", " · ")])

    def send_warning(self, message: str) -> bool:
        return self._alert("WARNING", message)

    def send_error(self, message: str) -> bool:
        return self._alert("ERROR", message)

    def send_trade_open(self, data: dict[str, Any]) -> bool:
        return self._send(f"🟢 {data.get('side') or 'Trade'} filled · {data.get('symbol') or 'Unknown symbol'} {data.get('timeframe') or ''}", [
            f"Entry {price(data.get('entry'), data)} · SL {price(data.get('sl'), data)} · TP {price(data.get('tp'), data)}",
            f"Lot {number(data.get('executed_volume') or data.get('volume'))} · Risk {money(data.get('risk_usd'))}",
            f"Zone {data.get('zone_id') or data.get('setup_family') or 'not supplied'} · {data.get('entry_label') or ''} · #{data.get('ticket') or '—'}"])

    def send_trade_close(self, data: dict[str, Any]) -> bool:
        pnl = data.get("pnl")
        try: n = float(pnl) if pnl is not None else None
        except (ValueError, TypeError): n = None
        label = "Closed · P&L unavailable" if n is None or not math.isfinite(n) else "✅ WIN" if n > 0 else "❌ LOSS" if n < 0 else "➖ BREAKEVEN"
        r = data.get("net_r", data.get("r_multiple"))
        multiple = f" ({number(r, 1, True)}R)" if r is not None else ""
        return self._send(f"{label} {money(pnl)}{multiple} · {data.get('symbol') or 'Unknown symbol'} {data.get('side') or ''}", [
            f"Exit {price(data.get('exit_price'), data)} · {data.get('close_reason') or 'Close reason unavailable'}",
            f"Zone {data.get('zone_id') or data.get('setup_family') or 'not supplied'} · #{data.get('ticket') or '—'}"])

    def send_signal(self, data: dict[str, Any]) -> bool:
        return self._send(f"📍 {data.get('symbol') or 'Unknown symbol'} {data.get('side') or ''} signal", [
            "Executed" if data.get("executed") else "Not executed",
            str(data.get("execution_blocked_reason") or data.get("execution_reason") or data.get("reason") or "No reason supplied"),
            f"Entry {price(data.get('entry'), data)} · SL {price(data.get('sl'), data)} · TP {price(data.get('tp'), data)}"])

    def send_daily_summary(self, data: dict[str, Any]) -> bool:
        if data.get("source") != "mt5_deals":
            cached = load_json(Path(__file__).resolve().parents[1]/"storage/telegram_trade_summary.json", {})
            try: stamp = to_utc(cached.get("updated_at"))
            except (ValueError, TypeError): stamp = None
            if stamp and 0 <= (utc_now()-stamp).total_seconds() <= 120 and cached.get("day") == utc_now().date().isoformat():
                data = cached
        verified = data.get("source") == "mt5_deals"
        return self._send("📊 Daily summary" if verified else "📊 Legacy daily snapshot (not broker reconciled)", [
            f"Trades {data.get('trades', '—')} · {data.get('wins', '—')}W {data.get('losses', '—') if verified else 'unknown'}L {data.get('breakevens', '—')}BE",
            f"Net {money(data.get('pnl'))} · Win rate {number(data.get('win_rate'),1)}%",
            f"Execution: {data.get('execution_state') or 'not supplied'}"])

    def send_dashboard_action(self, actor: str, action: str, target: str, details: dict[str, Any] | None = None, severity: str = "INFO") -> bool:
        body = f"{actor} · {action} · {target}"
        if severity.upper() in {"ERROR", "CRITICAL", "WARNING"}:
            return self._alert("WARNING" if severity.upper() == "WARNING" else "ERROR", body)
        return self._send("ℹ️ Dashboard action", [body, str((details or {}).get("reason") or "Recorded in audit history")])

    def send_heartbeat(self, data: dict[str, Any]) -> bool:
        return self._send("💓 Health", [f"Status: {data.get('status') or 'unknown'}", f"Symbol: {data.get('symbol') or 'not supplied'}", f"Spread: {number(data.get('spread'))}"])
