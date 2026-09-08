from __future__ import annotations

from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    try:
        return value is None or pd.isna(value)
    except Exception:
        return value is None


def safe_int(value: Any, default: int = 0) -> int:
    if is_missing(value):
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def safe_float(value: Any, default: float = 0.0) -> float:
    if is_missing(value):
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_bool(value: Any, default: bool = False) -> bool:
    if is_missing(value):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def safe_text(value: Any, default: str = "") -> str:
    if is_missing(value):
        return default
    text = str(value).strip()
    return text if text else default
