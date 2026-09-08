"""
Deprecated compatibility entrypoint for LEBPRIM backtests.

The project now uses one unified strategy engine for LIVE, DEMO, DRY_RUN, and
BACKTEST. This module intentionally no longer injects or rewires
LebprimStrategy. It simply submits an XAU_LEBPRIM-only request to the normal
BacktestRunner so external scripts that import run_lebprim_backtest keep working.
New code should call services.backtest_runner.BacktestRunner or
trading_bot.backtest.runner.BacktestRunner directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.backtest_runner import BacktestRunner
from services.database import DatabaseService
from utils import load_runtime_config


def run_lebprim_backtest(
    base_dir: str | Path = ".",
    start_date: str = "2026-01-01",
    end_date: str = "2026-03-31",
    initial_balance: float = 10000.0,
    risk_percent: float = 0.5,
    spread_points: float = 20.0,
    execution_model: str = "current_bar_close",
) -> dict[str, Any]:
    """Run a LEBPRIM-only backtest through the unified backtest runner."""
    base = Path(base_dir)
    config = load_runtime_config(base, require_mt5_credentials=False, apply_mode_env_override=False)

    db = DatabaseService(base / config["storage"]["database_path"])
    runner = BacktestRunner(base, config, db)

    request = {
        "symbol": config["mt5"]["symbol"],
        "timeframe": "M1",
        "start_date": start_date,
        "end_date": end_date,
        "enabled_strategies": ["XAU_LEBPRIM"],
        "session_filter": {"enabled": False, "allowed_sessions": []},
        "initial_balance": initial_balance,
        "risk_percent": risk_percent,
        "spread_model": {"type": "fixed_points", "points": spread_points},
        "slippage_model": {"type": "fixed_points", "points": 1.0},
        "execution_model": execution_model,
        "notes": f"LEBPRIM backtest {start_date} to {end_date}",
    }
    return runner.run(request)


if __name__ == "__main__":
    import sys

    start = sys.argv[1] if len(sys.argv) > 1 else "2026-02-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-03-25"
    print(f"Running LEBPRIM backtest: {start} to {end}")
    result = run_lebprim_backtest(
        base_dir=".",
        start_date=start,
        end_date=end,
        initial_balance=10000.0,
        risk_percent=0.5,
    )
    summary = result.get("summary", {})
    print("\n=== LEBPRIM RESULTS ===")
    print(f"Trades:        {summary.get('total_trades', 0)}")
    print(f"Win rate:      {summary.get('win_rate', 0):.1f}%")
    print(f"Net profit:    ${summary.get('net_profit', 0):,.2f}")
    print(f"Profit factor: {summary.get('profit_factor', 0):.2f}")
    print(f"Max drawdown:  {summary.get('max_drawdown', 0):.1f}%")
    print(f"Avg R:         {summary.get('average_r', 0):.3f}")
    print(f"Best strategy: {summary.get('best_strategy', 'N/A')}")
