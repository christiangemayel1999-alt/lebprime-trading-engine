/* global window, document, fetch */
(function () {
    const state = {
        config: window.__INITIAL_CONFIG__ || {},
        configMetadata: {},
        status: window.__INITIAL_STATUS__ || {},
        auth: window.__DASHBOARD_AUTH__ || {},
        logs: [],
        audit: [],
        positions: [],
        workerStatus: window.__INITIAL_STATUS__?.worker || null,
        jobs: [],
        strategiesUi: { mounted: false, dirty: false, saving: false, draft: null, lastSaveRestartRequired: false },
        runtimeActionsUi: { mounted: false, pending: null },
        indicatorLab: {
            datasets: [],
            campaigns: [],
            parameters: {},
            selectedIndicator: "LEBPRIME",
            selectedCampaignId: null,
            selectedTestId: null,
            results: [],
            zones: [],
            selectedZoneId: null,
            selectedZone: null,
            zoneEvents: [],
            zoneFilters: { side: "ALL", status: "ALL", from: "", to: "", search: "", flipped: "ALL" },
            events: [],
            draft: {},
            error: null,
        },
        indicatorLabDraft: {
            dataset: {},
            campaign: {},
            parameters: {},
            optimization: {},
        },
        indicatorLabUi: { dirty: false, pauseRefreshUntil: 0, submitting: false, message: "", error: "", lastCreatedCampaignId: null },
        lebprimeLive: null,
        activeSection: "overview",
    };
    let preservedUiState = null;
    const FALLBACK_MODE_OPTIONS = [
        { value: "V1_BASELINE", label: "V1 Baseline" },
        { value: "V2_FULL", label: "V2 Full" },
    ];
    const FAMILY_DISPLAY_NAMES = {
        BREAKOUT_RETEST_CONTINUATION: "BREAKOUT",
        COMPRESSION_RELEASE: "COMPRESS",
        LEBPRIM_SCALP: "LEBPRIM",
        TREND_PULLBACK_RECLAIM: "TREND PULLBACK",
        LIQUIDITY_SWEEP_REVERSAL: "LIQUIDITY SWEEP",
    };
    const BACKTEST_REPLAY_WINDOW_SIZE = 240;
    const BACKTEST_REPLAY_PREFETCH = 36;
    const BACKTEST_REPLAY_MAX_CACHE = 420;
    const BACKTEST_REPLAY_SPEED_MAP = { 1: 700, 5: 260, 10: 140, 25: 80, 50: 50 };
    const BACKTEST_REPLAY_SPEEDS = [1, 5, 10, 25, 50];

    function dashboardLog(scope, message, details = null, level = "log") {
        const logger = (console && typeof console[level] === "function") ? console[level] : console.log;
        if (details !== null && details !== undefined) {
            logger(`[dashboard:${scope}] ${message}`, details);
            return;
        }
        logger(`[dashboard:${scope}] ${message}`);
    }

    async function api(path, options = {}) {
        const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
        const text = await response.text();
        let payload = {};
        try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
        if (!response.ok) throw new Error(payload.detail || payload.message || `Request failed: ${response.status}`);
        return payload;
    }

    const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const num = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
    const val = (id) => document.getElementById(id)?.value || "";
    const on = (id) => !!document.getElementById(id)?.checked;
    const csv = (value) => String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
    const field = (id, label, value, type = "text", step = "") => `<label class="form-label w-100"><span class="small text-secondary">${esc(label)}</span><input class="form-control" id="${id}" type="${type}" value="${esc(value ?? "")}" ${step ? `step="${step}"` : ""}></label>`;
    const small = (id, value, type = "text", step = "") => `<input class="form-control form-control-sm" id="${id}" type="${type}" value="${esc(value ?? "")}" ${step ? `step="${step}"` : ""}>`;
    const toggle = (id, label, value) => `<div class="form-check form-switch"><input class="form-check-input" id="${id}" type="checkbox" ${value ? "checked" : ""}><label class="form-check-label" for="${id}">${esc(label)}</label></div>`;
    const select = (id, label, options, selected) => `<label class="form-label w-100"><span class="small text-secondary">${esc(label)}</span><select class="form-select" id="${id}">${options.map((option) => `<option ${option === selected ? "selected" : ""}>${esc(option)}</option>`).join("")}</select></label>`;
    const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
    // LebPrime symbols connected on the MT5 broker account. key = logical symbol used in config and zone IDs.
    const LEBPRIME_SYMBOLS = [
        { key: "XAUUSD", broker: "XAUUSD", name: "GOLD vs USD", buffer: "gold_buffer", bufferDefault: 1, step: "0.1" },
        { key: "US30", broker: "US30", name: "US Wall Street 30 Index", buffer: "us30_buffer", bufferDefault: 30, step: "1" },
        { key: "NAS100", broker: "USTEC", name: "US Tech 100 Index", buffer: "nas_buffer", bufferDefault: 15, step: "1" },
        { key: "XAGUSD", broker: "XAGUSD", name: "Silver vs USD", buffer: "silver_buffer", bufferDefault: 0.1, step: "0.01" },
        { key: "WTI", broker: "WTI", name: "Spot WTI Light Crude Oil", buffer: "wti_buffer", bufferDefault: 0.15, step: "0.01" },
        { key: "EURUSD", broker: "EURUSD", name: "Euro vs US Dollar", buffer: "forex_buffer", bufferDefault: 0.0005, step: "0.0001" },
    ];
    const LEBPRIME_BROKER_SYMBOLS = LEBPRIME_SYMBOLS.map((item) => item.broker);
    const lebprimeSymbolLabel = (item) => (item.broker === item.key ? item.key : `${item.broker} (${item.key})`);
    const lebprimeSpreadId = (item) => `lebprime-spread-${item.key.toLowerCase()}`;
    const lebprimeBufferId = (item) => `lebprime-buffer-${item.key.toLowerCase()}`;
    const lebprimeSymbolCheckboxes = (selected = new Set()) => LEBPRIME_SYMBOLS.map((item) => `<label class="form-check-label" title="${esc(item.name)}"><input class="form-check-input me-1" type="checkbox" data-lebprime-symbol="${item.key}" ${selected.has(item.key) ? "checked" : ""}>${esc(lebprimeSymbolLabel(item))}</label>`).join("");
    const asArray = (value) => (Array.isArray(value) ? value : []);
    const textOr = (value, fallback = "") => {
        if (value === null || value === undefined) return fallback;
        const output = String(value).trim();
        return output || fallback;
    };
    const formatFamilyName = (value) => {
        const normalized = textOr(value, "UNKNOWN").toUpperCase();
        return FAMILY_DISPLAY_NAMES[normalized] || normalized.replace(/_/g, " ");
    };
    const stateTone = (value) => {
        const normalized = textOr(value, "IDLE").toUpperCase();
        if (["COMPLETED", "SUCCESS", "READY"].includes(normalized)) return "success";
        if (["RUNNING", "QUEUED", "STARTING"].includes(normalized)) return "info";
        if (["PARTIAL", "WARNING", "PAUSED"].includes(normalized)) return "warning";
        if (["FAILED", "ERROR", "INTERRUPTED"].includes(normalized)) return "danger";
        return "secondary";
    };
    const badge = (value, tone = "secondary") => `<span class="badge text-bg-${tone}">${esc(value)}</span>`;
    const parseJsonValue = (value, fallback) => {
        if (value === null || value === undefined || value === "") return fallback;
        if (typeof value === "object") return value;
        try {
            return JSON.parse(value);
        } catch {
            return fallback;
        }
    };
    const unwrapApiResult = (payload) => {
        if (payload && typeof payload === "object" && "result" in payload) return payload.result;
        return payload || {};
    };
    const safeMetricValue = (value, fallback = "N/A") => {
        if (value === null || value === undefined || value === "") return fallback;
        return String(value);
    };
    const ensureContainer = (id) => {
        const container = document.getElementById(id);
        if (!container) {
            dashboardLog("dom", `Missing container ${id}`, null, "warn");
            return null;
        }
        return container;
    };
    const safeSetHtml = (id, html) => {
        const container = ensureContainer(id);
        if (!container) return null;
        container.innerHTML = html;
        return container;
    };
    const safeSetText = (id, value) => {
        const node = ensureContainer(id);
        if (!node) return null;
        node.textContent = textOr(value);
        return node;
    };
    const renderEmptyState = (title, message) => `
        <div class="dashboard-empty-state">
            <div class="dashboard-empty-state__title">${esc(title)}</div>
            <div class="dashboard-empty-state__body">${esc(message)}</div>
        </div>`;
    const renderErrorState = (title, message, retryId = "") => `
        <div class="dashboard-empty-state dashboard-empty-state--error">
            <div class="dashboard-empty-state__title">${esc(title)}</div>
            <div class="dashboard-empty-state__body">${esc(message)}</div>
            ${retryId ? `<button class="btn btn-sm btn-outline-light mt-3" id="${retryId}">Retry</button>` : ""}
        </div>`;
    const loadStateNotice = (label) => `<div class="small text-secondary">${esc(label)}</div>`;
    const radioValue = (name, fallback = "") => document.querySelector(`input[name="${name}"]:checked`)?.value || fallback;
    const normalizeModeOptions = (options) => {
        const source = asArray(options);
        if (!source.length) return FALLBACK_MODE_OPTIONS.map((item) => ({ ...item }));
        return source.map((item) => {
            if (typeof item === "string") {
                return { value: textOr(item, "V2_FULL").toUpperCase(), label: textOr(item, "V2 Full").replace(/_/g, " ") };
            }
            return {
                value: textOr(item.value || item.id || item.mode, "V2_FULL").toUpperCase(),
                label: textOr(item.label || item.value || item.id || item.mode, "V2 Full").replace(/_/g, " "),
            };
        });
    };
    const normalizeModeData = (payload = {}) => {
        const modeSource = asObject(payload.mode || payload.execution_mode || payload);
        const selectedMode = textOr(
            modeSource.selected_mode || modeSource.requested_mode || modeSource.resolved_mode || state.config.bot?.execution_mode,
            "V2_FULL",
        ).toUpperCase();
        return {
            selected_mode: selectedMode,
            requested_mode: textOr(modeSource.requested_mode, selectedMode),
            resolved_mode: textOr(modeSource.resolved_mode, selectedMode),
            label: textOr(modeSource.label, selectedMode.replace(/_/g, " ")),
            adaptive_execution: !!modeSource.adaptive_execution,
            family_specific_management: !!modeSource.family_specific_management,
            lebprim_enabled: !!modeSource.lebprim_enabled,
            lebprim_mode: textOr(modeSource.lebprim_mode, "off"),
            enabled_families: asArray(modeSource.enabled_families).map(formatFamilyName),
            enabled_strategy_names: asArray(modeSource.enabled_strategy_names),
            corrections: asArray(modeSource.corrections),
        };
    };
    function ensureIndicatorLabDraft() {
        state.indicatorLabDraft = state.indicatorLabDraft || {};
        state.indicatorLabDraft.dataset = asObject(state.indicatorLabDraft.dataset);
        state.indicatorLabDraft.campaign = asObject(state.indicatorLabDraft.campaign);
        state.indicatorLabDraft.parameters = asObject(state.indicatorLabDraft.parameters);
        state.indicatorLabDraft.optimization = asObject(state.indicatorLabDraft.optimization);
        state.indicatorLabUi = {
            dirty: false,
            pauseRefreshUntil: 0,
            submitting: false,
            message: "",
            error: "",
            lastCreatedCampaignId: null,
            ...asObject(state.indicatorLabUi),
        };
        return state.indicatorLabDraft;
    }

    function isIndicatorLabInputActive() {
        const active = document.activeElement;
        return !!(active && typeof active.closest === "function" && active.closest("#section-indicator-lab"));
    }

    const indicatorLabInteractionLocked = () => (
        isIndicatorLabInputActive()
        || Date.now() < Number(state.indicatorLabUi?.pauseRefreshUntil || 0)
    );

    function markIndicatorLabInteraction() {
        ensureIndicatorLabDraft();
        state.indicatorLabUi.pauseRefreshUntil = Date.now() + 30000;
    }

    function csvValues(value) {
        return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
    }

    function captureIndicatorLabDraft(options = {}) {
        const draft = ensureIndicatorLabDraft();
        const container = document.getElementById("section-indicator-lab");
        if (!container || !container.querySelector("#il-ds-name, #il-campaign-name, .il-param-value, .il-opt-enabled")) return draft;
        const currentIndicator = options.indicator || val("il-indicator") || draft.campaign.indicator || state.indicatorLab.selectedIndicator || "LEBPRIME";
        draft.dataset = {
            name: val("il-ds-name"),
            symbol: val("il-ds-symbol"),
            timeframe: val("il-ds-timeframe"),
            start_date: val("il-ds-start"),
            end_date: val("il-ds-end"),
            notes: val("il-ds-notes"),
        };
        draft.campaign = {
            name: val("il-campaign-name"),
            dataset_id: val("il-dataset"),
            indicator: options.campaignIndicator || val("il-indicator") || currentIndicator,
            notes: val("il-notes"),
            confirm_large: on("il-confirm-large"),
        };
        draft.parameters[currentIndicator] = {};
        document.querySelectorAll("#section-indicator-lab .il-param-value").forEach((node) => {
            const name = node.dataset.param;
            if (!name) return;
            draft.parameters[currentIndicator][name] = node.type === "checkbox" ? !!node.checked : node.value;
        });
        draft.optimization[currentIndicator] = {};
        document.querySelectorAll("#section-indicator-lab .il-opt-enabled").forEach((node) => {
            const name = node.dataset.param;
            if (!name) return;
            const row = node.closest("tr");
            draft.optimization[currentIndicator][name] = {
                enabled: !!node.checked,
                start: row?.querySelector(".il-opt-start")?.value || "",
                end: row?.querySelector(".il-opt-end")?.value || "",
                step: row?.querySelector(".il-opt-step")?.value || "",
                values: row?.querySelector(".il-opt-values")?.value || "",
            };
        });
        if (options.markDirty) {
            state.indicatorLabUi.dirty = true;
            markIndicatorLabInteraction();
        }
        return draft;
    }

    function captureUiState() {
        const values = {};
        document.querySelectorAll("input[id], textarea[id], select[id]").forEach((node) => {
            if (!node.id) return;
            if (node instanceof HTMLInputElement && (node.type === "checkbox" || node.type === "radio")) {
                values[node.id] = { type: "checked", value: !!node.checked };
                return;
            }
            values[node.id] = { type: "value", value: node.value };
        });
        const active = document.activeElement && document.activeElement.id ? String(document.activeElement.id) : "";
        preservedUiState = { values, active };
    }

    function restoreUiState() {
        if (!preservedUiState || !preservedUiState.values) return;
        Object.entries(preservedUiState.values).forEach(([id, item]) => {
            const node = document.getElementById(id);
            if (!node) return;
            if (item.type === "checked" && node instanceof HTMLInputElement) {
                node.checked = !!item.value;
                return;
            }
            if ("value" in node) {
                node.value = item.value ?? "";
            }
        });
        if (preservedUiState.active) {
            const activeNode = document.getElementById(preservedUiState.active);
            if (activeNode && typeof activeNode.focus === "function") {
                activeNode.focus({ preventScroll: true });
            }
        }
    }

    function setFeedback(message, error = false) {
        const node = safeSetText("save-feedback", message);
        if (!node) return;
        node.className = error ? "small text-danger" : "small text-secondary";
    }

    const isReadonly = () => !!(state.status.readonly_mode || state.config.dashboard?.readonly_mode);
    const currentExecutionMode = () => normalizeModeData(state.config.mode || state.status.execution_mode || state.config.bot?.execution_mode_summary || {});
    const executionModeLabel = () => currentExecutionMode().label || currentExecutionMode().selected_mode || "Unknown";
    const renderExecutionModeSummary = (modeInput = currentExecutionMode()) => {
        const mode = normalizeModeData(modeInput);
        const families = asArray(mode.enabled_families).join(", ") || "None";
        return `
            <div class="small text-secondary">Mode: ${esc(mode.selected_mode || "Unknown")}</div>
            <div class="small text-secondary">LEBPRIM: ${mode.lebprim_enabled ? "Enabled" : "Disabled"}</div>
            <div class="small text-secondary">Adaptive Execution: ${mode.adaptive_execution ? "Enabled" : "Disabled"}</div>
            <div class="small text-secondary">Family Management: ${mode.family_specific_management ? "Enabled" : "Disabled"}</div>
            <div class="small text-secondary">Enabled Families: ${esc(families)}</div>
        `;
    };
    const applyPlanMessage = (result) => {
        const plan = result?.apply_plan || {};
        const hotCount = Number(plan.hot_reloadable_count || 0);
        const restartCount = Number(plan.restart_required_count || 0);
        if (restartCount > 0 && hotCount > 0) return `Hot-applied ${hotCount} change(s). RESTART REQUIRED for ${restartCount} saved change(s).`;
        if (restartCount > 0) return `RESTART REQUIRED for ${restartCount} saved change(s).`;
        if (hotCount > 0) return `Hot-applied ${hotCount} change(s).`;
        return "No effective config changes.";
    };
    const latestJobByType = (jobType) => (state.jobs || []).find((job) => job.job_type === jobType) || null;
    const currentWorkerStatus = () => asObject(state.workerStatus || state.status.worker || {});
    const workerStateLabel = (worker = currentWorkerStatus()) => {
        const stateValue = textOr(worker.state || worker.status, "unavailable").toLowerCase();
        if (stateValue === "running") return "Worker RUNNING";
        if (stateValue === "stale") return "Worker STALE";
        return "Worker STOPPED";
    };
    const workerTone = (worker = currentWorkerStatus()) => {
        const stateValue = textOr(worker.state || worker.status, "unavailable").toLowerCase();
        if (stateValue === "running") return "success";
        if (stateValue === "stale") return "warning";
        return "danger";
    };

    async function saveConfigPatch(updates, reason) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return false;
        }
        try {
            const preview = await api("/api/config/preview", { method: "POST", body: JSON.stringify({ actor: "dashboard", updates, preview_only: true, reason }) });
            const summary = (preview.changes || []).slice(0, 12).map((entry) => `${entry.path}: ${JSON.stringify(entry.before)} -> ${JSON.stringify(entry.after)}`).join("\n");
            if (summary && !window.confirm(`Apply these config changes?\n\n${summary}`)) return false;
            const result = await api("/api/config", { method: "POST", body: JSON.stringify({ actor: "dashboard", updates, reason }) });
            setFeedback(`${applyPlanMessage(result)} Backup: ${result.backup_path}`);
            await refreshConfig();
            await refreshStatus();
            return result;
        } catch (error) {
            setFeedback(error.message, true);
            return false;
        }
    }

    async function runControl(path, payload) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return false;
        }
        try {
            const result = await api(path, { method: "POST", body: JSON.stringify({ actor: "dashboard", ...payload }) });
            setFeedback(result.message || "Action completed");
            await refreshStatus();
            return true;
        } catch (error) {
            setFeedback(error.message, true);
            return false;
        }
    }

    async function switchMode(mode) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return false;
        }
        let confirmationText = null;
        if (mode === "LIVE") {
            confirmationText = window.prompt("Type LIVE to confirm switching the bot to live mode.");
            if ((confirmationText || "").toUpperCase() !== "LIVE") {
                setFeedback("Live switch cancelled.");
                return false;
            }
        }
        try {
            const result = await api("/api/control/mode", { method: "POST", body: JSON.stringify({ actor: "dashboard", trading_mode: mode, confirmation_text: confirmationText }) });
            setFeedback(`Trading mode updated to ${mode}. ${applyPlanMessage(result)}`);
            await refreshConfig();
            await refreshStatus();
            return true;
        } catch (error) {
            setFeedback(error.message, true);
            return false;
        }
    }

    function renderTopStatusBar() {
        const control = state.status.control || {};
        const runtimeControl = control.runtime || {};
  const rawMode = state.config.bot?.trading_mode || runtimeControl.mode || "N/A";
        const mode = rawMode === "DRY_RUN" ? "DRY" : rawMode;
        const botRunning = Boolean(control.process_alive || control.bot_running);
        const mt5Connected = botRunning ? Boolean(runtimeControl.mt5_connected) : null;
        const mt5Label = mt5Connected === null ? "DISCONNECTED" : (mt5Connected ? "CONNECTED" : "DISCONNECTED");
        const mt5Tone = mt5Connected === null ? "secondary" : (mt5Connected ? "success" : "danger");
        const worker = currentWorkerStatus();
        document.getElementById("top-status-bar").innerHTML = `
            <div class="status-chip"><span class="badge text-bg-${control.process_alive || control.bot_running ? "success" : "danger"}">Bot ${control.process_alive || control.bot_running ? "RUNNING" : "STOPPED"}</span></div>
            <div class="status-chip"><span class="badge text-bg-${mode === "LIVE" ? "danger" : "info"}">${esc(mode)}</span></div>
            <div class="status-chip"><span class="badge text-bg-${mt5Tone}">MT5 ${mt5Label}</span></div>
            <div class="status-chip"><span class="badge text-bg-${workerTone(worker)}">${esc(workerStateLabel(worker))}</span></div>
            <div class="status-chip"><span class="badge text-bg-${control.kill_switch ? "danger" : state.config.bot?.force_reduced_risk_mode ? "warning" : "success"}">${control.kill_switch ? "KILL" : state.config.bot?.force_reduced_risk_mode ? "SAFE" : "NORMAL"}</span></div>
            ${control.pending_restart ? `<div class="status-chip"><span class="badge text-bg-warning">RESTART PENDING</span></div>` : ""}
            <div class="status-chip"><span class="badge text-bg-${isReadonly() ? "warning" : "secondary"}">${isReadonly() ? "READ-ONLY" : "READ-WRITE"}</span></div>
            <div class="status-chip">Heartbeat: ${esc(runtimeControl.heartbeat_at || "N/A")}</div>`;
    }

    function renderQuickActions() {
        const container = ensureContainer("quick-actions");
        if (!container) return;
        if (!state.runtimeActionsUi.mounted) {
            container.innerHTML = `
                <button class="btn btn-success" data-runtime-action="start">Start</button>
                <button class="btn btn-outline-light" data-runtime-action="stop">Stop</button>
                <button class="btn btn-outline-warning" data-runtime-action="pause">Pause</button>
                <button class="btn btn-outline-info" data-runtime-action="resume">Resume</button>
                <button class="btn btn-outline-primary" data-runtime-action="dry">Switch To Dry</button>
                <button class="btn btn-danger" data-runtime-action="live">Switch To Live</button>
                <button class="btn btn-outline-danger" data-runtime-action="kill">Kill Switch</button>
                <button class="btn btn-outline-success" data-runtime-action="safe">Safe Mode</button>`;
            container.addEventListener("click", handleRuntimeActionClick);
            state.runtimeActionsUi.mounted = true;
        }
        updateRuntimeActionButtons();
    }

    const runtimeActionLabels = {
        start: ["Start", "Starting..."], stop: ["Stop", "Stopping..."],
        pause: ["Pause", "Pausing..."], resume: ["Resume", "Resuming..."],
        dry: ["Switch To Dry", "Switching..."], live: ["Switch To Live", "Switching..."],
        kill: ["Kill Switch", "Applying..."], safe: ["Safe Mode", "Applying..."],
    };

    function updateRuntimeActionButtons() {
        document.querySelectorAll("#quick-actions [data-runtime-action]").forEach((button) => {
            const action = button.dataset.runtimeAction;
            const pending = state.runtimeActionsUi.pending;
            button.disabled = isReadonly() || !!pending;
            button.textContent = pending === action ? runtimeActionLabels[action][1] : runtimeActionLabels[action][0];
            button.setAttribute("aria-busy", pending === action ? "true" : "false");
        });
    }

    async function executeRuntimeAction(action, operation) {
        if (state.runtimeActionsUi.pending) return false;
        state.runtimeActionsUi.pending = action;
        updateRuntimeActionButtons();
        try {
            return await operation();
        } finally {
            state.runtimeActionsUi.pending = null;
            updateRuntimeActionButtons();
        }
    }

    async function handleRuntimeActionClick(event) {
        const button = event.target.closest("[data-runtime-action]");
        if (!button || state.runtimeActionsUi.pending) return;
        const action = button.dataset.runtimeAction;
        await executeRuntimeAction(action, async () => {
            if (action === "start") return runControl("/api/control/start", { enabled: true });
            if (action === "stop") return runControl("/api/control/stop", { enabled: true });
            if (action === "pause") return runControl("/api/control/pause", { enabled: true });
            if (action === "resume") return runControl("/api/control/resume", { enabled: true });
            if (action === "dry") return switchMode("DRY_RUN");
            if (action === "live") return switchMode("LIVE");
            if (action === "safe") return applyPreset("SAFE_MODE");
            if (action === "kill") {
                const enabled = !(state.status.control?.kill_switch);
                if (enabled && !window.confirm("Enable the kill switch? This will block all new trades.")) return false;
                return runControl("/api/control/kill-switch", { enabled, reason: enabled ? "dashboard_kill_switch" : null });
            }
            return false;
        });
    }

    async function applyPreset(presetName) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return false;
        }
        const presetLabels = { SAFE_MODE: "safe mode", BALANCED_MODE: "balanced mode", SCALPING_MODE: "scalping mode", AGGRESSIVE_MODE: "aggressive mode" };
        const human = presetLabels[presetName] || presetName.toLowerCase();
        if (!window.confirm(`Apply the ${human} preset?`)) return false;
        try {
            const result = await api("/api/config/preset", { method: "POST", body: JSON.stringify({ actor: "dashboard", preset_name: presetName }) });
            setFeedback(`Preset applied. ${applyPlanMessage(result)}`);
            await refreshConfig();
            await refreshStatus();
            return true;
        } catch (error) {
            setFeedback(error.message, true);
            return false;
        }
    }

    function blockedFamilySummary(blocked) {
        const counts = {};
        (blocked || []).forEach((item) => {
            const key = `${item.setup_family || "UNKNOWN"} | ${item.reason_code || "unknown"}`;
            counts[key] = (counts[key] || 0) + 1;
        });
        return Object.entries(counts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 6);
    }

    function latestBlockLookup(entries) {
        const lookup = {};
        (entries || []).forEach((entry) => {
            const key = entry.group_name || "UNKNOWN";
            if (!lookup[key]) {
                lookup[key] = [];
            }
            lookup[key].push(`${entry.reason_code} (${entry.count})`);
        });
        Object.keys(lookup).forEach((key) => {
            lookup[key] = lookup[key].slice(0, 3).join(", ");
        });
        return lookup;
    }

    async function exportConfigText() {
        try {
            const response = await fetch("/api/config/export");
            const text = await response.text();
            const node = document.getElementById("config-import-export");
            if (node) node.value = text;
            setFeedback("Config exported to the editor.");
        } catch (error) {
            setFeedback(error.message, true);
        }
    }

    async function importConfigText() {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return;
        }
        const node = document.getElementById("config-import-export");
        const payload = node ? node.value : "";
        if (!payload.trim()) {
            setFeedback("Paste config JSON first.", true);
            return;
        }
        if (!window.confirm("Import the JSON from the editor and overwrite the current config?")) return;
        try {
            const result = await api("/api/config/import", {
                method: "POST",
                body: JSON.stringify({ actor: "dashboard", config_json: payload }),
            });
            setFeedback(`Config imported. ${applyPlanMessage(result)}`);
            await refreshConfig();
            await refreshStatus();
        } catch (error) {
            setFeedback(error.message, true);
        }
    }

    function renderConfig() {
        const dashboard = state.config.dashboard || {};
        const control = state.status.control || {};
        const metadata = state.configMetadata || {};
        const lastApply = control.last_config_apply || {};
        const restartReasons = control.restart_required_reasons || [];
        document.getElementById("section-config").innerHTML = `
            <div class="row g-3">
                <div class="col-12 col-xl-5">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body">
                            <h2 class="h5">Dashboard Settings</h2>
                            <div class="small text-secondary mb-3">These settings control the dashboard itself, not the trading strategy.</div>
                            ${field("dashboard-host", "Dashboard Host", dashboard.host || "0.0.0.0", "text")}
                            ${field("dashboard-port", "Dashboard Port", dashboard.default_port || dashboard.port || 8501, "number", "1")}
                            ${field("dashboard-refresh", "Refresh Seconds", dashboard.refresh_seconds || 5, "number", "1")}
                            ${field("dashboard-stale", "Heartbeat Stale Seconds", dashboard.stale_heartbeat_seconds || 90, "number", "1")}
                            ${toggle("dashboard-readonly-config", "Readonly Mode", !!dashboard.readonly_mode)}
                            <div class="small text-secondary mt-3">Auth ${state.auth?.enabled ? `enabled for ${esc(state.auth.username || "configured user")}` : "disabled"}</div>
                            <button class="btn btn-primary mt-3" id="save-dashboard-settings">Save Dashboard Settings</button>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-xl-7">
                    <div class="card border-0 shadow-sm mb-3 ${control.pending_restart ? "live-warning" : ""}">
                        <div class="card-body">
                            <h2 class="h5">Apply / Reload State</h2>
                            <div class="small text-secondary mb-2">Dashboard changes are persisted to the canonical backend config, then either hot-reloaded by the bot loop or marked pending restart.</div>
                            <div>Source: ${esc(metadata.source || "config.json")}</div>
                            <div>Model: ${esc(metadata.model || "validated canonical config")}</div>
                            <div>Pending restart: ${control.pending_restart ? "Yes" : "No"}</div>
                            <div>Last apply: ${esc(lastApply.applied_at || "N/A")} ${lastApply.reason ? `(${esc(lastApply.reason)})` : ""}</div>
                            <div class="small text-secondary mt-2">Restart-required paths: ${esc(restartReasons.length ? restartReasons.join(", ") : "none")}</div>
                            <div class="d-flex flex-wrap gap-2 mt-3">
                                <button class="btn btn-outline-light" id="request-config-reload">Request Hot Reload</button>
                                <button class="btn btn-outline-warning" id="clear-pending-restart">Clear Restart Marker</button>
                            </div>
                        </div>
                    </div>
                    <div class="card border-0 shadow-sm">
                        <div class="card-body">
                            <h2 class="h5">Config Import / Export</h2>
                            <div class="small text-secondary mb-2">Export the current config or paste JSON to import it with backup and rollback protection.</div>
                            <textarea class="form-control config-editor" id="config-import-export" spellcheck="false"></textarea>
                            <div class="d-flex flex-wrap gap-2 mt-3">
                                <button class="btn btn-outline-light" id="export-config-button">Export Current Config</button>
                                <button class="btn btn-primary" id="import-config-button">Import From Editor</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;
        document.getElementById("export-config-button").onclick = () => exportConfigText();
        document.getElementById("import-config-button").onclick = () => importConfigText();
        document.getElementById("request-config-reload").onclick = () => runControl("/api/config/reload", { enabled: true });
        document.getElementById("clear-pending-restart").onclick = () => runControl("/api/config/clear-pending-restart", { enabled: true });
        document.getElementById("save-dashboard-settings").onclick = async () => {
            await saveConfigPatch({
                dashboard: {
                    host: val("dashboard-host"),
                    default_port: num(val("dashboard-port"), 8501),
                    refresh_seconds: num(val("dashboard-refresh"), 5),
                    stale_heartbeat_seconds: num(val("dashboard-stale"), 90),
                    readonly_mode: on("dashboard-readonly-config"),
                },
            }, "dashboard_settings_update");
        };
    }

    function metric(label, value, danger = false) {
        return `<div class="card border-0 metric-card shadow-sm ${danger ? "live-warning" : ""}"><div class="card-body"><div class="text-secondary small">${esc(label)}</div><div class="metric-value">${esc(value)}</div></div></div>`;
    }

    function renderOverview() {
        const runtime = state.status.runtime || {};
        const metrics = runtime.metrics || {};
        const control = state.status.control || {};
        const runtimeControl = control.runtime || {};
        const blocked = runtime.blocked_setups || [];
        const rejections = runtime.rejection_summary || [];
        const familySummary = blockedFamilySummary(blocked);
        document.getElementById("section-overview").innerHTML = `
            <div class="row g-3 mb-3">
                <div class="col-12 col-xl-8"><div class="section-grid">
                    ${metric("Bot Status", metrics.bot_status || "N/A")}
${metric("Current Mode", state.config.bot?.trading_mode || runtimeControl.mode || "N/A", (state.config.bot?.trading_mode || runtimeControl.mode) === "LIVE")}
                    ${metric("Open Positions", num(metrics.open_positions).toFixed(0))}
                    ${metric("Today PnL", num(metrics.today_pnl).toFixed(2))}
                    ${metric("Daily Loss Used", `${num(metrics.daily_loss_used_pct).toFixed(1)}%`)}
                    ${metric("Current Spread", num(metrics.spread).toFixed(2))}
                    ${metric("Session", runtimeControl.session || "N/A")}
                    ${metric("Regime", runtimeControl.regime || "N/A")}
                    ${metric("Win Rate", `${num(metrics.win_rate).toFixed(2)}%`)}
                    ${metric("Active Strategies", num(metrics.enabled_strategy_count).toFixed(0))}
                    ${metric("Enabled Families", num(metrics.enabled_family_count).toFixed(0))}
                </div></div>
<div class="col-12 col-xl-4"><div class="card border-0 shadow-sm ${((state.config.bot?.trading_mode || runtimeControl.mode) === "LIVE") ? "live-warning" : ""}"><div class="card-body">
                    <h2 class="h5">Live Monitoring</h2>
                    <div>Last heartbeat: ${esc(runtimeControl.heartbeat_at || "N/A")}</div>
                    <div>Last execution: ${esc(runtimeControl.last_execution_result || "N/A")}</div>
                    <div>Broker login: ${esc(runtimeControl.account_login || "N/A")}</div>
                    <div>Trade allowed: ${runtimeControl.trade_allowed ? "Yes" : "No"}</div>
                    <div>Execution paused: ${control.execution_paused ? "Yes" : "No"}</div>
                </div></div></div>
            </div>
            <div class="row g-3">
                <div class="col-12 col-xl-4"><div class="card border-0 shadow-sm mb-3"><div class="card-body">
                    <h2 class="h5">Top Rejections (24h)</h2>
                    ${(rejections.length ? rejections : [{ reason_code: "none", count: 0 }]).map((item) => `<div class="d-flex justify-content-between py-1 border-bottom border-secondary-subtle"><span>${esc(item.reason_code)}</span><span class="badge text-bg-dark">${esc(item.count)}</span></div>`).join("")}
                </div></div><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Blocked Families</h2>
                    ${(familySummary.length ? familySummary : [["none", 0]]).map((item) => `<div class="d-flex justify-content-between py-1 border-bottom border-secondary-subtle"><span>${esc(item[0])}</span><span class="badge text-bg-secondary">${esc(item[1])}</span></div>`).join("")}
                </div></div></div>
                <div class="col-12 col-xl-8"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Recently Blocked Setups</h2>
                    <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Time</th><th>Family</th><th>Context</th><th>Reason</th><th>Scores</th></tr></thead>
                    <tbody>${blocked.map((item) => `<tr><td>${esc(item.timestamp)}</td><td>${esc(item.setup_family)}</td><td>${esc(item.side)} | ${esc(item.session_name)} | ${esc(item.regime_name)}</td><td><span class="badge text-bg-secondary">${esc(item.reason_code)}</span><div class="small text-secondary mt-1">${esc(item.reason)}</div></td><td class="small">T ${num(item.trend_score).toFixed(0)} / S ${num(item.setup_score).toFixed(0)} / G ${num(item.trigger_score).toFixed(0)} / E ${num(item.entry_score).toFixed(1)}</td></tr>`).join("") || `<tr><td colspan="5" class="text-secondary">No blocked setups found.</td></tr>`}</tbody></table></div>
                </div></div></div>
            </div>`;
    }

    function lebprimeConfigDraft() {
        const source = asObject(state.config.lebprime_v3_4_0);
        return {
            ...source,
            enabled: !!source.enabled,
            execution_mode: source.execution_mode === "LIVE" ? "LIVE" : "DRY",
            symbols: asArray(source.symbols).slice(),
            timeframes: asArray(source.timeframes).slice(),
            bars_to_fetch: num(source.bars_to_fetch, 360),
            max_spread_points: num(source.max_spread_points, 25),
            max_spread_points_by_symbol: { ...asObject(source.max_spread_points_by_symbol) },
            parameters: { ...asObject(source.parameters) },
        };
    }

    function lebprimeComparableDraft(draft) {
        return JSON.stringify({
            enabled: !!draft.enabled,
            execution_mode: draft.execution_mode === "LIVE" ? "LIVE" : "DRY",
            symbols: asArray(draft.symbols),
            timeframes: asArray(draft.timeframes),
            bars_to_fetch: String(draft.bars_to_fetch ?? ""),
            max_spread_points: String(draft.max_spread_points ?? ""),
            // Same fallbacks as syncLebprimeStrategyEditor, so a symbol missing from config is not "unsaved".
            spreads: LEBPRIME_SYMBOLS.map((item) => String(num(draft.max_spread_points_by_symbol?.[item.key] ?? draft.max_spread_points))),
            buffers: LEBPRIME_SYMBOLS.map((item) => String(num(draft.parameters?.[item.buffer], item.bufferDefault))),
        });
    }

    function readLebprimeStrategyDraft() {
        const baseline = lebprimeConfigDraft();
        return {
            ...baseline,
            enabled: on("lebprime-live-enabled"),
            execution_mode: val("lebprime-live-mode") || "DRY",
            symbols: Array.from(document.querySelectorAll("[data-lebprime-symbol]:checked")).map((node) => node.dataset.lebprimeSymbol),
            timeframes: Array.from(document.querySelectorAll("[data-lebprime-timeframe]:checked")).map((node) => node.dataset.lebprimeTimeframe),
            bars_to_fetch: val("lebprime-bars"),
            max_spread_points: val("lebprime-spread"),
            max_spread_points_by_symbol: {
                ...baseline.max_spread_points_by_symbol,
                ...Object.fromEntries(LEBPRIME_SYMBOLS.map((item) => [item.key, val(lebprimeSpreadId(item))])),
            },
            parameters: {
                ...baseline.parameters,
                ...Object.fromEntries(LEBPRIME_SYMBOLS.map((item) => [item.buffer, val(lebprimeBufferId(item))])),
            },
        };
    }

    function updateLebprimeDraftState() {
        if (!state.strategiesUi.mounted) return;
        const draft = readLebprimeStrategyDraft();
        state.strategiesUi.draft = draft;
        state.strategiesUi.dirty = lebprimeComparableDraft(draft) !== lebprimeComparableDraft(lebprimeConfigDraft());
        const savedText = state.strategiesUi.lastSaveRestartRequired ? "Saved - RESTART REQUIRED" : "Saved";
        const statusNode = safeSetText("lebprime-draft-state", state.strategiesUi.saving ? "Saving..." : state.strategiesUi.dirty ? "Unsaved changes" : savedText);
        if (statusNode) statusNode.className = state.strategiesUi.dirty ? "small text-warning" : "small text-secondary";
        const saveButton = document.getElementById("save-lebprime-live");
        if (saveButton) {
            saveButton.textContent = state.strategiesUi.saving ? "Saving..." : "Save LebPrime v3.4.0";
            saveButton.classList.toggle("btn-warning", state.strategiesUi.dirty && !state.strategiesUi.saving);
            saveButton.classList.toggle("btn-primary", !state.strategiesUi.dirty || state.strategiesUi.saving);
        }
        updateLebprimeEditorAvailability();
    }

    function updateLebprimeEditorAvailability() {
        const disabled = isReadonly() || state.strategiesUi.saving;
        document.querySelectorAll("#lebprime-editable-config input, #lebprime-editable-config select, #save-lebprime-live").forEach((node) => { node.disabled = disabled; });
    }

    function validateLebprimeDraft(draft) {
        if (!draft.symbols.length) return "Select at least one symbol.";
        if (!draft.timeframes.length) return "Select at least one timeframe.";
        const bars = Number(draft.bars_to_fetch);
        if (!Number.isInteger(bars) || bars <= 0) return "Bars To Fetch must be a positive whole number.";
        const numericFields = [
            ["Max Spread Points", draft.max_spread_points],
            ...LEBPRIME_SYMBOLS.map((item) => [`${item.broker} Max Spread Points`, draft.max_spread_points_by_symbol[item.key]]),
            ...LEBPRIME_SYMBOLS.map((item) => [`${item.broker} SL Buffer`, draft.parameters[item.buffer]]),
        ];
        const invalid = numericFields.find(([, value]) => value === "" || !Number.isFinite(Number(value)) || Number(value) < 0);
        return invalid ? `${invalid[0]} must be zero or greater.` : "";
    }

    function mountLebprimeStrategyEditor() {
        const container = ensureContainer("section-strategies");
        if (!container || state.strategiesUi.mounted) return;
        container.innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                    <div><h2 class="h5 mb-1">lebprime_v3_4_0</h2><div class="small text-secondary" id="lebprime-startup-reason">Awaiting runtime status</div></div>
                    <div id="lebprime-runtime-badge"></div>
                </div>
                <div id="lebprime-editable-config">
                    <div class="row g-3 mb-3">
                        <div class="col-md-2"><label class="form-label small text-secondary" for="lebprime-live-enabled">Enabled</label><div><input type="checkbox" id="lebprime-live-enabled"></div></div>
                        <div class="col-md-2"><label class="form-label small text-secondary" for="lebprime-live-mode">Execution</label><select class="form-select" id="lebprime-live-mode"><option value="DRY">DRY</option><option value="LIVE">LIVE DEMO</option></select></div>
                        <div class="col-md-4"><div class="form-label small text-secondary">Symbols</div><div class="d-flex flex-wrap gap-3">${lebprimeSymbolCheckboxes()}</div></div>
                        <div class="col-md-4"><div class="form-label small text-secondary">Timeframes</div><div class="d-flex flex-wrap gap-3">${["M1", "M3", "M5", "M15", "M30", "M45"].map((timeframe) => `<label class="form-check-label"><input class="form-check-input me-1" type="checkbox" data-lebprime-timeframe="${timeframe}">${timeframe}</label>`).join("")}</div></div>
                    </div>
                    <div class="row g-3 mb-3">
                        <div class="col-6 col-lg-2">${field("lebprime-bars", "Bars To Fetch", "", "number", "1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-spread", "Max Spread Points", "", "number", "0.1")}</div>
                    </div>
                    <div class="row g-3 mb-3">
                        ${LEBPRIME_SYMBOLS.map((item) => `<div class="col-6 col-lg-2">${field(lebprimeSpreadId(item), `${item.broker} Spread Max`, "", "number", "0.1")}</div>`).join("")}
                    </div>
                    <div class="row g-3 mb-3">
                        ${LEBPRIME_SYMBOLS.map((item) => `<div class="col-6 col-lg-2">${field(lebprimeBufferId(item), `${item.broker} SL Buffer`, "", "number", item.step)}</div>`).join("")}
                    </div>
                    <div class="d-flex align-items-center gap-3 mb-4"><button class="btn btn-primary" id="save-lebprime-live">Save LebPrime v3.4.0</button><span class="small text-secondary" id="lebprime-draft-state">Saved</span></div>
                </div>
                <div class="row g-2 mb-3">
                    <div class="col-6 col-lg"><div class="small text-secondary">Families</div><div class="h5 mb-0" id="lebprime-family-count">0</div></div>
<div class="col-6 col-lg"><div class="small text-secondary">Internal Pending LIMITs</div><div class="h5 mb-0" id="lebprime-pending-count">0</div></div>
<div class="col-6 col-lg"><div class="small text-secondary">Broker Filled Positions (Open Positions)</div><div class="h5 mb-0" id="lebprime-active-count">0</div></div>
<div class="col-6 col-lg"><div class="small text-secondary">Broker Pending Orders (Broker Orders)</div><div class="h5 mb-0" id="lebprime-broker-count">0</div></div>
<div class="col-6 col-lg"><div class="small text-secondary">Historical Active States</div><div class="h5 mb-0" id="lebprime-historical-active-count">0</div></div>
<div class="col-6 col-lg"><div class="small text-secondary">Realtime Activated Trades</div><div class="h5 mb-0" id="lebprime-realtime-active-count">0</div></div>
                    <div class="col-6 col-lg"><div class="small text-secondary">Duplicate Suppressed</div><div class="h5 mb-0" id="lebprime-duplicate-count">0</div></div>
                </div>
<div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Context</th><th>Health</th><th>Last Closed Candle</th><th>Last Scan</th><th>Candidates</th><th>Live Zones</th><th>Pending LIMITs</th><th>Active Trades</th><th>Last Error</th><th>Heartbeat</th></tr></thead><tbody id="lebprime-context-status-body"></tbody></table></div>
<h3 class="h6 mt-4">Spread Validation</h3>
<div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Logical Symbol</th><th>Broker Symbol</th><th>Bid</th><th>Ask</th><th>Point</th><th>Current Spread Points</th><th>Configured Max</th><th>Status</th><th>Rejection Reason</th></tr></thead><tbody id="lebprime-spread-status-body"></tbody></table></div>
<h3 class="h6 mt-4">LebPrime Broker Entries</h3>
<div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Context</th><th>Zone ID</th><th>Entry</th><th>Order Type</th><th>Price</th><th>Volume</th><th>SL</th><th>TP</th><th>MT5 Ticket</th><th>Status</th></tr></thead><tbody id="lebprime-broker-entry-body"></tbody></table></div>
            </div></div>`;
        state.strategiesUi.mounted = true;
        container.querySelectorAll("#lebprime-editable-config input, #lebprime-editable-config select").forEach((node) => {
            node.addEventListener("input", updateLebprimeDraftState);
            node.addEventListener("change", updateLebprimeDraftState);
        });
        document.getElementById("save-lebprime-live").onclick = saveLebprimeStrategyDraft;
    }

    function syncLebprimeStrategyEditor() {
        if (state.strategiesUi.dirty) return;
        const draft = lebprimeConfigDraft();
        state.strategiesUi.draft = draft;
        document.getElementById("lebprime-live-enabled").checked = draft.enabled;
        document.getElementById("lebprime-live-mode").value = draft.execution_mode;
        document.querySelectorAll("[data-lebprime-symbol]").forEach((node) => { node.checked = draft.symbols.includes(node.dataset.lebprimeSymbol); });
        document.querySelectorAll("[data-lebprime-timeframe]").forEach((node) => { node.checked = draft.timeframes.includes(node.dataset.lebprimeTimeframe); });
        document.getElementById("lebprime-bars").value = draft.bars_to_fetch;
        document.getElementById("lebprime-spread").value = draft.max_spread_points;
        LEBPRIME_SYMBOLS.forEach((item) => {
            document.getElementById(lebprimeSpreadId(item)).value = draft.max_spread_points_by_symbol[item.key] ?? draft.max_spread_points;
            document.getElementById(lebprimeBufferId(item)).value = num(draft.parameters[item.buffer], item.bufferDefault);
        });
        updateLebprimeDraftState();
    }

    async function saveLebprimeStrategyDraft() {
        if (state.strategiesUi.saving) return;
        const localDraft = readLebprimeStrategyDraft();
        const validationError = validateLebprimeDraft(localDraft);
        if (validationError) {
            setFeedback(validationError, true);
            return;
        }
        const baseline = lebprimeConfigDraft();
        const draft = {
            ...baseline,
            enabled: localDraft.enabled,
            execution_mode: localDraft.execution_mode,
            symbols: localDraft.symbols,
            timeframes: localDraft.timeframes,
            bars_to_fetch: Number(localDraft.bars_to_fetch),
            max_spread_points: Number(localDraft.max_spread_points),
            max_spread_points_by_symbol: Object.fromEntries(LEBPRIME_SYMBOLS.map((item) => [item.key, Number(localDraft.max_spread_points_by_symbol[item.key])])),
            parameters: {
                ...baseline.parameters,
                ...Object.fromEntries(LEBPRIME_SYMBOLS.map((item) => [item.buffer, Number(localDraft.parameters[item.buffer])])),
            },
        };
        state.strategiesUi.saving = true;
        updateLebprimeDraftState();
        const saveResult = await saveConfigPatch({ lebprime_v3_4_0: draft }, "lebprime_v3_4_0_update");
        state.strategiesUi.saving = false;
        if (saveResult) {
            state.strategiesUi.dirty = false;
            state.strategiesUi.lastSaveRestartRequired = !!saveResult.apply_plan?.pending_restart;
            syncLebprimeStrategyEditor();
        } else {
            state.strategiesUi.draft = localDraft;
            updateLebprimeDraftState();
        }
    }

    function updateLebprimeStrategyStatus() {
        const status = asObject(state.lebprimeLive);
        const config = asObject(state.config.lebprime_v3_4_0);
        safeSetText("lebprime-startup-reason", status.startup_block_reason || (status.started ? "Runtime active" : "Disabled or awaiting startup"));
        safeSetHtml("lebprime-runtime-badge", badge(config.enabled ? (config.execution_mode === "LIVE" ? "LIVE DEMO" : "DRY") : "DISABLED", config.enabled ? "warning" : "secondary"));
        safeSetText("lebprime-family-count", num(status.active_zone_families).toFixed(0));
        safeSetText("lebprime-pending-count", num(status.pending_orders).toFixed(0));
        safeSetText("lebprime-active-count", num(status.active_positions).toFixed(0));
safeSetText("lebprime-broker-count", num(status.broker_order_count).toFixed(0));
safeSetText("lebprime-historical-active-count", num(status.historical_active_states).toFixed(0));
safeSetText("lebprime-realtime-active-count", num(status.realtime_activated_trades).toFixed(0));
        safeSetText("lebprime-duplicate-count", num(status.duplicate_suppressed_zones).toFixed(0));
        const contexts = asArray(status.contexts);
 safeSetHtml("lebprime-context-status-body", contexts.map((row) => `<tr><td>${esc(row.logical_symbol)} ${esc(row.timeframe)}</td><td>${badge(row.status || "IDLE", row.status === "HEALTHY" ? "success" : row.status === "ERROR" ? "danger" : "secondary")}</td><td>${esc(row.last_closed_candle || "-")}</td><td>${esc(row.last_scan_time || "-")}</td><td>${num(row.candidate_count).toFixed(0)}</td><td>${num(row.live_zones).toFixed(0)}</td><td>${num(row.pending_limits).toFixed(0)}</td><td>${num(row.active_trades).toFixed(0)}</td><td>${esc(row.last_error || "-")}</td><td>${esc(row.heartbeat || "-")}</td></tr>`).join("") || `<tr><td colspan="10" class="text-secondary">Context status will appear after the runtime initializes.</td></tr>`);
 const spreadStatus = asArray(status.spread_status);
 safeSetHtml("lebprime-spread-status-body", spreadStatus.map((row) => `<tr><td>${esc(row.logical_symbol)}</td><td>${esc(row.broker_symbol)}</td><td>${esc(row.bid ?? "-")}</td><td>${esc(row.ask ?? "-")}</td><td>${esc(row.point ?? "-")}</td><td>${num(row.spread_points).toFixed(2)}</td><td>${num(row.max_spread_points).toFixed(2)}</td><td>${badge(row.status || "UNKNOWN", row.status === "PASS" ? "success" : "warning")}</td><td>${esc(row.rejection_reason || "-")}</td></tr>`).join("") || `<tr><td colspan="9" class="text-secondary">Spread status will appear after the next runtime scan.</td></tr>`);
const brokerEntries = asArray(status.broker_entries);
 safeSetHtml("lebprime-broker-entry-body", brokerEntries.map((row) => `<tr><td>${esc(row.context_key)}</td><td>${esc(row.zone_id)}</td><td>Entry ${num(row.entry_number).toFixed(0)}</td><td>${esc(row.order_type)}</td><td>${esc(row.normalized_price ?? row.entry_price)}</td><td>${esc(row.volume ?? "-")}</td><td>${esc(row.sl ?? "-")}</td><td>${esc(row.tp ?? "-")}</td><td>${esc(row.mt5_order_ticket || "-")}</td><td>${badge(row.broker_status || "UNKNOWN", row.broker_status === "FILLED" ? "success" : row.broker_status === "REJECTED_BY_BROKER" ? "danger" : "info")}</td></tr>`).join("") || `<tr><td colspan="10" class="text-secondary">No broker entry records.</td></tr>`);
}

    function renderStrategies() {
        mountLebprimeStrategyEditor();
        syncLebprimeStrategyEditor();
        updateLebprimeStrategyStatus();
    }

    function renderRisk() {
        const risk = state.config.risk || {};
        const exit = state.config.exit || {};
        document.getElementById("section-risk").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <h2 class="h5">Risk Management</h2>
                <div class="row g-3">
                    <div class="col-md-3">${field("risk-percent", "Risk Per Trade %", risk.risk_percent, "number", "0.01")}</div>
                    <div class="col-md-3">${field("risk-daily-loss", "Max Daily Loss %", risk.max_daily_drawdown_pct, "number", "0.01")}</div>
                    <div class="col-md-3">${field("risk-max-open", "Max Trades Per Day", risk.max_trades_per_day, "number", "1")}</div>
                    <div class="col-md-3">${field("risk-max-lot", "Max Lot Size", risk.max_lot, "number", "0.01")}</div>
                    <div class="col-md-3">${field("risk-min-lot", "Min Lot Size", risk.min_lot, "number", "0.01")}</div>
                    <div class="col-md-3">${field("risk-max-symbol", "Max Trades Per Symbol", risk.max_trades_per_symbol || 1, "number", "1")}</div>
                    <div class="col-md-3">${field("risk-margin-level", "Min Margin Level", risk.min_margin_level, "number", "1")}</div>
                    <div class="col-md-3">${toggle("exit-trailing-toggle", "Trailing Stop Enabled", !!exit.trailing_enabled)}</div>
                    <div class="col-md-3">${toggle("exit-breakeven-toggle", "Break-even Enabled", !!exit.breakeven_after_tp1)}</div>
                </div>
                <button class="btn btn-primary mt-3" id="save-risk">Save Risk Settings</button>
            </div></div>`;
        document.getElementById("save-risk").onclick = async () => {
            await saveConfigPatch({
                risk: {
                    risk_percent: num(val("risk-percent")),
                    max_daily_drawdown_pct: num(val("risk-daily-loss")),
                    max_trades_per_day: num(val("risk-max-open")),
                    max_lot: num(val("risk-max-lot")),
                    min_lot: num(val("risk-min-lot")),
                    max_trades_per_symbol: num(val("risk-max-symbol")),
                    min_margin_level: num(val("risk-margin-level")),
                },
                exit: { trailing_enabled: on("exit-trailing-toggle"), breakeven_after_tp1: on("exit-breakeven-toggle") },
            }, "risk_update");
        };
    }

    function renderAlerts() {
        const telegram = state.config.telegram || {};
        document.getElementById("section-alerts").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <h2 class="h5">Telegram And Alert Controls</h2>
                ${toggle("telegram-enabled", "Telegram Enabled", !!telegram.enabled)}
                ${toggle("telegram-remote", "Telegram Remote Control Enabled", !!telegram.remote_control_enabled)}
                ${toggle("telegram-manual-trades", "Telegram Manual Trade Commands", !!telegram.manual_trade_commands_enabled)}
                ${toggle("alert-signal", "Signal Alerts", telegram.signal_alerts !== false)}
                ${toggle("alert-success", "Execution Success Alerts", telegram.execution_success_alerts !== false)}
                ${toggle("alert-blocked", "Execution Blocked Alerts", telegram.execution_blocked_alerts !== false)}
                ${toggle("alert-errors", "Error Alerts", telegram.error_alerts !== false)}
                ${toggle("alert-summary", "Daily Summary Alerts", telegram.daily_summary_alerts !== false)}
                ${toggle("alert-dashboard", "Dashboard Change Alerts", telegram.dashboard_change_alerts !== false)}
                ${field("telegram-admin-chats", "Admin Chat IDs", (telegram.admin_chat_ids || []).join(", "), "text")}
                ${field("telegram-admin-users", "Admin User IDs", (telegram.admin_user_ids || []).join(", "), "text")}
                ${field("telegram-confirm-ttl", "Confirmation TTL Seconds", telegram.confirmation_ttl_seconds || 120, "number", "1")}
                ${field("telegram-dangerous-commands", "Commands Requiring Confirmation", (telegram.confirmation_required_commands || []).join(", "), "text")}
                <button class="btn btn-primary mt-3" id="save-alerts">Save Alert Settings</button>
            </div></div>`;
        document.getElementById("save-alerts").onclick = async () => {
            await saveConfigPatch({
                telegram: {
                    enabled: on("telegram-enabled"),
                    remote_control_enabled: on("telegram-remote"),
                    manual_trade_commands_enabled: on("telegram-manual-trades"),
                    signal_alerts: on("alert-signal"),
                    execution_success_alerts: on("alert-success"),
                    execution_blocked_alerts: on("alert-blocked"),
                    error_alerts: on("alert-errors"),
                    daily_summary_alerts: on("alert-summary"),
                    dashboard_change_alerts: on("alert-dashboard"),
                    admin_chat_ids: csv(val("telegram-admin-chats")),
                    admin_user_ids: csv(val("telegram-admin-users")),
                    confirmation_ttl_seconds: num(val("telegram-confirm-ttl"), 120),
                    confirmation_required_commands: csv(val("telegram-dangerous-commands")).map((item) => item.toUpperCase()),
                },
            }, "alert_update");
        };
    }

    function renderTrades() {
        const unresolved = state.status.runtime?.unresolved_trades || [];
        const recentClosed = state.status.runtime?.recent_closed_trades || [];
        const unresolvedCount = unresolved.length;
        const closedCount = recentClosed.length;
        document.getElementById("section-trades").innerHTML = `
            <div class="row g-3">
                <div class="col-12"><div class="card border-0 shadow-sm ${unresolvedCount > 0 ? "live-warning" : ""}"><div class="card-body">
                    <div class="d-flex flex-wrap justify-content-between align-items-center gap-3">
                        <div>
                            <h2 class="h5 mb-1">Trade Recovery</h2>
                            <div class="small text-secondary">Review open, pending, and recently finalized trades in one place.</div>
                        </div>
                        <div class="d-flex flex-wrap gap-2">
                            <div class="badge text-bg-warning">Unresolved: ${unresolvedCount}</div>
                            <div class="badge text-bg-success">Recent closes: ${closedCount}</div>
                        </div>
                    </div>
                </div></div></div>
                <div class="col-12 col-xl-6"><div class="card border-0 shadow-sm"><div class="card-body">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <div>
                            <h2 class="h5 mb-1">Unresolved Trades</h2>
                            <div class="small text-secondary">These trades are waiting for MT5 history to be matched and finalized.</div>
                        </div>
                    </div>
                    <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Ticket</th><th>Setup / Regime</th><th>Status</th><th>Reason</th><th>Attempts</th><th>Last Try</th></tr></thead>
                    <tbody>${unresolved.map((row) => `<tr><td>${esc(row.mt5_ticket || row.trade_id)}</td><td><div>${esc(row.setup_family || row.setup || "")}</div><div class="small text-secondary">${esc(row.regime || "")} | ${esc(row.session || "")}</div></td><td><span class="badge text-bg-warning">${esc(row.status || "PENDING")}</span></td><td><div>${esc(row.unresolved_reason || row.close_reason || "pending")}</div><div class="small text-secondary">${esc(row.note || "")}</div></td><td>${esc(row.resolution_attempts || 0)}</td><td>${esc(row.last_resolution_attempt_at || row.created_at || row.updated_at || "")}</td></tr>`).join("") || `<tr><td colspan="6" class="text-secondary">No unresolved trades.</td></tr>`}</tbody></table></div>
                </div></div></div>
                <div class="col-12 col-xl-6"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Recently Closed Trades</h2>
                    <div class="small text-secondary mb-3">Finalized outcomes with realized PnL, R multiple, and exit reason.</div>
                    <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Time</th><th>Ticket</th><th>Outcome</th><th>PnL</th><th>R</th><th>Close Reason</th><th>Exec Reason</th></tr></thead>
                    <tbody>${recentClosed.map((row) => `<tr><td>${esc(row.closed_at || row.updated_at || row.timestamp || "")}</td><td>${esc(row.mt5_ticket || row.trade_id)}</td><td><span class="badge text-bg-${(row.outcome_label || row.win_loss) === "WIN" ? "success" : (row.outcome_label || row.win_loss) === "LOSS" ? "danger" : "secondary"}">${esc(row.outcome_label || row.win_loss || "UNKNOWN")}</span></td><td>${num(row.pnl).toFixed(2)}</td><td>${num(row.realized_r).toFixed(2)}</td><td>${esc(row.close_reason || row.execution_reason || "")}</td><td>${esc(row.execution_reason || "")}</td></tr>`).join("") || `<tr><td colspan="7" class="text-secondary">No closed trades yet.</td></tr>`}</tbody></table></div>
                </div></div></div>
            </div>`;
    }

    function renderPerformance() {
        const perf = state.status.runtime?.daily_performance || {};
        const summary = perf.summary || {};
        const rows = perf.performance_rows || [];
        const sortedRows = [...rows].sort((a, b) => (num(b.total_r) - num(a.total_r)) || (num(b.average_r) - num(a.average_r)) || (num(b.trade_count) - num(a.trade_count)));
        const bestSetup = sortedRows[0] || {};
        const worstSetup = [...rows].sort((a, b) => (num(a.total_r) - num(b.total_r)) || (num(a.average_r) - num(b.average_r)) || (num(a.trade_count) - num(b.trade_count)))[0] || {};
        const topBlocked = Object.entries(summary.by_blocked_reason || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
        const topClose = Object.entries(summary.by_close_reason || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
        const topExec = Object.entries(summary.by_execution_reason || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
        document.getElementById("section-performance").innerHTML = `
            <div class="row g-3">
                <div class="col-12">
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <div class="d-flex flex-wrap justify-content-between align-items-center gap-3">
                            <div>
                                <h2 class="h5 mb-1">Daily Performance</h2>
                                <div class="small text-secondary">Strategy-level outcome review for ${esc(perf.day || summary.day || "today")}.</div>
                            </div>
                            <div class="d-flex flex-wrap gap-2">
                                <div class="badge text-bg-dark">Trades ${num(summary.live_trades).toFixed(0)}</div>
                                <div class="badge text-bg-dark">Win ${num(summary.win_rate * 100).toFixed(1)}%</div>
                                <div class="badge text-bg-dark">PF ${num(summary.profit_factor).toFixed(2)}</div>
                            </div>
                        </div>
                    </div></div>
                </div>
                <div class="col-12 col-xl-8">
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body">
                        <div class="row g-2">
                            <div class="col-6 col-md-3">${metric("Trades", num(summary.live_trades).toFixed(0))}</div>
                            <div class="col-6 col-md-3">${metric("Win Rate", `${(num(summary.win_rate) * 100).toFixed(2)}%`)}</div>
                            <div class="col-6 col-md-3">${metric("Total PnL", num(summary.total_pnl).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Total R", num(summary.total_r).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Expectancy", num(summary.expectancy).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Profit Factor", num(summary.profit_factor).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Avg Hold", `${num(summary.average_hold_minutes).toFixed(1)}m`)}</div>
                            <div class="col-6 col-md-3">${metric("Breakevens", num(summary.breakevens).toFixed(0))}</div>
                        </div>
                    </div></div>
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Setup / Regime Performance</h2>
                        <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Setup</th><th>Regime</th><th>Session</th><th>Count</th><th>Avg PnL</th><th>Avg R</th><th>Total R</th><th>Hold Mins</th><th>Close Reason</th><th>Blocked Reason</th></tr></thead>
                        <tbody>${sortedRows.map((row) => `<tr><td>${esc(row.setup_name || row.setup_family || "UNKNOWN")}</td><td>${esc(row.regime_name || "UNKNOWN")}</td><td>${esc(row.session_name || "UNKNOWN")}</td><td>${esc(row.trade_count || 0)}</td><td>${num(row.average_pnl).toFixed(2)}</td><td>${num(row.average_r).toFixed(2)}</td><td>${num(row.total_r).toFixed(2)}</td><td>${num(row.average_hold_minutes).toFixed(1)}</td><td>${esc(row.close_reason || "")}</td><td>${esc(row.blocked_reason || "")}</td></tr>`).join("") || `<tr><td colspan="10" class="text-secondary">No performance rows available yet.</td></tr>`}</tbody></table></div>
                    </div></div>
                </div>
                <div class="col-12 col-xl-4">
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body">
                        <h2 class="h5">Best / Worst</h2>
                        <div class="mb-2"><strong>Best Setup:</strong> ${esc(bestSetup.setup_name || "N/A")} <span class="text-secondary">(${num(bestSetup.total_r).toFixed(2)} R)</span></div>
                        <div><strong>Worst Setup:</strong> ${esc(worstSetup.setup_name || "N/A")} <span class="text-secondary">(${num(worstSetup.total_r).toFixed(2)} R)</span></div>
                    </div></div>
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Daily Breakdowns</h2>
                        <div class="small text-secondary mb-2">Close reasons</div>
                        ${(topClose.length ? topClose : [["none", 0]]).map((item) => `<div class="d-flex justify-content-between py-1 border-bottom border-secondary-subtle"><span>${esc(item[0])}</span><span class="badge text-bg-dark">${esc(item[1])}</span></div>`).join("")}
                        <div class="small text-secondary mt-3 mb-2">Execution reasons</div>
                        ${(topExec.length ? topExec : [["none", 0]]).map((item) => `<div class="d-flex justify-content-between py-1 border-bottom border-secondary-subtle"><span>${esc(item[0])}</span><span class="badge text-bg-dark">${esc(item[1])}</span></div>`).join("")}
                        <div class="small text-secondary mt-3 mb-2">Blocked reasons</div>
                        ${(topBlocked.length ? topBlocked : [["none", 0]]).map((item) => `<div class="d-flex justify-content-between py-1 border-bottom border-secondary-subtle"><span>${esc(item[0])}</span><span class="badge text-bg-dark">${esc(item[1])}</span></div>`).join("")}
                    </div></div>
                </div>
            </div>`;
    }

    function renderLogs() {
        const blocked = state.status.runtime?.blocked_setups || [];
        const validNotExecuted = state.status.runtime?.valid_not_executed || [];
        document.getElementById("section-logs").innerHTML = `
            <div class="row g-3">
                <div class="col-12 col-xl-7"><div class="card border-0 shadow-sm"><div class="card-body">
                    <div class="row g-2 mb-3"><div class="col-md-3"><select class="form-select" id="log-level"><option value="">All Levels</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select></div><div class="col-md-3"><input class="form-control" id="log-source" placeholder="Source / event"></div><div class="col-md-3"><input class="form-control" id="log-date" type="date"></div><div class="col-md-3"><button class="btn btn-outline-light w-100" id="refresh-logs">Apply</button></div><div class="col-12"><input class="form-control" id="log-search" placeholder="Filter by message or details"></div></div>
                    <div class="sticky-log">${(state.logs || []).map((entry) => `<div class="mb-3 border-bottom border-secondary-subtle pb-2"><div><span class="text-secondary">${esc(entry.timestamp || "")}</span> <span class="badge text-bg-dark">${esc(entry.level || "")}</span> <strong>${esc(entry.event_type || "")}</strong></div><div>${esc(entry.message || "")}</div><div class="small text-secondary">${esc(entry.reason_code || "")}</div></div>`).join("")}</div>
                </div></div></div>
                <div class="col-12 col-xl-5">
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body"><h2 class="h5">Audit Trail</h2><div class="sticky-log">${(state.audit || []).map((entry) => `<div class="mb-3 border-bottom border-secondary-subtle pb-2"><div><span class="text-secondary">${esc(entry.timestamp || "")}</span> <span class="badge text-bg-secondary">${esc(entry.actor || "")}</span></div><div><strong>${esc(entry.action || "")}</strong></div><div>${esc(entry.target || "")}</div></div>`).join("")}</div></div></div>
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body"><h2 class="h5">Blocked Execution Detail</h2><div class="sticky-log">${blocked.map((entry) => `<div class="mb-3 border-bottom border-secondary-subtle pb-2"><div><span class="text-secondary">${esc(entry.timestamp || "")}</span> <span class="badge text-bg-secondary">${esc(entry.reason_code || "")}</span></div><div><strong>${esc(entry.setup_family || "")}</strong> ${esc(entry.side || "")}</div><div class="small text-secondary">${esc(entry.session_name || "")} | ${esc(entry.regime_name || "")} | spread ${num(entry.spread_points).toFixed(1)} / ${num(entry.spread_limit_used).toFixed(1)} (${esc(entry.spread_limit_source || "default")})</div><div class="small text-secondary">send_attempted=${entry.order_send_attempted ? "yes" : "no"} | cooldown=${entry.cooldown_applied ? "yes" : "no"} | duplicate_guard=${entry.duplicate_guard_applied ? "yes" : "no"} | stage=${esc(entry.failure_stage || "n/a")}</div><div>${esc(entry.reason || "")} ${entry.order_send_retcode ? `(retcode=${esc(entry.order_send_retcode)} ${esc(entry.order_send_retcode_text || "")})` : ""}</div></div>`).join("") || `<div class="text-secondary">No recent blocked execution attempts.</div>`}</div></div></div>
                    <div class="card border-0 shadow-sm"><div class="card-body"><h2 class="h5">Valid But Not Executed</h2><div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Time</th><th>Setup</th><th>Outcome</th><th>Spread</th><th>Send</th><th>Cooldown</th></tr></thead><tbody>${validNotExecuted.map((row) => `<tr><td>${esc(row.timestamp || "")}</td><td>${esc(row.setup_family || "")} ${esc(row.side || "")}<div class="small text-secondary">${esc(row.session_name || "")} | ${esc(row.regime_name || "")}</div></td><td>${esc(row.final_outcome_reason || row.reason_code || "")}<div class="small text-secondary">${esc(row.reason || "")}</div></td><td>${num(row.spread_points).toFixed(1)} / ${num(row.spread_limit_used).toFixed(1)}<div class="small text-secondary">${esc(row.spread_limit_source || "default")}</div></td><td>${row.order_send_attempted ? `yes${row.order_send_retcode ? ` (${esc(row.order_send_retcode)})` : ""}` : "no"}</td><td>${row.cooldown_applied ? "yes" : "no"} / ${row.duplicate_guard_applied ? "yes" : "no"}</td></tr>`).join("") || `<tr><td colspan="6" class="text-secondary">No recent valid-but-not-executed attempts.</td></tr>`}</tbody></table></div></div></div>
                </div>
            </div>`;
        document.getElementById("refresh-logs").onclick = () => refreshLogs();
    }

    function renderHealth() {
        const jobs = state.jobs || [];
        const worker = currentWorkerStatus();
        document.getElementById("section-health").innerHTML = `
            <div class="row g-3">
                <div class="col-12 col-lg-6"><div class="card border-0 shadow-sm"><div class="card-body"><h2 class="h5">System Health</h2><pre>${esc(JSON.stringify(state.status.control || {}, null, 2))}</pre></div></div></div>
                <div class="col-12 col-lg-6"><div class="card border-0 shadow-sm"><div class="card-body"><h2 class="h5">Worker</h2><div class="mb-2">${badge(workerStateLabel(worker), workerTone(worker))}</div><pre>${esc(JSON.stringify(worker, null, 2))}</pre></div></div></div>
                <div class="col-12"><div class="card border-0 shadow-sm"><div class="card-body"><h2 class="h5">Runtime Snapshot</h2><pre>${esc(JSON.stringify(state.status.runtime || {}, null, 2))}</pre></div></div></div>
                <div class="col-12"><div class="card border-0 shadow-sm"><div class="card-body"><h2 class="h5">Recent Jobs</h2><div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>ID</th><th>Type</th><th>State</th><th>Progress</th><th>Run</th><th>Failure</th></tr></thead><tbody>${jobs.map((job) => `<tr><td>${esc(job.id)}</td><td>${esc(job.job_type)}</td><td>${esc(job.state)}</td><td>${esc(job.progress_current || 0)} / ${esc(job.progress_total || 0)}</td><td>${esc(job.related_run_type || "")} ${esc(job.related_run_id || "")}</td><td>${esc(job.failure_reason || job.interrupted_reason || "")}</td></tr>`).join("") || `<tr><td colspan="6" class="text-secondary">No jobs yet.</td></tr>`}</tbody></table></div></div></div></div>
            </div>`;
    }

    async function refreshConfig() {
        const payload = await api("/api/config");
        state.config = payload.config || {};
        state.configMetadata = payload.metadata || {};
        if (payload.runtime_state) {
            state.status = { ...state.status, control: payload.runtime_state };
        }
        renderAll();
    }
    async function refreshStatus() {
        const [statusPayload, lebprimePayload] = await Promise.all([
            api("/api/status"),
            api("/api/strategies/lebprime-v3-4-0/status"),
        ]);
        state.status = statusPayload;
        state.lebprimeLive = lebprimePayload;
        state.workerStatus = state.status.worker || state.workerStatus;
        renderAll();
    }
    async function fetchWorkerStatus() {
        const payload = await api("/api/worker/status");
        state.workerStatus = payload.worker || {};
        renderAll();
    }
    async function refreshPositions() {
        const positionsPayload = await api("/api/manual/positions");
        state.positions = positionsPayload.positions || [];
        renderAll();
    }
    async function refreshLogs() {
        const logsPayload = await api(`/api/logs?limit=100&level=${encodeURIComponent(val("log-level"))}&source=${encodeURIComponent(val("log-source"))}&search=${encodeURIComponent(val("log-search"))}&date_from=${encodeURIComponent(val("log-date"))}`);
        const auditPayload = await api("/api/audit?limit=50");
        state.logs = logsPayload.logs || [];
        state.audit = auditPayload.audit || [];
        renderAll();
    }
    async function fetchJobs() {
        const payload = await api("/api/jobs?limit=30");
        state.jobs = payload.jobs || [];
        renderAll();
    }
    async function refreshExecutionMode() {
        state.modeControl.loading = true;
        state.modeControl.error = null;
        renderAll();
        try {
            dashboardLog("mode", "GET /api/mode");
            const payload = await api("/api/mode");
            const normalizedMode = normalizeModeData(payload.mode || payload);
            state.modeControl = {
                mode: normalizedMode,
                options: normalizeModeOptions(payload.options),
                loading: false,
                error: null,
            };
            state.config.mode = normalizedMode;
            dashboardLog("mode", "execution mode payload received", normalizedMode);
        } catch (error) {
            state.modeControl.loading = false;
            state.modeControl.error = error.message;
            dashboardLog("mode", "execution mode load failed", { error: error.message }, "error");
            renderAll();
            throw error;
        }
        renderAll();
    }
    async function fetchIndicatorLab() {
        ensureIndicatorLabDraft();
        try {
            const [datasets, campaigns, lebprime, vss3] = await Promise.all([
                api("/api/indicator-lab/datasets"),
                api("/api/indicator-lab/campaigns?limit=20"),
                api("/api/indicator-lab/parameters/LEBPRIME"),
                api("/api/indicator-lab/parameters/VSS3"),
            ]);
            state.indicatorLab.datasets = datasets.datasets || [];
            state.indicatorLab.campaigns = campaigns.campaigns || [];
            state.indicatorLab.parameters = { LEBPRIME: lebprime.parameters || [], VSS3: vss3.parameters || [] };
            const selected = num(state.indicatorLab.selectedCampaignId, 0) || num(state.indicatorLab.campaigns[0]?.id, 0);
            state.indicatorLab.selectedCampaignId = selected || null;
            if (selected) {
                const results = await api(`/api/indicator-lab/campaigns/${selected}/results?limit=200`);
                state.indicatorLab.results = results.results || [];
                await selectIndicatorLabResultForCampaign(selected);
            } else {
                state.indicatorLab.results = [];
                state.indicatorLab.selectedTestId = null;
                state.indicatorLab.zones = [];
            }
            state.indicatorLab.error = null;
            state.indicatorLabUi.error = "";
        } catch (error) {
            state.indicatorLab.error = error.message;
            state.indicatorLabUi.error = error.message;
        }
        renderAll();
    }

    async function selectIndicatorLabResultForCampaign(campaignId, options = {}) {
        const lab = state.indicatorLab;
        const results = asArray(lab.results);
        const campaign = asArray(lab.campaigns).find((row) => num(row.id) === num(campaignId)) || null;
        if (!results.length) {
            lab.selectedTestId = null;
            lab.zones = [];
            return;
        }
        const existing = results.find((row) => num(row.id) === num(lab.selectedTestId));
        const terminal = ["COMPLETED", "FAILED", "PARTIAL"].includes(textOr(campaign?.state).toUpperCase());
        let target = null;
        if (!options.force && existing && !terminal && results.length !== 1) {
            target = existing;
        } else {
            const bestId = num(campaign?.best_test_id, 0);
            target = results.find((row) => bestId && num(row.id) === bestId)
                || results.find((row) => textOr(row.state).toUpperCase() === "COMPLETED")
                || (results.length === 1 ? results[0] : existing)
                || results[0];
        }
        if (!target) return;
        if (num(lab.selectedTestId) === num(target.id) && asArray(lab.zones).length && !options.force) return;
        lab.selectedTestId = num(target.id);
        try {
            const zones = await api(`/api/indicator-lab/tests/${lab.selectedTestId}/zones`);
            lab.zones = zones.zones || [];
            const selectedZone = lab.zones.find((zone) => num(zone.id) === num(lab.selectedZoneId)) || lab.zones[0] || null;
            if (selectedZone) {
                await loadIndicatorZone(selectedZone.id, { silent: true });
            } else {
                lab.selectedZoneId = null;
                lab.selectedZone = null;
                lab.zoneEvents = [];
            }
        } catch (error) {
            lab.zones = [];
            state.indicatorLabUi.error = `Failed to load zones for test #${lab.selectedTestId}: ${error.message}`;
        }
    }

    function indicatorLabProgress(row) {
        const current = num(row?.progress_current, 0);
        const total = num(row?.progress_total || row?.generated_configurations, 0);
        const pct = total > 0 ? Math.round((current / total) * 100) : 0;
        return { current, total, pct };
    }

    function indicatorLabCampaignId(payload) {
        return num(payload?.campaign?.id || payload?.result?.campaign?.id || payload?.campaign_id, 0) || null;
    }

    const zoneDisplaySide = (value) => {
        const side = textOr(value).toUpperCase();
        if (side === "DEMAND" || side === "BUY") return "BUY";
        if (side === "SUPPLY" || side === "SELL") return "SELL";
        return side || "N/A";
    };

    const zoneDisplayStatus = (zone) => {
        const state = textOr(zone?.terminal_state).toUpperCase();
        if (state === "TP_HIT") return "TP";
        if (state === "SL_HIT") return "SL";
        if (state === "BE_EXIT") return "BE";
        if (["CANCELED", "INVALIDATED", "EXPIRED"].includes(state)) return state;
        if (zone?.activated_time) return "ACTIVE";
        return "PENDING";
    };

    function indicatorZoneDownload(path) {
        window.location.href = path;
    }

    function filteredIndicatorZones() {
        const filters = state.indicatorLab.zoneFilters || {};
        return asArray(state.indicatorLab.zones).filter((zone) => {
            const side = zoneDisplaySide(zone.side);
            const status = zoneDisplayStatus(zone);
            const sourceTime = textOr(zone.source_time);
            const engineId = textOr(zone.engine_zone_id).toLowerCase();
            if (filters.side && filters.side !== "ALL" && side !== filters.side) return false;
            if (filters.status && filters.status !== "ALL" && status !== filters.status) return false;
            if (filters.flipped === "YES" && !zone.is_flipped) return false;
            if (filters.flipped === "NO" && zone.is_flipped) return false;
            if (filters.from && sourceTime && sourceTime.slice(0, 10) < filters.from) return false;
            if (filters.to && sourceTime && sourceTime.slice(0, 10) > filters.to) return false;
            if (filters.search) {
                const query = String(filters.search).trim().toLowerCase();
                if (query && !engineId.includes(query) && String(zone.id || "").toLowerCase() !== query) return false;
            }
            return true;
        });
    }

    async function loadIndicatorZone(zoneId, options = {}) {
        const lab = state.indicatorLab;
        const id = num(zoneId, 0);
        if (!id) return;
        lab.selectedZoneId = id;
        try {
            const [zonePayload, eventsPayload] = await Promise.all([
                api(`/api/indicator-lab/zones/${id}`),
                api(`/api/indicator-lab/zones/${id}/events`),
            ]);
            lab.selectedZone = zonePayload.zone || null;
            lab.zoneEvents = eventsPayload.events || [];
            lab.selectedTestId = num(lab.selectedZone?.test_id, lab.selectedTestId);
            state.indicatorLabUi.error = "";
        } catch (error) {
            state.indicatorLabUi.error = error.message;
        }
        if (!options.silent) renderAll({ force: true });
    }

    async function selectAdjacentIndicatorZone(direction) {
        const zones = filteredIndicatorZones();
        if (!zones.length) return;
        const currentIndex = Math.max(0, zones.findIndex((zone) => num(zone.id) === num(state.indicatorLab.selectedZoneId)));
        const nextIndex = Math.max(0, Math.min(zones.length - 1, currentIndex + direction));
        await loadIndicatorZone(zones[nextIndex].id);
    }

    function detailItem(label, value) {
        return `<div class="col-md-3 col-sm-6"><div class="small text-secondary">${esc(label)}</div><div class="fw-semibold">${esc(value ?? "N/A")}</div></div>`;
    }

    function renderZoneDetails() {
        const zone = state.indicatorLab.selectedZone;
        if (!zone) return renderEmptyState("No Zone Selected", "Select a zone row to inspect creation, lifecycle, trade, parameters, and events.");
        const metadata = asObject(zone.metadata_json);
        const snapshot = asObject(zone.parameter_snapshot_json);
        const params = asObject(snapshot.parameters);
        const events = asArray(state.indicatorLab.zoneEvents);
        const paramRows = Object.entries(params).map(([key, value]) => `<tr><td>${esc(key)}</td><td>${esc(value)}</td></tr>`).join("");
        return `
            <div class="row g-3">
                <div class="col-12"><h4 class="h6 text-uppercase text-secondary fw-semibold mb-0">Zone Details</h4></div>
                <div class="col-12"><div class="row g-2">
                    ${detailItem("Zone ID", zone.engine_zone_id || zone.id)}
                    ${detailItem("Indicator", zone.indicator)}
                    ${detailItem("Campaign", zone.campaign_id)}
                    ${detailItem("Test", zone.test_id)}
                    ${detailItem("Dataset", zone.dataset_id)}
                    ${detailItem("Side", zoneDisplaySide(zone.side))}
                    ${detailItem("Flipped", zone.is_flipped ? "YES" : "NO")}
                    ${detailItem("Parent", zone.parent_zone_id || "")}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Source</h5><div class="row g-2">
                    ${detailItem("Timestamp", zone.source_time)}
                    ${detailItem("Bar", zone.source_bar_index)}
                    ${detailItem("Open", zone.source_open)}
                    ${detailItem("High", zone.source_high)}
                    ${detailItem("Low", zone.source_low)}
                    ${detailItem("Close", zone.source_close)}
                    ${detailItem("Body", zone.source_body)}
                    ${detailItem("Range", zone.source_range)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Detection</h5><div class="row g-2">
                    ${detailItem("ATR", zone.atr_at_source)}
                    ${detailItem("Average Body", zone.average_body_at_source)}
                    ${detailItem("Momentum Body", zone.momentum_body || metadata.body_mult_actual)}
                    ${detailItem("Momentum Ratio", zone.momentum_body_ratio)}
                    ${detailItem("Momentum Count", zone.momentum_count || metadata.same_direction_count)}
                    ${detailItem("Momentum Span", zone.momentum_span || metadata.momentum_span_used)}
                    ${detailItem("Base Bars", zone.base_bar_count)}
                    ${detailItem("Refinement", zone.refinement_method || metadata.zone_refinement_method)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Zone</h5><div class="row g-2">
                    ${detailItem("Original Top", metadata.original_zone_top || metadata.raw_zone_top)}
                    ${detailItem("Original Bottom", metadata.original_zone_bottom || metadata.raw_zone_bottom)}
                    ${detailItem("Top", zone.zone_top)}
                    ${detailItem("Bottom", zone.zone_bottom)}
                    ${detailItem("Mid", zone.zone_mid)}
                    ${detailItem("Width", zone.zone_width_price || zone.zone_width)}
                    ${detailItem("ATR Width", zone.zone_width_atr)}
                    ${detailItem("Overlap", metadata.overlap_detected ?? "")}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Lifecycle</h5><div class="row g-2">
                    ${detailItem("Candidate", `${zone.candidate_time || "N/A"} | ${zone.candidate_bar_index ?? "N/A"}`)}
                    ${detailItem("Created", `${zone.created_time || "N/A"} | ${zone.created_bar_index ?? "N/A"}`)}
                    ${detailItem("Confirmed", `${zone.confirmed_time || "N/A"} | ${zone.confirmed_bar_index ?? "N/A"}`)}
                    ${detailItem("Armed", `${zone.armed_time || "N/A"} | ${zone.armed_bar_index ?? "N/A"}`)}
                    ${detailItem("Limit", `${zone.limit_created_time || "N/A"} | ${zone.limit_created_bar_index ?? "N/A"}`)}
                    ${detailItem("First Retest", `${zone.first_retest_time || "N/A"} | ${zone.first_retest_bar_index ?? "N/A"}`)}
                    ${detailItem("Entry 1 Activated", `${zone.entry1_activated_time || zone.activated_time || "N/A"} | ${zone.entry1_activated_bar_index ?? zone.activated_bar_index ?? "N/A"}`)}
                    ${detailItem("Entry 2 Activated", `${zone.entry2_activated_time || "N/A"} | ${zone.entry2_activated_bar_index ?? "N/A"}`)}
                    ${detailItem("Terminal", `${zone.terminal_time || "N/A"} | ${zone.terminal_bar_index ?? "N/A"}`)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Trade</h5><div class="row g-2">
                    ${detailItem("Entry", zone.entry_price)}
                    ${detailItem("Entry 1", zone.entry1)}
                    ${detailItem("Entry 2", zone.entry2)}
                    ${detailItem("SL", zone.sl_price)}
                    ${detailItem("Structural Swing", zone.structural_swing_price)}
                    ${detailItem("TP1", zone.tp1_price)}
                    ${detailItem("TP2", zone.tp2_price || zone.tp_price)}
                    ${detailItem("RR", zone.rr)}
                    ${detailItem("Buffer", zone.buffer_used || metadata.buffer_used)}
                    ${detailItem("Departure", zone.departure_distance)}
                    ${detailItem("MFE", zone.mfe_r)}
                    ${detailItem("MAE", zone.mae_r)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Result</h5><div class="row g-2">
                    ${detailItem("State", zone.terminal_state || zoneDisplayStatus(zone))}
                    ${detailItem("Exit", zone.exit_price || zone.result_price)}
                    ${detailItem("Result R", zone.result_r)}
                    ${detailItem("Result Points", zone.result_points)}
                    ${detailItem("Holding Bars", zone.holding_bars)}
                    ${detailItem("Holding Minutes", zone.holding_minutes)}
                    ${detailItem("Reason", zone.terminal_reason || zone.activation_reason || zone.creation_reason)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Parity Data</h5><div class="row g-2">
                    ${detailItem("Source Time", zone.source_time)}
                    ${detailItem("Side", zoneDisplaySide(zone.side))}
                    ${detailItem("Top", zone.zone_top)}
                    ${detailItem("Bottom", zone.zone_bottom)}
                    ${detailItem("Created Time", zone.created_time)}
                    ${detailItem("Limit Time", zone.limit_created_time)}
                    ${detailItem("Activation Time", zone.activated_time)}
                    ${detailItem("SL", zone.sl_price)}
                    ${detailItem("TP", zone.tp_price)}
                    ${detailItem("Terminal", zone.terminal_state)}
                    ${detailItem("Result", zone.result_r)}
                </div></div>
                <div class="col-12"><h5 class="h6 mb-2">Parameters</h5>
                    <div class="small text-secondary mb-2">Hash: ${esc(zone.parameter_hash || "")}</div>
                    <details><summary class="small">Parameter Snapshot</summary><div class="table-responsive mt-2"><table class="table table-sm strategy-table"><tbody>${paramRows || `<tr><td class="text-secondary">No parameters.</td></tr>`}</tbody></table></div></details>
                </div>
                <div class="col-12"><h5 class="h6 mb-2">Events</h5>
                    <div class="table-responsive"><table class="table table-sm strategy-table">
                        <thead><tr><th>Time</th><th>Bar</th><th>Event</th><th>Price</th><th>Reason</th></tr></thead>
                        <tbody>${events.map((event) => `<tr><td>${esc(event.timestamp)}</td><td>${esc(event.bar_index)}</td><td>${esc(event.event_type)}</td><td>${esc(event.price ?? "")}</td><td>${esc(event.reason || "")}</td></tr>`).join("") || `<tr><td colspan="5" class="text-secondary">No events for this zone.</td></tr>`}</tbody>
                    </table></div>
                </div>
            </div>`;
    }

    function indicatorParamInputs(indicator) {
        const draft = ensureIndicatorLabDraft();
        const paramDraft = asObject(draft.parameters[indicator]);
        const optDraft = asObject(draft.optimization[indicator]);
        return asArray(state.indicatorLab.parameters[indicator]).map((param) => {
            const value = paramDraft[param.name] ?? param.default;
            const opt = asObject(optDraft[param.name]);
            const type = param.type === "bool" ? "checkbox" : (param.type === "int" || param.type === "float" ? "number" : (param.type === "enum" ? "select" : "text"));
            const valueControl = type === "checkbox"
                ? `<input class="form-check-input il-param-value" data-param="${esc(param.name)}" data-kind="${esc(param.type)}" type="checkbox" ${value ? "checked" : ""}>`
                : type === "select"
                    ? `<select class="form-select form-select-sm il-param-value" data-param="${esc(param.name)}" data-kind="${esc(param.type)}">${asArray(param.values).map((option) => `<option value="${esc(option)}" ${String(option) === String(value) ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`
                    : `<input class="form-control form-control-sm il-param-value" data-param="${esc(param.name)}" data-kind="${esc(param.type)}" type="${type}" value="${esc(value)}" ${param.step ? `step="${esc(param.step)}"` : ""}>`;
            return `<tr>
                <td><div class="fw-semibold">${esc(param.label || param.name)}</div><div class="small text-secondary">${esc(param.pine_name || "")}</div></td>
                <td>${valueControl}</td>
                <td><input class="form-check-input il-opt-enabled" data-param="${esc(param.name)}" type="checkbox" ${opt.enabled ? "checked" : ""}></td>
                <td><input class="form-control form-control-sm il-opt-start" data-param="${esc(param.name)}" placeholder="start" value="${esc(opt.start ?? "")}"></td>
                <td><input class="form-control form-control-sm il-opt-end" data-param="${esc(param.name)}" placeholder="end" value="${esc(opt.end ?? "")}"></td>
                <td><input class="form-control form-control-sm il-opt-step" data-param="${esc(param.name)}" placeholder="step" value="${esc(opt.step ?? "")}"></td>
                <td><input class="form-control form-control-sm il-opt-values" data-param="${esc(param.name)}" placeholder="CSV values" value="${esc(opt.values ?? "")}"></td>
            </tr>`;
        }).join("");
    }

    function collectIndicatorCampaignPayload() {
        captureIndicatorLabDraft();
        const draft = ensureIndicatorLabDraft();
        const indicator = draft.campaign.indicator || state.indicatorLab.selectedIndicator || "LEBPRIME";
        const paramDraft = asObject(draft.parameters[indicator]);
        const optDraft = asObject(draft.optimization[indicator]);
        const schema = asArray(state.indicatorLab.parameters[indicator]);
        const fixed = {};
        const grid = {};
        schema.forEach((param) => {
            const name = param.name;
            const kind = param.type;
            const raw = paramDraft[name] ?? param.default;
            if (kind === "bool") fixed[name] = !!raw;
            else if (kind === "int") fixed[name] = Number.parseInt(raw, 10);
            else if (kind === "float") fixed[name] = Number.parseFloat(raw);
            else fixed[name] = raw;
            const opt = asObject(optDraft[name]);
            if (!opt.enabled) return;
            const item = { enabled: true };
            const values = csvValues(opt.values);
            if (values.length) item.values = values;
            if (opt.start !== undefined && opt.start !== "") item.start = opt.start;
            if (opt.end !== undefined && opt.end !== "") item.end = opt.end;
            if (opt.step !== undefined && opt.step !== "") item.step = opt.step;
            grid[name] = item;
        });
        return {
            actor: "dashboard",
            name: draft.campaign.name || "Indicator Campaign",
            dataset_id: num(draft.campaign.dataset_id, 0),
            indicator,
            fixed_parameters: fixed,
            optimization_grid: grid,
            notes: draft.campaign.notes || "",
            confirm_large: !!draft.campaign.confirm_large,
        };
    }

    function renderIndicatorLab() {
        const container = ensureContainer("section-indicator-lab");
        const lab = state.indicatorLab;
        const draft = ensureIndicatorLabDraft();
        const datasets = asArray(lab.datasets);
        const campaigns = asArray(lab.campaigns);
        const results = asArray(lab.results);
        const ui = state.indicatorLabUi || {};
        const selectedIndicator = draft.campaign.indicator || lab.selectedIndicator || "LEBPRIME";
        lab.selectedIndicator = selectedIndicator;
        const latest = campaigns.find((row) => num(row.id) === num(lab.selectedCampaignId)) || campaigns[0] || null;
        const selectedDatasetId = draft.campaign.dataset_id ?? String(datasets[0]?.id || "");
        const latestProgress = indicatorLabProgress(latest);
        container.innerHTML = `
            <div class="row g-3">
                <div class="col-12 d-flex justify-content-between align-items-center">
                    <div>
                        <h2 class="h4 mb-1">Indicator Lab</h2>
                        <div class="small text-secondary">Frozen-data experiments for LebPrime and VSS3. No live order path is used.</div>
                        ${latest ? `<div class="small mt-1">${badge(latest.state || "IDLE", stateTone(latest.state))} <span class="text-secondary">Campaign #${esc(latest.id)} | ${esc(latestProgress.current)} / ${esc(latestProgress.total)} tests (${esc(latestProgress.pct)}%)</span></div>` : ""}
                    </div>
                    <button class="btn btn-outline-primary" id="il-refresh">Refresh</button>
                </div>
                ${lab.error ? `<div class="col-12"><div class="alert alert-danger py-2">${esc(lab.error)}</div></div>` : ""}
                ${ui.error ? `<div class="col-12"><div class="alert alert-danger py-2">${esc(ui.error)}</div></div>` : ""}
                ${ui.message ? `<div class="col-12"><div class="alert alert-success py-2">${esc(ui.message)}</div></div>` : ""}
                <div class="col-lg-5">
                    <div class="panel h-100">
                        <h3 class="h6 text-uppercase text-secondary fw-semibold">Datasets</h3>
                        <div class="row g-2 mb-3">
                            <div class="col-12">${field("il-ds-name", "Name", draft.dataset.name ?? "XAUUSD_M1_2026_08_01_2026_09_14")}</div>
                            <div class="col-6">${select("il-ds-symbol", "Symbol", LEBPRIME_BROKER_SYMBOLS, draft.dataset.symbol ?? "XAUUSD")}</div>
                            <div class="col-6">${select("il-ds-timeframe", "Timeframe", ["M1", "M3", "M5", "M15", "M30", "H1"], draft.dataset.timeframe ?? "M1")}</div>
                            <div class="col-6">${field("il-ds-start", "Start", draft.dataset.start_date ?? "", "date")}</div>
                            <div class="col-6">${field("il-ds-end", "End", draft.dataset.end_date ?? "", "date")}</div>
                            <div class="col-12">${field("il-ds-notes", "Notes", draft.dataset.notes ?? "")}</div>
                            <div class="col-12"><button class="btn btn-primary" id="il-create-dataset">Create MT5 Dataset</button></div>
                        </div>
                        <div class="table-responsive"><table class="table table-sm strategy-table">
                            <thead><tr><th>Name</th><th>Symbol</th><th>TF</th><th>Bars</th><th>Status</th><th>Hash</th></tr></thead>
                            <tbody>${datasets.map((row) => `<tr><td>${esc(row.name)}</td><td>${esc(row.symbol)}</td><td>${esc(row.timeframe)}</td><td>${num(row.bar_count)}</td><td><span class="badge text-bg-${stateTone(row.status)}">${esc(row.status)}</span></td><td class="small text-secondary">${esc(String(row.content_hash || "").slice(0, 10))}</td></tr>`).join("") || `<tr><td colspan="6" class="text-secondary">No frozen datasets yet.</td></tr>`}</tbody>
                        </table></div>
                    </div>
                </div>
                <div class="col-lg-7">
                    <div class="panel h-100">
                        <h3 class="h6 text-uppercase text-secondary fw-semibold">New Campaign</h3>
                        <div class="row g-2">
                            <div class="col-md-5">${field("il-campaign-name", "Campaign Name", draft.campaign.name ?? "Indicator Campaign")}</div>
                            <div class="col-md-4">${select("il-dataset", "Dataset", datasets.map((row) => String(row.id)), String(selectedDatasetId))}</div>
                            <div class="col-md-3">${select("il-indicator", "Indicator", ["LEBPRIME", "VSS3"], selectedIndicator)}</div>
                            <div class="col-12">${field("il-notes", "Notes", draft.campaign.notes ?? "")}</div>
                        </div>
                        <div class="table-responsive mt-3"><table class="table table-sm strategy-table">
                            <thead><tr><th>Parameter</th><th>Value</th><th>Opt</th><th>Start</th><th>End</th><th>Step</th><th>Values</th></tr></thead>
                            <tbody>${indicatorParamInputs(selectedIndicator)}</tbody>
                        </table></div>
                        <div class="d-flex flex-wrap gap-2 align-items-center">
                            <button class="btn btn-outline-info" id="il-preview">Preview</button>
                            <button class="btn btn-primary" id="il-start" ${ui.submitting ? "disabled" : ""}>${ui.submitting ? "Creating campaign..." : "Start Campaign"}</button>
                            <div class="form-check"><input class="form-check-input" id="il-confirm-large" type="checkbox" ${draft.campaign.confirm_large ? "checked" : ""}><label class="form-check-label" for="il-confirm-large">Confirm large matrix</label></div>
                            <span class="small text-secondary" id="il-preview-result"></span>
                        </div>
                    </div>
                </div>
                <div class="col-12">
                    <div class="panel">
                        <h3 class="h6 text-uppercase text-secondary fw-semibold">Running Campaigns</h3>
                        <div class="table-responsive"><table class="table table-sm strategy-table">
                            <thead><tr><th>ID</th><th>Name</th><th>Indicator</th><th>Dataset</th><th>State</th><th>Progress</th><th>Best</th><th></th></tr></thead>
                            <tbody>${campaigns.map((row) => { const progress = indicatorLabProgress(row); return `<tr class="${num(row.id) === num(lab.selectedCampaignId) ? "table-active" : ""}"><td>${row.id}</td><td>${esc(row.name)}</td><td>${esc(row.indicator)}</td><td>${row.dataset_id}</td><td>${badge(row.state || "IDLE", stateTone(row.state))}</td><td>${esc(progress.current)} / ${esc(progress.total)} <span class="text-secondary">(${esc(progress.pct)}%)</span></td><td>${esc(row.best_test_id || "")}</td><td><button class="btn btn-sm btn-outline-light" data-il-campaign="${row.id}">Load</button></td></tr>`; }).join("") || `<tr><td colspan="8" class="text-secondary">No campaigns yet.</td></tr>`}</tbody>
                        </table></div>
                    </div>
                </div>
                <div class="col-12">
                    <div class="panel">
                        <h3 class="h6 text-uppercase text-secondary fw-semibold">Results</h3>
                        <div class="table-responsive"><table class="table table-sm strategy-table">
                            <thead><tr><th>Rank</th><th>Test</th><th>State</th><th>Zones</th><th>Flipped</th><th>Limits</th><th>Activated</th><th>TP</th><th>SL</th><th>BE</th><th>Win %</th><th>Net R</th><th>PF</th><th></th></tr></thead>
                            <tbody>${results.map((row, index) => { const m = asObject(row.metrics_json); return `<tr class="${num(row.id) === num(lab.selectedTestId) ? "table-active" : ""}"><td>${index + 1}</td><td>${row.id}</td><td>${badge(row.state || "PENDING", stateTone(row.state))}</td><td>${num(m.zones_created)}</td><td>${num(m.flipped_zones)}</td><td>${num(m.limit_created)}</td><td>${num(m.activated)}</td><td>${num(m.tp_hit)}</td><td>${num(m.sl_hit)}</td><td>${num(m.be_exit)}</td><td>${num(m.win_rate).toFixed(2)}</td><td>${num(m.net_r).toFixed(2)}</td><td>${num(m.profit_factor).toFixed(2)}</td><td><button class="btn btn-sm btn-outline-info" data-il-test="${row.id}">Zones</button></td></tr>`; }).join("") || `<tr><td colspan="14" class="text-secondary">Results appear as worker tests complete.</td></tr>`}</tbody>
                        </table></div>
                    </div>
                </div>
                <div class="col-12">
                    <div class="panel">
                        <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                            <h3 class="h6 text-uppercase text-secondary fw-semibold mb-0">Zone Explorer</h3>
                            <div class="d-flex flex-wrap gap-2">
                                <button class="btn btn-sm btn-outline-light" id="il-zone-prev">Previous Zone</button>
                                <button class="btn btn-sm btn-outline-light" id="il-zone-next">Next Zone</button>
                                <button class="btn btn-sm btn-outline-info" id="il-open-replay" ${lab.selectedTestId ? "" : "disabled"}>Open Replay</button>
                                <button class="btn btn-sm btn-outline-success" id="il-export-zones" ${lab.selectedTestId ? "" : "disabled"}>Export Zones CSV</button>
                                <button class="btn btn-sm btn-outline-success" id="il-export-candidates" ${lab.selectedTestId ? "" : "disabled"}>Export Candidates</button>
                                <button class="btn btn-sm btn-outline-success" id="il-export-events" ${lab.selectedTestId ? "" : "disabled"}>Export Events CSV</button>
                                <button class="btn btn-sm btn-outline-success" id="il-export-results" ${lab.selectedCampaignId ? "" : "disabled"}>Export Results CSV</button>
                            </div>
                        </div>
                        <div class="row g-2 mb-3">
                            <div class="col-md-2">${select("il-zone-side", "Side", ["ALL", "BUY", "SELL"], lab.zoneFilters?.side || "ALL")}</div>
                            <div class="col-md-2">${select("il-zone-status", "Status", ["ALL", "PENDING", "ACTIVE", "TP", "SL", "BE", "CANCELED", "INVALIDATED"], lab.zoneFilters?.status || "ALL")}</div>
                            <div class="col-md-2">${field("il-zone-from", "Date From", lab.zoneFilters?.from || "", "date")}</div>
                            <div class="col-md-2">${field("il-zone-to", "Date To", lab.zoneFilters?.to || "", "date")}</div>
                            <div class="col-md-2">${field("il-zone-search", "Zone ID Search", lab.zoneFilters?.search || "")}</div>
                            <div class="col-md-2">${select("il-zone-flipped", "Flipped", ["ALL", "YES", "NO"], lab.zoneFilters?.flipped || "ALL")}</div>
                        </div>
                        <div class="table-responsive"><table class="table table-sm strategy-table">
                            <thead><tr><th>Zone ID</th><th>Side</th><th>Source Time</th><th>Created Time</th><th>Top</th><th>Bottom</th><th>Width</th><th>ATR Width</th><th>Limit</th><th>Activated</th><th>Entry</th><th>SL</th><th>TP</th><th>Terminal</th><th>Result R</th></tr></thead>
                            <tbody>${filteredIndicatorZones().map((z) => `<tr class="${num(z.id) === num(lab.selectedZoneId) ? "table-active" : ""}" data-il-zone="${esc(z.id)}"><td>${esc(z.engine_zone_id || z.id)}</td><td>${esc(zoneDisplaySide(z.side))}</td><td>${esc(z.source_time || "")}</td><td>${esc(z.created_time || "")}</td><td>${num(z.zone_top).toFixed(2)}</td><td>${num(z.zone_bottom).toFixed(2)}</td><td>${num(z.zone_width_price ?? z.zone_width).toFixed(2)}</td><td>${z.zone_width_atr === null || z.zone_width_atr === undefined ? "" : num(z.zone_width_atr).toFixed(3)}</td><td>${esc(z.limit_created_time || "")}</td><td>${esc(z.activated_time || "")}</td><td>${esc(z.entry_price ?? "")}</td><td>${esc(z.sl_price ?? "")}</td><td>${esc(z.tp_price ?? "")}</td><td>${esc(z.terminal_state || zoneDisplayStatus(z))}</td><td>${z.result_r === null || z.result_r === undefined ? "" : num(z.result_r).toFixed(2)}</td></tr>`).join("") || `<tr><td colspan="15" class="text-secondary">Select a result to inspect zones.</td></tr>`}</tbody>
                        </table></div>
                        <div class="mt-3">${renderZoneDetails()}</div>
                    </div>
                </div>
            </div>`;
        document.getElementById("il-refresh").onclick = () => fetchIndicatorLab();
        document.querySelectorAll("#section-indicator-lab input, #section-indicator-lab select").forEach((node) => {
            node.addEventListener("focus", () => markIndicatorLabInteraction());
            node.addEventListener("click", () => markIndicatorLabInteraction());
            node.addEventListener("keydown", () => markIndicatorLabInteraction());
            if (node.id === "il-indicator") return;
            node.addEventListener("input", () => captureIndicatorLabDraft({ markDirty: true }));
            node.addEventListener("change", () => captureIndicatorLabDraft({ markDirty: true }));
            node.addEventListener("blur", () => {
                window.setTimeout(() => {
                    if (!isIndicatorLabInputActive()) {
                        state.indicatorLabUi.pauseRefreshUntil = Date.now() + 3000;
                    }
                }, 0);
            });
        });
        document.getElementById("il-indicator").onchange = (event) => {
            const previousIndicator = lab.selectedIndicator || ensureIndicatorLabDraft().campaign.indicator || "LEBPRIME";
            captureIndicatorLabDraft({ markDirty: true, indicator: previousIndicator, campaignIndicator: event.target.value });
            lab.selectedIndicator = event.target.value;
            ensureIndicatorLabDraft().campaign.indicator = event.target.value;
            renderAll({ force: true });
        };
        document.getElementById("il-create-dataset").onclick = async () => {
            state.indicatorLabUi.error = "";
            try {
                const currentDraft = captureIndicatorLabDraft();
                await api("/api/indicator-lab/datasets/mt5", { method: "POST", body: JSON.stringify({ actor: "dashboard", name: currentDraft.dataset.name, symbol: currentDraft.dataset.symbol, timeframe: currentDraft.dataset.timeframe, start_date: currentDraft.dataset.start_date, end_date: currentDraft.dataset.end_date, notes: currentDraft.dataset.notes }) });
                state.indicatorLabUi.dirty = false;
                state.indicatorLabUi.pauseRefreshUntil = 0;
                await fetchIndicatorLab();
            } catch (error) {
                state.indicatorLabUi.error = error.message;
                renderAll({ force: true });
            }
        };
        document.getElementById("il-preview").onclick = async () => {
            state.indicatorLabUi.error = "";
            try {
                const payload = collectIndicatorCampaignPayload();
                const preview = await api("/api/indicator-lab/campaigns/preview", { method: "POST", body: JSON.stringify(payload) });
                safeSetText("il-preview-result", `Generated configurations: ${preview.preview.generated_configurations}`);
            } catch (error) {
                state.indicatorLabUi.error = error.message;
                renderAll({ force: true });
            }
        };
        document.getElementById("il-start").onclick = async () => {
            if (state.indicatorLabUi.submitting) return;
            const button = document.getElementById("il-start");
            state.indicatorLabUi.submitting = true;
            state.indicatorLabUi.message = "Creating campaign...";
            state.indicatorLabUi.error = "";
            if (button) {
                button.disabled = true;
                button.textContent = "Creating campaign...";
            }
            try {
                const created = await api("/api/indicator-lab/campaigns", { method: "POST", body: JSON.stringify(collectIndicatorCampaignPayload()) });
                const campaignId = indicatorLabCampaignId(created);
                if (campaignId) {
                    state.indicatorLab.selectedCampaignId = campaignId;
                    state.indicatorLab.selectedTestId = null;
                    state.indicatorLabUi.lastCreatedCampaignId = campaignId;
                    state.indicatorLabUi.message = `Campaign #${campaignId} created`;
                } else {
                    state.indicatorLabUi.message = "Campaign created";
                }
                state.indicatorLabUi.dirty = false;
                state.indicatorLabUi.pauseRefreshUntil = 0;
                await fetchIndicatorLab();
            } catch (error) {
                state.indicatorLabUi.error = error.message;
                state.indicatorLabUi.message = "";
                renderAll({ force: true });
            } finally {
                state.indicatorLabUi.submitting = false;
                const latestButton = document.getElementById("il-start");
                if (latestButton) {
                    latestButton.disabled = false;
                    latestButton.textContent = "Start Campaign";
                }
            }
        };
        ["il-zone-side", "il-zone-status", "il-zone-from", "il-zone-to", "il-zone-search", "il-zone-flipped"].forEach((id) => {
            const node = document.getElementById(id);
            if (!node) return;
            node.oninput = () => {
                lab.zoneFilters = {
                    side: val("il-zone-side") || "ALL",
                    status: val("il-zone-status") || "ALL",
                    from: val("il-zone-from"),
                    to: val("il-zone-to"),
                    search: val("il-zone-search"),
                    flipped: val("il-zone-flipped") || "ALL",
                };
                renderAll({ force: true });
            };
            node.onchange = node.oninput;
        });
        const prevZone = document.getElementById("il-zone-prev");
        if (prevZone) prevZone.onclick = () => selectAdjacentIndicatorZone(-1);
        const nextZone = document.getElementById("il-zone-next");
        if (nextZone) nextZone.onclick = () => selectAdjacentIndicatorZone(1);
        const openReplay = document.getElementById("il-open-replay");
        if (openReplay) openReplay.onclick = () => {
            if (lab.selectedTestId) window.open(`/api/indicator-lab/tests/${lab.selectedTestId}/replay`, "_blank");
        };
        const exportZones = document.getElementById("il-export-zones");
        if (exportZones) exportZones.onclick = () => {
            if (lab.selectedTestId) indicatorZoneDownload(`/api/indicator-lab/tests/${lab.selectedTestId}/zones/export.csv`);
        };
        const exportCandidates = document.getElementById("il-export-candidates");
        if (exportCandidates) exportCandidates.onclick = () => {
            if (lab.selectedTestId) indicatorZoneDownload(`/api/indicator-lab/tests/${lab.selectedTestId}/candidates/export.csv`);
        };
        const exportEvents = document.getElementById("il-export-events");
        if (exportEvents) exportEvents.onclick = () => {
            if (lab.selectedTestId) indicatorZoneDownload(`/api/indicator-lab/tests/${lab.selectedTestId}/events/export.csv`);
        };
        const exportResults = document.getElementById("il-export-results");
        if (exportResults) exportResults.onclick = () => {
            if (lab.selectedCampaignId) indicatorZoneDownload(`/api/indicator-lab/campaigns/${lab.selectedCampaignId}/results/export.csv`);
        };
        document.querySelectorAll("[data-il-zone]").forEach((row) => {
            row.onclick = () => loadIndicatorZone(num(row.dataset.ilZone));
        });
        document.querySelectorAll("[data-il-campaign]").forEach((button) => {
            button.onclick = async () => {
                lab.selectedCampaignId = num(button.dataset.ilCampaign);
                lab.selectedTestId = null;
                lab.zones = [];
                lab.selectedZoneId = null;
                lab.selectedZone = null;
                lab.zoneEvents = [];
                await fetchIndicatorLab();
            };
        });
        document.querySelectorAll("[data-il-test]").forEach((button) => {
            button.onclick = async () => {
                lab.selectedTestId = num(button.dataset.ilTest);
                const zones = await api(`/api/indicator-lab/tests/${lab.selectedTestId}/zones`);
                lab.zones = zones.zones || [];
                lab.selectedZoneId = null;
                lab.selectedZone = null;
                lab.zoneEvents = [];
                if (lab.zones[0]) await loadIndicatorZone(lab.zones[0].id, { silent: true });
                renderAll();
            };
        });
    }

    function activateSection(section) {
        const sectionName = textOr(section, "overview");
        if (state.activeSection === "strategies" && sectionName !== "strategies" && state.strategiesUi.dirty) {
            if (!window.confirm("You have unsaved LebPrime changes. Leave Strategies and discard them?")) return false;
            state.strategiesUi.dirty = false;
            state.strategiesUi.draft = null;
            syncLebprimeStrategyEditor();
        }
        const target = ensureContainer(`section-${sectionName}`);
        if (!target) {
            setFeedback(`Section ${sectionName} is missing from the dashboard template.`, true);
            return false;
        }
        state.activeSection = sectionName;
        document.querySelectorAll("#dashboard-tabs .nav-link").forEach((item) => item.classList.toggle("active", item.dataset.section === sectionName));
        document.querySelectorAll(".dashboard-section").forEach((node) => node.classList.add("d-none"));
        target.classList.remove("d-none");
        dashboardLog("tabs", "mounted section", { section: sectionName, container: target.id });
        return true;
    }

    function bindTabs() {
        document.querySelectorAll("#dashboard-tabs .nav-link").forEach((button) => {
            button.addEventListener("click", () => activateSection(button.dataset.section));
        });
        activateSection(state.activeSection);
    }

    function safeRenderSection(sectionId, label, renderer) {
        try {
            renderer();
            dashboardLog("render", `${label} mounted`, { container: sectionId });
        } catch (error) {
            dashboardLog("render", `${label} failed`, { error: error.message, container: sectionId }, "error");
            const container = ensureContainer(sectionId);
            if (container) {
                container.innerHTML = renderErrorState(`${label} Failed To Render`, error.message);
            }
        }
    }

    function renderAll(options = {}) {
        captureIndicatorLabDraft();
        const preserveIndicatorLabEditing = !options.force && indicatorLabInteractionLocked();
        captureUiState();
        renderTopStatusBar();
        renderQuickActions();
        safeRenderSection("section-overview", "Overview", renderOverview);
        safeRenderSection("section-strategies", "Strategies", renderStrategies);
        safeRenderSection("section-risk", "Risk Management", renderRisk);
        safeRenderSection("section-trades", "Trades", renderTrades);
        safeRenderSection("section-performance", "Performance", renderPerformance);
        if (!preserveIndicatorLabEditing) {
            safeRenderSection("section-indicator-lab", "Indicator Lab", renderIndicatorLab);
        }
        safeRenderSection("section-config", "Settings", renderConfig);
        // System tab: health, Telegram alerts and logs stacked on one page.
        safeRenderSection("section-health", "System Health", renderHealth);
        safeRenderSection("section-alerts", "Alerts", renderAlerts);
        safeRenderSection("section-logs", "Logs", renderLogs);
        restoreUiState();
        activateSection(state.activeSection);
        if (isReadonly()) {
            document.querySelectorAll("#section-strategies input, #section-strategies select, #section-strategies button, #section-risk input, #section-risk button, #section-alerts input, #section-alerts button, #section-trades input, #section-trades button, #section-performance input, #section-performance button, #section-indicator-lab input, #section-indicator-lab select, #section-indicator-lab button, #section-config input, #section-config button, #section-config textarea").forEach((node) => {
                if (node.id !== "dashboard-readonly-mode" && node.id !== "apply-runtime-control") {
                    node.disabled = true;
                }
            });
            document.querySelectorAll("#import-config-button").forEach((node) => { node.disabled = true; });
        }
    }

    async function boot() {
        bindTabs();
        window.addEventListener("beforeunload", (event) => {
            if (!state.strategiesUi.dirty) return;
            event.preventDefault();
            event.returnValue = "";
        });
        const loaders = [
            refreshStatus,
            refreshConfig,
            refreshPositions,
            refreshLogs,
            fetchJobs,
            fetchWorkerStatus,
            fetchIndicatorLab,
        ];
        for (const loader of loaders) {
            try {
                await loader();
            } catch (error) {
                dashboardLog("boot", `${loader.name} failed`, { error: error.message }, "error");
                setFeedback(`Dashboard loaded with partial data: ${error.message}`, true);
            }
        }
        const refreshSeconds = Math.max(2, num(state.config.dashboard?.refresh_seconds || 5, 5));
        setInterval(refreshStatus, refreshSeconds * 1000);
        setInterval(refreshPositions, Math.max(5, refreshSeconds) * 1000);
        setInterval(refreshLogs, 15000);
        setInterval(fetchJobs, 5000);
        setInterval(fetchWorkerStatus, 5000);
        setInterval(fetchIndicatorLab, 10000);
    }

    document.addEventListener("DOMContentLoaded", boot);
})();
