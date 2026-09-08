"""Trading filters and guardrails for session, spread, volatility, and risk limits."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils import UTC, combine_utc, to_utc


class TradeFilters:
    """Encapsulate all hard filters required before opening trades."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def spread_ok(self, spread_points: float) -> tuple[bool, str]:
        """Validate current spread."""
        max_spread = float(self.config["filters"]["max_spread_points"])
        if spread_points <= max_spread:
            return True, "Spread OK"
        return False, f"Spread too high: {spread_points:.2f} > {max_spread:.2f}"

    def volatility_ok(self, atr_3m: float) -> tuple[bool, str]:
        """Validate 3-minute ATR range."""
        min_atr = float(self.config["filters"]["min_atr_3min"])
        max_atr = float(self.config["filters"]["max_atr_3min"])
        if min_atr <= atr_3m <= max_atr:
            return True, "ATR regime OK"
        return False, f"3m ATR outside range: {atr_3m:.2f} not in [{min_atr:.2f}, {max_atr:.2f}]"

    def in_session(self, now_utc: datetime) -> tuple[bool, str]:
        """Check whether the current session bucket is live-allowed."""
        sessions_cfg = self.config.get("sessions", {})
        buckets = sessions_cfg.get("buckets", [])
        if buckets:
            today = now_utc.astimezone(UTC).date()
            for bucket in buckets:
                start = combine_utc(today, bucket["start"])
                end = combine_utc(today, bucket["end"])
                if start <= end:
                    in_bucket = start <= now_utc <= end
                else:
                    in_bucket = now_utc >= start or now_utc <= end
                if in_bucket:
                    if bool(bucket.get("live_allowed", False)):
                        return True, f"Session OK: {bucket.get('name', 'UNKNOWN')}"
                    return False, f"Session disabled: {bucket.get('name', 'UNKNOWN')}"
            return False, "No session bucket matched"

        today = now_utc.astimezone(UTC).date()
        london_start = combine_utc(today, self.config["sessions"]["london_start"])
        london_end = combine_utc(today, self.config["sessions"]["london_end"])
        newyork_start = combine_utc(today, self.config["sessions"]["newyork_start"])
        newyork_end = combine_utc(today, self.config["sessions"]["newyork_end"])

        in_london = london_start <= now_utc <= london_end
        in_newyork = newyork_start <= now_utc <= newyork_end
        if in_london or in_newyork:
            return True, "Session OK"
        return False, "Outside configured trading sessions"

    def is_blackout(self, now_utc: datetime) -> tuple[bool, str]:
        """Check for configured blackout windows."""
        today = now_utc.astimezone(UTC).date()
        for window in self.config.get("blackout_times", []):
            start = combine_utc(today, window["start"])
            end = combine_utc(today, window["end"])
            if start <= now_utc <= end:
                return True, window.get("reason", "Blackout")
        return False, ""

    def should_close_for_session_end(self, now_utc: datetime) -> bool:
        """Check if the session close threshold has been reached."""
        close_time = combine_utc(now_utc.astimezone(UTC).date(), self.config["sessions"]["session_close"])
        return now_utc >= close_time

    def position_filter(self, has_position: bool, has_pending: bool) -> tuple[bool, str]:
        """Enforce one-position-at-a-time policy."""
        if has_position:
            return False, "Open position already exists"
        if has_pending:
            return False, "Pending order already exists"
        return True, "Position filter OK"

    def cooldown_ok(self, state: dict[str, Any], now_utc: datetime) -> tuple[bool, str]:
        """Block entries during trade cooldown windows."""
        cooldown_until = to_utc(state.get("cooldown_until"))
        if cooldown_until and now_utc < cooldown_until:
            return False, f"Cooldown active until {cooldown_until.isoformat()}"
        return True, "Cooldown OK"

    def signal_freshness_ok(
        self,
        state: dict[str, Any],
        fingerprint: str,
        signal_time: datetime,
    ) -> tuple[bool, str]:
        """Prevent duplicate signals within the configured cooldown window."""
        last_signal = state.get("last_signal", {})
        last_fingerprint = last_signal.get("fingerprint")
        last_timestamp = to_utc(last_signal.get("timestamp"))
        cooldown_minutes = int(self.config["filters"]["signal_cooldown_minutes"])

        if (
            last_fingerprint == fingerprint
            and last_timestamp is not None
            and (signal_time - last_timestamp).total_seconds() < cooldown_minutes * 60
        ):
            return False, "Duplicate signal blocked by cooldown"
        return True, "Signal is fresh"

    def limits_ok(
        self,
        state: dict[str, Any],
        balance: float,
        now_utc: datetime,
    ) -> tuple[bool, str]:
        """Validate daily, weekly, account, and consecutive-loss limits."""
        risk = self.config["risk"]

        if balance < float(risk["min_balance"]):
            return False, "Account balance below minimum threshold"

        pause_until = to_utc(state.get("pause_until"))
        if pause_until and now_utc < pause_until:
            return False, f"Loss lock active until {pause_until.isoformat()}"

        if int(state.get("daily_trade_count", 0)) >= int(risk["max_trades_per_day"]):
            return False, "Max daily trades reached"

        start_of_day_balance = float(state.get("start_of_day_balance") or balance)
        if start_of_day_balance > 0:
            daily_drawdown_pct = abs(min(float(state.get("daily_pnl", 0.0)), 0.0)) / start_of_day_balance * 100
            if daily_drawdown_pct >= float(risk["max_daily_loss_percent"]):
                return False, "Daily loss lock reached"

        start_of_week_balance = float(state.get("start_of_week_balance") or balance)
        if start_of_week_balance > 0:
            weekly_drawdown_pct = abs(min(float(state.get("weekly_pnl", 0.0)), 0.0)) / start_of_week_balance * 100
            if weekly_drawdown_pct >= float(risk["max_weekly_loss_percent"]):
                return False, "Weekly loss limit reached"

        if int(state.get("consecutive_losses", 0)) >= int(risk["max_consecutive_losses"]):
            return False, "Consecutive-loss protection active"

        return True, "Risk limits OK"

    def evaluate_trade_window(
        self,
        state: dict[str, Any],
        balance: float,
        now_utc: datetime,
        spread_points: float,
        atr_3m: float,
        has_position: bool,
        has_pending: bool,
    ) -> dict[str, Any]:
        """Run the full hard-filter stack and return a structured result."""
        blackout, blackout_reason = self.is_blackout(now_utc)
        checks = [
            self.limits_ok(state, balance, now_utc),
            self.cooldown_ok(state, now_utc),
            self.in_session(now_utc),
            self.spread_ok(spread_points),
            self.volatility_ok(atr_3m),
            self.position_filter(has_position, has_pending),
        ]

        if blackout:
            return {
                "can_trade": False,
                "reason": f"Blackout window: {blackout_reason}",
                "reasons": [f"Blackout window: {blackout_reason}"],
            }

        failed_reasons = [reason for passed, reason in checks if not passed]
        if failed_reasons:
            return {
                "can_trade": False,
                "reason": failed_reasons[0],
                "reasons": failed_reasons,
            }

        return {
            "can_trade": True,
            "reason": "Trade window open",
            "reasons": ["All hard filters passed"],
        }
