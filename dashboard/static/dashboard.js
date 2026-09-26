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
        backtestRuns: [],
        backtestResult: null,
        backtestDraft: {},
        backtestUi: { dirty: false, pauseRefreshUntil: 0 },
        backtestReplay: {
            selectedRunId: null,
            summary: null,
            frames: [],
            events: [],
            visibleFrames: [],
            visibleEvents: [],
            viewportStart: 0,
            viewportSize: 120,
            windowStart: 0,
            windowEnd: -1,
            currentBarIndex: 0,
            selectedBar: null,
            selectedEvent: null,
            runStatus: "IDLE",
            isPlaying: false,
            playbackSpeed: 1,
            mode: "replay",
            autoScroll: true,
            filters: {
                category: "all",
                showBlocked: true,
                showPending: true,
                showManagement: true,
                family: "",
                strategy: "",
                reason: "",
            },
            loading: false,
            error: null,
            sourceSummary: "",
            totalBars: 0,
            latestKnownBar: -1,
            streamCursor: -1,
            progress: {
                current: 0,
                total: 0,
                pct: 0,
                latestFrameCount: 0,
                latestEventCount: 0,
                lastUpdateAt: "",
                liveUpdatesOn: false,
                indeterminate: true,
            },
        },
        strategiesUi: { mounted: false, dirty: false, saving: false, draft: null, lastSaveRestartRequired: false },
        runtimeActionsUi: { mounted: false, pending: null },
        oosStatus: { run: null, history: [], active_run_id: null, state: "IDLE" },
        oosHistory: [],
        oosUi: { loading: false, error: null, historyError: null, selectedRunId: null },
        oosDraft: {},
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
        lebprimeTrades: { pending: [], open: [], closed: [], failed: [], summary: {} },
        lebprimePerformance: { summary: {}, by_status: [], breakdown: [], notes: [] },
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
    const normalizeOOSScenario = (row, index = 0) => {
        const record = asObject(row);
        const metadata = asObject(parseJsonValue(record.metadata_json, {}));
        const resolvedMode = normalizeModeData(metadata.resolved_execution_mode || { selected_mode: metadata.requested_execution_mode || record.engine_mode || "" });
        const metrics = asObject(metadata.metrics || metadata.summary || {});
        const signals = num(record.signals ?? record.signal_count ?? metrics.signals ?? metrics.signal_count, NaN);
        const trades = num(record.trades ?? record.trade_count ?? metrics.trades ?? metrics.trade_count, NaN);
        return {
            ...record,
            scenario_index: num(record.scenario_index, index + 1),
            scenario_label: textOr(record.scenario_label, `Scenario ${index + 1}`),
            status: textOr(record.status, "QUEUED").toUpperCase(),
            run_id: record.run_id ?? "",
            export_path: textOr(record.export_path || record.bundle_path || ""),
            report_path: textOr(record.report_path || ""),
            error: textOr(record.error || record.failure_reason || ""),
            metadata_json: metadata,
            resolved_execution_mode: resolvedMode,
            signals: Number.isFinite(signals) ? signals : null,
            trades: Number.isFinite(trades) ? trades : null,
        };
    };
    const normalizeOOSRun = (row) => {
        const record = asObject(row);
        if (!Object.keys(record).length) return null;
        const request = asObject(parseJsonValue(record.request_json, {}));
        const metadata = asObject(parseJsonValue(record.metadata_json, {}));
        const scenarios = asArray(record.scenarios).map((item, index) => normalizeOOSScenario(item, index));
        const rawProgress = asObject(record.progress);
        const total = num(rawProgress.total, num(record.progress_total, scenarios.length));
        const current = num(rawProgress.current, num(record.progress_current, 0));
        return {
            ...record,
            id: record.id ?? null,
            title: textOr(record.title, "Out-of-Sample Evaluation"),
            symbol: textOr(record.symbol || request.symbol, "XAUUSD"),
            timeframe: textOr(record.timeframe || request.timeframe, "M1"),
            start_date: textOr(record.start_date || request.start_date),
            end_date: textOr(record.end_date || request.end_date),
            state: textOr(record.state, "IDLE").toUpperCase(),
            report_status: textOr(record.report_status, record.report_path ? "completed" : "pending").toUpperCase(),
            report_path: textOr(record.report_path || ""),
            output_dir: textOr(record.output_dir || ""),
            request_json: request,
            metadata_json: metadata,
            scenarios,
            current_scenario: record.current_scenario ? normalizeOOSScenario(record.current_scenario, current) : null,
            progress: {
                current,
                total,
                pct: num(rawProgress.pct, total > 0 ? (current / total) * 100 : 0),
                completed_scenarios: num(rawProgress.completed_scenarios, scenarios.filter((item) => item.status === "COMPLETED").length),
                failed_scenarios: num(rawProgress.failed_scenarios, scenarios.filter((item) => item.status === "FAILED").length),
                interrupted_scenarios: num(rawProgress.interrupted_scenarios, scenarios.filter((item) => item.status === "INTERRUPTED").length),
            },
        };
    };
    const normalizeOOSPayload = (payload = {}) => {
        const record = asObject(payload);
        const run = normalizeOOSRun(record.run || record.current_run);
        const history = asArray(record.history || record.runs).map((item) => normalizeOOSRun(item)).filter(Boolean);
        return {
            ok: record.ok !== false,
            run,
            history,
            active_run_id: record.active_run_id ?? run?.id ?? null,
            state: textOr(record.state || run?.state, "IDLE").toUpperCase(),
        };
    };

    const replayState = () => state.backtestReplay || {};
    const replayIsVisible = () => state.activeSection === "research" && document.getElementById("research-replay")?.classList.contains("active");
    const replayEventTone = (type) => {
        const normalized = textOr(type, "UNKNOWN").toLowerCase();
        if (normalized.includes("blocked")) return "warning";
        if (normalized.includes("closed") || normalized.includes("filled")) return "danger";
        if (normalized.includes("opened") || normalized.includes("selected") || normalized.includes("detected")) return "success";
        if (normalized.includes("pending") || normalized.includes("stop") || normalized.includes("partial")) return "info";
        return "secondary";
    };
    const replayEventIcon = (type) => {
        const normalized = textOr(type, "event").toLowerCase();
        if (normalized.includes("trade_opened")) return "▲";
        if (normalized.includes("trade_closed")) return "▼";
        if (normalized.includes("trade_partial_close")) return "◐";
        if (normalized.includes("trade_stop_moved")) return "⤴";
        if (normalized.includes("pending")) return "○";
        if (normalized.includes("blocked")) return "!";
        if (normalized.includes("candidate_selected")) return "●";
        if (normalized.includes("candidate_rejected")) return "×";
        if (normalized.includes("candidate")) return "◌";
        if (normalized.includes("management")) return "◆";
        return "•";
    };
    const replayEventRawReason = (event) => {
        const payload = asObject(event?.payload);
        const diagnostics = asObject(payload.diagnostics);
        const candidates = [
            event?.reason_code,
            payload.reason_code,
            payload.blocked_reason,
            payload.rejection_reason,
            payload.failure_reason,
            payload.reason,
            diagnostics.reason_code,
            diagnostics.blocked_reason,
            diagnostics.rejection_reason,
            diagnostics.blocker_reason,
            diagnostics.reason,
        ];
        return candidates.map((item) => textOr(item)).find(Boolean) || "";
    };
    const replayEventCategory = (event) => {
        const type = textOr(event?.event_type, "").toLowerCase();
        const payload = asObject(event?.payload);
        const reason = replayEventRawReason(event).toLowerCase();
        const textBlob = `${type} ${reason} ${textOr(payload.reason)} ${textOr(payload.failure_reason)} ${textOr(payload.blocked_reason)} ${textOr(payload.rejection_reason)} ${JSON.stringify(payload.diagnostics || {})}`.toLowerCase();
        if (type.includes("trade_opened") || type.includes("pending_filled")) return "trade_open";
        if (type.includes("trade_closed")) return "trade_close";
        if (type.includes("pending_created") || type.includes("pending_expired")) return "pending";
        if (type.includes("trade_partial_close") || type.includes("trade_stop_moved") || type.includes("breakeven") || type.includes("management")) return "management_event";
        if (type.includes("candidate_rejected") || type.includes("signal_blocked") || textBlob.includes("blocked") || textBlob.includes("rejected")) return "blocked";
        if (type.includes("candidate_selected") || type.includes("signal_detected")) return "candidate_selected";
        if (type.includes("current_cursor")) return "current_cursor";
        if (type.includes("candidate") || type.includes("signal")) return "candidate_rejected";
        return "management_event";
    };
    const replayEventLabel = (event) => {
        const type = textOr(event?.event_type, "event").replace(/_/g, " ");
        const family = textOr(event?.setup_family, "");
        const strategy = textOr(event?.strategy_name, "");
        return `${type}${family ? ` | ${family}` : ""}${strategy ? ` | ${strategy}` : ""}`;
    };
    const replayEventReason = (event) => {
        const reason = replayEventRawReason(event);
        if (reason) return reason;
        const category = replayEventCategory(event);
        if (category === "blocked") return "unknown_block";
        if (category === "candidate_rejected") return "unknown_rejection";
        return "No reason";
    };
    const replayEventIsManagement = (event) => {
        const type = textOr(event?.event_type, "").toLowerCase();
        return type.includes("partial") || type.includes("stop_moved") || type.includes("breakeven") || type.includes("pending") || type.includes("filled") || type.includes("opened") || type.includes("closed");
    };
    const replayEventIsTrade = (event) => {
        const type = textOr(event?.event_type, "").toLowerCase();
        return type.includes("trade_opened") || type.includes("trade_closed") || type.includes("pending_filled") || type.includes("pending_created");
    };
    const replayEventShortReason = (event) => {
        const payload = asObject(event?.payload);
        const diagnostics = asObject(payload.diagnostics);
        return textOr(
            event?.reason_code
            || payload.reason_code
            || payload.blocked_reason
            || payload.rejection_reason
            || payload.failure_reason
            || payload.reason
            || diagnostics.reason_code
            || diagnostics.blocked_reason
            || diagnostics.rejection_reason
            || diagnostics.reason,
            replayEventReason(event),
        );
    };
    const normalizeReplayFrame = (row) => ({
        run_id: num(row?.run_id),
        bar_index: num(row?.bar_index),
        timestamp: textOr(row?.timestamp),
        open: num(row?.open),
        high: num(row?.high),
        low: num(row?.low),
        close: num(row?.close),
        volume: num(row?.volume),
        tick_volume: num(row?.tick_volume),
        spread: num(row?.spread),
        equity: num(row?.equity),
        balance: num(row?.balance),
        floating_pnl: num(row?.floating_pnl),
        open_positions: asArray(parseJsonValue(row?.open_positions_json ?? row?.open_positions, [])),
        pending_orders: asArray(parseJsonValue(row?.pending_orders_json ?? row?.pending_orders, [])),
        selected_candidate: asObject(parseJsonValue(row?.selected_candidate_json ?? row?.selected_candidate, {})),
        candidate_summary: asObject(parseJsonValue(row?.candidate_summary_json ?? row?.candidate_summary, {})),
        state: asObject(parseJsonValue(row?.state_json ?? row?.state, {})),
    });
    const normalizeReplayEvent = (row) => ({
        run_id: num(row?.run_id),
        bar_index: num(row?.bar_index),
        timestamp: textOr(row?.timestamp),
        event_type: textOr(row?.event_type, "UNKNOWN"),
        position_id: textOr(row?.position_id),
        signal_id: textOr(row?.signal_id),
        strategy_name: textOr(row?.strategy_name),
        setup_family: textOr(row?.setup_family),
        side: textOr(row?.side),
        price: num(row?.price),
        reason_code: textOr(row?.reason_code),
        payload: asObject(parseJsonValue(row?.payload ?? row?.event_payload_json, {})),
    });
    const normalizeReplaySummary = (payload = {}) => {
        const record = asObject(payload);
        const run = asObject(record.run);
        const summary = asObject(record.summary);
        const historyResolution = asObject(record.history_resolution || run.metadata_json?.history_resolution || {});
        return {
            run: {
                ...run,
                metadata_json: asObject(run.metadata_json),
                artifacts_json: asObject(run.artifacts_json),
                enabled_strategies_json: asArray(run.enabled_strategies_json),
                session_filter_json: asObject(run.session_filter_json),
                spread_model: asObject(run.spread_model),
                slippage_model: asObject(run.slippage_model),
            },
            summary,
            frames_count: num(record.frames_count),
            events_count: num(record.events_count),
            last_frame: record.last_frame ? normalizeReplayFrame(record.last_frame) : null,
            last_event: record.last_event ? normalizeReplayEvent(record.last_event) : null,
            playback_ready: !!record.playback_ready,
            history_resolution: historyResolution,
        };
    };
    const replayVisibleFrames = () => asArray(replayState().visibleFrames || []);
    const replayVisibleEvents = () => asArray(replayState().visibleEvents || []);
    const replayVisibleStrategies = () => Array.from(new Set(replayVisibleEvents().map((event) => textOr(event.strategy_name).toUpperCase()).filter(Boolean))).sort();
    const replayVisibleReasonCodes = () => Array.from(new Set(replayVisibleEvents().map((event) => textOr(replayEventReason(event)).toUpperCase()).filter(Boolean))).sort();
    const replayFilteredEvents = (events = replayVisibleEvents()) => {
        const filters = asObject(replayState().filters || {});
        const familyFilter = textOr(filters.family, "").toUpperCase();
        const strategyFilter = textOr(filters.strategy, "").toUpperCase();
        const reasonFilter = textOr(filters.reason, "").toUpperCase();
        const categoryFilter = textOr(filters.category, "all").toLowerCase();
        return asArray(events).filter((event) => {
            const category = replayEventCategory(event);
            const reason = textOr(replayEventReason(event)).toUpperCase();
            if (categoryFilter !== "all" && category !== categoryFilter) return false;
            if (!filters.showBlocked && (category === "blocked" || category === "candidate_rejected")) return false;
            if (!filters.showPending && category === "pending") return false;
            if (!filters.showManagement && category === "management_event") return false;
            if (familyFilter && textOr(event.setup_family, "").toUpperCase() !== familyFilter) return false;
            if (strategyFilter && textOr(event.strategy_name, "").toUpperCase() !== strategyFilter) return false;
            if (reasonFilter && reason !== reasonFilter) return false;
            return true;
        });
    };
    const replayGroupedEventsByBar = (events = replayFilteredEvents()) => {
        const groups = new Map();
        asArray(events).forEach((event) => {
            const key = num(event.bar_index);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(event);
        });
        return groups;
    };
    const replayEventsAtBar = (barIndex, events = replayFilteredEvents()) => asArray(events).filter((event) => num(event.bar_index) === num(barIndex));
    const replayTradeFocusEvents = (events = replayFilteredEvents()) => asArray(events).filter((event) => ["trade_open", "trade_close", "pending", "management_event"].includes(replayEventCategory(event)));
    const replayBlockedFocusEvents = (events = replayFilteredEvents()) => asArray(events).filter((event) => ["blocked", "candidate_rejected"].includes(replayEventCategory(event)));
    const replayBlockedClusters = (events = replayBlockedFocusEvents()) => {
        const sorted = asArray(events).slice().sort((a, b) => (num(a.bar_index) - num(b.bar_index)) || textOr(a.timestamp).localeCompare(textOr(b.timestamp)));
        const clusters = [];
        let current = [];
        let lastBar = null;
        sorted.forEach((event) => {
            const bar = num(event.bar_index);
            if (lastBar === null || bar - lastBar <= 5) {
                current.push(event);
            } else {
                clusters.push(current);
                current = [event];
            }
            lastBar = bar;
        });
        if (current.length) clusters.push(current);
        return clusters;
    };
    const replaySelectedEvent = () => replayState().selectedEvent || null;
    const replaySelectedBarIndex = () => {
        const selected = replayState().selectedBar;
        if (Number.isFinite(Number(selected))) return Number(selected);
        return num(replayState().currentBarIndex, 0);
    };
    const replayFrameByBar = (barIndex) => replayVisibleFrames().find((frame) => num(frame.bar_index) === num(barIndex)) || null;
    const replayLatestFrame = () => {
        const frames = replayVisibleFrames();
        return frames.length ? frames[frames.length - 1] : null;
    };
    const replayTradeEventTypes = new Set(["trade_opened", "trade_closed", "trade_partial_close", "trade_stop_moved", "pending_created", "pending_filled", "pending_expired"]);
    const replayStatusTone = (status) => stateTone(status);
    function stopReplayTimers() {
        const replay = replayState();
        if (replay.playTimer) {
            window.clearInterval(replay.playTimer);
            replay.playTimer = null;
        }
        if (replay.pollTimer) {
            window.clearInterval(replay.pollTimer);
            replay.pollTimer = null;
        }
    }
    function replaySetMessage(message, error = false) {
        const node = safeSetText("bt-replay-status", message);
        if (node) node.className = error ? "small text-danger" : "small text-secondary";
    }
    function replaySerializeCandidate(candidate) {
        if (!candidate || typeof candidate !== "object") return {};
        return {
            setup_family: textOr(candidate.setup_family),
            side: textOr(candidate.side),
            setup_score: num(candidate.setup_score),
            trend_score: num(candidate.trend_score),
            session_quality_score: num(candidate.session_quality_score),
            regime_confidence: num(candidate.regime_confidence),
            setup_fingerprint: textOr(candidate.setup_fingerprint),
            reason_code: textOr(candidate.reason_code),
        };
    }
    function replaySetSelection(updates = {}) {
        const replay = replayState();
        Object.assign(replay, updates);
    }
    function replayWindowBoundsAround(barIndex) {
        const replay = replayState();
        const total = Math.max(num(replay.totalBars, 0), replayVisibleFrames().length);
        const safeIndex = Math.max(0, num(barIndex, 0));
        const half = Math.floor(BACKTEST_REPLAY_WINDOW_SIZE / 2);
        let start = Math.max(0, safeIndex - half);
        let end = Math.max(start, start + BACKTEST_REPLAY_WINDOW_SIZE - 1);
        if (total > 0 && end >= total) {
            end = total - 1;
            start = Math.max(0, end - BACKTEST_REPLAY_WINDOW_SIZE + 1);
        }
        return { start, end };
    }
    function replayViewportBounds() {
        const frames = replayVisibleFrames();
        const replay = replayState();
        if (!frames.length) return { start: 0, end: -1, frames: [] };
        const maxSize = Math.max(24, Math.min(frames.length, BACKTEST_REPLAY_WINDOW_SIZE));
        const size = Math.max(24, Math.min(num(replay.viewportSize, 120), maxSize));
        let start = Math.max(0, num(replay.viewportStart, 0));
        if (start + size > frames.length) {
            start = Math.max(0, frames.length - size);
        }
        const end = Math.max(start, Math.min(frames.length - 1, start + size - 1));
        return { start, end, frames: frames.slice(start, end + 1), size };
    }
    function replayFrameIndexByBar(barIndex) {
        return replayVisibleFrames().findIndex((frame) => num(frame.bar_index) === num(barIndex));
    }
    function replayViewportContainsBar(barIndex) {
        const idx = replayFrameIndexByBar(barIndex);
        const replay = replayState();
        return idx >= replay.viewportStart && idx <= replay.viewportStart + num(replay.viewportSize, 120) - 1;
    }
    function replaySetViewport(startIndex, size = null, { clampToCurrent = true } = {}) {
        const replay = replayState();
        const frames = replayVisibleFrames();
        if (!frames.length) return;
        const maxSize = Math.max(24, frames.length);
        const nextSize = Math.max(24, Math.min(num(size ?? replay.viewportSize, 120), maxSize));
        let nextStart = Math.max(0, num(startIndex, 0));
        if (clampToCurrent && nextStart + nextSize > frames.length) {
            nextStart = Math.max(0, frames.length - nextSize);
        }
        replay.viewportStart = nextStart;
        replay.viewportSize = nextSize;
    }
    function replayCenterViewportOnBar(barIndex, size = null) {
        const idx = replayFrameIndexByBar(barIndex);
        const frames = replayVisibleFrames();
        if (idx < 0 || !frames.length) return;
        const nextSize = Math.max(24, Math.min(num(size ?? replayState().viewportSize, 120), frames.length));
        const nextStart = Math.max(0, Math.min(idx - Math.floor(nextSize / 2), frames.length - nextSize));
        replaySetViewport(nextStart, nextSize, { clampToCurrent: false });
    }
    function replayZoomViewport(direction = 1) {
        const replay = replayState();
        const frames = replayVisibleFrames();
        if (!frames.length) return;
        const current = Math.max(24, Math.min(num(replay.viewportSize, 120), frames.length));
        const nextSize = direction < 0 ? Math.max(24, Math.floor(current * 0.75)) : Math.min(frames.length, Math.ceil(current * 1.35));
        replayCenterViewportOnBar(replaySelectedBarIndex(), nextSize);
        scheduleReplayRender();
    }
    function replayPanViewport(offsetBars = 0) {
        const replay = replayState();
        const frames = replayVisibleFrames();
        if (!frames.length) return;
        const nextStart = Math.max(0, Math.min(num(replay.viewportStart, 0) + num(offsetBars, 0), Math.max(0, frames.length - num(replay.viewportSize, 120))));
        replaySetViewport(nextStart, replay.viewportSize, { clampToCurrent: false });
        scheduleReplayRender();
    }
    function replayAutoScrollToBar(barIndex) {
        const replay = replayState();
        if (!replay.autoScroll) return;
        const idx = replayFrameIndexByBar(barIndex);
        if (idx < 0) return;
        const size = Math.max(24, Math.min(num(replay.viewportSize, 120), replayVisibleFrames().length));
        const nextStart = Math.max(0, Math.min(idx - Math.floor(size / 2), Math.max(0, replayVisibleFrames().length - size)));
        replaySetViewport(nextStart, size, { clampToCurrent: false });
    }
    function replayApplyWindow(frames = [], events = [], options = {}) {
        const replay = replayState();
        const mergedFrames = [...replay.frames];
        const mergedEvents = [...replay.events];
        const frameMap = new Map(mergedFrames.map((frame) => [num(frame.bar_index), frame]));
        asArray(frames).forEach((frame) => { frameMap.set(num(frame.bar_index), normalizeReplayFrame(frame)); });
        const eventKeys = new Set(mergedEvents.map((event) => `${num(event.bar_index)}:${textOr(event.event_type)}:${textOr(event.position_id)}:${textOr(event.signal_id)}:${textOr(event.reason_code)}`));
        asArray(events).forEach((event) => {
            const normalized = normalizeReplayEvent(event);
            const key = `${normalized.bar_index}:${normalized.event_type}:${normalized.position_id}:${normalized.signal_id}:${normalized.reason_code}`;
            if (!eventKeys.has(key)) {
                mergedEvents.push(normalized);
                eventKeys.add(key);
            }
        });
        replay.frames = Array.from(frameMap.values()).sort((a, b) => a.bar_index - b.bar_index);
        replay.events = mergedEvents.sort((a, b) => (a.bar_index - b.bar_index) || a.timestamp.localeCompare(b.timestamp));
        if (options.windowStart !== undefined) replay.windowStart = num(options.windowStart);
        if (options.windowEnd !== undefined) replay.windowEnd = num(options.windowEnd);
        if (options.totalBars !== undefined) replay.totalBars = num(options.totalBars);
        if (options.runStatus !== undefined) replay.runStatus = textOr(options.runStatus, "IDLE");
        if (options.sourceSummary !== undefined) replay.sourceSummary = textOr(options.sourceSummary);
        if (options.latestKnownBar !== undefined) replay.latestKnownBar = num(options.latestKnownBar);
        if (options.streamCursor !== undefined) replay.streamCursor = num(options.streamCursor);
        replay.visibleFrames = replay.frames.filter((frame) => frame.bar_index >= replay.windowStart && frame.bar_index <= replay.windowEnd);
        replay.visibleEvents = replay.events.filter((event) => event.bar_index >= replay.windowStart && event.bar_index <= replay.windowEnd);
        if (!Number.isFinite(Number(replay.viewportSize)) || replay.viewportSize <= 0) {
            replay.viewportSize = Math.min(BACKTEST_REPLAY_WINDOW_SIZE, Math.max(24, replay.visibleFrames.length || BACKTEST_REPLAY_WINDOW_SIZE));
        }
        if (!Number.isFinite(Number(replay.viewportStart)) || replay.viewportStart < 0) {
            replay.viewportStart = 0;
        }
        if (replay.autoScroll && options.streamCursor !== undefined && replay.visibleFrames.length) {
            replayCenterViewportOnBar(replay.currentBarIndex || options.streamCursor, replay.viewportSize);
        } else if (replay.viewportStart + replay.viewportSize > replay.visibleFrames.length) {
            replaySetViewport(replay.viewportStart, replay.viewportSize);
        }
        if (replay.visibleFrames.length) {
            if (!Number.isFinite(Number(replay.currentBarIndex)) || replay.currentBarIndex < replay.windowStart) {
                replay.currentBarIndex = replay.visibleFrames[0].bar_index;
            }
            replay.selectedBar = replay.selectedBar === null ? replay.currentBarIndex : replay.selectedBar;
        }
        if (replay.frames.length > BACKTEST_REPLAY_MAX_CACHE) {
            const trimmed = replay.frames.slice(-BACKTEST_REPLAY_MAX_CACHE);
            replay.frames = trimmed;
            const minIndex = trimmed[0]?.bar_index ?? 0;
            replay.events = replay.events.filter((event) => event.bar_index >= minIndex);
        }
        replay.progress = {
            ...replay.progress,
            latestFrameCount: Math.max(num(replay.progress.latestFrameCount, 0), replay.frames.length),
            latestEventCount: Math.max(num(replay.progress.latestEventCount, 0), replay.events.length),
        };
    }
    function replayCursorFrame() {
        const replay = replayState();
        const index = replaySelectedBarIndex();
        return replayFrameByBar(index) || replayLatestFrame() || null;
    }
    function replayEnsureEventSelection(event) {
        const replay = replayState();
        replay.selectedEvent = event || null;
        if (event) {
            replay.selectedBar = num(event.bar_index);
            replay.currentBarIndex = num(event.bar_index);
        }
    }
    function replaySetBar(barIndex, { event = null, center = false } = {}) {
        const replay = replayState();
        const resolved = Math.max(0, num(barIndex, 0));
        replay.currentBarIndex = resolved;
        replay.selectedBar = resolved;
        if (event) replay.selectedEvent = event;
        if (center || replay.autoScroll) {
            replayCenterViewportOnBar(resolved);
        }
        const nearLeft = resolved <= replay.windowStart + BACKTEST_REPLAY_PREFETCH;
        const nearRight = resolved >= replay.windowEnd - BACKTEST_REPLAY_PREFETCH;
        if (center || nearLeft || nearRight) {
            replayEnsureWindowForBar(resolved, { preferCenter: center });
        }
        scheduleReplayRender();
    }
    async function replayFetchWindow(startBar, endBar) {
        const replay = replayState();
        if (!replay.selectedRunId) return;
        const response = unwrapApiResult(await api(`/api/backtests/${replay.selectedRunId}/replay/window?start_bar=${encodeURIComponent(startBar)}&end_bar=${encodeURIComponent(endBar)}`));
        replayApplyWindow(response.frames || [], response.events || [], {
            windowStart: startBar,
            windowEnd: endBar,
            totalBars: replay.totalBars || replay.frames.length || 0,
        });
    }
    async function replayFetchSummary(runId) {
        const response = unwrapApiResult(await api(`/api/backtests/${runId}/replay/summary`));
        const summary = normalizeReplaySummary(response);
        const replay = replayState();
        replay.summary = summary;
        replay.runStatus = textOr(summary.run.state, "UNKNOWN");
        replay.totalBars = num(summary.frames_count);
        replay.progress = {
            current: num(summary.run.progress_current, 0),
            total: num(summary.run.progress_total, summary.frames_count),
            pct: num(summary.run.progress_total, 0) > 0 ? (num(summary.run.progress_current, 0) / num(summary.run.progress_total, 1)) * 100 : 0,
            latestFrameCount: num(summary.frames_count, 0),
            latestEventCount: num(summary.events_count, 0),
            lastUpdateAt: textOr(summary.run.updated_at || summary.run.last_heartbeat_at || summary.run.created_at || ""),
            liveUpdatesOn: textOr(summary.run.state, "").toUpperCase() === "RUNNING",
            indeterminate: num(summary.run.progress_total, 0) <= 0,
        };
        replay.sourceSummary = [
            textOr(summary.history_resolution?.source_kind, "unknown").toUpperCase(),
            ...Object.keys(summary.history_resolution?.timeframes || {}),
        ].filter(Boolean).join(" | ");
        if (summary.run?.id) replay.selectedRunId = num(summary.run.id);
        if (summary.run?.state) replay.runStatus = textOr(summary.run.state, "UNKNOWN");
        return summary;
    }
    async function replayLoadRun(runId, options = {}) {
        const replay = replayState();
        const targetRunId = num(runId, 0);
        if (!targetRunId) return;
        const token = Date.now() + Math.random();
        replay.loadToken = token;
        replay.loading = true;
        replay.error = null;
        replay.isPlaying = false;
        replay.selectedRunId = targetRunId;
        replay.selectedEvent = null;
        replay.selectedBar = null;
        replay.currentBarIndex = 0;
        replay.frames = [];
        replay.events = [];
        replay.visibleFrames = [];
        replay.visibleEvents = [];
        replay.windowStart = 0;
        replay.windowEnd = -1;
        replay.latestKnownBar = -1;
        replay.streamCursor = -1;
        stopReplayTimers();
        replaySetMessage(`Loading replay run #${targetRunId}...`);
        scheduleReplayRender();
        try {
            const summary = await replayFetchSummary(targetRunId);
            if (replay.loadToken !== token) return;
            const runState = textOr(summary.run?.state, "UNKNOWN").toUpperCase();
            const totalBars = Math.max(num(summary.frames_count), 0);
            const initialEnd = Math.min(totalBars > 0 ? totalBars - 1 : BACKTEST_REPLAY_WINDOW_SIZE - 1, BACKTEST_REPLAY_WINDOW_SIZE - 1);
            const initialStart = 0;
            if (runState === "RUNNING" || runState === "QUEUED") {
                replay.mode = "live";
                const latestBar = Math.max(0, num(summary.last_frame?.bar_index, totalBars > 0 ? totalBars - 1 : 0));
                replay.currentBarIndex = latestBar;
                await replayFetchStreamWindow(targetRunId, Math.max(-1, latestBar - BACKTEST_REPLAY_WINDOW_SIZE), token);
                startReplayPolling();
            } else {
                replay.mode = "replay";
                await replayFetchWindow(initialStart, Math.max(initialEnd, initialStart));
                replay.currentBarIndex = replay.visibleFrames[0]?.bar_index ?? 0;
            }
            replay.summary = summary;
            replay.runStatus = runState;
            replay.loading = false;
            if (!options.silent) {
                state.activeSection = "research";
                activateSection("research");
                document.querySelector('[data-bs-target="#research-replay"]')?.click();
            }
            replaySetMessage(`Loaded replay run #${targetRunId} (${runState.toLowerCase()}).`);
            scheduleReplayRender();
        } catch (error) {
            replay.loading = false;
            replay.error = error.message;
            replaySetMessage(error.message, true);
            scheduleReplayRender();
        }
    }
    async function replayFetchStreamWindow(runId, afterBar, token = null) {
        const response = unwrapApiResult(await api(`/api/backtests/${runId}/stream/window?after_bar=${encodeURIComponent(afterBar)}`));
        const replay = replayState();
        if (token && replay.loadToken !== token) return;
        const windowStart = replay.frames.length ? replay.windowStart : num(response.frames?.[0]?.bar_index, replay.windowStart >= 0 ? replay.windowStart : 0);
        const windowEnd = Math.max(replay.windowEnd, num(response.cursor, afterBar));
        const status = replay.progress || {};
        replay.progress = {
            ...status,
            latestFrameCount: Math.max(num(status.latestFrameCount), asArray(response.frames).length + num(status.latestFrameCount, 0)),
            latestEventCount: Math.max(num(status.latestEventCount), asArray(response.events).length + num(status.latestEventCount, 0)),
            lastUpdateAt: new Date().toISOString(),
            liveUpdatesOn: true,
        };
        replayApplyWindow(response.frames || [], response.events || [], {
            windowStart,
            windowEnd,
            latestKnownBar: num(response.cursor, afterBar),
            streamCursor: num(response.cursor, afterBar),
        });
        if (num(response.cursor, afterBar) >= 0) {
            replay.currentBarIndex = Math.max(replay.currentBarIndex, num(response.cursor, afterBar));
        }
    }
    async function replayPollLive() {
        const replay = replayState();
        if (!replay.selectedRunId || replay.runStatus !== "RUNNING") {
            stopReplayTimers();
            return;
        }
        try {
            const status = unwrapApiResult(await api(`/api/backtests/${replay.selectedRunId}/stream/status`));
            const nextState = textOr(status.state || status.run?.state, replay.runStatus).toUpperCase();
            replay.runStatus = nextState;
            replay.totalBars = Math.max(num(status.progress_total), replay.totalBars);
            replay.latestKnownBar = Math.max(num(status.latest_frame?.bar_index, replay.latestKnownBar), replay.latestKnownBar);
            replay.progress = {
                current: num(status.progress_current, replay.progress.current),
                total: num(status.progress_total, replay.progress.total),
                pct: num(status.progress_total, 0) > 0 ? (num(status.progress_current, 0) / num(status.progress_total, 1)) * 100 : replay.progress.pct,
                latestFrameCount: Math.max(num(status.latest_frame?.bar_index, -1) + 1, replay.progress.latestFrameCount),
                latestEventCount: Math.max(num(status.latest_event?.bar_index, -1) + 1, replay.progress.latestEventCount),
                lastUpdateAt: new Date().toISOString(),
                liveUpdatesOn: nextState === "QUEUED" || nextState === "RUNNING",
                indeterminate: num(status.progress_total, 0) <= 0,
            };
            if (num(status.latest_frame?.bar_index) >= 0) {
                await replayFetchStreamWindow(replay.selectedRunId, replay.streamCursor >= 0 ? replay.streamCursor : num(status.latest_frame.bar_index) - 1);
            }
            if (nextState === "COMPLETED" || nextState === "FAILED" || nextState === "INTERRUPTED" || nextState === "PARTIAL") {
                stopReplayTimers();
                replay.mode = "replay";
            }
            if (!replay.isPlaying && replay.visibleFrames.length) {
                replay.currentBarIndex = replay.latestKnownBar >= 0 ? replay.latestKnownBar : replay.currentBarIndex;
            }
            scheduleReplayRender();
        } catch (error) {
            replay.error = error.message;
            replaySetMessage(`Live polling failed: ${error.message}`, true);
        }
    }
    function startReplayPolling() {
        const replay = replayState();
        if (!replay.selectedRunId) return;
        if (replay.pollTimer) return;
        replay.pollTimer = window.setInterval(replayPollLive, 900);
    }
    function replayAdvance(step = 1) {
        const replay = replayState();
        const current = replaySelectedBarIndex();
        const next = Math.max(0, current + step);
        replaySetBar(next, { center: false });
        replayAutoScrollToBar(next);
        const visible = replayVisibleFrames();
        if (visible.length && next > visible[visible.length - 1].bar_index - BACKTEST_REPLAY_PREFETCH) {
            const futureStart = visible[visible.length - 1].bar_index + 1;
            const futureEnd = futureStart + BACKTEST_REPLAY_WINDOW_SIZE - 1;
            replayFetchWindow(futureStart, futureEnd).catch((error) => {
                replay.error = error.message;
            });
        }
    }
    function replayStepBackward() {
        replayAdvance(-1);
    }
    function replayStepForward() {
        replayAdvance(1);
    }
    function replayJumpToTrade(direction = 1) {
        const replay = replayState();
        const trades = replayTradeFocusEvents();
        if (!trades.length) return;
        const current = replaySelectedBarIndex();
        const candidates = direction > 0 ? trades.filter((event) => event.bar_index > current) : trades.filter((event) => event.bar_index < current);
        const selected = direction > 0 ? candidates[0] : candidates[candidates.length - 1];
        if (selected) {
            replayEnsureEventSelection(selected);
            replaySetBar(selected.bar_index, { event: selected, center: true });
        }
    }
    function replayJumpToTimestamp(inputValue) {
        const ts = textOr(inputValue);
        if (!ts) return;
        const frame = replayVisibleFrames().find((item) => item.timestamp.startsWith(ts)) || replayVisibleFrames().find((item) => item.timestamp === ts);
        if (frame) replaySetBar(frame.bar_index, { center: true });
    }
    function replayJumpToBar(barIndex) {
        const target = num(barIndex, NaN);
        if (!Number.isFinite(target)) return;
        replaySetBar(target, { center: true });
    }
    function replayTogglePlay() {
        const replay = replayState();
        if (replay.isPlaying) {
            replay.isPlaying = false;
            stopReplayTimers();
            scheduleReplayRender();
            return;
        }
        replay.isPlaying = true;
        const intervalMs = BACKTEST_REPLAY_SPEED_MAP[replay.playbackSpeed] || BACKTEST_REPLAY_SPEED_MAP[1];
        stopReplayTimers();
        replay.playTimer = window.setInterval(() => {
            const current = replaySelectedBarIndex();
            const next = current + 1;
            const limit = replay.visibleFrames.length ? replay.visibleFrames[replay.visibleFrames.length - 1].bar_index : current;
            if (next > limit && replay.runStatus === "RUNNING") {
                replayAdvance(1);
                return;
            }
            if (next > limit && replay.runStatus !== "RUNNING") {
                replay.isPlaying = false;
                stopReplayTimers();
                scheduleReplayRender();
                return;
            }
            replayAdvance(1);
        }, intervalMs);
        scheduleReplayRender();
    }
    function replayPause() {
        const replay = replayState();
        replay.isPlaying = false;
        stopReplayTimers();
        scheduleReplayRender();
    }
    function replaySetSpeed(speed) {
        const replay = replayState();
        replay.playbackSpeed = BACKTEST_REPLAY_SPEEDS.includes(num(speed)) ? num(speed) : 1;
        if (replay.isPlaying) {
            stopReplayTimers();
            const intervalMs = BACKTEST_REPLAY_SPEED_MAP[replay.playbackSpeed] || BACKTEST_REPLAY_SPEED_MAP[1];
            replay.playTimer = window.setInterval(() => {
                const current = replaySelectedBarIndex();
                const next = current + 1;
                const limit = replay.visibleFrames.length ? replay.visibleFrames[replay.visibleFrames.length - 1].bar_index : current;
                if (next > limit && replay.runStatus !== "RUNNING") {
                    replay.isPlaying = false;
                    stopReplayTimers();
                    scheduleReplayRender();
                    return;
                }
                replayAdvance(1);
            }, intervalMs);
        }
        scheduleReplayRender();
    }
    function replayCurrentCandidate(frame) {
        return asObject(frame?.selected_candidate || {});
    }
    const replayMarkerIconByCategory = {
        trade_open: "▲",
        trade_close: "▼",
        pending: "○",
        blocked: "!",
        candidate_rejected: "×",
        candidate_selected: "●",
        management_event: "◆",
        current_cursor: "│",
    };
    const replayMarkerToneByCategory = {
        trade_open: "success",
        trade_close: "danger",
        pending: "info",
        blocked: "warning",
        candidate_rejected: "secondary",
        candidate_selected: "primary",
        management_event: "light",
        current_cursor: "secondary",
    };
    function replayMarkersForFrame(frame) {
        const frameBar = num(frame.bar_index);
        const barEvents = replayEventsAtBar(frameBar);
        const groups = new Map();
        barEvents.forEach((event) => {
            const category = replayEventCategory(event);
            const key = category;
            if (!groups.has(key)) {
                groups.set(key, []);
            }
            groups.get(key).push(event);
        });
        const order = ["trade_open", "trade_close", "pending", "blocked", "candidate_rejected", "candidate_selected", "management_event"];
        return order.flatMap((category) => {
            const items = groups.get(category) || [];
            if (!items.length) return [];
            const primary = items[0];
            return [{
                category,
                items,
                event: primary,
                count: items.length,
                tone: replayMarkerToneByCategory[category] || replayEventTone(primary.event_type),
            }];
        });
    }
    function replayDrawCanvas() {
        const canvas = document.getElementById("bt-replay-canvas");
        if (!canvas) return;
        const wrap = canvas.parentElement;
        if (!wrap) return;
        const chart = replayViewportBounds();
        const frames = chart.frames;
        const replay = replayState();
        const selectedBar = replaySelectedBarIndex();
        const dpr = window.devicePixelRatio || 1;
        const rect = wrap.getBoundingClientRect();
        const width = Math.max(320, Math.floor(rect.width));
        const height = Math.max(320, Math.floor(rect.height));
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(height * dpr);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "rgba(7,11,18,0.95)";
        ctx.fillRect(0, 0, width, height);
        if (!frames.length) {
            ctx.fillStyle = "rgba(255,255,255,0.7)";
            ctx.font = "14px Inter, Arial, sans-serif";
            ctx.fillText("No replay bars loaded.", 20, 32);
            return;
        }
        const padding = { left: 58, right: 20, top: 18, bottom: 30 };
        const chartWidth = Math.max(1, width - padding.left - padding.right);
        const chartHeight = Math.max(1, height - padding.top - padding.bottom);
        const highs = frames.map((frame) => num(frame.high));
        const lows = frames.map((frame) => num(frame.low));
        let maxPrice = Math.max(...highs);
        let minPrice = Math.min(...lows);
        if (maxPrice === minPrice) {
            maxPrice += 1;
            minPrice -= 1;
        }
        const range = maxPrice - minPrice;
        const toY = (price) => padding.top + (chartHeight * (maxPrice - price)) / range;
        const barSpacing = chartWidth / Math.max(frames.length, 1);
        const bodyWidth = Math.max(2, Math.min(12, barSpacing * 0.62));
        const centerX = (index) => padding.left + (index + 0.5) * barSpacing;
        const xByBar = new Map(frames.map((frame, index) => [num(frame.bar_index), centerX(index)]));

        ctx.strokeStyle = "rgba(255,255,255,0.08)";
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i += 1) {
            const y = padding.top + (chartHeight * i) / 4;
            ctx.beginPath();
            ctx.moveTo(padding.left, y);
            ctx.lineTo(width - padding.right, y);
            ctx.stroke();
        }
        ctx.fillStyle = "rgba(255,255,255,0.65)";
        ctx.font = "11px Consolas, monospace";
        ctx.fillText(minPrice.toFixed(2), 6, height - padding.bottom);
        ctx.fillText(maxPrice.toFixed(2), 6, padding.top + 10);

        const overlay = document.getElementById("bt-replay-overlay");
        const markers = [];
        frames.forEach((frame, index) => {
            const x = centerX(index);
            const openY = toY(num(frame.open));
            const highY = toY(num(frame.high));
            const lowY = toY(num(frame.low));
            const closeY = toY(num(frame.close));
            const up = num(frame.close) >= num(frame.open);
            ctx.strokeStyle = up ? "rgba(34,197,94,0.95)" : "rgba(248,113,113,0.95)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, highY);
            ctx.lineTo(x, lowY);
            ctx.stroke();
            ctx.fillStyle = up ? "rgba(34,197,94,0.8)" : "rgba(248,113,113,0.8)";
            const top = Math.min(openY, closeY);
            const bodyHeight = Math.max(1, Math.abs(closeY - openY));
            ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
            if (num(frame.bar_index) === selectedBar) {
                ctx.strokeStyle = "rgba(110,168,254,0.95)";
                ctx.lineWidth = 2;
                ctx.strokeRect(x - bodyWidth - 4, Math.min(highY, lowY), bodyWidth * 2 + 8, Math.abs(lowY - highY) || 8);
            }
            const markerEvents = replayMarkersForFrame(frame);
            const sortedMarkers = markerEvents.slice(0, 4);
            sortedMarkers.forEach((marker, markerIndex) => {
                const directionOffset = marker.category === "trade_close" ? -1 : 1;
                const yBase = marker.category === "trade_close" ? highY - 10 : lowY + 10;
                const y = yBase + (markerIndex * 13 * directionOffset);
                markers.push({
                    ...marker,
                    x,
                    y,
                    bar_index: num(frame.bar_index),
                });
            });
            if (markerEvents.length > 4) {
                markers.push({
                    category: "management_event",
                    items: markerEvents.flatMap((item) => item.items).slice(0, 10),
                    event: markerEvents[0].event,
                    count: markerEvents.length,
                    x,
                    y: lowY + 42,
                    bar_index: num(frame.bar_index),
                    overflow: true,
                    tone: "secondary",
                });
            }
        });
        ctx.strokeStyle = "rgba(255, 193, 7, 0.8)";
        ctx.lineWidth = 1.5;
        const currentX = xByBar.get(selectedBar);
        if (currentX) {
            ctx.setLineDash([6, 6]);
            ctx.beginPath();
            ctx.moveTo(currentX, padding.top);
            ctx.lineTo(currentX, height - padding.bottom);
            ctx.stroke();
            ctx.setLineDash([]);
        }
        const selectedFrame = replayCursorFrame();
        if (selectedFrame) {
            const candidate = replayCurrentCandidate(selectedFrame);
            const lines = [
                { price: candidate.entry_price || candidate.pending_entry_price || candidate.value_price, color: "#6ea8fe" },
                { price: candidate.stop_loss || candidate.sl, color: "#dc3545" },
                { price: candidate.tp1 || candidate.take_profit, color: "#198754" },
                { price: candidate.tp2, color: "#0dcaf0" },
            ];
            lines.forEach((line) => {
                const price = num(line.price, NaN);
                if (!Number.isFinite(price) || price <= 0) return;
                const y = toY(price);
                ctx.strokeStyle = line.color;
                ctx.setLineDash([6, 4]);
                ctx.beginPath();
                ctx.moveTo(padding.left, y);
                ctx.lineTo(width - padding.right, y);
                ctx.stroke();
                ctx.setLineDash([]);
            });
        }
        const tickY = height - 10;
        frames.slice(0, 8).forEach((frame, index) => {
            const x = centerX(index);
            ctx.fillStyle = "rgba(255,255,255,0.45)";
            ctx.fillText(textOr(frame.timestamp).slice(11, 19), x - 24, tickY);
        });
        if (overlay) {
            overlay.innerHTML = markers.slice(0, 80).map((marker, index) => {
                const category = marker.category;
                const tone = replayMarkerToneByCategory[category] || "secondary";
                const icon = replayMarkerIconByCategory[category] || replayEventIcon(marker.event.event_type);
                const classes = [
                    "replay-marker",
                    `replay-marker--${category}`,
                    marker.count > 1 ? "replay-marker--stacked" : "",
                    category === "blocked" || category === "candidate_rejected" ? "replay-marker--muted" : "",
                ].filter(Boolean).join(" ");
                const itemReasons = asArray(marker.items).map((item) => replayEventLabel(item) + " | " + replayEventReason(item)).join("\n");
                const title = `${replayEventLabel(marker.event)} | ${replayEventReason(marker.event)} | ${marker.event.timestamp}\n${itemReasons}`;
                const badgeCount = marker.count > 1 ? `<span class="replay-marker__count">${marker.count}</span>` : "";
                const style = `left:${marker.x}px;top:${marker.y}px;`;
                return `<button class="${classes}" data-replay-marker="${index}" data-bar-index="${marker.bar_index}" title="${esc(title)}" style="${style}"><span class="replay-marker__icon">${esc(icon)}</span>${badgeCount}</button>`;
            }).join("");
            overlay.querySelectorAll("[data-replay-marker]").forEach((button) => {
                button.onclick = () => {
                    const markerIndex = Number(button.dataset.replayMarker);
                    const marker = markers[markerIndex];
                    if (!marker) return;
                    replayEnsureEventSelection(marker.event);
                    replaySetBar(marker.bar_index, { event: marker.event, center: true });
                };
            });
        }
    }
    function replayDrawNavigator() {
        const canvas = document.getElementById("bt-replay-navigator");
        if (!canvas) return;
        const wrap = canvas.parentElement;
        if (!wrap) return;
        const frames = replayVisibleFrames();
        const replay = replayState();
        const dpr = window.devicePixelRatio || 1;
        const rect = wrap.getBoundingClientRect();
        const width = Math.max(320, Math.floor(rect.width));
        const height = Math.max(54, Math.floor(rect.height));
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(height * dpr);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "rgba(7,11,18,0.95)";
        ctx.fillRect(0, 0, width, height);
        if (!frames.length) {
            ctx.fillStyle = "rgba(255,255,255,0.55)";
            ctx.font = "12px Consolas, monospace";
            ctx.fillText("Navigator unavailable", 20, 24);
            return;
        }
        const lows = frames.map((frame) => num(frame.low));
        const highs = frames.map((frame) => num(frame.high));
        let maxPrice = Math.max(...highs);
        let minPrice = Math.min(...lows);
        if (maxPrice === minPrice) {
            maxPrice += 1;
            minPrice -= 1;
        }
        const range = maxPrice - minPrice;
        const toY = (price) => 8 + ((height - 16) * (maxPrice - price)) / range;
        const step = width / Math.max(frames.length, 1);
        frames.forEach((frame, index) => {
            const x = (index + 0.5) * step;
            const closeY = toY(num(frame.close));
            const openY = toY(num(frame.open));
            ctx.strokeStyle = num(frame.close) >= num(frame.open) ? "rgba(34,197,94,0.7)" : "rgba(248,113,113,0.7)";
            ctx.beginPath();
            ctx.moveTo(x, toY(num(frame.high)));
            ctx.lineTo(x, toY(num(frame.low)));
            ctx.stroke();
            ctx.fillStyle = num(frame.close) >= num(frame.open) ? "rgba(34,197,94,0.55)" : "rgba(248,113,113,0.55)";
            ctx.fillRect(x - Math.max(1, step * 0.25), Math.min(openY, closeY), Math.max(2, step * 0.5), Math.max(1, Math.abs(closeY - openY)));
        });
        const viewport = replayViewportBounds();
        const start = viewport.start;
        const end = viewport.end;
        const left = start * step;
        const right = (end + 1) * step;
        ctx.fillStyle = "rgba(110,168,254,0.16)";
        ctx.fillRect(left, 0, Math.max(2, right - left), height);
        ctx.strokeStyle = "rgba(110,168,254,0.9)";
        ctx.setLineDash([4, 4]);
        ctx.strokeRect(left, 1, Math.max(2, right - left), height - 2);
        ctx.setLineDash([]);
        const cursorIndex = replayFrameIndexByBar(replaySelectedBarIndex());
        if (cursorIndex >= 0) {
            const x = (cursorIndex + 0.5) * step;
            ctx.strokeStyle = "rgba(255, 193, 7, 0.95)";
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, height);
            ctx.stroke();
        }
    }
    function replayBarIndexFromCanvasX(canvas, clientX) {
        const chart = replayViewportBounds();
        const frames = chart.frames;
        if (!frames.length || !canvas) return null;
        const rect = canvas.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(rect.width, 1)));
        const index = Math.max(0, Math.min(frames.length - 1, Math.floor(ratio * frames.length)));
        return frames[index]?.bar_index ?? null;
    }
    function replayViewportIndexFromNavigatorX(canvas, clientX) {
        const frames = replayVisibleFrames();
        if (!frames.length || !canvas) return null;
        const rect = canvas.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(rect.width, 1)));
        return Math.max(0, Math.min(frames.length - 1, Math.floor(ratio * frames.length)));
    }
    function replayRenderInspector() {
        const inspector = document.getElementById("bt-replay-inspector");
        if (!inspector) return;
        const replay = replayState();
        const frame = replayCursorFrame();
        const event = replaySelectedEvent();
        const candidate = replayCurrentCandidate(frame);
        const selectedFamily = textOr(event?.setup_family || candidate.setup_family || frame?.candidate_summary?.selected_family, "N/A");
        const tradeStatus = textOr(event?.event_type, "").toLowerCase().includes("open")
            ? "OPEN"
            : textOr(event?.event_type, "").toLowerCase().includes("close")
                ? "CLOSED"
                : textOr(event?.event_type, "").toLowerCase().includes("pending")
                    ? "PENDING"
                    : "UNKNOWN";
        const diagnostics = asObject(candidate?.diagnostics || frame?.state?.diagnostics || event?.payload?.diagnostics || {});
        const tradeLevels = {
            entry: num(candidate.entry_price || candidate.pending_entry_price || candidate.value_price || event?.price || frame?.close || 0, NaN),
            sl: num(candidate.stop_loss || candidate.sl || event?.payload?.sl, NaN),
            tp1: num(candidate.tp1 || candidate.take_profit || event?.payload?.tp1, NaN),
            tp2: num(candidate.tp2 || event?.payload?.tp2, NaN),
        };
        inspector.innerHTML = `
            <div class="dashboard-summary-card mb-3">
                <div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
                    <div>
                        <div class="small text-secondary">Cursor</div>
                        <div class="fw-semibold">${esc(frame?.timestamp || event?.timestamp || "N/A")}</div>
                    </div>
                    <div>${badge(textOr(replay.runStatus, "UNKNOWN"), replayStatusTone(replay.runStatus))}</div>
                </div>
                <div class="small text-secondary mt-2">Bar ${esc(replaySelectedBarIndex())} | Family ${esc(selectedFamily)} | Side ${esc(textOr(event?.side || candidate.side || "N/A"))}</div>
            </div>
            <div class="dashboard-summary-card mb-3">
                <div class="small text-uppercase text-secondary fw-semibold mb-2">Selected Event</div>
                ${event ? `
                    <div class="fw-semibold">${esc(replayEventLabel(event))}</div>
                    <div class="d-flex flex-wrap gap-2 mt-2">
                        ${badge(textOr(event.event_type, "event"), replayEventTone(event.event_type))}
                        ${badge(textOr(event.side || candidate.side || "N/A"), "dark")}
                        ${badge(textOr(replayEventReason(event)), "warning")}
                    </div>
                    <div class="row g-2 small mt-2">
                        <div class="col-6">Timestamp: ${esc(event.timestamp)}</div>
                        <div class="col-6">Bar: ${esc(event.bar_index)}</div>
                        <div class="col-6">Strategy: ${esc(event.strategy_name || "N/A")}</div>
                        <div class="col-6">Family: ${esc(event.setup_family || "N/A")}</div>
                        <div class="col-6">Price: ${esc(event.price || "")}</div>
                        <div class="col-6">Position: ${esc(event.position_id || "N/A")}</div>
                        <div class="col-12">Reason code: ${esc(event.reason_code || replayEventReason(event))}</div>
                    </div>
                    <div class="small text-secondary mt-2">Diagnostics summary</div>
                    <div class="small">${esc(textOr(diagnostics.reason || diagnostics.reason_code || diagnostics.blocked_reason || diagnostics.rejection_reason || "None"))}</div>
                    <details class="mt-2">
                        <summary class="small text-secondary">Raw event payload</summary>
                        <pre class="mb-0 mt-2">${esc(JSON.stringify(event.payload || {}, null, 2))}</pre>
                    </details>
                ` : `<div class="replay-muted">Select an event marker or feed row to inspect details.</div>`}
            </div>
            <div class="dashboard-summary-card">
                <div class="small text-uppercase text-secondary fw-semibold mb-2">Frame Snapshot</div>
                ${frame ? `
                    <div class="row g-2 small">
                        <div class="col-6">Open: ${esc(frame.open)}</div>
                        <div class="col-6">High: ${esc(frame.high)}</div>
                        <div class="col-6">Low: ${esc(frame.low)}</div>
                        <div class="col-6">Close: ${esc(frame.close)}</div>
                        <div class="col-6">Equity: ${esc(frame.equity)}</div>
                        <div class="col-6">Balance: ${esc(frame.balance)}</div>
                        <div class="col-6">Floating PnL: ${esc(frame.floating_pnl)}</div>
                        <div class="col-6">Spread: ${esc(frame.spread)}</div>
                        <div class="col-6">Tick Vol: ${esc(frame.tick_volume)}</div>
                        <div class="col-6">Volume: ${esc(frame.volume)}</div>
                    </div>
                    <div class="row g-2 small mt-2">
                        <div class="col-6">Entry: ${esc(Number.isFinite(tradeLevels.entry) ? tradeLevels.entry : "N/A")}</div>
                        <div class="col-6">SL: ${esc(Number.isFinite(tradeLevels.sl) ? tradeLevels.sl : "N/A")}</div>
                        <div class="col-6">TP1: ${esc(Number.isFinite(tradeLevels.tp1) ? tradeLevels.tp1 : "N/A")}</div>
                        <div class="col-6">TP2: ${esc(Number.isFinite(tradeLevels.tp2) ? tradeLevels.tp2 : "N/A")}</div>
                    </div>
                    <div class="small text-secondary mt-2">Selected candidate</div>
                    <details open class="mb-0">
                        <summary class="small text-secondary">Candidate / diagnostics</summary>
                        <div class="small mt-2">Category: ${esc(replayEventCategory(event || { event_type: "candidate_selected", payload: candidate }))}</div>
                        <div class="small">Side: ${esc(textOr(candidate.side || event?.side || "N/A"))}</div>
                        <div class="small">Score: ${esc(candidate.setup_score || frame.candidate_summary?.selected_score || "N/A")}</div>
                        <div class="small">Reason: ${esc(replayEventReason(event || { reason_code: frame.candidate_summary?.decision_reason_code, payload: candidate }))}</div>
                        <pre class="mb-0 mt-2">${esc(JSON.stringify(frame.selected_candidate || candidate || {}, null, 2))}</pre>
                    </details>
                ` : `<div class="replay-muted">No frame selected.</div>`}
            </div>
            <div class="dashboard-summary-card mt-3">
                <div class="small text-uppercase text-secondary fw-semibold mb-2">Trade Focus</div>
                <div class="small">Status: ${esc(tradeStatus)}</div>
                <div class="small">Selection: ${esc(textOr(event?.strategy_name || candidate.strategy_name || "N/A"))} / ${esc(selectedFamily)}</div>
            </div>
        `;
    }
    function replayRenderFeed() {
        const feed = document.getElementById("bt-replay-feed");
        if (!feed) return;
        const replay = replayState();
        const events = replayFilteredEvents();
        const selected = replaySelectedEvent();
        const chipDefs = [
            ["all", "All"],
            ["trade_open", "Trades"],
            ["trade_close", "Close"],
            ["blocked", "Blocked"],
            ["pending", "Pending"],
            ["management_event", "Management"],
            ["candidate_rejected", "No Candidate"],
        ];
        const categories = Array.from(new Set(replayVisibleEvents().map((event) => replayEventCategory(event)))).sort();
        const reasons = replayVisibleReasonCodes();
        const strategies = replayVisibleStrategies();
        const families = replayVisibleFamilies();
        const grouped = replayBlockedClusters(replayBlockedFocusEvents(events)).slice(0, 50);
        feed.innerHTML = `
            <div class="d-flex flex-wrap gap-2 mb-3">
                ${chipDefs.map(([value, label]) => `<button class="btn btn-sm ${textOr(replay.filters.category, "all") === value ? "btn-info" : "btn-outline-light"} replay-filter-chip" data-replay-category="${esc(value)}">${esc(label)}</button>`).join("")}
            </div>
            <div class="row g-2 mb-3">
                <div class="col-12"><select class="form-select form-select-sm" id="bt-replay-feed-strategy-filter"><option value="">All strategies</option>${strategies.map((strategy) => `<option value="${esc(strategy)}" ${textOr(replay.filters.strategy).toUpperCase() === strategy ? "selected" : ""}>${esc(strategy)}</option>`).join("")}</select></div>
                <div class="col-12"><select class="form-select form-select-sm" id="bt-replay-feed-reason-filter"><option value="">All reasons</option>${reasons.map((reason) => `<option value="${esc(reason)}" ${textOr(replay.filters.reason).toUpperCase() === reason ? "selected" : ""}>${esc(reason)}</option>`).join("")}</select></div>
                <div class="col-12"><select class="form-select form-select-sm" id="bt-replay-feed-family-filter"><option value="">All families</option>${families.map((family) => `<option value="${esc(family)}" ${textOr(replay.filters.family).toUpperCase() === family ? "selected" : ""}>${esc(formatFamilyName(family))}</option>`).join("")}</select></div>
            </div>
            <div class="replay-feed-meta small text-secondary mb-2">${esc(events.length)} visible events | ${esc(categories.length)} categories</div>
            ${events.length ? events.map((event) => {
                const eventCategory = replayEventCategory(event);
                const isSelected = event.bar_index === replaySelectedBarIndex() && selected?.event_type === event.event_type && selected?.timestamp === event.timestamp;
                const title = replayEventLabel(event);
                const reason = replayEventReason(event);
                const price = Number.isFinite(Number(event.price)) && Number(event.price) > 0 ? Number(event.price).toFixed(2) : "";
                return `
                    <div class="replay-feed-row ${isSelected ? "is-selected" : ""}" data-replay-event="${esc(`${event.bar_index}:${event.event_type}:${event.timestamp}`)}">
                        <div class="d-flex justify-content-between align-items-start gap-2">
                            <div class="flex-grow-1">
                                <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
                                    <span class="replay-event-icon replay-event-icon--${esc(eventCategory)}">${esc(replayEventIcon(event.event_type))}</span>
                                    <div class="replay-event-type">${esc(title)}</div>
                                    ${badge(textOr(event.event_type).replace(/_/g, " "), replayEventTone(event.event_type))}
                                </div>
                                <div class="small text-secondary">${esc(event.timestamp)} | Bar ${esc(event.bar_index)}${price ? ` | ${esc(price)}` : ""}</div>
                                <div class="small">${esc(textOr(event.strategy_name || "N/A"))} / ${esc(textOr(event.setup_family || "N/A"))} / ${esc(textOr(event.side || "N/A"))}</div>
                                <div class="small text-warning">${esc(reason)}</div>
                            </div>
                            <div class="text-end">
                                <div class="badge text-bg-dark">${esc(eventCategory.replace(/_/g, " "))}</div>
                                <div class="small text-secondary mt-1">#${esc(event.bar_index)}</div>
                            </div>
                        </div>
                    </div>
                `;
            }).join("") : `<div class="dashboard-empty-state"><div class="dashboard-empty-state__title">No events in window</div><div class="dashboard-empty-state__body">Move the chart cursor or widen the replay window. If the run has frames but no events, the chart still remains usable.</div></div>`}
            ${grouped.length ? `<div class="small text-secondary mt-3">Blocked clusters</div>${grouped.map((cluster, index) => `<button class="btn btn-sm btn-outline-warning w-100 mt-2 replay-blocked-cluster" data-replay-blocked-cluster="${index}">${esc(cluster.length)} event${cluster.length === 1 ? "" : "s"} at bars ${esc(cluster[0].bar_index)}-${esc(cluster[cluster.length - 1].bar_index)}</button>`).join("")}` : ""}
        `;
        feed.querySelectorAll("[data-replay-event]").forEach((row) => {
            row.onclick = () => {
                const text = String(row.dataset.replayEvent || "");
                const event = events.find((item) => `${item.bar_index}:${item.event_type}:${item.timestamp}` === text);
                if (!event) return;
                replayEnsureEventSelection(event);
                replaySetBar(event.bar_index, { event, center: true });
                scheduleReplayRender();
            };
        });
        feed.querySelectorAll("[data-replay-category]").forEach((button) => {
            button.onclick = () => {
                replay.filters.category = textOr(button.dataset.replayCategory, "all").toLowerCase();
                scheduleReplayRender();
            };
        });
        const strategyNode = document.getElementById("bt-replay-feed-strategy-filter");
        if (strategyNode) strategyNode.onchange = () => { replay.filters.strategy = textOr(strategyNode.value).toUpperCase(); scheduleReplayRender(); };
        const reasonNode = document.getElementById("bt-replay-feed-reason-filter");
        if (reasonNode) reasonNode.onchange = () => { replay.filters.reason = textOr(reasonNode.value).toUpperCase(); scheduleReplayRender(); };
        const familyNode = document.getElementById("bt-replay-feed-family-filter");
        if (familyNode) familyNode.onchange = () => { replay.filters.family = textOr(familyNode.value).toUpperCase(); scheduleReplayRender(); };
        feed.querySelectorAll("[data-replay-blocked-cluster]").forEach((row) => {
            row.onclick = () => {
                const cluster = grouped[num(row.dataset.replayBlockedCluster)];
                if (!cluster || !cluster.length) return;
                const event = cluster[0];
                replayEnsureEventSelection(event);
                replaySetBar(event.bar_index, { event, center: true });
                scheduleReplayRender();
            };
        });
    }
    function replayRenderControls() {
        const replay = replayState();
        const run = asObject(replay.summary?.run || {});
        const runs = (state.backtestRuns || []).slice();
        const selector = document.getElementById("bt-replay-run-select");
        if (selector) {
            selector.innerHTML = `<option value="">Select a run...</option>${runs.map((row) => `<option value="${esc(row.id)}" ${num(row.id) === num(replay.selectedRunId) ? "selected" : ""}>#${esc(row.id)} | ${esc(row.symbol || "")} | ${esc(row.timeframe || "")} | ${esc(row.state || row.status || "")}</option>`).join("")}`;
        }
        const familySelector = document.getElementById("bt-replay-window-family-filter");
        if (familySelector) {
            const families = Array.from(new Set(replayVisibleEvents().map((event) => textOr(event.setup_family).toUpperCase()).filter(Boolean)));
            familySelector.innerHTML = `<option value="">All families</option>${families.map((family) => `<option value="${esc(family)}" ${textOr(replay.filters.family).toUpperCase() === family ? "selected" : ""}>${esc(formatFamilyName(family))}</option>`).join("")}`;
        }
        safeSetText("bt-replay-current-bar", `${replaySelectedBarIndex()}`);
        safeSetText("bt-replay-source-chip", replay.sourceSummary || "Unknown source");
        safeSetText("bt-replay-status-chip", `${textOr(replay.runStatus, "UNKNOWN")} | ${replay.mode.toUpperCase()}`);
        safeSetText("bt-replay-window-chip", `${replay.windowStart} - ${replay.windowEnd}`);
        safeSetText("bt-replay-play", replay.isPlaying ? "Pause" : "Play / Pause");
        safeSetText("bt-replay-status", replay.loading ? "Loading replay data..." : replay.error ? `Error: ${replay.error}` : (replay.isPlaying ? `Playing at x${replay.playbackSpeed}` : `Ready. ${replay.mode === "live" ? "Live polling active." : "Replay mode."}`));
        const progressNode = document.querySelector(".replay-progress-bar .progress-bar");
        if (progressNode) {
            const pct = replay.progress.indeterminate ? 100 : Math.max(0, Math.min(100, num(replay.progress.pct, 0)));
            progressNode.style.width = `${pct}%`;
            progressNode.classList.toggle("progress-bar-striped", !!replay.progress.indeterminate || replay.progress.liveUpdatesOn);
            progressNode.classList.toggle("progress-bar-animated", !!replay.progress.indeterminate || replay.progress.liveUpdatesOn);
        }
        const autoScrollNode = document.getElementById("bt-replay-auto-scroll");
        if (autoScrollNode) autoScrollNode.checked = !!replay.autoScroll;
        const zoomLabel = document.querySelector("#bt-replay-status-chip");
        if (zoomLabel && replay.progress.indeterminate) {
            zoomLabel.title = "Progress total is unknown; frames/events may still continue to append.";
        }
    }
    function scheduleReplayRender() {
        if (!replayIsVisible()) return;
        window.requestAnimationFrame(() => {
            replayRenderControls();
            replayDrawCanvas();
            replayRenderFeed();
            replayRenderInspector();
        });
    }
    async function replayEnsureWindowForBar(barIndex, { preferCenter = false } = {}) {
        const replay = replayState();
        const visible = replayVisibleFrames();
        if (!visible.length) return;
        const target = num(barIndex, 0);
        if (target < replay.windowStart + BACKTEST_REPLAY_PREFETCH || target > replay.windowEnd - BACKTEST_REPLAY_PREFETCH) {
            if (replay.runStatus === "RUNNING") {
                await replayFetchStreamWindow(replay.selectedRunId, replay.streamCursor >= 0 ? replay.streamCursor : replay.windowEnd);
            } else {
                const bounds = preferCenter ? replayWindowBoundsAround(target) : { start: Math.max(0, replay.windowEnd + 1), end: replay.windowEnd + BACKTEST_REPLAY_WINDOW_SIZE };
                if (bounds.end >= bounds.start) {
                    await replayFetchWindow(bounds.start, bounds.end);
                }
            }
        }
    }
    function replayJumpToNextVisibleTrade() {
        replayJumpToTrade(1);
    }
    function replayJumpToPreviousVisibleTrade() {
        replayJumpToTrade(-1);
    }
    function replayJumpToBlockedCluster(direction = 1) {
        const clusters = replayBlockedClusters();
        if (!clusters.length) return;
        const current = replaySelectedBarIndex();
        const candidates = direction > 0
            ? clusters.filter((cluster) => cluster[0]?.bar_index > current)
            : clusters.filter((cluster) => cluster[0]?.bar_index < current);
        const cluster = direction > 0 ? candidates[0] : candidates[candidates.length - 1];
        if (!cluster || !cluster.length) return;
        const event = cluster[0];
        replayEnsureEventSelection(event);
        replaySetBar(event.bar_index, { event, center: true });
        scheduleReplayRender();
    }
    function replayApplyKeyboardShortcuts(event) {
        if (!replayIsVisible()) return;
        const target = event.target;
        if (target && typeof target.closest === "function" && target.closest("input, textarea, select, button")) return;
        if (event.code === "Space") {
            event.preventDefault();
            replayTogglePlay();
            return;
        }
        if (event.code === "ArrowRight") {
            event.preventDefault();
            replayStepForward();
            return;
        }
        if (event.code === "ArrowLeft") {
            event.preventDefault();
            replayStepBackward();
            return;
        }
        if (event.code === "KeyN") {
            event.preventDefault();
            replayJumpToNextVisibleTrade();
            return;
        }
        if (event.code === "KeyP") {
            event.preventDefault();
            replayJumpToPreviousVisibleTrade();
        }
    }
    function replayVisibleFamilies() {
        return Array.from(new Set(replayVisibleEvents().map((event) => textOr(event.setup_family).toUpperCase()).filter(Boolean))).sort();
    }
    function isBacktestInputActive() {
        const active = document.activeElement;
        return !!(active && typeof active.closest === "function" && active.closest("#section-backtest"));
    }

    const backtestInteractionLocked = () => (
        isBacktestInputActive()
        || Date.now() < Number(state.backtestUi?.pauseRefreshUntil || 0)
        || !!state.backtestUi?.dirty
    );

    function markBacktestInteraction() {
        state.backtestUi.pauseRefreshUntil = Date.now() + 30000;
    }

    function syncBacktestDraft(strategies = []) {
        state.backtestDraft = {
            symbol: val("bt-symbol"),
            timeframe: val("bt-timeframe"),
            start: val("bt-start"),
            end: val("bt-end"),
            initial_balance: val("bt-initial-balance"),
            risk_percent: val("bt-risk-percent"),
            spread_points: val("bt-spread"),
            slippage_points: val("bt-slippage"),
            execution_model: val("bt-execution-model"),
            enabled_strategies: strategies.filter((name) => on(`bt-strategy-${name}`)),
        };
        state.backtestUi.dirty = true;
        markBacktestInteraction();
    }

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
        document.getElementById("section-config-core").innerHTML = `
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
        const control = asObject(state.status.control);
        const runtimeControl = asObject(control.runtime);
        const worker = currentWorkerStatus();
        const lebprime = asObject(state.lebprimeLive);
        const trades = asObject(state.lebprimeTrades.summary);
        const contexts = asArray(lebprime.contexts);
        const unhealthyContexts = contexts.filter((row) => !["HEALTHY", "IDLE"].includes(textOr(row.status).toUpperCase()));
        document.getElementById("section-overview").innerHTML = `
            <div class="row g-3 mb-3">
                <div class="col-6 col-lg-3">${metric("LebPrime", lebprime.started ? "RUNNING" : "STOPPED", !lebprime.started)}</div>
                <div class="col-6 col-lg-3">${metric("Execution", lebprime.execution_mode || "DRY", lebprime.execution_mode === "LIVE")}</div>
                <div class="col-6 col-lg-3">${metric("MT5 Trade Allowed", runtimeControl.trade_allowed ? "YES" : "NO", !runtimeControl.trade_allowed)}</div>
                <div class="col-6 col-lg-3">${metric("Worker", workerStateLabel(worker), workerStateLabel(worker) !== "RUNNING")}</div>
                <div class="col-6 col-lg-3">${metric("Contexts", contexts.length.toFixed(0), unhealthyContexts.length > 0)}</div>
                <div class="col-6 col-lg-3">${metric("Pending Orders", num(trades.pending).toFixed(0))}</div>
                <div class="col-6 col-lg-3">${metric("Open Entries", num(trades.open).toFixed(0))}</div>
                <div class="col-6 col-lg-3">${metric("Failed Orders", num(trades.failed).toFixed(0), num(trades.failed) > 0)}</div>
            </div>
            <div class="row g-3">
                <div class="col-12 col-xl-5"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Operational State</h2>
                    <div>LebPrime heartbeat: ${esc(lebprime.service_heartbeat || "N/A")}</div>
                    <div>LebPrime PID: ${esc(lebprime.service_pid || "N/A")}</div>
                    <div>Startup block: ${esc(lebprime.startup_block_reason || "none")}</div>
                    <div>MT5 account: ${esc(runtimeControl.account_login || "N/A")}</div>
                    <div>Runtime heartbeat: ${esc(runtimeControl.heartbeat_at || "N/A")}</div>
                    <div>Execution paused: ${control.execution_paused ? "Yes" : "No"}</div>
                    <div>Kill switch: ${control.kill_switch ? "Enabled" : "Disabled"}</div>
                    <div>New entries: ${control.signal_generation_enabled === false ? "Disabled" : "Enabled"}</div>
                </div></div></div>
                <div class="col-12 col-xl-7"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">LebPrime Contexts</h2>
                    <div class="table-responsive"><table class="table table-sm"><thead><tr><th>Symbol</th><th>Timeframe</th><th>Status</th><th>Last Scan</th><th>Zones</th><th>Pending</th><th>Active</th><th>Error</th></tr></thead><tbody>${contexts.map((row) => `<tr><td>${esc(row.logical_symbol)}</td><td>${esc(row.timeframe)}</td><td>${badge(row.status || "IDLE", row.status === "HEALTHY" ? "success" : row.status === "ERROR" ? "danger" : "secondary")}</td><td>${esc(row.last_scan_time || "-")}</td><td>${esc(row.live_zones ?? 0)}</td><td>${esc(row.pending_limits ?? 0)}</td><td>${esc(row.active_trades ?? 0)}</td><td>${esc(row.last_error || "-")}</td></tr>`).join("") || `<tr><td colspan="8" class="text-secondary">LebPrime contexts will appear after runtime initialization.</td></tr>`}</tbody></table></div>
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
            xau_spread_points: String(draft.max_spread_points_by_symbol?.XAUUSD ?? ""),
            us30_spread_points: String(draft.max_spread_points_by_symbol?.US30 ?? ""),
            nas100_spread_points: String(draft.max_spread_points_by_symbol?.NAS100 ?? ""),
            gold_buffer: String(draft.parameters?.gold_buffer ?? ""),
            us30_buffer: String(draft.parameters?.us30_buffer ?? ""),
            nas_buffer: String(draft.parameters?.nas_buffer ?? ""),
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
                XAUUSD: val("lebprime-spread-xau"),
                US30: val("lebprime-spread-us30"),
                NAS100: val("lebprime-spread-nas100"),
            },
            parameters: {
                ...baseline.parameters,
                gold_buffer: val("lebprime-gold-buffer"),
                us30_buffer: val("lebprime-us30-buffer"),
                nas_buffer: val("lebprime-nas-buffer"),
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
            ["XAUUSD Max Spread Points", draft.max_spread_points_by_symbol.XAUUSD],
            ["US30 Max Spread Points", draft.max_spread_points_by_symbol.US30],
            ["NAS100 Max Spread Points", draft.max_spread_points_by_symbol.NAS100],
            ["Gold Buffer", draft.parameters.gold_buffer],
            ["US30 Buffer", draft.parameters.us30_buffer],
            ["NAS100 Buffer", draft.parameters.nas_buffer],
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
                        <div class="col-md-4"><div class="form-label small text-secondary">Symbols</div><div class="d-flex flex-wrap gap-3">${["XAUUSD", "US30", "NAS100"].map((symbol) => `<label class="form-check-label"><input class="form-check-input me-1" type="checkbox" data-lebprime-symbol="${symbol}">${symbol}</label>`).join("")}</div></div>
                        <div class="col-md-4"><div class="form-label small text-secondary">Timeframes</div><div class="d-flex flex-wrap gap-3">${["M1", "M3", "M5", "M15", "M30", "M45"].map((timeframe) => `<label class="form-check-label"><input class="form-check-input me-1" type="checkbox" data-lebprime-timeframe="${timeframe}">${timeframe}</label>`).join("")}</div></div>
                    </div>
                    <div class="row g-3 mb-3">
                        <div class="col-6 col-lg-2">${field("lebprime-bars", "Bars To Fetch", "", "number", "1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-spread", "Max Spread Points", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-spread-xau", "XAUUSD Spread Max", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-spread-us30", "US30 Spread Max", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-spread-nas100", "NAS100 Spread Max", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-gold-buffer", "Gold Buffer", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-us30-buffer", "US30 Buffer", "", "number", "0.1")}</div>
                        <div class="col-6 col-lg-2">${field("lebprime-nas-buffer", "NAS100 Buffer", "", "number", "0.1")}</div>
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
<div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Context</th><th>Zone ID</th><th>Entry</th><th>Order Type</th><th>Price</th><th>Placed/Fitted Volume</th><th>Requested Volume</th><th>Minimum Volume</th><th>Required Margin</th><th>Free Margin</th><th>Projected Margin Level</th><th>Required Margin Level</th><th>SL</th><th>TP</th><th>MT5 Ticket</th><th>Status</th><th>Attempts</th><th>Retryable</th><th>Next Retry</th><th>Exact Failure</th></tr></thead><tbody id="lebprime-broker-entry-body"></tbody></table></div>
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
        document.getElementById("lebprime-spread-xau").value = draft.max_spread_points_by_symbol.XAUUSD ?? draft.max_spread_points;
        document.getElementById("lebprime-spread-us30").value = draft.max_spread_points_by_symbol.US30 ?? draft.max_spread_points;
        document.getElementById("lebprime-spread-nas100").value = draft.max_spread_points_by_symbol.NAS100 ?? draft.max_spread_points;
        document.getElementById("lebprime-gold-buffer").value = num(draft.parameters.gold_buffer, 1);
        document.getElementById("lebprime-us30-buffer").value = num(draft.parameters.us30_buffer, 30);
        document.getElementById("lebprime-nas-buffer").value = num(draft.parameters.nas_buffer, 15);
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
            max_spread_points_by_symbol: {
                XAUUSD: Number(localDraft.max_spread_points_by_symbol.XAUUSD),
                US30: Number(localDraft.max_spread_points_by_symbol.US30),
                NAS100: Number(localDraft.max_spread_points_by_symbol.NAS100),
            },
            parameters: {
                ...baseline.parameters,
                gold_buffer: Number(localDraft.parameters.gold_buffer),
                us30_buffer: Number(localDraft.parameters.us30_buffer),
                nas_buffer: Number(localDraft.parameters.nas_buffer),
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
 safeSetHtml("lebprime-broker-entry-body", brokerEntries.map((row) => `<tr><td>${esc(row.context_key)}</td><td>${esc(row.zone_id)}</td><td>Entry ${num(row.entry_number).toFixed(0)}</td><td>${esc(row.order_type)}</td><td>${esc(row.normalized_price ?? row.entry_price)}</td><td>${esc(row.volume ?? "-")}</td><td>${esc(row.requested_volume ?? "-")}</td><td>${esc(row.minimum_volume ?? "-")}</td><td>${esc(row.required_margin ?? "-")}</td><td>${esc(row.free_margin ?? "-")}</td><td>${row.projected_margin_level == null ? "-" : `${num(row.projected_margin_level).toFixed(2)}%`}</td><td>${row.required_margin_level == null ? "-" : `${num(row.required_margin_level).toFixed(2)}%`}</td><td>${esc(row.sl ?? "-")}</td><td>${esc(row.tp ?? "-")}</td><td>${esc(row.mt5_order_ticket || "-")}</td><td>${badge(row.broker_status || "UNKNOWN", row.broker_status === "FILLED" ? "success" : row.broker_status === "PERMANENT_FAILURE" ? "danger" : row.broker_status === "RETRY_SCHEDULED" ? "warning" : "info")}</td><td>${num(row.attempt_count).toFixed(0)}</td><td>${row.retryable ? "Yes" : "No"}</td><td>${esc(row.next_retry_at || "-")}</td><td>${esc(row.last_failure || row.failure_reason || "-")}</td></tr>`).join("") || `<tr><td colspan="20" class="text-secondary">No broker entry records.</td></tr>`);
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



    function renderLebprimeEntryTable(title, description, rows, emptyMessage) {
        return `<div class="card border-0 shadow-sm"><div class="card-body">
            <h2 class="h5 mb-1">${esc(title)}</h2>
            <div class="small text-secondary mb-3">${esc(description)}</div>
            <div class="table-responsive"><table class="table table-sm strategy-table align-middle">
                <thead><tr><th>Symbol / TF</th><th>Zone / Entry</th><th>Side / Type</th><th>Requested / Normalized</th><th>Volume</th><th>Strategy SL / Broker SL</th><th>TP1 / TP2</th><th>Order / Deal / Position</th><th>Status</th><th>Created / Filled / Closed</th><th>Close / PnL</th><th>Failure / Retries</th></tr></thead>
                <tbody>${rows.map((row) => `<tr>
                    <td><div>${esc(row.symbol || row.broker_symbol || "-")}</div><div class="small text-secondary">${esc(row.timeframe || "-")}</div></td>
                    <td><div>${esc(row.zone_id || "-")}</div><div class="small text-secondary">Entry ${esc(row.entry_number ?? "-")}</div></td>
                    <td><div>${esc(row.side || "-")}</div><div class="small text-secondary">${esc(row.order_type || "-")}</div></td>
                    <td><div>${esc(row.requested_entry ?? "-")}</div><div class="small text-secondary">${esc(row.normalized_entry ?? "-")}</div></td>
                    <td>${esc(row.volume ?? "-")}</td>
                    <td><div>${esc(row.strategy_sl ?? "-")}</div><div class="small text-secondary">${esc(row.broker_sl ?? "-")}</div></td>
                    <td><div>${esc(row.tp1 ?? "-")}</div><div class="small text-secondary">${esc(row.tp2 ?? "-")}</div></td>
                    <td><div>${esc(row.mt5_order_ticket || "-")}</div><div class="small text-secondary">D ${esc(row.mt5_deal_ticket || "-")} / P ${esc(row.mt5_position_ticket || "-")}</div></td>
                    <td>${badge(row.status || "UNKNOWN", row.status === "CLOSED" || row.status === "FILLED" ? "success" : row.status === "PENDING" ? "info" : "warning")}</td>
                    <td><div>${esc(row.created_at || "-")}</div><div class="small text-secondary">F ${esc(row.filled_at || "-")} / C ${esc(row.closed_at || "-")}</div></td>
                    <td><div>${esc(row.close_price ?? "Unknown")}</div><div class="small text-secondary">PnL ${esc(row.realized_pnl ?? "Unknown")}</div></td>
                    <td><div>${esc(row.failure_reason || "-")}</div><div class="small text-secondary">${num(row.retry_count).toFixed(0)} retries</div></td>
                </tr>`).join("") || `<tr><td colspan="12" class="text-secondary">${esc(emptyMessage)}</td></tr>`}</tbody>
            </table></div>
        </div></div>`;
    }

    function renderTrades() {
        const snapshot = asObject(state.lebprimeTrades);
        const summary = asObject(snapshot.summary);
        document.getElementById("section-trades").innerHTML = `
            <div class="row g-3">
                <div class="col-12"><div class="card border-0 shadow-sm"><div class="card-body">
                    <div class="d-flex flex-wrap justify-content-between align-items-center gap-3">
                        <div><h2 class="h5 mb-1">LebPrime Broker Lifecycle</h2><div class="small text-secondary">Read-only records from the persisted LebPrime broker-entry lifecycle. MT5 remains authoritative.</div></div>
                        <div class="d-flex flex-wrap gap-2">
                            ${badge(`Pending ${num(summary.pending).toFixed(0)}`, "info")}
                            ${badge(`Open ${num(summary.open).toFixed(0)}`, "success")}
                            ${badge(`Closed ${num(summary.closed).toFixed(0)}`, "secondary")}
                            ${badge(`Failed ${num(summary.failed).toFixed(0)}`, "danger")}
                            ${badge(`Canceled ${num(summary.canceled).toFixed(0)}`, "warning")}
                            ${badge(`Expired ${num(summary.expired).toFixed(0)}`, "warning")}
                        </div>
                    </div>
                    <div class="small text-secondary mt-2">Historical replay excluded: ${num(summary.historical_replay_excluded).toFixed(0)}. Realized PnL stays unknown until an exact value is persisted.</div>
                </div></div></div>
                <div class="col-12">${renderLebprimeEntryTable("Pending Orders", "Orders being placed, retrying, pending at MT5, or awaiting final broker history classification.", asArray(snapshot.pending), "No pending LebPrime orders.")}</div>
                <div class="col-12">${renderLebprimeEntryTable("Open / Filled Entries", "Broker-confirmed entries with an active filled lifecycle state.", asArray(snapshot.open), "No open or filled LebPrime entries.")}</div>
                <div class="col-12">${renderLebprimeEntryTable("Closed Entries / Positions", "Broker-confirmed closed lifecycle records; unavailable outcome fields remain unknown.", asArray(snapshot.closed), "No closed LebPrime entries.")}</div>
                <div class="col-12">${renderLebprimeEntryTable("Failed / Rejected / Canceled / Expired Orders", "Terminal placement failures and broker cancellation outcomes.", asArray(snapshot.failed), "No failed, rejected, canceled, or expired LebPrime orders.")}</div>
            </div>`;
    }

    function renderPerformance() {
        const payload = asObject(state.lebprimePerformance);
        const summary = asObject(payload.summary);
        const metrics = [
            ["Zones Created", summary.zones_created], ["Orders Created", summary.orders_created],
            ["Orders Accepted", summary.orders_accepted], ["Orders Filled", summary.orders_filled],
            ["Orders Failed", summary.orders_failed], ["Orders Canceled", summary.orders_canceled],
            ["Orders Expired", summary.orders_expired], ["Open Entries", summary.open_entries],
            ["Closed Entries", summary.closed_entries],
        ];
        document.getElementById("section-performance").innerHTML = `
            <div class="row g-3">
                <div class="col-12"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5 mb-1">LebPrime Lifecycle Performance</h2>
                    <div class="small text-secondary">Counts come from persisted production zone and broker-entry records; historical replay is excluded.</div>
                    <div class="row g-2 mt-2">${metrics.map(([label, value]) => `<div class="col-6 col-md-4 col-xl">${metric(label, num(value).toFixed(0), label.includes("Failed"))}</div>`).join("")}</div>
                    <div class="alert alert-secondary mt-3 mb-0">Realized PnL and win rate: <strong>Unknown</strong>. The LebPrime broker-entry table does not currently persist exact realized outcomes, so this dashboard does not infer them from candle touches or unrelated trade models.</div>
                </div></div></div>
                <div class="col-12 col-xl-4"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">By Status</h2>
                    <div class="table-responsive"><table class="table table-sm"><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>${asArray(payload.by_status).map((row) => `<tr><td>${esc(row.status)}</td><td>${esc(row.count)}</td></tr>`).join("") || `<tr><td colspan="2" class="text-secondary">No lifecycle records.</td></tr>`}</tbody></table></div>
                </div></div></div>
                <div class="col-12 col-xl-8"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Symbol / Timeframe / Entry / Side</h2>
                    <div class="table-responsive"><table class="table table-sm"><thead><tr><th>Symbol</th><th>Timeframe</th><th>Entry</th><th>Side</th><th>Status</th><th>Count</th></tr></thead><tbody>${asArray(payload.breakdown).map((row) => `<tr><td>${esc(row.symbol)}</td><td>${esc(row.timeframe || "-")}</td><td>${esc(row.entry_number)}</td><td>${esc(row.side)}</td><td>${esc(row.status)}</td><td>${esc(row.count)}</td></tr>`).join("") || `<tr><td colspan="6" class="text-secondary">No lifecycle breakdown is available.</td></tr>`}</tbody></table></div>
                </div></div></div>
            </div>`;
    }

    function renderBacktest() {
        const backtestCfg = state.config.backtest || {};
        const strategies = ["XAU_BOT_TREND_PU", "XAU_BOT_LIQUIDIT", "XAU_BOT_COMPRESS", "XAU_BOT_BREAKOUT", "XAU_LEBPRIM", "XAU_SCORE"];
        const draft = state.backtestDraft || {};
        const selected = new Set(draft.enabled_strategies || backtestCfg.selected_strategies || strategies);
        const currentJob = latestJobByType("BACKTEST");
        const latest = state.backtestResult || {};
        const summary = latest.summary || {};
        const analysis = latest.analysis || {};
        const comparison = latest.strategy_comparison || [];
        const trades = latest.trades || [];
        const runs = state.backtestRuns || [];
        const latestMode = latest.run?.metadata_json?.resolved_execution_mode || {};
        const worker = currentWorkerStatus();
        document.getElementById("section-backtest").innerHTML = `
            <div class="row g-3">
                <div class="col-12 col-xl-4">
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Backtest Controls</h2>
                        ${field("bt-symbol", "Symbol", draft.symbol ?? backtestCfg.symbol ?? state.config.mt5?.symbol ?? "XAUUSD", "text")}
                        ${select("bt-timeframe", "Timeframe", ["M1", "M3", "M5", "M15", "M30", "H1"], draft.timeframe ?? backtestCfg.timeframe ?? "M1")}
                        ${field("bt-start", "Start Date", draft.start ?? backtestCfg.start ?? "", "date")}
                        ${field("bt-end", "End Date", draft.end ?? backtestCfg.end ?? "", "date")}
                        ${field("bt-initial-balance", "Initial Balance", draft.initial_balance ?? backtestCfg.initial_balance ?? 10000, "number", "0.01")}
                        ${field("bt-risk-percent", "Risk %", draft.risk_percent ?? backtestCfg.risk_percent ?? state.config.risk?.risk_percent ?? 0.5, "number", "0.01")}
                        ${field("bt-spread", "Spread Points", draft.spread_points ?? backtestCfg.spread_model?.points ?? 20, "number", "0.1")}
                        ${field("bt-slippage", "Slippage Points", draft.slippage_points ?? backtestCfg.slippage_model?.points ?? 2, "number", "0.1")}
                        ${select("bt-execution-model", "Execution Model", ["next_bar_open", "signal_price_touch", "current_bar_close"], draft.execution_model ?? backtestCfg.execution_model ?? "next_bar_open")}
                        <div class="small mb-2">${badge(workerStateLabel(worker), workerTone(worker))} <span class="text-secondary">${worker.current_job_id ? `Job #${esc(worker.current_job_id)}` : "Idle"} | last seen ${esc(worker.last_seen_at || "N/A")}</span></div>
                        ${textOr(worker.state || worker.status, "unavailable").toLowerCase() !== "running" ? `<div class="alert alert-warning py-2">Backtest jobs can queue, but execution waits until the worker is running.</div>` : ""}
                        <div class="small text-secondary mb-2">Strategies</div>
                        ${strategies.map((name) => `<div class="form-check"><input class="form-check-input" type="checkbox" id="bt-strategy-${name}" ${selected.has(name) ? "checked" : ""}><label class="form-check-label" for="bt-strategy-${name}">${esc(name)}</label></div>`).join("")}
                        <button class="btn btn-primary mt-3 w-100" id="bt-run">Run Backtest</button>
                        <div class="small text-secondary mt-3">Job: ${esc(currentJob?.state || "IDLE")} ${currentJob ? `| #${esc(currentJob.id)} | ${esc((currentJob.related_run || {}).id || currentJob.related_run_id || "")}` : ""}</div>
                        <div class="small text-secondary">Progress: ${esc((currentJob?.progress_current ?? 0))} / ${esc((currentJob?.progress_total ?? 0))}</div>
                        ${currentJob?.failure_reason ? `<div class="small text-danger mt-2">Job error: ${esc(currentJob.failure_reason)}</div>` : ""}
                        ${currentJob?.interrupted_reason ? `<div class="small text-warning mt-2">Interrupted: ${esc(currentJob.interrupted_reason)}</div>` : ""}
                    </div></div>
                </div>
                <div class="col-12 col-xl-8">
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <h2 class="h5 mb-0">Backtest Summary</h2>
                            <div class="badge text-bg-secondary">${esc(latest.run?.id || "No run selected")}</div>
                        </div>
                        <div class="small text-secondary mb-2">Resolved Mode: ${esc(latestMode.label || latestMode.selected_mode || "N/A")} | Families: ${esc((latestMode.enabled_families || []).join(", ") || "N/A")}</div>
                        <div class="row g-2">
                            <div class="col-6 col-md-3">${metric("Trades", num(summary.total_trades).toFixed(0))}</div>
                            <div class="col-6 col-md-3">${metric("Signals", num(summary.total_signals).toFixed(0))}</div>
                            <div class="col-6 col-md-3">${metric("Blocked", num(summary.blocked_signals).toFixed(0))}</div>
                            <div class="col-6 col-md-3">${metric("Win Rate", `${num(summary.win_rate).toFixed(2)}%`)}</div>
                            <div class="col-6 col-md-3">${metric("Net Profit", num(summary.net_profit).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Profit Factor", num(summary.profit_factor).toFixed(2))}</div>
                            <div class="col-6 col-md-3">${metric("Max Drawdown", `${num(summary.max_drawdown).toFixed(2)}%`)}</div>
                            <div class="col-6 col-md-3">${metric("Avg R", num(summary.average_r).toFixed(2))}</div>
                        </div>
                        <div class="small text-secondary mt-3">Best: ${esc(summary.best_strategy || "N/A")} | Worst: ${esc(summary.worst_strategy || "N/A")} | Expectancy: ${num(summary.expectancy).toFixed(2)} | Avg Duration: ${num(summary.average_trade_duration).toFixed(1)}s</div>
                    </div></div>
                    <div class="card border-0 shadow-sm mb-3"><div class="card-body">
                        <h2 class="h5">Strategy Comparison</h2>
                        <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>PnL</th><th>Avg R</th><th>Profit Factor</th><th>Blocked</th></tr></thead><tbody>
                        ${comparison.map((row) => `<tr><td>${esc(row.strategy)}</td><td>${num(row.trades).toFixed(0)}</td><td>${num(row.win_rate).toFixed(2)}%</td><td>${num(row.pnl).toFixed(2)}</td><td>${num(row.avg_r).toFixed(2)}</td><td>${num(row.profit_factor).toFixed(2)}</td><td>${num(row.blocked_count).toFixed(0)}</td></tr>`).join("") || `<tr><td colspan="7" class="text-secondary">No strategy data.</td></tr>`}
                        </tbody></table></div>
                        <div class="small text-secondary mt-2">Executed signals: ${num(analysis.executed_signals).toFixed(0)} | Blocked signals: ${num(analysis.blocked_signals).toFixed(0)}</div>
                    </div></div>
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Trade Log</h2>
                        <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>Strategy</th><th>Side</th><th>Open</th><th>Close</th><th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>PnL</th><th>R</th><th>Reason</th><th>MFE</th><th>MAE</th></tr></thead><tbody>
                        ${trades.slice(-300).map((row) => `<tr><td>${esc(row.strategy_name)}</td><td>${esc(row.side)}</td><td>${esc(row.open_time)}</td><td>${esc(row.close_time)}</td><td>${num(row.entry).toFixed(3)}</td><td>${num(row.sl).toFixed(3)}</td><td>${num(row.tp).toFixed(3)}</td><td>${num(row.exit_price).toFixed(3)}</td><td>${num(row.pnl).toFixed(2)}</td><td>${num(row.pnl_r).toFixed(2)}</td><td>${esc(row.exit_reason || "")}</td><td>${num(row.mfe).toFixed(3)}</td><td>${num(row.mae).toFixed(3)}</td></tr>`).join("") || `<tr><td colspan="13" class="text-secondary">No trades yet.</td></tr>`}
                        </tbody></table></div>
                    </div></div>
                </div>
                <div class="col-12">
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Recent Backtest Runs</h2>
                        <div class="table-responsive"><table class="table table-sm strategy-table"><thead><tr><th>ID</th><th>Created</th><th>Symbol</th><th>TF</th><th>Start</th><th>End</th><th>Mode</th><th>Strategies</th><th>Action</th></tr></thead><tbody>
                        ${runs.map((row) => `<tr><td>${esc(row.id)}</td><td>${esc(row.created_at)}</td><td>${esc(row.symbol)}</td><td>${esc(row.timeframe)}</td><td>${esc(row.start_date)}</td><td>${esc(row.end_date)}</td><td>${esc(row.metadata_json?.resolved_execution_mode?.label || row.metadata_json?.resolved_execution_mode?.selected_mode || "N/A")}</td><td>${esc((row.enabled_strategies_json || []).join(", "))}</td><td class="d-flex flex-wrap gap-2"><button class="btn btn-sm btn-outline-light" data-bt-run="${row.id}">Load</button><button class="btn btn-sm btn-outline-info" data-bt-replay="${row.id}">Replay</button></td></tr>`).join("") || `<tr><td colspan="9" class="text-secondary">No backtest runs saved yet.</td></tr>`}
                        </tbody></table></div>
                    </div></div>
                </div>
            </div>`;

        document.querySelectorAll("#section-backtest input, #section-backtest select").forEach((node) => {
            node.addEventListener("focus", () => markBacktestInteraction());
            node.addEventListener("click", () => markBacktestInteraction());
            node.addEventListener("keydown", () => markBacktestInteraction());
            node.addEventListener("input", () => syncBacktestDraft(strategies));
            node.addEventListener("change", () => syncBacktestDraft(strategies));
            node.addEventListener("blur", () => {
                window.setTimeout(() => {
                    if (!isBacktestInputActive()) {
                        state.backtestUi.pauseRefreshUntil = Date.now() + 3000;
                    }
                }, 0);
            });
        });

        document.getElementById("bt-run").onclick = async () => {
            if (isReadonly()) {
                setFeedback("Dashboard is in readonly mode.", true);
                return;
            }
            syncBacktestDraft(strategies);
            const enabled_strategies = strategies.filter((name) => on(`bt-strategy-${name}`));
            try {
                setFeedback("Queueing backtest job...");
                const payload = {
                    actor: "dashboard",
                    symbol: val("bt-symbol").toUpperCase(),
                    timeframe: val("bt-timeframe"),
                    start_date: val("bt-start"),
                    end_date: val("bt-end"),
                    enabled_strategies,
                    session_filter: backtestCfg.session_filter || { enabled: false, allowed_sessions: [] },
                    initial_balance: num(val("bt-initial-balance"), 10000),
                    risk_percent: num(val("bt-risk-percent"), 0.5),
                    spread_model: { type: "fixed_points", points: num(val("bt-spread"), 20) },
                    slippage_model: { type: "fixed_points", points: num(val("bt-slippage"), 2) },
                    execution_model: val("bt-execution-model"),
                    notes: backtestCfg.notes || null,
                };
                const result = await api("/api/jobs/backtest", { method: "POST", body: JSON.stringify(payload) });
                if (result.job) {
                    state.jobs = [result.job, ...(state.jobs || []).filter((job) => job.id !== result.job.id)];
                }
                state.backtestUi.dirty = false;
                state.backtestUi.pauseRefreshUntil = 0;
                state.workerStatus = result.worker || state.workerStatus;
                setFeedback(result.warning || `Backtest job queued. Job #${result.job?.id || "N/A"}`, !!result.warning);
                await fetchJobs();
                await fetchBacktests();
                await fetchWorkerStatus();
                renderAll({ force: true });
            } catch (error) {
                setFeedback(error.message, true);
            }
        };
        document.querySelectorAll("[data-bt-run]").forEach((button) => {
            button.onclick = async () => {
                try {
                    const runId = Number(button.dataset.btRun);
                    const result = await api(`/api/backtest/run/${runId}`);
                    state.backtestResult = result.result || null;
                    state.backtestUi.dirty = false;
                    state.backtestUi.pauseRefreshUntil = 0;
                    setFeedback(`Loaded backtest run #${runId}`);
                    if (!state.backtestReplay.selectedRunId) {
                        state.backtestReplay.selectedRunId = runId;
                    }
                    renderAll({ force: true });
                } catch (error) {
                    setFeedback(error.message, true);
                }
            };
        });
    }

    function renderBacktestReplay() {
        const container = ensureContainer("section-backtest-replay");
        if (!container) return;
        const replay = replayState();
        const summary = asObject(replay.summary || {});
        const run = asObject(summary.run || {});
        const frames = replayVisibleFrames();
        const events = replayFilteredEvents();
        const visibleSource = textOr(replay.sourceSummary || summary.history_resolution?.source_kind || run.metadata_json?.history_resolution?.source_kind || "Unknown");
        const runTone = replayStatusTone(replay.runStatus);
        const controlsDisabled = !replay.selectedRunId || replay.loading;
        const selectedRunLabel = replay.selectedRunId ? `#${esc(replay.selectedRunId)}` : "No run selected";
        const familyOptions = replayVisibleFamilies();
        container.innerHTML = `
            <div class="row g-3">
                <div class="col-12">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body d-flex flex-wrap justify-content-between align-items-start gap-3">
                            <div>
                                <h2 class="h5 mb-1">Backtest Replay</h2>
                                <div class="text-secondary">Replay finished runs candle by candle or watch active runs update live.</div>
                            </div>
                            <div class="d-flex flex-wrap gap-2 align-items-center">
                                ${badge(textOr(replay.runStatus, "IDLE"), runTone)}
                                ${badge(replay.mode.toUpperCase(), replay.mode === "live" ? "info" : "secondary")}
                                ${badge(`Run ${selectedRunLabel}`, "dark")}
                            </div>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-xxl-9">
                    <div class="replay-card replay-panel">
                        <div class="replay-hero mb-3">
                            <div class="dashboard-summary-card">
                                <div class="small text-uppercase text-secondary fw-semibold mb-2">Run Selector</div>
                                <div class="row g-2 align-items-end">
                                    <div class="col-12 col-lg-7">
                                        <label class="form-label w-100"><span class="small text-secondary">Backtest run</span>
                                            <select class="form-select" id="bt-replay-run-select">
                                                <option value="">Select a run...</option>
                                                ${(state.backtestRuns || []).map((row) => `<option value="${esc(row.id)}" ${num(row.id) === num(replay.selectedRunId) ? "selected" : ""}>#${esc(row.id)} | ${esc(row.symbol || "")} | ${esc(row.timeframe || "")} | ${esc(row.state || row.status || "")}</option>`).join("")}
                                            </select>
                                        </label>
                                    </div>
                                    <div class="col-6 col-lg-2"><button class="btn btn-outline-light w-100" id="bt-replay-open" ${controlsDisabled ? "disabled" : ""}>Open</button></div>
                                    <div class="col-6 col-lg-3"><button class="btn btn-outline-light w-100" id="bt-replay-refresh" ${controlsDisabled ? "disabled" : ""}>Refresh</button></div>
                                </div>
                                <div class="small text-secondary mt-2">Symbol ${esc(run.symbol || "N/A")} | Timeframe ${esc(run.timeframe || "N/A")} | Source ${esc(visibleSource)}</div>
                                <div class="small text-secondary">Window ${esc(replay.windowStart)} to ${esc(replay.windowEnd)} | Bars ${esc(replay.frames.length)} loaded / ${esc(replay.totalBars || summary.frames_count || 0)} total</div>
                                <div class="small text-secondary">Source summary: ${esc(visibleSource)}</div>
                                <div id="bt-replay-status" class="small text-secondary mt-2"></div>
                                <div class="replay-progress mt-3">
                                    <div class="d-flex flex-wrap justify-content-between gap-2 small text-secondary mb-1">
                                        <span>Job / Run: ${esc(run.id || replay.selectedRunId || "N/A")}</span>
                                        <span>Processed ${esc(replay.progress.current)} / ${esc(replay.progress.total || "?" )} bars</span>
                                    </div>
                                    <div class="progress replay-progress-bar" role="progressbar" aria-label="Replay progress">
                                        <div class="progress-bar bg-info" style="width: ${esc(replay.progress.indeterminate ? 100 : Math.max(0, Math.min(100, replay.progress.pct)).toFixed(1))}%"></div>
                                    </div>
                                    <div class="d-flex flex-wrap gap-2 mt-2">
                                        <span class="replay-badge text-bg-dark">Frames ${esc(replay.progress.latestFrameCount || summary.frames_count || 0)}</span>
                                        <span class="replay-badge text-bg-dark">Events ${esc(replay.progress.latestEventCount || summary.events_count || 0)}</span>
                                        <span class="replay-badge text-bg-dark">Last update ${esc(replay.progress.lastUpdateAt || run.updated_at || "N/A")}</span>
                                        <span class="replay-badge text-bg-${replay.progress.liveUpdatesOn ? "success" : "secondary"}">Live updates ${replay.progress.liveUpdatesOn ? "ON" : "OFF"}</span>
                                        <span class="replay-badge text-bg-${replay.autoScroll ? "success" : "secondary"}">Auto-scroll ${replay.autoScroll ? "ON" : "OFF"}</span>
                                    </div>
                                </div>
                            </div>
                            <div class="dashboard-summary-card">
                                <div class="small text-uppercase text-secondary fw-semibold mb-2">Playback</div>
                                <div class="replay-toolbar mb-2">
                                    <button class="btn btn-sm btn-success" id="bt-replay-play" ${controlsDisabled ? "disabled" : ""}>Play / Pause</button>
                                    <button class="btn btn-sm btn-outline-light" id="bt-replay-back" ${controlsDisabled ? "disabled" : ""}>Step Back</button>
                                    <button class="btn btn-sm btn-outline-light" id="bt-replay-forward" ${controlsDisabled ? "disabled" : ""}>Step Forward</button>
                                    <button class="btn btn-sm btn-outline-light" id="bt-replay-reset" ${controlsDisabled ? "disabled" : ""}>Reset</button>
                                </div>
                                <div class="btn-group replay-speed-group mb-2" role="group" aria-label="Playback speed">
                                    ${BACKTEST_REPLAY_SPEEDS.map((speed) => `<button class="btn btn-sm btn-outline-info ${num(replay.playbackSpeed) === speed ? "active" : ""}" data-replay-speed="${speed}" ${controlsDisabled ? "disabled" : ""}>x${speed}</button>`).join("")}
                                </div>
                                <div class="replay-toolbar mb-2">
                                    <button class="btn btn-sm btn-outline-primary" id="bt-replay-zoom-in" ${controlsDisabled ? "disabled" : ""}>Zoom In</button>
                                    <button class="btn btn-sm btn-outline-primary" id="bt-replay-zoom-out" ${controlsDisabled ? "disabled" : ""}>Zoom Out</button>
                                    <button class="btn btn-sm btn-outline-light" id="bt-replay-zoom-reset" ${controlsDisabled ? "disabled" : ""}>Reset Zoom</button>
                                    <div class="form-check form-switch ms-2">
                                        <input class="form-check-input" type="checkbox" id="bt-replay-auto-scroll" ${replay.autoScroll ? "checked" : ""} ${controlsDisabled ? "disabled" : ""}>
                                        <label class="form-check-label small text-secondary" for="bt-replay-auto-scroll">Auto-scroll</label>
                                    </div>
                                </div>
                                <div class="row g-2">
                                    <div class="col-12"><input class="form-control form-control-sm" id="bt-replay-jump-bar" type="number" min="0" placeholder="Jump to bar index"></div>
                                    <div class="col-12"><input class="form-control form-control-sm" id="bt-replay-jump-ts" type="text" placeholder="Jump to timestamp (ISO or prefix)"></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-light w-100" id="bt-replay-jump" ${controlsDisabled ? "disabled" : ""}>Jump</button></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-light w-100" id="bt-replay-reset-bar" ${controlsDisabled ? "disabled" : ""}>Reset To Start</button></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-light w-100" id="bt-replay-prev-trade" ${controlsDisabled ? "disabled" : ""}>Prev Trade</button></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-light w-100" id="bt-replay-next-trade" ${controlsDisabled ? "disabled" : ""}>Next Trade</button></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-warning w-100" id="bt-replay-prev-block" ${controlsDisabled ? "disabled" : ""}>Prev Blocked Cluster</button></div>
                                    <div class="col-6"><button class="btn btn-sm btn-outline-warning w-100" id="bt-replay-next-block" ${controlsDisabled ? "disabled" : ""}>Next Blocked Cluster</button></div>
                                </div>
                            </div>
                        </div>
                        <div class="replay-shell">
                            <div>
                                <div class="replay-chart-wrap mb-3">
                                    <canvas id="bt-replay-canvas" class="replay-chart"></canvas>
                                    <div id="bt-replay-overlay" class="replay-overlay"></div>
                                </div>
                                <div class="replay-navigator-wrap mb-3">
                                    <canvas id="bt-replay-navigator" class="replay-navigator"></canvas>
                                </div>
                                <div class="d-flex flex-wrap gap-2 align-items-center mb-3">
                                    <span class="replay-badge text-bg-dark">Current bar <strong class="ms-1" id="bt-replay-current-bar">${esc(replay.currentBarIndex)}</strong></span>
                                    <span class="replay-badge text-bg-dark">Cursor ${esc(replaySelectedBarIndex())}</span>
                                    <span class="replay-badge text-bg-dark">Frames ${esc(frames.length)}</span>
                                    <span class="replay-badge text-bg-dark">Events ${esc(events.length)}</span>
                                    <span class="replay-badge text-bg-dark" id="bt-replay-status-chip">${esc(textOr(replay.runStatus, "UNKNOWN"))}</span>
                                    <span class="replay-badge text-bg-dark" id="bt-replay-source-chip">${esc(visibleSource)}</span>
                                    <span class="replay-badge text-bg-dark" id="bt-replay-window-chip">${esc(`${replay.windowStart} - ${replay.windowEnd}`)}</span>
                                </div>
                                <div class="dashboard-summary-card">
                                    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
                                        <div>
                                            <div class="small text-uppercase text-secondary fw-semibold">Event Overlay Legend</div>
                                            <div class="small text-secondary">Candles, entries, exits, pending orders, and management events use badges and line styles.</div>
                                        </div>
                                        <div class="d-flex flex-wrap gap-2">
                                            <span class="replay-badge text-bg-success">Trade Open</span>
                                            <span class="replay-badge text-bg-danger">Trade Close</span>
                                            <span class="replay-badge text-bg-warning text-dark">Blocked</span>
                                            <span class="replay-badge text-bg-info text-dark">Pending</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div class="d-grid gap-3">
                                <div class="dashboard-summary-card">
                                    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
                                        <div>
                                            <div class="small text-uppercase text-secondary fw-semibold">Filters</div>
                                            <div class="small text-secondary">Keep the view focused on the events you want to inspect.</div>
                                        </div>
                                    </div>
                                    <div class="row g-2">
                <div class="col-12 col-lg-6"><label class="form-label w-100"><span class="small text-secondary">Family filter</span><select class="form-select form-select-sm" id="bt-replay-window-family-filter"><option value="">All families</option>${familyOptions.map((family) => `<option value="${esc(family)}" ${textOr(replay.filters.family).toUpperCase() === family ? "selected" : ""}>${esc(formatFamilyName(family))}</option>`).join("")}</select></label></div>
                                        <div class="col-6 col-lg-2"><div class="form-check form-switch mt-4"><input class="form-check-input" id="bt-replay-show-blocked" type="checkbox" ${replay.filters.showBlocked ? "checked" : ""}><label class="form-check-label" for="bt-replay-show-blocked">Blocked</label></div></div>
                                        <div class="col-6 col-lg-2"><div class="form-check form-switch mt-4"><input class="form-check-input" id="bt-replay-show-pending" type="checkbox" ${replay.filters.showPending ? "checked" : ""}><label class="form-check-label" for="bt-replay-show-pending">Pending</label></div></div>
                                        <div class="col-6 col-lg-2"><div class="form-check form-switch mt-4"><input class="form-check-input" id="bt-replay-show-management" type="checkbox" ${replay.filters.showManagement ? "checked" : ""}><label class="form-check-label" for="bt-replay-show-management">Management</label></div></div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-xxl-3">
                    <div class="replay-card replay-panel mb-3">
                        <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
                            <div>
                                <div class="small text-uppercase text-secondary fw-semibold">Event Feed</div>
                                <div class="small text-secondary">Click an event to jump the chart cursor.</div>
                            </div>
                            <div class="badge text-bg-dark">${esc(events.length)}</div>
                        </div>
                        <div id="bt-replay-feed" class="replay-feed"></div>
                    </div>
                    <div class="replay-card replay-panel">
                        <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
                            <div>
                                <div class="small text-uppercase text-secondary fw-semibold">Inspector</div>
                                <div class="small text-secondary">Structured details for the selected candle or event.</div>
                            </div>
                        </div>
                        <div id="bt-replay-inspector" class="replay-inspector"></div>
                    </div>
                </div>
            </div>
        `;

        document.getElementById("bt-replay-open").onclick = () => {
            const select = document.getElementById("bt-replay-run-select");
            const runId = Number(select?.value || replay.selectedRunId || 0);
            if (runId) replayLoadRun(runId).catch((error) => replaySetMessage(error.message, true));
        };
        document.getElementById("bt-replay-refresh").onclick = async () => {
            const runId = Number(replay.selectedRunId || document.getElementById("bt-replay-run-select")?.value || 0);
            if (!runId) return;
            await fetchBacktests();
            await replayLoadRun(runId, { silent: true });
        };
        document.getElementById("bt-replay-play").onclick = () => replayTogglePlay();
        document.getElementById("bt-replay-back").onclick = () => replayStepBackward();
        document.getElementById("bt-replay-forward").onclick = () => replayStepForward();
        document.getElementById("bt-replay-reset").onclick = () => replaySetBar(0, { center: true });
        document.getElementById("bt-replay-reset-bar").onclick = () => replaySetBar(0, { center: true });
        document.getElementById("bt-replay-prev-trade").onclick = () => replayJumpToPreviousVisibleTrade();
        document.getElementById("bt-replay-next-trade").onclick = () => replayJumpToNextVisibleTrade();
        document.getElementById("bt-replay-prev-block").onclick = () => replayJumpToBlockedCluster(-1);
        document.getElementById("bt-replay-next-block").onclick = () => replayJumpToBlockedCluster(1);
        document.getElementById("bt-replay-zoom-in").onclick = () => replayZoomViewport(-1);
        document.getElementById("bt-replay-zoom-out").onclick = () => replayZoomViewport(1);
        document.getElementById("bt-replay-zoom-reset").onclick = () => {
            const replayStateValue = replayState();
            replaySetViewport(0, Math.min(BACKTEST_REPLAY_WINDOW_SIZE, Math.max(24, replayVisibleFrames().length || BACKTEST_REPLAY_WINDOW_SIZE)));
            replayCenterViewportOnBar(replaySelectedBarIndex());
            replayStateValue.autoScroll = true;
            scheduleReplayRender();
        };
        const autoScrollNode = document.getElementById("bt-replay-auto-scroll");
        if (autoScrollNode) {
            autoScrollNode.onchange = () => {
                replay.autoScroll = !!autoScrollNode.checked;
                scheduleReplayRender();
            };
        }
        document.getElementById("bt-replay-jump").onclick = () => {
            const barValue = document.getElementById("bt-replay-jump-bar")?.value;
            const tsValue = document.getElementById("bt-replay-jump-ts")?.value;
            if (barValue !== "") {
                replayJumpToBar(barValue);
                return;
            }
            if (tsValue) {
                replayJumpToTimestamp(tsValue);
            }
        };
        document.querySelectorAll("[data-replay-speed]").forEach((button) => {
            button.onclick = () => replaySetSpeed(button.dataset.replaySpeed);
        });
        document.getElementById("bt-replay-run-select").onchange = (event) => {
            const runId = Number(event.target.value || 0);
            if (runId) {
                replayLoadRun(runId, { silent: true }).catch((error) => replaySetMessage(error.message, true));
            }
        };
        document.getElementById("bt-replay-window-family-filter").onchange = (event) => {
            replay.filters.family = textOr(event.target.value);
            scheduleReplayRender();
        };
        document.getElementById("bt-replay-show-blocked").onchange = (event) => {
            replay.filters.showBlocked = !!event.target.checked;
            scheduleReplayRender();
        };
        document.getElementById("bt-replay-show-pending").onchange = (event) => {
            replay.filters.showPending = !!event.target.checked;
            scheduleReplayRender();
        };
        document.getElementById("bt-replay-show-management").onchange = (event) => {
            replay.filters.showManagement = !!event.target.checked;
            scheduleReplayRender();
        };
        document.getElementById("bt-replay-jump-bar").addEventListener("keydown", (event) => {
            if (event.key === "Enter") document.getElementById("bt-replay-jump").click();
        });
        document.getElementById("bt-replay-jump-ts").addEventListener("keydown", (event) => {
            if (event.key === "Enter") document.getElementById("bt-replay-jump").click();
        });
        const canvas = document.getElementById("bt-replay-canvas");
        if (canvas) {
            let dragState = null;
            canvas.addEventListener("wheel", (event) => {
                if (!replay.selectedRunId) return;
                event.preventDefault();
                replayZoomViewport(event.deltaY > 0 ? 1 : -1);
            }, { passive: false });
            canvas.addEventListener("pointerdown", (event) => {
                dragState = { x: event.clientX, start: replayState().viewportStart };
                canvas.setPointerCapture?.(event.pointerId);
            });
            canvas.addEventListener("pointermove", (event) => {
                if (!dragState) return;
                const frames = replayVisibleFrames();
                if (!frames.length) return;
                const rect = canvas.getBoundingClientRect();
                const deltaPx = event.clientX - dragState.x;
                const approxBars = Math.round((deltaPx / Math.max(rect.width, 1)) * replayState().viewportSize);
                if (Math.abs(approxBars) >= 1) {
                    replaySetViewport(dragState.start - approxBars, replayState().viewportSize, { clampToCurrent: false });
                    scheduleReplayRender();
                }
            });
            const endDrag = () => { dragState = null; };
            canvas.addEventListener("pointerup", endDrag);
            canvas.addEventListener("pointercancel", endDrag);
            canvas.addEventListener("click", (event) => {
                const frames = replayVisibleFrames();
                if (!frames.length) return;
                const barIndex = replayBarIndexFromCanvasX(canvas, event.clientX);
                if (barIndex === null) return;
                const frame = frames.find((item) => num(item.bar_index) === num(barIndex));
                if (!frame) return;
                replayEnsureEventSelection(null);
                replaySetBar(frame.bar_index, { center: false });
            });
        }
        const navigator = document.getElementById("bt-replay-navigator");
        if (navigator) {
            navigator.addEventListener("click", (event) => {
                const index = replayViewportIndexFromNavigatorX(navigator, event.clientX);
                const frames = replayVisibleFrames();
                if (index === null || !frames.length) return;
                const target = frames[index];
                if (!target) return;
                replaySetBar(target.bar_index, { center: true });
            });
        }
        if (replay.loading) {
            replaySetMessage("Loading replay...");
        } else if (replay.error) {
            replaySetMessage(replay.error, true);
        } else if (!replay.selectedRunId) {
            replaySetMessage("Select a backtest run to start replay.");
        }
        scheduleReplayRender();
    }

    function renderOOS() {
        const container = ensureContainer("section-oos");
        if (!container) return;
        dashboardLog("render", "renderOOS started", { container: "section-oos", selectedRunId: state.oosUi.selectedRunId });
        try {
            const statusPayload = normalizeOOSPayload(state.oosStatus || {});
            const history = (state.oosHistory.length ? state.oosHistory : statusPayload.history).filter(Boolean);
            const requestedRunId = num(state.oosUi.selectedRunId, 0);
            const currentRun = normalizeOOSRun(statusPayload.run);
            const selectedHistoryRun = history.find((item) => num(item.id, 0) === requestedRunId) || null;
            const displayRun = (currentRun && (!requestedRunId || num(currentRun.id, 0) === requestedRunId)) ? currentRun : (selectedHistoryRun || currentRun);
            const progress = asObject(displayRun?.progress);
            const scenarios = asArray(displayRun?.scenarios);
            const currentJob = latestJobByType("OOS_MATRIX");
            const latestRunId = num(displayRun?.id, 0) || num(history[0]?.id, 0) || null;
            const runRequest = asObject(displayRun?.request_json);
            const draft = state.oosDraft || {};
            const strategies = ["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS", "XAU_LEBPRIM"];
            const defaultStrategies = asArray(runRequest.enabled_strategies).length ? asArray(runRequest.enabled_strategies) : strategies;
            const selected = new Set(asArray(draft.enabled_strategies).length ? draft.enabled_strategies : defaultStrategies);
            const currentScenarioModeLabel = textOr(
                displayRun?.current_scenario?.resolved_execution_mode?.selected_mode
                || displayRun?.current_scenario?.resolved_execution_mode?.label
                || displayRun?.current_scenario?.metadata_json?.requested_execution_mode,
            );
            container.innerHTML = `
                <div class="row g-3">
                    <div class="col-12">
                        <div class="card border-0 shadow-sm">
                            <div class="card-body d-flex flex-wrap justify-content-between align-items-start gap-3">
                                <div>
                                    <h2 class="h5 mb-1">Out-of-Sample Evaluation</h2>
                                    <div class="text-secondary">Run, resume, and audit the six-scenario OOS matrix with clear status, artifacts, and history.</div>
                                </div>
                                <div class="d-flex flex-wrap gap-2 align-items-center">
                                    ${badge(displayRun?.state || statusPayload.state || "IDLE", stateTone(displayRun?.state || statusPayload.state))}
                                    ${displayRun?.id ? badge(`Run #${displayRun.id}`, "dark") : ""}
                                    ${currentJob ? badge(`Job #${currentJob.id}`, stateTone(currentJob.state)) : badge("No Active Job", "secondary")}
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="col-12 col-xl-4">
                        <div class="card border-0 shadow-sm h-100"><div class="card-body">
                            <h3 class="h6 text-uppercase text-secondary fw-semibold mb-3">Run Controls</h3>
                            ${field("oos-title", "Run Title", draft.title ?? displayRun?.title ?? "Bot OOS Matrix")}
                            <div class="row g-2">
                                <div class="col-md-6">${field("oos-symbol", "Symbol", draft.symbol ?? displayRun?.symbol ?? "XAUUSD", "text")}</div>
                                <div class="col-md-6">${select("oos-timeframe", "Timeframe", ["M1", "M3", "M5", "M15", "M30", "H1"], draft.timeframe ?? displayRun?.timeframe ?? "M1")}</div>
                                <div class="col-md-6">${field("oos-start", "Start Date", draft.start_date ?? displayRun?.start_date ?? "", "date")}</div>
                                <div class="col-md-6">${field("oos-end", "End Date", draft.end_date ?? displayRun?.end_date ?? "", "date")}</div>
                                <div class="col-md-6">${field("oos-initial-balance", "Initial Balance", draft.initial_balance ?? runRequest.initial_balance ?? 10000, "number", "0.01")}</div>
                                <div class="col-md-6">${field("oos-risk-percent", "Risk %", draft.risk_percent ?? runRequest.risk_percent ?? state.config.risk?.risk_percent ?? 0.5, "number", "0.01")}</div>
                                <div class="col-md-6">${field("oos-spread", "Spread Points", draft.spread_points ?? runRequest.spread_model?.points ?? 20, "number", "0.1")}</div>
                                <div class="col-md-6">${field("oos-slippage", "Slippage Points", draft.slippage_points ?? runRequest.slippage_model?.points ?? 2, "number", "0.1")}</div>
                                <div class="col-12">${select("oos-execution-model", "Execution Model", ["next_bar_open", "signal_price_touch", "current_bar_close"], draft.execution_model ?? runRequest.execution_model ?? "next_bar_open")}</div>
                                <div class="col-12">${field("oos-baseline-run-id", "Baseline Run ID", draft.baseline_run_id ?? displayRun?.baseline_run_id ?? "", "number", "1")}</div>
                                <div class="col-12">${field("oos-baseline-bundle", "Baseline Bundle Path", draft.baseline_bundle_path ?? displayRun?.baseline_bundle_path ?? "", "text")}</div>
                                <div class="col-12">${field("oos-baseline-db", "Baseline DB Path", draft.baseline_database_path ?? displayRun?.baseline_database_path ?? "", "text")}</div>
                                <div class="col-12">${field("oos-allowed-sessions", "Allowed Sessions CSV", draft.allowed_sessions ?? asArray(runRequest.session_filter?.allowed_sessions).join(", "), "text")}</div>
                            </div>
                            <div class="small text-secondary mt-3">Historical comparison scenarios include final report rebuild support.</div>
                            <div class="mt-3">
                                <div class="small text-uppercase text-secondary fw-semibold mb-2">Strategies</div>
                                ${strategies.map((name) => `<div class="form-check"><input class="form-check-input" type="checkbox" id="oos-strategy-${name}" ${selected.has(name) ? "checked" : ""}><label class="form-check-label" for="oos-strategy-${name}">${esc(name)}</label></div>`).join("")}
                            </div>
                            <div class="d-grid gap-2 mt-3">
                                <button class="btn btn-primary" id="oos-run">Start OOS</button>
                                <button class="btn btn-outline-warning" id="oos-resume" ${latestRunId ? "" : "disabled"}>Resume OOS</button>
                                <button class="btn btn-outline-info" id="oos-rebuild" ${latestRunId ? "" : "disabled"}>Rebuild Final Report</button>
                            </div>
                        </div></div>
                    </div>
                    <div class="col-12 col-xl-8">
                        <div class="row g-3">
                            <div class="col-12 col-lg-7">
                                <div class="card border-0 shadow-sm h-100"><div class="card-body">
                                    <div class="d-flex justify-content-between align-items-start gap-2 mb-3">
                                        <div>
                                            <h3 class="h6 text-uppercase text-secondary fw-semibold mb-1">Current Status</h3>
                                            <div class="small text-secondary">Selected run and worker progress for the active OOS evaluation.</div>
                                        </div>
                                        ${badge(displayRun?.state || "IDLE", stateTone(displayRun?.state))}
                                    </div>
                                    ${displayRun ? `
                                        <div class="row g-2 mb-3">
                                            <div class="col-6 col-md-3">${metric("Run ID", safeMetricValue(displayRun.id))}</div>
                                            <div class="col-6 col-md-3">${metric("Progress", `${num(progress.current).toFixed(0)} / ${num(progress.total).toFixed(0)}`)}</div>
                                            <div class="col-6 col-md-3">${metric("Completed", num(progress.completed_scenarios).toFixed(0))}</div>
                                            <div class="col-6 col-md-3">${metric("Failed", num(progress.failed_scenarios).toFixed(0))}</div>
                                        </div>
                                        <div class="small text-secondary">Current scenario: ${esc(displayRun.current_scenario?.scenario_label || "None")}</div>
                                        <div class="small text-secondary">Requested mode: ${esc(displayRun.metadata_json?.requested_execution_mode || currentExecutionMode().selected_mode || "Unknown")}${currentScenarioModeLabel ? ` | Active scenario mode: ${esc(currentScenarioModeLabel)}` : ""}</div>
                                        <div class="small text-secondary">Elapsed markers: started ${esc(displayRun.started_at || "N/A")} | finished ${esc(displayRun.finished_at || "N/A")} | heartbeat ${esc(displayRun.last_heartbeat_at || "N/A")}</div>
                                        <div class="small text-secondary">Window: ${esc(displayRun.start_date || "N/A")} to ${esc(displayRun.end_date || "N/A")} | ${esc(displayRun.symbol || "N/A")} ${esc(displayRun.timeframe || "")}</div>
                                        <div class="small text-secondary">Worker job: ${esc(currentJob?.state || "IDLE")} ${currentJob ? `| #${esc(currentJob.id)}` : ""} | ${esc(currentJob?.progress_current ?? 0)} / ${esc(currentJob?.progress_total ?? 0)}</div>
                                        ${displayRun.failure_reason ? `<div class="small text-danger mt-2">Run failure: ${esc(displayRun.failure_reason)}</div>` : ""}
                                        ${displayRun.interrupted_reason ? `<div class="small text-warning mt-2">Interrupted: ${esc(displayRun.interrupted_reason)}</div>` : ""}
                                        ${currentJob?.failure_reason ? `<div class="small text-danger mt-2">Job failure: ${esc(currentJob.failure_reason)}</div>` : ""}
                                    ` : renderEmptyState("No OOS Run Loaded", "Start a new run or load one from history to inspect scenario-level progress.")}
                                    ${state.oosUi.loading ? `<div class="small text-secondary mt-3">Loading OOS status...</div>` : ""}
                                    ${state.oosUi.error ? `<div class="alert alert-danger mt-3 mb-0 py-2">Failed to load OOS status: ${esc(state.oosUi.error)} <button class="btn btn-sm btn-outline-light ms-2" id="retry-oos-status">Retry</button></div>` : ""}
                                </div></div>
                            </div>
                            <div class="col-12 col-lg-5">
                                <div class="card border-0 shadow-sm h-100"><div class="card-body">
                                    <h3 class="h6 text-uppercase text-secondary fw-semibold mb-3">Final Report And Artifacts</h3>
                                    <div class="small text-secondary">Report status: ${esc(displayRun?.report_status || "PENDING")}</div>
                                    <div class="small text-secondary mt-2">Report path: ${esc(displayRun?.report_path || "Not built yet")}</div>
                                    <div class="small text-secondary mt-2">Output directory: ${esc(displayRun?.output_dir || "Pending")}</div>
                                    <div class="small text-secondary mt-2">Scenario artifacts: ${scenarios.filter((row) => row.export_path || row.report_path).length} available</div>
                                    ${displayRun?.report_error ? `<div class="small text-danger mt-2">Report error: ${esc(displayRun.report_error)}</div>` : ""}
                                    <div class="d-grid gap-2 mt-3">
                                        <button class="btn btn-outline-info" id="oos-rebuild-secondary" ${latestRunId ? "" : "disabled"}>Rebuild Final Report</button>
                                    </div>
                                </div></div>
                            </div>
                            <div class="col-12">
                                <div class="card border-0 shadow-sm"><div class="card-body">
                                    <div class="d-flex justify-content-between align-items-start gap-2 mb-3">
                                        <div>
                                            <h3 class="h6 text-uppercase text-secondary fw-semibold mb-1">Scenario Results</h3>
                                            <div class="small text-secondary">Per-scenario execution mode, status, run linkages, and failure context.</div>
                                        </div>
                                        ${badge(`${scenarios.length} scenarios`, "dark")}
                                    </div>
                                    <div class="table-responsive">
                                        <table class="table table-sm strategy-table">
                                            <thead><tr><th>#</th><th>Scenario</th><th>Mode</th><th>Status</th><th>Run ID</th><th>Signals</th><th>Trades</th><th>Artifact / Report</th><th>Error</th></tr></thead>
                                            <tbody>
                                                ${scenarios.map((row) => `
                                                    <tr>
                                                        <td>${esc(row.scenario_index)}</td>
                                                        <td>${esc(row.scenario_label)}</td>
                                                        <td>${esc(row.resolved_execution_mode.selected_mode || row.metadata_json?.requested_execution_mode || "Unknown")}</td>
                                                        <td>${badge(row.status, stateTone(row.status))}</td>
                                                        <td>${esc(row.run_id || "N/A")}</td>
                                                        <td>${row.signals === null ? "<span class=\"text-secondary\">N/A</span>" : esc(row.signals)}</td>
                                                        <td>${row.trades === null ? "<span class=\"text-secondary\">N/A</span>" : esc(row.trades)}</td>
                                                        <td class="small">${esc(row.report_path || row.export_path || "Pending")}</td>
                                                        <td class="small ${row.error ? "text-danger" : "text-secondary"}">${esc(row.error || "-")}</td>
                                                    </tr>
                                                `).join("") || `<tr><td colspan="9">${renderEmptyState("No Scenario Rows Yet", "Scenario results will appear here after the first OOS run is queued.")}</td></tr>`}
                                            </tbody>
                                        </table>
                                    </div>
                                </div></div>
                            </div>
                            <div class="col-12">
                                <div class="card border-0 shadow-sm"><div class="card-body">
                                    <div class="d-flex justify-content-between align-items-start gap-2 mb-3">
                                        <div>
                                            <h3 class="h6 text-uppercase text-secondary fw-semibold mb-1">OOS History</h3>
                                            <div class="small text-secondary">Recent OOS runs with quick load actions and report availability.</div>
                                        </div>
                                        ${badge(`${history.length} saved`, "dark")}
                                    </div>
                                    <div class="table-responsive">
                                        <table class="table table-sm strategy-table">
                                            <thead><tr><th>ID</th><th>Created</th><th>Window</th><th>State</th><th>Completed</th><th>Report</th><th>Quick Action</th></tr></thead>
                                            <tbody>
                                                ${history.map((row) => `
                                                    <tr>
                                                        <td>${esc(row.id)}</td>
                                                        <td>${esc(row.created_at || "N/A")}</td>
                                                        <td>${esc(row.start_date || "N/A")} to ${esc(row.end_date || "N/A")}</td>
                                                        <td>${badge(row.state, stateTone(row.state))}</td>
                                                        <td>${esc(`${num(row.progress?.completed_scenarios).toFixed(0)} / ${num(row.progress?.total).toFixed(0)}`)}</td>
                                                        <td class="small">${esc(row.report_path || row.output_dir || "Pending")}</td>
                                                        <td><button class="btn btn-sm btn-outline-light" data-oos-run="${row.id}">Load</button></td>
                                                    </tr>
                                                `).join("") || `<tr><td colspan="7" class="text-secondary">No OOS runs saved yet.</td></tr>`}
                                            </tbody>
                                        </table>
                                    </div>
                                    ${state.oosUi.historyError ? `<div class="alert alert-danger mt-3 mb-0 py-2">Failed to load OOS history: ${esc(state.oosUi.historyError)} <button class="btn btn-sm btn-outline-light ms-2" id="retry-oos-history">Retry</button></div>` : ""}
                                    ${!history.length && !state.oosUi.historyError ? loadStateNotice("No OOS history yet. Queue a run to build the first matrix and report.") : ""}
                                </div></div>
                            </div>
                        </div>
                    </div>
                </div>`;

            document.querySelectorAll("#section-oos input, #section-oos select").forEach((node) => {
                node.addEventListener("input", () => {
                    state.oosDraft = {
                        title: val("oos-title"),
                        symbol: val("oos-symbol"),
                        timeframe: val("oos-timeframe"),
                        start_date: val("oos-start"),
                        end_date: val("oos-end"),
                        initial_balance: val("oos-initial-balance"),
                        risk_percent: val("oos-risk-percent"),
                        spread_points: val("oos-spread"),
                        slippage_points: val("oos-slippage"),
                        execution_model: val("oos-execution-model"),
                        baseline_run_id: val("oos-baseline-run-id"),
                        baseline_bundle_path: val("oos-baseline-bundle"),
                        baseline_database_path: val("oos-baseline-db"),
                        allowed_sessions: val("oos-allowed-sessions"),
                        enabled_strategies: strategies.filter((name) => on(`oos-strategy-${name}`)),
                    };
                });
            });

            const runButton = document.getElementById("oos-run");
            if (runButton) {
                runButton.onclick = async () => {
                    if (isReadonly()) {
                        setFeedback("Dashboard is in readonly mode.", true);
                        return;
                    }
                    const payload = {
                        actor: "dashboard",
                        title: val("oos-title"),
                        symbol: val("oos-symbol").toUpperCase(),
                        timeframe: val("oos-timeframe"),
                        start_date: val("oos-start"),
                        end_date: val("oos-end"),
                        initial_balance: num(val("oos-initial-balance"), 10000),
                        risk_percent: num(val("oos-risk-percent"), 0.5),
                        spread_points: num(val("oos-spread"), 20),
                        slippage_points: num(val("oos-slippage"), 2),
                        execution_model: val("oos-execution-model"),
                        baseline_run_id: val("oos-baseline-run-id") ? Number(val("oos-baseline-run-id")) : null,
                        baseline_bundle_path: val("oos-baseline-bundle") || null,
                        baseline_database_path: val("oos-baseline-db") || null,
                        allowed_sessions: csv(val("oos-allowed-sessions")),
                        enabled_strategies: strategies.filter((name) => on(`oos-strategy-${name}`)),
                    };
                    try {
                        dashboardLog("oos", "POST /api/oos/run", payload);
                        setFeedback("Queueing OOS run...");
                        const result = await api("/api/oos/run", { method: "POST", body: JSON.stringify(payload) });
                        if (result.job) {
                            state.jobs = [result.job, ...(state.jobs || []).filter((job) => job.id !== result.job.id)];
                            state.oosUi.selectedRunId = num(result.job.related_run_id || result.job.result_json?.oos_run_id, latestRunId || 0) || state.oosUi.selectedRunId;
                        }
                        await fetchJobs();
                        await fetchOOS();
                        setFeedback(`OOS run queued. Job #${result.job?.id || "N/A"}`);
                        renderAll({ force: true });
                    } catch (error) {
                        setFeedback(error.message, true);
                    }
                };
            }

            const rebuild = async () => {
                if (!latestRunId) return;
                try {
                    setFeedback(`Rebuilding OOS report for #${latestRunId}...`);
                    await api("/api/oos/rebuild-report", { method: "POST", body: JSON.stringify({ actor: "dashboard", oos_run_id: latestRunId }) });
                    await fetchOOS({ runId: latestRunId });
                    setFeedback(`OOS report rebuilt for run #${latestRunId}.`);
                    renderAll({ force: true });
                } catch (error) {
                    setFeedback(error.message, true);
                }
            };

            const resumeButton = document.getElementById("oos-resume");
            if (resumeButton) {
                resumeButton.onclick = async () => {
                    if (!latestRunId) return;
                    try {
                        setFeedback(`Resuming OOS run #${latestRunId}...`);
                        const result = await api("/api/oos/resume", { method: "POST", body: JSON.stringify({ actor: "dashboard", oos_run_id: latestRunId, rerun_partial: true }) });
                        if (result.job) {
                            state.jobs = [result.job, ...(state.jobs || []).filter((job) => job.id !== result.job.id)];
                        }
                        state.oosUi.selectedRunId = latestRunId;
                        await fetchJobs();
                        await fetchOOS({ runId: latestRunId });
                        setFeedback(`OOS resume job queued for run #${latestRunId}.`);
                        renderAll({ force: true });
                    } catch (error) {
                        setFeedback(error.message, true);
                    }
                };
            }

            const rebuildButton = document.getElementById("oos-rebuild");
            if (rebuildButton) rebuildButton.onclick = rebuild;
            const rebuildSecondaryButton = document.getElementById("oos-rebuild-secondary");
            if (rebuildSecondaryButton) rebuildSecondaryButton.onclick = rebuild;

            const retryStatusButton = document.getElementById("retry-oos-status");
            if (retryStatusButton) retryStatusButton.onclick = () => fetchOOS({ runId: latestRunId || null });
            const retryHistoryButton = document.getElementById("retry-oos-history");
            if (retryHistoryButton) retryHistoryButton.onclick = () => fetchOOS({ runId: latestRunId || null });

            document.querySelectorAll("[data-oos-run]").forEach((button) => {
                button.onclick = async () => {
                    const runId = Number(button.dataset.oosRun);
                    if (!runId) return;
                    try {
                        state.oosUi.selectedRunId = runId;
                        await fetchOOS({ runId });
                        setFeedback(`Loaded OOS run #${runId}.`);
                        renderAll({ force: true });
                    } catch (error) {
                        setFeedback(error.message, true);
                    }
                };
            });
        } catch (error) {
            dashboardLog("render", "renderOOS failed", { error: error.message }, "error");
            container.innerHTML = renderErrorState("OOS Panel Failed To Render", error.message, "retry-oos-render");
            const retryRenderButton = document.getElementById("retry-oos-render");
            if (retryRenderButton) {
                retryRenderButton.onclick = () => fetchOOS({ runId: state.oosUi.selectedRunId || null });
            }
        }
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
            api("/api/lebprime/status"),
        ]);
        state.status = statusPayload;
        state.lebprimeLive = lebprimePayload;
        state.workerStatus = state.status.worker || state.workerStatus;
        if (!state.oosUi.selectedRunId && state.status.oos) {
            const embeddedOOS = normalizeOOSPayload(state.status.oos);
            state.oosStatus = embeddedOOS;
            if (!state.oosHistory.length && embeddedOOS.history.length) {
                state.oosHistory = embeddedOOS.history;
            }
        }
        renderAll();
    }
    async function fetchWorkerStatus() {
        const payload = await api("/api/worker/status");
        state.workerStatus = payload.worker || {};
        renderAll();
    }
    async function refreshLogs() {
        const logsPayload = await api(`/api/logs?limit=100&level=${encodeURIComponent(val("log-level"))}&source=${encodeURIComponent(val("log-source"))}&search=${encodeURIComponent(val("log-search"))}&date_from=${encodeURIComponent(val("log-date"))}`);
        const auditPayload = await api("/api/audit?limit=50");
        state.logs = logsPayload.logs || [];
        state.audit = auditPayload.audit || [];
        renderAll();
    }
    async function fetchBacktests() {
        const runsPayload = await api("/api/backtest/runs?limit=20");
        state.backtestRuns = runsPayload.runs || [];
        if (!state.backtestReplay.selectedRunId && state.backtestRuns.length > 0) {
            state.backtestReplay.selectedRunId = Number(state.backtestRuns[0].id) || null;
        }
        if (replayIsVisible() && state.backtestReplay.selectedRunId) {
            const selectedRun = state.backtestRuns.find((row) => num(row.id) === num(state.backtestReplay.selectedRunId)) || null;
            if (selectedRun && (!state.backtestReplay.summary || num(state.backtestReplay.summary.run?.id) !== num(selectedRun.id))) {
                await replayLoadRun(selectedRun.id, { silent: true });
            }
        }
        if (!state.backtestResult && state.backtestRuns.length > 0) {
            try {
                const latestPayload = await api(`/api/backtest/run/${state.backtestRuns[0].id}`);
                state.backtestResult = latestPayload.result || null;
            } catch {
                state.backtestResult = null;
            }
        }
        renderAll();
    }
    async function fetchJobs() {
        const payload = await api("/api/jobs?limit=30");
        state.jobs = payload.jobs || [];
        const latestBacktestJob = latestJobByType("BACKTEST");
        const relatedBacktestRunId = Number(latestBacktestJob?.related_run_id || latestBacktestJob?.result_json?.backtest_run_id || 0);
        if (latestBacktestJob && ["COMPLETED", "PARTIAL"].includes(latestBacktestJob.state) && relatedBacktestRunId > 0) {
            const currentRunId = Number(state.backtestResult?.run?.id || 0);
            if (currentRunId !== relatedBacktestRunId) {
                try {
                    const latestPayload = await api(`/api/backtest/run/${relatedBacktestRunId}`);
                    state.backtestResult = latestPayload.result || null;
                } catch {}
            }
        }
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
    async function fetchOOS(options = {}) {
        const requestedRunId = num(options.runId, 0) || num(state.oosUi.selectedRunId, 0) || null;
        const statusPath = requestedRunId ? `/api/oos/status?oos_run_id=${requestedRunId}` : "/api/oos/status";
        state.oosUi.loading = true;
        state.oosUi.error = null;
        state.oosUi.historyError = null;
        renderAll();
        dashboardLog("oos", "GET OOS payloads", { statusPath, historyPath: "/api/oos/history?limit=10" });
        const [statusResult, historyResult] = await Promise.allSettled([
            api(statusPath),
            api("/api/oos/history?limit=10"),
        ]);
        if (statusResult.status === "fulfilled") {
            state.oosStatus = normalizeOOSPayload(statusResult.value);
            dashboardLog("oos", "oos status payload received", state.oosStatus);
        } else {
            state.oosUi.error = statusResult.reason?.message || "Unknown OOS status error";
            dashboardLog("oos", "oos status load failed", { error: state.oosUi.error }, "error");
        }
        if (historyResult.status === "fulfilled") {
            state.oosHistory = normalizeOOSPayload(historyResult.value).history;
            dashboardLog("oos", "oos history payload received", { rows: state.oosHistory.length });
        } else {
            state.oosUi.historyError = historyResult.reason?.message || "Unknown OOS history error";
            dashboardLog("oos", "oos history load failed", { error: state.oosUi.historyError }, "error");
        }
        if (requestedRunId) {
            state.oosUi.selectedRunId = requestedRunId;
        } else {
            state.oosUi.selectedRunId = num(state.oosStatus.run?.id, 0) || num(state.oosHistory[0]?.id, 0) || null;
        }
        state.oosUi.loading = false;
        if (!state.oosStatus.history?.length && state.oosHistory.length) {
            state.oosStatus.history = state.oosHistory;
        }
        renderAll();
    }
    async function refreshLebprimeReadModels() {
        const [tradesPayload, performancePayload] = await Promise.all([
            api("/api/trades/lebprime?limit_per_category=200"),
            api("/api/performance/lebprime"),
        ]);
        state.lebprimeTrades = tradesPayload;
        state.lebprimePerformance = performancePayload;
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
                            <div class="col-6">${field("il-ds-symbol", "Symbol", draft.dataset.symbol ?? "XAUUSD")}</div>
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
        if (state.activeSection === "lebprime" && sectionName !== "lebprime" && state.strategiesUi.dirty) {
            if (!window.confirm("You have unsaved LebPrime changes. Leave LebPrime and discard them?")) return false;
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
        document.querySelectorAll('[data-bs-target="#research-replay"]').forEach((button) => {
            button.addEventListener("shown.bs.tab", () => {
                if (state.backtestReplay.selectedRunId && (!state.backtestReplay.summary || num(state.backtestReplay.summary.run?.id) !== num(state.backtestReplay.selectedRunId))) {
                    replayLoadRun(state.backtestReplay.selectedRunId, { silent: true }).catch((error) => replaySetMessage(error.message, true));
                }
            });
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
        const preserveBacktestEditing = !options.force && backtestInteractionLocked();
        const preserveIndicatorLabEditing = !options.force && indicatorLabInteractionLocked();
        if (preserveBacktestEditing) {
            renderTopStatusBar();
            renderQuickActions();
            return;
        }
        captureUiState();
        renderTopStatusBar();
        renderQuickActions();
        safeRenderSection("section-overview", "Overview", renderOverview);
        safeRenderSection("section-strategies", "LebPrime", renderStrategies);
        safeRenderSection("section-risk", "Risk Management", renderRisk);
        safeRenderSection("section-alerts", "Alerts", renderAlerts);
        safeRenderSection("section-trades", "Trades", renderTrades);
        safeRenderSection("section-performance", "Performance", renderPerformance);
        if (!preserveBacktestEditing) {
            safeRenderSection("section-backtest", "Backtest", renderBacktest);
        }
        safeRenderSection("section-backtest-replay", "Backtest Replay", renderBacktestReplay);
        if (!preserveIndicatorLabEditing) {
            safeRenderSection("section-indicator-lab", "Indicator Lab", renderIndicatorLab);
        }
        safeRenderSection("section-oos", "OOS", renderOOS);
        safeRenderSection("section-config-core", "Config", renderConfig);
        safeRenderSection("section-logs", "Logs", renderLogs);
        safeRenderSection("section-health", "System Health", renderHealth);
        restoreUiState();
        activateSection(state.activeSection);
        if (isReadonly()) {
            document.querySelectorAll("#section-strategies input, #section-strategies select, #section-strategies button, #section-risk input, #section-risk button, #section-alerts input, #section-alerts button, #section-trades input, #section-trades button, #section-performance input, #section-performance button, #section-backtest input, #section-backtest select, #section-backtest button, #section-indicator-lab input, #section-indicator-lab select, #section-indicator-lab button, #section-oos input, #section-oos select, #section-oos button, #section-config input, #section-config button, #section-config textarea").forEach((node) => {
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
        document.addEventListener("keydown", replayApplyKeyboardShortcuts);
        window.addEventListener("resize", () => {
            if (replayIsVisible()) {
                scheduleReplayRender();
            }
        });
        const loaders = [
            refreshStatus,
            refreshConfig,
            refreshLebprimeReadModels,
            refreshLogs,
            fetchBacktests,
            fetchJobs,
            fetchWorkerStatus,
            fetchIndicatorLab,
            fetchOOS,
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
        setInterval(refreshLebprimeReadModels, Math.max(5, refreshSeconds) * 1000);
        setInterval(refreshLogs, 15000);
        setInterval(fetchBacktests, 30000);
        setInterval(fetchJobs, 5000);
        setInterval(fetchWorkerStatus, 5000);
        setInterval(fetchIndicatorLab, 10000);
        setInterval(fetchOOS, 5000);
    }

    document.addEventListener("DOMContentLoaded", boot);
})();
