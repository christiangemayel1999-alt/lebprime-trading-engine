from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from trading_bot.core.execution_mode import FAMILY_KEY_TO_NAME


def build_strategy_candidate_diagnostics(
    *,
    config: dict[str, Any],
    engine: Any,
    market_context: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    selected_candidate: dict[str, Any] | None,
    runtime_mode: str,
    allowed_families: set[str] | None = None,
) -> dict[str, Any]:
    strategy_cfg = config.get("strategy", {}) if isinstance(config.get("strategy"), dict) else {}
    setup_families = strategy_cfg.get("setup_families", {}) if isinstance(strategy_cfg.get("setup_families"), dict) else {}
    setup_controls = strategy_cfg.get("setup_controls", {}) if isinstance(strategy_cfg.get("setup_controls"), dict) else {}
    market = market_context if isinstance(market_context, dict) else {}
    session_name = _market_name(market, "session", "session_name")
    regime_name = _market_name(market, "regime", "regime_name")
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_family[str(candidate.get("setup_family") or "UNKNOWN").upper()].append(candidate)

    per_family: list[dict[str, Any]] = []
    family_names = sorted(set(FAMILY_KEY_TO_NAME.values()) | set(by_family))
    for family in family_names:
        family_key = _family_key(family)
        family_cfg = setup_families.get(family_key, {}) if isinstance(setup_families.get(family_key, {}), dict) else {}
        control_cfg = setup_controls.get(family_key, {}) if isinstance(setup_controls.get(family_key, {}), dict) else {}
        family_candidates = by_family.get(family, [])
        best = _best_candidate(family_candidates)
        enabled = bool(family_cfg.get("enabled", control_cfg.get("enabled", True)))
        live_allowed = _optional_bool(family_cfg.get("live_allowed", control_cfg.get("live_allowed")))
        session_allowed = _allowed_by_name(
            session_name,
            family_cfg.get("sessions", control_cfg.get("allowed_sessions", [])),
            fallback=True,
        )
        regime_allowed = _allowed_by_name(
            regime_name,
            family_cfg.get("regimes", control_cfg.get("allowed_regimes", [])),
            fallback=True,
        )
        if best:
            session_allowed = _optional_bool(best.get("session_allowed", best.get("session_live_allowed"))) if best.get("session_allowed", best.get("session_live_allowed")) is not None else session_allowed
            regime_allowed = _optional_bool(best.get("regime_allowed")) if best.get("regime_allowed") is not None else regime_allowed
        invalidation_reasons = _invalidation_reasons(family_candidates)
        per_family.append(
            {
                "family": family,
                "enabled": enabled,
                "live_allowed": live_allowed,
                "backtest_allowed": None if allowed_families is None else family in allowed_families,
                "session_allowed": session_allowed,
                "regime_allowed": regime_allowed,
                "raw_candidate_present": bool(family_candidates),
                "raw_candidate_count": len(family_candidates),
                "setup_valid": bool(best.get("setup_valid")) if best else False,
                "setup_score": _float_or_none(best.get("setup_score")) if best else None,
                "invalidated_by_quality_validation": bool(best.get("invalidated_by_quality_validation")) if best else False,
                "invalidation_reasons": invalidation_reasons,
            }
        )

    raw_counts = Counter(str(candidate.get("setup_family") or "UNKNOWN").upper() for candidate in candidates)
    selected_family = str(selected_candidate.get("setup_family") or "") if selected_candidate else ""
    selected_score = _float_or_none(selected_candidate.get("setup_score")) if selected_candidate else None
    return {
        "category": "strategy_candidate_diagnostics",
        "mode": runtime_mode,
        "selected_execution_mode": str(config.get("bot", {}).get("execution_mode") or ""),
        "engine_type": str(getattr(engine, "engine_type", config.get("bot", {}).get("execution_mode_summary", {}).get("engine_type", ""))),
        "enabled_families": sorted(
            family for family, payload in setup_families.items() if isinstance(payload, dict) and bool(payload.get("enabled", True))
        ),
        "raw_candidate_count": len(candidates),
        "raw_candidate_count_by_family": dict(sorted(raw_counts.items())),
        "per_family": per_family,
        "selected_family": selected_family or None,
        "selected_score": selected_score,
        "no_candidate_reason_summary": _reason_summary(candidates, selected_candidate, per_family),
    }


def _market_name(market: dict[str, Any], section_name: str, field_name: str) -> str:
    section = market.get(section_name) if isinstance(market.get(section_name), dict) else {}
    return str(section.get(field_name) or "").upper()


def _family_key(family: str) -> str:
    lookup = {value: key for key, value in FAMILY_KEY_TO_NAME.items()}
    return lookup.get(str(family or "").upper(), str(family or "").lower())


def _best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: float(item.get("setup_score", 0.0) or 0.0), reverse=True)[0]


def _allowed_by_name(name: str, allowed: Any, *, fallback: bool) -> bool:
    if not allowed:
        return fallback
    values = {str(item).upper() for item in allowed if str(item).strip()}
    return str(name or "").upper() in values


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _invalidation_reasons(candidates: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for candidate in candidates:
        for item in candidate.get("quality_blocked_reasons", []) or []:
            if str(item):
                reasons.append(str(item))
        for key in ("live_block_reason", "reason_code"):
            value = str(candidate.get(key) or "")
            if value and value not in {"setup_valid", "entry_valid"}:
                reasons.append(value)
        reason = str(candidate.get("reason") or "")
        if reason and not bool(candidate.get("setup_valid")):
            reasons.append(reason)
    return sorted(set(reasons))


def _reason_summary(
    candidates: list[dict[str, Any]],
    selected_candidate: dict[str, Any] | None,
    per_family: list[dict[str, Any]],
) -> str | None:
    if selected_candidate is not None:
        return None
    if not candidates:
        blocked = [
            f"{item['family']}:{'disabled' if not item['enabled'] else 'no_raw_candidate'}"
            for item in per_family
            if not item["enabled"] or not item["raw_candidate_present"]
        ]
        return "no_raw_candidates_generated" if not blocked else "; ".join(blocked[:8])
    invalid = []
    for item in per_family:
        if item["raw_candidate_present"] and not item["setup_valid"]:
            reasons = ",".join(item["invalidation_reasons"][:3]) or "setup_invalid"
            invalid.append(f"{item['family']}:{reasons}")
    return "; ".join(invalid[:8]) if invalid else "no_candidate_survived_selection"
