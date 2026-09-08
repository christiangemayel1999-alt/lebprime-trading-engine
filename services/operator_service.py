"""Shared operator actions for dashboard and Telegram control paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.audit_service import AuditService
from services.bot_control_service import BotControlService
from services.config_manager import ConfigManager


class OperatorService:
    """Centralize config-backed control actions shared by dashboard and Telegram."""

    SUPPORTED_PRESETS = {"SAFE_MODE", "BALANCED_MODE", "SCALPING_MODE", "AGGRESSIVE_MODE"}

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        audit_service: AuditService,
        bot_control_service: BotControlService,
        control_plane_service: Any | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.audit_service = audit_service
        self.bot_control_service = bot_control_service
        self.control_plane_service = control_plane_service

    def start_bot(self, actor: str) -> dict[str, Any]:
        return self.bot_control_service.start_bot(actor)

    def stop_bot(self, actor: str, force: bool = False) -> dict[str, Any]:
        return self.bot_control_service.stop_bot(actor, force=force)

    def pause_execution(self, actor: str) -> dict[str, Any]:
        return self.bot_control_service.pause_execution(actor)

    def resume_execution(self, actor: str) -> dict[str, Any]:
        return self.bot_control_service.resume_execution(actor)

    def set_kill_switch(self, actor: str, enabled: bool, reason: str | None = None) -> dict[str, Any]:
        return self.bot_control_service.set_kill_switch(actor, enabled, reason)

    def set_auto_execution(self, actor: str, enabled: bool) -> dict[str, Any]:
        return self.bot_control_service.set_auto_execution(actor, enabled)

    def set_signal_generation(self, actor: str, enabled: bool) -> dict[str, Any]:
        return self.bot_control_service.set_signal_generation(actor, enabled)

    def request_config_reload(self, actor: str) -> dict[str, Any]:
        return self.bot_control_service.request_config_reload(actor)

    def set_readonly(self, actor: str, enabled: bool) -> dict[str, Any]:
        """Toggle dashboard readonly mode."""
        return self._save_config_change(
            actor=actor,
            reason="readonly_toggle",
            action="readonly_toggle",
            target="dashboard.readonly_mode",
            updates={"dashboard": {"readonly_mode": bool(enabled)}},
            extra_details={"enabled": bool(enabled)},
        )

    def switch_mode(self, actor: str, trading_mode: str, confirmation_text: str | None = None) -> dict[str, Any]:
        """Switch the bot trading mode with LIVE confirmation protection."""
        mode = str(trading_mode or "").strip().upper()
        if mode == "LIVE" and (confirmation_text or "").strip().upper() != "LIVE":
            raise ValueError("Type LIVE to confirm switching to live mode")
        result = self._save_config_change(
            actor=actor,
            reason="mode_switch",
            action="mode_switch",
            target="bot.trading_mode",
            updates={"bot": {"trading_mode": mode}},
            extra_details={"trading_mode": mode},
        )
        self.bot_control_service.control_state.update({"runtime": {"mode": mode}})
        return result

    def switch_execution_mode(self, actor: str, execution_mode: str) -> dict[str, Any]:
        """Switch the authoritative strategy-engine mode."""
        mode = str(execution_mode or "").strip().upper()
        return self._save_config_change(
            actor=actor,
            reason="execution_mode_switch",
            action="execution_mode_switch",
            target="bot.execution_mode",
            updates={"bot": {"execution_mode": mode}},
            extra_details={"execution_mode": mode},
        )

    def toggle_family(self, actor: str, family_name: str, enabled: bool, live_allowed: bool | None = None) -> dict[str, Any]:
        """Enable/disable a setup family and its live permission."""
        before = self.config_manager.dashboard_view()
        current_family = dict(before.get("strategy", {}).get("setup_families", {}).get(family_name, {}))
        if not current_family:
            raise ValueError(f"Unknown family: {family_name}")
        updates = {
            "strategy": {
                "setup_families": {
                    family_name: {
                        **current_family,
                        "enabled": bool(enabled),
                        "live_allowed": bool(live_allowed) if live_allowed is not None else bool(current_family.get("live_allowed", False)),
                    }
                }
            }
        }
        return self._save_config_change(
            actor=actor,
            reason="family_toggle",
            action="family_toggle",
            target=family_name,
            updates=updates,
        )

    def toggle_strategy(self, actor: str, strategy_name: str, enabled: bool) -> dict[str, Any]:
        """Enable/disable an individual strategy control."""
        before = self.config_manager.dashboard_view()
        current_strategy = dict(before.get("strategy", {}).get("setup_controls", {}).get(strategy_name, {}))
        if not current_strategy:
            raise ValueError(f"Unknown strategy: {strategy_name}")
        updates = {
            "strategy": {
                "setup_controls": {
                    strategy_name: {
                        **current_strategy,
                        "enabled": bool(enabled),
                    }
                }
            }
        }
        return self._save_config_change(
            actor=actor,
            reason="strategy_toggle",
            action="strategy_toggle",
            target=strategy_name,
            updates=updates,
        )

    def apply_preset(self, actor: str, preset_name: str) -> dict[str, Any]:
        """Apply a named runtime preset."""
        preset = str(preset_name or "").strip().upper()
        if preset not in self.SUPPORTED_PRESETS:
            raise ValueError(f"Unsupported preset: {preset_name}")
        return self._save_config_change(
            actor=actor,
            reason=f"preset_{preset.lower()}",
            action="preset_apply",
            target=preset,
            updates=self._preset_updates(preset),
            extra_details={"preset": preset},
        )

    def _save_config_change(
        self,
        actor: str,
        reason: str,
        action: str,
        target: str,
        updates: dict[str, Any],
        extra_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a config mutation, audit it, and request a bot reload."""
        if self.control_plane_service is not None:
            return self.control_plane_service.update_config(
                actor=actor,
                updates=updates,
                reason=action,
                target=target,
                preview_only=False,
                extra_details=extra_details,
            )
        before = self.config_manager.load_raw()
        after = self.config_manager.apply_updates(updates)
        changes = self.config_manager.diff(before, after)
        apply_plan = self.config_manager.classify_changes(changes)
        result = self.config_manager.save(after, actor=actor, reason=reason)
        self.audit_service.log_action(actor, action, target, {"changes": changes, "apply_plan": apply_plan, **(extra_details or {})})
        if apply_plan["applied_immediately"]:
            self.bot_control_service.request_config_reload(actor)
        if apply_plan["pending_restart"]:
            self.bot_control_service.control_state.update(
                {
                    "pending_restart": True,
                    "restart_required_reasons": [change["path"] for change in apply_plan["restart_required"]],
                }
            )
        runtime_mode = str(after.get("bot", {}).get("trading_mode", "") or "").strip().upper()
        if runtime_mode:
            self.bot_control_service.control_state.update({"runtime": {"mode": runtime_mode}})
        return {**result, "changes": changes, "apply_plan": apply_plan}

    @staticmethod
    def _preset_updates(preset: str) -> dict[str, Any]:
        """Return cohesive config updates for a preset."""
        if preset == "SAFE_MODE":
            return {
                "bot": {"force_reduced_risk_mode": True},
                "execution": {"max_spread_points": 20.0, "duplicate_history_minutes": 60},
                "risk": {
                    "risk_percent": 0.20,
                    "max_trades_per_day": 2,
                    "max_trades_per_symbol": 1,
                    "max_lot": 0.50,
                },
                "exit": {"trailing_enabled": True, "breakeven_after_tp1": True},
                "cooldowns": {"execution_cooldown_minutes": 12},
            }
        if preset == "BALANCED_MODE":
            return {
                "bot": {"force_reduced_risk_mode": False},
                "execution": {"max_spread_points": 25.0, "duplicate_history_minutes": 45},
                "risk": {
                    "risk_percent": 0.35,
                    "max_trades_per_day": 5,
                    "max_trades_per_symbol": 3,
                    "max_lot": 1.00,
                },
                "entry": {"setup_entry_distance_atr_tolerance": 0.80},
                "cooldowns": {"execution_cooldown_minutes": 8},
            }
        if preset in {"SCALPING_MODE", "AGGRESSIVE_MODE"}:
            return {
                "bot": {"force_reduced_risk_mode": False},
                "execution": {"max_spread_points": 30.0, "duplicate_history_minutes": 45},
                "risk": {
                    "risk_percent": 0.50,
                    "max_trades_per_day": 6,
                    "max_trades_per_symbol": 4,
                    "max_lot": 1.50,
                },
                "entry": {"setup_entry_distance_atr_tolerance": 0.85},
                "cooldowns": {"execution_cooldown_minutes": 8},
                "strategy": {
                    "min_score_to_trade": 47.0,
                    "require_trend_alignment": False,
                    "live_thresholds": {
                        "live_min_trend_score": 40.0,
                        "live_min_setup_score": 48.0,
                        "live_min_trigger_score": 25.0,
                        "live_min_entry_score": 47.0,
                    },
                    "entry_modes": {
                        "aggressive_enabled": True,
                        "confirmed_min_entry_score": 50.0,
                        "aggressive_min_entry_score": 60.0,
                    },
                    "setup_controls": {
                        "liquidity_sweep_reversal": {
                            "confirmation_required": False,
                            "min_trend_score": 40.0,
                            "min_setup_score": 50.0,
                            "min_trigger_score": 25.0,
                            "min_entry_score": 47.0,
                            "cooldown_minutes": 8,
                            "max_trades_per_session": 3,
                        },
                        "compression_release": {
                            "confirmation_required": False,
                            "min_trend_score": 40.0,
                            "min_setup_score": 50.0,
                            "min_trigger_score": 25.0,
                            "min_entry_score": 47.0,
                            "cooldown_minutes": 8,
                            "max_trades_per_session": 3,
                        },
                        "breakout_retest_continuation": {
                            "confirmation_required": False,
                            "min_trend_score": 45.0,
                            "min_setup_score": 52.0,
                            "min_trigger_score": 28.0,
                            "min_entry_score": 50.0,
                            "cooldown_minutes": 8,
                            "max_trades_per_session": 3,
                        },
                        "trend_pullback_reclaim": {
                            "confirmation_required": False,
                            "min_trend_score": 45.0,
                            "min_setup_score": 52.0,
                            "min_trigger_score": 28.0,
                            "min_entry_score": 50.0,
                            "cooldown_minutes": 8,
                            "max_trades_per_session": 3,
                        },
                    }
                },
            }
        raise ValueError(f"Unsupported preset: {preset}")
