// Strategy tests panel, shown under the Indicator Lab tab.
// Queues a test (POST /api/strategy-tests); the server's research sandbox runs it, one at a time,
// and this panel polls for progress and shows year-by-year results per symbol.
(() => {
    "use strict";
    const panel = document.getElementById("strategy-test-panel");
    const labSection = document.getElementById("section-indicator-lab");
    if (!panel || !labSection) return;

    const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const fmt = (v, digits = 1, signed = false) => {
        if (v === null || v === undefined || Number.isNaN(Number(v))) return "–";
        const n = Number(v);
        return (signed && n > 0 ? "+" : "") + n.toFixed(digits);
    };
    const tone = (v) => (v === null || v === undefined ? "" : Number(v) > 0 ? "text-success" : Number(v) < 0 ? "text-danger" : "");
    const state = { tests: [], meta: null, selected: null, detail: null, defaults: null, message: "", error: "", timer: null, busy: false };

    async function api(path, options = {}) {
        const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
        const text = await response.text();
        let payload = {};
        try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
        if (!response.ok) throw new Error(payload.detail || payload.message || `Request failed: ${response.status}`);
        return payload;
    }

    const visible = () => !labSection.classList.contains("d-none");

    function readLabParameters() {
        // Indicator Lab parameter inputs (LEBPRIME only); only values that differ from the defaults are sent.
        const indicator = document.getElementById("il-indicator")?.value || "LEBPRIME";
        if (indicator !== "LEBPRIME" || !state.defaults) return {};
        const out = {};
        labSection.querySelectorAll(".il-param-value").forEach((node) => {
            const name = node.dataset.param;
            if (!name || !(name in state.defaults)) return;
            const kind = node.dataset.kind;
            let value = kind === "bool" ? node.checked : node.value;
            if (kind === "int") value = parseInt(value, 10);
            if (kind === "float") value = parseFloat(value);
            if ((kind === "int" || kind === "float") && !Number.isFinite(value)) return;
            if (String(value) !== String(state.defaults[name])) out[name] = value;
        });
        return out;
    }

    function formHtml() {
        const meta = state.meta || { symbols: [], timeframes: [], data_until: {} };
        const today = new Date().toISOString().slice(0, 10);
        const saved = state.form || { symbols: ["XAUUSD"], timeframe: "M5", start: "2020-01-01", end: today, useLab: true, name: "" };
        const symbolBoxes = meta.symbols.map((s) => `<label class="form-check form-check-inline mb-0"><input class="form-check-input st-symbol" type="checkbox" value="${s}" ${saved.symbols.includes(s) ? "checked" : ""}><span class="form-check-label">${s}</span></label>`).join("");
        const tfOptions = meta.timeframes.map((t) => `<option ${t === saved.timeframe ? "selected" : ""}>${t}</option>`).join("");
        const until = Object.entries(meta.data_until || {}).map(([s, t]) => `${s} ${t ? t.slice(0, 16).replace("T", " ") : "none"}`).join(" · ");
        return `
        <div class="row g-2 align-items-end">
            <div class="col-12"><span class="small text-secondary d-block mb-1">Symbols</span>${symbolBoxes}</div>
            <div class="col-6 col-md-2"><label class="form-label w-100 mb-0"><span class="small text-secondary">Timeframe</span><select class="form-select" id="st-timeframe">${tfOptions}</select></label></div>
            <div class="col-6 col-md-2"><label class="form-label w-100 mb-0"><span class="small text-secondary">From</span><input class="form-control" id="st-start" type="date" value="${esc(saved.start)}"></label></div>
            <div class="col-6 col-md-2"><label class="form-label w-100 mb-0"><span class="small text-secondary">To (exclusive)</span><input class="form-control" id="st-end" type="date" value="${esc(saved.end)}"></label></div>
            <div class="col-6 col-md-3"><label class="form-label w-100 mb-0"><span class="small text-secondary">Name (optional)</span><input class="form-control" id="st-name" maxlength="80" value="${esc(saved.name)}"></label></div>
            <div class="col-12 col-md-3 d-flex gap-2"><button class="btn btn-primary flex-grow-1" id="st-run" ${state.busy ? "disabled" : ""}>Run test</button></div>
            <div class="col-12"><label class="form-check mb-0"><input class="form-check-input" type="checkbox" id="st-use-lab" ${saved.useLab ? "checked" : ""}><span class="form-check-label small">Use the LEBPRIME parameters set in the Indicator Lab form above (only changed values are sent)</span></label></div>
            <div class="col-12 small text-secondary">Bot engine on closed candles (no look-ahead), net of spread at entry + commission, years by exit time. Data until: ${esc(until)}. M1/M2 over several years can take hours.</div>
            ${state.meta && !state.meta.dispatcher_alive ? `<div class="col-12"><div class="alert alert-warning py-2 mb-0 small">The research-sandbox dispatcher has not run in the last 2 minutes, so queued tests will wait. On the server: <code>sudo systemctl enable --now lebprime-research-dispatch.timer</code></div></div>` : ""}
            ${state.error ? `<div class="col-12"><div class="alert alert-danger py-2 mb-0 small">${esc(state.error)}</div></div>` : ""}
            ${state.message ? `<div class="col-12"><div class="alert alert-info py-2 mb-0 small">${esc(state.message)}</div></div>` : ""}
        </div>`;
    }

    function listHtml() {
        if (!state.tests.length) return `<div class="small text-secondary">No tests yet.</div>`;
        const badge = { QUEUED: "secondary", RUNNING: "primary", CANCELING: "warning", COMPLETED: "success", FAILED: "danger", CANCELED: "dark" };
        const rows = state.tests.map((t) => {
            const r = t.request || {};
            const total = t.total || {};
            return `<tr class="st-row ${t.id === state.selected ? "table-active" : ""}" data-id="${esc(t.id)}" role="button">
                <td class="text-nowrap">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</td>
                <td>${esc(r.name || "")} ${esc((r.symbols || []).join(", "))} ${esc(r.timeframe || "")}<div class="small text-secondary">${esc(r.start)} → ${esc(r.end)}${Object.keys(r.parameters || {}).length ? ` · ${Object.keys(r.parameters).length} changed parameter(s)` : ""}</div></td>
                <td><span class="badge text-bg-${badge[t.state] || "secondary"}">${esc(t.state)}</span></td>
                <td class="text-end">${total.trades ?? "–"}</td>
                <td class="text-end">${total.win_pct !== undefined ? fmt(total.win_pct) + "%" : "–"}</td>
                <td class="text-end ${tone(total.net_r)}">${total.net_r !== undefined ? fmt(total.net_r, 1, true) + "R" : "–"}</td>
            </tr>`;
        }).join("");
        return `<div class="table-responsive" style="overflow-x:auto;max-width:100%"><table class="table table-sm table-hover align-middle mb-0">
            <thead><tr><th>Queued (UTC)</th><th>Test</th><th>State</th><th class="text-end">Trades</th><th class="text-end">Win %</th><th class="text-end">Net R</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    }

    function statsRow(label, s, strong = false) {
        if (!s || !s.trades) return `<tr><td>${esc(label)}</td><td class="text-end">0</td><td colspan="10"></td></tr>`;
        const side = (x) => x && x.trades ? `${x.trades} · ${fmt(x.win_pct, 0)}% · <span class="${tone(x.net_r)}">${fmt(x.net_r, 1, true)}R</span>` : "0";
        return `<tr class="${strong ? "fw-semibold" : ""}"><td>${esc(label)}</td><td class="text-end">${s.trades}</td><td class="text-end">${fmt(s.win_pct)}%</td>
            <td class="text-end ${tone(s.net_r)}">${fmt(s.net_r, 1, true)}</td><td class="text-end">${fmt(s.avg_win_r, 2, true)}</td><td class="text-end">${fmt(s.avg_loss_r, 2)}</td>
            <td class="text-end">${fmt(s.cost_r, 1)}</td><td class="text-end">${fmt(s.max_dd_r, 1)}</td><td class="text-nowrap">${side(s.buys)}</td><td class="text-nowrap">${side(s.sells)}</td></tr>`;
    }

    function detailHtml() {
        const t = state.detail;
        if (!t) return "";
        const head = `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
            <div><strong>${esc(t.request?.name || t.id)}</strong> <span class="small text-secondary">${esc((t.request?.symbols || []).join(", "))} ${esc(t.request?.timeframe)} · ${esc(t.request?.start)} → ${esc(t.request?.end)}</span></div>
            <div class="d-flex gap-2">
                ${["QUEUED", "RUNNING"].includes(t.state) ? `<button class="btn btn-sm btn-outline-warning" id="st-cancel" data-id="${esc(t.id)}">Cancel</button>` : ""}
                ${t.has_trades ? `<a class="btn btn-sm btn-outline-light" href="/api/strategy-tests/${encodeURIComponent(t.id)}/trades.csv">Trades CSV</a>` : ""}
            </div></div>`;
        const params = Object.entries(t.request?.parameters || {});
        const paramLine = params.length ? `<div class="small text-secondary mb-2">Changed parameters: ${params.map(([k, v]) => `${esc(k)}=${esc(v)}`).join(", ")}</div>` : `<div class="small text-secondary mb-2">Default LEBPRIME parameters.</div>`;
        const progress = (t.progress || []).length ? `<pre class="small bg-body-tertiary p-2 rounded mb-2" style="max-height:10rem;overflow:auto;white-space:pre-wrap">${esc(t.progress.join("\n"))}</pre>` : "";
        const error = t.error ? `<div class="alert alert-danger py-2 small">${esc(t.error)}</div>` : "";
        let tables = "";
        const summary = t.summary;
        if (summary && summary.symbols) {
            const header = `<thead><tr><th>Year</th><th class="text-end">Trades</th><th class="text-end">Win %</th><th class="text-end">Net R</th><th class="text-end">Avg win R</th><th class="text-end">Avg loss R</th><th class="text-end">Costs R</th><th class="text-end">Max DD R</th><th>Buys (n · win · R)</th><th>Sells (n · win · R)</th></tr></thead>`;
            tables = Object.entries(summary.symbols).map(([symbol, block]) => {
                const years = Object.entries(block.by_year || {}).map(([y, s]) => statsRow(y, s)).join("");
                const losing = block.losing_years !== undefined ? ` <span class="small text-secondary">· ${block.losing_years} losing year(s)</span>` : "";
                return `<h6 class="mt-3 mb-1">${symbol === "ALL" ? "All symbols" : esc(symbol)}${losing}</h6>
                    <div class="table-responsive" style="overflow-x:auto;max-width:100%"><table class="table table-sm mb-0">${header}<tbody>${years}${statsRow("Total", block.total, true)}</tbody></table></div>`;
            }).join("");
            tables += `<div class="small text-secondary mt-2">${esc(summary.method || "")}</div>`;
        }
        return `<hr>${head}${paramLine}${error}${progress}${tables}`;
    }

    function render() {
        // Keep what the user typed while re-rendering.
        if (panel.querySelector("#st-timeframe")) state.form = currentForm();
        panel.innerHTML = `<div class="card shadow-sm border-0 mt-3"><div class="card-body">
            <h5 class="card-title mb-1">Strategy test</h5>
            <div class="small text-secondary mb-3">Runs the bot's own engine in the capped research sandbox on the server, never in the live bot, and never places trades.</div>
            ${formHtml()}<hr>${listHtml()}${detailHtml()}</div></div>`;
    }

    function currentForm() {
        return {
            symbols: [...panel.querySelectorAll(".st-symbol:checked")].map((n) => n.value),
            timeframe: panel.querySelector("#st-timeframe")?.value || "M5",
            start: panel.querySelector("#st-start")?.value || "",
            end: panel.querySelector("#st-end")?.value || "",
            name: panel.querySelector("#st-name")?.value || "",
            useLab: !!panel.querySelector("#st-use-lab")?.checked,
        };
    }

    const typing = () => panel.contains(document.activeElement) && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);

    async function refresh(force = false) {
        if (!visible() && !force) return;
        try {
            const list = await api("/api/strategy-tests?limit=20");
            state.tests = list.tests || [];
            state.meta = list;
            if (!state.selected && state.tests.length) state.selected = state.tests[0].id;
            if (state.selected) state.detail = (await api(`/api/strategy-tests/${encodeURIComponent(state.selected)}`)).test;
            if (!state.defaults) {
                const p = await api("/api/indicator-lab/parameters/LEBPRIME");
                state.defaults = Object.fromEntries((p.parameters || []).map((x) => [x.name, x.default]));
            }
        } catch (error) {
            state.error = error.message;
        }
        if (!typing()) render();
        schedule();
    }

    function schedule() {
        clearTimeout(state.timer);
        const active = state.tests.some((t) => ["QUEUED", "RUNNING", "CANCELING"].includes(t.state));
        state.timer = setTimeout(() => refresh(), active ? 5000 : 30000);
    }

    panel.addEventListener("click", async (event) => {
        const row = event.target.closest(".st-row");
        if (row) { state.selected = row.dataset.id; state.detail = null; await refresh(true); return; }
        if (event.target.id === "st-cancel") {
            if (!window.confirm("Cancel this strategy test?")) return;
            try { await api(`/api/strategy-tests/${encodeURIComponent(event.target.dataset.id)}/cancel`, { method: "POST" }); } catch (e) { state.error = e.message; }
            await refresh(true);
            return;
        }
        if (event.target.id === "st-run") {
            const form = currentForm();
            state.form = form;
            state.error = ""; state.message = "";
            if (!form.symbols.length) { state.error = "Pick at least one symbol."; render(); return; }
            const parameters = form.useLab ? readLabParameters() : {};
            state.busy = true; render();
            try {
                const out = await api("/api/strategy-tests", { method: "POST", body: JSON.stringify({ actor: "dashboard", symbols: form.symbols, timeframe: form.timeframe, start: form.start, end: form.end, name: form.name, parameters }) });
                state.selected = out.test.id; state.detail = null;
                state.message = `Queued ${form.symbols.join(", ")} ${form.timeframe}${Object.keys(parameters).length ? ` with ${Object.keys(parameters).length} changed parameter(s)` : " with default parameters"}. It starts within ~15 s if nothing else is running.`;
            } catch (e) { state.error = e.message; }
            state.busy = false;
            await refresh(true);
        }
    });

    // Show the panel only while the Indicator Lab tab is open (dashboard.js toggles d-none on the section).
    const sync = () => {
        const show = visible();
        panel.classList.toggle("d-none", !show);
        if (show && !panel.dataset.loaded) { panel.dataset.loaded = "1"; refresh(true); }
        else if (show) refresh();
    };
    new MutationObserver(sync).observe(labSection, { attributes: true, attributeFilter: ["class"] });
    sync();
})();
