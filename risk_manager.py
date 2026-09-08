"""Professional risk sizing, drawdown governance, and trade management for the MT5 bot."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from trading_bot.risk.management_profiles import TradeManagementProfileResolver
from utils import floor_volume_to_step, normalize_price, pips_to_price, price_to_pips, round_volume, to_utc, utc_now


class RiskManager:
    """Compute lot sizes, enforce locks, and drive deterministic position management."""

    def __init__(self, config: dict[str, Any], logger: Any | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("mtf_sniper_bot")
        self.profile_resolver = TradeManagementProfileResolver(config)

    def sync_state(
        self,
        state: dict[str, Any],
        now_utc: datetime,
        balance: float,
        session_name: str,
        manual_reset_id: int,
    ) -> dict[str, Any]:
        """Reset day/session state, clear expired cooldowns, and apply manual resets.
        
        Transactions: All mutations are collected and applied atomically to prevent
        partial state updates if an exception occurs mid-sync.
        """
        day_key = now_utc.date().isoformat()
        
        # Collect all mutations in a local dict first (read-only until we apply all)
        mutations: dict[str, Any] = {}
        
        # Daily reset mutations
        if state.get("last_daily_reset") != day_key:
            mutations["daily_trade_count"] = 0
            mutations["daily_pnl"] = 0.0
            mutations["start_of_day_balance"] = float(balance)
            mutations["last_daily_reset"] = day_key
            mutations["daily_summary_date"] = None
            # Also need to reset daily_loss_lock in locks
            locks = state.setdefault("locks", {})
            if isinstance(locks.get("daily_loss_lock"), dict):
                mutations.setdefault("locks", dict(locks))
                mutations["locks"]["daily_loss_lock"] = {"active": False, "reason": None, "locked_at": None}
        
        # Session reset mutations
        session_stats = state.setdefault("session_stats", {})
        if session_stats.get("date") != day_key or session_stats.get("session_name") != session_name:
            mutations["session_stats"] = {
                "session_name": session_name,
                "date": day_key,
                "start_balance": float(balance),
                "pnl": 0.0,
                "trade_count": 0,
                "wins": 0,
                "losses": 0,
            }
            # Reset session_loss_lock
            locks = state.setdefault("locks", {})
            if isinstance(locks.get("session_loss_lock"), dict):
                mutations.setdefault("locks", dict(locks))
                mutations["locks"]["session_loss_lock"] = {
                    "active": False,
                    "reason": None,
                    "session_name": None,
                    "locked_at": None,
                }
        
        # Cooldown expiry mutations
        cooldowns = state.setdefault("cooldowns_v2", {})
        cooldown_mutations = False
        for key in ["entry_until", "post_loss_until", "consecutive_loss_until"]:
            expiry = to_utc(cooldowns.get(key))
            if expiry and now_utc >= expiry:
                mutations.setdefault("cooldowns_v2", dict(cooldowns))
                mutations["cooldowns_v2"][key] = None
                cooldown_mutations = True
        
        if cooldown_mutations:
            if not any(mutations.get("cooldowns_v2", {}).get(key) for key in ["entry_until", "post_loss_until", "consecutive_loss_until"]):
                mutations["cooldowns_v2"]["reason"] = None
        
        # Manual reset mutations
        if manual_reset_id > int(state.get("last_manual_reset_id", 0)):
            mutations["last_manual_reset_id"] = int(manual_reset_id)
            locks = mutations.get("locks", dict(state.get("locks", {})))
            locks["manual_reset_required"] = False
            locks["manual_reset_reason"] = None
            locks["journal_failure_lock"] = False
            locks["kill_switch"] = False
            locks["kill_switch_reason"] = None
            if isinstance(locks.get("consecutive_loss_lock"), dict):
                locks["consecutive_loss_lock"] = {"active": False, "reason": None, "locked_at": None}
            mutations["locks"] = locks
            
            journal_state = state.setdefault("journal", {})
            mutations["journal"] = {
                **journal_state,
                "failure_count": 0,
                "disabled_live": False,
                "last_error": None,
                "last_failure_at": None,
            }
        
        # Ensure journal_state defaults exist
        if "journal" not in mutations:
            journal_state = state.setdefault("journal", {})
            journal_state.setdefault("failure_count", 0)
            journal_state.setdefault("disabled_live", False)
            journal_state.setdefault("last_error", None)
            journal_state.setdefault("last_failure_at", None)
            if bool(journal_state.get("disabled_live")):
                locks = mutations.get("locks", dict(state.get("locks", {})))
                locks["journal_failure_lock"] = True
                locks["journal_failure_reason"] = str(journal_state.get("last_error") or "journal_failure_threshold_reached")
                mutations["locks"] = locks
        
        # NOW apply all collected mutations atomically
        for key, value in mutations.items():
            state[key] = value

        return state

    def evaluate_risk_gate(
        self,
        state: dict[str, Any],
        account_info: Any,
        now_utc: datetime,
        session_name: str,
        live_requested: bool,
        force_reduced_risk: bool = False,
    ) -> dict[str, Any]:
        """Return whether trading is allowed and whether reduced-risk mode is active."""
        locks = state.setdefault("locks", {})
        cooldowns = state.setdefault("cooldowns_v2", {})
        journal_state = state.setdefault("journal", {})
        risk_cfg = self.config["risk"]
        safety_cfg = self.config["safety"]
        session_stats = state.setdefault("session_stats", {})
        balance = float(getattr(account_info, "balance", 0.0) or 0.0)

        reasons: list[str] = []
        reduced_risk = bool(force_reduced_risk)
        risk_lock_reason = None

        if balance < float(risk_cfg["min_balance"]):
            reasons.append("balance_below_minimum")
        if bool(locks.get("kill_switch")):
            reasons.append(str(locks.get("kill_switch_reason") or "kill_switch_active"))
        if bool(locks.get("manual_reset_required")):
            reasons.append(str(locks.get("manual_reset_reason") or "manual_reset_required"))
        daily_drawdown_pct = self._drawdown_pct(float(state.get("start_of_day_balance") or balance), float(state.get("daily_pnl", 0.0)))
        session_drawdown_pct = self._drawdown_pct(float(session_stats.get("start_balance") or balance), float(session_stats.get("pnl", 0.0)))
        if daily_drawdown_pct >= float(risk_cfg["max_daily_drawdown_pct"]):
            lock = locks.setdefault("daily_loss_lock", {})
            lock.update({"active": True, "reason": "max_daily_drawdown_pct", "locked_at": now_utc.isoformat()})
            reasons.append("disabled_due_to_drawdown")
            if bool(safety_cfg.get("require_manual_reset_after_daily_lock", True)):
                locks["manual_reset_required"] = True
                locks["manual_reset_reason"] = "daily_drawdown_lock"
        if session_drawdown_pct >= float(risk_cfg["max_session_drawdown_pct"]):
            lock = locks.setdefault("session_loss_lock", {})
            lock.update(
                {
                    "active": True,
                    "reason": "max_session_drawdown_pct",
                    "session_name": session_name,
                    "locked_at": now_utc.isoformat(),
                }
            )
            reasons.append("session_drawdown_lock")
        if int(state.get("consecutive_losses", 0)) >= int(risk_cfg["max_consecutive_losses"]):
            lock = locks.setdefault("consecutive_loss_lock", {})
            lock.update({"active": True, "reason": "max_consecutive_losses", "locked_at": now_utc.isoformat()})
            reasons.append("consecutive_loss_lock")

        cooldown_reason, cooldown_remaining = self._active_cooldown(cooldowns, now_utc)
        if cooldown_reason:
            reasons.append(cooldown_reason)

        if int(state.get("daily_trade_count", 0)) >= int(risk_cfg["max_trades_per_day"]):
            reasons.append("max_trades_per_day")
        if bool(locks.get("journal_failure_lock")) or bool(journal_state.get("disabled_live")):
            reasons.append("journal_failure_lock")

        trigger_drawdown = float(risk_cfg["reduced_risk_trigger_drawdown_pct"])
        if daily_drawdown_pct >= trigger_drawdown or session_drawdown_pct >= trigger_drawdown or int(state.get("consecutive_losses", 0)) > 0:
            reduced_risk = True

        live_allowed = live_requested and not reasons
        if reasons:
            risk_lock_reason = reasons[0]

        return {
            "live_allowed": live_allowed,
            "live_requested": live_requested,
            "reduced_risk": reduced_risk,
            "risk_lock_active": bool(reasons),
            "risk_lock_reason": risk_lock_reason,
            "cooldown_active": cooldown_reason is not None,
            "cooldown_reason": cooldown_reason,
            "cooldown_remaining_seconds": cooldown_remaining,
            "daily_drawdown_pct": round(daily_drawdown_pct, 3),
            "session_drawdown_pct": round(session_drawdown_pct, 3),
            "reasons": reasons,
        }

    def calculate_trade_levels(
        self,
        candidate: dict[str, Any],
        entry_price: float,
        symbol_spec: dict[str, Any],
        entry_assessment: dict[str, Any] | None = None,
        market_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Calculate stop loss and profit targets from structural invalidation and ATR."""
        exit_cfg = self.config["exit"]
        strategy_control = candidate.get("strategy_control", {}) if isinstance(candidate, dict) else {}
        profile = self.profile_resolver.for_candidate(candidate, entry_assessment, market_context)
        pip_size = float(symbol_spec["pip_size"])
        digits = int(symbol_spec["digits"])
        atr_price = max(float(candidate["atr_at_setup"]), 1e-9)
        sl_multiplier = float(strategy_control.get("sl_multiplier", 1.0) or 1.0)
        tp_multiplier = float(strategy_control.get("tp_multiplier", 1.0) or 1.0)
        sl_buffer_price = max(
            atr_price * float(exit_cfg["sl_buffer_atr"]) * profile.sl_buffer_atr_multiplier * sl_multiplier,
            pips_to_price(float(exit_cfg["sl_buffer_pips"]), pip_size),
        )
        min_sl_price = pips_to_price(float(exit_cfg["sl_min_pips"]), pip_size)
        max_sl_price = pips_to_price(float(exit_cfg["sl_max_pips"]), pip_size)
        final_tp_rr = float(profile.final_tp_rr) * tp_multiplier
        partial_tp_rr = float(profile.partial_tp_rr) * tp_multiplier

        structure_level = float(candidate["structure_level"])
        min_sl_atr_multiplier = float(exit_cfg.get("min_sl_atr_multiplier", 1.5) or 1.5)
        atr_floor_price = atr_price * min_sl_atr_multiplier
        if str(candidate["side"]) == "LONG":
            raw_stop = structure_level - sl_buffer_price
            structural_risk_price = max(0.0, entry_price - raw_stop)
            risk_price = max(min_sl_price, atr_floor_price, structural_risk_price)
            risk_price = min(max_sl_price, risk_price)
            stop_loss = entry_price - risk_price
            tp1 = entry_price + (risk_price * partial_tp_rr)
            tp2 = entry_price + (risk_price * final_tp_rr)
        else:
            raw_stop = structure_level + sl_buffer_price
            structural_risk_price = max(0.0, raw_stop - entry_price)
            risk_price = max(min_sl_price, atr_floor_price, structural_risk_price)
            risk_price = min(max_sl_price, risk_price)
            stop_loss = entry_price + risk_price
            tp1 = entry_price - (risk_price * partial_tp_rr)
            tp2 = entry_price - (risk_price * final_tp_rr)

        return {
            "direction": str(candidate["side"]),
            "entry_price": normalize_price(entry_price, digits),
            "stop_loss": normalize_price(stop_loss, digits),
            "tp1": normalize_price(tp1, digits),
            "tp2": normalize_price(tp2, digits),
            "sl_distance_pips": price_to_pips(risk_price, pip_size),
            "initial_risk_price": risk_price,
            "atr_at_entry": atr_price,
            "atr_floor_price": atr_floor_price,
            "atr_floor_multiplier": min_sl_atr_multiplier,
            "setup_fingerprint": candidate["setup_fingerprint"],
            "setup_family": candidate["setup_family"],
            "trigger_type": candidate["trigger_type"],
            "regime_name": candidate["regime_name"],
            "session_name": candidate["session_name"],
            "management_profile": profile.profile_name,
            "management_profile_key": profile.strategy_family,
            "quality_tier": profile.quality_tier,
            "volatility_state": profile.volatility_state,
            "partial_tp_rr": partial_tp_rr,
            "final_tp_rr": final_tp_rr,
        }

    def rebase_trade_levels(
        self,
        trade_plan: dict[str, Any],
        entry_price: float,
        symbol_spec: dict[str, Any],
    ) -> dict[str, Any]:
        """Shift SL/TP levels to a new entry price while preserving risk geometry."""
        digits = int(symbol_spec["digits"])
        risk_price = abs(float(trade_plan["entry_price"]) - float(trade_plan["stop_loss"]))
        tp1_distance = abs(float(trade_plan["tp1"]) - float(trade_plan["entry_price"]))
        tp2_distance = abs(float(trade_plan["tp2"]) - float(trade_plan["entry_price"]))
        normalized_entry = normalize_price(entry_price, digits)
        rebased = dict(trade_plan)
        rebased["entry_price"] = normalized_entry
        if str(trade_plan["direction"]) == "LONG":
            rebased["stop_loss"] = normalize_price(normalized_entry - risk_price, digits)
            rebased["tp1"] = normalize_price(normalized_entry + tp1_distance, digits)
            rebased["tp2"] = normalize_price(normalized_entry + tp2_distance, digits)
        else:
            rebased["stop_loss"] = normalize_price(normalized_entry + risk_price, digits)
            rebased["tp1"] = normalize_price(normalized_entry - tp1_distance, digits)
            rebased["tp2"] = normalize_price(normalized_entry - tp2_distance, digits)
        return rebased

    def calculate_position_size(
        self,
        account_info: Any,
        trade_plan: dict[str, Any],
        symbol_spec: dict[str, Any],
        live_requested: bool,
        reduced_risk: bool,
        entry_mode: str,
        risk_adjustment: float = 1.0,
    ) -> dict[str, Any]:
        """Calculate dynamic position size using the risk governor multipliers."""
        sl_pips = float(trade_plan["sl_distance_pips"])
        if sl_pips <= 0:
            return {"valid": False, "reason": "Invalid SL distance", "reason_code": "invalid_sl_distance"}

        risk_cfg = self.config["risk"]
        risk_percent = float(risk_cfg["risk_percent"])
        final_reason_parts = ["base_risk"]
        if live_requested:
            risk_percent *= float(risk_cfg["live_risk_multiplier"])
            final_reason_parts.append("live_risk_multiplier")
        if reduced_risk:
            risk_percent *= float(risk_cfg["reduced_risk_multiplier_after_drawdown"])
            final_reason_parts.append("reduced_risk_multiplier_after_drawdown")
        atr_at_entry = float(trade_plan.get("atr_at_entry", 0.0) or 0.0)
        atr_median = float(trade_plan.get("atr_median", atr_at_entry or 1.0) or (atr_at_entry or 1.0))
        atr_regime_ratio = atr_at_entry / max(atr_median, 1e-9)
        if atr_regime_ratio > float(risk_cfg.get("high_vol_atr_ratio_threshold", 1.5)):
            risk_percent *= float(risk_cfg.get("high_vol_risk_multiplier", 0.5))
            final_reason_parts.append("high_volatility_risk_multiplier")
        if entry_mode == "fallback":
            risk_percent *= 0.5
            final_reason_parts.append("fallback_half_risk")
        applied_risk_adjustment = max(0.0, float(risk_adjustment or 0.0))
        if applied_risk_adjustment != 1.0:
            risk_percent *= applied_risk_adjustment
            final_reason_parts.append("risk_adjustment")

        risk_amount = float(account_info.balance) * (risk_percent / 100.0)
        pip_value_per_lot = float(symbol_spec["pip_value_per_lot"])
        if pip_value_per_lot <= 0:
            return {"valid": False, "reason": "Pip value unavailable", "reason_code": "pip_value_unavailable"}

        raw_lot = risk_amount / (sl_pips * pip_value_per_lot)
        min_volume = float(max(symbol_spec["volume_min"], risk_cfg["min_lot"], risk_cfg.get("min_practical_volume", 0.01)))
        hard_cap = float(risk_cfg["live_max_lot"] if live_requested else risk_cfg["max_lot"])
        max_volume = float(min(symbol_spec["volume_max"], hard_cap))
        step = float(symbol_spec["volume_step"])
        capped_raw_lot = min(raw_lot, max_volume)
        volume = floor_volume_to_step(capped_raw_lot, min_volume, max_volume, step)

        if volume <= 0 or volume + 1e-12 < min_volume:
            return {
                "valid": False,
                "reason": "Calculated lot falls below minimum practical volume",
                "reason_code": "volume_below_min",
                "requested_volume": raw_lot,
                "capped_volume": capped_raw_lot,
                "raw_lot": raw_lot,
                "normalized_volume": volume,
                "entry_price": float(trade_plan["entry_price"]),
                "sl_distance_pips": sl_pips,
                "risk_adjustment_multiplier": applied_risk_adjustment,
                "min_volume": min_volume,
                "max_volume": max_volume,
            }

        payload = {
            "valid": True,
            "requested_volume": raw_lot,
            "capped_volume": capped_raw_lot,
            "volume": volume,
            "raw_lot": raw_lot,
            "normalized_volume": volume,
            "risk_amount": risk_amount,
            "risk_percent": risk_percent,
            "entry_price": float(trade_plan["entry_price"]),
            "sl_distance_pips": sl_pips,
            "final_live_volume_reason": "+".join(final_reason_parts),
            "atr_regime_ratio": atr_regime_ratio,
            "risk_adjustment_multiplier": applied_risk_adjustment,
            "min_volume": min_volume,
            "max_volume": max_volume,
            "volume_step": step,
        }
        if hasattr(self.logger, "structured"):
            self.logger.structured(
                "risk_sizing",
                {
                    "entry_price": float(trade_plan["entry_price"]),
                    "sl_distance_pips": sl_pips,
                    "risk_amount": round(risk_amount, 4),
                    "risk_percent": round(risk_percent, 4),
                    "raw_lot": round(raw_lot, 4),
                    "normalized_lot": round(volume, 4),
                    "min_volume": min_volume,
                    "max_volume": max_volume,
                    "volume_step": step,
                    "live_requested": live_requested,
                    "reduced_risk": reduced_risk,
                    "entry_mode": entry_mode,
                    "atr_regime_ratio": round(atr_regime_ratio, 4),
                },
            )
        return payload

    def validate_margin(self, account_info: Any, required_margin: float) -> tuple[bool, str]:
        """Validate free margin and projected margin level."""
        if required_margin <= 0:
            return False, "Invalid required margin"
        risk_cfg = self.config["risk"]
        if not bool(risk_cfg.get("margin_protection_enabled", True)):
            return True, "Margin protection disabled"

        free_margin = float(account_info.margin_free)
        free_margin_buffer = float(risk_cfg.get("min_free_margin_buffer", 0.0))
        if free_margin < required_margin + free_margin_buffer:
            return False, "Insufficient free margin"

        projected_margin = float(account_info.margin) + required_margin
        if projected_margin <= 0:
            return True, "Margin OK"
        projected_margin_level = float(account_info.equity) / projected_margin * 100.0
        if projected_margin_level < float(risk_cfg["min_margin_level"]):
            return False, f"Projected margin level too low: {projected_margin_level:.2f}%"
        return True, "Margin OK"

    def projected_margin_level(self, account_info: Any, required_margin: float) -> float:
        """Return the projected margin level after opening a hypothetical trade."""
        projected_margin = float(account_info.margin) + float(required_margin)
        if projected_margin <= 0:
            return 0.0
        return float(account_info.equity) / projected_margin * 100.0

    def fit_volume_to_margin(
        self,
        requested_volume: float,
        account_info: Any,
        symbol_spec: dict[str, Any],
        margin_for_volume: Callable[[float], float],
    ) -> dict[str, Any]:
        """Shrink a valid risk-sized volume until it satisfies margin rules."""
        risk_cfg = self.config["risk"]
        min_volume = float(max(symbol_spec["volume_min"], risk_cfg["min_lot"]))
        max_volume = float(min(symbol_spec["volume_max"], risk_cfg["max_lot"]))
        step = float(symbol_spec["volume_step"])
        min_fit_ratio = max(0.0, min(1.0, float(risk_cfg.get("min_fitted_volume_ratio", 0.6))))
        requested_volume = floor_volume_to_step(float(requested_volume), min_volume, max_volume, step)
        fit_candidates: list[dict[str, Any]] = []
        free_margin_before = float(getattr(account_info, "margin_free", 0.0) or 0.0)
        margin_before = float(getattr(account_info, "margin", 0.0) or 0.0)
        margin_level_before = float(getattr(account_info, "margin_level", 0.0) or 0.0)

        if requested_volume <= 0 or requested_volume + 1e-12 < min_volume:
            return {
                "valid": False,
                "volume": 0.0,
                "required_margin": 0.0,
                "requested_volume": float(requested_volume),
                "fitted_volume_ratio": 0.0,
                "min_fitted_volume_ratio": min_fit_ratio,
                "reason": "Requested volume is below the broker minimum",
                "reason_code": "volume_below_min",
                "fit_candidates": fit_candidates,
                "free_margin_before": free_margin_before,
                "margin_before": margin_before,
                "margin_level_before": margin_level_before,
            }

        requested_margin = float(margin_for_volume(requested_volume))
        requested_margin_level = self.projected_margin_level(account_info, requested_margin)
        margin_ok, margin_reason = self.validate_margin(account_info, requested_margin)
        if margin_ok:
            return {
                "valid": True,
                "volume": requested_volume,
                "required_margin": requested_margin,
                "requested_volume": requested_volume,
                "requested_margin": requested_margin,
                "requested_projected_margin_level": requested_margin_level,
                "projected_margin_level": requested_margin_level,
                "fitted_volume_ratio": 1.0,
                "min_fitted_volume_ratio": min_fit_ratio,
                "reason": "Margin OK",
                "reason_code": "margin_ok",
                "fit_candidates": fit_candidates,
                "adjusted": False,
                "free_margin_before": free_margin_before,
                "margin_before": margin_before,
                "margin_level_before": margin_level_before,
            }

        candidate = requested_volume
        seen: set[float] = set()
        while candidate >= min_volume - 1e-12:
            rounded = round_volume(candidate, min_volume, max_volume, step)
            if rounded not in seen and rounded > 0:
                seen.add(rounded)
                required_margin = float(margin_for_volume(rounded))
                candidate_ok, _ = self.validate_margin(account_info, required_margin)
                projected_margin_level = self.projected_margin_level(account_info, required_margin)
                fit_ratio = rounded / requested_volume if requested_volume > 0 else 0.0
                fit_candidates.append(
                    {
                        "candidate_volume": rounded,
                        "required_margin": required_margin,
                        "projected_margin_level": projected_margin_level,
                        "fit_ratio": fit_ratio,
                        "accepted": candidate_ok,
                    }
                )
                if candidate_ok and fit_ratio + 1e-12 >= min_fit_ratio:
                    return {
                        "valid": True,
                        "volume": rounded,
                        "required_margin": required_margin,
                        "requested_volume": requested_volume,
                        "requested_margin": requested_margin,
                        "requested_projected_margin_level": requested_margin_level,
                        "projected_margin_level": projected_margin_level,
                        "fitted_volume_ratio": fit_ratio,
                        "min_fitted_volume_ratio": min_fit_ratio,
                        "reason": "Margin-adjusted volume found",
                        "reason_code": "margin_fit",
                        "fit_candidates": fit_candidates,
                        "adjusted": rounded < requested_volume,
                        "free_margin_before": free_margin_before,
                        "margin_before": margin_before,
                        "margin_level_before": margin_level_before,
                    }
            candidate -= step if step > 0 else requested_volume

        max_fit_ratio = max((float(item["fit_ratio"]) for item in fit_candidates), default=0.0)
        reason_code = "fitted_volume_ratio_below_min" if fit_candidates and max_fit_ratio < min_fit_ratio else "margin_constraint"
        final_reason = (
            f"Executable volume ratio {max_fit_ratio:.3f} is below minimum {min_fit_ratio:.3f}"
            if reason_code == "fitted_volume_ratio_below_min"
            else margin_reason
        )
        return {
            "valid": False,
            "volume": 0.0,
            "required_margin": requested_margin,
            "requested_volume": requested_volume,
            "requested_margin": requested_margin,
            "requested_projected_margin_level": requested_margin_level,
            "projected_margin_level": requested_margin_level,
            "fitted_volume_ratio": max_fit_ratio,
            "min_fitted_volume_ratio": min_fit_ratio,
            "reason": final_reason,
            "reason_code": reason_code,
            "fit_candidates": fit_candidates,
            "adjusted": False,
            "free_margin_before": free_margin_before,
            "margin_before": margin_before,
            "margin_level_before": margin_level_before,
        }

    def build_position_state(
        self,
        trade_plan: dict[str, Any],
        ticket: int | str,
        symbol: str,
        volume: float,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        opened_at: datetime,
        mode: str,
        spread_points: float,
        requested_volume: float,
        slippage: float = 0.0,
    ) -> dict[str, Any]:
        """Create the persistent trade state tracked across management cycles."""
        profile = self.profile_resolver.for_candidate(candidate, entry_assessment)
        return {
            "ticket": str(ticket),
            "position_id": str(ticket),
            "symbol": symbol,
            "mode": mode,
            "direction": trade_plan["direction"],
            "entry_price": float(trade_plan["entry_price"]),
            "sl": float(trade_plan["stop_loss"]),
            "tp1": float(trade_plan["tp1"]),
            "tp2": float(trade_plan["tp2"]),
            "volume": float(volume),
            "requested_volume": float(requested_volume),
            "volume_adjusted": abs(float(volume) - float(requested_volume)) > 1e-9,
            "executed_volume": float(volume),
            "remaining_volume": float(volume),
            "setup_fingerprint": str(candidate["setup_fingerprint"]),
            "setup_family": str(candidate["setup_family"]),
            "setup_type": str(candidate["setup_family"]),
            "regime_at_entry": str(candidate["regime_name"]),
            "session_at_entry": str(candidate["session_name"]),
            "trigger_type": str(entry_assessment.get("trigger_type") or candidate.get("trigger_type")),
            "entry_mode": str(entry_assessment.get("entry_mode", "confirmed")),
            "execution_decision": str(entry_assessment.get("execution_decision") or entry_assessment.get("entry_mode") or "confirmed"),
            "atr_at_entry": float(trade_plan["atr_at_entry"]),
            "initial_risk_price": float(trade_plan["initial_risk_price"]),
            "initial_risk_pips": float(trade_plan["sl_distance_pips"]),
            "trend_score": float(entry_assessment.get("trend_score", 0.0)),
            "setup_score": float(entry_assessment.get("setup_score", 0.0)),
            "trigger_score": float(entry_assessment.get("trigger_score", 0.0)),
            "entry_score": float(entry_assessment.get("entry_score", 0.0)),
            "quality_tier": str(entry_assessment.get("quality_tier") or trade_plan.get("quality_tier") or profile.quality_tier),
            "strategy_family": str(entry_assessment.get("strategy_family") or profile.strategy_family),
            "management_profile": str(entry_assessment.get("management_profile") or trade_plan.get("management_profile") or profile.profile_name),
            "management_profile_key": str(entry_assessment.get("management_profile_key") or trade_plan.get("management_profile_key") or profile.strategy_family),
            "volatility_state": str(entry_assessment.get("volatility_state") or trade_plan.get("volatility_state") or profile.volatility_state),
            "spread_at_entry": float(spread_points),
            "slippage": float(slippage),
            "partial_closed": False,
            "partial_closed_volume": 0.0,
            "breakeven_moved": False,
            "trailing_active": False,
            "management_bars_seen": 0,
            "opened_at": opened_at.isoformat(),
            "highest_price": float(trade_plan["entry_price"]),
            "lowest_price": float(trade_plan["entry_price"]),
            "mfe": 0.0,
            "mae": 0.0,
            "partial_realized_pnl": 0.0,
            "close_reason": None,
            "last_management_action": None,
        }

    def update_position_excursion(self, position: dict[str, Any], high: float, low: float) -> dict[str, Any]:
        """Update MFE and MAE tracking using the latest observed range."""
        position["highest_price"] = max(float(position.get("highest_price", position["entry_price"])), float(high))
        position["lowest_price"] = min(float(position.get("lowest_price", position["entry_price"])), float(low))
        risk_price = max(float(position["initial_risk_price"]), 1e-9)
        if str(position["direction"]) == "LONG":
            mfe = (float(position["highest_price"]) - float(position["entry_price"])) / risk_price
            mae = (float(position["entry_price"]) - float(position["lowest_price"])) / risk_price
        else:
            mfe = (float(position["entry_price"]) - float(position["lowest_price"])) / risk_price
            mae = (float(position["highest_price"]) - float(position["entry_price"])) / risk_price
        position["mfe"] = round(max(float(position.get("mfe", 0.0)), mfe), 4)
        position["mae"] = round(max(float(position.get("mae", 0.0)), mae), 4)
        return position

    def evaluate_management_actions(
        self,
        position: dict[str, Any],
        trigger_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        current_price: float,
        now_utc: datetime,
        symbol_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return deterministic trade-management actions for an open position."""
        if trigger_df.empty:
            return []

        actions: list[dict[str, Any]] = []
        latest = trigger_df.iloc[-1]
        self.update_position_excursion(position, float(latest["high"]), float(latest["low"]))
        position["management_bars_seen"] = int(position.get("management_bars_seen", 0) or 0) + 1
        risk_price = max(float(position["initial_risk_price"]), 1e-9)
        rr_progress = self._rr_progress(str(position["direction"]), float(position["entry_price"]), current_price, risk_price)
        exit_cfg = self.config["exit"]
        profile = self.profile_resolver.for_position(position)
        bars_seen = int(position.get("management_bars_seen", 0) or 0)
        latest_close = float(latest["close"])
        recent_failure_window = max(1, int(profile.momentum_failure_bars))
        recent = trigger_df.tail(recent_failure_window)
        if str(position["direction"]) == "LONG":
            adverse_closes = int((recent["close"] < recent["ema_20"]).sum())
        else:
            adverse_closes = int((recent["close"] > recent["ema_20"]).sum())
        adverse_fraction = adverse_closes / max(len(recent), 1)

        if (
            bool(exit_cfg.get("partial_tp_enabled", True))
            and not bool(position.get("partial_closed"))
            and rr_progress >= float(profile.partial_min_rr)
            and self._price_reached(str(position["direction"]), current_price, float(position["tp1"]))
        ):
            partial_volume = self.partial_close_volume(float(position["volume"]), symbol_spec)
            actions.append(
                {
                    "action": "partial_close",
                    "volume": partial_volume,
                    "reason_code": "partial_tp1",
                    "human_reason": "TP1 partial take-profit reached",
                }
            )

        partial_close_pending = any(action.get("action") == "partial_close" for action in actions)
        breakeven_allowed = bool(profile.breakeven_after_tp1) and (bool(position.get("partial_closed")) or partial_close_pending)
        if (
            not bool(position.get("breakeven_moved"))
            and breakeven_allowed
            and (bars_seen >= int(profile.breakeven_min_bars) or partial_close_pending)
            and rr_progress >= float(profile.breakeven_activation_rr)
        ):
            new_sl = self.breakeven_price(str(position["direction"]), float(position["entry_price"]), float(symbol_spec["pip_size"]))
            actions.append(
                {
                    "action": "move_stop",
                    "sl": new_sl,
                    "reason_code": "breakeven_after_tp1",
                    "human_reason": "Move stop to breakeven after TP1",
                }
            )

        trailing_activation_rr = float(profile.trailing_activation_rr)
        if bool(profile.trailing_enabled) and rr_progress >= trailing_activation_rr:
            position["trailing_active"] = True
            trailing_sl = self.trailing_stop_price(
                str(position["direction"]),
                current_price,
                float(setup_df.iloc[-1]["atr"]),
                float(symbol_spec["pip_size"]),
                trigger_df,
            )
            if self._is_better_stop(str(position["direction"]), float(position["sl"]), trailing_sl):
                actions.append(
                    {
                        "action": "move_stop",
                        "sl": trailing_sl,
                        "reason_code": "trailing_updated",
                        "human_reason": "Trailing stop updated",
                    }
                )

        opened_at = to_utc(position.get("opened_at"))
        if opened_at is not None:
            duration_minutes = (now_utc - opened_at).total_seconds() / 60.0
            if duration_minutes >= float(profile.time_stop_hard_minutes):
                actions.append(
                    {
                        "action": "close_full",
                        "reason_code": "time_stop_exit",
                        "human_reason": "Hard timeout reached",
                    }
                )
            elif (
                duration_minutes >= float(profile.time_stop_mid_minutes)
                and rr_progress < float(profile.time_stop_mid_min_rr)
                and adverse_fraction >= 0.50
                and float(position.get("mfe", 0.0) or 0.0) < 0.85
            ):
                actions.append(
                    {
                        "action": "close_full",
                        "reason_code": "time_stop_exit",
                        "human_reason": "Mid-stage time review failed",
                    }
                )
            elif (
                duration_minutes >= float(profile.time_stop_early_minutes)
                and rr_progress < float(profile.time_stop_early_min_rr)
                and float(position.get("mae", 0.0) or 0.0) > 0.55
                and adverse_fraction >= 0.67
            ):
                actions.append(
                    {
                        "action": "close_full",
                        "reason_code": "time_stop_exit",
                        "human_reason": "Early-stage time review failed",
                    }
                )

        if (
            bool(profile.momentum_failure_enabled)
            and bars_seen >= int(profile.momentum_failure_min_bars)
            and len(trigger_df) >= int(profile.momentum_failure_bars)
        ):
            if str(position["direction"]) == "LONG":
                continuation_absent = latest_close <= float(recent["high"].max())
                adverse_structure = latest_close < float(trigger_df.tail(3)["low"].min())
            else:
                continuation_absent = latest_close >= float(recent["low"].min())
                adverse_structure = latest_close > float(trigger_df.tail(3)["high"].max())
            momentum_failed = (
                adverse_fraction >= float(profile.momentum_failure_max_adverse_close_fraction)
                and rr_progress < float(profile.momentum_failure_rr_grace)
                and float(position.get("mfe", 0.0) or 0.0) < max(float(profile.momentum_failure_min_progress_rr), rr_progress + 0.10)
                and continuation_absent
                and adverse_structure
            )
            if momentum_failed:
                actions.append(
                    {
                        "action": "close_full",
                        "reason_code": "momentum_failure_exit",
                        "human_reason": "Momentum failure confirmed across multiple checks",
                    }
                )

        if bool(profile.structure_break_enabled) and len(setup_df) >= int(profile.structure_break_lookback_bars):
            recent_setup = setup_df.tail(int(profile.structure_break_lookback_bars))
            min_bars_required = max(0, int(profile.structure_break_min_bars_after_entry))
            consecutive_required = max(1, int(profile.structure_break_require_consecutive_closes))
            atr_buffer = float(setup_df.iloc[-1].get("atr", 0.0) or 0.0) * float(profile.structure_break_atr_buffer_multiplier)
            spread_buffer_price = float(profile.structure_break_spread_buffer_points) * float(symbol_spec["point"])
            buffer_price = atr_buffer + spread_buffer_price
            if str(position["direction"]) == "LONG":
                structure_level = float(recent_setup["low"].min()) - buffer_price
                close_window = [float(value) for value in trigger_df.tail(consecutive_required)["close"].tolist()]
                structure_break = len(close_window) >= consecutive_required and all(close < structure_level for close in close_window)
            else:
                structure_level = float(recent_setup["high"].max()) + buffer_price
                close_window = [float(value) for value in trigger_df.tail(consecutive_required)["close"].tolist()]
                structure_break = len(close_window) >= consecutive_required and all(close > structure_level for close in close_window)
            position["last_structure_break_evaluation"] = {
                "bars_since_entry": bars_seen,
                "structure_level": structure_level,
                "break_close_prices": close_window,
                "min_bars_required": min_bars_required,
                "consecutive_closes_required": consecutive_required,
                "buffer_price": buffer_price,
            }
            if bars_seen < min_bars_required:
                position["last_structure_break_evaluation"]["exit_blocked_reason"] = "min_bars_after_entry"
            elif structure_break:
                actions.append(
                    {
                        "action": "close_full",
                        "reason_code": "structure_break_exit",
                        "human_reason": "Micro structure invalidation triggered",
                        "metadata": {
                            "bars_since_entry": bars_seen,
                            "structure_level": structure_level,
                            "break_close_prices": close_window,
                            "min_bars_required": min_bars_required,
                            "consecutive_closes_required": consecutive_required,
                        },
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen = set()
        for action in actions:
            key = (action["action"], action.get("reason_code"))
            if key not in seen:
                seen.add(key)
                deduped.append(action)
        return deduped

    def partial_close_volume(self, original_volume: float, symbol_spec: dict[str, Any]) -> float:
        """Return the partial-close volume rounded to broker constraints."""
        close_fraction = float(self.config["exit"].get("partial_close_fraction", 0.5))
        min_volume = float(symbol_spec["volume_min"])
        volume_step = float(symbol_spec["volume_step"])
        target_volume = round_volume(original_volume * close_fraction, min_volume, original_volume, volume_step)
        remaining = original_volume - target_volume
        if remaining < min_volume and original_volume > min_volume:
            target_volume = round_volume(original_volume - min_volume, min_volume, original_volume, volume_step)
        return min(original_volume, target_volume if target_volume > 0 else original_volume)

    def breakeven_price(self, direction: str, entry_price: float, pip_size: float) -> float:
        """Return breakeven plus configured buffer."""
        buffer_price = pips_to_price(float(self.config["exit"]["breakeven_buffer_pips"]), pip_size)
        return entry_price + buffer_price if direction == "LONG" else entry_price - buffer_price

    def trailing_stop_price(
        self,
        direction: str,
        current_price: float,
        atr_value: float,
        pip_size: float,
        trigger_df: pd.DataFrame,
    ) -> float:
        """Calculate a one-directional structure/ATR trailing stop."""
        exit_cfg = self.config["exit"]
        lookback = max(2, int(exit_cfg.get("trailing_structure_lookback_bars", 3)))
        recent = trigger_df.tail(lookback)
        atr_distance = max(float(atr_value) * float(exit_cfg.get("trailing_atr_multiplier", 0.8)), pips_to_price(float(exit_cfg["min_trailing_stop_pips"]), pip_size))
        if direction == "LONG":
            return float(recent["low"].min()) - atr_distance
        return float(recent["high"].max()) + atr_distance

    def apply_trade_close_to_state(
        self,
        state: dict[str, Any],
        pnl: float,
        closed_at: datetime,
        session_name: str,
    ) -> dict[str, Any]:
        """Update persistent statistics, cooldowns, and lock state after a close."""
        risk_cfg = self.config["risk"]
        cooldowns = state.setdefault("cooldowns_v2", {})
        session_stats = state.setdefault("session_stats", {})
        locks = state.setdefault("locks", {})

        state["last_trade_time"] = closed_at.isoformat()
        state["daily_pnl"] = float(state.get("daily_pnl", 0.0)) + float(pnl)
        state["weekly_pnl"] = float(state.get("weekly_pnl", 0.0)) + float(pnl)
        state["total_pnl"] = float(state.get("total_pnl", 0.0)) + float(pnl)
        state["total_trades"] = int(state.get("total_trades", 0)) + 1
        session_stats["session_name"] = session_name
        session_stats["pnl"] = float(session_stats.get("pnl", 0.0)) + float(pnl)
        session_stats["trade_count"] = int(session_stats.get("trade_count", 0)) + 1

        if pnl >= 0:
            state["winning_trades"] = int(state.get("winning_trades", 0)) + 1
            state["consecutive_losses"] = 0
            state["last_trade_result"] = "WIN"
            session_stats["wins"] = int(session_stats.get("wins", 0)) + 1
        else:
            state["losing_trades"] = int(state.get("losing_trades", 0)) + 1
            state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
            state["last_trade_result"] = "LOSS"
            session_stats["losses"] = int(session_stats.get("losses", 0)) + 1

        cooldowns["entry_until"] = (closed_at + timedelta(minutes=int(self.config["cooldowns"]["execution_cooldown_minutes"]))).isoformat()
        cooldowns["reason"] = "execution_cooldown"
        if pnl < 0:
            cooldowns["post_loss_until"] = (closed_at + timedelta(minutes=int(self.config["cooldowns"]["post_loss_cooldown_minutes"]))).isoformat()
            cooldowns["reason"] = "post_loss_cooldown"
        if int(state.get("consecutive_losses", 0)) >= int(risk_cfg["max_consecutive_losses"]):
            cooldowns["consecutive_loss_until"] = (closed_at + timedelta(minutes=int(self.config["cooldowns"]["consecutive_loss_cooldown_minutes"]))).isoformat()
            cooldowns["reason"] = "consecutive_loss_cooldown"
            lock = locks.setdefault("consecutive_loss_lock", {})
            lock.update({"active": True, "reason": "max_consecutive_losses", "locked_at": closed_at.isoformat()})

        return state

    def register_journal_failure(self, state: dict[str, Any], reason: str) -> dict[str, Any]:
        """Track journaling failures and optionally disable live mode."""
        journal_state = state.setdefault("journal", {})
        journal_state["failure_count"] = int(journal_state.get("failure_count", 0)) + 1
        journal_state["last_error"] = reason
        journal_state["last_failure_at"] = utc_now().isoformat()
        locks = state.setdefault("locks", {})
        threshold = int(self.config.get("safety", {}).get("max_journal_failures_before_disable", 2))
        disable_live = bool(self.config.get("safety", {}).get("disable_live_on_journal_failure", True))
        if disable_live and journal_state["failure_count"] >= threshold:
            journal_state["disabled_live"] = True
            locks["journal_failure_lock"] = True
            locks["journal_failure_reason"] = reason
        else:
            journal_state["disabled_live"] = bool(journal_state.get("disabled_live", False))
            locks["journal_failure_lock"] = bool(locks.get("journal_failure_lock", False))
        return state

    def _active_cooldown(self, cooldowns: dict[str, Any], now_utc: datetime) -> tuple[str | None, int]:
        """Return the first active cooldown and its remaining seconds."""
        for key, reason in [
            ("consecutive_loss_until", "consecutive_loss_cooldown"),
            ("post_loss_until", "post_loss_cooldown"),
            ("entry_until", "execution_cooldown"),
        ]:
            expiry = to_utc(cooldowns.get(key))
            if expiry and expiry > now_utc:
                return reason, int((expiry - now_utc).total_seconds())
        return None, 0

    def _drawdown_pct(self, starting_balance: float, realized_pnl: float) -> float:
        """Return realized drawdown as a percentage of the reference balance."""
        if starting_balance <= 0:
            return 0.0
        return abs(min(float(realized_pnl), 0.0)) / float(starting_balance) * 100.0

    def _rr_progress(self, direction: str, entry_price: float, current_price: float, risk_price: float) -> float:
        """Return current progress in R multiples."""
        if risk_price <= 0:
            return 0.0
        if direction == "LONG":
            return (current_price - entry_price) / risk_price
        return (entry_price - current_price) / risk_price

    def _price_reached(self, direction: str, current_price: float, target_price: float) -> bool:
        """Compare price against a directional target."""
        return current_price >= target_price if direction == "LONG" else current_price <= target_price

    def _is_better_stop(self, direction: str, current_sl: float, new_sl: float) -> bool:
        """Only move stops in the profit-protecting direction."""
        return new_sl > current_sl if direction == "LONG" else new_sl < current_sl
