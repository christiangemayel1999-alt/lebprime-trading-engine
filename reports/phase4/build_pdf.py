"""PDF of every tested setting and period, built from the research folder."""
import json, os
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
R = "LebPrime AI Research"; B = f"{R}/02 - Backtest Results"
st = getSampleStyleSheet(); small = st["BodyText"].clone("s", fontSize=6.5, leading=8); body = st["BodyText"].clone("b", fontSize=9, leading=11)
def tbl(rows, widths=None, font=6.5):
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), font), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
                           ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t
def fmt(v, d=1):
    if v is None or (isinstance(v, float) and pd.isna(v)): return ""
    return f"{v:+.{d}f}" if isinstance(v, (int, float)) else str(v)
story = [Paragraph("LebPrime bot: all backtest results", st["Title"]),
         Paragraph(f"Generated {pd.Timestamp.now():%d %b %Y %H:%M}. All results include spread and commission. R = risk from Entry 1 to SL on the whole position. "
                   "Period A / B are the two periods named in each row (usually tune / check).", body), Spacer(1, 6)]
story += [Paragraph("1. Settings that passed every period", st["Heading2"])]
passed = [["Setting", "Market", "Result (continuous 2020-2026 unless noted)", "Notes"],
          ["G1B: G1 buys only + 30-day zone expiry", "Gold M5", "1,136 trades, 69% wins, +146.7R, PF 1.29, max DD 16.4R, every year positive", "+90R with 0.05R slippage; strongest result"],
          ["G3: London only, ADX 20-40, spread<=5% risk, expiry 300", "Gold M15", "864 trades, 67% wins, +66.7R, PF 1.15, max DD 33.5R", "2021 -1R, 2022 -16R; +23.5R with 0.05R slippage"],
          ["Sweep reclaim, sells only (2026 regime search)", "USTEC M30", "+6.6 / +2.9 (2026), +0.3 / +3.6 (2025), +13.8 / +9.0 (2020-24)", "about 190 trades in total"],
          ["Sweep reclaim + H4 trend + real base, TP 2R", "US30 M5", "positive in all 6 periods", "few trades (about 30 in 5 recent months)"]]
story += [tbl(passed, [70*mm, 22*mm, 110*mm, 70*mm], 7.5), Spacer(1, 6)]
story += [Paragraph("Root cause of losses: the candle-close stop exits about 0.66R past the SL, so the average loss (-1.34 to -1.43R) is larger than the average win "
                    "(+0.72 to +0.96R) even at 55-67% win rate. TradingView scores every SL as -1R, which is why it shows a profit. A 30-zone cap per side with no "
                    "expiry stops new zones in long trends (fixed by zone expiry).", body)]
for name, title in (("G1buys_exp8640_gold_M5_summary.json", "G1B Gold M5"), ("G3_gold_M15_summary.json", "G3 Gold M15"), ("G1_gold_M5_summary.json", "G1 Gold M5 (buys and sells)")):
    p = f"{B}/{name}"
    if not os.path.exists(p): continue
    d = json.load(open(p))
    rows = [["Group", "Trades", "Win %", "Net R", "Avg R", "Avg win", "Avg loss", "PF", "Max DD R"]]
    for lab, v in [("All", d["all"]), ("Spread x1.5", d["spread_x1.5"]), ("+0.02R slippage", d["slip_0.02R"]), ("+0.05R slippage", d["slip_0.05R"])] + \
            [(f"Year {y}", v) for y, v in d["by_year"].items()] + [(f"Side {k}", v) for k, v in d["by_side"].items()] + [(f"Legs filled {k}", v) for k, v in d["by_legs"].items()]:
        rows.append([lab, v["trades"], v["win_pct"], fmt(v["net_r"]), fmt(v["avg_r"], 3), fmt(v["avg_win"], 2), fmt(v["avg_loss"], 2), v["profit_factor"], v["max_dd_r"]])
    story += [Spacer(1, 6), Paragraph(f"{title}: continuous trade list summary", st["Heading3"]), tbl(rows, None, 7)]
for name, title in (("regime_validation.csv", "2. 2026 uptrend/downtrend winners checked on older years"), ("per_symbol_pass_fail.csv", "3. Per-symbol search: candidates that reached 2020-2024"),
                    ("ustec_deep_pass_fail.csv", "4. USTEC deep search: candidates that reached 2020-2024"), ("M2_pass_fail.csv", "5. M2 search: candidates that reached 2020-2024")):
    p = f"{B}/{name}"
    if not os.path.exists(p) or os.path.getsize(p) < 5: continue
    t = pd.read_csv(p).drop(columns=[c for c in ("overrides",) if c in pd.read_csv(p).columns])
    story += [PageBreak(), Paragraph(title, st["Heading2"]), tbl([list(t.columns)] + [[fmt(x) if isinstance(x, float) else str(x) for x in r] for r in t.values.tolist()], None, 6)]
for name, title, top in (("final_review_best.csv", "FINAL REVIEW: best setup per symbol and timeframe (2020-2026, real-broker scoring, costs)", None),
                         ("final_11_setups.csv", "Final portfolio: 11 setups, direction, buy/sell split, year by year", None),
                         ("orb_trailing_by_year.csv", "NY breakout: fixed TP vs trailing stop", None),
                         ("rollover_by_year.csv", "No trades around daily close/open, nothing over the weekend", None),
                         ("momentum_by_year.csv", "Momentum candles 2-6 on each zone setup", None),
                         ("confirm_entry_by_year.csv", "LIMIT vs waiting for a reaction, then market entry", None),
                         ("orb_grid.csv", "NY/London opening-range breakout grid (top 60 by net R)", 60)):
    p = f"{B}/{name}"
    if not os.path.exists(p): continue
    t = pd.read_csv(p)
    if top: t = t.head(top)
    story += [PageBreak(), Paragraph(title, st["Heading2"]), tbl([list(t.columns)] + [[fmt(x) if isinstance(x, float) else str(x) for x in r] for r in t.values.tolist()], None, 6)]
for name, title in (("portfolio2_summary.txt", "Combined portfolio summary"), ("money_sim_1000usd.txt", "Money simulation from $1,000"), ("money_sim_risk_levels.txt", "Money simulation at 1-3% risk")):
    p = f"{B}/{name}"
    if os.path.exists(p):
        story += [PageBreak() if name == "portfolio2_summary.txt" else Spacer(1, 8), Paragraph(title, st["Heading2"])] + [Paragraph(l.replace("**", "").replace("$", "&#36;"), small) for l in open(p).read().splitlines()]
a = pd.read_csv(f"{B}/all_results.csv", low_memory=False)
story += [PageBreak(), Paragraph("6. Every test run, grouped by symbol and timeframe", st["Heading2"]),
          Paragraph(f"{len(a):,} runs. Each row: test, period A and B (trades, win %, net R), uptrend/downtrend split where recorded, and the settings changed from default. "
                    "Sorted by symbol, timeframe, then net R of period A.", body)]
a["sym"] = a.symbol.fillna("?"); a["tfx"] = a.timeframe.fillna("?")
order = {"H1": 0, "M30": 1, "M15": 2, "M5": 3, "M3": 4, "M2": 5, "M1": 6}
for (sym, tf), g in sorted(a.groupby(["sym", "tfx"]), key=lambda k: (k[0][0], order.get(k[0][1], 9))):
    g = g.sort_values("A_net_r", ascending=False)
    rows = [["Test", "Period A", "A tr", "A win%", "A net R", "Period B", "B tr", "B win%", "B net R", "Bull R", "Bear R", "Settings changed from default"]]
    for _, r in g.iterrows():
        sett = str(r.settings_changed_from_default)
        try:
            sett = ", ".join(f"{k}={v}" for k, v in json.loads(sett).items() if k not in ("tp1_execution",))
        except Exception:
            pass
        rows.append([Paragraph(str(r.test), small), str(r.period_A), fmt(r.A_trades, 0).replace("+", ""), fmt(r.A_win_pct, 0).replace("+", ""), fmt(r.A_net_r), str(r.period_B),
                     fmt(r.B_trades, 0).replace("+", ""), fmt(r.B_win_pct, 0).replace("+", ""), fmt(r.B_net_r), fmt(r.get("bull_net_r")), fmt(r.get("bear_net_r")), Paragraph(sett or "default", small)])
    story += [PageBreak(), Paragraph(f"{sym} {tf} ({len(g)} runs)", st["Heading3"]),
              tbl(rows, [32*mm, 22*mm, 9*mm, 9*mm, 12*mm, 22*mm, 9*mm, 9*mm, 12*mm, 11*mm, 11*mm, 115*mm], 6)]
out = f"{R}/LebPrime_All_Results.pdf"
SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=8*mm, rightMargin=8*mm, topMargin=8*mm, bottomMargin=8*mm, title="LebPrime all results").build(story)
print(out, os.path.getsize(out) // 1024, "KB")
