"""Telegram remote-control poller and command handler for the trading bot."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
from pathlib import Path
from typing import Any

import requests
from requests import Response

from dashboard.services import DashboardDataService
from services.audit_service import AuditService
from services.config_manager import ConfigManager
from services.control_state import ControlStateService
from services.manual_trade_service import ManualTradeService
from services.operator_service import OperatorService
from utils import load_json, save_json_atomic, utc_now


class TelegramCommandService:
    """Receive and execute authorized Telegram bot commands via long polling."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        control_state: ControlStateService,
        dashboard_data: DashboardDataService,
        audit_service: AuditService,
        operator_service: OperatorService,
        manual_trade_service: ManualTradeService,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.control_state = control_state
        self.dashboard_data = dashboard_data
        self.audit_service = audit_service
        self.operator_service = operator_service
        self.manual_trade_service = manual_trade_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._pending_confirmations: dict[str, dict[str, Any]] = {}
        self._polling_lock_path = self.base_dir / "storage" / "telegram_remote_control.lock"
        self._owns_polling_lock = False
        self._poll_session = requests.Session()
        self._poll_error_count = 0
        self._poll_dns_error_count = 0

    def start(self) -> None:
        """Start the Telegram polling loop if enabled."""
        if not self._claim_polling_lock():
            return
        if self._thread and self._thread.is_alive():
            return
        self._poll_session = requests.Session()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="telegram-remote-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the Telegram polling loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._release_polling_lock()
        try:
            self._poll_session.close()
        except Exception:
            pass

    def _poll_loop(self) -> None:
        """Long-poll Telegram for admin commands."""
        try:
            while not self._stop_event.is_set():
                try:
                    config = self.config_manager.load_runtime()
                    telegram_cfg = config.get("telegram", {})
                    if not self._remote_control_enabled(telegram_cfg):
                        self._stop_event.wait(max(2.0, float(telegram_cfg.get("poll_interval_seconds", 3.0))))
                        continue

                    token = str(telegram_cfg.get("bot_token", "") or "")
                    if not token:
                        self._stop_event.wait(max(2.0, float(telegram_cfg.get("poll_interval_seconds", 3.0))))
                        continue
                    timeout_seconds = max(5, int(telegram_cfg.get("polling_timeout_seconds", 20)))
                    response = self._poll_session.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={
                            "timeout": timeout_seconds,
                            "offset": self._offset,
                            "allowed_updates": ["message"],
                        },
                        timeout=timeout_seconds + 5,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    self._poll_error_count = 0
                    self._poll_dns_error_count = 0
                    for item in payload.get("result", []):
                        update_id = int(item.get("update_id", 0))
                        self._offset = max(self._offset, update_id + 1)
                        message = item.get("message")
                        if isinstance(message, dict):
                            self._handle_message(message, config)
                except requests.HTTPError as exc:
                    response = exc.response
                    self._handle_poll_error(exc, response, config if "config" in locals() else None)
                except Exception as exc:
                    self._handle_poll_error(exc, None, config if "config" in locals() else None)
        finally:
            self._release_polling_lock()

    def _handle_poll_error(self, exc: Exception, response: Response | None, config: dict[str, Any] | None) -> None:
        """Log Telegram poll failures with bounded backoff so transient issues do not spam logs."""
        self._poll_error_count += 1
        telegram_cfg = (config or {}).get("telegram", {}) if isinstance(config, dict) else {}
        base_delay = max(5.0, float(telegram_cfg.get("poll_interval_seconds", 3.0)))
        backoff_seconds = min(60.0, base_delay * (2 ** min(self._poll_error_count - 1, 4)))
        status_code = getattr(response, "status_code", None)
        body = ""
        if response is not None:
            try:
                body = (response.text or "")[:240]
            except Exception:
                body = ""

        level = "WARNING"
        action = "telegram_remote_poll_error"
        details: dict[str, Any] = {
            "error": str(exc),
            "status_code": status_code,
            "backoff_seconds": backoff_seconds,
        }
        if body:
            details["body"] = body

        failure_text = " ".join(
            part
            for part in [str(exc), body]
            if part
        ).lower()
        dns_failure = any(
            marker in failure_text
            for marker in (
                "getaddrinfo failed",
                "nameorresolutionerror",
                "failed to resolve",
                "temporary failure in name resolution",
                "nodename nor servname provided",
            )
        )

        if status_code in {401, 403}:
            level = "ERROR"
            action = "telegram_remote_poll_unauthorized"
        elif status_code == 409:
            action = "telegram_remote_poll_conflict"
        elif status_code == 429:
            action = "telegram_remote_poll_rate_limited"
            backoff_seconds = min(60.0, max(backoff_seconds, 15.0))
        elif dns_failure:
            self._poll_dns_error_count += 1
            level = "ERROR"
            action = "telegram_remote_poll_unreachable"
            details["failure_class"] = "dns_resolution_failure"
            details["dns_error_count"] = self._poll_dns_error_count
            backoff_seconds = min(900.0, max(backoff_seconds, 300.0 if self._poll_dns_error_count >= 3 else 60.0))
        else:
            self._poll_dns_error_count = 0

        self.audit_service.log_action("telegram_poller", action, "telegram", details, level=level)
        self._stop_event.wait(backoff_seconds)

    def _handle_message(self, message: dict[str, Any], config: dict[str, Any]) -> None:
        """Parse and execute a Telegram command message."""
        telegram_cfg = config.get("telegram", {})
        text = str(message.get("text", "") or "").strip()
        if not text.startswith("/"):
            return

        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        actor = f"telegram:{chat_id}:{user_id}"
        if not self._is_authorized(telegram_cfg, chat_id, user_id):
            self.audit_service.log_action(
                actor,
                "telegram_unauthorized_attempt",
                "telegram",
                {"chat_id": chat_id, "user_id": user_id, "text": text},
                level="WARNING",
            )
            return

        try:
            self.audit_service.log_action(
                actor,
                "telegram_command_received",
                "telegram",
                {"chat_id": chat_id, "user_id": user_id, "text": text},
                level="INFO",
            )
            response_text = self._dispatch_command(message, config)
            self.audit_service.log_action(
                actor,
                "telegram_command_executed",
                "telegram",
                {"chat_id": chat_id, "user_id": user_id, "text": text, "response": response_text[:240]},
                level="INFO",
            )
        except Exception as exc:
            response_text = f"Command failed: {exc}"
            self.audit_service.log_action(
                actor,
                "telegram_command_error",
                "telegram",
                {"chat_id": chat_id, "user_id": user_id, "text": text, "error": str(exc)},
                level="WARNING",
            )
        self._reply(config, chat_id, response_text, reply_to_message_id=message.get("message_id"))

    def _dispatch_command(self, message: dict[str, Any], config: dict[str, Any]) -> str:
        """Route an authorized Telegram command to the shared backend service layer."""
        text = str(message.get("text", "") or "").strip()
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        actor = f"telegram:{chat_id}:{user_id}"
        dangerous = {item.upper() for item in config.get("telegram", {}).get("confirmation_required_commands", [])}

        if command == "/confirm":
            if not args:
                return "Usage: /confirm <token>"
            return self._confirm_pending(actor, chat_id, user_id, args[0], config)

        if command in {"/start", "/help"}:
            return self._help_text(config)
        if command == "/status":
            return self._status_text(config)
        if command == "/startbot":
            return self._result_message(self.operator_service.start_bot(actor))
        if command == "/stopbot":
            return self._result_message(self.operator_service.stop_bot(actor))
        if command == "/pause":
            return self._result_message(self.operator_service.pause_execution(actor))
        if command == "/resume":
            return self._result_message(self.operator_service.resume_execution(actor))
        if command == "/dry":
            return self._result_message(self.operator_service.switch_mode(actor, "DRY_RUN"))
        if command == "/live":
            return self._confirm_or_execute("LIVE", actor, chat_id, user_id, {"command": command}, config)
        if command == "/killswitch_on":
            return self._result_message(self.operator_service.set_kill_switch(actor, True, "telegram_remote"))
        if command == "/killswitch_off":
            if "KILLSWITCH_OFF" in dangerous:
                return self._confirm_or_execute("KILLSWITCH_OFF", actor, chat_id, user_id, {"command": command}, config)
            return self._result_message(self.operator_service.set_kill_switch(actor, False, None))
        if command == "/positions":
            result = self.manual_trade_service.list_positions(actor, "telegram")
            return self._positions_text(result)
        if command == "/close":
            if not args:
                return "Usage: /close <ticket>"
            return self._confirm_or_execute("CLOSE", actor, chat_id, user_id, {"command": command, "ticket": int(args[0])}, config)
        if command == "/closeall":
            return self._confirm_or_execute("CLOSEALL", actor, chat_id, user_id, {"command": command}, config)
        if command == "/strategy":
            if len(args) != 2:
                return "Usage: /strategy <name> on|off"
            enabled = args[1].strip().lower() == "on"
            return self._result_message(self.operator_service.toggle_strategy(actor, args[0], enabled))
        if command == "/family":
            if len(args) not in {2, 3}:
                return "Usage: /family <name> on|off [live_on|live_off]"
            enabled = args[1].strip().lower() == "on"
            live_allowed = None
            if len(args) == 3:
                live_allowed = args[2].strip().lower() in {"live_on", "on", "true"}
            return self._result_message(self.operator_service.toggle_family(actor, args[0], enabled, live_allowed))
        if command == "/preset":
            if len(args) != 1:
                return "Usage: /preset <safe_mode|balanced_mode|scalping_mode>"
            return self._result_message(self.operator_service.apply_preset(actor, args[0]))
        if command in {"/buy", "/sell"}:
            if not bool(config.get("telegram", {}).get("manual_trade_commands_enabled", False)):
                return "Manual trade commands are disabled."
            if len(args) < 3:
                return f"Usage: {command} <volume> <sl> <tp> [comment]"
            side = "BUY" if command == "/buy" else "SELL"
            payload = {
                "command": command,
                "side": side,
                "volume": float(args[0]),
                "sl": float(args[1]),
                "tp": float(args[2]),
                "comment": " ".join(args[3:]).strip() or None,
            }
            return self._confirm_or_execute("BUY" if side == "BUY" else "SELL", actor, chat_id, user_id, payload, config)
        return "Unsupported command."

    def _confirm_or_execute(
        self,
        action_key: str,
        actor: str,
        chat_id: str,
        user_id: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> str:
        """Require confirmation for dangerous commands when configured."""
        dangerous = {item.upper() for item in config.get("telegram", {}).get("confirmation_required_commands", [])}
        if action_key.upper() not in dangerous:
            return self._execute_confirmed_action(actor, payload, config)

        token = secrets.token_hex(3).upper()
        ttl = max(30, int(config.get("telegram", {}).get("confirmation_ttl_seconds", 120)))
        self._pending_confirmations[token] = {
            "actor": actor,
            "chat_id": chat_id,
            "user_id": user_id,
            "expires_at": utc_now().timestamp() + ttl,
            "payload": payload,
        }
        return f"Confirm {action_key.upper()} with /confirm {token} within {ttl} seconds."

    def _confirm_pending(
        self,
        actor: str,
        chat_id: str,
        user_id: str,
        token: str,
        config: dict[str, Any],
    ) -> str:
        """Execute a pending dangerous command after confirmation."""
        pending = self._pending_confirmations.pop(str(token).strip().upper(), None)
        if not pending:
            return "Confirmation token not found."
        if pending["chat_id"] != chat_id or pending["user_id"] != user_id:
            return "This confirmation token belongs to a different admin session."
        if float(pending["expires_at"]) < utc_now().timestamp():
            return "Confirmation token expired."
        return self._execute_confirmed_action(actor, pending["payload"], config)

    def _execute_confirmed_action(self, actor: str, payload: dict[str, Any], config: dict[str, Any]) -> str:
        """Run a confirmed Telegram action via shared backend services."""
        command = str(payload.get("command", "")).lower()
        if command == "/live":
            return self._result_message(self.operator_service.switch_mode(actor, "LIVE", confirmation_text="LIVE"))
        if command == "/close":
            return self._result_message(self.manual_trade_service.close_position(actor, "telegram", int(payload["ticket"])))
        if command == "/closeall":
            return self._result_message(self.manual_trade_service.close_all_positions(actor, "telegram"))
        if command in {"/buy", "/sell"}:
            return self._result_message(
                self.manual_trade_service.open_market_trade(
                    actor=actor,
                    source="telegram",
                    symbol=str(config.get("mt5", {}).get("symbol", "XAUUSD")),
                    side=str(payload["side"]),
                    volume=float(payload["volume"]),
                    sl=float(payload["sl"]),
                    tp=float(payload["tp"]),
                    comment=payload.get("comment"),
                )
            )
        if command == "/killswitch_off":
            return self._result_message(self.operator_service.set_kill_switch(actor, False, None))
        raise ValueError("Unsupported confirmed command payload")

    def _status_text(self, config: dict[str, Any]) -> str:
        """Render a compact Telegram status report."""
        control = self.control_state.summarize(stale_after_seconds=int(config.get("dashboard", {}).get("stale_heartbeat_seconds", 90)))
        snapshot = self.dashboard_data.status_snapshot(
            self.base_dir / config["storage"]["database_path"],
            int(config.get("dashboard", {}).get("stale_heartbeat_seconds", 90)),
            config=config,
        )
        runtime = control.get("runtime", {})
        metrics = snapshot.get("metrics", {})
        return "\n".join(
            [
                "Status",
                f"Mode: {runtime.get('mode') or config.get('bot', {}).get('trading_mode')}",
                f"Bot Running: {bool(control.get('process_alive') or control.get('bot_running'))}",
                f"Desired State: {control.get('desired_state')}",
                f"PID: {control.get('bot_pid') or 'N/A'}",
                f"Execution Paused: {control.get('execution_paused')}",
                f"Kill Switch: {control.get('kill_switch')}",
                f"Auto Execution: {control.get('auto_execution_enabled')}",
                f"Heartbeat Status: {runtime.get('heartbeat_status')}",
                f"MT5 Connected: {runtime.get('mt5_connected')}",
                f"Trade Allowed: {runtime.get('trade_allowed')}",
                f"Session: {runtime.get('session')}",
                f"Regime: {runtime.get('regime')}",
                f"Open Positions: {metrics.get('open_positions', 0)}",
                f"Today PnL: {metrics.get('today_pnl', 0):.2f}",
                f"Heartbeat: {runtime.get('heartbeat_at') or 'N/A'}",
            ]
        )

    @staticmethod
    def _positions_text(result: dict[str, Any]) -> str:
        """Render a compact Telegram positions response."""
        if not result.get("ok"):
            return f"Positions request failed: {result.get('message')}"
        positions = result.get("positions", [])
        if not positions:
            return "No open bot-managed positions."
        lines = ["Open Positions"]
        for position in positions[:10]:
            lines.append(
                f"{position['ticket']} | {position['symbol']} | {position['side']} | "
                f"{position['volume']:.2f} | PnL {position['profit']:.2f} | SL {position['sl']:.5f} | TP {position['tp']:.5f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _help_text(config: dict[str, Any]) -> str:
        """Render a compact Telegram help/status intro."""
        manual_enabled = bool(config.get("telegram", {}).get("manual_trade_commands_enabled", False))
        return "\n".join(
            [
                "Trading Bot Control",
                "Commands: /status /startbot /stopbot /pause /resume /live /dry /killswitch_on /killswitch_off /positions /close <ticket> /closeall /strategy <name> on|off /family <name> on|off /preset <name>",
                f"Manual trades: {'enabled' if manual_enabled else 'disabled'}",
                "Use /status to see the current bot state.",
            ]
        )

    @staticmethod
    def _result_message(result: dict[str, Any]) -> str:
        """Summarize a backend action result for Telegram replies."""
        if result.get("ok"):
            ticket = result.get("ticket") or result.get("data", {}).get("ticket")
            suffix = f" | Ticket: {ticket}" if ticket else ""
            return f"OK: {result.get('message', 'Action completed')}{suffix}"
        return f"ERROR: {result.get('message', 'Action failed')}"

    @staticmethod
    def _remote_control_enabled(telegram_cfg: dict[str, Any]) -> bool:
        """Return whether remote control polling should run."""
        return bool(
            telegram_cfg.get("enabled")
            and telegram_cfg.get("remote_control_enabled", False)
            and telegram_cfg.get("bot_token")
        )

    @staticmethod
    def _is_authorized(telegram_cfg: dict[str, Any], chat_id: str, user_id: str) -> bool:
        """Validate the Telegram chat/user against configured admin allowlists."""
        allowed_chats = {str(item).strip() for item in telegram_cfg.get("admin_chat_ids", []) if str(item).strip()}
        allowed_users = {str(item).strip() for item in telegram_cfg.get("admin_user_ids", []) if str(item).strip()}
        primary_chat = str(telegram_cfg.get("chat_id", "") or "").strip()
        if primary_chat:
            allowed_chats.add(primary_chat)
        if not allowed_chats and not allowed_users:
            return False
        return chat_id in allowed_chats or user_id in allowed_users

    def _reply(self, config: dict[str, Any], chat_id: str, text: str, reply_to_message_id: Any | None = None) -> None:
        """Send a Telegram reply message."""
        telegram_cfg = config.get("telegram", {})
        token = str(telegram_cfg.get("bot_token", "") or "")
        if not token or not chat_id:
            return
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=max(5, int(telegram_cfg.get("timeout_seconds", 5))),
            )
        except requests.RequestException:
            return

    def _claim_polling_lock(self) -> bool:
        """Claim the singleton Telegram poller lock if no other process owns it."""
        if self._owns_polling_lock:
            return True
        self._polling_lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": utc_now().isoformat(),
            "base_dir": str(self.base_dir),
        }
        while True:
            try:
                fd = os.open(str(self._polling_lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                self._owns_polling_lock = True
                self.audit_service.log_action(
                    "telegram_poller",
                    "telegram_poller_started",
                    "telegram",
                    {"pid": os.getpid()},
                    level="INFO",
                )
                return True
            except FileExistsError:
                existing: dict[str, Any] = {}
                try:
                    existing = load_json(self._polling_lock_path, {})
                except Exception:
                    existing = {}
                existing_pid = int(existing.get("pid") or 0)
                if existing_pid and self._pid_is_alive(existing_pid):
                    self.audit_service.log_action(
                        "telegram_poller",
                        "telegram_poller_already_owned",
                        "telegram",
                        {"existing_pid": existing_pid, "lock_path": str(self._polling_lock_path)},
                        level="INFO",
                    )
                    return False
                self.audit_service.log_action(
                    "telegram_poller",
                    "telegram_poller_reclaiming_stale_lock",
                    "telegram",
                    {
                        "existing_pid": existing_pid,
                        "lock_path": str(self._polling_lock_path),
                        "lock_data": existing,
                    },
                    level="WARNING",
                )
                try:
                    self._polling_lock_path.unlink(missing_ok=True)
                except Exception as exc:
                    self.audit_service.log_action(
                        "telegram_poller",
                        "telegram_poller_lock_cleanup_failed",
                        "telegram",
                        {"existing_pid": existing_pid, "error": str(exc)},
                        level="WARNING",
                    )
                    return False

    def _release_polling_lock(self) -> None:
        """Release the singleton Telegram poller lock if this process owns it."""
        if not self._owns_polling_lock:
            return
        try:
            existing = load_json(self._polling_lock_path, {})
        except Exception:
            existing = {}
        if int(existing.get("pid") or 0) == os.getpid():
            try:
                self._polling_lock_path.unlink(missing_ok=True)
            except Exception:
                pass
        self._owns_polling_lock = False

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        """Return whether a process id still appears to be active."""
        if pid <= 0:
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                try:
                    exit_code = ctypes.c_ulong()
                    if kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                        return int(exit_code.value) == 259
                finally:
                    kernel32.CloseHandle(process)
                return False
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = (result.stdout or "").strip().lower()
            if not output or "no tasks are running" in output:
                return False
            return str(int(pid)) in output
        except Exception:
            try:
                os.kill(pid, 0)
            except Exception:
                return False
            return True
