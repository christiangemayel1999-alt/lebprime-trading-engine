from __future__ import annotations

import json
from pathlib import Path

from services.config_manager import ConfigManager
from strategy_factory import IGNORED_LEGACY_LIVE_CONTROL_KEYS, LIVE_SUPPORT_ENGINE_TYPE, build_live_runtime_support_engine
from trading_bot.core.execution_mode import V1_BASELINE, apply_execution_mode_to_config


ROOT = Path(__file__).resolve().parents[1]


def test_active_dashboard_has_one_strategy_control_surface() -> None:
    template = (ROOT / "dashboard" / "templates" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert 'data-section="bot-control"' not in template
    assert 'data-section="families"' not in template
    assert 'data-section="strategies"' in template
    assert 'safeRenderSection("section-bot-control"' not in source
    assert 'safeRenderSection("section-families"' not in source
    assert 'safeRenderSection("section-strategies", "Strategies", renderStrategies);' in source
    header_renderer = source.split("function renderTopStatusBar()", 1)[1].split("function renderQuickActions()", 1)[0]
    assert "V1" not in header_renderer
    assert "V2" not in header_renderer
    assert "rollout" not in header_renderer.lower()


def test_final_strategy_page_contains_required_controls_and_metrics() -> None:
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
    editor = source.split("function mountLebprimeStrategyEditor()", 1)[1].split("function syncLebprimeStrategyEditor()", 1)[0]

    for label in ("lebprime_v3_4_0", "Enabled", "DRY", "LIVE DEMO", "XAUUSD", "US30", "NAS100"):
        assert label in editor
    for timeframe in ("M1", "M3", "M5", "M15", "M30", "M45"):
        assert timeframe in editor
    for metric in ("Families", "Pending Orders", "Broker Orders", "Open Positions", "Duplicate Suppressed"):
        assert metric in editor
    for column in ("Health", "Last Closed Candle", "Last Scan", "Candidates", "Live Zones", "Pending LIMITs", "Active Trades", "Last Error", "Heartbeat"):
        assert column in editor


def test_lebprime_editor_is_stable_and_uses_dirty_draft_state() -> None:
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "strategiesUi: { mounted: false, dirty: false, saving: false, draft: null, lastSaveRestartRequired: false }" in source
    assert "function mountLebprimeStrategyEditor" in source
    assert "if (!container || state.strategiesUi.mounted) return;" in source
    assert "if (state.strategiesUi.dirty) return;" in source
    assert "function updateLebprimeStrategyStatus" in source
    assert 'safeSetHtml("lebprime-context-status-body"' in source
    assert "state.strategiesUi.dirty = false;" in source


def test_polling_does_not_replace_controls_or_dirty_values() -> None:
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
    active_renderer = source.rsplit("function renderStrategies()", 1)[1].split("function renderRisk()", 1)[0]

    assert "mountLebprimeStrategyEditor();" in active_renderer
    assert "syncLebprimeStrategyEditor();" in active_renderer
    assert "updateLebprimeStrategyStatus();" in active_renderer
    assert ".innerHTML" not in active_renderer
    assert "if (state.strategiesUi.dirty) return;" in source
    assert "setInterval(refreshStatus" in source
    for _ in range(10):
        assert "state.strategiesUi.mounted" in source


def test_runtime_buttons_are_stable_guarded_and_fully_wired() -> None:
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "runtimeActionsUi: { mounted: false, pending: null }" in source
    assert "if (!state.runtimeActionsUi.mounted)" in source
    assert 'container.addEventListener("click", handleRuntimeActionClick);' in source
    assert "if (state.runtimeActionsUi.pending) return false;" in source
    assert 'button.setAttribute("aria-busy"' in source
    for action in ("start", "stop", "pause", "resume", "dry", "live", "kill", "safe"):
        assert f'data-runtime-action="{action}"' in source
    for endpoint in ("start", "stop", "pause", "resume", "kill-switch"):
        assert f'runControl("/api/control/{endpoint}"' in source
    assert 'switchMode("DRY_RUN")' in source
    assert 'switchMode("LIVE")' in source
    assert 'applyPreset("SAFE_MODE")' in source


def test_save_and_unsaved_workflow_is_explicit() -> None:
    source = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "function validateLebprimeDraft" in source
    assert "function readLebprimeStrategyDraft" in source
    assert "function updateLebprimeDraftState" in source
    assert "if (state.strategiesUi.saving) return;" in source
    assert "state.strategiesUi.draft = localDraft;" in source
    assert '"Unsaved changes"' in source
    assert '"Saved - RESTART REQUIRED"' in source
    assert "You have unsaved LebPrime changes. Leave Strategies and discard them?" in source
    assert 'window.addEventListener("beforeunload"' in source


def test_live_config_and_support_engine_disable_legacy_strategies() -> None:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    assert "execution_mode" not in config["bot"]
    assert "rollout_phase" not in config["bot"]
    assert config["lebprime_v3_4_0"]["enabled"] is True
    assert all(not row["enabled"] and not row["live_allowed"] for row in config["strategy"]["setup_controls"].values())
    assert all(not row["enabled"] and not row["live_allowed"] for row in config["strategy"]["setup_families"].values())

    engine = build_live_runtime_support_engine(config, ROOT)
    assert engine.engine_type == LIVE_SUPPORT_ENGINE_TYPE
    assert all(not row["enabled"] and not row["live_allowed"] for row in engine.config["strategy"]["setup_controls"].values())


def test_historical_mode_resolution_remains_available_without_mutating_live_controls() -> None:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    resolved_config, resolved = apply_execution_mode_to_config(
        config,
        V1_BASELINE,
        apply_legacy_strategy_controls=False,
    )

    assert resolved.selected_mode == V1_BASELINE
    assert all(not row["enabled"] for row in resolved_config["strategy"]["setup_controls"].values())
    assert all(not row["enabled"] for row in resolved_config["strategy"]["setup_families"].values())


def test_config_migration_preserves_unknown_keys_and_creates_backup(tmp_path: Path) -> None:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config["future_extension"] = {"unknown_key": {"preserve_me": 17}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    manager = ConfigManager(tmp_path)

    updated = manager.apply_updates({"lebprime_v3_4_0": {"bars_to_fetch": 420}})
    assert updated["future_extension"]["unknown_key"]["preserve_me"] == 17
    result = manager.save(updated, actor="test", reason="migration_test")
    backup = json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))

    assert backup["future_extension"]["unknown_key"]["preserve_me"] == 17
    assert backup["lebprime_v3_4_0"]["bars_to_fetch"] == 360
    assert manager.load_raw()["lebprime_v3_4_0"]["bars_to_fetch"] == 420


def test_ignored_legacy_live_control_keys_are_documented() -> None:
    assert IGNORED_LEGACY_LIVE_CONTROL_KEYS == (
        "bot.execution_mode",
        "bot.rollout_phase",
        "strategy.lebprim_mode",
        "strategy.setup_controls.<legacy_strategy>.enabled",
        "strategy.setup_controls.<legacy_strategy>.live_allowed",
        "strategy.setup_families.<legacy_strategy>.enabled",
        "strategy.setup_families.<legacy_strategy>.live_allowed",
        "backtest.selected_strategies",
    )
