"""MT5 gateway: a small authenticated server next to the MT5 terminal.

Runs under Windows Python inside Wine (the only place the MetaTrader5 package
works on Linux) and exposes an explicit list of MT5 operations to the bot's
native Linux Python over 127.0.0.1. Standard library + MetaTrader5 only.

Start it from the project root:

    wine python.exe -m trading_bot.mt5_transport.gateway_server

Safety rules enforced here, independent of the bot:
  * only allow-listed operations with validated arguments;
  * every frame is HMAC-signed with the shared key, time-limited and single-use;
  * order_send is journalled durably by request id and is never sent twice for
    the same id; an id that was in flight when the gateway died is reported as
    "ambiguous" and is never resent;
  * trading gate: ``disabled`` (default), ``reduce_only`` or ``enabled``;
  * demo-only by default, and the account must match the expected login/server;
  * requests whose deadline has passed are not sent to the broker.

Configuration comes from environment variables (see deploy/linux/config/gateway.env.example).
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from trading_bot.mt5_transport import protocol  # type: ignore[no-redef]
else:
    from . import protocol

import MetaTrader5 as mt5  # noqa: E402  (Windows-only; this file runs under Wine)


def _log(event: str, **fields: Any) -> None:
    record = {"at": protocol.utc_now_iso(), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class GatewayConfig:
    def __init__(self) -> None:
        self.bind = _env("LEBPRIME_MT5_GATEWAY_BIND", protocol.DEFAULT_HOST)
        self.port = int(_env("LEBPRIME_MT5_GATEWAY_PORT", str(protocol.DEFAULT_PORT)))
        self.key_file = _env("LEBPRIME_MT5_GATEWAY_KEY_FILE", r"Z:\etc\lebprime\gateway.key")
        self.terminal_path = _env("LEBPRIME_MT5_TERMINAL_PATH") or None
        self.expected_login = int(_env("LEBPRIME_MT5_EXPECTED_LOGIN", "0") or 0) or None
        self.expected_server = _env("LEBPRIME_MT5_EXPECTED_SERVER") or None
        self.login = int(_env("MT5_LOGIN", "0") or 0) or None
        self.password = _env("MT5_PASSWORD") or None
        self.server = _env("MT5_SERVER") or None
        self.trading = _env("LEBPRIME_GATEWAY_TRADING", "disabled").lower()
        if self.trading not in {"disabled", "reduce_only", "enabled"}:
            raise SystemExit(f"LEBPRIME_GATEWAY_TRADING must be disabled, reduce_only or enabled, not {self.trading!r}")
        self.demo_only = _env("LEBPRIME_GATEWAY_DEMO_ONLY", "1") != "0"
        self.symbols = {s.strip() for s in _env("LEBPRIME_GATEWAY_SYMBOLS", "XAUUSD").split(",") if s.strip()}
        # "1.0" caps every symbol; "XAUUSD=1,XAGUSD=1,US30=5,USTEC=5,*=1" caps per symbol ("*" = default).
        self.max_volume, self.max_volume_by_symbol = _parse_volume_caps(_env("LEBPRIME_GATEWAY_MAX_VOLUME", "1.0"))
        self.journal_path = Path(_env("LEBPRIME_GATEWAY_JOURNAL", r"Z:\var\lib\lebprime\gateway\executions.jsonl"))
        if self.bind not in {"127.0.0.1", "::1", "localhost"} and _env("LEBPRIME_GATEWAY_ALLOW_NON_LOOPBACK") != "1":
            raise SystemExit("Refusing to bind the MT5 gateway to a non-loopback address")


def _parse_volume_caps(raw: str) -> tuple[float, dict[str, float]]:
    default = 1.0
    caps: dict[str, float] = {}
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if "=" not in part:
            default = float(part)
            continue
        symbol, value = (item.strip() for item in part.split("=", 1))
        if symbol == "*":
            default = float(value)
        else:
            caps[symbol] = float(value)
    return default, caps


# --------------------------------------------------------------------------- execution journal

class ExecutionJournal:
    """Append-only, fsync'd record of every order_send request id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    self._entries[str(entry.get("request_id"))] = entry
        for request_id, entry in self._entries.items():
            if entry.get("state") == "in_flight":
                # The gateway stopped between sending and recording the result.
                entry["state"] = "ambiguous"
                _log("execution_ambiguous_after_restart", request_id=request_id)

    def _append(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._entries[entry["request_id"]] = entry

    def claim(self, request_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        """Mark a request in flight. Returns the existing entry if the id was already used."""
        with self._lock:
            existing = self._entries.get(request_id)
            if existing is not None:
                return existing
            self._append({"request_id": request_id, "state": "in_flight", "at": protocol.utc_now_iso(), "request": request})
            return None

    def finish(self, request_id: str, state: str, reply: dict[str, Any]) -> None:
        with self._lock:
            self._append({"request_id": request_id, "state": state, "at": protocol.utc_now_iso(), "reply": reply})

    def status(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get(request_id)
        if entry is None:
            return {"state": "unknown"}
        return {"state": entry.get("state"), "reply": entry.get("reply")}

    def counts(self) -> dict[str, int]:
        with self._lock:
            result: dict[str, int] = {}
            for entry in self._entries.values():
                result[str(entry.get("state"))] = result.get(str(entry.get("state")), 0) + 1
            return result


# --------------------------------------------------------------------------- validation

class Rejected(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _num(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise Rejected(protocol.ERR_REQUEST_REJECTED, "symbol must be a short string")
    return value


def _time_arg(value: Any) -> Any:
    if isinstance(value, datetime) or _is_int(value):
        return value
    raise Rejected(protocol.ERR_REQUEST_REJECTED, "date arguments must be datetimes or integer timestamps")


def _count(value: Any, limit: int = 200_000) -> int:
    if not _is_int(value) or value < 0 or value > limit:
        raise Rejected(protocol.ERR_REQUEST_REJECTED, f"count must be an integer between 0 and {limit}")
    return value


_FILTER_KWARGS = {"symbol": _symbol, "group": _symbol, "ticket": int, "position": int}

_REQUEST_FIELDS: dict[str, Callable[[Any], bool]] = {
    "action": _is_int,
    "magic": _is_int,
    "order": _is_int,
    "symbol": lambda v: isinstance(v, str) and 0 < len(v) <= 64,
    "volume": _num,
    "price": _num,
    "stoplimit": _num,
    "sl": _num,
    "tp": _num,
    "deviation": _is_int,
    "type": _is_int,
    "type_filling": _is_int,
    "type_time": _is_int,
    "expiration": lambda v: _is_int(v) or isinstance(v, datetime),
    "comment": lambda v: isinstance(v, str) and len(v) <= 64,
    "position": _is_int,
    "position_by": _is_int,
}


def _filter_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in kwargs.items():
        checker = _FILTER_KWARGS.get(key)
        if checker is None:
            raise Rejected(protocol.ERR_REQUEST_REJECTED, f"unexpected keyword {key!r}")
        if checker is int:
            if not _is_int(value):
                raise Rejected(protocol.ERR_REQUEST_REJECTED, f"{key} must be an integer")
            clean[key] = value
        else:
            clean[key] = checker(value)
    return clean


def _trade_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Rejected(protocol.ERR_REQUEST_REJECTED, "trade request must be an object")
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        checker = _REQUEST_FIELDS.get(key)
        if checker is None:
            raise Rejected(protocol.ERR_REQUEST_REJECTED, f"unexpected trade request field {key!r}")
        if not checker(value):
            raise Rejected(protocol.ERR_REQUEST_REJECTED, f"invalid value for trade request field {key!r}")
        clean[key] = value
    if "action" not in clean:
        raise Rejected(protocol.ERR_REQUEST_REJECTED, "trade request needs an action")
    return clean


# --------------------------------------------------------------------------- gateway

class Gateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.key = protocol.load_key(config.key_file)
        self.journal = ExecutionJournal(config.journal_path)
        self.mt5_lock = threading.RLock()
        self.nonces = protocol.NonceCache()
        self.nonce_lock = threading.Lock()
        self.started_at = time.time()
        self.initialized = False
        self.ops: dict[str, Callable[[list[Any], dict[str, Any]], Any]] = {
            "initialize": self.op_initialize,
            "login": self.op_login,
            "shutdown": lambda a, k: None,  # the gateway owns the terminal session
            "version": lambda a, k: mt5.version(),
            "terminal_info": lambda a, k: mt5.terminal_info(),
            "account_info": lambda a, k: mt5.account_info(),
            "symbols_total": lambda a, k: mt5.symbols_total(),
            "symbols_get": lambda a, k: mt5.symbols_get(**_filter_kwargs(k)),
            "symbol_info": lambda a, k: mt5.symbol_info(_symbol(a[0])),
            "symbol_info_tick": lambda a, k: mt5.symbol_info_tick(_symbol(a[0])),
            "symbol_select": self.op_symbol_select,
            "copy_rates_from": lambda a, k: mt5.copy_rates_from(_symbol(a[0]), self._int(a[1]), _time_arg(a[2]), _count(a[3])),
            "copy_rates_from_pos": lambda a, k: mt5.copy_rates_from_pos(_symbol(a[0]), self._int(a[1]), _count(a[2]), _count(a[3])),
            "copy_rates_range": lambda a, k: mt5.copy_rates_range(_symbol(a[0]), self._int(a[1]), _time_arg(a[2]), _time_arg(a[3])),
            "copy_ticks_from": lambda a, k: mt5.copy_ticks_from(_symbol(a[0]), _time_arg(a[1]), _count(a[2], 1_000_000), self._int(a[3])),
            "copy_ticks_range": lambda a, k: mt5.copy_ticks_range(_symbol(a[0]), _time_arg(a[1]), _time_arg(a[2]), self._int(a[3])),
            "orders_total": lambda a, k: mt5.orders_total(),
            "orders_get": lambda a, k: mt5.orders_get(**_filter_kwargs(k)),
            "positions_total": lambda a, k: mt5.positions_total(),
            "positions_get": lambda a, k: mt5.positions_get(**_filter_kwargs(k)),
            "history_orders_total": lambda a, k: mt5.history_orders_total(_time_arg(a[0]), _time_arg(a[1])),
            "history_orders_get": self.op_history("history_orders_get"),
            "history_deals_total": lambda a, k: mt5.history_deals_total(_time_arg(a[0]), _time_arg(a[1])),
            "history_deals_get": self.op_history("history_deals_get"),
            "order_calc_margin": lambda a, k: mt5.order_calc_margin(self._int(a[0]), _symbol(a[1]), self._num(a[2]), self._num(a[3])),
            "order_calc_profit": lambda a, k: mt5.order_calc_profit(self._int(a[0]), _symbol(a[1]), self._num(a[2]), self._num(a[3]), self._num(a[4])),
            "order_check": lambda a, k: mt5.order_check(_trade_request(a[0])),
            "health": lambda a, k: self.health(),
        }

    @staticmethod
    def _int(value: Any) -> int:
        if not _is_int(value):
            raise Rejected(protocol.ERR_REQUEST_REJECTED, "expected an integer argument")
        return value

    @staticmethod
    def _num(value: Any) -> float:
        if not _num(value):
            raise Rejected(protocol.ERR_REQUEST_REJECTED, "expected a numeric argument")
        return value

    def op_history(self, name: str) -> Callable[[list[Any], dict[str, Any]], Any]:
        def _run(args: list[Any], kwargs: dict[str, Any]) -> Any:
            func = getattr(mt5, name)
            clean = _filter_kwargs(kwargs)
            if args:
                if len(args) != 2:
                    raise Rejected(protocol.ERR_REQUEST_REJECTED, f"{name} takes date_from and date_to")
                return func(_time_arg(args[0]), _time_arg(args[1]), **clean)
            return func(**clean)

        return _run

    def op_symbol_select(self, args: list[Any], kwargs: dict[str, Any]) -> Any:
        enable = args[1] if len(args) > 1 else kwargs.get("enable", True)
        if not isinstance(enable, bool):
            raise Rejected(protocol.ERR_REQUEST_REJECTED, "enable must be true or false")
        return mt5.symbol_select(_symbol(args[0]), enable)

    # ---- session

    def _check_account(self, login: int | None, server: str | None) -> None:
        if self.config.expected_login and login and int(login) != self.config.expected_login:
            raise Rejected(protocol.ERR_WRONG_ACCOUNT, f"login {login} is not the account this gateway serves")
        if self.config.expected_server and server and server != self.config.expected_server:
            raise Rejected(protocol.ERR_WRONG_ACCOUNT, f"server {server!r} is not the expected server")

    def _session_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        login = kwargs.get("login") or self.config.login
        server = kwargs.get("server") or self.config.server
        password = kwargs.get("password") or self.config.password
        self._check_account(login, server)
        clean: dict[str, Any] = {}
        if login:
            clean["login"] = int(login)
        if password:
            clean["password"] = str(password)
        if server:
            clean["server"] = str(server)
        if _is_int(kwargs.get("timeout")):
            clean["timeout"] = kwargs["timeout"]
        return clean

    def op_initialize(self, args: list[Any], kwargs: dict[str, Any]) -> Any:
        # The client's terminal path is a Linux or Windows path that means nothing
        # inside the Wine prefix, so the gateway always uses its own.
        session = self._session_kwargs({k: v for k, v in kwargs.items() if k not in {"path", "portable"}})
        if self.initialized:
            account = mt5.account_info()
            if account is not None and (not session.get("login") or account.login == session["login"]):
                return True
        init_kwargs = dict(session)
        if self.config.terminal_path:
            init_kwargs["path"] = self.config.terminal_path
        ok = bool(mt5.initialize(**init_kwargs))
        self.initialized = ok
        _log("mt5_initialize", ok=ok, login=session.get("login"), server=session.get("server"), last_error=mt5.last_error())
        return ok

    def op_login(self, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if args:
            kwargs = {**kwargs, "login": args[0]}
        session = self._session_kwargs(kwargs)
        account = mt5.account_info()
        if account is not None and account.login == session.get("login") and (not session.get("server") or account.server == session["server"]):
            return True
        login = session.pop("login", None)
        if not login:
            raise Rejected(protocol.ERR_REQUEST_REJECTED, "login needs an account number")
        return bool(mt5.login(login, **session))

    def health(self) -> dict[str, Any]:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        ticks = {}
        for symbol in sorted(self.config.symbols):
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                ticks[symbol] = {"time_msc": int(tick.time_msc), "bid": float(tick.bid), "ask": float(tick.ask)}
        return {
            "gateway_uptime_seconds": round(time.time() - self.started_at, 1),
            "initialized": self.initialized,
            "terminal_connected": bool(getattr(terminal, "connected", False)),
            "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
            "terminal_ping_ms": round(int(getattr(terminal, "ping_last", 0) or 0) / 1000.0, 1),
            "account_login": getattr(account, "login", None),
            "account_server": getattr(account, "server", None),
            "account_is_demo": getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO,
            "expected_login_ok": (self.config.expected_login is None) or (getattr(account, "login", None) == self.config.expected_login),
            "trading_gate": self.config.trading,
            "demo_only": self.config.demo_only,
            "ticks": ticks,
            "executions": self.journal.counts(),
            "last_error": list(mt5.last_error()),
        }

    # ---- execution

    def _execution_gate(self, request: dict[str, Any]) -> None:
        action = request.get("action")
        reduce_only_actions = {mt5.TRADE_ACTION_SLTP, mt5.TRADE_ACTION_REMOVE}
        closes_position = action == mt5.TRADE_ACTION_DEAL and _is_int(request.get("position")) and request["position"] > 0
        if self.config.trading == "disabled":
            raise Rejected(protocol.ERR_TRADING_DISABLED, "trading is disabled at the gateway (LEBPRIME_GATEWAY_TRADING=disabled)")
        if self.config.trading == "reduce_only" and not (action in reduce_only_actions or closes_position):
            raise Rejected(protocol.ERR_TRADING_DISABLED, "gateway is reduce-only: only stop changes, cancels and closes are allowed")
        symbol = request.get("symbol")
        if symbol is not None and symbol not in self.config.symbols:
            raise Rejected(protocol.ERR_REQUEST_REJECTED, f"symbol {symbol!r} is not allow-listed at the gateway")
        volume = request.get("volume")
        cap = self.config.max_volume_by_symbol.get(str(symbol), self.config.max_volume)
        if volume is not None and not (0 < float(volume) <= cap):
            raise Rejected(protocol.ERR_REQUEST_REJECTED, f"volume {volume} outside (0, {cap}] for {symbol}")
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected:
            raise Rejected(protocol.ERR_TERMINAL_DISCONNECTED, "MT5 terminal is not connected to the broker")
        account = mt5.account_info()
        if account is None:
            raise Rejected(protocol.ERR_TERMINAL_DISCONNECTED, "no account information from the terminal")
        if self.config.expected_login and account.login != self.config.expected_login:
            raise Rejected(protocol.ERR_WRONG_ACCOUNT, f"terminal is logged into {account.login}, not the expected account")
        if self.config.expected_server and account.server != self.config.expected_server:
            raise Rejected(protocol.ERR_WRONG_ACCOUNT, f"terminal is on server {account.server!r}, not the expected server")
        if self.config.demo_only and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise Rejected(protocol.ERR_NOT_DEMO, "gateway is demo-only and this is not a demo account")

    def order_send(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = str(body.get("request_id") or "")
        if len(request_id) < 16:
            return {"ok": False, "code": protocol.ERR_REQUEST_REJECTED, "error": "order_send needs a request_id"}
        try:
            request = _trade_request(protocol.decode_value((body.get("args") or [None])[0]))
        except Rejected as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        with self.mt5_lock:
            existing = self.journal.claim(request_id, protocol.encode_value(request))
            if existing is not None:
                state = existing.get("state")
                if state in {"done", "rejected"} and existing.get("reply"):
                    return existing["reply"]
                return {"ok": False, "code": protocol.ERR_EXECUTION_AMBIGUOUS, "error": f"request {request_id} is {state}; not sending it again"}
            try:
                if time.time() > float(body.get("deadline", 0)):
                    raise Rejected(protocol.ERR_DEADLINE_EXCEEDED, "deadline passed before the order reached the broker")
                self._execution_gate(request)
            except Rejected as exc:
                reply = {"ok": False, "code": exc.code, "error": str(exc)}
                self.journal.finish(request_id, "rejected", reply)
                _log("order_rejected_at_gateway", request_id=request_id, code=exc.code, reason=str(exc))
                return reply
            result = mt5.order_send(request)
            reply = {"ok": True, "result": protocol.encode_value(result), "last_error": list(mt5.last_error())}
            self.journal.finish(request_id, "done", reply)
            _log("order_send", request_id=request_id, retcode=getattr(result, "retcode", None), order=getattr(result, "order", None), deal=getattr(result, "deal", None))
            return reply

    # ---- dispatch

    def handle(self, body: dict[str, Any]) -> dict[str, Any]:
        op = body.get("op")
        if op == "order_send":
            return self.order_send(body)
        if op == "execution_status":
            return {"ok": True, "result": self.journal.status(str(body.get("request_id") or ""))}
        func = self.ops.get(str(op))
        if func is None:
            return {"ok": False, "code": protocol.ERR_REQUEST_REJECTED, "error": f"operation {op!r} is not allowed"}
        args = protocol.decode_value(body.get("args") or [])
        kwargs = protocol.decode_value(body.get("kwargs") or {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return {"ok": False, "code": protocol.ERR_REQUEST_REJECTED, "error": "args must be a list and kwargs an object"}
        with self.mt5_lock:
            if time.time() > float(body.get("deadline", 0)):
                return {"ok": False, "code": protocol.ERR_DEADLINE_EXCEEDED, "error": "deadline passed while queued"}
            try:
                result = func(args, kwargs)
            except Rejected as exc:
                return {"ok": False, "code": exc.code, "error": str(exc)}
            except IndexError:
                return {"ok": False, "code": protocol.ERR_REQUEST_REJECTED, "error": f"missing arguments for {op}"}
            return {"ok": True, "result": protocol.encode_value(result), "last_error": list(mt5.last_error())}


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        gateway = self.server.gateway
        sock: socket.socket = self.request
        sock.settimeout(300)
        while True:
            try:
                body =protocol.recv_frame(sock, gateway.key, _LockedNonces(gateway))
            except (ConnectionError, socket.timeout, OSError):
                return
            except protocol.ProtocolError as exc:
                _log("protocol_error", peer=str(self.client_address), error=str(exc))
                return
            try:
                reply = gateway.handle(body)
            except Exception as exc:  # noqa: BLE001 - report, never crash the gateway
                _log("operation_failed", op=body.get("op"), error=repr(exc))
                reply = {"ok": False, "code": protocol.ERR_REQUEST_REJECTED, "error": f"gateway error: {exc}"}
            reply["id"] = body.get("id")
            try:
                protocol.send_frame(sock, gateway.key, reply)
            except OSError:
                return


class _LockedNonces(protocol.NonceCache):
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def check_and_add(self, nonce: str) -> bool:
        with self.gateway.nonce_lock:
            return self.gateway.nonces.check_and_add(nonce)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gateway: Gateway) -> None:
        self.gateway = gateway
        super().__init__(address, _Handler)


def main() -> int:
    config = GatewayConfig()
    gateway = Gateway(config)
    with gateway.mt5_lock:
        try:
            gateway.op_initialize([], {})
        except Rejected as exc:
            _log("startup_initialize_rejected", error=str(exc))
    server = _Server((config.bind, config.port), gateway)
    _log(
        "gateway_listening",
        bind=config.bind,
        port=config.port,
        trading=config.trading,
        demo_only=config.demo_only,
        symbols=sorted(config.symbols),
        expected_login=config.expected_login,
        mt5_package=getattr(mt5, "__version__", None),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
