from __future__ import annotations

from typing import Any


STRATEGY_FAMILY_BREAKOUT = "BREAKOUT"
STRATEGY_FAMILY_COMPRESS = "COMPRESS"
STRATEGY_FAMILY_LEBPRIM = "LEBPRIM"
STRATEGY_FAMILY_OTHER = "OTHER"


SETUP_TO_STRATEGY_FAMILY: dict[str, str] = {
    "BREAKOUT_RETEST_CONTINUATION": STRATEGY_FAMILY_BREAKOUT,
    "COMPRESSION_RELEASE": STRATEGY_FAMILY_COMPRESS,
    "LEBPRIM_SCALP": STRATEGY_FAMILY_LEBPRIM,
}


FAMILY_TO_PROFILE: dict[str, str] = {
    STRATEGY_FAMILY_BREAKOUT: "breakout",
    STRATEGY_FAMILY_COMPRESS: "compress",
    STRATEGY_FAMILY_LEBPRIM: "lebprim",
    STRATEGY_FAMILY_OTHER: "default",
}


def strategy_family_for_setup(setup_family: str) -> str:
    return SETUP_TO_STRATEGY_FAMILY.get(str(setup_family or "").upper(), STRATEGY_FAMILY_OTHER)


def management_profile_for_setup(setup_family: str) -> str:
    return FAMILY_TO_PROFILE.get(strategy_family_for_setup(setup_family), "default")


def quality_tier(score: float, config: dict[str, Any] | None = None) -> str:
    execution_cfg = (config or {}).get("execution", {}) if isinstance(config, dict) else {}
    tiers = execution_cfg.get("quality_tiers", {}) if isinstance(execution_cfg, dict) else {}
    high = float(tiers.get("high_min_score", 64.0) or 64.0)
    medium = float(tiers.get("medium_min_score", 54.0) or 54.0)
    value = float(score or 0.0)
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return "marginal"


def volatility_state(market_context: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> str:
    market = market_context if isinstance(market_context, dict) else {}
    regime = market.get("regime") if isinstance(market.get("regime"), dict) else {}
    regime_name = str(
        (candidate or {}).get("regime_name")
        or regime.get("regime_name")
        or ""
    ).upper()
    if "HIGH_VOLATILITY" in regime_name or "BREAKOUT" in regime_name:
        return "high"
    if "NO_TRADE" in regime_name or "LOW" in regime_name or "DEAD_SESSION" in regime_name:
        return "low"
    return "normal"
