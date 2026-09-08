"""Risk metrics calculator with proper R-multiple computation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TradeRiskMetadata:
    """Standardized risk metadata for R-multiple calculation."""

    entry_price: float
    stop_loss_price: float
    exit_price: float
    position_size: float
    direction: str
    setup_family: str
    risk_percent: float
    account_balance: float


class RMultipleCalculator:
    """Calculates and validates R-multiples for strategy and manual trades."""

    def __init__(self) -> None:
        self.min_acceptable_r = -10.0
        self.max_acceptable_r = 10.0
        self.anomaly_threshold = 100.0

    def calculate_initial_risk(self, metadata: TradeRiskMetadata) -> float:
        if metadata.direction == "LONG":
            return abs(metadata.entry_price - metadata.stop_loss_price)
        if metadata.direction == "SHORT":
            return abs(metadata.stop_loss_price - metadata.entry_price)
        raise ValueError(f"Invalid direction: {metadata.direction}")

    def calculate_r_multiple(self, metadata: TradeRiskMetadata) -> Optional[float]:
        initial_risk = self.calculate_initial_risk(metadata)
        if initial_risk <= 0:
            logger.error(
                "Invalid initial risk (%s) for trade. Entry: %s, Stop: %s",
                initial_risk,
                metadata.entry_price,
                metadata.stop_loss_price,
            )
            return None

        if metadata.direction == "LONG":
            profit_loss = metadata.exit_price - metadata.entry_price
        else:
            profit_loss = metadata.entry_price - metadata.exit_price

        r_multiple = profit_loss / initial_risk
        if abs(r_multiple) > self.anomaly_threshold:
            logger.error("Anomalous R-multiple detected: %.2fR. Trade metadata: %s", r_multiple, metadata)
            return self._calculate_fallback_r(metadata)

        return round(r_multiple, 2)

    def _calculate_fallback_r(self, metadata: TradeRiskMetadata) -> Optional[float]:
        if metadata.entry_price == 0:
            return None
        if metadata.direction == "LONG":
            pnl_pct = (metadata.exit_price - metadata.entry_price) / metadata.entry_price
        else:
            pnl_pct = (metadata.entry_price - metadata.exit_price) / metadata.entry_price

        risk_pct = metadata.risk_percent / 100.0
        if risk_pct <= 0:
            return None
        r_multiple = pnl_pct / risk_pct
        if abs(r_multiple) <= self.max_acceptable_r:
            logger.info("Fallback R calculation succeeded: %.2fR", r_multiple)
            return round(r_multiple, 2)

        logger.error("Fallback R calculation also anomalous: %.2fR", r_multiple)
        return None

    def validate_trade_metadata(self, metadata: TradeRiskMetadata) -> dict[str, Any]:
        warnings: list[str] = []
        is_valid = True

        if metadata.stop_loss_price is None or metadata.stop_loss_price == 0:
            warnings.append("Missing or zero stop loss price")
            is_valid = False
        if metadata.entry_price is None or metadata.entry_price == 0:
            warnings.append("Missing or zero entry price")
            is_valid = False
        if metadata.exit_price is None or metadata.exit_price == 0:
            warnings.append("Missing or zero exit price")
            is_valid = False

        if metadata.direction == "LONG" and metadata.stop_loss_price >= metadata.entry_price:
            warnings.append("Stop loss above entry for LONG trade")
            is_valid = False
        if metadata.direction == "SHORT" and metadata.stop_loss_price <= metadata.entry_price:
            warnings.append("Stop loss below entry for SHORT trade")
            is_valid = False

        return {"is_valid": is_valid, "warnings": warnings}

    def process_trade_batch(self, trades: pd.DataFrame) -> pd.DataFrame:
        results: list[dict[str, Any]] = []

        for idx, row in trades.iterrows():
            try:
                metadata = TradeRiskMetadata(
                    entry_price=row["entry_price"],
                    stop_loss_price=row.get("stop_loss_price", 0),
                    exit_price=row["exit_price"],
                    position_size=row.get("position_size", 0),
                    direction=row.get("direction", "LONG"),
                    setup_family=row.get("setup_family", "MANUAL"),
                    risk_percent=row.get("risk_percent", 1.0),
                    account_balance=row.get("account_balance", 0),
                )
                validation = self.validate_trade_metadata(metadata)
                r_multiple = self.calculate_r_multiple(metadata) if validation["is_valid"] else None
                results.append(
                    {
                        "original_index": idx,
                        "r_multiple_corrected": r_multiple,
                        "r_is_valid": validation["is_valid"],
                        "r_warnings": "; ".join(validation["warnings"]),
                        "setup_family": metadata.setup_family,
                    }
                )
            except Exception as exc:  # defensive
                logger.error("Error processing trade %s: %s", idx, str(exc))
                results.append(
                    {
                        "original_index": idx,
                        "r_multiple_corrected": None,
                        "r_is_valid": False,
                        "r_warnings": f"Processing error: {str(exc)}",
                        "setup_family": "ERROR",
                    }
                )

        results_df = pd.DataFrame(results)
        return trades.join(results_df.set_index("original_index"))
