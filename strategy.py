"""Legacy concrete strategy implementation.

Canonical strategy construction lives in `strategy_factory.build_strategy_engine`
and `trading_bot.strategy.*`. This module is kept as an adapter-backed
implementation detail for the legacy non-LEBPRIM setup families; new strategy
wiring should be registered through `trading_bot.strategy.registry`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pytz

from indicators import (
    add_setup_indicators,
    add_trend_indicators,
    add_trigger_indicators,
    candle_body,
    candle_range,
    close_position_in_range,
    indicators_are_valid,
    lower_wick,
    upper_wick,
)
from utils import normalize_price, to_utc


class StrategyEngine:
    """Classify regime, build explicit setup families, and validate entries."""

    engine_type = "legacy_strategy_engine"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._logger = logging.getLogger("mtf_sniper_bot")

    def _neutral_bias(self) -> dict[str, Any]:
        return {
            "direction": "NONE",
            "trend_score": 0.0,
            "long_score": 0.0,
            "short_score": 0.0,
            "confidence": 0.0,
            "reasons": ["slice_unavailable"],
        }

    def _no_trade_regime(self, block_reason: str) -> dict[str, Any]:
        return {
            "regime_name": "NO_TRADE",
            "regime_confidence": 0.0,
            "bias": "NONE",
            "live_allowed": False,
            "regime_block_reason": block_reason,
            "metrics": {},
        }

    def _log_slice_diagnostics(
        self,
        category: str,
        trend_df: pd.DataFrame | None,
        setup_df: pd.DataFrame | None,
        trigger_df: pd.DataFrame | None,
        *,
        market_context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        level: int = logging.INFO,
    ) -> None:
        payload: dict[str, Any] = {
            "category": category,
            "symbol": str(self.config.get("mt5", {}).get("symbol") or "UNKNOWN"),
            "mode": str(self.config.get("bot", {}).get("trading_mode") or "UNKNOWN"),
            "trend_len": int(len(trend_df)) if isinstance(trend_df, pd.DataFrame) else 0,
            "setup_len": int(len(setup_df)) if isinstance(setup_df, pd.DataFrame) else 0,
            "trigger_len": int(len(trigger_df)) if isinstance(trigger_df, pd.DataFrame) else 0,
            "bias": str((market_context or {}).get("bias", {}).get("direction") or "NONE"),
            "regime": str((market_context or {}).get("regime", {}).get("regime_name") or "UNKNOWN"),
            "session": str((market_context or {}).get("session", {}).get("session_name") or "UNKNOWN"),
            "timestamp": str((market_context or {}).get("timestamp") or ""),
        }
        if extra:
            payload.update(extra)
        self._logger.log(level, "strategy_slice %s", payload)

    def _log_empty_slice(
        self,
        category: str,
        trend_df: pd.DataFrame | None,
        setup_df: pd.DataFrame | None,
        trigger_df: pd.DataFrame | None,
        *,
        market_context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._log_slice_diagnostics(
            "strategy_slice_empty",
            trend_df,
            setup_df,
            trigger_df,
            market_context=market_context,
            extra={"stage": category, **(extra or {})},
            level=logging.INFO,
        )

    def _setup_control(self, family: str) -> dict[str, Any]:
        """Return the dashboard-controlled setup config for a family."""
        controls = self.config.get("strategy", {}).get("setup_controls", {})
        if not isinstance(controls, dict):
            return {}
        normalized_keys = {
            family,
            family.lower(),
            family.upper(),
            family.lower().replace("-", "_"),
            family.lower().replace(" ", "_"),
        }
        for key in normalized_keys:
            if key in controls and isinstance(controls[key], dict):
                return controls[key]
        return {}

    @staticmethod
    def _float_override(payload: dict[str, Any], key: str, fallback: float) -> float:
        """Return a float override when present and valid."""
        value = payload.get(key)
        if value in (None, ""):
            return float(fallback)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(fallback)

    @staticmethod
    def _bool_override(payload: dict[str, Any], key: str, fallback: bool) -> bool:
        """Return a bool override when present."""
        value = payload.get(key)
        if value is None:
            return bool(fallback)
        return bool(value)

    @staticmethod
    def _session_quality_score(session_name: str, allow_asia_session: bool) -> tuple[float, str]:
        """Return a session quality score for scalping context."""
        session_name = str(session_name or "UNKNOWN").upper()
        profile = {
            "LONDON_OPEN": (96.0, "primary"),
            "LONDON": (90.0, "primary"),
            "NY_OPEN": (96.0, "primary"),
            "LONDON_NY_OVERLAP": (100.0, "primary"),
            "LATE_NY": (68.0, "secondary"),
            "ASIA": (72.0 if allow_asia_session else 45.0, "secondary" if allow_asia_session else "restricted"),
            "UNCLASSIFIED": (40.0, "restricted"),
        }
        score, label = profile.get(session_name, (60.0, "secondary"))
        return float(score), label

    def _effective_setup_thresholds(self, strategy_control: dict[str, Any], live_profile: bool) -> dict[str, float]:
        """Merge global thresholds with any per-setup overrides."""
        thresholds = dict(self.setup_thresholds(live_profile))
        thresholds["trend"] = self._float_override(strategy_control, "min_trend_score", thresholds["trend"])
        thresholds["setup"] = self._float_override(strategy_control, "min_setup_score", thresholds["setup"])
        thresholds["trigger"] = self._float_override(strategy_control, "min_trigger_score", thresholds["trigger"])
        thresholds["entry"] = self._float_override(strategy_control, "min_entry_score", thresholds["entry"])
        return thresholds

    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Append higher-timeframe trend indicators."""
        indicators_cfg = self.config["indicators"]
        frame = add_trend_indicators(
            df,
            ema_fast_period=int(indicators_cfg["ema_slow"]),
            ema_slow_period=int(indicators_cfg["ema_trend"]),
            atr_period=int(indicators_cfg["atr_period"]),
        )
        return self._augment_common_columns(frame)

    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Append 3m setup indicators and local structure helpers."""
        indicators_cfg = self.config["indicators"]
        frame = add_setup_indicators(
            df,
            ema_fast=int(indicators_cfg["ema_fast"]),
            ema_slow=int(indicators_cfg["ema_slow"]),
            rsi_period=int(indicators_cfg["rsi_period"]),
            atr_period=int(indicators_cfg["atr_period"]),
            adx_period=int(indicators_cfg["adx_period"]),
            volume_period=int(indicators_cfg["volume_period"]),
        )
        frame = self._augment_common_columns(frame)
        frame["ema_20_slope_2"] = frame["ema_20"] - frame["ema_20"].shift(2)
        frame["ema_50_slope_3"] = frame["ema_50"] - frame["ema_50"].shift(3)
        frame["atr_median_20"] = frame["atr"].rolling(window=20, min_periods=5).median()
        frame["rolling_high_10"] = frame["high"].rolling(window=10, min_periods=3).max()
        frame["rolling_low_10"] = frame["low"].rolling(window=10, min_periods=3).min()
        return frame

    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Append 1m trigger indicators and microstructure helpers."""
        frame = add_trigger_indicators(
            df,
            volume_period=int(self.config["indicators"]["volume_period"]),
        )
        frame = self._augment_common_columns(frame)
        frame["ema_20"] = frame["close"].ewm(span=20, adjust=False).mean()
        frame["ema_9"] = frame["close"].ewm(span=9, adjust=False).mean()
        frame["rolling_high_8"] = frame["high"].rolling(window=8, min_periods=3).max()
        frame["rolling_low_8"] = frame["low"].rolling(window=8, min_periods=3).min()
        return frame

    def _augment_common_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Append derived candle fields shared by all timeframes."""
        frame = df.copy()
        frame["body"] = frame.apply(candle_body, axis=1)
        frame["range"] = frame.apply(candle_range, axis=1)
        frame["close_location"] = frame.apply(close_position_in_range, axis=1)
        frame["lower_wick"] = frame.apply(lower_wick, axis=1)
        frame["upper_wick"] = frame.apply(upper_wick, axis=1)
        frame["body_ratio"] = frame["body"] / frame["range"].replace(0, 1e-9)
        return frame

    def analyze_market_context(
        self,
        now_utc: datetime,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]:
        """Return session, bias, regime, and quality context for the current cycle."""
        if now_utc is None:
            now_utc = datetime.now(tz=pytz.UTC)
        session = self.classify_session(now_utc)
        self._log_slice_diagnostics(
            "strategy_slice_diagnostics",
            trend_df,
            setup_df,
            trigger_df,
            extra={"stage": "analyze_market_context", "timestamp": now_utc.isoformat()},
        )
        if trend_df is None or trend_df.empty or setup_df is None or setup_df.empty or trigger_df is None or trigger_df.empty:
            self._log_empty_slice(
                "analyze_market_context",
                trend_df,
                setup_df,
                trigger_df,
                extra={"timestamp": now_utc.isoformat()},
            )
            return {
                "timestamp": now_utc.isoformat(),
                "session": session,
                "bias": self._neutral_bias(),
                "regime": self._no_trade_regime("slice_unavailable"),
                "spread_points": float(spread_points),
            }
        bias = self._analyze_bias(trend_df, setup_df)
        regime = self.classify_market_regime(trend_df, setup_df, trigger_df, spread_points, session, bias)
        return {
            "timestamp": now_utc.isoformat(),
            "session": session,
            "bias": bias,
            "regime": regime,
            "spread_points": float(spread_points),
        }

    def classify_session(self, now_utc: datetime) -> dict[str, Any]:
        """Classify the current time into a configured session bucket."""
        sessions_cfg = self.config["sessions"]
        timezone_name = str(sessions_cfg.get("timezone", "UTC"))
        timezone = pytz.timezone(timezone_name)
        local_now = now_utc.astimezone(timezone)
        local_minutes = local_now.hour * 60 + local_now.minute
        selected_bucket: dict[str, Any] | None = None

        for bucket in sessions_cfg.get("buckets", []):
            start_minutes = self._hhmm_to_minutes(str(bucket["start"]))
            end_minutes = self._hhmm_to_minutes(str(bucket["end"]))
            if start_minutes <= end_minutes:
                in_bucket = start_minutes <= local_minutes <= end_minutes
            else:
                in_bucket = local_minutes >= start_minutes or local_minutes <= end_minutes
            if in_bucket:
                selected_bucket = bucket
                break

        if selected_bucket is None:
            selected_bucket = {"name": "UNCLASSIFIED", "live_allowed": False, "start": "", "end": ""}
        name = str(selected_bucket["name"])
        allow_asia_session = bool(sessions_cfg.get("allow_asia_session", False))
        live_allowed_buckets = set(sessions_cfg.get("live_allowed_buckets", []))
        configured_live_allowed = bool(selected_bucket.get("live_allowed", False)) and (name in live_allowed_buckets if live_allowed_buckets else True)
        live_allowed = configured_live_allowed
        if name == "ASIA":
            live_allowed = configured_live_allowed or allow_asia_session
        session_quality_score, session_quality_label = self._session_quality_score(name, allow_asia_session)
        if live_allowed:
            block_reason = None
        elif name == "ASIA" and not allow_asia_session:
            block_reason = "asia_session_disabled"
        else:
            block_reason = "session_bucket_live_disabled"
        return {
            "session_name": name,
            "session_live_allowed": live_allowed,
            "session_block_reason": block_reason,
            "timezone": timezone_name,
            "local_time": local_now.isoformat(),
            "bucket": dict(selected_bucket),
            "session_quality_score": round(session_quality_score, 2),
            "session_quality_label": session_quality_label,
        }

    def _analyze_bias(self, trend_df: pd.DataFrame, setup_df: pd.DataFrame) -> dict[str, Any]:
        """Score directional bias from 15m structure and 3m alignment."""
        required_trend = ["ema_50", "ema_200", "atr", "ema_50_slope_3", "ema_50_slope_6"]
        required_setup = ["ema_20", "ema_50", "atr"]
        if not indicators_are_valid(trend_df, required_trend) or not indicators_are_valid(setup_df, required_setup):
            return {
                "direction": "NONE",
                "trend_score": 0.0,
                "long_score": 0.0,
                "short_score": 0.0,
                "confidence": 0.0,
                "reasons": ["indicator_data_unavailable"],
            }

        trend_latest = trend_df.iloc[-1]
        setup_latest = setup_df.iloc[-1]
        trend_recent = trend_df.tail(4)
        atr_value = max(float(trend_latest["atr"]), 1e-9)
        slope_3_atr = float(trend_latest["ema_50_slope_3"]) / atr_value
        slope_6_atr = float(trend_latest["ema_50_slope_6"]) / atr_value
        close_price = float(trend_latest["close"])
        ema_50 = float(trend_latest["ema_50"])
        ema_200 = float(trend_latest["ema_200"])
        setup_ema_20 = float(setup_latest["ema_20"])
        setup_ema_50 = float(setup_latest["ema_50"])

        long_score = 0.0
        short_score = 0.0
        long_reasons: list[str] = []
        short_reasons: list[str] = []

        def add(score: float, reasons: list[str], condition: bool, points: float, text: str) -> float:
            if condition:
                reasons.append(text)
                return score + points
            return score

        long_score = add(long_score, long_reasons, close_price > ema_50, 14, "15m close above EMA50")
        long_score = add(long_score, long_reasons, close_price > ema_200, 14, "15m close above EMA200")
        long_score = add(long_score, long_reasons, ema_50 > ema_200, 18, "EMA50 above EMA200")
        long_score = add(long_score, long_reasons, slope_3_atr > 0.06, 10, "EMA50 slope rising")
        long_score = add(long_score, long_reasons, slope_6_atr > 0.10, 10, "trend acceleration positive")
        long_score = add(long_score, long_reasons, int((trend_recent["close"] > trend_recent["ema_50"]).sum()) >= 3, 8, "recent closes holding above EMA50")
        long_score = add(long_score, long_reasons, setup_ema_20 > setup_ema_50, 12, "3m EMA20 above EMA50")
        long_score = add(long_score, long_reasons, float(setup_latest["close"]) > setup_ema_20, 8, "3m close above EMA20")

        short_score = add(short_score, short_reasons, close_price < ema_50, 14, "15m close below EMA50")
        short_score = add(short_score, short_reasons, close_price < ema_200, 14, "15m close below EMA200")
        short_score = add(short_score, short_reasons, ema_50 < ema_200, 18, "EMA50 below EMA200")
        short_score = add(short_score, short_reasons, slope_3_atr < -0.06, 10, "EMA50 slope falling")
        short_score = add(short_score, short_reasons, slope_6_atr < -0.10, 10, "trend acceleration negative")
        short_score = add(short_score, short_reasons, int((trend_recent["close"] < trend_recent["ema_50"]).sum()) >= 3, 8, "recent closes holding below EMA50")
        short_score = add(short_score, short_reasons, setup_ema_20 < setup_ema_50, 12, "3m EMA20 below EMA50")
        short_score = add(short_score, short_reasons, float(setup_latest["close"]) < setup_ema_20, 8, "3m close below EMA20")

        direction = "LONG" if long_score > short_score else "SHORT"
        score = long_score if direction == "LONG" else short_score
        opposite = short_score if direction == "LONG" else long_score
        confidence = max(0.0, min(100.0, score - opposite + 50.0))
        bias_min_score = float(self.config["strategy"].get("bias_min_score", 65.0) or 65.0)
        bias_min_delta = float(self.config["strategy"].get("bias_min_delta", 18.0) or 18.0)
        if score < bias_min_score or (score - opposite) < bias_min_delta:
            direction = "NONE"
            score = max(long_score, short_score)

        return {
            "direction": direction,
            "trend_score": round(score, 2),
            "long_score": round(long_score, 2),
            "short_score": round(short_score, 2),
            "confidence": round(confidence, 2),
            "reasons": long_reasons if direction == "LONG" else short_reasons,
        }

    def classify_market_regime(
        self,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
        session: dict[str, Any],
        bias: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify the market into the production trading regime buckets."""
        required_setup = ["ema_20", "ema_50", "atr", "atr_median_20"]
        if not indicators_are_valid(setup_df, required_setup):
            return {
                "regime_name": "DEAD_SESSION / LOW_LIQUIDITY",
                "regime_confidence": 0.0,
                "bias": bias.get("direction", "NONE"),
                "live_allowed": False,
                "regime_block_reason": "setup_indicator_data_unavailable",
                "metrics": {},
            }

        regime_cfg = self.config["regime"]
        execution_cfg = self.config["execution"]
        setup_latest = setup_df.iloc[-1]
        setup_recent = setup_df.tail(max(6, int(self.config["indicators"].get("regime_overlap_lookback", 6))))
        trigger_recent = trigger_df.tail(6)
        session_quality_score = float(session.get("session_quality_score", 60.0))

        atr_current = max(float(setup_latest["atr"]), 1e-9)
        atr_median = max(float(setup_latest["atr_median_20"] or atr_current), 1e-9)
        atr_ratio = atr_current / atr_median
        efficiency = self._directional_efficiency(setup_df["close"], int(self.config["indicators"].get("regime_efficiency_lookback", 8)))
        overlap_ratio = self._average_overlap_ratio(setup_recent)
        ema_distance_atr = abs(float(setup_latest["ema_20"]) - float(setup_latest["ema_50"])) / atr_current
        ema_slope_atr = abs(float(setup_latest["ema_20_slope_2"] or 0.0)) / atr_current
        distance_from_ema20_atr = abs(float(setup_latest["close"]) - float(setup_latest["ema_20"])) / atr_current
        latest_1m_range_atr = float(trigger_recent.iloc[-1]["range"]) / atr_current if not trigger_recent.empty else 0.0
        spread_ratio = float(spread_points) / max(float(execution_cfg["max_spread_points"]), 1e-9)
        allow_range_regime = bool(regime_cfg.get("allow_range_regime", False))
        regime_name = "RANGE_MEAN_REVERSION"
        confidence = 50.0
        block_reason = None
        live_allowed = True

        low_quality_session = session_quality_score < 55.0 and spread_ratio > 0.92
        if low_quality_session and atr_ratio <= float(regime_cfg["low_volatility_atr_ratio"]):
            regime_name = "NO_TRADE"
            confidence = 82.0
            block_reason = "low_quality_session_low_volatility"
            live_allowed = False
        elif atr_ratio >= float(regime_cfg["high_volatility_atr_ratio"]) or (spread_ratio >= float(regime_cfg["spread_instability_ratio"]) and atr_ratio >= 1.05):
            regime_name = "HIGH_VOLATILITY_BREAKOUT"
            confidence = min(96.0, 62.0 + atr_ratio * 8.0 + session_quality_score * 0.1)
            block_reason = None if atr_ratio <= float(regime_cfg["high_volatility_atr_ratio"]) + 0.35 else "high_volatility_breakout"
            live_allowed = True
        elif bias.get("direction") != "NONE" and efficiency >= float(regime_cfg["trend_efficiency_min"]) and overlap_ratio < float(regime_cfg["overlap_chop_min"]) and ema_distance_atr >= float(regime_cfg["ema_alignment_min_atr"]) and ema_slope_atr >= float(regime_cfg["ema_slope_min_atr"]):
            regime_name = "TREND_CONTINUATION"
            confidence = min(96.0, 56.0 + bias.get("confidence", 0.0) * 0.35 + efficiency * 42.0 + session_quality_score * 0.05)
            block_reason = None
            live_allowed = True
        elif distance_from_ema20_atr >= float(regime_cfg["distance_from_value_extreme_atr"]) and latest_1m_range_atr > 0.9:
            regime_name = "HIGH_VOLATILITY_BREAKOUT"
            confidence = 76.0
            block_reason = "price_expanded_away_from_value"
            live_allowed = True

        if regime_name == "RANGE_MEAN_REVERSION":
            live_allowed = bool(allow_range_regime)
            if not live_allowed:
                block_reason = "range_regime_disabled"
        if regime_name == "NO_TRADE":
            live_allowed = False

        return {
            "regime_name": regime_name,
            "regime_confidence": round(confidence, 2),
            "bias": bias.get("direction", "NONE"),
            "live_allowed": live_allowed,
            "regime_block_reason": block_reason,
            "metrics": {
                "atr_ratio": round(atr_ratio, 3),
                "efficiency": round(efficiency, 3),
                "overlap_ratio": round(overlap_ratio, 3),
                "ema_distance_atr": round(ema_distance_atr, 3),
                "ema_slope_atr": round(ema_slope_atr, 3),
                "spread_ratio": round(spread_ratio, 3),
                "distance_from_ema20_atr": round(distance_from_ema20_atr, 3),
                "session_quality_score": round(session_quality_score, 2),
            },
        }

    def generate_setup_candidates(
        self,
        market_context: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build all eligible setup family candidates for the current cycle."""
        self._log_slice_diagnostics(
            "strategy_slice_diagnostics",
            trend_df,
            setup_df,
            trigger_df,
            market_context=market_context,
            extra={"stage": "generate_setup_candidates"},
        )
        if trend_df is None or trend_df.empty or setup_df is None or setup_df.empty or trigger_df is None or trigger_df.empty:
            self._log_empty_slice(
                "generate_setup_candidates",
                trend_df,
                setup_df,
                trigger_df,
                market_context=market_context,
            )
            return []
        candidates = [
            self._trend_pullback_reclaim(market_context, trend_df, setup_df),
            self._breakout_retest_continuation(market_context, setup_df),
            self._liquidity_sweep_reversal(market_context, setup_df, trigger_df),
            self._compression_release(market_context, setup_df, trigger_df),
        ]
        return [candidate for candidate in candidates if candidate is not None]

    def choose_best_setup(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the strongest eligible setup candidate."""
        eligible = [candidate for candidate in candidates if candidate.get("setup_valid")]
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda item: (
                float(item.get("setup_score", 0.0)),
                float(item.get("trend_score", 0.0)),
                float(item.get("session_quality_score", 0.0)),
                float(item.get("regime_confidence", 0.0)),
            ),
            reverse=True,
        )[0]

    def evaluate_entry(
        self,
        candidate: dict[str, Any] | None,
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
        now_utc: datetime,
        live_profile: bool,
    ) -> dict[str, Any]:
        """Validate whether a setup is still tradable and assign an entry mode."""
        if candidate is None:
            return {
                "valid": False,
                "live_ready": False,
                "dry_run_ready": False,
                "entry_mode": "observation_only",
                "reason_code": "no_setup_candidate",
                "reason": "No eligible setup candidate",
                "blocked_reasons": ["no_setup_candidate"],
                "trigger_score": 0.0,
                "entry_score": 0.0,
            }
        if trigger_df.empty:
            return {
                "valid": False,
                "live_ready": False,
                "dry_run_ready": False,
                "entry_mode": "observation_only",
                "reason_code": "trigger_data_unavailable",
                "reason": "1m trigger data unavailable",
                "blocked_reasons": ["trigger_data_unavailable"],
                "trigger_score": 0.0,
                "entry_score": float(candidate.get("setup_score", 0.0)),
            }

        latest = trigger_df.iloc[-1]
        previous = trigger_df.iloc[-2] if len(trigger_df) >= 2 else latest
        atr_value = max(float(candidate.get("atr_at_setup", 0.0) or 0.0), 1e-9)
        entry_cfg = self.config["entry"]
        family = str(candidate["setup_family"])
        strategy_control = self._setup_control(family)
        side = str(candidate["side"])
        entry_price = float(latest["close"])
        trigger_range_atr = float(latest["range"]) / atr_value
        value_distance_atr = abs(entry_price - float(candidate["value_price"])) / atr_value
        anchor_time = to_utc(candidate["anchor_time"])
        setup_age = now_utc - anchor_time if anchor_time else timedelta.max
        confirmation_required = bool(strategy_control.get("confirmation_required", False))
        session_quality_score = float(candidate.get("session_quality_score", candidate.get("regime_confidence", 60.0)))
        regime_quality_score = float(candidate.get("regime_confidence", 60.0))
        thresholds = self._effective_setup_thresholds(strategy_control, live_profile)
        dynamic_value_tolerance = float(entry_cfg["setup_entry_distance_atr_tolerance"])
        if family in {"COMPRESSION_RELEASE", "LIQUIDITY_SWEEP_REVERSAL"}:
            dynamic_value_tolerance *= 1.10
        dynamic_value_tolerance += min(0.20, max(0.0, trigger_range_atr - 0.75) * 0.10)

        blocked_reasons: list[str] = []
        blocked_reasons.extend([str(item) for item in candidate.get("quality_blocked_reasons", []) if str(item)])
        if trigger_range_atr > float(entry_cfg["max_trigger_candle_size_atr"]) * 1.35:
            blocked_reasons.append("blocked_expanded_trigger")
        if value_distance_atr > dynamic_value_tolerance * 1.75:
            if side == "LONG" and entry_price > float(candidate["value_price"]):
                blocked_reasons.append("blocked_chasing_entry")
            elif side == "SHORT" and entry_price < float(candidate["value_price"]):
                blocked_reasons.append("blocked_chasing_entry")
            else:
                blocked_reasons.append("blocked_far_from_value_zone")
        elif value_distance_atr > dynamic_value_tolerance * 1.15:
            if side == "LONG" and entry_price > float(candidate["value_price"]):
                blocked_reasons.append("blocked_chasing_entry")
            elif side == "SHORT" and entry_price < float(candidate["value_price"]):
                blocked_reasons.append("blocked_chasing_entry")
        if setup_age > timedelta(minutes=int(entry_cfg["max_setup_age_minutes"])):
            blocked_reasons.append("blocked_stale_setup")

        trigger_score = 0.0
        trigger_type = ""
        trigger_score += 6.0 if float(latest["body_ratio"]) >= 0.35 else 0.0
        trigger_score += 6.0 if (float(latest["close_location"]) >= 0.55 if side == "LONG" else float(latest["close_location"]) <= 0.45) else 0.0
        trigger_score += 4.0 if float(latest["volume_ratio"] or 0.0) >= 0.9 else 0.0
        trigger_score += 4.0 if value_distance_atr <= dynamic_value_tolerance else 0.0
        if family == "TREND_PULLBACK_RECLAIM":
            if side == "LONG":
                trigger_score += 18.0 if float(latest["close"]) > float(previous["high"]) else 0.0
                trigger_score += 14.0 if float(latest["close"]) > float(latest["ema_20"]) else 0.0
                trigger_score += 10.0 if float(latest["lower_wick"]) >= float(latest["body"]) * 0.7 else 0.0
                trigger_score += 8.0 if float(latest["close_location"]) >= 0.60 else 0.0
            else:
                trigger_score += 18.0 if float(latest["close"]) < float(previous["low"]) else 0.0
                trigger_score += 14.0 if float(latest["close"]) < float(latest["ema_20"]) else 0.0
                trigger_score += 10.0 if float(latest["upper_wick"]) >= float(latest["body"]) * 0.7 else 0.0
                trigger_score += 8.0 if float(latest["close_location"]) <= 0.40 else 0.0
            trigger_type = "RECLAIM_CONFIRMATION"
        elif family == "BREAKOUT_RETEST_CONTINUATION":
            level = float(candidate["structure_level"])
            if side == "LONG":
                trigger_score += 18.0 if float(latest["close"]) > level else 0.0
                trigger_score += 12.0 if float(latest["close"]) > float(previous["high"]) else 0.0
                trigger_score += 10.0 if float(latest["low"]) <= level + (atr_value * 0.18) else 0.0
            else:
                trigger_score += 18.0 if float(latest["close"]) < level else 0.0
                trigger_score += 12.0 if float(latest["close"]) < float(previous["low"]) else 0.0
                trigger_score += 10.0 if float(latest["high"]) >= level - (atr_value * 0.18) else 0.0
            trigger_score += 12.0 if float(latest["body_ratio"]) >= 0.45 else 0.0
            trigger_score += 8.0 if float(latest["volume_ratio"] or 0.0) >= 0.95 else 0.0
            trigger_score += 6.0 if value_distance_atr <= dynamic_value_tolerance * 0.85 else 0.0
            trigger_type = "RETEST_CONTINUATION"
        elif family == "LIQUIDITY_SWEEP_REVERSAL":
            level = float(candidate["structure_level"])
            if side == "LONG":
                trigger_score += 20.0 if float(latest["close"]) > level else 0.0
                trigger_score += 12.0 if float(latest["close"]) > float(latest["ema_9"]) else 0.0
                trigger_score += 10.0 if float(latest["lower_wick"]) >= float(latest["body"]) * 0.8 else 0.0
                trigger_score += 8.0 if float(latest["close_location"]) >= 0.58 else 0.0
            else:
                trigger_score += 20.0 if float(latest["close"]) < level else 0.0
                trigger_score += 12.0 if float(latest["close"]) < float(latest["ema_9"]) else 0.0
                trigger_score += 10.0 if float(latest["upper_wick"]) >= float(latest["body"]) * 0.8 else 0.0
                trigger_score += 8.0 if float(latest["close_location"]) <= 0.42 else 0.0
            trigger_score += 10.0 if float(latest["body_ratio"]) >= 0.40 else 0.0
            trigger_type = "SWEEP_RECLAIM"
        elif family == "COMPRESSION_RELEASE":
            level = float(candidate["structure_level"])
            if side == "LONG":
                trigger_score += 18.0 if float(latest["close"]) > level else 0.0
                trigger_score += 12.0 if float(latest["high"]) >= float(candidate["compression_high"]) else 0.0
                trigger_score += 10.0 if float(latest["close"]) > float(latest["ema_20"]) else 0.0
            else:
                trigger_score += 18.0 if float(latest["close"]) < level else 0.0
                trigger_score += 12.0 if float(latest["low"]) <= float(candidate["compression_low"]) else 0.0
                trigger_score += 10.0 if float(latest["close"]) < float(latest["ema_20"]) else 0.0
            trigger_score += 12.0 if float(latest["volume_ratio"] or 0.0) >= 0.95 else 0.0
            trigger_score += 8.0 if value_distance_atr <= dynamic_value_tolerance else 0.0
            trigger_type = "COMPRESSION_RELEASE"

        if trigger_score < max(5.0, float(thresholds["trigger"]) * (0.50 if confirmation_required else 0.35)):
            blocked_reasons.append("trigger_not_confirmed")

        trend_score = float(candidate.get("trend_score", 0.0))
        setup_score = float(candidate.get("setup_score", 0.0))
        entry_score = (trend_score * 0.24) + (setup_score * 0.36) + (trigger_score * 0.22) + (session_quality_score * 0.10) + (regime_quality_score * 0.08)
        if value_distance_atr > dynamic_value_tolerance:
            entry_score -= min(10.0, (value_distance_atr - dynamic_value_tolerance) * 12.0)
        if trigger_range_atr > float(entry_cfg["max_trigger_candle_size_atr"]):
            entry_score -= min(8.0, (trigger_range_atr - float(entry_cfg["max_trigger_candle_size_atr"])) * 10.0)
        if session_quality_score < 70.0:
            entry_score -= min(8.0, (70.0 - session_quality_score) * 0.15)
        require_trend_alignment = self._bool_override(
            strategy_control,
            "require_trend_alignment",
            bool(
                self.config["strategy"].get(
                    "require_trend_alignment",
                    self.config["strategy"].get("bias_alignment_required", True),
                )
            ),
        )
        min_score_to_trade = float(self.config["strategy"].get("min_score_to_trade", thresholds["entry"]))
        if strategy_control.get("min_entry_score") is not None:
            min_score_to_trade = float(strategy_control["min_entry_score"])
        effective_entry_threshold = max(thresholds["entry"], min_score_to_trade if live_profile else thresholds["entry"])

        if require_trend_alignment and not bool(candidate.get("trend_alignment", True)):
            blocked_reasons.append("trend_alignment_required")
        if trend_score < thresholds["trend"] * 0.92:
            blocked_reasons.append("trend_score_below_threshold")
        if setup_score < thresholds["setup"] * 0.92:
            blocked_reasons.append("setup_score_below_threshold")
        if trigger_score < thresholds["trigger"] * (0.85 if confirmation_required else 0.70):
            blocked_reasons.append("trigger_score_below_threshold")
        if live_profile and entry_score < min_score_to_trade:
            blocked_reasons.append("entry_score_below_min_score_to_trade")

        modes_cfg = self.config["strategy"]["entry_modes"]
        entry_mode = "observation_only"
        if not candidate.get("observation_only") and candidate.get("family_live_allowed", False) and len(blocked_reasons) == 0:
            if bool(modes_cfg.get("aggressive_enabled", True)) and entry_score >= float(modes_cfg["aggressive_min_entry_score"]) and trigger_score >= thresholds["trigger"]:
                entry_mode = "aggressive"
            elif entry_score >= float(modes_cfg["confirmed_min_entry_score"]):
                entry_mode = "confirmed"
            elif bool(modes_cfg.get("fallback_enabled", False)) and entry_score >= float(modes_cfg["fallback_min_entry_score"]):
                entry_mode = "fallback"

        trend_filter_pass = trend_score >= thresholds["trend"] and (
            not require_trend_alignment or bool(candidate.get("trend_alignment", True))
        )
        setup_filter_pass = setup_score >= thresholds["setup"]
        trigger_filter_pass = trigger_score >= thresholds["trigger"] and "trigger_not_confirmed" not in blocked_reasons
        valid = len(blocked_reasons) == 0 and entry_score >= effective_entry_threshold
        live_ready = valid and entry_mode != "observation_only"
        dry_run_ready = "blocked_stale_setup" not in blocked_reasons and "blocked_far_from_value_zone" not in blocked_reasons

        return {
            "valid": valid,
            "live_ready": live_ready,
            "dry_run_ready": dry_run_ready,
            "entry_mode": entry_mode,
            "trigger_type": trigger_type,
            "entry_price": normalize_price(entry_price, int(symbol_spec["digits"])),
            "reason_code": blocked_reasons[0] if blocked_reasons else ("entry_valid" if valid else "entry_score_below_threshold"),
            "reason": ", ".join(blocked_reasons) if blocked_reasons else f"entry_score={entry_score:.1f}",
            "blocked_reasons": blocked_reasons,
            "trend_filter_pass": trend_filter_pass,
            "setup_filter_pass": setup_filter_pass,
            "trigger_filter_pass": trigger_filter_pass,
            "trigger_score": round(trigger_score, 2),
            "entry_score": round(entry_score, 2),
            "trend_score": round(trend_score, 2),
            "setup_score": round(setup_score, 2),
            "session_quality_score": round(session_quality_score, 2),
            "regime_quality_score": round(regime_quality_score, 2),
            "entry_threshold": round(effective_entry_threshold, 2),
            "min_score_to_trade": round(min_score_to_trade, 2),
            "trigger_candle_atr": round(trigger_range_atr, 3),
            "value_distance_atr": round(value_distance_atr, 3),
            "candidate_quality_score": float(candidate.get("candidate_quality_score", candidate.get("setup_score", 0.0)) or 0.0),
            "candidate_quality_bucket": str(candidate.get("candidate_quality_bucket") or ""),
            "quality_metrics": dict(candidate.get("quality_metrics") or {}),
        }

    def _trend_pullback_reclaim(
        self,
        market_context: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """Build a trend pullback reclaim setup candidate."""
        bias = market_context["bias"]
        regime = market_context["regime"]
        if bias.get("direction") == "NONE" or len(setup_df) < 8:
            return None

        latest = setup_df.iloc[-1]
        recent = setup_df.tail(6)
        atr_value = max(float(latest["atr"]), 1e-9)
        side = str(bias["direction"])
        family_cfg = self.config["strategy"]["setup_families"]["trend_pullback_reclaim"]
        if not bool(family_cfg.get("enabled", True)):
            return None

        if side == "LONG":
            touched_ema20 = bool(((recent["low"] - recent["ema_20"]).abs() / atr_value <= 0.22).any())
            touched_ema50 = bool(((recent["low"] - recent["ema_50"]).abs() / atr_value <= 0.32).any())
            breakout_level = float(recent.iloc[:-1]["high"].max()) if len(recent) >= 2 else float(latest["high"])
            tested_breakout = abs(float(latest["low"]) - breakout_level) / atr_value <= 0.35
            impulse_origin = float(recent["low"].min())
            tested_impulse = abs(float(latest["low"]) - impulse_origin) / atr_value <= 0.55
            rejection = float(latest["close"]) > float(latest["open"]) and float(latest["lower_wick"]) >= float(latest["body"]) * 0.8
            reclaimed = float(latest["close"]) > float(latest["ema_20"])
            structure_level = min(float(recent["low"].min()), float(latest["ema_50"]))
            value_points = [float(latest["ema_20"]), float(latest["ema_50"])]
        else:
            touched_ema20 = bool(((recent["high"] - recent["ema_20"]).abs() / atr_value <= 0.22).any())
            touched_ema50 = bool(((recent["high"] - recent["ema_50"]).abs() / atr_value <= 0.32).any())
            breakout_level = float(recent.iloc[:-1]["low"].min()) if len(recent) >= 2 else float(latest["low"])
            tested_breakout = abs(float(latest["high"]) - breakout_level) / atr_value <= 0.35
            impulse_origin = float(recent["high"].max())
            tested_impulse = abs(float(latest["high"]) - impulse_origin) / atr_value <= 0.55
            rejection = float(latest["close"]) < float(latest["open"]) and float(latest["upper_wick"]) >= float(latest["body"]) * 0.8
            reclaimed = float(latest["close"]) < float(latest["ema_20"])
            structure_level = max(float(recent["high"].max()), float(latest["ema_50"]))
            value_points = [float(latest["ema_20"]), float(latest["ema_50"])]

        if tested_breakout:
            value_points.append(breakout_level)
        if tested_impulse:
            value_points.append(impulse_origin)

        confluences = sum([touched_ema20, touched_ema50, tested_breakout, tested_impulse])
        setup_score = 0.0
        setup_score += 20.0 if touched_ema20 else 0.0
        setup_score += 12.0 if touched_ema50 else 0.0
        setup_score += 10.0 if tested_breakout else 0.0
        setup_score += 8.0 if tested_impulse else 0.0
        setup_score += 16.0 if rejection else 0.0
        setup_score += 14.0 if reclaimed else 0.0
        setup_score += 10.0 if float(latest["adx"]) >= 18 else 0.0
        setup_score += 10.0 if float(latest["body_ratio"]) >= 0.40 else 0.0

        setup_valid = (
            regime["regime_name"] == "TREND_CONTINUATION"
            and confluences >= int(self.config["entry"]["value_zone_min_confluence_count"])
            and (rejection or reclaimed)
            and setup_score >= 38.0
        )
        anchor_time = pd.to_datetime(latest["time"], utc=True).to_pydatetime().isoformat()
        return self._setup_payload(
            family="TREND_PULLBACK_RECLAIM",
            family_cfg=family_cfg,
            market_context=market_context,
            side=side,
            anchor_time=anchor_time,
            setup_price=float(latest["close"]),
            value_price=sum(value_points) / max(len(value_points), 1),
            structure_level=structure_level,
            setup_score=setup_score,
            atr_value=atr_value,
            setup_valid=setup_valid,
            trigger_type="VALUE_RECLAIM",
            extra={
                "value_confluences": confluences,
                "breakout_level": breakout_level,
                "impulse_origin": impulse_origin,
                "origin_price": impulse_origin,
                "profit_headroom_atr": abs((float(recent["high"].max()) if side == "LONG" else float(recent["low"].min())) - float(latest["close"])) / atr_value,
                "price_extension_atr": abs(float(latest["close"]) - impulse_origin) / atr_value,
                "structure_quality_score": round(setup_score, 2),
            },
        )

    def _breakout_retest_continuation(
        self,
        market_context: dict[str, Any],
        setup_df: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """Build a breakout retest continuation setup candidate."""
        bias = market_context["bias"]
        regime = market_context["regime"]
        if bias.get("direction") == "NONE" or len(setup_df) < 8:
            return None

        latest = setup_df.iloc[-1]
        previous = setup_df.iloc[-2]
        lookback = setup_df.tail(7).iloc[:-2] if len(setup_df) > 3 else setup_df.iloc[:-1]
        if lookback.empty:
            return None

        atr_value = max(float(latest["atr"]), 1e-9)
        side = str(bias["direction"])
        family_cfg = self.config["strategy"]["setup_families"]["breakout_retest_continuation"]
        if not bool(family_cfg.get("enabled", True)):
            return None

        if side == "LONG":
            breakout_level = float(lookback["high"].max())
            breakout_impulse = float(previous["close"]) > breakout_level and float(previous["range"]) / atr_value >= 0.85
            retest = abs(float(latest["low"]) - breakout_level) / atr_value <= float(self.config["entry"]["breakout_retest_distance_atr_tolerance"])
            continuation_hold = float(latest["close"]) >= breakout_level and float(latest["close"]) > float(latest["open"])
            structure_level = min(float(latest["low"]), breakout_level)
        else:
            breakout_level = float(lookback["low"].min())
            breakout_impulse = float(previous["close"]) < breakout_level and float(previous["range"]) / atr_value >= 0.85
            retest = abs(float(latest["high"]) - breakout_level) / atr_value <= float(self.config["entry"]["breakout_retest_distance_atr_tolerance"])
            continuation_hold = float(latest["close"]) <= breakout_level and float(latest["close"]) < float(latest["open"])
            structure_level = max(float(latest["high"]), breakout_level)

        setup_score = 0.0
        setup_score += 26.0 if breakout_impulse else 0.0
        setup_score += 20.0 if retest else 0.0
        setup_score += 14.0 if continuation_hold else 0.0
        setup_score += 10.0 if float(previous["volume_ratio"] or 0.0) >= 1.0 else 0.0
        setup_score += 10.0 if float(latest["body_ratio"]) >= 0.40 else 0.0

        setup_valid = regime["regime_name"] in {"TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT"} and breakout_impulse and (retest or continuation_hold) and setup_score >= 42.0
        anchor_bar = previous if breakout_impulse else latest
        anchor_time = pd.to_datetime(anchor_bar["time"], utc=True).to_pydatetime().isoformat()
        return self._setup_payload(
            family="BREAKOUT_RETEST_CONTINUATION",
            family_cfg=family_cfg,
            market_context=market_context,
            side=side,
            anchor_time=anchor_time,
            setup_price=float(latest["close"]),
            value_price=breakout_level,
            structure_level=structure_level,
            setup_score=setup_score,
            atr_value=atr_value,
            setup_valid=setup_valid,
            trigger_type="BREAKOUT_RETEST",
            extra={
                "breakout_level": breakout_level,
                "origin_price": breakout_level,
                "profit_headroom_atr": abs((float(lookback["high"].max()) if side == "LONG" else float(lookback["low"].min())) - float(latest["close"])) / atr_value,
                "price_extension_atr": abs(float(latest["close"]) - breakout_level) / atr_value,
                "structure_quality_score": round(setup_score, 2),
            },
        )

    def _liquidity_sweep_reversal(
        self,
        market_context: dict[str, Any],
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """Build a liquidity sweep reversal setup candidate."""
        if setup_df is None or setup_df.empty or trigger_df is None or len(trigger_df) < 6:
            return None

        latest = trigger_df.iloc[-1]
        recent = trigger_df.tail(6).iloc[:-1]
        atr_value = max(float(setup_df.iloc[-1]["atr"]), 1e-9)
        family_cfg = self.config["strategy"]["setup_families"]["liquidity_sweep_reversal"]
        if not bool(family_cfg.get("enabled", True)):
            return None
        if float(latest["low"]) < float(recent["low"].min()) and float(latest["close"]) > float(recent["low"].min()):
            side = "LONG"
            sweep_valid = True
            structure_level = float(recent["low"].min())
            sweep_extreme = float(latest["low"])
        elif float(latest["high"]) > float(recent["high"].max()) and float(latest["close"]) < float(recent["high"].max()):
            side = "SHORT"
            sweep_valid = True
            structure_level = float(recent["high"].max())
            sweep_extreme = float(latest["high"])
        else:
            return None

        sweep_depth = abs(sweep_extreme - structure_level)
        min_sweep_depth = atr_value * float(self.config["strategy"].get("liquidity_sweep_min_depth_atr", 0.5) or 0.5)
        if sweep_depth < min_sweep_depth:
            return None

        setup_score = 0.0
        setup_score += 30.0 if sweep_valid else 0.0
        setup_score += 18.0 if float(latest["body_ratio"]) >= 0.45 else 0.0
        setup_score += 14.0 if (float(latest["close_location"]) >= 0.60 if side == "LONG" else float(latest["close_location"]) <= 0.40) else 0.0
        setup_score += 10.0 if float(latest["volume_ratio"] or 0.0) >= 1.0 else 0.0
        setup_score += 8.0 if market_context["bias"].get("direction") in {"NONE", side} else 0.0

        setup_valid = market_context["regime"]["regime_name"] in {"RANGE_MEAN_REVERSION", "HIGH_VOLATILITY_BREAKOUT"} and setup_score >= 38.0
        anchor_time = pd.to_datetime(latest["time"], utc=True).to_pydatetime().isoformat()
        return self._setup_payload(
            family="LIQUIDITY_SWEEP_REVERSAL",
            family_cfg=family_cfg,
            market_context=market_context,
            side=side,
            anchor_time=anchor_time,
            setup_price=float(latest["close"]),
            value_price=structure_level,
            structure_level=structure_level,
            setup_score=setup_score,
            atr_value=atr_value,
            setup_valid=setup_valid,
            trigger_type="SWEEP_RECLAIM",
            extra={
                "sweep_extreme": sweep_extreme,
                "sweep_depth": sweep_depth,
                "min_sweep_depth": min_sweep_depth,
                "origin_price": sweep_extreme,
                "profit_headroom_atr": abs((float(recent["high"].max()) if side == "LONG" else float(recent["low"].min())) - float(latest["close"])) / atr_value,
                "price_extension_atr": abs(float(latest["close"]) - sweep_extreme) / atr_value,
                "structure_quality_score": round(setup_score, 2),
            },
        )

    def _compression_release(
        self,
        market_context: dict[str, Any],
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """Build a compression release setup candidate."""
        bias = market_context["bias"]
        if (
            bias.get("direction") == "NONE"
            or setup_df is None
            or len(setup_df) < 8
            or trigger_df is None
            or trigger_df.empty
        ):
            return None

        recent = setup_df.tail(6)
        latest = setup_df.iloc[-1]
        atr_value = max(float(latest["atr"]), 1e-9)
        compression_range = float(recent["high"].max()) - float(recent["low"].min())
        compression_ratio = compression_range / (atr_value * max(len(recent), 1))
        overlapping = self._average_overlap_ratio(recent)
        side = str(bias["direction"])
        family_cfg = self.config["strategy"]["setup_families"]["compression_release"]
        if not bool(family_cfg.get("enabled", True)):
            return None
        compression_high = float(recent["high"].max())
        compression_low = float(recent["low"].min())
        release_candidate = compression_ratio <= float(self.config["regime"]["compression_ratio_max"]) and overlapping >= 0.45

        if side == "LONG":
            hold = float(latest["close"]) >= float(latest["ema_20"])
            structure_level = compression_low
            overextended = (float(trigger_df.iloc[-1]["close"]) - compression_high) / atr_value > float(self.config["entry"]["impulse_extension_limit_atr"])
        else:
            hold = float(latest["close"]) <= float(latest["ema_20"])
            structure_level = compression_high
            overextended = (compression_low - float(trigger_df.iloc[-1]["close"])) / atr_value > float(self.config["entry"]["impulse_extension_limit_atr"])

        setup_score = 0.0
        setup_score += 24.0 if release_candidate else 0.0
        setup_score += 18.0 if hold else 0.0
        setup_score += 12.0 if float(latest["adx"]) >= 16 else 0.0
        setup_score += 10.0 if float(latest["volume_ratio"] or 0.0) >= 0.95 else 0.0
        setup_score += 10.0 if not overextended else 0.0

        setup_valid = market_context["regime"]["regime_name"] in {"TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT"} and release_candidate and hold and not overextended and setup_score >= 38.0
        anchor_time = pd.to_datetime(latest["time"], utc=True).to_pydatetime().isoformat()
        return self._setup_payload(
            family="COMPRESSION_RELEASE",
            family_cfg=family_cfg,
            market_context=market_context,
            side=side,
            anchor_time=anchor_time,
            setup_price=float(latest["close"]),
            value_price=structure_level,
            structure_level=structure_level,
            setup_score=setup_score,
            atr_value=atr_value,
            setup_valid=setup_valid,
            trigger_type="COMPRESSION_RELEASE",
            extra={
                "compression_high": compression_high,
                "compression_low": compression_low,
                "origin_price": compression_high if side == "LONG" else compression_low,
                "profit_headroom_atr": abs((float(recent["high"].max()) if side == "LONG" else float(recent["low"].min())) - float(latest["close"])) / atr_value,
                "price_extension_atr": abs(float(trigger_df.iloc[-1]["close"]) - (compression_high if side == "LONG" else compression_low)) / atr_value,
                "structure_quality_score": round(setup_score, 2),
            },
        )

    def _setup_payload(
        self,
        family: str,
        family_cfg: dict[str, Any],
        market_context: dict[str, Any],
        side: str,
        anchor_time: str,
        setup_price: float,
        value_price: float,
        structure_level: float,
        setup_score: float,
        atr_value: float,
        setup_valid: bool,
        trigger_type: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a normalized setup candidate payload."""
        symbol = str(self.config["mt5"]["symbol"])
        regime = market_context["regime"]
        session = market_context["session"]
        strategy_cfg = self.config["strategy"]
        strategy_control = self._setup_control(family)
        sessions_cfg = self.config["sessions"]
        regime_cfg = self.config["regime"]
        fingerprint = f"{symbol}|{side}|{family}|{anchor_time}|{normalize_price(structure_level, 2)}"
        family_sessions = set(family_cfg.get("sessions", []))
        family_regimes = set(family_cfg.get("regimes", []))
        strategy_allowed_regimes = set(strategy_control.get("allowed_regimes", [])) if strategy_control.get("allowed_regimes") else set()
        family_enabled = bool(family_cfg.get("enabled", True)) and bool(strategy_control.get("enabled", True))
        session_allowed = bool(session["session_live_allowed"])
        regime_allowed = regime["regime_name"] in family_regimes if family_regimes else True
        if strategy_allowed_regimes:
            regime_allowed = regime["regime_name"] in strategy_allowed_regimes
        if regime["regime_name"] == "RANGE_MEAN_REVERSION":
            regime_allowed = bool(regime_allowed) and bool(regime_cfg.get("allow_range_regime", False))
        family_live_allowed = bool(family_cfg.get("live_allowed", False))
        if side == "LONG" and not bool(strategy_control.get("allow_long", True)):
            family_enabled = False
            family_live_allowed = False
        if side == "SHORT" and not bool(strategy_control.get("allow_short", True)):
            family_enabled = False
            family_live_allowed = False
        trend_alignment = market_context["bias"].get("direction") in {"NONE", side}
        require_trend_alignment = bool(strategy_cfg.get("require_trend_alignment", strategy_cfg.get("bias_alignment_required", True)))
        observation_only = not (
            family_enabled
            and family_live_allowed
            and session_allowed
            and regime_allowed
            and (trend_alignment or not require_trend_alignment)
        )
        if not family_enabled:
            live_block_reason = "setup_family_disabled"
        elif not family_live_allowed:
            live_block_reason = "setup_family_live_disabled"
        elif not session_allowed:
            live_block_reason = str(session.get("session_block_reason") or "session_filter_blocked")
        elif not regime_allowed:
            live_block_reason = str(regime.get("regime_block_reason") or "regime_filter_blocked")
        elif require_trend_alignment and not trend_alignment:
            live_block_reason = "trend_alignment_required"
        else:
            live_block_reason = None

        return {
            "setup_valid": bool(setup_valid),
            "setup_family": family,
            "side": side,
            "setup_fingerprint": fingerprint,
            "anchor_time": anchor_time,
            "setup_price": normalize_price(setup_price, 2),
            "value_price": normalize_price(value_price, 2),
            "structure_level": normalize_price(structure_level, 2),
            "setup_score": round(setup_score, 2),
            "trend_score": float(market_context["bias"].get("trend_score", 0.0)),
            "regime_name": regime["regime_name"],
            "regime_confidence": float(regime["regime_confidence"]),
            "session_name": session["session_name"],
            "session_live_allowed": bool(session["session_live_allowed"]),
            "session_allowed": bool(session_allowed),
            "session_quality_score": float(session.get("session_quality_score", 60.0)),
            "session_quality_label": session.get("session_quality_label", "secondary"),
            "regime_allowed": bool(regime_allowed),
            "family_enabled": bool(family_enabled),
            "family_live_allowed": family_live_allowed,
            "family_allowed_sessions": sorted(family_sessions),
            "family_allowed_regimes": sorted(family_regimes),
            "effective_allowed_regimes": sorted(strategy_allowed_regimes or family_regimes),
            "strategy_control": strategy_control,
            "observation_only": observation_only,
            "trend_alignment": bool(trend_alignment),
            "bias_direction": str(market_context["bias"].get("direction", "NONE")),
            "bias_confidence": float(market_context["bias"].get("confidence", 0.0) or 0.0),
            "bias_long_score": float(market_context["bias"].get("long_score", 0.0) or 0.0),
            "bias_short_score": float(market_context["bias"].get("short_score", 0.0) or 0.0),
            "bias_delta": abs(float(market_context["bias"].get("long_score", 0.0) or 0.0) - float(market_context["bias"].get("short_score", 0.0) or 0.0)),
            "ema_slope_atr": float(regime.get("metrics", {}).get("ema_slope_atr", 0.0) or 0.0),
            "live_block_reason": live_block_reason,
            "trigger_type": trigger_type,
            "atr_at_setup": float(atr_value),
            "reason_code": "setup_valid" if setup_valid else "setup_ineligible",
            "reason": f"{family} score={setup_score:.1f}",
            **extra,
        }

    def _directional_efficiency(self, series: pd.Series, lookback: int) -> float:
        """Return directional efficiency over a lookback window."""
        window = series.tail(max(lookback, 2))
        if len(window) < 2:
            return 0.0
        net = abs(float(window.iloc[-1]) - float(window.iloc[0]))
        traveled = float(window.diff().abs().sum())
        if traveled <= 0:
            return 0.0
        return net / traveled

    def _average_overlap_ratio(self, frame: pd.DataFrame) -> float:
        """Return the average overlap ratio between consecutive candle ranges."""
        if len(frame) < 2:
            return 0.0
        overlaps: list[float] = []
        for index in range(1, len(frame)):
            previous = frame.iloc[index - 1]
            current = frame.iloc[index]
            overlap_low = max(float(previous["low"]), float(current["low"]))
            overlap_high = min(float(previous["high"]), float(current["high"]))
            overlap = max(0.0, overlap_high - overlap_low)
            reference_range = max(float(previous["range"]), float(current["range"]), 1e-9)
            overlaps.append(overlap / reference_range)
        return sum(overlaps) / max(len(overlaps), 1)

    @staticmethod
    def _hhmm_to_minutes(hhmm: str) -> int:
        """Convert HH:MM into minutes from midnight."""
        hours, minutes = hhmm.split(":")
        return (int(hours) * 60) + int(minutes)

    def setup_thresholds(self, live_profile: bool) -> dict[str, float]:
        """Return the active threshold profile."""
        thresholds = self.config["strategy"]["live_thresholds"] if live_profile else self.config["strategy"]["dry_run_thresholds"]
        prefix = "live_" if live_profile else "dry_run_"
        return {
            "trend": float(thresholds[f"{prefix}min_trend_score"]),
            "setup": float(thresholds[f"{prefix}min_setup_score"]),
            "trigger": float(thresholds[f"{prefix}min_trigger_score"]),
            "entry": float(thresholds[f"{prefix}min_entry_score"]),
        }
