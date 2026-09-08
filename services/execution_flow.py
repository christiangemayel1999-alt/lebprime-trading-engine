"""Execution flow helpers for final outcomes, state transitions, and spread policy."""

from __future__ import annotations

from typing import Any


FINAL_OUTCOMES = {
    "executed_live",
    "spread_too_wide",
    "duplicate_entry_blocked",
    "cooldown_active",
    "max_open_trades_reached",
    "position_exists",
    "margin_insufficient",
    "risk_rejected",
    "invalid_volume",
    "invalid_stops",
    "order_send_runtime_failure",
    "order_send_rejected",
    "broker_requote_or_price_change",
    "mt5_not_connected",
    "exception_before_send",
    "exception_after_send_unknown_state",
    "execution_paused",
    "auto_execution_disabled",
}


def normalize_final_outcome(
    reason_code: str | None,
    failure_class: str | None = None,
    execution_state: str | None = None,
    order_send_attempted: bool = False,
) -> str:
    """Map engine/internal reasons to a strict final execution outcome."""
    normalized = str(reason_code or "").strip()
    state = str(execution_state or "").strip()

    # Preserve true validation/precheck reasons when no broker send was attempted.
    if state == "precheck_failed" and not bool(order_send_attempted):
        if normalized in {"execution_cooldown", "post_loss_cooldown", "consecutive_loss_cooldown", "hard_rejection_cooldown_active"}:
            return "cooldown_active"
        if normalized in {"exposure_already_open", "pending_order_already_open", "pending_order_exists"}:
            return "position_exists"
        if normalized in {"blocked_margin_constraint", "blocked_insufficient_margin", "insufficient_margin"}:
            return "margin_insufficient"
        if normalized:
            return normalized
        return "precheck_failed"

    if normalized in {"executed", "order_send_done"}:
        return "executed_live"
    if normalized in {"execution_paused"}:
        return "execution_paused"
    if normalized in {"auto_execution_disabled"}:
        return "auto_execution_disabled"
    if normalized in {"spread_too_wide"}:
        return "spread_too_wide"
    if normalized in {"duplicate_entry_blocked", "setup_fingerprint_already_traded"}:
        return "duplicate_entry_blocked"
    if normalized in {"execution_cooldown", "post_loss_cooldown", "consecutive_loss_cooldown", "cooldown_active", "hard_rejection_cooldown_active"}:
        return "cooldown_active"
    if normalized in {"max_trades_per_day", "max_trades_per_symbol"}:
        return "max_open_trades_reached"
    if normalized in {"exposure_already_open", "pending_order_already_open", "pending_order_exists"}:
        return "position_exists"
    if normalized in {"blocked_margin_constraint", "blocked_insufficient_margin", "insufficient_margin", "margin_insufficient"}:
        return "margin_insufficient"
    if normalized in {"risk_lock_active", "disabled_due_to_drawdown"}:
        return "risk_rejected"
    if normalized in {
        "setup_score_below_threshold",
        "entry_score_below_min_score_to_trade",
        "trigger_score_below_threshold",
        "trend_score_below_threshold",
        "blocked_bad_regime",
        "blocked_bad_session",
    }:
        return "risk_rejected"
    if normalized in {"blocked_invalid_volume", "invalid_volume"}:
        return "invalid_volume"
    if normalized in {"blocked_invalid_stops", "invalid_stops"}:
        return "invalid_stops"
    if normalized in {"order_send_runtime_failure"}:
        return "order_send_runtime_failure"
    if normalized in {"blocked_no_connection", "blocked_no_tick", "blocked_symbol_unavailable", "disabled_due_to_infra", "mt5_not_connected"}:
        return "mt5_not_connected"
    if normalized in {"broker_requote_or_price_change"}:
        return "broker_requote_or_price_change"
    if normalized in {"exception_before_send", "exception_after_send_unknown_state"}:
        return normalized
    if not bool(order_send_attempted) and state == "precheck_failed":
        return normalized or "precheck_failed"
    if failure_class == "runtime_api_failure":
        return "order_send_runtime_failure"
    if failure_class == "broker_rejection":
        return "order_send_rejected"
    if normalized in {"order_send_failed", "order_check_api_error", "order_check_none", "unknown_order_check_failure"}:
        return "order_send_rejected"
    return "order_send_rejected"


def should_mark_duplicate_or_cooldown(execution_state: str) -> bool:
    """Duplicate/cooldown markers are only valid once an order send was attempted."""
    return execution_state in {"send_attempted", "send_rejected", "send_accepted"}


def resolve_spread_limit(
    execution_cfg: dict[str, Any],
    setup_name: str,
    regime_name: str,
    fallback_limit: float,
) -> tuple[float, str]:
    """Resolve spread limit from default/setup/regime and optional dynamic policy."""
    spread_cfg = dict(execution_cfg.get("spread") or {})
    limit = float(spread_cfg.get("max_points", {}).get("default", fallback_limit))
    source = "default"

    by_setup = spread_cfg.get("max_points_by_setup", {})
    if isinstance(by_setup, dict) and setup_name in by_setup:
        limit = float(by_setup.get(setup_name, limit))
        source = "setup"

    by_regime = spread_cfg.get("max_points_by_regime", {})
    if isinstance(by_regime, dict) and regime_name in by_regime:
        limit = float(by_regime.get(regime_name, limit))
        source = "regime"

    if bool(spread_cfg.get("enable_dynamic_limit", False)):
        setup_key = setup_name.lower()
        if any(token in setup_key for token in ["breakout", "compression"]):
            limit = min(limit + 3.0, limit * 1.15)
            source = "dynamic"
        elif any(token in setup_key for token in ["reversal", "mean"]):
            limit = max(1.0, limit * 0.9)
            source = "dynamic"

    return float(limit), source
