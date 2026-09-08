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
        modeControl: { mode: null, options: [], loading: false, error: null },
        oosStatus: { run: null, history: [], active_run_id: null, state: "IDLE" },
        oosHistory: [],
        oosUi: { loading: false, error: null, historyError: null, selectedRunId: null },
        oosDraft: {},
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
    const replayIsVisible = () => state.activeSection === "backtest-replay";
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
                state.activeSection = "backtest-replay";
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
    const currentExecutionMode = () => normalizeModeData(state.modeControl.mode || state.config.mode || state.status.execution_mode || state.config.bot?.execution_mode_summary || {});
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
        if (restartCount > 0 && hotCount > 0) return `Hot-applied ${hotCount} change(s); ${restartCount} change(s) saved and pending restart.`;
        if (restartCount > 0) return `${restartCount} change(s) saved and pending restart.`;
        if (hotCount > 0) return `Hot-applied ${hotCount} change(s).`;
        return "No effective config changes.";
    };
    const latestJobByType = (jobType) => (state.jobs || []).find((job) => job.job_type === jobType) || null;
    const currentWorkerStatus = () => asObject(state.workerStatus || state.status.worker || {});
    const workerStateLabel = (worker = currentWorkerStatus()) => {
        const stateValue = textOr(worker.state || worker.status, "unavailable").toLowerCase();
        if (stateValue === "running") return "Worker Running";
        if (stateValue === "stale") return "Worker Stale";
        return "Worker Not Running";
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
            return;
        }
        try {
            const preview = await api("/api/config/preview", { method: "POST", body: JSON.stringify({ actor: "dashboard", updates, preview_only: true, reason }) });
            const summary = (preview.changes || []).slice(0, 12).map((entry) => `${entry.path}: ${JSON.stringify(entry.before)} -> ${JSON.stringify(entry.after)}`).join("\n");
            if (summary && !window.confirm(`Apply these config changes?\n\n${summary}`)) return;
            const result = await api("/api/config", { method: "POST", body: JSON.stringify({ actor: "dashboard", updates, reason }) });
            setFeedback(`${applyPlanMessage(result)} Backup: ${result.backup_path}`);
            await refreshConfig();
            await refreshStatus();
            await refreshLogs();
        } catch (error) {
            setFeedback(error.message, true);
        }
    }

    async function runControl(path, payload) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return;
        }
        try {
            const result = await api(path, { method: "POST", body: JSON.stringify({ actor: "dashboard", ...payload }) });
            setFeedback(result.message || "Action completed");
            await refreshStatus();
        } catch (error) {
            setFeedback(error.message, true);
        }
    }

    async function runManual(path, payload = {}, refresh = true) {
        if (isReadonly() && path !== "/api/manual/positions") {
            setFeedback("Dashboard is in readonly mode.", true);
            return;
        }
        try {
            const result = path === "/api/manual/positions"
                ? await api(path)
                : await api(path, { method: "POST", body: JSON.stringify({ actor: "dashboard", ...payload }) });
            setFeedback(result.message || "Manual action completed");
            if (refresh) {
                await refreshPositions();
                await refreshStatus();
                await refreshLogs();
            }
            return result;
        } catch (error) {
            setFeedback(error.message, true);
            throw error;
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
        const mode = runtimeControl.mode || state.config.bot?.trading_mode || "N/A";
        const botRunning = Boolean(control.process_alive || control.bot_running);
        const mt5Connected = botRunning ? Boolean(runtimeControl.mt5_connected) : null;
        const mt5Label = mt5Connected === null ? "UNKNOWN" : (mt5Connected ? "CONNECTED" : "DOWN");
        const mt5Tone = mt5Connected === null ? "secondary" : (mt5Connected ? "success" : "danger");
        const worker = currentWorkerStatus();
        document.getElementById("top-status-bar").innerHTML = `
            <div class="status-chip"><span class="badge text-bg-${control.process_alive || control.bot_running ? "success" : "danger"}">Bot ${control.process_alive || control.bot_running ? "RUNNING" : "STOPPED"}</span></div>
            <div class="status-chip"><span class="badge text-bg-${mode === "LIVE" ? "danger" : "info"}">${esc(mode)}</span></div>
            <div class="status-chip"><span class="badge text-bg-secondary">${esc(executionModeLabel())}</span></div>
            <div class="status-chip"><span class="badge text-bg-${mt5Tone}">MT5 ${mt5Label}</span></div>
            <div class="status-chip"><span class="badge text-bg-${workerTone(worker)}">${esc(workerStateLabel(worker))}</span></div>
            <div class="status-chip"><span class="badge text-bg-${control.kill_switch ? "danger" : "success"}">${control.kill_switch ? "EMERGENCY" : "NORMAL"}</span></div>
            ${control.pending_restart ? `<div class="status-chip"><span class="badge text-bg-warning">RESTART PENDING</span></div>` : ""}
            <div class="status-chip"><span class="badge text-bg-${isReadonly() ? "warning" : "secondary"}">${isReadonly() ? "READONLY" : "READ-WRITE"}</span></div>
            <div class="status-chip">Heartbeat: ${esc(runtimeControl.heartbeat_at || "N/A")}</div>`;
    }

    function renderQuickActions() {
        document.getElementById("quick-actions").innerHTML = `
            <button class="btn btn-success" id="qa-start">Start</button>
            <button class="btn btn-outline-light" id="qa-stop">Stop</button>
            <button class="btn btn-outline-warning" id="qa-pause">Pause</button>
            <button class="btn btn-outline-info" id="qa-resume">Resume</button>
            <button class="btn btn-outline-primary" id="qa-dry">Switch To Dry</button>
            <button class="btn btn-danger" id="qa-live">Switch To Live</button>
            <button class="btn btn-outline-danger" id="qa-kill">Kill Switch</button>
            <button class="btn btn-outline-success" id="qa-safe">Safe Mode</button>
            <button class="btn btn-outline-info" id="qa-balanced">Balanced</button>
            <button class="btn btn-outline-warning" id="qa-scalping">Scalping</button>`;
        document.getElementById("qa-start").onclick = () => runControl("/api/control/start", { enabled: true });
        document.getElementById("qa-stop").onclick = () => runControl("/api/control/stop", { enabled: true });
        document.getElementById("qa-pause").onclick = () => runControl("/api/control/pause", { enabled: true });
        document.getElementById("qa-resume").onclick = () => runControl("/api/control/resume", { enabled: true });
        document.getElementById("qa-dry").onclick = () => switchMode("DRY_RUN");
        document.getElementById("qa-live").onclick = () => switchMode("LIVE");
        document.getElementById("qa-kill").onclick = async () => {
            const enabled = !(state.status.control?.kill_switch);
            if (enabled && !window.confirm("Enable the kill switch? This will block all new trades.")) return;
            await runControl("/api/control/kill-switch", { enabled, reason: enabled ? "dashboard_kill_switch" : null });
        };
        document.getElementById("qa-safe").onclick = () => applyPreset("SAFE_MODE");
        document.getElementById("qa-balanced").onclick = () => applyPreset("BALANCED_MODE");
        document.getElementById("qa-scalping").onclick = () => applyPreset("SCALPING_MODE");
        if (isReadonly()) {
            document.querySelectorAll("#quick-actions button").forEach((button) => { button.disabled = true; });
        }
    }

    async function applyPreset(presetName) {
        if (isReadonly()) {
            setFeedback("Dashboard is in readonly mode.", true);
            return;
        }
        const presetLabels = { SAFE_MODE: "safe mode", BALANCED_MODE: "balanced mode", SCALPING_MODE: "scalping mode", AGGRESSIVE_MODE: "aggressive mode" };
        const human = presetLabels[presetName] || presetName.toLowerCase();
        if (!window.confirm(`Apply the ${human} preset?`)) return;
        try {
            const result = await api("/api/config/preset", { method: "POST", body: JSON.stringify({ actor: "dashboard", preset_name: presetName }) });
            setFeedback(`Preset applied. ${applyPlanMessage(result)}`);
            await refreshConfig();
            await refreshStatus();
        } catch (error) {
            setFeedback(error.message, true);
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
                    ${metric("Current Mode", runtimeControl.mode || state.config.bot?.trading_mode || "N/A", (runtimeControl.mode || state.config.bot?.trading_mode) === "LIVE")}
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
                <div class="col-12 col-xl-4"><div class="card border-0 shadow-sm ${((runtimeControl.mode || state.config.bot?.trading_mode) === "LIVE") ? "live-warning" : ""}"><div class="card-body">
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

    function renderBotControl() {
        const bot = state.config.bot || {};
        const execution = state.config.execution || {};
        const control = state.status.control || {};
        const entryModes = state.config.strategy?.entry_modes || {};
        const executionMode = currentExecutionMode();
        const options = normalizeModeOptions(state.modeControl.options);
        const selectedExecutionMode = executionMode.selected_mode || "V2_FULL";
        const container = safeSetHtml("section-bot-control", `
            <div class="row g-3">
                <div class="col-12 col-xl-7"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Mode And Execution</h2>
                    ${select("bot-trading-mode", "Trading Mode", ["DRY_RUN", "LIVE", "DEMO", "VALIDATION_TEST", "BACKTEST"], bot.trading_mode)}
                    <div class="dashboard-subpanel mb-3">
                        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-3">
                            <div>
                                <div class="text-uppercase small text-secondary fw-semibold">Execution Mode</div>
                                <div class="small text-secondary">Switch between the baseline engine and the full V2 orchestration path.</div>
                            </div>
                            ${badge(executionMode.selected_mode || "UNKNOWN", stateTone(executionMode.selected_mode))}
                        </div>
                        <div class="dashboard-mode-switch" role="radiogroup" aria-label="Execution Mode">
                            ${options.map((option, index) => `
                                <input class="btn-check" type="radio" name="bot-execution-mode" id="bot-execution-mode-${index}" value="${esc(option.value)}" ${option.value === selectedExecutionMode ? "checked" : ""}>
                                <label class="btn btn-outline-light" for="bot-execution-mode-${index}">
                                    <span class="d-block fw-semibold">${esc(option.value)}</span>
                                    <span class="small text-secondary">${esc(option.label)}</span>
                                </label>
                            `).join("")}
                        </div>
                        <div class="dashboard-summary-card mt-3">
                            <div class="small text-uppercase text-secondary fw-semibold mb-2">Resolved Summary</div>
                            ${renderExecutionModeSummary(executionMode)}
                        </div>
                        ${state.modeControl.loading ? `<div class="small text-secondary mt-3">Loading execution mode from /api/mode...</div>` : ""}
                        ${state.modeControl.error ? `<div class="alert alert-danger mt-3 mb-0 py-2">${esc(state.modeControl.error)} <button class="btn btn-sm btn-outline-light ms-2" id="retry-execution-mode">Retry</button></div>` : ""}
                    </div>
                    ${field("bot-rollout-phase", "Rollout Phase", bot.rollout_phase, "number", "1")}
                    ${toggle("bot-force-reduced-risk", "Force Reduced Risk Mode", !!bot.force_reduced_risk_mode)}
                    <div class="small text-secondary">Allow Live Execution: ${bot.allow_live_execution ? "Yes" : "No"} (derived from trading mode)</div>
                    ${toggle("control-auto-execution", "Auto Execution Enabled", !(control.auto_execution_enabled === false))}
                    ${toggle("control-new-entries", "New Entries / Signals Enabled", !(control.signal_generation_enabled === false))}
                    ${toggle("manual-trading-enabled", "Manual Trading Enabled", execution.manual_trading_enabled !== false)}
                    ${field("execution-order-comment", "Order Comment Tag", execution.order_comment_tag || "", "text")}
                    ${field("execution-slippage", "Slippage Tolerance", state.config.mt5?.deviation || 10, "number", "1")}
                    ${field("entry-confirmed-score", "Confirmed Entry Score", entryModes.confirmed_min_entry_score || 78, "number", "0.1")}
                    ${field("entry-aggressive-score", "Aggressive Entry Score", entryModes.aggressive_min_entry_score || 88, "number", "0.1")}
                    <button class="btn btn-primary mt-3" id="save-bot-control">Save Mode And Controls</button>
                </div></div></div>
                <div class="col-12 col-xl-5"><div class="card border-0 shadow-sm"><div class="card-body">
                    <h2 class="h5">Runtime Controls</h2>
                    ${toggle("control-paused", "Pause Execution", !!control.execution_paused)}
                    ${toggle("control-kill-switch", "Kill Switch Enabled", !!control.kill_switch)}
                    ${toggle("dashboard-readonly-mode", "Readonly Dashboard", !!state.config.dashboard?.readonly_mode)}
                    <div class="small text-secondary mt-2">Auth ${state.auth?.enabled ? `enabled for ${esc(state.auth.username || "configured user")}` : "disabled"}</div>
                    <button class="btn btn-outline-light mt-3" id="apply-runtime-control">Apply Runtime Controls</button>
                </div></div></div>
            </div>`);
        if (!container) return;
        dashboardLog("render", "renderBotControl started", { container: "section-bot-control", selectedMode: selectedExecutionMode });
        const retryModeButton = document.getElementById("retry-execution-mode");
        if (retryModeButton) {
            retryModeButton.onclick = () => refreshExecutionMode();
        }
        document.getElementById("save-bot-control").onclick = async () => {
            const desiredMode = val("bot-trading-mode");
            const desiredExecutionMode = radioValue("bot-execution-mode", selectedExecutionMode);
            if (desiredMode !== bot.trading_mode) {
                const modeApplied = await switchMode(desiredMode);
                if (!modeApplied) {
                    return;
                }
            }
            let modeUpdated = false;
            if (desiredExecutionMode !== selectedExecutionMode) {
                try {
                    dashboardLog("mode", "POST /api/mode", { execution_mode: desiredExecutionMode });
                    const modeResult = await api("/api/mode", { method: "POST", body: JSON.stringify({ actor: "dashboard", execution_mode: desiredExecutionMode }) });
                    const normalizedMode = normalizeModeData(modeResult.mode || modeResult);
                    state.config.mode = normalizedMode;
                    state.modeControl.mode = normalizedMode;
                    state.modeControl.options = normalizeModeOptions(modeResult.options);
                    state.modeControl.error = null;
                    modeUpdated = true;
                    dashboardLog("mode", "execution mode payload received", normalizedMode);
                } catch (error) {
                    state.modeControl.error = error.message;
                    setFeedback(error.message, true);
                    renderAll({ force: true });
                    return;
                }
            }
            const updates = {
                bot: { rollout_phase: num(val("bot-rollout-phase")), force_reduced_risk_mode: on("bot-force-reduced-risk") },
                mt5: { deviation: num(val("execution-slippage")) },
                execution: { order_comment_tag: val("execution-order-comment"), manual_trading_enabled: on("manual-trading-enabled") },
                strategy: { entry_modes: { confirmed_min_entry_score: num(val("entry-confirmed-score")), aggressive_min_entry_score: num(val("entry-aggressive-score")) } },
            };
            const needsPatch = (
                num(bot.rollout_phase) !== num(updates.bot.rollout_phase)
                || !!bot.force_reduced_risk_mode !== !!updates.bot.force_reduced_risk_mode
                || num(state.config.mt5?.deviation) !== num(updates.mt5.deviation)
                || textOr(execution.order_comment_tag) !== textOr(updates.execution.order_comment_tag)
                || !!(execution.manual_trading_enabled !== false) !== !!updates.execution.manual_trading_enabled
                || num(entryModes.confirmed_min_entry_score) !== num(updates.strategy.entry_modes.confirmed_min_entry_score)
                || num(entryModes.aggressive_min_entry_score) !== num(updates.strategy.entry_modes.aggressive_min_entry_score)
            );
            if (needsPatch) {
                await saveConfigPatch(updates, "bot_control_update");
            } else if (modeUpdated) {
                setFeedback(`Execution mode saved as ${desiredExecutionMode}.`);
                await refreshConfig();
                await refreshStatus();
            } else {
                setFeedback("No bot-control changes detected.");
            }
        };
        document.getElementById("apply-runtime-control").onclick = async () => {
            await runControl(on("control-paused") ? "/api/control/pause" : "/api/control/resume", { enabled: true });
            await runControl("/api/runtime/live-enabled", { enabled: on("control-auto-execution") });
            await runControl("/api/runtime/new-entries", { enabled: on("control-new-entries") });
            await runControl("/api/control/kill-switch", { enabled: on("control-kill-switch"), reason: on("control-kill-switch") ? "dashboard_kill_switch" : null });
            try {
                await api("/api/control/readonly", { method: "POST", body: JSON.stringify({ actor: "dashboard", enabled: on("dashboard-readonly-mode") }) });
                await refreshConfig();
                await refreshStatus();
            } catch (error) {
                setFeedback(error.message, true);
            }
        };
    }

    function renderFamilies() {
        const families = state.config.strategy?.setup_families || {};
        const familyBlocks = latestBlockLookup(state.status.runtime?.family_block_matrix || []);
        document.getElementById("section-families").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <h2 class="h5">Strategy Families</h2>
                <div class="table-responsive"><table class="table strategy-table"><thead><tr><th>Family</th><th>Enabled</th><th>Live Allowed</th><th>Sessions</th><th>Regimes</th><th>Recent Blocks</th></tr></thead>
                <tbody>${Object.entries(families).map(([key, family]) => `<tr><td>${esc(key)}</td><td><input type="checkbox" data-family-enabled="${key}" ${family.enabled ? "checked" : ""}></td><td><input type="checkbox" data-family-live="${key}" ${family.live_allowed ? "checked" : ""}></td><td>${small(`family-sessions-${key}`, (family.sessions || []).join(", "))}</td><td>${small(`family-regimes-${key}`, (family.regimes || []).join(", "))}</td><td class="small text-secondary">${esc(familyBlocks[key] || "none")}</td></tr>`).join("")}</tbody></table></div>
                <button class="btn btn-primary" id="save-families">Save Family Controls</button>
            </div></div>`;
        document.getElementById("save-families").onclick = async () => {
            const updates = { strategy: { setup_families: {} } };
            Object.keys(families).forEach((key) => {
                updates.strategy.setup_families[key] = { ...families[key], enabled: document.querySelector(`[data-family-enabled="${key}"]`).checked, live_allowed: document.querySelector(`[data-family-live="${key}"]`).checked, sessions: csv(val(`family-sessions-${key}`)), regimes: csv(val(`family-regimes-${key}`)) };
            });
            await saveConfigPatch(updates, "family_toggle_update");
        };
    }

    function renderStrategies() {
        const controls = state.config.strategy?.setup_controls || {};
        const allSessions = [...new Set(Object.values(controls).flatMap((setup) => setup.allowed_sessions || []))].sort();
        const strategyBlocks = latestBlockLookup(state.status.runtime?.strategy_block_matrix || []);
        document.getElementById("section-strategies").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <div class="row g-2 align-items-end mb-3">
                    <div class="col-md-4"><label class="form-label small text-secondary">Search</label><input class="form-control" id="strategy-filter" placeholder="Search strategy"></div>
                    <div class="col-md-2"><label class="form-label small text-secondary">Enabled</label><select class="form-select" id="strategy-enabled-filter"><option value="">All</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option></select></div>
                    <div class="col-md-2"><label class="form-label small text-secondary">Direction</label><select class="form-select" id="strategy-direction-filter"><option value="">Any</option><option value="long">Long Enabled</option><option value="short">Short Enabled</option><option value="both">Both Enabled</option></select></div>
                    <div class="col-md-2"><label class="form-label small text-secondary">Session</label><select class="form-select" id="strategy-session-filter"><option value="">All</option>${allSessions.map((session) => `<option>${esc(session)}</option>`).join("")}</select></div>
                    <div class="col-md-2"><button class="btn btn-outline-light w-100" id="strategy-apply-filter">Filter</button></div>
                </div>
                <div class="table-responsive"><table class="table strategy-table" id="strategies-table"><thead><tr><th>Strategy</th><th>Enabled</th><th>Confirm</th><th>Trend Align</th><th>Trend</th><th>Setup</th><th>Trigger</th><th>Entry</th><th>Spread</th><th>SLx</th><th>TPx</th><th>Cooldown</th><th>Max/Session</th><th>Sessions</th><th>Regimes</th><th>Recent Blocks</th><th>Long</th><th>Short</th></tr></thead>
                <tbody>${Object.entries(controls).map(([key, setup]) => `<tr data-strategy="${key}" data-enabled="${setup.enabled ? "enabled" : "disabled"}" data-long="${setup.allow_long ? "1" : "0"}" data-short="${setup.allow_short ? "1" : "0"}" data-sessions="${esc((setup.allowed_sessions || []).join('|'))}"><td>${esc(key)}</td><td><input type="checkbox" data-setup-enabled="${key}" ${setup.enabled ? "checked" : ""}></td><td><input type="checkbox" data-setup-confirm="${key}" ${setup.confirmation_required !== false ? "checked" : ""}></td><td><input type="checkbox" data-setup-trend-align="${key}" ${setup.require_trend_alignment !== false ? "checked" : ""}></td><td>${small(`setup-min-trend-${key}`, setup.min_trend_score ?? "", "number", "0.1")}</td><td>${small(`setup-min-setup-${key}`, setup.min_setup_score ?? "", "number", "0.1")}</td><td>${small(`setup-min-trigger-${key}`, setup.min_trigger_score ?? "", "number", "0.1")}</td><td>${small(`setup-min-entry-${key}`, setup.min_entry_score ?? 75, "number", "0.1")}</td><td>${small(`setup-spread-${key}`, setup.max_spread_points ?? 25, "number", "0.1")}</td><td>${small(`setup-sl-${key}`, setup.sl_multiplier ?? 1, "number", "0.1")}</td><td>${small(`setup-tp-${key}`, setup.tp_multiplier ?? 1, "number", "0.1")}</td><td>${small(`setup-cooldown-${key}`, setup.cooldown_minutes ?? 20, "number", "1")}</td><td>${small(`setup-max-session-${key}`, setup.max_trades_per_session ?? 2, "number", "1")}</td><td>${small(`setup-sessions-${key}`, (setup.allowed_sessions || []).join(", "))}</td><td>${small(`setup-regimes-${key}`, (setup.allowed_regimes || []).join(", "))}</td><td class="small text-secondary">${esc(strategyBlocks[key] || "none")}</td><td><input type="checkbox" data-setup-long="${key}" ${setup.allow_long ? "checked" : ""}></td><td><input type="checkbox" data-setup-short="${key}" ${setup.allow_short ? "checked" : ""}></td></tr>`).join("")}</tbody></table></div>
                <button class="btn btn-primary" id="save-strategies">Save Strategy Controls</button>
            </div></div>`;

        const applyFilter = () => {
            const query = val("strategy-filter").toLowerCase();
            const enabledFilter = val("strategy-enabled-filter");
            const directionFilter = val("strategy-direction-filter");
            const sessionFilter = val("strategy-session-filter");
            document.querySelectorAll("#strategies-table tbody tr").forEach((row) => {
                const longEnabled = row.dataset.long === "1";
                const shortEnabled = row.dataset.short === "1";
                const matchDirection = !directionFilter || (directionFilter === "long" && longEnabled) || (directionFilter === "short" && shortEnabled) || (directionFilter === "both" && longEnabled && shortEnabled);
                const sessionMatches = !sessionFilter || (row.dataset.sessions || "").split("|").includes(sessionFilter);
                const match = row.dataset.strategy.toLowerCase().includes(query) && (!enabledFilter || row.dataset.enabled === enabledFilter) && matchDirection && sessionMatches;
                row.classList.toggle("d-none", !match);
            });
        };
        document.getElementById("strategy-apply-filter").onclick = applyFilter;
        document.getElementById("strategy-filter").addEventListener("input", applyFilter);
        document.getElementById("save-strategies").onclick = async () => {
            const updates = { strategy: { setup_controls: {} } };
            Object.keys(controls).forEach((key) => {
                updates.strategy.setup_controls[key] = {
                    ...controls[key],
                    enabled: document.querySelector(`[data-setup-enabled="${key}"]`).checked,
                    confirmation_required: document.querySelector(`[data-setup-confirm="${key}"]`).checked,
                    require_trend_alignment: document.querySelector(`[data-setup-trend-align="${key}"]`).checked,
                    min_trend_score: val(`setup-min-trend-${key}`) === "" ? null : num(val(`setup-min-trend-${key}`)),
                    min_setup_score: val(`setup-min-setup-${key}`) === "" ? null : num(val(`setup-min-setup-${key}`)),
                    min_trigger_score: val(`setup-min-trigger-${key}`) === "" ? null : num(val(`setup-min-trigger-${key}`)),
                    min_entry_score: num(val(`setup-min-entry-${key}`)),
                    max_spread_points: num(val(`setup-spread-${key}`)),
                    sl_multiplier: num(val(`setup-sl-${key}`), 1),
                    tp_multiplier: num(val(`setup-tp-${key}`), 1),
                    cooldown_minutes: num(val(`setup-cooldown-${key}`)),
                    max_trades_per_session: num(val(`setup-max-session-${key}`), 2),
                    allowed_sessions: csv(val(`setup-sessions-${key}`)),
                    allowed_regimes: csv(val(`setup-regimes-${key}`)),
                    allow_long: document.querySelector(`[data-setup-long="${key}"]`).checked,
                    allow_short: document.querySelector(`[data-setup-short="${key}"]`).checked,
                };
            });
            await saveConfigPatch(updates, "strategy_control_update");
        };
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

    function renderSessions() {
        const sessions = state.config.sessions || {};
        const buckets = sessions.buckets || [];
        document.getElementById("section-sessions").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <h2 class="h5">Session Controls</h2>
                <div class="row g-3 mb-3"><div class="col-md-4">${toggle("allow-asia-session", "Allow Asia Session", !!sessions.allow_asia_session)}</div><div class="col-md-4">${field("session-close", "Session Close", sessions.session_close || "21:40", "text")}</div></div>
                <div class="table-responsive"><table class="table"><thead><tr><th>Session</th><th>Start</th><th>End</th><th>Enabled</th></tr></thead><tbody>${buckets.map((bucket, index) => `<tr><td>${esc(bucket.name)}</td><td>${small(`session-start-${index}`, bucket.start)}</td><td>${small(`session-end-${index}`, bucket.end)}</td><td><input type="checkbox" data-session-enabled="${index}" ${bucket.live_allowed ? "checked" : ""}></td></tr>`).join("")}</tbody></table></div>
                <button class="btn btn-primary" id="save-sessions">Save Sessions</button>
            </div></div>`;
        document.getElementById("save-sessions").onclick = async () => {
            const updatedBuckets = buckets.map((bucket, index) => ({ ...bucket, start: val(`session-start-${index}`), end: val(`session-end-${index}`), live_allowed: document.querySelector(`[data-session-enabled="${index}"]`).checked }));
            await saveConfigPatch({ sessions: { allow_asia_session: on("allow-asia-session"), session_close: val("session-close"), buckets: updatedBuckets, live_allowed_buckets: updatedBuckets.filter((bucket) => bucket.live_allowed).map((bucket) => bucket.name) } }, "session_update");
        };
    }

    function renderSymbols() {
        const symbols = state.config.symbols || {};
        document.getElementById("section-symbols").innerHTML = `
            <div class="card border-0 shadow-sm"><div class="card-body">
                <h2 class="h5">Symbol Controls</h2>
                <div class="table-responsive"><table class="table"><thead><tr><th>Symbol</th><th>Enabled</th><th>Tradeable</th><th>Display</th></tr></thead><tbody>${Object.entries(symbols).map(([symbol, cfg]) => `<tr><td>${esc(symbol)}</td><td><input type="checkbox" data-symbol-enabled="${symbol}" ${cfg.enabled ? "checked" : ""}></td><td><input type="checkbox" data-symbol-tradeable="${symbol}" ${cfg.tradeable !== false ? "checked" : ""}></td><td>${esc(cfg.display_name || symbol)}</td></tr>`).join("")}</tbody></table></div>
                <button class="btn btn-primary" id="save-symbols">Save Symbols</button>
            </div></div>`;
        document.getElementById("save-symbols").onclick = async () => {
            const updates = { symbols: {} };
            Object.entries(symbols).forEach(([symbol, cfg]) => {
                updates.symbols[symbol] = { ...cfg, enabled: document.querySelector(`[data-symbol-enabled="${symbol}"]`).checked, tradeable: document.querySelector(`[data-symbol-tradeable="${symbol}"]`).checked };
            });
            await saveConfigPatch(updates, "symbol_update");
        };
    }

    function renderManualTrading() {
        const symbol = val("manual-symbol") || state.config.mt5?.symbol || "XAUUSD";
        const currentSide = val("manual-side") || "BUY";
        const currentVolume = val("manual-volume") || "0.01";
        const currentSl = val("manual-sl") || "";
        const currentTp = val("manual-tp") || "";
        const currentComment = val("manual-comment") || "";
        const positions = state.positions || [];
        document.getElementById("section-manual-trading").innerHTML = `
            <div class="row g-3">
                <div class="col-12 col-xl-4">
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <h2 class="h5">Open Manual Trade</h2>
                        <div class="small text-secondary mb-3">Manual orders use the same MT5 execution path, validations, and audit trail as Telegram remote control.</div>
                        ${field("manual-symbol", "Symbol", symbol, "text")}
                        ${select("manual-side", "Side", ["BUY", "SELL"], currentSide)}
                        ${field("manual-volume", "Volume", currentVolume, "number", "0.01")}
                        ${field("manual-sl", "Stop Loss", currentSl, "number", "0.01")}
                        ${field("manual-tp", "Take Profit", currentTp, "number", "0.01")}
                        ${field("manual-comment", "Comment", currentComment, "text")}
                        <div class="d-flex gap-2 mt-3">
                            <button class="btn btn-primary" id="manual-open-trade">Open Trade</button>
                            <button class="btn btn-outline-light" id="manual-refresh-positions">Refresh Positions</button>
                        </div>
                        <button class="btn btn-outline-danger mt-3" id="manual-close-all">Emergency Close All Positions</button>
                    </div></div>
                </div>
                <div class="col-12 col-xl-8">
                    <div class="card border-0 shadow-sm"><div class="card-body">
                        <div class="d-flex justify-content-between align-items-center gap-2 mb-3">
                            <div>
                                <h2 class="h5 mb-1">Open Positions</h2>
                                <div class="small text-secondary">Bot-managed live positions on the configured symbol.</div>
                            </div>
                            <div class="badge text-bg-secondary">${positions.length} open</div>
                        </div>
                        <div class="table-responsive">
                            <table class="table strategy-table">
                                <thead>
                                    <tr><th>Ticket</th><th>Symbol</th><th>Side</th><th>Volume</th><th>Open</th><th>SL</th><th>TP</th><th>Profit</th><th>Opened</th><th>Source</th><th>Actions</th></tr>
                                </thead>
                                <tbody>
                                    ${positions.map((position) => `
                                        <tr>
                                            <td>${esc(position.ticket)}</td>
                                            <td>${esc(position.symbol)}</td>
                                            <td>${esc(position.side)}</td>
                                            <td>${num(position.volume).toFixed(2)}</td>
                                            <td>${num(position.open_price).toFixed(5)}</td>
                                            <td>${num(position.sl).toFixed(5)}</td>
                                            <td>${num(position.tp).toFixed(5)}</td>
                                            <td class="${num(position.profit) < 0 ? "text-danger" : "text-success"}">${num(position.profit).toFixed(2)}</td>
                                            <td>${esc(position.opened_at)}</td>
                                            <td>${esc(position.comment || position.source || "manual")}</td>
                                            <td class="d-flex flex-wrap gap-2">
                                                <button class="btn btn-sm btn-outline-danger" data-manual-close="${position.ticket}">Close</button>
                                                <button class="btn btn-sm btn-outline-warning" data-manual-partial="${position.ticket}">Partial</button>
                                                <button class="btn btn-sm btn-outline-info" data-manual-modify="${position.ticket}">Edit SL/TP</button>
                                                <button class="btn btn-sm btn-outline-success" data-manual-breakeven="${position.ticket}">Break-even</button>
                                            </td>
                                        </tr>
                                    `).join("") || `<tr><td colspan="11" class="text-secondary">No open positions.</td></tr>`}
                                </tbody>
                            </table>
                        </div>
                    </div></div>
                </div>
            </div>`;

        document.getElementById("manual-refresh-positions").onclick = () => refreshPositions();
        document.getElementById("manual-open-trade").onclick = async () => {
            await runManual("/api/manual/open", {
                symbol: val("manual-symbol"),
                side: val("manual-side"),
                volume: num(val("manual-volume")),
                sl: val("manual-sl") === "" ? null : num(val("manual-sl")),
                tp: val("manual-tp") === "" ? null : num(val("manual-tp")),
                comment: val("manual-comment") || null,
            });
        };
        document.getElementById("manual-close-all").onclick = async () => {
            if (!window.confirm("Close all open bot-managed positions now?")) return;
            await runManual("/api/manual/close-all", { enabled: true });
        };
        document.querySelectorAll("[data-manual-close]").forEach((button) => {
            button.onclick = async () => {
                const ticket = Number(button.dataset.manualClose);
                if (!window.confirm(`Close position ${ticket}?`)) return;
                await runManual("/api/manual/close", { ticket });
            };
        });
        document.querySelectorAll("[data-manual-partial]").forEach((button) => {
            button.onclick = async () => {
                const ticket = Number(button.dataset.manualPartial);
                const volume = window.prompt(`Enter partial-close volume for position ${ticket}`);
                if (volume === null) return;
                await runManual("/api/manual/partial-close", { ticket, volume: num(volume) });
            };
        });
        document.querySelectorAll("[data-manual-modify]").forEach((button) => {
            button.onclick = async () => {
                const ticket = Number(button.dataset.manualModify);
                const sl = window.prompt(`Enter new SL for position ${ticket} (leave blank to keep current)`, "");
                if (sl === null) return;
                const tp = window.prompt(`Enter new TP for position ${ticket} (leave blank to keep current)`, "");
                if (tp === null) return;
                await runManual("/api/manual/modify", { ticket, sl: sl === "" ? null : num(sl), tp: tp === "" ? null : num(tp) });
            };
        });
        document.querySelectorAll("[data-manual-breakeven]").forEach((button) => {
            button.onclick = async () => {
                const ticket = Number(button.dataset.manualBreakeven);
                if (!window.confirm(`Move position ${ticket} stop loss to break-even?`)) return;
                await runManual("/api/manual/breakeven", { ticket });
            };
        });
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
                        <div class="small text-secondary mb-2">Engine Mode Snapshot: ${esc(executionModeLabel())}</div>
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
                    engine_mode: currentExecutionMode().selected_mode || "V2_FULL",
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
            const defaultStrategies = asArray(runRequest.enabled_strategies).length ? asArray(runRequest.enabled_strategies) : (currentExecutionMode().enabled_strategy_names.length ? currentExecutionMode().enabled_strategy_names : strategies);
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
                            <div class="dashboard-summary-card mt-3">
                                <div class="small text-uppercase text-secondary fw-semibold mb-2">Execution Snapshot</div>
                                ${renderExecutionModeSummary(currentExecutionMode())}
                            </div>
                            <div class="small text-secondary mt-3">Matrix: baseline and V2 comparison scenarios with final report rebuild support.</div>
                            <div class="small text-secondary">Current dashboard mode: ${esc(executionModeLabel())}</div>
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
                        engine_mode: currentExecutionMode().selected_mode || "V2_FULL",
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
        if (!state.modeControl.mode && state.config.mode) {
            state.modeControl.mode = normalizeModeData(state.config.mode);
        }
        renderAll();
    }
    async function refreshStatus() {
        state.status = await api("/api/status");
        state.workerStatus = state.status.worker || state.workerStatus;
        if (!state.modeControl.mode && state.status.execution_mode) {
            state.modeControl.mode = normalizeModeData(state.status.execution_mode);
        }
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
    async function fetchBacktests() {
        const runsPayload = await api("/api/backtest/runs?limit=20");
        state.backtestRuns = runsPayload.runs || [];
        if (!state.backtestReplay.selectedRunId && state.backtestRuns.length > 0) {
            state.backtestReplay.selectedRunId = Number(state.backtestRuns[0].id) || null;
        }
        if (state.activeSection === "backtest-replay" && state.backtestReplay.selectedRunId) {
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

    function activateSection(section) {
        const sectionName = textOr(section, "overview");
        const target = ensureContainer(`section-${sectionName}`);
        if (!target) {
            setFeedback(`Section ${sectionName} is missing from the dashboard template.`, true);
            return;
        }
        state.activeSection = sectionName;
        document.querySelectorAll("#dashboard-tabs .nav-link").forEach((item) => item.classList.toggle("active", item.dataset.section === sectionName));
        document.querySelectorAll(".dashboard-section").forEach((node) => node.classList.add("d-none"));
        target.classList.remove("d-none");
        dashboardLog("tabs", "mounted section", { section: sectionName, container: target.id });
        if (sectionName === "backtest-replay" && state.backtestReplay.selectedRunId && (!state.backtestReplay.summary || num(state.backtestReplay.summary.run?.id) !== num(state.backtestReplay.selectedRunId))) {
            replayLoadRun(state.backtestReplay.selectedRunId, { silent: true }).catch((error) => replaySetMessage(error.message, true));
        }
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
        const preserveBacktestEditing = !options.force && backtestInteractionLocked();
        if (preserveBacktestEditing) {
            renderTopStatusBar();
            renderQuickActions();
            return;
        }
        captureUiState();
        renderTopStatusBar();
        renderQuickActions();
        safeRenderSection("section-overview", "Overview", renderOverview);
        safeRenderSection("section-bot-control", "Bot Control", renderBotControl);
        safeRenderSection("section-strategies", "Strategies", renderStrategies);
        safeRenderSection("section-families", "Families", renderFamilies);
        safeRenderSection("section-risk", "Risk Management", renderRisk);
        safeRenderSection("section-sessions", "Sessions", renderSessions);
        safeRenderSection("section-symbols", "Symbols", renderSymbols);
        safeRenderSection("section-manual-trading", "Manual Trading", renderManualTrading);
        safeRenderSection("section-alerts", "Alerts", renderAlerts);
        safeRenderSection("section-trades", "Trades", renderTrades);
        safeRenderSection("section-performance", "Performance", renderPerformance);
        if (!preserveBacktestEditing) {
            safeRenderSection("section-backtest", "Backtest", renderBacktest);
        }
        safeRenderSection("section-backtest-replay", "Backtest Replay", renderBacktestReplay);
        safeRenderSection("section-oos", "OOS", renderOOS);
        safeRenderSection("section-config", "Config", renderConfig);
        safeRenderSection("section-logs", "Logs", renderLogs);
        safeRenderSection("section-health", "System Health", renderHealth);
        restoreUiState();
        activateSection(state.activeSection);
        if (isReadonly()) {
            document.querySelectorAll("#section-bot-control input, #section-bot-control select, #section-bot-control button, #section-strategies input, #section-strategies select, #section-strategies button, #section-families input, #section-families button, #section-risk input, #section-risk button, #section-sessions input, #section-sessions button, #section-symbols input, #section-symbols button, #section-manual-trading input, #section-manual-trading select, #section-manual-trading button, #section-alerts input, #section-alerts button, #section-trades input, #section-trades button, #section-performance input, #section-performance button, #section-backtest input, #section-backtest select, #section-backtest button, #section-oos input, #section-oos select, #section-oos button, #section-config input, #section-config button, #section-config textarea").forEach((node) => {
                if (node.id !== "dashboard-readonly-mode" && node.id !== "apply-runtime-control") {
                    node.disabled = true;
                }
            });
            document.querySelectorAll("#import-config-button").forEach((node) => { node.disabled = true; });
        }
    }

    async function boot() {
        bindTabs();
        document.addEventListener("keydown", replayApplyKeyboardShortcuts);
        window.addEventListener("resize", () => {
            if (replayIsVisible()) {
                scheduleReplayRender();
            }
        });
        const loaders = [
            refreshStatus,
            refreshConfig,
            refreshExecutionMode,
            refreshPositions,
            refreshLogs,
            fetchBacktests,
            fetchJobs,
            fetchWorkerStatus,
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
        setInterval(refreshPositions, Math.max(5, refreshSeconds) * 1000);
        setInterval(refreshLogs, 15000);
        setInterval(fetchBacktests, 30000);
        setInterval(fetchJobs, 5000);
        setInterval(fetchWorkerStatus, 5000);
        setInterval(fetchOOS, 5000);
    }

    document.addEventListener("DOMContentLoaded", boot);
})();
