"""Monitors correlation between active setup families to detect concentration risk."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class StrategyCorrelationMonitor:
    def __init__(
        self,
        correlation_threshold: float = 0.7,
        concentration_threshold: float = 0.8,
        min_samples: int = 20,
        lookback_days: int = 30,
    ) -> None:
        self.correlation_threshold = correlation_threshold
        self.concentration_threshold = concentration_threshold
        self.min_samples = min_samples
        self.lookback_days = lookback_days
        self.family_returns: dict[str, list[float]] = defaultdict(list)
        self.family_trades: dict[str, int] = defaultdict(int)
        self.last_alerts: dict[str, datetime] = {}

    def add_trade_result(self, setup_family: str, r_multiple: float, timestamp: datetime) -> None:
        _ = timestamp
        self.family_returns[setup_family].append(r_multiple)
        self.family_trades[setup_family] += 1
        self._check_correlation()
        self._check_concentration()

    def _calculate_rolling_correlation(self) -> Optional[pd.DataFrame]:
        valid_families = {k: v for k, v in self.family_returns.items() if len(v) >= self.min_samples}
        if len(valid_families) < 2:
            return None
        max_len = max(len(v) for v in valid_families.values())
        aligned = {}
        for family, returns in valid_families.items():
            padding = max_len - len(returns)
            aligned[family] = [np.nan] * padding + returns
        return pd.DataFrame(aligned).corr()

    def _check_correlation(self) -> None:
        corr_matrix = self._calculate_rolling_correlation()
        if corr_matrix is None:
            return
        families = corr_matrix.columns
        for i in range(len(families)):
            for j in range(i + 1, len(families)):
                corr_value = corr_matrix.iloc[i, j]
                if abs(corr_value) <= self.correlation_threshold:
                    continue
                key = f"corr_{families[i]}_{families[j]}"
                last = self.last_alerts.get(key)
                if last and (datetime.now() - last) < timedelta(hours=24):
                    continue
                logger.warning(
                    "HIGH CORRELATION ALERT: %s and %s correlation = %.3f. Consider disabling one to reduce concentration risk.",
                    families[i],
                    families[j],
                    corr_value,
                )
                self.last_alerts[key] = datetime.now()

    def _check_concentration(self) -> None:
        total = sum(self.family_trades.values())
        if total < self.min_samples:
            return
        for family, trades in self.family_trades.items():
            concentration = trades / total
            if concentration <= self.concentration_threshold:
                continue
            key = f"conc_{family}"
            last = self.last_alerts.get(key)
            if last and (datetime.now() - last) < timedelta(hours=24):
                continue
            logger.warning(
                "CONCENTRATION ALERT: %s represents %.1f%% of all trades (%s/%s). Consider diversifying strategy mix.",
                family,
                concentration * 100.0,
                trades,
                total,
            )
            self.last_alerts[key] = datetime.now()

    def get_diversification_score(self) -> float:
        total = sum(self.family_trades.values())
        if total == 0:
            return 0.0
        hhi = sum((trades / total) ** 2 for trades in self.family_trades.values())
        n = max(1, len(self.family_trades))
        normalized_hhi = (hhi - 1 / n) / (1 - 1 / n) if n > 1 else 1
        return round((1 - normalized_hhi) * 100.0, 1)

    def get_recommendations(self) -> list[str]:
        recommendations: list[str] = []
        corr_matrix = self._calculate_rolling_correlation()
        if corr_matrix is not None:
            high_pairs: list[tuple[str, str]] = []
            families = corr_matrix.columns
            for i in range(len(families)):
                for j in range(i + 1, len(families)):
                    if abs(corr_matrix.iloc[i, j]) > self.correlation_threshold:
                        high_pairs.append((families[i], families[j]))
            if high_pairs:
                recommendations.append(
                    "High correlation detected between: "
                    + ", ".join([f"{a}+{b}" for a, b in high_pairs])
                    + ". Consider enabling an uncorrelated family like TREND_PULLBACK_RECLAIM."
                )

        total = sum(self.family_trades.values())
        if total > 0:
            for family, trades in self.family_trades.items():
                if trades / total > self.concentration_threshold:
                    recommendations.append(
                        f"Over-reliance on {family} ({trades}/{total} trades). Consider adjusting setup_controls to balance execution."
                    )

        if total < 30:
            recommendations.append(
                f"Insufficient sample size ({total} trades). Need at least 30 trades per family for reliable analysis."
            )
        return recommendations
