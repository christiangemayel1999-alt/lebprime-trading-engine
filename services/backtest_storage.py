"""Persistence and aggregation helpers for backtest results."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from services.database import DatabaseService


class BacktestStorage:
    """Store, aggregate, and export replay/backtest runs."""

    def __init__(self, database: DatabaseService) -> None:
        self.database = database

    @staticmethod
    def _parse_json(value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return json.loads(value)
        except Exception:
            return value

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, (datetime, Path)):
            return str(value)
        return str(value)

    @staticmethod
    def _safe_slug(value: str, fallback: str = "run") -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
        return text or fallback

    @staticmethod
    def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame(rows if rows else [])

    @classmethod
    def _write_json(cls, path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=cls._json_default), encoding="utf-8")

    @classmethod
    def _write_csv(cls, path: Path, rows: list[dict[str, Any]]) -> None:
        frame = cls._frame(rows)
        frame.to_csv(path, index=False)

    @classmethod
    def _write_jsonl(cls, path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=cls._json_default))
                handle.write("\n")

    @staticmethod
    def _build_run_folder_name(run_payload: dict[str, Any]) -> str:
        run_id = int(run_payload.get("id") or 0)
        created_at = str(run_payload.get("created_at") or "")
        stamp = BacktestStorage._safe_slug(created_at.replace(":", "-").replace("+", "_"), fallback=f"run_{run_id}")
        symbol = BacktestStorage._safe_slug(str(run_payload.get("symbol") or "SYMBOL"), fallback="SYMBOL")
        timeframe = BacktestStorage._safe_slug(str(run_payload.get("timeframe") or "TF"), fallback="TF")
        return f"run_{run_id:04d}_{stamp}_{symbol}_{timeframe}"

    @staticmethod
    def _equity_chart_html(bundle: dict[str, Any]) -> str:
        equity = bundle.get("equity_curve") or []
        if not equity:
            points_js = "[]"
            min_equity = 0.0
            max_equity = 1.0
        else:
            points = []
            for idx, row in enumerate(equity):
                points.append(
                    {
                        "x": idx,
                        "equity": float(row.get("equity", 0.0) or 0.0),
                        "balance": float(row.get("balance", 0.0) or 0.0),
                        "ts": str(row.get("ts") or ""),
                    }
                )
            min_equity = min(min(p["equity"], p["balance"]) for p in points)
            max_equity = max(max(p["equity"], p["balance"]) for p in points)
            points_js = json.dumps(points)

        summary = bundle.get("summary") or {}
        title = f"Backtest Run {bundle.get('run', {}).get('id', '')}"
        return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>{title} - Equity Curve</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; background: #111827; color: #f9fafb; }}
    .meta {{ margin-bottom: 18px; color: #d1d5db; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .card {{ background: #1f2937; padding: 12px; border-radius: 10px; }}
    .label {{ font-size: 12px; color: #9ca3af; }}
    .value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
    svg {{ width: 100%; height: 420px; background: #0f172a; border-radius: 12px; }}
    .axis {{ stroke: #6b7280; stroke-width: 1; }}
    .equity {{ fill: none; stroke: #22c55e; stroke-width: 2.5; }}
    .balance {{ fill: none; stroke: #38bdf8; stroke-width: 2; stroke-dasharray: 5 4; }}
    .legend {{ margin-top: 10px; color: #d1d5db; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class=\"meta\">Saved from Trading Bot backtest exporter.</div>
  <div class=\"grid\">
    <div class=\"card\"><div class=\"label\">Net Profit</div><div class=\"value\">{float(summary.get('net_profit', 0.0)):.2f}</div></div>
    <div class=\"card\"><div class=\"label\">Profit Factor</div><div class=\"value\">{float(summary.get('profit_factor', 0.0)):.2f}</div></div>
    <div class=\"card\"><div class=\"label\">Win Rate</div><div class=\"value\">{float(summary.get('win_rate', 0.0)):.2f}%</div></div>
    <div class=\"card\"><div class=\"label\">Max Drawdown</div><div class=\"value\">{float(summary.get('max_drawdown', 0.0)):.2f}%</div></div>
  </div>
  <svg id=\"chart\" viewBox=\"0 0 1200 420\" preserveAspectRatio=\"none\"></svg>
  <div class=\"legend\">Green = equity, blue dashed = balance</div>
  <script>
    const points = {points_js};
    const svg = document.getElementById('chart');
    const width = 1200;
    const height = 420;
    const pad = 40;
    const minY = {min_equity};
    const maxY = {max_equity};
    function sx(i) {{
      if (points.length <= 1) return pad;
      return pad + (i / (points.length - 1)) * (width - pad * 2);
    }}
    function sy(v) {{
      if (maxY <= minY) return height / 2;
      return height - pad - ((v - minY) / (maxY - minY)) * (height - pad * 2);
    }}
    function makePath(key) {{
      return points.map((p, i) => `${{i === 0 ? 'M' : 'L'}}${{sx(i).toFixed(2)}},${{sy(p[key]).toFixed(2)}}`).join(' ');
    }}
    svg.innerHTML = `
      <line class=\"axis\" x1=\"${{pad}}\" y1=\"${{height - pad}}\" x2=\"${{width - pad}}\" y2=\"${{height - pad}}\"></line>
      <line class=\"axis\" x1=\"${{pad}}\" y1=\"${{pad}}\" x2=\"${{pad}}\" y2=\"${{height - pad}}\"></line>
      <path class=\"equity\" d=\"${{makePath('equity')}}\"></path>
      <path class=\"balance\" d=\"${{makePath('balance')}}\"></path>
    `;
  </script>
</body>
</html>
"""

    def create_run(self, payload: dict[str, Any]) -> int:
        return self.database.create_backtest_run(payload)

    def store_signal(self, payload: dict[str, Any]) -> None:
        self.database.insert_backtest_signal(payload)

    def store_trade(self, payload: dict[str, Any]) -> None:
        self.database.insert_backtest_trade(payload)

    def store_equity(self, payload: dict[str, Any]) -> None:
        self.database.insert_backtest_equity_point(payload)

    def store_playback_frame(self, payload: dict[str, Any]) -> None:
        self.database.insert_backtest_playback_frame(payload)

    def store_playback_event(self, payload: dict[str, Any]) -> None:
        self.database.insert_backtest_playback_event(payload)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.database.get_backtest_runs(limit=limit)
        payload: list[dict[str, Any]] = []
        for row in rows:
            parsed = dict(row)
            parsed["enabled_strategies_json"] = self._parse_json(parsed.get("enabled_strategies_json"))
            parsed["session_filter_json"] = self._parse_json(parsed.get("session_filter_json"))
            parsed["spread_model"] = self._parse_json(parsed.get("spread_model"))
            parsed["slippage_model"] = self._parse_json(parsed.get("slippage_model"))
            parsed["artifacts_json"] = self._parse_json(parsed.get("artifacts_json")) or {}
            parsed["metadata_json"] = self._parse_json(parsed.get("metadata_json")) or {}
            payload.append(parsed)
        return payload

    def get_run_bundle(self, run_id: int) -> dict[str, Any]:
        run = self.database.get_backtest_run(run_id)
        if not run:
            raise ValueError(f"Backtest run {run_id} not found")
        run_payload = dict(run)
        run_payload["enabled_strategies_json"] = self._parse_json(run_payload.get("enabled_strategies_json")) or []
        run_payload["session_filter_json"] = self._parse_json(run_payload.get("session_filter_json")) or {}
        run_payload["spread_model"] = self._parse_json(run_payload.get("spread_model")) or {}
        run_payload["slippage_model"] = self._parse_json(run_payload.get("slippage_model")) or {}
        run_payload["artifacts_json"] = self._parse_json(run_payload.get("artifacts_json")) or {}
        run_payload["metadata_json"] = self._parse_json(run_payload.get("metadata_json")) or {}
        signals = self.database.get_backtest_signals(run_id)
        trades = self.database.get_backtest_trades(run_id)
        equity = self.database.get_backtest_equity_curve(run_id)
        frames = self.database.get_backtest_playback_frames(run_id)
        events = self.database.get_backtest_playback_events(run_id)
        for row in signals:
            row["raw_signal_json"] = self._parse_json(row.get("raw_signal_json")) or {}
        for row in trades:
            row["trade_metadata_json"] = self._parse_json(row.get("trade_metadata_json")) or {}
        for row in frames:
            row["open_positions_json"] = self._parse_json(row.get("open_positions_json")) or []
            row["pending_orders_json"] = self._parse_json(row.get("pending_orders_json")) or []
            row["selected_candidate_json"] = self._parse_json(row.get("selected_candidate_json")) or {}
            row["candidate_summary_json"] = self._parse_json(row.get("candidate_summary_json")) or {}
            row["state_json"] = self._parse_json(row.get("state_json")) or {}
        for row in events:
            row["event_payload_json"] = self._parse_json(row.get("event_payload_json")) or {}
        return {
            "run": run_payload,
            "signals": signals,
            "trades": trades,
            "equity_curve": equity,
            "playback_frames": frames,
            "playback_events": events,
            "summary": self.build_summary(signals, trades, equity, run=run_payload, frames=frames, events=events),
            "strategy_comparison": self.strategy_comparison(signals, trades),
            "analysis": self.signal_blocking_analysis(signals),
        }

    def export_run_bundle(self, run_id: int, base_dir: str | Path) -> dict[str, str]:
        bundle = self.get_run_bundle(run_id)
        exports_root = Path(base_dir) / "backtests"
        exports_root.mkdir(parents=True, exist_ok=True)
        run_dir = exports_root / self._build_run_folder_name(bundle["run"])
        run_dir.mkdir(parents=True, exist_ok=True)

        self._write_json(run_dir / "run.json", bundle["run"])
        self._write_json(run_dir / "summary.json", bundle["summary"])
        self._write_json(run_dir / "analysis.json", bundle["analysis"])
        self._write_csv(run_dir / "signals.csv", bundle["signals"])
        self._write_csv(run_dir / "trades.csv", bundle["trades"])
        self._write_csv(run_dir / "equity_curve.csv", bundle["equity_curve"])
        self._write_csv(run_dir / "strategy_comparison.csv", bundle["strategy_comparison"])
        self._write_jsonl(run_dir / "playback_frames.jsonl", bundle.get("playback_frames") or [])
        self._write_jsonl(run_dir / "playback_events.jsonl", bundle.get("playback_events") or [])
        self._write_json(run_dir / "bundle.json", bundle)
        (run_dir / "equity_curve.html").write_text(self._equity_chart_html(bundle), encoding="utf-8")

        manifest = {
            "run_id": run_id,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "files": sorted([p.name for p in run_dir.iterdir() if p.is_file()]),
        }
        self._write_json(run_dir / "manifest.json", manifest)
        exports = {
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "run": str(run_dir / "run.json"),
            "summary": str(run_dir / "summary.json"),
            "signals_csv": str(run_dir / "signals.csv"),
            "trades_csv": str(run_dir / "trades.csv"),
            "equity_curve_csv": str(run_dir / "equity_curve.csv"),
            "equity_curve_html": str(run_dir / "equity_curve.html"),
            "strategy_comparison_csv": str(run_dir / "strategy_comparison.csv"),
            "playback_frames_jsonl": str(run_dir / "playback_frames.jsonl"),
            "playback_events_jsonl": str(run_dir / "playback_events.jsonl"),
            "bundle_json": str(run_dir / "bundle.json"),
        }
        self.database.update_backtest_run(run_id, {"artifacts_json": exports})
        return exports

    def signal_blocking_analysis(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        by_strategy: dict[str, int] = {}
        blocked: dict[str, int] = {}
        blocked_sources: dict[str, int] = {}
        pending_created = 0
        pending_expired = 0
        executed = 0
        for row in signals:
            strategy = str(row.get("strategy_name") or "UNKNOWN")
            by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
            if bool(row.get("executed")):
                executed += 1
            if str(row.get("execution_reason") or "") == "pending_order_created":
                pending_created += 1
            if str(row.get("blocked_reason") or "") == "expired_not_filled":
                pending_expired += 1
            reason = str(row.get("blocked_reason") or "")
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
            raw = row.get("raw_signal_json") if isinstance(row.get("raw_signal_json"), dict) else {}
            diagnostics = raw.get("diagnostics") if isinstance(raw, dict) else {}
            blocker_source = str(diagnostics.get("blocker_source") or raw.get("blocker_source") or "")
            if blocker_source:
                blocked_sources[blocker_source] = blocked_sources.get(blocker_source, 0) + 1
        return {
            "signal_count_by_strategy": by_strategy,
            "executed_signals": executed,
            "blocked_signals": sum(1 for row in signals if bool(row.get("blocked_reason"))),
            "pending_orders_created": pending_created,
            "pending_orders_expired": pending_expired,
            "blocked_reason_distribution": blocked,
            "blocked_source_distribution": blocked_sources,
            "execution_reason_distribution": self._distribution(signals, "execution_reason"),
        }

    @staticmethod
    def _distribution(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key) or "")
            if not value:
                continue
            out[value] = out.get(value, 0) + 1
        return out

    def strategy_comparison(self, signals: list[dict[str, Any]], trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for signal in signals:
            key = str(signal.get("strategy_name") or "UNKNOWN")
            row = grouped.setdefault(
                key,
                {"strategy": key, "trades": 0, "wins": 0, "pnl": 0.0, "sum_r": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "blocked_count": 0},
            )
            if bool(signal.get("blocked_reason")):
                row["blocked_count"] += 1
        for trade in trades:
            key = str(trade.get("strategy_name") or "UNKNOWN")
            row = grouped.setdefault(
                key,
                {"strategy": key, "trades": 0, "wins": 0, "pnl": 0.0, "sum_r": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "blocked_count": 0},
            )
            pnl = float(trade.get("realized_pnl_total", trade.get("pnl", 0.0)) or 0.0)
            pnl_r = float(trade.get("pnl_r", 0.0) or 0.0)
            row["trades"] += 1
            row["wins"] += 1 if pnl > 0 else 0
            row["pnl"] += pnl
            row["sum_r"] += pnl_r
            if pnl >= 0:
                row["gross_profit"] += pnl
            else:
                row["gross_loss"] += abs(pnl)
        out: list[dict[str, Any]] = []
        for row in grouped.values():
            trades_count = max(1, int(row["trades"]))
            pf = (row["gross_profit"] / row["gross_loss"]) if row["gross_loss"] > 0 else (999.0 if row["gross_profit"] > 0 else 0.0)
            out.append(
                {
                    "strategy": row["strategy"],
                    "trades": int(row["trades"]),
                    "win_rate": (float(row["wins"]) / trades_count) * 100.0 if row["trades"] > 0 else 0.0,
                    "pnl": row["pnl"],
                    "avg_r": row["sum_r"] / trades_count if row["trades"] > 0 else 0.0,
                    "profit_factor": pf,
                    "drawdown_contribution": 0.0,
                    "blocked_count": int(row["blocked_count"]),
                }
            )
        out.sort(key=lambda item: item["pnl"], reverse=True)
        return out

    def build_summary(
        self,
        signals: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        equity: list[dict[str, Any]],
        *,
        run: dict[str, Any] | None = None,
        frames: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run_payload = dict(run or {})
        run_metadata = dict(run_payload.get("metadata_json") or {})
        playback_frames = list(frames or [])
        playback_events = list(events or [])
        total_signals = len(signals)
        total_trades = len(trades)
        blocked_signals = sum(1 for row in signals if bool(row.get("blocked_reason")))
        realized_values = [float(row.get("realized_pnl_total", row.get("pnl", 0.0)) or 0.0) for row in trades]
        partial_realized_pnl = sum(float(row.get("partial_realized_pnl", 0.0) or 0.0) for row in trades)
        final_realized_pnl = sum(float(row.get("final_realized_pnl", row.get("pnl", 0.0)) or 0.0) for row in trades)
        wins = sum(1 for value in realized_values if value > 0)
        gross_profit = sum(value for value in realized_values if value > 0)
        gross_loss = sum(abs(value) for value in realized_values if value < 0)
        net_profit = sum(realized_values)
        total_r = sum(float(row.get("pnl_r", 0.0) or 0.0) for row in trades)
        avg_r = total_r / max(total_trades, 1)
        expectancy = net_profit / max(total_trades, 1)
        durations = [float(row.get("duration_seconds", 0.0) or 0.0) for row in trades]
        avg_duration = sum(durations) / max(len(durations), 1)
        max_drawdown = 0.0
        peak = None
        for point in equity:
            value = float(point.get("equity", 0.0) or 0.0)
            if peak is None or value > peak:
                peak = value
            if peak and peak > 0:
                dd = (peak - value) / peak * 100.0
                max_drawdown = max(max_drawdown, dd)
        strategy_rows = self.strategy_comparison(signals, trades)
        best = strategy_rows[0]["strategy"] if strategy_rows else None
        worst = strategy_rows[-1]["strategy"] if strategy_rows else None
        initial_balance = float(run_payload.get("initial_balance", equity[0]["balance"] if equity else 10000.0) or 10000.0)
        final_balance = float(equity[-1]["balance"] if equity else initial_balance)
        frame_final_balance = float(playback_frames[-1]["balance"] if playback_frames else final_balance)
        frame_final_equity = float(playback_frames[-1]["equity"] if playback_frames else (equity[-1]["equity"] if equity else initial_balance))
        tolerance = 1e-6
        reconciliation_diff = max(
            abs(net_profit - sum(realized_values)),
            abs(final_balance - (initial_balance + net_profit)),
            abs(frame_final_balance - final_balance),
        ) if trades or equity or playback_frames else 0.0
        reconciliation_status = "ok" if reconciliation_diff <= tolerance else "failed"
        pending_orders_created = sum(1 for row in signals if str(row.get("execution_reason") or "") == "pending_order_created")
        pending_orders_expired = sum(1 for row in signals if str(row.get("blocked_reason") or "") == "expired_not_filled")
        pending_orders_filled = sum(
            1
            for row in trades
            if isinstance(row.get("trade_metadata_json"), dict)
            and (
                row["trade_metadata_json"].get("pending_order_id")
                or row["trade_metadata_json"].get("filled_from_pending_order")
            )
        )

        def _trade_management_count(reason_code: str) -> int:
            total = 0
            for row in trades:
                metadata = row.get("trade_metadata_json")
                if not isinstance(metadata, dict):
                    continue
                events = metadata.get("management_events")
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    if event.get("reason_code") == reason_code:
                        total += 1
                        break
            return total

        event_type_counts: dict[str, int] = {}
        for row in playback_events:
            key = str(row.get("event_type") or "")
            if not key:
                continue
            event_type_counts[key] = event_type_counts.get(key, 0) + 1

        return {
            "total_trades": total_trades,
            "total_signals": total_signals,
            "blocked_signals": blocked_signals,
            "initial_balance": initial_balance,
            "final_balance": final_balance,
            "pending_orders_created": pending_orders_created,
            "pending_orders_filled": pending_orders_filled,
            "pending_orders_expired": pending_orders_expired,
            "deferred_wait_retest": sum(
                1
                for row in signals
                if isinstance(row.get("raw_signal_json"), dict)
                and isinstance(row["raw_signal_json"].get("execution_decision"), dict)
                and str(row["raw_signal_json"]["execution_decision"].get("action") or "") == "WAIT_RETEST"
            ),
            "deferred_limit_entry": sum(
                1
                for row in signals
                if isinstance(row.get("raw_signal_json"), dict)
                and isinstance(row["raw_signal_json"].get("execution_decision"), dict)
                and str(row["raw_signal_json"]["execution_decision"].get("action") or "") == "PLACE_LIMIT"
            ),
            "blocked_position_exists": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "position_exists"),
            "blocked_pending_exists": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "pending_order_exists"),
            "blocked_same_family_cap": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "blocked_same_family_cap"),
            "blocked_directional_risk_cap": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "blocked_directional_risk_cap"),
            "blocked_portfolio_risk_cap": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "blocked_portfolio_risk_cap"),
            "blocked_no_momentum_candle": sum(1 for row in signals if str(row.get("blocked_reason") or "") == "no_momentum_candle"),
            "time_stop_exits": sum(1 for row in trades if str(row.get("exit_reason_code") or "") == "time_stop_exit"),
            "momentum_failure_exits": sum(1 for row in trades if str(row.get("exit_reason_code") or "") == "momentum_failure_exit"),
            "structure_break_exits": sum(1 for row in trades if str(row.get("exit_reason_code") or "") == "structure_break_exit"),
            "partial_tp_executed": _trade_management_count("partial_tp1"),
            "breakeven_moves": _trade_management_count("breakeven_after_tp1"),
            "trailing_updates": _trade_management_count("trailing_updated"),
            "win_rate": (wins / max(total_trades, 1)) * 100.0 if total_trades else 0.0,
            "net_profit": net_profit,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "total_realized_pnl": net_profit,
            "partial_realized_pnl": partial_realized_pnl,
            "final_realized_pnl": final_realized_pnl,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "max_drawdown": max_drawdown,
            "average_r": avg_r,
            "expectancy": expectancy,
            "average_trade_duration": avg_duration,
            "best_strategy": best,
            "worst_strategy": worst,
            "requested_start": run_metadata.get("requested_start") or run_payload.get("start_date"),
            "requested_end": run_metadata.get("requested_end") or run_payload.get("end_date"),
            "effective_data_start": run_metadata.get("effective_data_start"),
            "effective_data_end": run_metadata.get("effective_data_end"),
            "first_bar_time": run_metadata.get("first_bar_time"),
            "last_bar_time": run_metadata.get("last_bar_time"),
            "data_source": run_metadata.get("data_source") or run_metadata.get("history_resolution", {}).get("source_kind"),
            "data_truncated_reason": run_metadata.get("data_truncated_reason"),
            "frame_final_balance": frame_final_balance,
            "frame_final_equity": frame_final_equity,
            "trade_ledger_final_balance": initial_balance + sum(realized_values),
            "summary_final_balance": final_balance,
            "reconciliation_status": reconciliation_status,
            "reconciliation_diff": reconciliation_diff,
            "account_status": run_metadata.get("account_status", "ACTIVE"),
            "blown_at_time": run_metadata.get("blown_at_time"),
            "equity_at_stop": run_metadata.get("equity_at_stop"),
            "trades_after_stop_prevented": int(run_metadata.get("trades_after_stop_prevented", 0) or 0),
            "rejected_insufficient_margin_count": int(run_metadata.get("rejected_insufficient_margin_count", 0) or 0),
            "equity_curve_downsampled": False,
            "original_frame_count": len(equity),
            "exported_frame_count": len(equity),
            "first_export_time": equity[0]["ts"] if equity else None,
            "last_export_time": equity[-1]["ts"] if equity else None,
            "trade_closed_events": event_type_counts.get("trade_closed", 0),
            "partial_close_events": event_type_counts.get("trade_partial_close", 0),
        }
