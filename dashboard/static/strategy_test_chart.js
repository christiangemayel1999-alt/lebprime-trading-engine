// Trade chart for a finished strategy test (Indicator Lab > Strategy test). Display only.
// Draws the tested symbol/timeframe as candles with every zone as a box (creation -> end), the limit
// order and entry fill, SL and TP lines (or the trailing stop's step path) and the exit with its result.
// Candles come in windows from /api/strategy-tests/<id>/chart/bars; more load as you scroll.
// Uses TradingView Lightweight Charts 4.2.0 (static/vendor, Apache 2.0).
(() => {
    "use strict";
    const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const fmtR = (v) => (v === null || v === undefined ? "–" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(2)}R`);
    const fmtT = (t) => (t ? new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") : "–");
    const COLORS = {
        buy: "#26a69a", sell: "#ef5350", buyFill: "rgba(38,166,154,0.16)", sellFill: "rgba(239,83,80,0.16)",
        fadedBuy: "rgba(38,166,154,0.05)", fadedSell: "rgba(239,83,80,0.05)", sl: "#ef5350", tp: "#26a69a",
        entry: "#d1d4dc", limit: "rgba(209,212,220,0.55)", trail: "#f5a623", select: "#4f8cff",
    };

    function exitLabel(z) {
        const s = z.state || "";
        if (z.stops && z.stops.length && (s === "SL_HIT" || s === "BE_EXIT")) return "Trail stop";
        if (s === "TP2_HIT") return "TP2";
        if (s === "TP1_FINAL") return "TP1 + close";
        if (s === "BE_EXIT") return "BE";
        if (s === "EARLY_EXIT") return "Early exit";
        if (s === "SL_HIT") return "SL";
        return s.replace(/_/g, " ").toLowerCase();
    }

    async function api(path) {
        const response = await fetch(path);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
        return payload;
    }

    class TradeChart {
        constructor(host) {
            this.host = host;
            this.testId = null; this.symbol = null; this.symbols = [];
            this.candles = []; this.times = []; this.fromIndex = 0; this.total = 0;
            this.zones = new Map(); this.trades = []; this.selected = -1;
            this.loading = false; this.showUntraded = true; this.barSeconds = 60;
            this.chart = null; this.series = null;
        }

        // ------------------------------------------------------------ public
        async open(testId, symbols) {
            if (!window.LightweightCharts) { this.host.innerHTML = `<div class="small text-secondary">Chart library did not load.</div>`; return; }
            if (testId === this.testId && symbols.join() === this.symbols.join()) return;
            this.testId = testId; this.symbols = symbols;
            this.buildDom();
            if (!symbols.length) {
                this.body.innerHTML = `<div class="small text-secondary">No chart for this test. Tests run before the chart was added have none: run it again to get one.</div>`;
                return;
            }
            await this.loadSymbol(symbols[0]);
        }

        clear() { this.testId = null; this.symbols = []; if (this.chart) { this.chart.remove(); this.chart = null; } this.host.innerHTML = ""; }

        // ------------------------------------------------------------ DOM
        buildDom() {
            if (this.chart) { this.chart.remove(); this.chart = null; }
            const options = this.symbols.map((s) => `<option>${esc(s)}</option>`).join("");
            this.host.innerHTML = `
            <h6 class="mt-3 mb-2">Trade chart</h6>
            <div class="d-flex flex-wrap gap-2 align-items-center mb-2 small">
                ${this.symbols.length > 1 ? `<select class="form-select form-select-sm w-auto" data-tc="symbol">${options}</select>` : ""}
                <button class="btn btn-sm btn-outline-light" data-tc="prev">◀ Prev trade</button>
                <button class="btn btn-sm btn-outline-light" data-tc="next">Next trade ▶</button>
                <span data-tc="counter" class="text-secondary"></span>
                <label class="form-check form-check-inline mb-0 ms-2"><input class="form-check-input" type="checkbox" data-tc="untraded" checked><span class="form-check-label">Zones that never traded</span></label>
                <span class="text-secondary ms-auto">Times UTC · scroll to zoom, drag to pan</span>
            </div>
            <div data-tc="body"></div>`;
            this.body = this.host.querySelector("[data-tc=body]");
            this.host.querySelector("[data-tc=symbol]")?.addEventListener("change", (e) => this.loadSymbol(e.target.value));
            this.host.querySelector("[data-tc=prev]").addEventListener("click", () => this.step(-1));
            this.host.querySelector("[data-tc=next]").addEventListener("click", () => this.step(1));
            this.host.querySelector("[data-tc=untraded]").addEventListener("change", (e) => { this.showUntraded = e.target.checked; this.requestDraw(); });
        }

        buildChart(precision) {
            if (this.chart) this.chart.remove();
            this.body.innerHTML = `
            <div class="row g-2">
                <div class="col-12 col-xl-9"><div data-tc="chart" style="position:relative;height:480px;width:100%">
                    <div data-tc="tip" class="small" style="position:absolute;z-index:5;pointer-events:none;display:none;background:rgba(20,24,33,.92);color:#d1d4dc;border:1px solid #2a2e39;border-radius:6px;padding:6px 8px;max-width:280px"></div>
                </div>
                <div class="small text-secondary mt-1">Box = zone from creation to end (faded = never traded). Dotted = limit order waiting, ▲▼ = entry fill, red = SL, orange steps = trailing stop, green dashed = TP1 / TP2 (Targets exit only), dotted grey = entry 2, ● = exit with net R.</div></div>
                <div class="col-12 col-xl-3"><div data-tc="list" style="max-height:480px;overflow:auto"></div></div>
            </div>`;
            const el = this.body.querySelector("[data-tc=chart]");
            this.tip = this.body.querySelector("[data-tc=tip]");
            this.list = this.body.querySelector("[data-tc=list]");
            const LC = window.LightweightCharts;
            this.chart = LC.createChart(el, {
                autoSize: true,
                layout: { background: { type: "solid", color: "#131722" }, textColor: "#b2b5be" },
                grid: { vertLines: { color: "rgba(42,46,57,0.6)" }, horzLines: { color: "rgba(42,46,57,0.6)" } },
                crosshair: { mode: LC.CrosshairMode.Normal },
                rightPriceScale: { borderColor: "#2a2e39" },
                timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false, rightOffset: 10 },
            });
            this.series = this.chart.addCandlestickSeries({
                upColor: COLORS.buy, downColor: COLORS.sell, borderVisible: false, wickUpColor: COLORS.buy, wickDownColor: COLORS.sell,
                priceFormat: { type: "price", precision, minMove: Math.pow(10, -precision) },
            });
            this.series.attachPrimitive(this.primitive());
            this.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => this.onRange(range));
            this.chart.subscribeCrosshairMove((param) => this.onHover(param));
            this.chart.subscribeClick((param) => this.onClick(param));
        }

        // ------------------------------------------------------------ data
        async loadSymbol(symbol) {
            this.symbol = symbol;
            // Forget the previous symbol's candles, so the first trade always loads its own window.
            this.candles = []; this.times = []; this.fromIndex = 0; this.total = 0; this.zones = new Map(); this.selected = -1;
            if (this.chart) { this.chart.remove(); this.chart = null; }
            this.body.innerHTML = `<div class="small text-secondary">Loading chart…</div>`;
            try {
                const meta = await api(`/api/strategy-tests/${encodeURIComponent(this.testId)}/chart?symbol=${encodeURIComponent(symbol)}`);
                this.meta = meta; this.trades = meta.trades || []; this.trail = meta.exit_mode === "Trail"; this.barSeconds = meta.bar_seconds || 60;
                this.buildChart(meta.precision ?? 2);
                this.renderList();
                if (this.trades.length) await this.select(0);
                else await this.loadWindow({ t0: meta.last, t1: meta.last });
            } catch (error) {
                this.body.innerHTML = `<div class="alert alert-warning py-2 small mb-0">${esc(error.message)}</div>`;
            }
        }

        async fetchBars(params) {
            const q = new URLSearchParams({ symbol: this.symbol });
            Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null) q.set(k, String(Math.round(v))); });
            return api(`/api/strategy-tests/${encodeURIComponent(this.testId)}/chart/bars?${q}`);
        }

        toCandles(rows) { return rows.map(([time, open, high, low, close]) => ({ time, open, high, low, close })); }

        mergeZones(list) { list.forEach((z) => this.zones.set(z.id, z)); }

        setData() {
            this.times = this.candles.map((c) => c.time);
            this.series.setData(this.candles);
            this.updateMarkers();
        }

        async loadWindow(params) {
            // Replace what is loaded with a fresh window (used to jump to a trade that is not loaded).
            this.loading = true;
            try {
                const out = await this.fetchBars(params);
                this.candles = this.toCandles(out.candles); this.fromIndex = out.from_index; this.total = out.total;
                this.zones = new Map(); this.mergeZones(out.zones);
                this.setData();
            } finally { this.loading = false; }
        }

        async onRange(range) {
            this.requestDraw();
            if (!range || this.loading || !this.candles.length) return;
            const n = this.candles.length;
            if (range.from < 60 && this.fromIndex > 0) {
                this.loading = true;
                try {
                    const i1 = this.fromIndex - 1, i0 = Math.max(0, i1 - 2999);
                    const out = await this.fetchBars({ i0, i1 });
                    const add = this.toCandles(out.candles);
                    if (add.length) {
                        const view = this.chart.timeScale().getVisibleLogicalRange();
                        this.candles = add.concat(this.candles); this.fromIndex = out.from_index;
                        this.mergeZones(out.zones); this.setData();
                        if (view) this.chart.timeScale().setVisibleLogicalRange({ from: view.from + add.length, to: view.to + add.length });
                    }
                } finally { this.loading = false; }
            } else if (range.to > n - 60 && this.fromIndex + n < this.total) {
                this.loading = true;
                try {
                    const i0 = this.fromIndex + n, i1 = i0 + 2999;
                    const out = await this.fetchBars({ i0, i1 });
                    const add = this.toCandles(out.candles);
                    if (add.length) {
                        const view = this.chart.timeScale().getVisibleLogicalRange();
                        this.candles = this.candles.concat(add); this.mergeZones(out.zones); this.setData();
                        if (view) this.chart.timeScale().setVisibleLogicalRange(view);
                    }
                } finally { this.loading = false; }
            }
        }

        // ------------------------------------------------------------ trades
        renderList() {
            if (!this.trades.length) { this.list.innerHTML = `<div class="small text-secondary">No trades for ${esc(this.symbol)} in this test.</div>`; return; }
            const rows = this.trades.map((t, i) => `<tr data-tc-trade="${i}" role="button" class="${i === this.selected ? "table-active" : ""}">
                <td class="text-secondary">${i + 1}</td>
                <td class="${t.side === "BUY" ? "text-success" : "text-danger"}">${t.side === "BUY" ? "Buy" : "Sell"}</td>
                <td class="text-nowrap">${esc(fmtT(t.entry_t))}</td>
                <td>${esc(exitLabel(t))}</td>
                <td class="text-end ${t.net_r > 0 ? "text-success" : t.net_r < 0 ? "text-danger" : ""}">${esc(fmtR(t.net_r))}</td></tr>`).join("");
            this.list.innerHTML = `<table class="table table-sm table-hover mb-0 small"><thead><tr><th>#</th><th>Side</th><th>Entry (UTC)</th><th>Exit</th><th class="text-end">Net</th></tr></thead><tbody>${rows}</tbody></table>`;
            this.list.querySelectorAll("[data-tc-trade]").forEach((row) => row.addEventListener("click", () => this.select(Number(row.dataset.tcTrade))));
        }

        async step(delta) {
            if (!this.trades.length) return;
            const next = this.selected < 0 ? 0 : Math.min(this.trades.length - 1, Math.max(0, this.selected + delta));
            await this.select(next);
        }

        async select(index) {
            const t = this.trades[index];
            if (!t) return;
            this.selected = index;
            const start = t.created, end = t.ended || t.entry_t || t.created;
            if (!this.times.length || start < this.times[0] || end > this.times[this.times.length - 1]) {
                await this.loadWindow({ t0: start, t1: end });
            }
            const a = this.logical(start), b = this.logical(end);
            const span = Math.max(80, b - a);
            this.chart.timeScale().setVisibleLogicalRange({ from: a - span * 0.35, to: b + span * 0.35 });
            this.updateMarkers();
            this.requestDraw();
            const counter = this.host.querySelector("[data-tc=counter]");
            if (counter) counter.textContent = `Trade ${index + 1} of ${this.trades.length} · ${t.side === "BUY" ? "Buy" : "Sell"} ${exitLabel(t)} ${fmtR(t.net_r)}`;
            this.list.querySelectorAll("[data-tc-trade]").forEach((row) => row.classList.toggle("table-active", Number(row.dataset.tcTrade) === index));
            this.list.querySelector(`[data-tc-trade="${index}"]`)?.scrollIntoView({ block: "nearest" });
        }

        updateMarkers() {
            if (!this.series || !this.times.length) return;
            const first = this.times[0], last = this.times[this.times.length - 1];
            const sel = this.trades[this.selected];
            const markers = [];
            for (const z of this.zones.values()) {
                if (!z.trade || !z.entry_t) continue;
                const buy = z.side === "BUY", picked = sel && sel.id === z.id;
                if (z.entry_t >= first && z.entry_t <= last) {
                    markers.push({ time: this.snap(z.entry_t), position: buy ? "belowBar" : "aboveBar", color: buy ? COLORS.buy : COLORS.sell,
                        shape: buy ? "arrowUp" : "arrowDown", text: picked ? (buy ? "Buy" : "Sell") : "", size: picked ? 1.4 : 1 });
                }
                if (picked && z.tp1_hit && z.tp1_hit !== z.ended && z.tp1_hit >= first && z.tp1_hit <= last) {
                    markers.push({ time: this.snap(z.tp1_hit), position: buy ? "aboveBar" : "belowBar", color: COLORS.tp, shape: "square", text: "TP1", size: 0.7 });
                }
                if (z.ended && z.ended >= first && z.ended <= last) {
                    markers.push({ time: this.snap(z.ended), position: buy ? "aboveBar" : "belowBar", color: z.net_r > 0 ? COLORS.buy : z.net_r < 0 ? COLORS.sell : "#b2b5be",
                        shape: "circle", text: `${exitLabel(z)} ${fmtR(z.net_r)}`, size: picked ? 1.2 : 0.8 });
                }
            }
            markers.sort((a, b) => a.time - b.time);
            this.series.setMarkers(markers);
        }

        // ------------------------------------------------------------ coordinates
        index(t) {  // last loaded bar at or before t (-1 if before the first)
            let lo = 0, hi = this.times.length - 1, ans = -1;
            while (lo <= hi) { const mid = (lo + hi) >> 1; if (this.times[mid] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
            return ans;
        }

        snap(t) { const i = this.index(t); return i < 0 ? this.times[0] : this.times[i]; }

        logical(t) {
            const n = this.times.length;
            if (!n) return 0;
            if (t <= this.times[0]) return (t - this.times[0]) / this.barSeconds;
            if (t >= this.times[n - 1]) return n - 1 + (t - this.times[n - 1]) / this.barSeconds;
            const i = this.index(t);
            return i + (t - this.times[i]) / Math.max(1, this.times[i + 1] - this.times[i]);
        }

        x(t) { return this.chart.timeScale().logicalToCoordinate(this.logical(t)); }

        y(p) { return this.series.priceToCoordinate(p); }

        requestDraw() { if (this.requestUpdate) this.requestUpdate(); }

        // ------------------------------------------------------------ drawing
        primitive() {
            const self = this;
            const renderer = { draw(target) { target.useMediaCoordinateSpace(({ context }) => self.draw(context)); } };
            const view = { renderer: () => renderer, zOrder: () => "bottom" };
            return {
                attached({ requestUpdate }) { self.requestUpdate = requestUpdate; },
                detached() { self.requestUpdate = null; },
                updateAllViews() {},
                paneViews: () => [view],
            };
        }

        visibleTimes() {
            const r = this.chart.timeScale().getVisibleLogicalRange();
            if (!r || !this.times.length) return null;
            const n = this.times.length;
            const clamp = (i) => Math.min(n - 1, Math.max(0, Math.round(i)));
            return [this.times[clamp(r.from)] - this.barSeconds * Math.max(0, -r.from), this.times[clamp(r.to)] + this.barSeconds * Math.max(0, r.to - n)];
        }

        line(ctx, x0, x1, y, color, width = 1, dash = []) {
            if (x0 === null || x1 === null || y === null) return;
            ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
            ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
        }

        draw(ctx) {
            if (!this.chart || !this.times.length) return;
            const vis = this.visibleTimes();
            if (!vis) return;
            const sel = this.trades[this.selected];
            const lastT = this.times[this.times.length - 1];
            ctx.save();
            for (const z of this.zones.values()) {
                const end = z.ended || lastT;
                if (z.created > vis[1] || end < vis[0]) continue;
                if (!z.trade && !this.showUntraded) continue;
                const buy = z.side === "BUY", picked = sel && sel.id === z.id;
                const x0 = this.x(z.created), x1 = this.x(end), yt = this.y(z.top), yb = this.y(z.bottom);
                if (x0 === null || x1 === null || yt === null || yb === null) continue;
                // Zone box
                ctx.setLineDash(z.trade ? [] : [3, 3]);
                ctx.fillStyle = z.trade ? (buy ? COLORS.buyFill : COLORS.sellFill) : (buy ? COLORS.fadedBuy : COLORS.fadedSell);
                ctx.fillRect(x0, Math.min(yt, yb), Math.max(1, x1 - x0), Math.abs(yb - yt) || 1);
                ctx.strokeStyle = picked ? COLORS.select : (buy ? COLORS.buy : COLORS.sell);
                ctx.globalAlpha = z.trade ? 0.9 : 0.35; ctx.lineWidth = picked ? 2 : 1;
                ctx.strokeRect(x0, Math.min(yt, yb), Math.max(1, x1 - x0), Math.abs(yb - yt) || 1);
                ctx.globalAlpha = 1;
                if (!z.trade) continue;
                const w = picked ? 2 : 1;
                const entryPrice = z.fill ?? z.entry;
                // Limit order waiting (dotted), then the position from fill to exit.
                if (z.limit && z.entry_t) this.line(ctx, this.x(z.limit), this.x(z.entry_t), this.y(z.entry), COLORS.limit, 1, [2, 3]);
                if (!z.entry_t) continue;
                const xe = this.x(z.entry_t), xx = this.x(end);
                this.line(ctx, xe, xx, this.y(entryPrice), COLORS.entry, w);
                if (z.entry2_t) {  // second entry (far edge) and when it filled
                    if (z.limit) this.line(ctx, this.x(z.limit), this.x(z.entry2_t), this.y(z.entry2), COLORS.limit, 1, [2, 3]);
                    this.line(ctx, this.x(z.entry2_t), xx, this.y(z.fill2 ?? z.entry2), COLORS.entry, w, [1, 2]);
                }
                if (!this.trail) {
                    if (z.tp1 !== undefined && z.tp1 !== z.tp) this.line(ctx, xe, z.tp1_hit ? this.x(z.tp1_hit) : xx, this.y(z.tp1), COLORS.tp, w, [2, 3]);
                    if (z.tp !== undefined) this.line(ctx, xe, xx, this.y(z.tp), COLORS.tp, w, [6, 4]);
                }
                if (z.stops && z.stops.length) {
                    // Trailing stop: start at the zone SL, step to each new stop level when the engine moved it.
                    ctx.strokeStyle = COLORS.trail; ctx.lineWidth = w + 0.5; ctx.setLineDash([]);
                    ctx.beginPath();
                    let price = z.sl, xPrev = xe;
                    ctx.moveTo(xe, this.y(price));
                    for (const [t, p] of z.stops) {
                        const xs = this.x(t);
                        if (xs === null) continue;
                        ctx.lineTo(xs, this.y(price)); ctx.lineTo(xs, this.y(p)); price = p; xPrev = xs;
                    }
                    ctx.lineTo(Math.max(xPrev, xx), this.y(price));
                    ctx.stroke();
                    this.line(ctx, xe, this.x(z.stops[0][0]), this.y(z.sl), COLORS.sl, w);
                } else if (z.sl !== undefined) {
                    this.line(ctx, xe, xx, this.y(z.sl), COLORS.sl, w);
                }
                if (picked && z.exit_price !== undefined) {
                    ctx.fillStyle = z.net_r > 0 ? COLORS.buy : COLORS.sell;
                    ctx.beginPath(); ctx.arc(xx, this.y(z.exit_price), 4, 0, Math.PI * 2); ctx.fill();
                }
            }
            ctx.restore();
        }

        // ------------------------------------------------------------ hover / click
        zoneAt(param) {
            if (!param || !param.point || param.time === undefined) return null;
            const t = Number(param.time), price = this.series.coordinateToPrice(param.point.y);
            if (price === null) return null;
            const lastT = this.times[this.times.length - 1];
            let best = null;
            for (const z of this.zones.values()) {
                if (!z.trade && !this.showUntraded) continue;
                const end = z.ended || lastT;
                if (t < z.created || t > end) continue;
                const tp = this.trail ? undefined : z.tp;
                const lo = Math.min(z.bottom, z.sl ?? z.bottom, tp ?? z.bottom), hi = Math.max(z.top, z.sl ?? z.top, tp ?? z.top);
                const inBox = price >= z.bottom && price <= z.top;
                const inTrade = z.trade && z.entry_t && t >= z.entry_t && price >= lo && price <= hi;
                if (!inBox && !inTrade) continue;
                if (!best || (z.trade && !best.trade)) best = z;
            }
            return best;
        }

        onHover(param) {
            const z = this.zoneAt(param);
            if (!z) { this.tip.style.display = "none"; return; }
            const d = this.meta?.precision ?? 2;
            const p = (v) => (v === undefined || v === null ? "–" : Number(v).toFixed(d));
            const lines = [
                `<strong class="${z.side === "BUY" ? "text-success" : "text-danger"}">${z.side === "BUY" ? "Demand (buy)" : "Supply (sell)"} zone</strong> · ${esc((z.state || "").replace(/_/g, " "))}`,
                `Created ${esc(fmtT(z.created))} · ended ${esc(fmtT(z.ended))}`,
                `Zone ${p(z.bottom)} – ${p(z.top)}`,
            ];
            if (z.limit) lines.push(`Limit placed ${esc(fmtT(z.limit))} at ${p(z.entry)}`);
            if (z.entry_t) lines.push(`Filled ${esc(fmtT(z.entry_t))} at ${p(z.fill ?? z.entry)}`);
            if (z.entry2_t) lines.push(`Entry 2 filled ${esc(fmtT(z.entry2_t))} at ${p(z.fill2 ?? z.entry2)}`);
            if (z.sl !== undefined) lines.push(`SL ${p(z.sl)}${this.trail ? " · trailing exit, no TP" : z.tp1 !== undefined && z.tp1 !== z.tp ? ` · TP1 ${p(z.tp1)}${z.tp1_hit ? " (hit)" : ""} · TP2 ${p(z.tp)}` : z.tp !== undefined ? ` · TP ${p(z.tp)}` : ""}${z.stops ? ` · stop moved ${z.stops.length}×` : ""}`);
            if (z.trade && z.reason) lines.push(`<span class="text-secondary">${esc(z.reason)}</span>`);
            if (z.trade) lines.push(`Exit ${esc(exitLabel(z))} at ${p(z.exit_price)} · gross ${esc(fmtR(z.gross_r))} · <strong>net ${esc(fmtR(z.net_r))}</strong>`);
            else if (z.reason) lines.push(esc(z.reason));
            if (z.trade) lines.push(`<span class="text-secondary">Click to select this trade</span>`);
            this.tip.innerHTML = lines.join("<br>");
            this.tip.style.display = "block";
            const box = this.tip.parentElement.getBoundingClientRect();
            const left = param.point.x + 16 + 280 > box.width ? param.point.x - 296 : param.point.x + 16;
            this.tip.style.left = `${Math.max(4, left)}px`;
            this.tip.style.top = `${Math.max(4, Math.min(param.point.y + 12, box.height - this.tip.offsetHeight - 30))}px`;
        }

        onClick(param) {
            const z = this.zoneAt(param);
            if (!z || !z.trade) return;
            const i = this.trades.findIndex((t) => t.id === z.id);
            if (i >= 0 && i !== this.selected) this.select(i);
        }
    }

    window.LabTradeChart = TradeChart;
})();
