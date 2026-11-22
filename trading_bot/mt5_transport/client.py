"""Linux-side stand-in for the ``MetaTrader5`` module that talks to the MT5 gateway.

``GatewayMT5`` exposes the same functions and constants the bot already uses
(``mt5.initialize``, ``mt5.symbol_info_tick``, ``mt5.order_send``,
``mt5.TRADE_RETCODE_DONE`` ...), so ``MT5Connector`` and the trading logic do
not change. Results come back as namedtuples and numpy structured arrays, the
same shapes the native package returns.

Execution safety:
  * every ``order_send`` carries a fresh request id; the gateway journals it
    durably and never sends the same id to the broker twice;
  * if the reply is lost (timeout, dropped socket) the client asks the gateway
    for that request id's outcome instead of resending blindly;
  * if the outcome cannot be established, ``order_send`` returns ``None`` and
    ``last_error()`` reports ``ERR_EXECUTION_AMBIGUOUS``; the connector then
    raises and does not retry, so positions must be reconciled first.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from typing import Any

from . import protocol
from .constants import MT5_CONSTANTS

# Functions the bot may call. Anything else raises AttributeError locally,
# and the gateway enforces its own allowlist as well.
_READ_OPS = {
    "initialize",
    "login",
    "shutdown",
    "version",
    "terminal_info",
    "account_info",
    "symbols_total",
    "symbols_get",
    "symbol_info",
    "symbol_info_tick",
    "symbol_select",
    "copy_rates_from",
    "copy_rates_from_pos",
    "copy_rates_range",
    "copy_ticks_from",
    "copy_ticks_range",
    "orders_total",
    "orders_get",
    "positions_total",
    "positions_get",
    "history_orders_total",
    "history_orders_get",
    "history_deals_total",
    "history_deals_get",
    "order_calc_margin",
    "order_calc_profit",
    "order_check",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class GatewayMT5:
    """Module-like proxy for the MetaTrader5 API over the authenticated gateway."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        key: bytes | None = None,
        *,
        call_timeout: float | None = None,
        execution_timeout: float | None = None,
        reconcile_timeout: float | None = None,
    ) -> None:
        self.__dict__.update(MT5_CONSTANTS)
        self._host = host or os.environ.get("LEBPRIME_MT5_GATEWAY_HOST", protocol.DEFAULT_HOST)
        self._port = int(port or os.environ.get("LEBPRIME_MT5_GATEWAY_PORT", protocol.DEFAULT_PORT))
        self._key = key
        self._call_timeout = call_timeout or _env_float("LEBPRIME_MT5_GATEWAY_CALL_TIMEOUT", 20.0)
        self._execution_timeout = execution_timeout or _env_float("LEBPRIME_MT5_GATEWAY_EXEC_TIMEOUT", 30.0)
        self._reconcile_timeout = reconcile_timeout or _env_float("LEBPRIME_MT5_GATEWAY_RECONCILE_TIMEOUT", 45.0)
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._last_error: tuple[int, str] = (1, "Success")
        self.__version__ = f"gateway-client/{protocol.PROTOCOL_VERSION}"
        self.transport_name = "gateway"

    # ------------------------------------------------------------------ plumbing

    def _get_key(self) -> bytes:
        if self._key is None:
            self._key = protocol.load_key()
        return self._key

    def _connect(self, timeout: float) -> socket.socket:
        if self._sock is None:
            sock = socket.create_connection((self._host, self._port), timeout=min(timeout, 10.0))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
        self._sock.settimeout(timeout)
        return self._sock

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _roundtrip(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send one request and wait for its reply. Raises OSError/ProtocolError on transport failure."""
        key = self._get_key()
        body = dict(body)
        body["id"] = body.get("id") or uuid.uuid4().hex
        body["deadline"] = time.time() + timeout
        sock = self._connect(timeout)
        try:
            protocol.send_frame(sock, key, body)
            while True:
                reply = protocol.recv_frame(sock, key)
                if reply.get("id") == body["id"]:
                    return reply
        except BaseException:
            self._drop()
            raise

    def _finish(self, reply: dict[str, Any]) -> Any:
        error = reply.get("last_error")
        if isinstance(error, list) and len(error) == 2:
            self._last_error = (int(error[0]), str(error[1]))
        if not reply.get("ok", False):
            self._last_error = (int(reply.get("code", protocol.ERR_REQUEST_REJECTED)), str(reply.get("error", "rejected")))
            return None
        return protocol.decode_value(reply.get("result"))

    def _call(self, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        body = {"op": op, "args": protocol.encode_value(list(args)), "kwargs": protocol.encode_value(kwargs)}
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._finish(self._roundtrip(body, self._call_timeout))
                except socket.timeout:
                    self._last_error = (protocol.ERR_GATEWAY_TIMEOUT, f"gateway did not answer {op} within {self._call_timeout:.0f}s")
                    return None
                except (OSError, protocol.ProtocolError) as exc:
                    # Read-only calls are safe to repeat once on a fresh connection.
                    self._last_error = (protocol.ERR_GATEWAY_UNAVAILABLE, f"gateway unavailable for {op}: {exc}")
                    if attempt == 2:
                        return None
            return None

    # ------------------------------------------------------------------ execution

    def order_send(self, request: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Send a trade request exactly once, reconciling through the gateway journal on lost replies."""
        if args or kwargs:
            raise TypeError("order_send takes a single request dict")
        request_id = uuid.uuid4().hex
        body = {"op": "order_send", "args": [protocol.encode_value(dict(request))], "kwargs": {}, "request_id": request_id}
        with self._lock:
            try:
                return self._finish(self._roundtrip(body, self._execution_timeout))
            except (OSError, protocol.ProtocolError) as exc:
                first_error = exc
            return self._reconcile(request_id, body, first_error)

    def _reconcile(self, request_id: str, body: dict[str, Any], first_error: BaseException) -> Any:
        deadline = time.monotonic() + self._reconcile_timeout
        resent = False
        delay = 0.5
        while time.monotonic() < deadline:
            try:
                status = self._roundtrip({"op": "execution_status", "request_id": request_id, "args": [], "kwargs": {}}, 10.0)
            except (OSError, protocol.ProtocolError):
                time.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            state = (status.get("result") or {}).get("state") if status.get("ok") else None
            if state in {"done", "rejected"}:
                return self._finish(status["result"]["reply"])
            if state == "unknown" and not resent:
                # The gateway never received it, so sending it now cannot duplicate it;
                # the gateway would refuse a second send of the same id anyway.
                resent = True
                try:
                    return self._finish(self._roundtrip(body, self._execution_timeout))
                except (OSError, protocol.ProtocolError):
                    continue
            if state == "ambiguous":
                break
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
        self._last_error = (
            protocol.ERR_EXECUTION_AMBIGUOUS,
            f"order_send {request_id} outcome unknown after {first_error!r}; reconcile positions and orders with the broker before any retry",
        )
        return None

    # ------------------------------------------------------------------ module surface

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def gateway_health(self) -> dict[str, Any] | None:
        """Gateway-only call used by the watchdog: terminal connection, account, trading gate."""
        with self._lock:
            try:
                reply = self._roundtrip({"op": "health", "args": [], "kwargs": {}}, 10.0)
            except (OSError, protocol.ProtocolError) as exc:
                self._last_error = (protocol.ERR_GATEWAY_UNAVAILABLE, str(exc))
                return None
            return self._finish(reply)

    def __getattr__(self, name: str) -> Any:
        if name in _READ_OPS:
            def _remote(*args: Any, **kwargs: Any) -> Any:
                return self._call(name, args, kwargs)

            _remote.__name__ = name
            return _remote
        raise AttributeError(f"MT5 gateway client has no attribute {name!r}")
