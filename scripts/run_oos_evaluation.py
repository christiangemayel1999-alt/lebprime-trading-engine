"""Run the Bot V2 out-of-sample evaluation matrix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.backtest.oos_evaluation import OOSEvaluationFramework
from utils import load_runtime_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Bot V2 out-of-sample evaluation framework")
    parser.add_argument("--start-date", required=True, help="ISO start date, e.g. 2026-04-15")
    parser.add_argument("--end-date", default=None, help="ISO end date, e.g. 2026-05-15")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="M1")
    parser.add_argument("--initial-balance", type=float, default=10000.0)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument("--spread-points", type=float, default=20.0)
    parser.add_argument("--slippage-points", type=float, default=2.0)
    parser.add_argument("--execution-model", default="next_bar_open")
    parser.add_argument("--output-dir", default=None, help="Optional directory for the OOS report artifacts")
    parser.add_argument("--database-path", default=None, help="Optional sqlite path for scenario runs")
    parser.add_argument("--baseline-run-id", type=int, default=None, help="Optional existing Bot V1 run id")
    parser.add_argument("--baseline-bundle", default=None, help="Optional exported Bot V1 bundle.json or run directory")
    parser.add_argument("--baseline-database-path", default=None, help="Optional sqlite path to resolve baseline run ids")
    parser.add_argument("--enabled-strategies", default="XAU_BOT_BREAKOUT,XAU_BOT_COMPRESS,XAU_LEBPRIM")
    parser.add_argument("--allowed-sessions", default="", help="Optional comma-separated session filter for all scenarios")
    parser.add_argument("--notes", default=None, help="Optional notes stored on each scenario run")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_runtime_config(PROJECT_ROOT, require_mt5_credentials=False, apply_mode_env_override=False)
    framework = OOSEvaluationFramework(PROJECT_ROOT, config, database_path=args.database_path)
    enabled_strategies = [item.strip() for item in str(args.enabled_strategies).split(",") if item.strip()]
    allowed_sessions = [item.strip() for item in str(args.allowed_sessions).split(",") if item.strip()]
    request = {
        "symbol": str(args.symbol).upper(),
        "timeframe": str(args.timeframe).upper(),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date or args.start_date),
        "enabled_strategies": enabled_strategies,
        "session_filter": {"enabled": bool(allowed_sessions), "allowed_sessions": allowed_sessions},
        "initial_balance": float(args.initial_balance),
        "risk_percent": float(args.risk_percent if args.risk_percent is not None else config.get("risk", {}).get("risk_percent", 0.5)),
        "spread_model": {"type": "fixed_points", "points": float(args.spread_points)},
        "slippage_model": {"type": "fixed_points", "points": float(args.slippage_points)},
        "execution_model": str(args.execution_model),
        "notes": args.notes,
    }
    report = framework.run_matrix(
        request=request,
        output_dir=args.output_dir,
        baseline_run_id=args.baseline_run_id,
        baseline_bundle_path=args.baseline_bundle,
        baseline_database_path=args.baseline_database_path,
    )
    print(f"OOS evaluation written to: {report['output_dir']}")
    print(f"Summary markdown: {Path(report['output_dir']) / 'evaluation_summary.md'}")
    print(f"Report json: {Path(report['output_dir']) / 'report.json'}")


if __name__ == "__main__":
    main()
