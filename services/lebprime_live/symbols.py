"""Central logical-to-broker symbol mappings for LebPrime v3.4.0."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SymbolMapping:
    logical_symbol: str
    mt5_symbol: str
    broker_description: str
    id_prefix: str
    sl_buffer: float


SYMBOLS: dict[str, SymbolMapping] = {
    "XAUUSD": SymbolMapping("XAUUSD", "XAUUSD", "Gold Spot", "XAU", 1.0),
    "US30": SymbolMapping("US30", "US30", "US Wall Street 30 Index", "US30", 30.0),
    "NAS100": SymbolMapping("NAS100", "USTEC", "US Tech 100 Index", "NAS", 15.0),
}


def resolve_symbol(logical_symbol: str) -> SymbolMapping:
    key = str(logical_symbol or "").strip().upper()
    try:
        return SYMBOLS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported LebPrime logical symbol: {logical_symbol}") from exc
