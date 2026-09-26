"""Authoritative Indicator Lab parameter schemas derived from Pine inputs/constants."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


ENGINE_VERSION = "indicator-lab-1.0"
LEBPRIME_INDICATOR_VERSION = "3.4.0"
LEBPRIME_PINE_PATH = Path("docs/indicator_reference/LebPrime_Final.pine")
VSS3_PINE_PATH = Path("docs/indicator_reference/VSS3_KOSOVI_limit.pine")


LEBPRIME_PARAMETERS: list[dict[str, Any]] = [
    {"name": "history_bars", "pine_name": "i_historyBars", "label": "Recent history bars", "type": "int", "default": 500, "min": 100, "max": 5000, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "body_len", "pine_name": "i_bodyLen", "label": "Average body length", "type": "int", "default": 20, "min": 5, "max": 100, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "body_mult", "pine_name": "i_bodyMult", "label": "Momentum body x average", "type": "float", "default": 0.50, "min": 0.10, "max": 3.00, "step": 0.05, "group": "Boundary 9 Engine"},
    {"name": "momentum_count", "pine_name": "i_momentumCount", "label": "Same-direction momentum candles", "type": "int", "default": 4, "min": 2, "max": 10, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "atr_len", "pine_name": "i_atrLen", "label": "ATR length", "type": "int", "default": 20, "min": 5, "max": 100, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "compact_search_bars", "pine_name": "i_compactSearchBars", "label": "Compact-base search bars", "type": "int", "default": 6, "min": 2, "max": 30, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "immediate_base_bars", "pine_name": "i_immediateBaseBars", "label": "Fallback immediate-base bars", "type": "int", "default": 2, "min": 1, "max": 6, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "compression_body_mult", "pine_name": "i_compressionBodyMult", "label": "Compact body threshold x average", "type": "float", "default": 0.60, "min": 0.05, "max": 1.50, "step": 0.05, "group": "Boundary 9 Engine"},
    {"name": "compact_range_atr", "pine_name": "i_compactRangeAtr", "label": "Compact range threshold x ATR", "type": "float", "default": 0.90, "min": 0.10, "max": 3.00, "step": 0.05, "group": "Boundary 9 Engine"},
    {"name": "compact_base_min_bars", "pine_name": "i_compactBaseMinBars", "label": "Minimum compact-base candles", "type": "int", "default": 2, "min": 1, "max": 8, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "leg_reset_mode", "pine_name": "i_legResetMode", "label": "Leg reset", "type": "enum", "default": "Opposite momentum", "values": ["Opposite momentum", "Any opposite close"], "group": "Boundary 9 Engine"},
    {"name": "min_same_side_gap", "pine_name": "i_minSameSideGap", "label": "Minimum bars between same-side candidates", "type": "int", "default": 5, "min": 0, "max": 50, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "zone_confirm_delay", "pine_name": "i_zoneConfirmDelay", "label": "Zone confirmation delay", "type": "int", "default": 5, "min": 0, "max": 30, "step": 1, "group": "Boundary 9 Engine"},
    {"name": "zone_refinement", "pine_name": "i_zoneRefinement", "label": "Zone refinement method", "type": "enum", "default": "Smallest Range Candle", "values": ["Full Base (Boundary 9)", "Last Opposing Candle", "Smallest Range Candle", "Extreme Wick + Tight Body"], "group": "Zone Refinement"},
    {"name": "use_refinement", "pine_name": "i_useRefinement", "label": "Enable zone refinement", "type": "bool", "default": True, "group": "Zone Refinement"},
    {"name": "max_zone_width_atr", "pine_name": "i_maxZoneWidthATR", "label": "Max zone width x ATR", "type": "float", "default": 1.5, "min": 0.1, "max": 5.0, "step": 0.1, "group": "Zone Filters"},
    {"name": "min_zone_width_atr", "pine_name": "i_minZoneWidthATR", "label": "Min zone width x ATR", "type": "float", "default": 0.1, "min": 0.01, "max": 1.0, "step": 0.01, "group": "Zone Filters"},
    {"name": "allow_overlap", "pine_name": "i_allowOverlap", "label": "Allow overlapping zones", "type": "bool", "default": False, "group": "Zone Filters"},
    {"name": "overlap_tolerance", "pine_name": "i_overlapTolerance", "label": "Overlap tolerance (pips)", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.1, "group": "Zone Filters"},
    {"name": "invalidation", "pine_name": "i_invalidation", "label": "Zone invalidation", "type": "enum", "default": "Close", "values": ["Wick", "Close"], "group": "Zone Lifecycle"},
    {"name": "min_bars_after_source", "pine_name": "i_minBarsAfterSource", "label": "Minimum bars since source", "type": "int", "default": 10, "min": 0, "max": 100, "step": 1, "group": "Zone Lifecycle"},
    {"name": "retest_cooldown", "pine_name": "i_retestCooldown", "label": "Retest cooldown bars", "type": "int", "default": 5, "min": 0, "max": 100, "step": 1, "group": "Zone Lifecycle"},
    {"name": "show_historic", "pine_name": "i_showHistoric", "label": "Show historic invalidated zones", "type": "bool", "default": True, "group": "Zone Lifecycle"},
    {"name": "max_zones", "pine_name": "i_maxZones", "label": "Maximum live zones per side", "type": "int", "default": 30, "min": 1, "max": 100, "step": 1, "group": "Zone Lifecycle"},
    {"name": "max_historic_zones", "pine_name": "i_maxHistoricZones", "label": "Maximum historic terminal zones per side", "type": "int", "default": 15, "min": 0, "max": 20, "step": 1, "group": "Zone Lifecycle"},
    {"name": "show_tpsl", "pine_name": "i_showTPSL", "label": "Show TP1 / TP2 / SL", "type": "bool", "default": True, "group": "TP / SL"},
    {"name": "gold_buffer", "pine_name": "i_goldBuffer", "label": "Gold / XAUUSD SL buffer", "type": "float", "default": 1.0, "min": 0.0, "max": None, "step": 0.1, "group": "SL Buffers - Built-in"},
    {"name": "silver_buffer", "pine_name": "i_silverBuffer", "label": "Silver / XAGUSD SL buffer", "type": "float", "default": 0.10, "min": 0.0, "max": None, "step": 0.01, "group": "SL Buffers - Built-in"},
    {"name": "us30_buffer", "pine_name": "i_us30Buffer", "label": "US30 / DJI SL buffer", "type": "float", "default": 30.0, "min": 0.0, "max": None, "step": 1.0, "group": "SL Buffers - Built-in"},
    {"name": "nas_buffer", "pine_name": "i_nasBuffer", "label": "NAS100 / US100 SL buffer", "type": "float", "default": 15.0, "min": 0.0, "max": None, "step": 1.0, "group": "SL Buffers - Built-in"},
    {"name": "wti_buffer", "pine_name": "i_wtiBuffer", "label": "WTI / USOIL SL buffer", "type": "float", "default": 0.15, "min": 0.0, "max": None, "step": 0.01, "group": "SL Buffers - Built-in"},
    {"name": "forex_buffer", "pine_name": "i_forexBuffer", "label": "Forex non-JPY default", "type": "float", "default": 0.0005, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Built-in"},
    {"name": "forex_jpy_buffer", "pine_name": "i_forexJpyBuffer", "label": "Forex JPY default", "type": "float", "default": 0.05, "min": 0.0, "max": None, "step": 0.01, "group": "SL Buffers - Built-in"},
    {"name": "fallback_buffer", "pine_name": "i_fallbackBuffer", "label": "Other / fallback buffer", "type": "float", "default": 1.0, "min": 0.0, "max": None, "step": 0.01, "group": "SL Buffers - Built-in"},
    {"name": "custom_symbol1", "pine_name": "i_customSymbol1", "label": "Custom symbol 1", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer1", "pine_name": "i_customBuffer1", "label": "Custom buffer 1", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "custom_symbol2", "pine_name": "i_customSymbol2", "label": "Custom symbol 2", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer2", "pine_name": "i_customBuffer2", "label": "Custom buffer 2", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "custom_symbol3", "pine_name": "i_customSymbol3", "label": "Custom symbol 3", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer3", "pine_name": "i_customBuffer3", "label": "Custom buffer 3", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "custom_symbol4", "pine_name": "i_customSymbol4", "label": "Custom symbol 4", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer4", "pine_name": "i_customBuffer4", "label": "Custom buffer 4", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "custom_symbol5", "pine_name": "i_customSymbol5", "label": "Custom symbol 5", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer5", "pine_name": "i_customBuffer5", "label": "Custom buffer 5", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "custom_symbol6", "pine_name": "i_customSymbol6", "label": "Custom symbol 6", "type": "string", "default": "", "group": "SL Buffers - Custom"},
    {"name": "custom_buffer6", "pine_name": "i_customBuffer6", "label": "Custom buffer 6", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.0001, "group": "SL Buffers - Custom"},
    {"name": "enable_lifecycle_alerts", "pine_name": "i_enableLifecycleAlerts", "label": "Enable lifecycle alerts", "type": "bool", "default": False, "group": "Telegram Alerts"},
    {"name": "alert_format", "pine_name": "i_alertFormat", "label": "Delivery format", "type": "enum", "default": "Telegram webhook JSON", "values": ["Telegram webhook JSON", "Plain text"], "group": "Telegram Alerts"},
    {"name": "telegram_chat_id", "pine_name": "i_telegramChatId", "label": "Telegram chat_id", "type": "string", "default": "", "group": "Telegram Alerts"},
    {"name": "auto_webhook_test", "pine_name": "i_autoWebhookTest", "label": "Auto-send safe webhook test", "type": "bool", "default": False, "group": "Telegram Alerts"},
]


VSS3_PARAMETERS: list[dict[str, Any]] = [
    {"name": "zone_state_warmup_bars", "pine_name": "zoneStateWarmupBars", "label": "Zone state warmup bars", "type": "int", "default": 5000, "min": 0, "max": 50000, "step": 100, "group": "Hidden Constants"},
    {"name": "trade_state_warmup_bars", "pine_name": "tradeStateWarmupBars", "label": "Trade state warmup bars", "type": "int", "default": 2000, "min": 0, "max": 50000, "step": 100, "group": "Hidden Constants"},
    {"name": "max_sd_zones", "pine_name": "maxSDZones", "label": "Maximum S/D zones", "type": "int", "default": 30, "min": 1, "max": 100, "step": 1, "group": "Hidden Constants"},
    {"name": "min_zone_size", "pine_name": "minZoneSize", "label": "Minimum zone size (bars)", "type": "int", "default": 10, "min": 1, "max": 200, "step": 1, "group": "Hidden Constants"},
    {"name": "retest_cooldown", "pine_name": "RETEST_COOLDOWN", "label": "Retest cooldown bars", "type": "int", "default": 5, "min": 0, "max": 100, "step": 1, "group": "Hidden Constants"},
    {"name": "min_distance_between_zones", "pine_name": "minDistanceBetweenZones", "label": "Minimum distance between zones", "type": "int", "default": 5, "min": 0, "max": 100, "step": 1, "group": "Hidden Constants"},
    {"name": "max_zone_size_atr", "pine_name": "maxZoneSizeATR", "label": "Max zone size x ATR", "type": "float", "default": 1.5, "min": 0.1, "max": 10.0, "step": 0.1, "group": "Hidden Constants"},
    {"name": "min_flipped_order_distance_percent", "pine_name": "minFlippedOrderDistancePercent", "label": "Min flipped order distance percent", "type": "float", "default": 0.10, "min": 0.0, "max": 5.0, "step": 0.01, "group": "Hidden Constants"},
    {"name": "combine_sds", "pine_name": "combineSDs", "label": "Combine zones", "type": "bool", "default": True, "group": "General Configuration"},
    {"name": "momentum_body_mult", "pine_name": "momentumBodyMult", "label": "Momentum Body Mult", "type": "float", "default": 0.5, "min": 0.1, "max": 3.0, "step": 0.1, "group": "General Configuration"},
    {"name": "momentum_count", "pine_name": "momentumCount", "label": "Momentum Count", "type": "int", "default": 4, "min": 1, "max": 20, "step": 1, "group": "General Configuration"},
    {"name": "momentum_span", "pine_name": "momentumSpan", "label": "Momentum Span", "type": "int", "default": 4, "min": 1, "max": 20, "step": 1, "group": "General Configuration"},
    {"name": "zone_count", "pine_name": "zoneCount", "label": "Zone Count", "type": "enum", "default": "High", "values": ["High", "Medium", "Low", "One"], "group": "General Configuration"},
    {"name": "sd_end_method", "pine_name": "sdEndMethod", "label": "Zone invalidation", "type": "enum", "default": "Close", "values": ["Wick", "Close"], "group": "General Configuration"},
    {"name": "min_distance_percent", "pine_name": "minDistancePercent", "label": "Minimum distance percent", "type": "float", "default": 0.15, "min": 0.0, "max": 5.0, "step": 0.01, "group": "Signal Settings"},
    {"name": "apply_min_distance_to_zones", "pine_name": "applyMinDistanceToZones", "label": "Apply min distance to zones", "type": "bool", "default": True, "group": "Signal Settings"},
    {"name": "sl_method", "pine_name": "slMethod", "label": "SL method", "type": "enum", "default": "Previous Box Swing", "values": ["Previous Box Swing"], "group": "TP / SL"},
    {"name": "gold_sl", "pine_name": "goldSL", "label": "Gold SL buffer", "type": "float", "default": 1.0, "min": 0.0, "max": None, "step": 0.1, "group": "TP / SL"},
    {"name": "us30_sl", "pine_name": "us30SL", "label": "US30 SL buffer", "type": "float", "default": 30.0, "min": 0.0, "max": None, "step": 1.0, "group": "TP / SL"},
    {"name": "nasdaq_sl", "pine_name": "nasdaqSL", "label": "NASDAQ SL buffer", "type": "float", "default": 15.0, "min": 0.0, "max": None, "step": 1.0, "group": "TP / SL"},
    {"name": "wti_sl", "pine_name": "wtiSL", "label": "WTI SL buffer", "type": "float", "default": 0.15, "min": 0.0, "max": None, "step": 0.01, "group": "TP / SL"},
    {"name": "tp_rr", "pine_name": "tpRR", "label": "TP RR", "type": "float", "default": 1.0, "min": 0.1, "max": 10.0, "step": 0.1, "group": "TP / SL"},
    {"name": "swing_pivot_len", "pine_name": "swingPivotLen", "label": "Swing pivot length", "type": "int", "default": 2, "min": 1, "max": 20, "step": 1, "group": "TP / SL"},
    {"name": "max_after_box_swing_bars", "pine_name": "maxAfterBoxSwingBars", "label": "Max after-box swing bars", "type": "int", "default": 60, "min": 1, "max": 500, "step": 1, "group": "TP / SL"},
    {"name": "be_trigger_r", "pine_name": "beTriggerR", "label": "BE trigger R", "type": "float", "default": 0.85, "min": 0.1, "max": 5.0, "step": 0.05, "group": "Trade Management"},
    {"name": "spread_buffer", "pine_name": "spreadBuffer", "label": "Spread buffer", "type": "float", "default": 0.0, "min": 0.0, "max": None, "step": 0.1, "group": "Trade Management"},
]


def normalize_indicator(indicator: str) -> str:
    value = str(indicator or "").strip().upper()
    if value not in {"LEBPRIME", "VSS3"}:
        raise ValueError("indicator must be LEBPRIME or VSS3")
    return value


def parameter_schema(indicator: str) -> list[dict[str, Any]]:
    value = normalize_indicator(indicator)
    return deepcopy(LEBPRIME_PARAMETERS if value == "LEBPRIME" else VSS3_PARAMETERS)


def default_parameters(indicator: str) -> dict[str, Any]:
    return {item["name"]: deepcopy(item["default"]) for item in parameter_schema(indicator)}


def validate_parameters(indicator: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    merged = default_parameters(indicator)
    supplied = parameters or {}
    schema = {item["name"]: item for item in parameter_schema(indicator)}
    unknown = sorted(set(supplied) - set(schema))
    if unknown:
        raise ValueError(f"Unknown {normalize_indicator(indicator)} parameter(s): {unknown}")
    for name, value in supplied.items():
        spec = schema[name]
        kind = spec["type"]
        if kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on"}
            value = bool(value)
        elif kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        elif kind == "enum":
            allowed = [str(item) for item in spec.get("values", [])]
            if str(value) not in allowed:
                raise ValueError(f"{name} must be one of {allowed}")
            value = str(value)
        elif kind == "string":
            value = str(value)
        minimum = spec.get("min")
        maximum = spec.get("max")
        if isinstance(value, (int, float)) and minimum is not None and value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        if isinstance(value, (int, float)) and maximum is not None and value > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
        merged[name] = value
    return merged
