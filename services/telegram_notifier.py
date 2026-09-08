"""Telegram notification service for operational bot alerts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import urllib3

from utils import UTC, utc_now

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TelegramNotifier:
    """Send concise operational alerts via the Telegram Bot API."""

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        timeout_seconds: int = 5,
        enabled: bool = False,
    ) -> None:
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled and bool(self.token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage" if self.token else ""
        self.api_host = "api.telegram.org"
        self.last_error: str | None = None

    def _resolve_fallback_ip(self) -> str | None:
        """Resolve Telegram through public DNS-over-HTTPS when local DNS is broken."""
        if not self.token:
            return None

        headers = {"accept": "application/dns-json"}
        doh_endpoints = [
            "https://1.1.1.1/dns-query?name=api.telegram.org&type=A",
            "https://dns.google/resolve?name=api.telegram.org&type=A",
        ]
        for endpoint in doh_endpoints:
            try:
                response = requests.get(endpoint, headers=headers, timeout=self.timeout_seconds, verify=True)
                response.raise_for_status()
                payload = response.json()
                for answer in payload.get("Answer", []):
                    if int(answer.get("type", 0)) == 1 and answer.get("data"):
                        return str(answer["data"])
            except (requests.RequestException, ValueError, TypeError):
                continue
        return None

    def _send_via_ip_fallback(self, title: str, lines: list[str]) -> bool:
        """Send using a DNS-over-HTTPS-resolved IP when local DNS cannot resolve Telegram."""
        fallback_ip = self._resolve_fallback_ip()
        if not fallback_ip:
            return False

        message = "\n".join([f"{title}", f"Time: {self._timestamp()}"] + lines)
        fallback_url = f"https://{fallback_ip}/bot{self.token}/sendMessage"
        try:
            response = requests.post(
                fallback_url,
                headers={"Host": self.api_host},
                data={
                    "chat_id": self.chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout_seconds,
                verify=False,
            )
            if response.ok:
                self.last_error = None
                return True
            self.last_error = f"Telegram fallback HTTP {response.status_code}: {response.text[:200]}"
            return False
        except requests.RequestException:
            return False

    def _timestamp(self) -> str:
        """Return a consistent UTC timestamp string."""
        return utc_now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _send(self, title: str, lines: list[str]) -> bool:
        """Send a message without allowing failures to bubble out."""
        if not self.enabled:
            return False
        message = "\n".join([f"{title}", f"Time: {self._timestamp()}"] + lines)
        try:
            response = requests.post(
                self.base_url,
                data={
                    "chat_id": self.chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout_seconds,
            )
            if response.ok:
                self.last_error = None
                return True
            self.last_error = f"Telegram HTTP {response.status_code}: {response.text[:200]}"
            return self._send_via_ip_fallback(title, lines)
        except requests.RequestException as exc:
            self.last_error = str(exc)
            return self._send_via_ip_fallback(title, lines)

    def send_info(self, message: str) -> bool:
        """Send a low-severity informational message."""
        return self._send("INFO", [message])

    def send_warning(self, message: str) -> bool:
        """Send a warning message."""
        return self._send("WARNING", [message])

    def send_error(self, message: str) -> bool:
        """Send a critical error message."""
        return self._send("ERROR", [message])

    def send_trade_open(self, trade_data: dict[str, Any]) -> bool:
        """Send a trade-open alert."""
        extra_lines: list[str] = []
        if trade_data.get("setup_family"):
            extra_lines.append(f"Setup: {trade_data.get('setup_family')}")
        if trade_data.get("regime_at_entry"):
            extra_lines.append(f"Regime: {trade_data.get('regime_at_entry')}")
        if trade_data.get("session_at_entry"):
            extra_lines.append(f"Session: {trade_data.get('session_at_entry')}")
        if trade_data.get("entry_mode"):
            extra_lines.append(f"Entry Mode: {trade_data.get('entry_mode')}")
        if trade_data.get("score") is not None:
            extra_lines.append(f"Entry Score: {float(trade_data.get('score', 0.0)):.1f}")
        if trade_data.get("volume_adjusted"):
            extra_lines.append(
                f"Requested Volume: {float(trade_data.get('requested_volume', trade_data.get('volume', 0.0))):.2f}"
            )
            extra_lines.append(f"Executed Volume: {float(trade_data.get('executed_volume', trade_data.get('volume', 0.0))):.2f}")
        if trade_data.get("note"):
            extra_lines.append(f"Note: {trade_data.get('note', '')}")
        return self._send(
            "TRADE OPEN",
            [
                f"Mode: {trade_data.get('mode', 'UNKNOWN')}",
                f"Symbol: {trade_data.get('symbol', '')}",
                f"Side: {trade_data.get('side', '')}",
                f"Entry: {trade_data.get('entry', 0):.5f}",
                f"SL: {trade_data.get('sl', 0):.5f}",
                f"TP: {trade_data.get('tp', 0):.5f}",
                f"Volume: {float(trade_data.get('executed_volume', trade_data.get('volume', 0.0))):.2f}",
                f"Ticket: {trade_data.get('ticket', '')}",
            ]
            + extra_lines,
        )

    def send_trade_close(self, trade_data: dict[str, Any]) -> bool:
        """Send a trade-close alert."""
        extra_lines: list[str] = []
        if trade_data.get("setup_family"):
            extra_lines.append(f"Setup: {trade_data.get('setup_family')}")
        if trade_data.get("close_reason"):
            extra_lines.append(f"Reason: {trade_data.get('close_reason')}")
        return self._send(
            "TRADE CLOSE",
            [
                f"Mode: {trade_data.get('mode', 'UNKNOWN')}",
                f"Symbol: {trade_data.get('symbol', '')}",
                f"Side: {trade_data.get('side', '')}",
                f"Exit: {trade_data.get('exit_price', 0):.5f}",
                f"PnL: {trade_data.get('pnl', 0):.2f}",
                f"Status: {trade_data.get('status', '')}",
                f"Ticket: {trade_data.get('ticket', '')}",
            ]
            + extra_lines,
        )

    def send_signal(self, signal_data: dict[str, Any]) -> bool:
        """Send a signal-detected alert."""
        extra_lines: list[str] = []
        if signal_data.get("setup_family"):
            extra_lines.append(f"Setup: {signal_data.get('setup_family')}")
        if signal_data.get("regime_name"):
            extra_lines.append(f"Regime: {signal_data.get('regime_name')}")
        if signal_data.get("session_name"):
            extra_lines.append(f"Session: {signal_data.get('session_name')}")
        if signal_data.get("entry_mode"):
            extra_lines.append(f"Entry Mode: {signal_data.get('entry_mode')}")
        if signal_data.get("score") is not None:
            extra_lines.append(f"Score: {float(signal_data.get('score', 0.0)):.1f}")
        if signal_data.get("execution_reason"):
            extra_lines.append(f"Execution Reason: {signal_data.get('execution_reason')}")
        if signal_data.get("execution_blocked_reason"):
            extra_lines.append(f"Blocked Reason: {signal_data.get('execution_blocked_reason')}")
        if signal_data.get("volume") is not None:
            extra_lines.append(f"Volume: {float(signal_data.get('volume', 0.0)):.2f}")
        if signal_data.get("entry") is not None:
            extra_lines.append(f"Entry: {float(signal_data.get('entry', 0.0)):.5f}")
        if signal_data.get("sl") is not None:
            extra_lines.append(f"SL: {float(signal_data.get('sl', 0.0)):.5f}")
        if signal_data.get("tp") is not None:
            extra_lines.append(f"TP: {float(signal_data.get('tp', 0.0)):.5f}")
        return self._send(
            "SIGNAL",
            [
                f"Symbol: {signal_data.get('symbol', '')}",
                f"Side: {signal_data.get('side', '')}",
                f"Reason: {signal_data.get('reason', '')}",
                f"Executed: {signal_data.get('executed', False)}",
            ]
            + extra_lines,
        )

    def send_daily_summary(self, summary_data: dict[str, Any]) -> bool:
        """Send a compact daily summary."""
        return self._send(
            "DAILY SUMMARY",
            [
                f"Trades: {summary_data.get('trades', 0)}",
                f"Wins: {summary_data.get('wins', 0)}",
                f"Losses: {summary_data.get('losses', 0)}",
                f"PnL: {summary_data.get('pnl', 0):.2f}",
                f"Win Rate: {summary_data.get('win_rate', 0):.2f}%",
            ],
        )

    def send_dashboard_action(
        self,
        actor: str,
        action: str,
        target: str,
        details: dict[str, Any] | None = None,
        severity: str = "INFO",
    ) -> bool:
        """Send a dashboard control or config-change alert."""
        payload = details or {}
        lines = [
            f"Actor: {actor}",
            f"Action: {action}",
            f"Target: {target}",
        ]
        changes = payload.get("changes")
        if isinstance(changes, list) and changes:
            preview_parts: list[str] = []
            for change in changes[:5]:
                path = str(change.get("path", ""))
                before = change.get("before")
                after = change.get("after")
                preview_parts.append(f"{path}: {before} -> {after}")
            lines.append(f"Changes: {'; '.join(preview_parts)}")
            if len(changes) > 5:
                lines.append(f"More Changes: +{len(changes) - 5} more")
        for key in ["enabled", "reason", "preset", "pid", "trading_mode"]:
            if key in payload:
                lines.append(f"{key.replace('_', ' ').title()}: {payload.get(key)}")
        title = "DASHBOARD ALERT"
        severity_name = severity.upper()
        if severity_name in {"ERROR", "CRITICAL"}:
            return self._send("DASHBOARD ERROR", lines)
        if severity_name == "WARNING":
            return self._send("DASHBOARD WARNING", lines)
        return self._send(title, lines)

    def send_heartbeat(self, status_data: dict[str, Any]) -> bool:
        """Send a heartbeat or health status message."""
        return self._send(
            "HEARTBEAT",
            [
                f"Status: {status_data.get('status', '')}",
                f"Symbol: {status_data.get('symbol', '')}",
                f"Spread: {status_data.get('spread', 0)}",
                f"Trend: {status_data.get('trend', '')}",
                f"Setup: {status_data.get('setup', '')}",
            ],
        )
