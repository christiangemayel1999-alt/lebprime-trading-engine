"""Compare a TradingView parity export with the Python LebPrime engine.

Usage:
    python scripts/run_pine_parity.py EXPORT.csv --symbol XAUUSD --timeframe M15 [--history-bars 5000]

The CSV must come from "Export chart data" on a chart running
docs/indicator_reference/LebPrime_Signals_v3_4_1_parity_export.pine, with the indicator's
"Recent history bars" input set to the same value as --history-bars.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.indicator_lab.parity import compare_tradingview_export  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_csv")
    parser.add_argument("--symbol", required=True, help="Ticker as on the TradingView chart, e.g. XAUUSD, EURUSD, US30")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--history-bars", type=int, default=5000)
    parser.add_argument("--out", help="Where to write the JSON report (default: reports/parity_<symbol>_<tf>.json)")
    args = parser.parse_args()

    report = compare_tradingview_export(args.export_csv, symbol=args.symbol, timeframe=args.timeframe, history_bars=args.history_bars)
    out = Path(args.out) if args.out else ROOT / "reports" / f"parity_{args.symbol}_{args.timeframe}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{report['symbol']} {report['timeframe']}: {report['bars']} bars, {report['first_bar']} .. {report['last_bar']}")
    created_tv = report["tradingview_events"].get("LP B CREATED", 0) + report["tradingview_events"].get("LP S CREATED", 0)
    created_py = report["python_events"].get("LP B CREATED", 0) + report["python_events"].get("LP S CREATED", 0)
    print(f"LIMIT CREATED: TradingView {created_tv}, Python {created_py}")
    print(f"Match rate {report['match_rate'] * 100:.2f}% ({report['mismatch_count']} mismatching event slots)")
    for item in report["examples"][:15]:
        print(f"  {item['time']}  {item['slot']:<16} TV={item['tradingview']}  PY={item['python']}")
    print(f"Report: {out}")
    return 0 if report["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
