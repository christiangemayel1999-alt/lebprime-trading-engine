"""Selectable MT5 transport: the native Windows package or the Linux gateway.

Set ``LEBPRIME_MT5_TRANSPORT`` to choose:

* ``native``  - ``import MetaTrader5`` (Windows, or Windows Python under Wine);
* ``gateway`` - :class:`~trading_bot.mt5_transport.client.GatewayMT5`, which
  talks to ``gateway_server.py`` running next to the MT5 terminal under Wine;
* ``auto`` (default) - native when the package imports, otherwise the
  import-safe placeholder the connector already used. ``auto`` never picks the
  gateway on its own, so a Windows install behaves exactly as before.

The returned object is used as a drop-in for the ``MetaTrader5`` module, so
the connector and the trading logic are the same on both platforms.
"""

from __future__ import annotations

import os
from typing import Any

TRANSPORT_ENV = "LEBPRIME_MT5_TRANSPORT"
VALID_TRANSPORTS = ("auto", "native", "gateway")


def selected_transport() -> str:
    value = (os.environ.get(TRANSPORT_ENV) or "auto").strip().lower()
    if value not in VALID_TRANSPORTS:
        raise ValueError(f"{TRANSPORT_ENV}={value!r} is not one of {', '.join(VALID_TRANSPORTS)}")
    return value


def load_mt5_module() -> tuple[Any | None, Exception | None]:
    """Return ``(module_like, import_error)``. ``module_like`` is None when nothing is available."""
    transport = selected_transport()
    if transport == "gateway":
        from .client import GatewayMT5  # noqa: PLC0415

        return GatewayMT5(), None
    try:
        import MetaTrader5 as native_mt5  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if transport == "native":
            raise
        return None, exc
    return native_mt5, None
