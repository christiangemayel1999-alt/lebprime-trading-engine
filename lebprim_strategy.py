"""
LEBPRIM — Limit Entry Breakout Pressure with Regime Intelligence Momentum
XAUUSD Scalping Strategy for MT5 Bot

Canonical usage:
  - Construct through strategy_factory.build_strategy_engine(...)
  - Register through trading_bot.strategy.adapters.LebprimStrategyAdapter
  - Treat this file as the concrete LEBPRIM implementation, not a standalone runner

Architecture:
  - Uses existing indicators.py plus the unified risk/execution/backtest path
  - New setup family: LEBPRIM_SCALP
  - Designed around 3 core insights from backtest data:
      1. 2349/3028 blocks were 'blocked_chasing_entry' → use limit orders, not market
      2. SHORT BREAKOUT fast scalps: 57% WR, AvgR 0.66 → focus on compression-into-break
      3. Hour 1 UTC: 80% WR, hours 9/11/16/19/20 > 55% WR → session-gated entries
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


# ---------------------------------------------------------------------------
# Session quality map — derived from actual backtest hour analysis
# Hour UTC → (quality_score, allowed)
# Hours 18, 7, 12 are losers and blocked
# ---------------------------------------------------------------------------
LEBPRIM_HOUR_PROFILE: dict[int, tuple[float, bool]] = {
    1:  (96.0, True),   # 80% WR in data — best hour
    2:  (88.0, True),   # 50% WR, positive
    3:  (82.0, False),  # 53% WR
    9:  (90.0, True),   # 60% WR
    11: (88.0, True),   # 58% WR
    13: (80.0, True),   # 50% WR
    14: (80.0, False),  # NY open
    15: (82.0, True),   # post NY
    16: (90.0, True),   # 57% WR
    19: (88.0, True),   # 58% WR
    20: (86.0, True),   # 56% WR
    21: (78.0, True),   # late NY, ok
    0:  (72.0, True),   # 43% WR but positive net
    4:  (65.0, True),   # 31% WR, marginal
    5:  (60.0, True),   # 29% WR, marginal, allowed for Asia
    6:  (58.0, False),  # thin, blocked
    8:  (52.0, False),  # 41% WR, consistently losing hour — blocked
    7:  (40.0, False),  # 23% WR — blocked
    10: (45.0, False),  # 27% WR — blocked
    12: (38.0, False),  # 18% WR — blocked
    17: (42.0, False),  # 38% WR — blocked
    18: (0.0,  False),  # 0% WR, -$9,757 — hard blocked
    22: (70.0, True),   # low data, allowed
    23: (70.0, True),   # low data, allowed
}


class LebprimStrategy:
    """
    LEBPRIM scalping engine.

    Setup family: LEBPRIM_SCALP
    Signal name:  XAU_LEBPRIM

    Core logic:
      1. PRESSURE ZONE DETECTION
         Look for candle compression (tight range, overlapping bars) on the
         1m/3m frame followed by a momentum expansion candle.
         This is the same concept as COMPRESSION_RELEASE but with:
         - stricter compression ratio (tighter squeeze required)
         - explicit momentum confirmation candle (body >= 55% of range)
         - volume surge confirmation (volume_ratio >= 1.15)

      2. MOMENTUM BREAKOUT ANCHOR
         Identify the most recent swing high/low on the 5m trend frame
         as the invalidation anchor. Use this for SL placement, NOT
         just ATR × multiplier.

      3. LIMIT ENTRY — not market
         Instead of entering at current bar close (which triggers chasing),
         LEBPRIM places entry at the VALUE ZONE:
           LONG: entry limit = (compression_high + last_close) / 2
           SHORT: entry limit = (compression_low + last_close) / 2
         This is set as value_price. The backtest executor uses the limit
         logic via entry_mode='limit_value'.

      4. SESSION GATE
         Only fires during high-probability hours (LEBPRIM_HOUR_PROFILE).
         Hard-blocks hours 7, 8, 10, 12, 17, 18 UTC.

      5. REGIME GATE
         Requires TREND_CONTINUATION or HIGH_VOLATILITY_BREAKOUT.
         Range regime only allowed if compression_ratio < 0.4 (very tight).

      6. BIAS AGREEMENT
         LONG: requires bias direction LONG or NONE (never counter-trend long)
         SHORT: requires bias direction SHORT or NONE
         Exception: if bias_score > 80 and delta > 25, allows contra-trend
                    only in HIGH_VOLATILITY_BREAKOUT regime.

      7. SCALP EXIT LOGIC
         TP1 at 1.0R (partial close 50%) — lock in profit fast
         TP2 at 2.2R — wider than current 2.0R (data shows winners avg 1.9R)
         Breakeven after TP1
         Time stop: 25 minutes (scalping, not swing)
         Momentum exit: if price returns to entry after 5 bars, exit flat

      8. POSITION SIZING
         Base risk: 0.5% per trade
         Scale: × 1.25 when hour quality >= 90 AND regime = TREND_CONTINUATION
         Scale: × 0.6 when it's the 3rd trade today (fatigue reduction)
         Scale: × 0.5 after 2 consecutive losses (max-pain reduction)
    """

    FAMILY = "LEBPRIM_SCALP"
    SIGNAL_NAME = "XAU_LEBPRIM"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._logger = logging.getLogger("mtf_sniper_bot")
        # inject LEBPRIM family into setup_families if not present
        sf = self.config.setdefault("strategy", {}).setdefault("setup_families", {})
        sf.setdefault("lebprim_scalp", {
            "enabled": True,
            "live_allowed": True,
            "sessions": ["ASIA", "LONDON_OPEN", "LONDON", "NY_OPEN",
                         "LONDON_NY_OVERLAP", "LATE_NY"],
            "regimes": ["TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT",
                        "RANGE_MEAN_REVERSION"],
        })
        self.config["strategy"].setdefault("lebprim_momentum_body_ratio_min", 0.55)
        self.config["strategy"].setdefault("lebprim_momentum_body_ratio_strict", 0.62)
        self.config["strategy"].setdefault("lebprim_momentum_close_zone_pct", 0.25)
        self.config["strategy"].setdefault("lebprim_volume_surge_strong_min", 1.15)
        self.config["strategy"].setdefault("lebprim_volume_surge_soft_min", 1.00)
        self.config["strategy"].setdefault("lebprim_pending_expiry_minutes", 5)
        self.config["strategy"].setdefault("lebprim_pending_expiry_bars", 3)
        self.config["strategy"].setdefault("lebprim_short_bias_delta_min", 25.0)
        self.config["strategy"].setdefault("lebprim_short_ema_slope_min_atr", 0.05)
        self.config["strategy"].setdefault("lebprim_max_trigger_range_atr", 1.0)
        self.config["strategy"].setdefault("lebprim_max_stop_distance_price", 6.5)
        self.config["strategy"].setdefault("lebprim_max_stop_distance_atr", 1.75)
        self.config["strategy"].setdefault("lebprim_mode", "restricted")
        self.config["strategy"].setdefault("lebprim_restricted_min_entry_score", 60.0)
        self.config["strategy"].setdefault("lebprim_research_min_entry_score", 52.0)
        self.config["strategy"].setdefault("lebprim_min_regime_confidence", 62.0)
        self.config["strategy"].setdefault("lebprim_min_session_quality", 80.0)
        self.config["strategy"].setdefault("lebprim_research_min_session_quality", 68.0)

    # ------------------------------------------------------------------
    # Public interface — same as StrategyEngine
    # ------------------------------------------------------------------

    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        ind = self.config["indicators"]
        frame = add_trend_indicators(
            df,
            ema_fast_period=int(ind["ema_slow"]),
            ema_slow_period=int(ind["ema_trend"]),
            atr_period=int(ind["atr_period"]),
        )
        return self._augment(frame)

    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        ind = self.config["indicators"]
        frame = add_setup_indicators(
            df,
            ema_fast=int(ind["ema_fast"]),
            ema_slow=int(ind["ema_slow"]),
            rsi_period=int(ind["rsi_period"]),
            atr_period=int(ind["atr_period"]),
            adx_period=int(ind["adx_period"]),
            volume_period=int(ind["volume_period"]),
        )
        frame = self._augment(frame)
        frame["ema_20_slope_2"] = frame["ema_20"] - frame["ema_20"].shift(2)
        frame["ema_50_slope_3"] = frame["ema_50"] - frame["ema_50"].shift(3)
        frame["atr_median_20"] = frame["atr"].rolling(window=20, min_periods=5).median()
        frame["rolling_high_10"] = frame["high"].rolling(window=10, min_periods=3).max()
        frame["rolling_low_10"] = frame["low"].rolling(window=10, min_periods=3).min()
        return frame

    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        frame = add_trigger_indicators(
            df, volume_period=int(self.config["indicators"]["volume_period"])
        )
        frame = self._augment(frame)
        frame["ema_20"] = frame["close"].ewm(span=20, adjust=False).mean()
        frame["ema_9"] = frame["close"].ewm(span=9, adjust=False).mean()
        frame["rolling_high_8"] = frame["high"].rolling(window=8, min_periods=3).max()
        frame["rolling_low_8"] = frame["low"].rolling(window=8, min_periods=3).min()
        return frame

    def analyze_market_context(
        self,
        now_utc: datetime,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]:
        self._log_slice_diagnostics(
            "strategy_slice_diagnostics",
            trend_df,
            setup_df,
            trigger_df,
            extra={"stage": "lebprim_analyze_market_context", "timestamp": now_utc.isoformat()},
        )
        session = self._classify_session(now_utc)
        if trend_df is None or trend_df.empty or setup_df is None or setup_df.empty or trigger_df is None or trigger_df.empty:
            self._log_slice_diagnostics(
                "strategy_slice_empty",
                trend_df,
                setup_df,
                trigger_df,
                extra={"stage": "lebprim_analyze_market_context", "timestamp": now_utc.isoformat()},
            )
            return {
                "timestamp": now_utc.isoformat(),
                "session": session,
                "bias": {"direction": "NONE", "trend_score": 0.0, "long_score": 0.0, "short_score": 0.0, "confidence": 0.0, "reasons": ["slice_unavailable"]},
                "regime": {"regime_name": "NO_TRADE", "regime_confidence": 0.0, "bias": "NONE", "live_allowed": False, "regime_block_reason": "slice_unavailable", "metrics": {}},
                "spread_points": float(spread_points),
            }
        bias = self._analyze_bias(trend_df, setup_df)
        regime = self._classify_regime(trend_df, setup_df, trigger_df,
                                       spread_points, session, bias)
        return {
            "timestamp": now_utc.isoformat(),
            "session": session,
            "bias": bias,
            "regime": regime,
            "spread_points": float(spread_points),
        }

    def classify_session(self, now_utc: datetime) -> dict[str, Any]:
        """Compatibility wrapper expected by the main live runner."""
        return self._classify_session(now_utc)

    def generate_setup_candidates(
        self,
        market_context: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if str(self.config.get("strategy", {}).get("lebprim_mode", "restricted") or "restricted").lower() == "off":
            return []
        self._log_slice_diagnostics(
            "strategy_slice_diagnostics",
            trend_df,
            setup_df,
            trigger_df,
            extra={"stage": "lebprim_generate_setup_candidates", "timestamp": str((market_context or {}).get("timestamp") or "")},
        )
        if trend_df is None or trend_df.empty or setup_df is None or setup_df.empty or trigger_df is None or trigger_df.empty:
            self._log_slice_diagnostics(
                "strategy_slice_empty",
                trend_df,
                setup_df,
                trigger_df,
                extra={"stage": "lebprim_generate_setup_candidates", "timestamp": str((market_context or {}).get("timestamp") or "")},
            )
            return []
        candidate = self._lebprim_scalp(
            market_context, trend_df, setup_df, trigger_df
        )
        return [candidate] if candidate is not None else []

    def choose_best_setup(
        self, candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        eligible = [c for c in candidates if c.get("setup_valid")]
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda x: (
                float(x.get("setup_score", 0.0)),
                float(x.get("trend_score", 0.0)),
                float(x.get("session_quality_score", 0.0)),
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
        if candidate is None or trigger_df.empty:
            return self._blocked("no_setup_candidate", "No LEBPRIM candidate")
        mode = str(self.config.get("strategy", {}).get("lebprim_mode", "restricted") or "restricted").lower()
        if mode == "off":
            return self._blocked("blocked_lebprim_disabled", "LEBPRIM disabled by config")

        latest = trigger_df.iloc[-1]
        previous = trigger_df.iloc[-2] if len(trigger_df) >= 2 else latest
        atr_value = max(float(candidate.get("atr_at_setup", 1.0) or 1.0), 1e-9)
        side = str(candidate["side"])
        trigger_price = float(latest["close"])
        pending_entry_price = float(candidate["value_price"])

        # -----------------------------------------------------------
        # LEBPRIM TRIGGER SCORING
        # -----------------------------------------------------------
        trigger_score = 0.0
        blocked_reasons: list[str] = [str(item) for item in candidate.get("quality_blocked_reasons", []) if str(item)]

        body_ratio = float(latest.get("body_ratio", 0.0) or 0.0)
        close_loc = float(latest.get("close_location", 0.5) or 0.5)
        vol_ratio = float(latest.get("volume_ratio", 0.0) or 0.0)
        lw = float(latest.get("lower_wick", 0.0) or 0.0)
        uw = float(latest.get("upper_wick", 0.0) or 0.0)
        body = float(latest.get("body", 0.0) or 0.0)
        rng = float(latest.get("range", 1.0) or 1.0)
        ema_9 = float(latest.get("ema_9", trigger_price) or trigger_price)

        body_min = float(self.config.get("strategy", {}).get("lebprim_momentum_body_ratio_min", 0.55))
        body_strict = float(self.config.get("strategy", {}).get("lebprim_momentum_body_ratio_strict", 0.62))
        close_zone = float(self.config.get("strategy", {}).get("lebprim_momentum_close_zone_pct", 0.25))
        vol_strong = float(self.config.get("strategy", {}).get("lebprim_volume_surge_strong_min", 1.15))
        vol_soft = float(self.config.get("strategy", {}).get("lebprim_volume_surge_soft_min", 1.00))

        # --- Momentum candle (core LEBPRIM condition) ---
        break_high = float(latest["close"]) > float(previous["high"])
        break_low = float(latest["close"]) < float(previous["low"])
        high_break = float(latest["high"]) > float(previous["high"])
        low_break = float(latest["low"]) < float(previous["low"])
        long_close_top = close_loc >= (1.0 - close_zone)
        short_close_bottom = close_loc <= close_zone
        long_body_confirm = body_ratio >= body_min and (break_high or (high_break and long_close_top and body_ratio >= body_strict))
        short_body_confirm = body_ratio >= body_min and (break_low or (low_break and short_close_bottom and body_ratio >= body_strict))

        if side == "LONG":
            momentum_candle = long_body_confirm
            ema_alignment = float(latest["close"]) > ema_9
            wick_rejection = lw >= body * 0.5
            close_strength = long_close_top
        else:
            momentum_candle = short_body_confirm
            ema_alignment = float(latest["close"]) < ema_9
            wick_rejection = uw >= body * 0.5
            close_strength = short_close_bottom

        trigger_score += 28.0 if momentum_candle else 0.0
        trigger_score += 16.0 if ema_alignment else 0.0
        trigger_score += 12.0 if wick_rejection else 0.0
        trigger_score += 10.0 if close_strength else 0.0
        trigger_score += 14.0 if vol_ratio >= vol_strong else (10.0 if vol_ratio >= vol_soft and rng / atr_value >= 0.8 else 0.0)
        trigger_score += 8.0 if body_ratio >= 0.45 else 0.0
        trigger_score += 6.0 if rng / atr_value <= 1.5 else 0.0  # not overextended

        if not momentum_candle:
            blocked_reasons.append("no_momentum_candle")

        # --- Value distance check (LEBPRIM limit-entry logic) ---
        value_price = float(candidate["value_price"])
        value_dist_atr = abs(trigger_price - value_price) / atr_value
        # LEBPRIM is more tolerant here because we use a LIMIT entry zone
        limit_tolerance = 1.20  # wider than standard 0.85 because we pre-place
        if value_dist_atr > limit_tolerance * 2.0:
            blocked_reasons.append("blocked_overextended_from_value")

        # --- Setup age ---
        anchor_time = to_utc(candidate.get("anchor_time"))
        if anchor_time:
            age = now_utc - anchor_time
            if age > timedelta(minutes=10):  # LEBPRIM: 10min max (scalp urgency)
                blocked_reasons.append("blocked_stale_scalp")

        # --- Minimum trigger score ---
        min_trigger = float(self.config.get("strategy", {}).get("lebprim_restricted_min_trigger_score", 50.0) or 50.0) if mode == "restricted" and live_profile else (45.0 if live_profile else 35.0)
        if trigger_score < min_trigger:
            blocked_reasons.append("trigger_score_below_threshold")

        # --- Entry score composite ---
        setup_score = float(candidate.get("setup_score", 0.0))
        trend_score = float(candidate.get("trend_score", 0.0))
        session_q = float(candidate.get("session_quality_score", 60.0))
        regime_q = float(candidate.get("regime_confidence", 60.0))
        entry_score = (
            trend_score   * 0.20 +
            setup_score   * 0.35 +
            trigger_score * 0.28 +
            session_q     * 0.10 +
            regime_q      * 0.07
        )
        min_entry = (
            float(self.config.get("strategy", {}).get("lebprim_restricted_min_entry_score", 60.0) or 60.0)
            if mode == "restricted" and live_profile
            else float(self.config.get("strategy", {}).get("lebprim_research_min_entry_score", 52.0) or 52.0)
            if mode == "research" and live_profile
            else (52.0 if live_profile else 44.0)
        )
        if live_profile and entry_score < min_entry:
            blocked_reasons.append("entry_score_below_min")
        min_regime = float(self.config.get("strategy", {}).get("lebprim_min_regime_confidence", 62.0) or 62.0)
        min_session = (
            float(self.config.get("strategy", {}).get("lebprim_min_session_quality", 80.0) or 80.0)
            if mode == "restricted"
            else float(self.config.get("strategy", {}).get("lebprim_research_min_session_quality", 68.0) or 68.0)
        )
        if live_profile and regime_q < min_regime:
            blocked_reasons.append("blocked_regime_mismatch")
        if live_profile and session_q < min_session:
            blocked_reasons.append("blocked_bad_session")

        valid = len(blocked_reasons) == 0
        entry_mode = "lebprim_limit" if valid else "observation_only"
        expiry_bonus = 1 if entry_score >= max(min_entry + 6.0, 66.0) else 0
        pending_expiry_minutes = int(self.config.get("strategy", {}).get("lebprim_pending_expiry_minutes", 5) or 5) + expiry_bonus
        pending_expiry_bars = int(self.config.get("strategy", {}).get("lebprim_pending_expiry_bars", 3) or 3) + expiry_bonus

        return {
            "valid": valid,
            "live_ready": valid and bool(candidate.get("family_live_allowed", False)),
            "dry_run_ready": "blocked_stale_scalp" not in blocked_reasons,
            "entry_mode": entry_mode,
            "trigger_type": "LEBPRIM_MOMENTUM",
            "entry_price": normalize_price(pending_entry_price, int(symbol_spec["digits"])),
            "trigger_price": normalize_price(trigger_price, int(symbol_spec["digits"])),
            "pending_entry_price": normalize_price(pending_entry_price, int(symbol_spec["digits"])),
            "pending_expiry_minutes": pending_expiry_minutes,
            "pending_expiry_bars": pending_expiry_bars,
            "reason_code": blocked_reasons[0] if blocked_reasons else "entry_valid",
            "reason": ", ".join(blocked_reasons) if blocked_reasons else f"lebprim_score={entry_score:.1f}",
            "blocked_reasons": blocked_reasons,
            "trigger_score": round(trigger_score, 2),
            "entry_score": round(entry_score, 2),
            "trend_score": round(trend_score, 2),
            "setup_score": round(setup_score, 2),
            "session_quality_score": round(session_q, 2),
            "regime_quality_score": round(regime_q, 2),
            "entry_threshold": min_entry,
            "min_score_to_trade": min_entry,
            "trigger_candle_atr": round(rng / atr_value, 3),
            "value_distance_atr": round(value_dist_atr, 3),
            "momentum_candle": momentum_candle,
            "ema_alignment": ema_alignment,
            "lebprim_mode": mode,
            "candidate_quality_score": float(candidate.get("candidate_quality_score", candidate.get("setup_score", 0.0)) or 0.0),
            "candidate_quality_bucket": str(candidate.get("candidate_quality_bucket") or ""),
            "quality_metrics": dict(candidate.get("quality_metrics") or {}),
        }

    # ------------------------------------------------------------------
    # LEBPRIM Core Setup Detection
    # ------------------------------------------------------------------

    def _lebprim_scalp(
        self,
        ctx: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
    ) -> dict[str, Any] | None:
        """
        LEBPRIM_SCALP setup detection.

        Three conditions must all be true:
          A. PRESSURE ZONE: 4+ consecutive bars with compression_ratio < 0.65
             (bars overlap >= 55%, range tightening, like a coiled spring)
          B. MOMENTUM BREAK: last closed bar breaks out of the compression
             with body >= 55% and volume >= 1.1× average
          C. CLEAN ANCHOR: swing high/low on setup_df provides a structural
             invalidation point that is at least 1.5× ATR away
        """
        if len(setup_df) < 12 or len(trigger_df) < 8:
            return None

        family_cfg = self.config["strategy"]["setup_families"].get("lebprim_scalp", {})
        if not bool(family_cfg.get("enabled", True)):
            return None

        bias = ctx["bias"]
        regime = ctx["regime"]
        session = ctx["session"]

        # --- Session hour gate ---
        now_utc_str = ctx.get("timestamp", "")
        hour = self._extract_hour_utc(now_utc_str)
        hour_quality, hour_allowed = LEBPRIM_HOUR_PROFILE.get(hour, (60.0, True))
        if not hour_allowed:
            return None

        # --- Regime gate ---
        regime_name = regime.get("regime_name", "NO_TRADE")
        if regime_name == "NO_TRADE":
            return None
        if regime_name == "RANGE_MEAN_REVERSION":
            # Only allow in very tight compression (rare high-quality range setups)
            pass  # will check compression_ratio below

        atr_value = max(float(setup_df.iloc[-1].get("atr", 1.0) or 1.0), 1e-9)
        atr_median = max(float(setup_df.iloc[-1].get("atr_median_20") or atr_value), 1e-9)
        atr_ratio = atr_value / atr_median
        if atr_ratio > float(self.config.get("strategy", {}).get("lebprim_max_atr_ratio", 2.2)):
            return None
        latest_s = setup_df.iloc[-1]
        recent_s = setup_df.tail(8)

        # ---------------------------------------------------------------
        # CONDITION A: PRESSURE ZONE (compression detection)
        # ---------------------------------------------------------------
        compression_window = setup_df.tail(6).iloc[:-1]  # exclude very last bar
        if len(compression_window) < 4:
            return None

        zone_high = float(compression_window["high"].max())
        zone_low = float(compression_window["low"].min())
        zone_range = zone_high - zone_low

        # Compression ratio: zone range vs ATR. Tight = small ratio
        compression_ratio = zone_range / (atr_value * max(len(compression_window), 1))
        # Overlap ratio: how overlapping are consecutive bars
        overlap = self._overlap_ratio(compression_window)

        # LEBPRIM requires tight compression
        max_compression_ratio = float(
            self.config.get("strategy", {}).get("lebprim_compression_ratio_max",
            self.config.get("regime", {}).get("compression_ratio_max", 0.78))
        )
        lebprim_tight = max_compression_ratio * 0.80  # 20% tighter than standard
        if compression_ratio > lebprim_tight or overlap < 0.40:
            return None

        # In range regime, require very tight compression
        if regime_name == "RANGE_MEAN_REVERSION" and compression_ratio > 0.45:
            return None

        # ---------------------------------------------------------------
        # CONDITION B: MOMENTUM BREAK on trigger (1m)
        # ---------------------------------------------------------------
        if len(trigger_df) < 4:
            return None

        last_t = trigger_df.iloc[-1]
        prev_t = trigger_df.iloc[-2]

        last_body_ratio = float(last_t.get("body_ratio", 0.0) or 0.0)
        last_vol_ratio = float(last_t.get("volume_ratio", 0.0) or 0.0)
        last_close = float(last_t["close"])
        last_open = float(last_t["open"])
        last_high = float(last_t["high"])
        last_low = float(last_t["low"])
        last_range = float(last_t.get("range", 0.0) or 0.0)
        last_range_atr = last_range / atr_value
        close_zone = float(self.config.get("strategy", {}).get("lebprim_momentum_close_zone_pct", 0.25))
        body_min = float(self.config.get("strategy", {}).get("lebprim_momentum_body_ratio_min", 0.55))
        body_strict = float(self.config.get("strategy", {}).get("lebprim_momentum_body_ratio_strict", 0.62))
        vol_strong = float(self.config.get("strategy", {}).get("lebprim_volume_surge_strong_min", 1.15))
        vol_soft = float(self.config.get("strategy", {}).get("lebprim_volume_surge_soft_min", 1.00))
        max_trigger_range_atr = float(self.config.get("strategy", {}).get("lebprim_max_trigger_range_atr", 1.0))
        top_close = close_position_in_range(last_t) >= (1.0 - close_zone)
        bottom_close = close_position_in_range(last_t) <= close_zone
        high_break = last_high > float(prev_t["high"])
        low_break = last_low < float(prev_t["low"])

        if last_range_atr > max_trigger_range_atr:
            return None

        # Momentum candle requirements (from data analysis: body >= 55% key)
        is_bull_momentum = (
            last_close > last_open
            and last_body_ratio >= body_min
            and (last_close > float(prev_t["high"]) or (high_break and top_close and last_body_ratio >= body_strict))
            and last_range_atr >= 0.5  # not a tiny candle
        )
        is_bear_momentum = (
            last_close < last_open
            and last_body_ratio >= body_min
            and (last_close < float(prev_t["low"]) or (low_break and bottom_close and last_body_ratio >= body_strict))
            and last_range_atr >= 0.5
        )

        volume_surge = last_vol_ratio >= vol_strong or (last_vol_ratio >= vol_soft and last_range_atr >= 0.8)

        # Determine direction
        if is_bull_momentum and not is_bear_momentum:
            side = "LONG"
            breakout_direction = "LONG"
        elif is_bear_momentum and not is_bull_momentum:
            side = "SHORT"
            breakout_direction = "SHORT"
        else:
            return None  # conflicting or no momentum

        # ---------------------------------------------------------------
        # CONDITION C: BIAS AGREEMENT
        # ---------------------------------------------------------------
        bias_dir = bias.get("direction", "NONE")
        bias_score_val = float(bias.get("trend_score", 0.0))
        long_score = float(bias.get("long_score", 0.0))
        short_score = float(bias.get("short_score", 0.0))
        bias_delta = abs(long_score - short_score)
        ema50_slope = float(trend_df.iloc[-1].get("ema_50_slope_3", 0.0) or 0.0)
        ema50_slope_atr = ema50_slope / atr_value
        short_bias_delta_min = float(self.config.get("strategy", {}).get("lebprim_short_bias_delta_min", 25.0))
        short_slope_min_atr = float(self.config.get("strategy", {}).get("lebprim_short_ema_slope_min_atr", 0.05))

        if side == "SHORT" and ema50_slope > 0 and bias_delta < 25.0:
            return None
        if side == "SHORT" and (
            bias_dir != "SHORT"
            or ema50_slope_atr > -short_slope_min_atr
            or bias_delta < short_bias_delta_min
        ):
            return None

        # Standard agreement: same direction or neutral
        min_delta = float(self.config.get("strategy", {}).get("lebprim_min_bias_delta", 12.0))
        if bias_dir == "NONE" and abs(long_score - short_score) < min_delta:
            return None
        bias_aligned = bias_dir in {"NONE", side}

        # Exception: strong conviction counter-trend in breakout regime only
        if not bias_aligned:
            strong_counter = (
                bias_score_val > 78.0
                and bias_delta > 22.0
                and regime_name == "HIGH_VOLATILITY_BREAKOUT"
            )
            if not strong_counter:
                return None
            bias_aligned = True  # granted exception

        # ---------------------------------------------------------------
        # STRUCTURAL ANCHOR — swing high/low for SL invalidation
        # ---------------------------------------------------------------
        lookback_s = setup_df.tail(12).iloc[:-2]
        if side == "LONG":
            # SL anchor: lowest recent swing low
            anchor_sl = float(lookback_s["low"].min())
            # Entry value zone: between compression zone high and current close
            value_price = (zone_high + last_close) / 2.0
            structure_level = anchor_sl
        else:
            # SL anchor: highest recent swing high
            anchor_sl = float(lookback_s["high"].max())
            value_price = (zone_low + last_close) / 2.0
            structure_level = anchor_sl

        # Structural distance must be at least sl_atr_mult × ATR (dynamic SL floor)
        atr_median = max(float(setup_df.iloc[-1].get("atr_median_20") or atr_value), 1e-9)
        atr_ratio = atr_value / atr_median
        sl_mult = 2.5 if atr_ratio > 1.6 else (2.0 if atr_ratio > 1.3 else 1.5)
        structural_dist = abs(last_close - structure_level)
        if structural_dist < atr_value * sl_mult:
            if side == "LONG":
                structure_level = last_close - (atr_value * sl_mult)
            else:
                structure_level = last_close + (atr_value * sl_mult)

        stop_dist_price = abs(value_price - structure_level)
        max_stop_price = float(self.config.get("strategy", {}).get("lebprim_max_stop_distance_price", 6.5))
        max_stop_atr = float(self.config.get("strategy", {}).get("lebprim_max_stop_distance_atr", 1.75))
        if stop_dist_price > max_stop_price or (stop_dist_price / atr_value) > max_stop_atr:
            return None

        # ---------------------------------------------------------------
        # SCORING
        # ---------------------------------------------------------------
        setup_score = 0.0
        setup_score += 30.0  # base: compression confirmed
        setup_score += 20.0 if volume_surge else 10.0
        setup_score += 15.0 if last_body_ratio >= 0.60 else (8.0 if last_body_ratio >= 0.50 else 0.0)
        setup_score += 12.0 if bias_aligned and bias_dir == side else 8.0
        setup_score += 10.0 if hour_quality >= 88.0 else (5.0 if hour_quality >= 70.0 else 0.0)
        setup_score += 8.0 if compression_ratio <= lebprim_tight * 0.75 else 0.0  # extra tight
        setup_score += 5.0 if float(latest_s.get("adx", 0.0) or 0.0) >= 18 else 0.0

        setup_valid = (
            setup_score >= 55.0
            and volume_surge
            and last_body_ratio >= 0.50
            and bias_aligned
        )

        anchor_time = pd.to_datetime(last_t["time"], utc=True).to_pydatetime().isoformat()
        fingerprint = f"XAUUSD|{side}|LEBPRIM_SCALP|{anchor_time}|{normalize_price(structure_level, 2)}"

        session_live_allowed = bool(session.get("session_live_allowed", False)) and hour_allowed
        observation_only = not (
            bool(family_cfg.get("live_allowed", True))
            and session_live_allowed
            and regime_name not in {"NO_TRADE"}
        )
        live_block = None if not observation_only else "session_or_regime_blocked"

        return {
            "setup_valid": setup_valid,
            "setup_family": self.FAMILY,
            "setup_subfam": "LEBPRIM_SCALP",
            "side": side,
            "setup_fingerprint": fingerprint,
            "anchor_time": anchor_time,
            "setup_price": normalize_price(last_close, 2),
            "value_price": normalize_price(value_price, 2),
            "structure_level": normalize_price(structure_level, 2),
            "setup_score": round(setup_score, 2),
            "trend_score": float(bias.get("trend_score", 0.0)),
            "regime_name": regime_name,
            "regime_confidence": float(regime.get("regime_confidence", 50.0)),
            "session_name": session.get("session_name", "UNKNOWN"),
            "session_live_allowed": session_live_allowed,
            "session_allowed": session_live_allowed,
            "session_quality_score": float(hour_quality),
            "session_quality_label": "lebprim_hour_gated",
            "regime_allowed": regime_name not in {"NO_TRADE"},
            "family_enabled": True,
            "family_live_allowed": bool(family_cfg.get("live_allowed", True)),
            "family_allowed_sessions": [],
            "family_allowed_regimes": ["TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT"],
            "effective_allowed_regimes": ["TREND_CONTINUATION", "HIGH_VOLATILITY_BREAKOUT"],
            "strategy_control": {},
            "observation_only": observation_only,
            "trend_alignment": bias_aligned,
            "bias_direction": str(bias_dir),
            "bias_confidence": float(bias.get("confidence", 0.0) or 0.0),
            "bias_long_score": float(long_score),
            "bias_short_score": float(short_score),
            "bias_delta": float(bias_delta),
            "ema_slope_atr": float(ema50_slope_atr),
            "live_block_reason": live_block,
            "trigger_type": "LEBPRIM_MOMENTUM",
            "atr_at_setup": float(atr_value),
            "reason_code": "setup_valid" if setup_valid else "setup_ineligible",
            "reason": f"LEBPRIM score={setup_score:.1f}",
            # LEBPRIM-specific extras
            "compression_ratio": round(compression_ratio, 3),
            "overlap_ratio": round(overlap, 3),
            "zone_high": round(zone_high, 2),
            "zone_low": round(zone_low, 2),
            "hour_utc": hour,
            "hour_quality": hour_quality,
            "volume_surge": volume_surge,
            "momentum_candle_body_ratio": round(last_body_ratio, 3),
            "origin_price": round(zone_high if side == "LONG" else zone_low, 2),
            "profit_headroom_atr": round(abs((float(recent_s["rolling_high_10"].max()) if side == "LONG" else float(recent_s["rolling_low_10"].min())) - value_price) / atr_value, 3),
            "price_extension_atr": round(abs(last_close - (zone_high if side == "LONG" else zone_low)) / atr_value, 3),
            "structure_quality_score": round(setup_score, 2),
        }

    # ------------------------------------------------------------------
    # Session / Bias / Regime (reused from StrategyEngine, simplified)
    # ------------------------------------------------------------------

    def _classify_session(self, now_utc: datetime) -> dict[str, Any]:
        sessions_cfg = self.config["sessions"]
        tz = pytz.timezone(str(sessions_cfg.get("timezone", "UTC")))
        local = now_utc.astimezone(tz)
        mins = local.hour * 60 + local.minute
        bucket = None
        for b in sessions_cfg.get("buckets", []):
            s = self._hhmm(str(b["start"]))
            e = self._hhmm(str(b["end"]))
            if s <= e:
                hit = s <= mins <= e
            else:
                hit = mins >= s or mins <= e
            if hit:
                bucket = b
                break
        if bucket is None:
            bucket = {"name": "UNCLASSIFIED", "live_allowed": False, "start": "", "end": ""}
        name = str(bucket["name"])
        hour = now_utc.hour
        hq, ha = LEBPRIM_HOUR_PROFILE.get(hour, (60.0, True))
        live_allowed = bool(bucket.get("live_allowed", False)) and ha
        block = None if live_allowed else ("lebprim_hour_blocked" if not ha else "session_bucket_live_disabled")
        return {
            "session_name": name,
            "session_live_allowed": live_allowed,
            "session_block_reason": block,
            "timezone": str(sessions_cfg.get("timezone", "UTC")),
            "local_time": local.isoformat(),
            "bucket": dict(bucket),
            "session_quality_score": hq,
            "session_quality_label": "lebprim_hour_gated",
        }

    def _analyze_bias(self, trend_df: pd.DataFrame, setup_df: pd.DataFrame) -> dict[str, Any]:
        required = ["ema_50", "ema_200", "atr", "ema_50_slope_3", "ema_50_slope_6"]
        if not indicators_are_valid(trend_df, required):
            return {"direction": "NONE", "trend_score": 0.0, "long_score": 0.0,
                    "short_score": 0.0, "confidence": 0.0, "reasons": []}

        t = trend_df.iloc[-1]
        s = setup_df.iloc[-1]
        recent = trend_df.tail(4)
        atr = max(float(t["atr"]), 1e-9)
        slope3 = float(t["ema_50_slope_3"]) / atr
        slope6 = float(t["ema_50_slope_6"]) / atr
        close = float(t["close"])
        e50 = float(t["ema_50"])
        e200 = float(t["ema_200"])
        se20 = float(s["ema_20"])
        se50 = float(s["ema_50"])

        ls, ss = 0.0, 0.0
        ls += 14.0 if close > e50 else 0.0
        ls += 14.0 if close > e200 else 0.0
        ls += 18.0 if e50 > e200 else 0.0
        ls += 10.0 if slope3 > 0.06 else 0.0
        ls += 10.0 if slope6 > 0.10 else 0.0
        ls += 8.0 if (recent["close"] > recent["ema_50"]).sum() >= 3 else 0.0
        ls += 12.0 if se20 > se50 else 0.0
        ls += 8.0 if float(s["close"]) > se20 else 0.0

        ss += 14.0 if close < e50 else 0.0
        ss += 14.0 if close < e200 else 0.0
        ss += 18.0 if e50 < e200 else 0.0
        ss += 10.0 if slope3 < -0.06 else 0.0
        ss += 10.0 if slope6 < -0.10 else 0.0
        ss += 8.0 if (recent["close"] < recent["ema_50"]).sum() >= 3 else 0.0
        ss += 12.0 if se20 < se50 else 0.0
        ss += 8.0 if float(s["close"]) < se20 else 0.0

        # LEBPRIM uses stricter bias thresholds (from config)
        min_score = float(self.config.get("strategy", {}).get("bias_min_score", 65.0))
        min_delta = float(self.config.get("strategy", {}).get("bias_min_delta", 18.0))

        direction = "LONG" if ls > ss else "SHORT"
        score = ls if direction == "LONG" else ss
        opp = ss if direction == "LONG" else ls
        confidence = max(0.0, min(100.0, score - opp + 50.0))
        if score < min_score or (score - opp) < min_delta:
            direction = "NONE"

        return {
            "direction": direction,
            "trend_score": round(score, 2),
            "long_score": round(ls, 2),
            "short_score": round(ss, 2),
            "confidence": round(confidence, 2),
            "reasons": [],
        }

    def _classify_regime(
        self,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
        session: dict[str, Any],
        bias: dict[str, Any],
    ) -> dict[str, Any]:
        required = ["ema_20", "ema_50", "atr", "atr_median_20"]
        if not indicators_are_valid(setup_df, required):
            return {"regime_name": "NO_TRADE", "regime_confidence": 0.0,
                    "bias": "NONE", "live_allowed": False,
                    "regime_block_reason": "indicator_unavailable", "metrics": {}}

        rc = self.config["regime"]
        ec = self.config["execution"]
        sl = setup_df.iloc[-1]
        sr = setup_df.tail(max(6, int(self.config["indicators"].get("regime_overlap_lookback", 6))))
        tr = trigger_df.tail(6)
        sq = float(session.get("session_quality_score", 60.0))

        atr_cur = max(float(sl["atr"]), 1e-9)
        atr_med = max(float(sl.get("atr_median_20") or atr_cur), 1e-9)
        atr_ratio = atr_cur / atr_med
        eff = self._dir_efficiency(setup_df["close"], int(self.config["indicators"].get("regime_efficiency_lookback", 8)))
        ovlp = self._overlap_ratio(sr)
        ema_dist = abs(float(sl["ema_20"]) - float(sl["ema_50"])) / atr_cur
        ema_slope = abs(float(sl.get("ema_20_slope_2") or 0.0)) / atr_cur
        dist_val = abs(float(sl["close"]) - float(sl["ema_20"])) / atr_cur
        spread_ratio = float(spread_points) / max(float(ec.get("max_spread_points", 25.0)), 1e-9)
        tr_rng = float(tr.iloc[-1]["range"]) / atr_cur if not tr.empty else 0.0

        name = "RANGE_MEAN_REVERSION"
        conf = 50.0
        block = None
        live_allowed = bool(rc.get("allow_range_regime", True))

        low_q = sq < 55.0 and spread_ratio > 0.92
        if low_q and atr_ratio <= float(rc["low_volatility_atr_ratio"]):
            name, conf, block, live_allowed = "NO_TRADE", 82.0, "low_quality_low_vol", False
        elif atr_ratio >= float(rc["high_volatility_atr_ratio"]) or (spread_ratio >= float(rc["spread_instability_ratio"]) and atr_ratio >= 1.05):
            name = "HIGH_VOLATILITY_BREAKOUT"
            conf = min(96.0, 62.0 + atr_ratio * 8.0 + sq * 0.1)
            block = None if atr_ratio <= float(rc["high_volatility_atr_ratio"]) + 0.35 else "extreme_volatility"
            live_allowed = True
        elif bias.get("direction") != "NONE" and eff >= float(rc["trend_efficiency_min"]) and ovlp < float(rc["overlap_chop_min"]) and ema_dist >= float(rc["ema_alignment_min_atr"]) and ema_slope >= float(rc["ema_slope_min_atr"]):
            name, conf, block, live_allowed = "TREND_CONTINUATION", min(96.0, 56.0 + float(bias.get("confidence", 0.0)) * 0.35 + eff * 42.0 + sq * 0.05), None, True
        elif dist_val >= float(rc["distance_from_value_extreme_atr"]) and tr_rng > 0.9:
            name, conf, block, live_allowed = "HIGH_VOLATILITY_BREAKOUT", 76.0, "price_expanded", True

        if name == "RANGE_MEAN_REVERSION":
            live_allowed = bool(rc.get("allow_range_regime", True))
            if not live_allowed:
                block = "range_regime_disabled"
        if name == "NO_TRADE":
            live_allowed = False

        return {
            "regime_name": name,
            "regime_confidence": round(conf, 2),
            "bias": bias.get("direction", "NONE"),
            "live_allowed": live_allowed,
            "regime_block_reason": block,
            "metrics": {
                "atr_ratio": round(atr_ratio, 3),
                "efficiency": round(eff, 3),
                "overlap_ratio": round(ovlp, 3),
                "ema_distance_atr": round(ema_dist, 3),
                "ema_slope_atr": round(ema_slope, 3),
                "spread_ratio": round(spread_ratio, 3),
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _augment(self, df: pd.DataFrame) -> pd.DataFrame:
        f = df.copy()
        f["body"] = f.apply(candle_body, axis=1)
        f["range"] = f.apply(candle_range, axis=1)
        f["close_location"] = f.apply(close_position_in_range, axis=1)
        f["lower_wick"] = f.apply(lower_wick, axis=1)
        f["upper_wick"] = f.apply(upper_wick, axis=1)
        f["body_ratio"] = f["body"] / f["range"].replace(0, 1e-9)
        return f

    def _log_slice_diagnostics(
        self,
        category: str,
        trend_df: pd.DataFrame | None,
        setup_df: pd.DataFrame | None,
        trigger_df: pd.DataFrame | None,
        *,
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
        }
        if extra:
            payload.update(extra)
        self._logger.log(level, "strategy_slice %s", payload)

    def _overlap_ratio(self, frame: pd.DataFrame) -> float:
        if len(frame) < 2:
            return 0.0
        overlaps = []
        for i in range(1, len(frame)):
            p, c = frame.iloc[i - 1], frame.iloc[i]
            ol = max(0.0, min(float(p["high"]), float(c["high"])) - max(float(p["low"]), float(c["low"])))
            ref = max(float(p.get("range", 1.0) or 1.0), float(c.get("range", 1.0) or 1.0), 1e-9)
            overlaps.append(ol / ref)
        return sum(overlaps) / max(len(overlaps), 1)

    def _dir_efficiency(self, series: pd.Series, lookback: int) -> float:
        w = series.tail(max(lookback, 2))
        if len(w) < 2:
            return 0.0
        net = abs(float(w.iloc[-1]) - float(w.iloc[0]))
        traveled = float(w.diff().abs().sum())
        return net / traveled if traveled > 0 else 0.0

    @staticmethod
    def _hhmm(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    @staticmethod
    def _extract_hour_utc(ts_str: str) -> int:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.hour
        except Exception:
            return 12  # safe default

    @staticmethod
    def _blocked(code: str, reason: str) -> dict[str, Any]:
        return {
            "valid": False, "live_ready": False, "dry_run_ready": False,
            "entry_mode": "observation_only", "reason_code": code,
            "reason": reason, "blocked_reasons": [code],
            "trigger_score": 0.0, "entry_score": 0.0,
            "trend_score": 0.0, "setup_score": 0.0,
        }

    # Keep same interface for setup_thresholds (used by backtest_runner)
    def setup_thresholds(self, live_profile: bool) -> dict[str, float]:
        return {
            "trend": 40.0,
            "setup": 55.0,
            "trigger": 45.0 if live_profile else 35.0,
            "entry": 52.0 if live_profile else 44.0,
        }
