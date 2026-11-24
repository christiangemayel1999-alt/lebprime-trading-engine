// Time Machine panel, shown under the Indicator Lab tab.
// Pick a live zone (or type one), queue it (POST /api/time-machine); the research sandbox on the server
// finds similar past zones and runs a Monte Carlo of recent price paths. This panel polls and shows the odds.
// It never places, changes or cancels an order.
(() => {
    "use strict";
    const panel = document.getElementById("time-machine-panel");
    const labSection = document.getElementById("section-indicator-lab");
    if (!panel || !labSection) return;

    const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const num = (v, d = 1, signed = false) => {
        if (v === null || v === undefined || Number.isNaN(Number(v))) return "–";
        const n = Number(v);
        return (signed && n > 0 ? "+" : "") + n.toFixed(d);
    };
    const tone = (v) => (v === null || v === undefined ? "" : Number(v) > 0 ? "text-success" : Number(v) < 0 ? "text-danger" : "");
    const when = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "");
    const state = { runs: [], meta: null, zones: [], selected: null, detail: null, form: null, error: "", message: "", timer: null, busy: false };

    async function api(path, options = {}) {
        const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
        const text = await response.text();
        let payload = {};
        try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
        if (!response.ok) throw new Error(payload.detail || payload.message || `Request failed: ${response.status}`);
        return payload;
    }
    const visible = () => !labSection.classList.contains("d-none");

    // ------------------------------------------------------------------ form
    function formHtml() {
        const meta = state.meta || { symbols: [], timeframes: [] };
        const f = state.form || { zone: "", symbol: "XAUUSD", timeframe: "M15", side: "BUY", entry1: "", sl: "", top: "", bottom: "", time: "", paths: 10000 };
        const zoneOptions = state.zones.map((z) => `<option value="${esc(z.zone_id)}" ${z.zone_id === f.zone ? "selected" : ""}>${z.open ? "● " : ""}${esc(z.zone_id)} · ${esc(z.side)} E1 ${esc(z.entry1)} SL ${esc(z.sl)} · ${esc(z.state || z.execution_status)} · ${esc(when(z.limit_time))}</option>`).join("");
        const sel = (id, values, current) => `<select class="form-select" id="${id}">${values.map((v) => `<option ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
        const input = (id, label, value, type = "number", extra = "") => `<label class="form-label w-100 mb-0"><span class="small text-secondary">${label}</span><input class="form-control" id="${id}" type="${type}" value="${esc(value)}" ${extra}></label>`;
        return `
        <div class="row g-2 align-items-end">
            <div class="col-12"><label class="form-label w-100 mb-0"><span class="small text-secondary">Live bot zone (● = still waiting or active). Picking one fills the boxes below; you can also type a zone yourself.</span>
                <select class="form-select" id="tm-zone"><option value="">Type a zone myself…</option>${zoneOptions}</select></label></div>
            <div class="col-6 col-md-2"><label class="form-label w-100 mb-0"><span class="small text-secondary">Symbol</span>${sel("tm-symbol", meta.symbols, f.symbol)}</label></div>
            <div class="col-6 col-md-1"><label class="form-label w-100 mb-0"><span class="small text-secondary">TF</span>${sel("tm-timeframe", meta.timeframes, f.timeframe)}</label></div>
            <div class="col-6 col-md-1"><label class="form-label w-100 mb-0"><span class="small text-secondary">Side</span>${sel("tm-side", ["BUY", "SELL"], f.side)}</label></div>
            <div class="col-6 col-md-2">${input("tm-entry1", "Entry 1", f.entry1, "number", 'step="any"')}</div>
            <div class="col-6 col-md-2">${input("tm-sl", "Stop loss", f.sl, "number", 'step="any"')}</div>
            <div class="col-6 col-md-2">${input("tm-top", "Zone top (optional)", f.top, "number", 'step="any"')}</div>
            <div class="col-6 col-md-2">${input("tm-bottom", "Zone bottom (optional)", f.bottom, "number", 'step="any"')}</div>
            <div class="col-6 col-md-3">${input("tm-time", "LIMIT placed at (UTC, empty = now)", f.time, "datetime-local")}</div>
            <div class="col-6 col-md-2">${input("tm-paths", "Monte Carlo paths", f.paths, "number", 'min="1000" max="50000" step="1000"')}</div>
            <div class="col-12 col-md-3"><button class="btn btn-primary w-100" id="tm-run" ${state.busy ? "disabled" : ""}>Check the odds</button></div>
            <div class="col-12 small text-secondary">TP = Entry 1 + 1R + extra (gold $0.20, others 2× median spread). Real spread and commission. Only candles and trades that existed before the LIMIT was placed are used. The first run for a symbol and timeframe builds its zone library with the bot's engine (about 30 s for M30, 1-2 minutes for M15, about 8-10 minutes for M1). After that a run takes about a minute.</div>
            ${state.meta && !state.meta.dispatcher_alive ? `<div class="col-12"><div class="alert alert-warning py-2 mb-0 small">The research-sandbox dispatcher has not run in the last 2 minutes, so runs will wait. On the server: <code>sudo systemctl enable --now lebprime-research-dispatch.timer</code></div></div>` : ""}
            ${state.error ? `<div class="col-12"><div class="alert alert-danger py-2 mb-0 small">${esc(state.error)}</div></div>` : ""}
            ${state.message ? `<div class="col-12"><div class="alert alert-info py-2 mb-0 small">${esc(state.message)}</div></div>` : ""}
        </div>`;
    }

    function readForm() {
        const v = (id) => panel.querySelector(id)?.value ?? "";
        return { zone: v("#tm-zone"), symbol: v("#tm-symbol"), timeframe: v("#tm-timeframe"), side: v("#tm-side"), entry1: v("#tm-entry1"), sl: v("#tm-sl"),
                 top: v("#tm-top"), bottom: v("#tm-bottom"), time: v("#tm-time"), paths: v("#tm-paths") || 10000 };
    }

    function fillFromZone(zoneId) {
        const z = state.zones.find((x) => x.zone_id === zoneId);
        const f = readForm();
        if (!z) { state.form = { ...f, zone: "" }; render(true); return; }
        state.form = { ...f, zone: z.zone_id, symbol: z.symbol, timeframe: z.timeframe, side: z.side, entry1: z.entry1, sl: z.sl,
                       top: z.zone_top ?? "", bottom: z.zone_bottom ?? "", time: z.limit_time ? String(z.limit_time).slice(0, 16) : "" };
        render(true);
    }

    // ------------------------------------------------------------------ list + detail
    function listHtml() {
        if (!state.runs.length) return `<div class="small text-secondary">No runs yet.</div>`;
        const badge = { QUEUED: "secondary", RUNNING: "primary", CANCELING: "warning", COMPLETED: "success", FAILED: "danger", CANCELED: "dark" };
        const rows = state.runs.map((t) => {
            const r = t.request || {};
            const o = t.odds || {};
            return `<tr class="tm-row ${t.id === state.selected ? "table-active" : ""}" data-id="${esc(t.id)}" role="button">
                <td class="text-nowrap">${esc(when(r.created_at))}</td>
                <td>${esc(r.symbol)} ${esc(r.timeframe)} ${esc(r.side)} E1 ${esc(r.entry1)} SL ${esc(r.sl)}<div class="small text-secondary">${esc(r.live_zone_id || "typed zone")}${r.limit_time ? ` · LIMIT ${esc(when(r.limit_time))}` : " · now"}</div></td>
                <td><span class="badge text-bg-${badge[t.state] || "secondary"}">${esc(t.state)}</span></td>
                <td class="text-end">${o.trades ?? "–"}</td>
                <td class="text-end">${o.win_pct !== undefined && o.win_pct !== null ? num(o.win_pct) + "%" : "–"}</td>
                <td class="text-end ${tone(o.avg_r)}">${o.avg_r !== undefined && o.avg_r !== null ? num(o.avg_r, 2, true) + "R" : "–"}</td>
                <td class="text-end">${o.mc_tp_pct !== undefined && o.mc_tp_pct !== null ? num(o.mc_tp_pct) + "%" : "–"}</td>
            </tr>`;
        }).join("");
        return `<div class="table-responsive" style="overflow-x:auto;max-width:100%"><table class="table table-sm table-hover align-middle mb-0">
            <thead><tr><th>Queued (UTC)</th><th>Zone</th><th>State</th><th class="text-end">Analogs</th><th class="text-end">Win %</th><th class="text-end">Avg R</th><th class="text-end">MC TP first</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    }

    const head = `<thead><tr><th></th><th class="text-end">Trades</th><th class="text-end">Win % (95% range)</th><th class="text-end">Net R</th><th class="text-end">Avg R</th><th class="text-end">Avg win</th><th class="text-end">Avg loss</th><th class="text-end">Break-even win %</th><th class="text-end">Costs R</th><th class="text-end">Max DD R</th><th class="text-end">TP / SL / 3-day</th></tr></thead>`;
    function row(label, s, strong = false) {
        if (!s || !s.trades) return `<tr><td>${label}</td><td class="text-end">0</td><td colspan="9"></td></tr>`;
        const ci = s.win_ci95 ? ` <span class="text-secondary">(${num(s.win_ci95[0], 0)}–${num(s.win_ci95[1], 0)})</span>` : "";
        const few = s.trusted === false ? ` <span class="badge text-bg-warning">too few</span>` : "";
        return `<tr class="${strong ? "fw-semibold" : ""}"><td>${label}${few}</td><td class="text-end">${s.trades}</td><td class="text-end text-nowrap">${num(s.win_pct)}%${ci}</td>
            <td class="text-end ${tone(s.net_r)}">${num(s.net_r, 1, true)}</td><td class="text-end ${tone(s.avg_r)}">${num(s.avg_r, 3, true)}</td>
            <td class="text-end">${num(s.avg_win_r, 2, true)}</td><td class="text-end">${num(s.avg_loss_r, 2)}</td><td class="text-end">${s.breakeven_win_pct != null ? num(s.breakeven_win_pct) + "%" : "–"}</td>
            <td class="text-end">${num(s.cost_r, 1)}</td><td class="text-end">${num(s.max_dd_r, 1)}</td><td class="text-end text-nowrap">${s.tp ?? 0} / ${s.sl ?? 0} / ${s.time_exit ?? 0}</td></tr>`;
    }
    const table = (body) => `<div class="table-responsive" style="overflow-x:auto;max-width:100%"><table class="table table-sm mb-0">${head}<tbody>${body}</tbody></table></div>`;

    function verdict(s, mc) {
        if (!s || !s.trades) return "No similar past zones had finished trading before this one, so there is no history to judge it by.";
        const edge = s.avg_r > 0 ? "made money" : "lost money";
        const trust = s.trusted ? "" : ` Only ${s.trades} trades, so treat this as a hint, not a verdict.`;
        const mcLine = mc ? ` Recent price paths give TP first ${num(mc.p_tp_first)}% of the time (break-even needs ${num(mc.breakeven_win_pct)}%).` : "";
        return `Zones like this won ${num(s.win_pct)}% of the time and ${edge}: ${num(s.avg_r, 2, true)}R per trade after costs, against a break-even of ${num(s.breakeven_win_pct)}%.${trust}${mcLine}`;
    }

    function detailHtml() {
        const t = state.detail;
        if (!t) return "";
        const r = t.request || {};
        const s = t.summary;
        const top = `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
            <div><strong>${esc(r.symbol)} ${esc(r.timeframe)} ${esc(r.side)}</strong> <span class="small text-secondary">E1 ${esc(r.entry1)} · SL ${esc(r.sl)} · ${esc(r.live_zone_id || "typed zone")}</span></div>
            ${["QUEUED", "RUNNING"].includes(t.state) ? `<button class="btn btn-sm btn-outline-warning" id="tm-cancel" data-id="${esc(t.id)}">Cancel</button>` : ""}</div>`;
        const progress = (t.progress || []).length && !s ? `<pre class="small bg-body-tertiary p-2 rounded mb-2" style="max-height:10rem;overflow:auto;white-space:pre-wrap">${esc(t.progress.join("\n"))}</pre>` : "";
        const error = t.error ? `<div class="alert alert-danger py-2 small">${esc(t.error)}</div>` : "";
        if (!s || !s.analogs) return `<hr>${top}${error}${progress}`;
        const a = s.analogs, mc = s.monte_carlo, md = s.monte_carlo_with_drift, z = s.zone, f = s.features;
        const tiers = a.tiers || [];
        const best = tiers.find((x) => x.key === a.headline);
        const c = a.conditions || {};
        const zoneLine = `<div class="small mb-2">LIMIT at <strong>${esc(when(z.decision_time))} UTC</strong> · TP ${esc(num(z.tp, 5))} (1R + ${esc(num(z.tp_extra, 4))}) · SL ${num(f.sl_atr, 2)} ATR · H1 trend <strong>${esc(f.h1_trend)}</strong> · session <strong>${esc(f.session)}</strong> · zone width ${esc(c.zone_width || "")}</div>`;
        const box = (title, body) => `<div class="col-12 col-lg-6"><div class="border rounded p-2 h-100"><div class="small text-secondary">${title}</div>${body}</div></div>`;
        const analogBox = best ? `<div class="fs-5 fw-semibold">${num(best.win_pct)}% win · <span class="${tone(best.avg_r)}">${num(best.avg_r, 2, true)}R</span> per trade</div>
            <div class="small">${best.trades} similar past trades (${esc(best.label.charAt(0).toLowerCase() + best.label.slice(1))}). 95% range of the win rate ${num(best.win_ci95?.[0], 0)}–${num(best.win_ci95?.[1], 0)}%. Avg win ${num(best.avg_win_r, 2, true)}R, avg loss ${num(best.avg_loss_r, 2)}R.</div>` : `<div class="small">No similar past trades yet.</div>`;
        const mcBox = mc ? `<div class="fs-5 fw-semibold">${num(mc.p_tp_first)}% TP first · <span class="${tone(mc.expected_r)}">${num(mc.expected_r, 2, true)}R</span> expected</div>
            <div class="small">${Number(mc.paths).toLocaleString()} paths of 3 days built from the ${Number(s.mc_window?.candles || 0).toLocaleString()} M1 candles before the LIMIT (${esc(when(s.mc_window?.from))} → ${esc(when(s.mc_window?.to))}), recent drift removed. SL first ${num(mc.p_sl_first)}%, neither in 3 days ${num(mc.p_neither_3d)}%. Break-even ${num(mc.breakeven_win_pct)}%.
            With the recent drift kept: TP first ${num(md?.p_tp_first)}%, ${num(md?.expected_r, 2, true)}R.</div>` : "";
        const fill = s.p_fill !== null && s.p_fill !== undefined ? `<div class="small mt-1">Chance price reaches Entry 1 before the LIMIT expires (360 candles): <strong>${num(s.p_fill)}%</strong>.</div>` : "";
        const actual = s.actual ? `<div class="alert alert-secondary py-2 small mt-2 mb-0"><strong>What actually happened</strong> (only shown because this zone is in the past; never used for the odds): ${
            s.actual.filled ? `filled ${esc(when(s.actual.fill_time))}, ${esc(s.actual.reason)} at ${esc(when(s.actual.exit_time))}, <span class="${tone(s.actual.net_r)}">${num(s.actual.net_r, 2, true)}R</span>${s.actual.final ? "" : " (still open in the data)"}` : (s.actual.final ? "never filled." : "not filled yet.")}</div>` : "";
        const tierRows = tiers.map((x) => row(esc(x.label), x, x.key === a.headline)).join("");
        const years = best ? Object.entries(best.by_year || {}).map(([y, v]) => row(esc(y), v)).join("") + row("Total", best, true) : "";
        const all = a.all_zones || {};
        const sides = row(`All ${esc(z.symbol)} ${esc(z.timeframe)} zones`, all, true) + row("Buys", all.buys) + row("Sells", all.sells);
        const allYears = Object.entries(all.by_year || {}).map(([y, v]) => row(esc(y), v)).join("");
        const examples = (a.closest_examples || []).map((e) => `<tr><td>${esc(when(e.limit_time))}</td><td>${esc(when(e.exit_time))}</td><td class="text-end">${esc(e.entry1)}</td><td class="text-end">${esc(e.sl)}</td><td class="text-end">${num(e.sl_atr, 2)}</td><td>${esc(e.reason)}</td><td class="text-end ${tone(e.net_r)}">${num(e.net_r, 2, true)}</td></tr>`).join("");
        const lib = s.library || {};
        return `<hr>${top}${error}${zoneLine}
            <div class="alert ${best && best.avg_r > 0 && best.trusted ? "alert-success" : "alert-warning"} py-2 small">${esc(verdict(best, mc))}</div>
            <div class="row g-2 mb-2">${box("(a) Similar past zones", analogBox)}${box("(b) Monte Carlo of recent price paths", mcBox + fill)}</div>${actual}
            <h6 class="mt-3 mb-1">Similar past zones, from strict to loose</h6>${table(tierRows)}
            <h6 class="mt-3 mb-1">Year by year: ${esc(best?.label || "")}</h6>${table(years)}
            <h6 class="mt-3 mb-1">Every ${esc(z.symbol)} ${esc(z.timeframe)} zone, buys vs sells</h6>${table(sides)}
            <details class="mt-2"><summary class="small">All zones year by year</summary>${table(allYears + row("Total", all, true))}</details>
            ${examples ? `<details class="mt-2"><summary class="small">The ${a.closest_examples.length} most recent closest matches</summary><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>LIMIT</th><th>Exit</th><th class="text-end">E1</th><th class="text-end">SL</th><th class="text-end">SL ATR</th><th>Result</th><th class="text-end">Net R</th></tr></thead><tbody>${examples}</tbody></table></div></details>` : ""}
            <div class="small text-secondary mt-2">Zone library: ${esc(lib.zones)} engine zones, ${esc(lib.trades)} filled trades, ${esc(when(lib.data_start))} → ${esc(when(lib.data_end))}, built ${esc(when(lib.built_at))} UTC (rebuilt weekly). ${esc(s.method || "")}</div>`;
    }

    function render(fromState = false) {
        if (!fromState && panel.querySelector("#tm-symbol")) state.form = readForm();
        panel.innerHTML = `<div class="card shadow-sm border-0 mt-3"><div class="card-body">
            <h5 class="card-title mb-1">Time Machine: odds for one zone</h5>
            <div class="small text-secondary mb-3">Before you take a zone, see how zones like it did before and what recent price paths say. Runs in the research sandbox on the server, never in the live bot, and never places trades.</div>
            ${formHtml()}<hr>${listHtml()}${detailHtml()}</div></div>`;
    }

    const typing = () => panel.contains(document.activeElement) && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);

    async function refresh(force = false) {
        if (!visible() && !force) return;
        try {
            const list = await api("/api/time-machine?limit=15");
            state.runs = list.runs || [];
            state.meta = list;
            if (!state.zones.length || force) state.zones = (await api("/api/time-machine/live-zones?limit=80")).zones || [];
            if (!state.selected && state.runs.length) state.selected = state.runs[0].id;
            if (state.selected) state.detail = (await api(`/api/time-machine/${encodeURIComponent(state.selected)}`)).run;
        } catch (error) {
            state.error = error.message;
        }
        if (!typing()) render();
        clearTimeout(state.timer);
        const active = state.runs.some((t) => ["QUEUED", "RUNNING", "CANCELING"].includes(t.state));
        state.timer = setTimeout(() => refresh(), active ? 5000 : 60000);
    }

    panel.addEventListener("change", (event) => {
        if (event.target.id === "tm-zone") fillFromZone(event.target.value);
    });

    panel.addEventListener("click", async (event) => {
        const tr = event.target.closest(".tm-row");
        if (tr) { state.selected = tr.dataset.id; state.detail = null; await refresh(true); return; }
        if (event.target.id === "tm-cancel") {
            if (!window.confirm("Cancel this Time Machine run?")) return;
            try { await api(`/api/time-machine/${encodeURIComponent(event.target.dataset.id)}/cancel`, { method: "POST" }); } catch (e) { state.error = e.message; }
            await refresh(true);
            return;
        }
        if (event.target.id === "tm-run") {
            const f = readForm();
            state.form = f; state.error = ""; state.message = "";
            const entry1 = parseFloat(f.entry1), sl = parseFloat(f.sl);
            if (!Number.isFinite(entry1) || !Number.isFinite(sl)) { state.error = "Enter Entry 1 and the stop loss."; render(); return; }
            const opt = (v) => (v === "" || v === null || v === undefined ? null : parseFloat(v));
            const body = { actor: "dashboard", symbol: f.symbol, timeframe: f.timeframe, side: f.side, entry1, sl, zone_top: opt(f.top), zone_bottom: opt(f.bottom),
                           limit_time: f.time ? `${f.time}:00+00:00` : null, live_zone_id: f.zone || null, paths: parseInt(f.paths, 10) || 10000 };
            state.busy = true; render();
            try {
                const out = await api("/api/time-machine", { method: "POST", body: JSON.stringify(body) });
                state.selected = out.run.id; state.detail = null;
                state.message = `Queued ${f.symbol} ${f.timeframe} ${f.side}. It starts within ~15 s if nothing else is running in the sandbox.`;
            } catch (e) { state.error = e.message; }
            state.busy = false;
            await refresh(true);
        }
    });

    const sync = () => {
        const show = visible();
        panel.classList.toggle("d-none", !show);
        if (show && !panel.dataset.loaded) { panel.dataset.loaded = "1"; refresh(true); }
        else if (show) refresh();
    };
    new MutationObserver(sync).observe(labSection, { attributes: true, attributeFilter: ["class"] });
    sync();
})();
