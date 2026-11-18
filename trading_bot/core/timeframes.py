"""Canonical timeframe normalization shared by runtime market-data adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_ALIASES = {
    "1MIN": "M1",
    "3MIN": "M3",
    "5MIN": "M5",
    "15MIN": "M15",
    "30MIN": "M30",
    "45MIN": "M45",
    "60MIN": "H1",
    "1H": "H1",
    "M1": "M1",
    "M3": "M3",
    "M5": "M5",
    "M15": "M15",
    "M30": "M30",
    "M45": "M45",
    "H1": "H1",
}

_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "M45": 45, "H1": 60}


@dataclass(frozen=True, slots=True)
class TimeframeResolution:
    label: str
    minutes: int
    mt5_value: int | None
    synthetic_source: str | None = None

    @property
    def synthetic(self) -> bool:
        return self.synthetic_source is not None


def normalize_timeframe_label(label: str) -> str:
    """Return one canonical label for every supported legacy/runtime spelling."""
    key = str(label or "").strip().upper()
    try:
        return _ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe label: {label}") from exc


def timeframe_minutes(label: str) -> int:
    return _MINUTES[normalize_timeframe_label(label)]


def resolve_timeframe(label: str, mt5_module: Any) -> TimeframeResolution:
    """Resolve native MT5 constants while making M45 explicitly synthetic."""
    canonical = normalize_timeframe_label(label)
    if canonical == "M45":
        return TimeframeResolution(canonical, 45, None, synthetic_source="M15")
    constant_name = "TIMEFRAME_H1" if canonical == "H1" else f"TIMEFRAME_{canonical}"
    value = getattr(mt5_module, constant_name, None)
    if value is None:
        raise ValueError(f"MT5 does not provide {constant_name} for timeframe {canonical}")
    return TimeframeResolution(canonical, _MINUTES[canonical], int(value))
