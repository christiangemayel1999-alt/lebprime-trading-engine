"""Canonical dashboard/Telegram control-plane service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.audit_service import AuditService
from services.bot_control_service import BotControlService
from services.config_manager import ConfigManager
from services.control_state import ControlStateService
from utils import utc_now


class ControlPlaneService:
    """Single backend source of truth for dashboard and remote control changes."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        control_state: ControlStateService,
        audit_service: AuditService,
        bot_control_service: BotControlService,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.control_state = control_state
        self.audit_service = audit_service
        self.bot_control_service = bot_control_service

    def get_config(self) -> dict[str, Any]:
        """Return effective config and apply metadata."""
        view = self.config_manager.control_plane_view()
        runtime_state = self.control_state.summarize()
        view["effective_config"] = view["config"]
        view["runtime_state"] = runtime_state
        view["source_of_truth"] = self.config_manager.source_of_truth_snapshot(runtime_state=runtime_state)
        return view

    def get_section(self, section: str) -> dict[str, Any]:
        """Return one dashboard-control section from effective config/state."""
        config = self.config_manager.dashboard_view()
        state = self.control_state.summarize()
        section_name = str(section).strip().lower()
        if section_name == "runtime":
            return {
                "runtime": state,
                "resolved_runtime": self.config_manager.resolved_runtime_snapshot(),
                "source_of_truth": self.config_manager.source_of_truth_snapshot(runtime_state=state),
            }
        if section_name == "mode":
            return {
                "mode": config.get("mode", config.get("bot", {}).get("execution_mode_summary", {})),
                "resolved_runtime": self.config_manager.resolved_runtime_snapshot(config),
                "options": self.config_manager.control_plane_view().get("metadata", {}).get("execution_mode_options", []),
            }
        if section_name == "strategies":
            return {
                "setup_families": config.get("strategy", {}).get("setup_families", {}),
                "setup_controls": config.get("strategy", {}).get("setup_controls", {}),
                "entry_modes": config.get("strategy", {}).get("entry_modes", {}),
            }
        if section_name in {"risk", "execution", "sessions", "symbols", "backtest"}:
            return {section_name: config.get(section_name, {})}
        raise ValueError(f"Unsupported control section: {section}")

    def update_config(
        self,
        *,
        actor: str,
        updates: dict[str, Any],
        reason: str,
        target: str,
        preview_only: bool = False,
        extra_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate, persist, audit, and apply/reload one config mutation."""
        before = self.config_manager.load_raw()
        after = self.config_manager.apply_updates(updates)
        changes = self.config_manager.diff(before, after)
        apply_plan = self.config_manager.classify_changes(changes)
        if preview_only:
            return {"ok": True, "preview_only": True, "changes": changes, "apply_plan": apply_plan, "config": after}

        result = self.config_manager.save(after, actor=actor, reason=reason)
        apply_record = {
            "actor": actor,
            "reason": reason,
            "target": target,
            "applied_at": utc_now().isoformat(),
            "change_count": len(changes),
            "hot_reloadable_count": int(apply_plan["hot_reloadable_count"]),
            "restart_required_count": int(apply_plan["restart_required_count"]),
            "applied_immediately": bool(apply_plan["applied_immediately"]),
            "pending_restart": bool(apply_plan["pending_restart"]),
        }
        self.audit_service.log_action(
            actor,
            reason,
            target,
            {
                "changes": changes,
                "apply_plan": apply_plan,
                "applied_immediately": bool(apply_plan["applied_immediately"]),
                "pending_restart": bool(apply_plan["pending_restart"]),
                **(extra_details or {}),
            },
        )
        if apply_plan["applied_immediately"]:
            self.bot_control_service.request_config_reload(actor)
        self.control_state.update({"last_config_apply": apply_record})
        if apply_plan["pending_restart"]:
            self.control_state.update(
                {
                    "pending_restart": True,
                    "restart_required_reasons": [change["path"] for change in apply_plan["restart_required"]],
                }
            )
        runtime_mode = str(after.get("bot", {}).get("trading_mode", "") or "").strip().upper()
        if runtime_mode:
            self.control_state.update({"runtime": {"mode": runtime_mode}})
        return {**result, "changes": changes, "apply_plan": apply_plan}

    def clear_pending_restart(self, actor: str) -> dict[str, Any]:
        """Clear restart-required marker after a controlled restart/apply."""
        state = self.control_state.mark_command(
            "clear_pending_restart",
            actor,
            pending_restart=False,
            restart_required_reasons=[],
        )
        self.audit_service.log_action(actor, "clear_pending_restart", "runtime", {})
        return {"ok": True, "message": "Pending restart marker cleared", "control_state": state}
