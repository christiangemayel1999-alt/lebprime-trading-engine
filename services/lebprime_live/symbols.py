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


# Broker names below are the symbols connected on the MT5 account.
SYMBOLS: dict[str, SymbolMapping] = {
    "XAUUSD": SymbolMapping("XAUUSD", "XAUUSD", "GOLD vs USD", "XAU", 1.0),
    "US30": SymbolMapping("US30", "US30", "US Wall Street 30 Index", "US30", 30.0),
    "NAS100": SymbolMapping("NAS100", "USTEC", "US Tech 100 Index", "NAS", 15.0),
    "XAGUSD": SymbolMapping("XAGUSD", "XAGUSD", "Silver vs USD", "XAG", 0.10),
    "WTI": SymbolMapping("WTI", "WTI", "Spot WTI Light Crude Oil", "WTI", 0.15),
    "EURUSD": SymbolMapping("EURUSD", "EURUSD", "Euro vs US Dollar", "EUR", 0.0005),
}

# Symbols the runtime trades when the config does not list any. Adding a symbol to
# SYMBOLS makes it selectable; it is only traded once chosen in the dashboard/config.
DEFAULT_SYMBOLS: tuple[str, ...] = ("XAUUSD", "US30", "NAS100")

# Every name accepted for a symbol in config keys (logical or broker spelling).
ACCEPTED_SYMBOL_NAMES: frozenset[str] = frozenset(
    name for mapping in SYMBOLS.values() for name in (mapping.logical_symbol, mapping.mt5_symbol)
)


def resolve_symbol(logical_symbol: str) -> SymbolMapping:
    key = str(logical_symbol or "").strip().upper()
    if key not in SYMBOLS:
        # Accept the broker spelling too, e.g. USTEC for NAS100.
        key = next((name for name, mapping in SYMBOLS.items() if mapping.mt5_symbol == key), key)
    try:
        return SYMBOLS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported LebPrime logical symbol: {logical_symbol}") from exc
