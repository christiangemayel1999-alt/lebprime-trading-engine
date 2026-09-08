"""Comprehensive strategy decision logger."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class StrategyDecisionLogger:
    """Logs strategy context, candidates, and execution decisions."""

    def __init__(self, log_dir: str = "logs/strategy_decisions") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.current_date: Optional[str] = None
        self.candidates_log: list[dict[str, Any]] = []
        self.execution_log: list[dict[str, Any]] = []
        self.context_log: list[dict[str, Any]] = []
        self.family_stats = defaultdict(
            lambda: {
                "generated": 0,
                "valid": 0,
                "blocked": 0,
                "executed": 0,
                "block_reasons": defaultdict(int),
            }
        )

    def _check_date_rollover(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_date != today:
            if self.current_date is not None:
                self.flush_daily_logs()
            self.current_date = today

    def log_market_context(self, context: dict[str, Any], cycle_id: str) -> None:
        self._check_date_rollover()
        self.context_log.append(
            {
                "timestamp": datetime.now().isoformat(),
                "cycle_id": cycle_id,
                "session": context.get("session", {}),
                "bias_direction": context.get("bias", {}).get("direction"),
                "bias_scores": {
                    "long": context.get("bias", {}).get("long_score"),
                    "short": context.get("bias", {}).get("short_score"),
                    "trend": context.get("bias", {}).get("trend_score"),
                    "delta": context.get("bias", {}).get("bias_delta"),
                },
                "regime": context.get("regime", {}).get("regime_name"),
                "regime_confidence": context.get("regime", {}).get("regime_confidence"),
                "spread_points": context.get("spread_points"),
            }
        )

    def log_candidate_generation(self, candidates: list[dict[str, Any]], market_context: dict[str, Any], cycle_id: str) -> None:
        self._check_date_rollover()
        _ = market_context
        for candidate in candidates:
            family = candidate.get("setup_family", "UNKNOWN")
            self.family_stats[family]["generated"] += 1
            entry = {
                "timestamp": datetime.now().isoformat(),
                "cycle_id": cycle_id,
                "setup_family": family,
                "side": candidate.get("side"),
                "setup_score": candidate.get("setup_score"),
                "trend_score": candidate.get("trend_score"),
                "is_valid": candidate.get("setup_valid", False),
                "is_observation_only": candidate.get("observation_only", True),
                "block_reason": candidate.get("live_block_reason", "N/A"),
                "regime_compatible": candidate.get("regime_allowed", False),
                "session_compatible": candidate.get("session_allowed", False),
                "trend_aligned": candidate.get("trend_alignment", False),
                "value_price": candidate.get("value_price"),
                "atr_at_setup": candidate.get("atr_at_setup"),
            }
            if not candidate.get("setup_valid", False) or candidate.get("observation_only", False):
                self.family_stats[family]["blocked"] += 1
                reason = candidate.get("live_block_reason", "Unknown")
                self.family_stats[family]["block_reasons"][reason] += 1
            else:
                self.family_stats[family]["valid"] += 1
            self.candidates_log.append(entry)

    def log_execution_decision(
        self,
        candidate: Optional[dict[str, Any]],
        entry_evaluation: dict[str, Any],
        risk_gates: dict[str, Any],
        cycle_id: str,
        executed: bool,
    ) -> None:
        self._check_date_rollover()
        self.execution_log.append(
            {
                "timestamp": datetime.now().isoformat(),
                "cycle_id": cycle_id,
                "executed": executed,
                "setup_family": candidate.get("setup_family") if candidate else None,
                "entry_score": entry_evaluation.get("entry_score"),
                "entry_valid": entry_evaluation.get("valid", False),
                "entry_blocked_reasons": entry_evaluation.get("blocked_reasons", []),
                "trend_filter": entry_evaluation.get("trend_filter_pass"),
                "setup_filter": entry_evaluation.get("setup_filter_pass"),
                "trigger_filter": entry_evaluation.get("trigger_filter_pass"),
                "risk_gates_passed": risk_gates.get("all_passed", False),
                "risk_gate_details": risk_gates.get("gate_results", {}),
                "entry_threshold": entry_evaluation.get("entry_threshold"),
                "min_score_to_trade": entry_evaluation.get("min_score_to_trade"),
            }
        )
        if executed and candidate:
            family = candidate.get("setup_family", "UNKNOWN")
            self.family_stats[family]["executed"] += 1

    def flush_daily_logs(self) -> None:
        if not self.current_date:
            return
        date_dir = self.log_dir / self.current_date
        date_dir.mkdir(parents=True, exist_ok=True)

        if self.candidates_log:
            candidates_df = pd.DataFrame(self.candidates_log)
            path = date_dir / f"candidates_{self.current_date}.parquet"
            candidates_df.to_parquet(path)
            logger.info("Saved %s candidate records to %s", len(candidates_df), path)

        if self.execution_log:
            execution_df = pd.DataFrame(self.execution_log)
            path = date_dir / f"execution_{self.current_date}.parquet"
            execution_df.to_parquet(path)
            logger.info("Saved %s execution records to %s", len(execution_df), path)

        if self.context_log:
            context_df = pd.DataFrame(self.context_log)
            path = date_dir / f"context_{self.current_date}.parquet"
            context_df.to_parquet(path)
            logger.info("Saved %s context records to %s", len(context_df), path)

        summary = {
            "date": self.current_date,
            "total_candidates_generated": sum(s["generated"] for s in self.family_stats.values()),
            "total_candidates_valid": sum(s["valid"] for s in self.family_stats.values()),
            "total_candidates_blocked": sum(s["blocked"] for s in self.family_stats.values()),
            "total_executed": sum(s["executed"] for s in self.family_stats.values()),
            "family_stats": dict(self.family_stats),
        }
        with (date_dir / f"summary_{self.current_date}.json").open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)

        self.candidates_log = []
        self.execution_log = []
        self.context_log = []
        self.family_stats.clear()

    def get_real_time_stats(self) -> dict[str, Any]:
        return {
            "current_date": self.current_date,
            "buffer_sizes": {
                "candidates": len(self.candidates_log),
                "executions": len(self.execution_log),
                "contexts": len(self.context_log),
            },
            "family_stats": dict(self.family_stats),
        }
