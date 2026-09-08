from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_bot.core.families import management_profile_for_setup, quality_tier, volatility_state


@dataclass(slots=True)
class ManagementProfileSettings:
    profile_name: str
    strategy_family: str
    quality_tier: str
    volatility_state: str
    sl_buffer_atr_multiplier: float
    partial_tp_rr: float
    partial_min_rr: float
    final_tp_rr: float
    breakeven_after_tp1: bool
    breakeven_activation_rr: float
    breakeven_min_bars: int
    trailing_enabled: bool
    trailing_activation_rr: float
    max_trade_duration_minutes: float
    time_stop_early_minutes: float
    time_stop_mid_minutes: float
    time_stop_hard_minutes: float
    time_stop_early_min_rr: float
    time_stop_mid_min_rr: float
    momentum_failure_enabled: bool
    momentum_failure_min_bars: int
    momentum_failure_bars: int
    momentum_failure_rr_grace: float
    momentum_failure_min_progress_rr: float
    momentum_failure_max_adverse_close_fraction: float
    structure_break_enabled: bool
    structure_break_lookback_bars: int
    structure_break_min_bars_after_entry: int
    structure_break_require_consecutive_closes: int
    structure_break_atr_buffer_multiplier: float
    structure_break_spread_buffer_points: float


class TradeManagementProfileResolver:
    """Resolve family-aware trade management settings from config and context."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def for_candidate(self, candidate: dict[str, Any], entry: dict[str, Any] | None = None, market_context: dict[str, Any] | None = None) -> ManagementProfileSettings:
        profile_key = management_profile_for_setup(str(candidate.get("setup_family") or ""))
        return self._resolve(profile_key, candidate, entry or {}, market_context)

    def for_position(self, position: dict[str, Any], market_context: dict[str, Any] | None = None) -> ManagementProfileSettings:
        profile_key = str(position.get("management_profile_key") or management_profile_for_setup(str(position.get("setup_family") or "")))
        pseudo_candidate = {
            "setup_family": position.get("setup_family"),
            "regime_name": position.get("regime_at_entry"),
        }
        pseudo_entry = {
            "entry_score": position.get("entry_score"),
        }
        return self._resolve(profile_key, pseudo_candidate, pseudo_entry, market_context)

    def _resolve(
        self,
        profile_key: str,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        market_context: dict[str, Any] | None,
    ) -> ManagementProfileSettings:
        exit_cfg = self.config.get("exit", {}) if isinstance(self.config, dict) else {}
        profiles = exit_cfg.get("family_profiles", {}) if isinstance(exit_cfg.get("family_profiles"), dict) else {}
        family_specific_enabled = bool(exit_cfg.get("enable_family_specific_management", True))
        family_cfg = profiles.get(profile_key, {}) if family_specific_enabled and isinstance(profiles.get(profile_key), dict) else {}
        score = float(entry.get("entry_score") or candidate.get("setup_score") or 0.0)
        tier = quality_tier(score, self.config)
        vol_state = volatility_state(market_context, candidate)
        partial_rr = float(family_cfg.get("partial_tp_rr", exit_cfg.get("partial_tp_rr", 1.0)) or 1.0)
        final_rr = float(family_cfg.get("final_tp_rr", exit_cfg.get("final_tp_rr", 2.0)) or 2.0)
        if family_specific_enabled and tier == "high" and str(candidate.get("regime_name") or "").upper() in {"TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT"}:
            final_rr += float(family_cfg.get("tp_extension_rr", 0.0) or 0.0)
        if family_specific_enabled and tier == "marginal":
            final_rr = min(final_rr, float(family_cfg.get("marginal_final_tp_rr_cap", final_rr) or final_rr))

        resolved_profile_name = str(family_cfg.get("label") or f"{profile_key.title()}ManagementProfile")
        resolved_profile_key = str(profile_key)
        if not family_specific_enabled:
            resolved_profile_name = "TradeManagementProfile"
            resolved_profile_key = "default"

        return ManagementProfileSettings(
            profile_name=resolved_profile_name,
            strategy_family=resolved_profile_key,
            quality_tier=tier,
            volatility_state=vol_state,
            sl_buffer_atr_multiplier=float(family_cfg.get("sl_buffer_atr_multiplier", 1.0) or 1.0),
            partial_tp_rr=partial_rr,
            partial_min_rr=float(family_cfg.get("partial_min_rr", exit_cfg.get("partial_min_rr", partial_rr)) or partial_rr),
            final_tp_rr=final_rr,
            breakeven_after_tp1=bool(family_cfg.get("breakeven_after_tp1", exit_cfg.get("breakeven_after_tp1", True))),
            breakeven_activation_rr=float(family_cfg.get("breakeven_activation_rr", exit_cfg.get("breakeven_activation_rr", 1.0)) or 1.0),
            breakeven_min_bars=int(family_cfg.get("breakeven_min_bars", exit_cfg.get("breakeven_min_bars", 2)) or 2),
            trailing_enabled=bool(family_cfg.get("trailing_enabled", exit_cfg.get("trailing_enabled", True))),
            trailing_activation_rr=float(family_cfg.get("trailing_activation_rr", exit_cfg.get("trailing_activation_rr", 1.8)) or 1.8),
            max_trade_duration_minutes=float(family_cfg.get("max_trade_duration_minutes", exit_cfg.get("max_trade_duration_minutes", 45)) or 45),
            time_stop_early_minutes=float(family_cfg.get("time_stop_early_minutes", exit_cfg.get("time_stop_early_minutes", 12.0)) or 12.0),
            time_stop_mid_minutes=float(family_cfg.get("time_stop_mid_minutes", exit_cfg.get("time_stop_mid_minutes", 24.0)) or 24.0),
            time_stop_hard_minutes=float(family_cfg.get("time_stop_hard_minutes", family_cfg.get("max_trade_duration_minutes", exit_cfg.get("max_trade_duration_minutes", 45))) or 45),
            time_stop_early_min_rr=float(family_cfg.get("time_stop_early_min_rr", exit_cfg.get("time_stop_early_min_rr", 0.10)) or 0.10),
            time_stop_mid_min_rr=float(family_cfg.get("time_stop_mid_min_rr", exit_cfg.get("time_stop_mid_min_rr", 0.25)) or 0.25),
            momentum_failure_enabled=bool(family_cfg.get("momentum_failure_exit_enabled", exit_cfg.get("momentum_failure_exit_enabled", True))),
            momentum_failure_min_bars=int(family_cfg.get("momentum_failure_min_bars", exit_cfg.get("momentum_failure_min_bars", 3)) or 3),
            momentum_failure_bars=int(family_cfg.get("momentum_failure_bars", exit_cfg.get("momentum_failure_bars", 3)) or 3),
            momentum_failure_rr_grace=float(family_cfg.get("momentum_failure_rr_grace", 0.8) or 0.8),
            momentum_failure_min_progress_rr=float(family_cfg.get("momentum_failure_min_progress_rr", exit_cfg.get("momentum_failure_min_progress_rr", 0.12)) or 0.12),
            momentum_failure_max_adverse_close_fraction=float(family_cfg.get("momentum_failure_max_adverse_close_fraction", exit_cfg.get("momentum_failure_max_adverse_close_fraction", 0.67)) or 0.67),
            structure_break_enabled=bool(family_cfg.get("structure_break_exit_enabled", exit_cfg.get("structure_break_exit_enabled", True))),
            structure_break_lookback_bars=int(family_cfg.get("structure_break_lookback_bars", 3) or 3),
            structure_break_min_bars_after_entry=int(
                family_cfg.get(
                    "structure_break_min_bars_after_entry",
                    (exit_cfg.get("structure_break_exit") or {}).get("min_bars_after_entry", 0),
                )
                or 0
            ),
            structure_break_require_consecutive_closes=int(
                family_cfg.get(
                    "structure_break_require_consecutive_closes",
                    (exit_cfg.get("structure_break_exit") or {}).get("require_consecutive_closes", 1),
                )
                or 1
            ),
            structure_break_atr_buffer_multiplier=float(
                family_cfg.get(
                    "structure_break_atr_buffer_multiplier",
                    (exit_cfg.get("structure_break_exit") or {}).get("atr_buffer_multiplier", 0.0),
                )
                or 0.0
            ),
            structure_break_spread_buffer_points=float(
                family_cfg.get(
                    "structure_break_spread_buffer_points",
                    (exit_cfg.get("structure_break_exit") or {}).get("spread_buffer_points", 0.0),
                )
                or 0.0
            ),
        )
