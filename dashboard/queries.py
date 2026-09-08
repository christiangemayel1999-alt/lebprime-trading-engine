"""SQLite query helpers for the Streamlit operations dashboard."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from dashboard.query_utils import safe_float, safe_int, safe_text
from services.heartbeat import HeartbeatService


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Return a SQLite connection for dashboard queries."""
    connection = sqlite3.connect(Path(db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def query_dataframe(db_path: str | Path, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    """Run a SQL query and return a dataframe, handling empty databases gracefully."""
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame()
    try:
        with get_connection(path) as connection:
            return pd.read_sql_query(sql, connection, params=params)
    except Exception:
        return pd.DataFrame()


def get_latest_heartbeat(db_path: str | Path) -> dict[str, Any]:
    """Return the most recent heartbeat row."""
    frame = query_dataframe(
        db_path,
        """
        SELECT timestamp, status, symbol, spread, trend, setup, balance, equity, open_positions
        FROM heartbeat
        ORDER BY id DESC
        LIMIT 1;
        """,
    )
    if frame.empty:
        return {}
    return frame.iloc[0].to_dict()


def get_last_signal(db_path: str | Path) -> dict[str, Any]:
    """Return the latest signal row."""
    frame = query_dataframe(
        db_path,
        """
        SELECT timestamp, symbol, side, executed, reason
        FROM signals
        ORDER BY id DESC
        LIMIT 1;
        """,
    )
    if frame.empty:
        return {}
    return frame.iloc[0].to_dict()


def get_last_error(db_path: str | Path) -> dict[str, Any]:
    """Return the latest error or critical event."""
    frame = query_dataframe(
        db_path,
        """
        SELECT timestamp, level, event_type, message
        FROM bot_events
        WHERE level IN ('ERROR', 'CRITICAL')
        ORDER BY id DESC
        LIMIT 1;
        """,
    )
    if frame.empty:
        return {}
    return frame.iloc[0].to_dict()


def get_recent_trades(db_path: str | Path, limit: int = 25) -> pd.DataFrame:
    """Return recent trades ordered by newest first."""
    return query_dataframe(
        db_path,
        """
        SELECT
               COALESCE(trade_id, position_id, mt5_ticket, ticket, order_id) AS trade_id,
               mt5_ticket,
               position_id,
               symbol,
               side,
               status,
               event_type,
               COALESCE(NULLIF(setup, ''), NULLIF(setup_fingerprint, ''), NULLIF(setup_family, '')) AS setup,
               COALESCE(NULLIF(setup_family, ''), NULLIF(setup, ''), NULLIF(setup_type, '')) AS setup_family,
               COALESCE(NULLIF(regime, ''), NULLIF(regime_at_entry, ''), 'UNKNOWN') AS regime,
               COALESCE(NULLIF(session, ''), NULLIF(session_at_entry, ''), 'UNKNOWN') AS session,
               entry_price,
               stop_loss,
               take_profit,
               exit_price,
               volume,
               pnl,
               pnl_pips,
               realized_r,
               hold_minutes,
               COALESCE(NULLIF(win_loss, ''), NULLIF(outcome_label, '')) AS win_loss,
               COALESCE(NULLIF(outcome_label, ''), CASE WHEN pnl IS NULL THEN 'OPEN' WHEN ABS(COALESCE(pnl, 0)) <= 1e-8 THEN 'BREAKEVEN' WHEN COALESCE(pnl, 0) > 0 THEN 'WIN' ELSE 'LOSS' END) AS outcome_label,
               execution_reason,
               blocked_reason,
               close_reason,
               comment,
               closed_at,
               updated_at,
               COALESCE(closed_at, exit_time, updated_at, created_at, timestamp) AS timestamp
        FROM trades
        WHERE status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
        ORDER BY COALESCE(closed_at, exit_time, updated_at, created_at, timestamp) DESC, id DESC
        LIMIT ?;
        """,
        (limit,),
    )


def get_recent_signals(db_path: str | Path, limit: int = 25) -> pd.DataFrame:
    """Return recent signals ordered by newest first."""
    return query_dataframe(
        db_path,
        """
        SELECT timestamp, symbol, side, trend_ok, setup_ok, entry_ok, executed, reason
        FROM signals
        ORDER BY id DESC
        LIMIT ?;
        """,
        (limit,),
    )


def get_recent_events(db_path: str | Path, limit: int = 25) -> pd.DataFrame:
    """Return recent bot events ordered by newest first."""
    return query_dataframe(
        db_path,
        """
        SELECT timestamp, level, event_type, message
        FROM bot_events
        ORDER BY id DESC
        LIMIT ?;
        """,
        (limit,),
    )


def get_daily_pnl_curve(db_path: str | Path) -> pd.DataFrame:
    """Return day-level PnL aggregation."""
    frame = query_dataframe(
        db_path,
        """
        SELECT
            substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10) AS day,
            COALESCE(SUM(CASE WHEN status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED') THEN pnl ELSE 0 END), 0) AS daily_pnl
        FROM trades
        WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
          AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
        GROUP BY substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10)
        ORDER BY day;
        """,
    )
    if frame.empty:
        return frame
    frame["cumulative_pnl"] = frame["daily_pnl"].cumsum()
    return frame


def get_trades_by_day(db_path: str | Path) -> pd.DataFrame:
    """Return daily trade counts."""
    return query_dataframe(
        db_path,
        """
        SELECT
            substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10) AS day,
            COUNT(*) AS trade_count
        FROM trades
        WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
          AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
        GROUP BY substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10)
        ORDER BY day;
        """,
    )


def get_win_loss_counts(db_path: str | Path) -> pd.DataFrame:
    """Return aggregate win/loss distribution."""
    return query_dataframe(
        db_path,
        """
        SELECT
            COALESCE(outcome_label, CASE WHEN pnl IS NULL THEN 'OPEN' WHEN pnl >= 0 THEN 'WIN' ELSE 'LOSS' END) AS outcome,
            COUNT(*) AS count
        FROM trades
        WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
          AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
        GROUP BY outcome
        ORDER BY outcome;
        """,
    )


def get_top_metrics(db_path: str | Path, stale_seconds: int) -> dict[str, Any]:
    """Return dashboard summary metrics."""
    latest_heartbeat = get_latest_heartbeat(db_path)
    today_stats = query_dataframe(
        db_path,
        """
        SELECT
            COALESCE(SUM(CASE WHEN status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED') THEN pnl ELSE 0 END), 0) AS today_pnl,
            SUM(CASE WHEN status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED') THEN 1 ELSE 0 END) AS today_trades,
            COALESCE(SUM(CASE WHEN outcome_label = 'WIN' THEN 1 ELSE 0 END), 0) AS today_wins,
            COALESCE(SUM(CASE WHEN outcome_label = 'LOSS' THEN 1 ELSE 0 END), 0) AS today_losses
        FROM trades
        WHERE substr(COALESCE(closed_at, exit_time, updated_at, timestamp), 1, 10) = date('now')
          AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN');
        """,
    )
    overall_stats = query_dataframe(
        db_path,
        """
        SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN outcome_label = 'WIN' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome_label = 'LOSS' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN outcome_label = 'BREAKEVEN' THEN 1 ELSE 0 END) AS breakevens
        FROM trades
        WHERE status IN ('CLOSED', 'FINALIZED', 'CLOSED_UNVERIFIED')
          AND status NOT IN ('MERGED_DUPLICATE', 'IGNORED_ORPHAN')
        """,
    )

    today_row = today_stats.iloc[0] if not today_stats.empty else {}
    overall_row = overall_stats.iloc[0] if not overall_stats.empty else {}
    today_pnl = safe_float(today_row.get("today_pnl"))
    today_trades = safe_int(today_row.get("today_trades"))
    total_trades = safe_int(overall_row.get("total_trades"))
    wins = safe_int(overall_row.get("wins"))
    losses = safe_int(overall_row.get("losses"))
    breakevens = safe_int(overall_row.get("breakevens"))
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    status = safe_text(latest_heartbeat.get("status"), "NO DATA") if latest_heartbeat else "NO DATA"
    last_heartbeat = safe_text(latest_heartbeat.get("timestamp")) if latest_heartbeat else ""
    if HeartbeatService.is_stale(last_heartbeat, stale_seconds):
        status = "STALE"

    return {
        "bot_status": status,
        "last_heartbeat_time": last_heartbeat or "N/A",
        "balance": safe_float(latest_heartbeat.get("balance")) if latest_heartbeat else 0.0,
        "equity": safe_float(latest_heartbeat.get("equity")) if latest_heartbeat else 0.0,
        "open_positions": safe_int(latest_heartbeat.get("open_positions")) if latest_heartbeat else 0,
        "today_pnl": today_pnl,
        "today_trades": today_trades,
        "win_rate": win_rate,
        "today_wins": safe_int(today_row.get("today_wins")),
        "today_losses": safe_int(today_row.get("today_losses")),
        "total_wins": wins,
        "total_losses": losses,
        "total_breakevens": breakevens,
        "spread": safe_float(latest_heartbeat.get("spread")) if latest_heartbeat else 0.0,
        "trend": safe_text(latest_heartbeat.get("trend"), "N/A") if latest_heartbeat else "N/A",
        "setup": safe_text(latest_heartbeat.get("setup"), "N/A") if latest_heartbeat else "N/A",
        "symbol": safe_text(latest_heartbeat.get("symbol"), "N/A") if latest_heartbeat else "N/A",
    }
