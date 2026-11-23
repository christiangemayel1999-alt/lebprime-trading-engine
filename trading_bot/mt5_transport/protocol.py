"""Wire protocol shared by the MT5 gateway client and server.

Standard library only: the server side runs under Windows Python inside Wine,
the client side runs in native Linux Python. Messages are length-prefixed JSON
frames, each signed with HMAC-SHA256 over a shared secret, a timestamp and a
nonce, so only processes that can read the key file can talk to the gateway
and a captured frame cannot be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import time
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 30.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18812

# Gateway-specific last_error codes. MT5 itself uses 1 and -1..-10 (RES_*),
# so these stay well clear of the terminal's own codes.
ERR_GATEWAY_UNAVAILABLE = -20001
ERR_GATEWAY_TIMEOUT = -20002
ERR_EXECUTION_AMBIGUOUS = -20003
ERR_REQUEST_REJECTED = -20004
ERR_DEADLINE_EXCEEDED = -20005
ERR_TRADING_DISABLED = -20010
ERR_WRONG_ACCOUNT = -20011
ERR_NOT_DEMO = -20012
ERR_TERMINAL_DISCONNECTED = -20013


class ProtocolError(Exception):
    """Raised for malformed, unsigned, stale or replayed frames."""


def load_key(path: str | os.PathLike[str] | None = None) -> bytes:
    """Read the shared gateway secret (hex or raw text) from a file."""
    key_path = Path(path or os.environ.get("LEBPRIME_MT5_GATEWAY_KEY_FILE") or "/etc/lebprime/gateway.key")
    raw = key_path.read_text(encoding="utf-8").strip()
    if len(raw) < 32:
        raise ProtocolError(f"gateway key in {key_path} is too short (need at least 32 characters)")
    return raw.encode("utf-8")


# --------------------------------------------------------------------------- encoding

def encode_value(value: Any) -> Any:
    """Turn MT5 results and arguments into JSON-safe structures."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__datetime__": datetime(value.year, value.month, value.day).isoformat()}
    if isinstance(value, timedelta):
        return {"__timedelta__": value.total_seconds()}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    # numpy scalars and structured arrays, without importing numpy here
    dtype = getattr(value, "dtype", None)
    if dtype is not None and getattr(dtype, "names", None) and hasattr(value, "tolist"):
        return {
            "__array__": [[name, dtype.fields[name][0].str] for name in dtype.names],
            "rows": [list(encode_value(item) for item in row) for row in value.tolist()],
        }
    if dtype is not None and hasattr(value, "item") and getattr(value, "shape", None) == ():
        return encode_value(value.item())
    if isinstance(value, tuple) and (hasattr(value, "_fields") or hasattr(value, "_asdict")):
        # MetaTrader5 returns struct-sequence records that have _asdict() but not always _fields.
        mapping = value._asdict() if hasattr(value, "_asdict") else dict(zip(value._fields, value))
        return {
            "__record__": type(value).__name__,
            "fields": list(mapping.keys()),
            "values": [encode_value(item) for item in mapping.values()],
        }
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return {"__tuple__": [encode_value(item) for item in value]} if isinstance(value, tuple) else [encode_value(item) for item in value]
    if hasattr(value, "tolist"):
        return encode_value(value.tolist())
    raise TypeError(f"cannot encode value of type {type(value).__name__}")


_RECORD_TYPES: dict[tuple[str, tuple[str, ...]], type] = {}


def _record_type(name: str, fields: list[str]) -> type:
    key = (name, tuple(fields))
    cached = _RECORD_TYPES.get(key)
    if cached is None:
        safe_name = name if name.isidentifier() else "Record"
        cached = namedtuple(safe_name, fields)  # type: ignore[misc]
        _RECORD_TYPES[key] = cached
    return cached


def decode_value(value: Any) -> Any:
    """Inverse of encode_value. Structured arrays come back as numpy arrays when numpy is present."""
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    if "__timedelta__" in value:
        return timedelta(seconds=float(value["__timedelta__"]))
    if "__bytes__" in value:
        return bytes.fromhex(value["__bytes__"])
    if "__tuple__" in value:
        return tuple(decode_value(item) for item in value["__tuple__"])
    if "__record__" in value:
        record_cls = _record_type(str(value["__record__"]), list(value["fields"]))
        return record_cls(*[decode_value(item) for item in value["values"]])
    if "__array__" in value:
        descr = [(str(name), str(kind)) for name, kind in value["__array__"]]
        rows = [tuple(row) for row in value["rows"]]
        try:
            import numpy as np  # noqa: PLC0415

            return np.array(rows, dtype=descr)
        except ImportError:  # pragma: no cover - numpy is a hard dependency of the bot
            names = [name for name, _ in descr]
            return [dict(zip(names, row)) for row in rows]
    return {key: decode_value(item) for key, item in value.items()}


# --------------------------------------------------------------------------- signing / framing

def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sign(key: bytes, body: dict[str, Any]) -> dict[str, Any]:
    envelope = {"v": PROTOCOL_VERSION, "ts": time.time(), "nonce": secrets.token_hex(16), "body": body}
    envelope["mac"] = hmac.new(key, _canonical(envelope), hashlib.sha256).hexdigest()
    return envelope


class NonceCache:
    """Remembers recently seen nonces so a captured frame cannot be replayed."""

    def __init__(self, ttl_seconds: float = MAX_CLOCK_SKEW_SECONDS * 2) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str) -> bool:
        now = time.time()
        if len(self._seen) > 10000:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self.ttl_seconds}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def verify(key: bytes, envelope: Any, nonces: NonceCache | None = None) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"v", "ts", "nonce", "body", "mac"}:
        raise ProtocolError("malformed envelope")
    if envelope["v"] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {envelope['v']!r}")
    unsigned = {k: envelope[k] for k in ("v", "ts", "nonce", "body")}
    expected = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope["mac"])):
        raise ProtocolError("bad signature")
    if abs(time.time() - float(envelope["ts"])) > MAX_CLOCK_SKEW_SECONDS:
        raise ProtocolError("stale frame")
    if nonces is not None and not nonces.check_and_add(str(envelope["nonce"])):
        raise ProtocolError("replayed frame")
    body = envelope["body"]
    if not isinstance(body, dict):
        raise ProtocolError("body must be an object")
    return body


def send_frame(sock: socket.socket, key: bytes, body: dict[str, Any]) -> None:
    data = json.dumps(sign(key, body), separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ConnectionError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket, key: bytes, nonces: NonceCache | None = None) -> dict[str, Any]:
    (size,) = struct.unpack(">I", _recv_exact(sock, 4))
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ProtocolError(f"invalid frame size {size}")
    try:
        envelope = json.loads(_recv_exact(sock, size).decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    return verify(key, envelope, nonces)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
