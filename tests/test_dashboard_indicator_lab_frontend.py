from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "dashboard" / "static" / "dashboard.js"


def test_indicator_lab_uses_persistent_draft_state() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "indicatorLabDraft: {" in source
    assert "indicatorLabUi: { dirty: false, pauseRefreshUntil: 0, submitting: false" in source
    assert "function captureIndicatorLabDraft" in source
    assert 'draft.dataset.start_date ?? ""' in source
    assert 'draft.dataset.end_date ?? ""' in source
    assert 'draft.dataset.name ?? "XAUUSD_M1_2026_08_01_2026_09_14"' in source
    assert 'draft.dataset.notes ?? ""' in source
    assert 'draft.campaign.name ?? "Indicator Campaign"' in source
    assert 'draft.campaign.notes ?? ""' in source
    assert "const value = paramDraft[param.name] ?? param.default" in source
    assert "const opt = asObject(optDraft[param.name])" in source
    assert "captureIndicatorLabDraft();" in source
    assert "const preserveIndicatorLabEditing = !options.force && indicatorLabInteractionLocked();" in source
    assert 'safeRenderSection("section-indicator-lab", "Indicator Lab", renderIndicatorLab);' in source


def test_indicator_lab_preserves_all_parameters_and_native_date_inputs() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")

    assert 'field("il-ds-start", "Start", draft.dataset.start_date ?? "", "date")' in source
    assert 'field("il-ds-end", "End", draft.dataset.end_date ?? "", "date")' in source
    assert "state.indicatorLab.parameters[indicator]).map((param)" in source
    assert ".slice(0, 16)" not in source
    assert "il-opt-values" in source


def test_indicator_lab_campaign_start_has_visible_submitting_and_selection_flow() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "if (state.indicatorLabUi.submitting) return;" in source
    assert "state.indicatorLabUi.submitting = true;" in source
    assert 'state.indicatorLabUi.message = "Creating campaign...";' in source
    assert 'button.textContent = "Creating campaign...";' in source
    assert "const campaignId = indicatorLabCampaignId(created);" in source
    assert "state.indicatorLab.selectedCampaignId = campaignId;" in source
    assert "state.indicatorLab.selectedTestId = null;" in source
    assert "state.indicatorLabUi.message = `Campaign #${campaignId} created`;" in source
    assert "state.indicatorLabUi.error = error.message;" in source
    assert "${ui.error ? `<div class=\"col-12\"><div class=\"alert alert-danger py-2\">${esc(ui.error)}</div></div>` : \"\"}" in source


def test_indicator_lab_campaign_progress_and_result_auto_selection_are_rendered() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "function selectIndicatorLabResultForCampaign" in source
    assert "await selectIndicatorLabResultForCampaign(selected);" in source
    assert "campaign?.best_test_id" in source
    assert "results.length === 1 ? results[0]" in source
    assert "lab.selectedTestId = num(target.id);" in source
    assert "lab.zones = zones.zones || [];" in source
    assert "function indicatorLabProgress" in source
    assert "Math.round((current / total) * 100)" in source
    assert "${esc(progress.current)} / ${esc(progress.total)} <span class=\"text-secondary\">(${esc(progress.pct)}%)</span>" in source


def test_indicator_lab_zone_explorer_filters_details_and_exports_are_present() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")

    assert "Zone Explorer" in source
    assert "il-zone-side" in source
    assert "il-zone-status" in source
    assert "il-zone-from" in source
    assert "il-zone-to" in source
    assert "il-zone-search" in source
    assert "il-zone-flipped" in source
    assert "Previous Zone" in source
    assert "Next Zone" in source
    assert "Open Replay" in source
    assert "Export Zones CSV" in source
    assert "Export Candidates" in source
    assert "Export Events CSV" in source
    assert "Export Results CSV" in source
    assert "function renderZoneDetails" in source
    assert "Parity Data" in source
    assert "Parameter Snapshot" in source
    assert "api(`/api/indicator-lab/zones/${id}`)" in source
    assert "api(`/api/indicator-lab/zones/${id}/events`)" in source
    assert "zones/export.csv" in source
    assert "candidates/export.csv" in source
    assert "events/export.csv" in source
    assert "results/export.csv" in source
