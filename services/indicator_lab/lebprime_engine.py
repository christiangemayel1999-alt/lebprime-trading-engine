"""LebPrime Pine-derived candle engine."""

from __future__ import annotations

from services.indicator_lab.engine import IndicatorEngine
from services.indicator_lab.features import EVENT_MOMENTS, FeatureTracker, flatten_snapshot
from services.indicator_lab.models import Candle, ZoneRecord

LEBPRIME_INDICATOR_VERSION = "3.4.0"
MIN_DEPARTURE_PERCENT = 0.15
TP1_R = 1.0
TP2_R = 2.0
SL_SWING_PIVOT_LEN = 2
MAX_AFTER_ZONE_SWING_BARS = 60
EXECUTION_LIVE = "Live broker"
EXECUTION_PINE = "Pine indicator"
STOP_CLOSE = "Candle close (Pine)"
STOP_TOUCH = "Price touch"
LEG_SIZING_OWN = "Own stop distance"
LEG_SIZING_EQUAL = "Entry 1 distance (equal lots)"
LEG_RISK_WEIGHT = 0.5  # Entry 1 and Entry 2 each carry half of the configured risk, as in the live runtime.
FOREX_CURRENCIES = {
    "EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "DKK",
    "SGD", "HKD", "ZAR", "MXN", "TRY", "PLN", "CNH", "HUF", "CZK",
}


def is_forex_symbol(ticker: str) -> bool:
    """Approximate Pine's syminfo.type == "forex" from a broker ticker such as EURUSD or EURUSD.m."""
    letters = "".join(ch for ch in str(ticker or "").upper() if ch.isalpha())
    return len(letters) >= 6 and letters[:3] in FOREX_CURRENCIES and letters[3:6] in FOREX_CURRENCIES


class LebPrimeEngine(IndicatorEngine):
    """Candle-by-candle implementation of LebPrime Boundary 9 zone lifecycle."""

    indicator = "LEBPRIME"

    def __init__(self, parameters: dict | None = None, *, symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
        super().__init__(parameters, symbol=symbol, timeframe=timeframe)
        self._features = FeatureTracker()
        self._pending: list[dict] = []
        self._candidate_diagnostics: list[dict] = []
        self._candidate_diagnostics_by_id: dict[str, dict] = {}
        self._last_demand_candidate: int | None = None
        self._last_supply_candidate: int | None = None
        self._bull_leg = False
        self._bear_leg = False
        self._prev_bull_qual = False
        self._prev_bear_qual = False
        self._sequence = 0
        self._last_day = ""
        self._swing_highs: list[tuple[int, str, float]] = []
        self._swing_lows: list[tuple[int, str, float]] = []

    def _zone_id(self, demand: bool, bar: Candle) -> str:
        day = bar.timestamp[5:10].replace("-", "")
        side = "B" if demand else "S"
        tf = self.timeframe.replace("M", "") + "M" if self.timeframe.startswith("M") else self.timeframe
        prefix = "XAU" if "XAU" in self.symbol or "GOLD" in self.symbol else self.symbol
        return f"{prefix}-{tf}-{side}-{day}-LEB{self._sequence + 1:02d}"

    def _bar_at_offset(self, offset: int) -> Candle:
        return self._bars[-1 - int(offset)]

    def _is_momentum(self, bar: Candle, avg_body: float, demand: bool) -> bool:
        return (bar.close > bar.open if demand else bar.close < bar.open) and abs(bar.close - bar.open) >= avg_body * float(self.parameters["body_mult"])

    def _is_compression_offset(self, offset: int, avg_body: float, atr: float) -> bool:
        bar = self._bar_at_offset(offset)
        return abs(bar.close - bar.open) <= avg_body * float(self.parameters["compression_body_mult"]) or (bar.high - bar.low) <= atr * float(self.parameters["compact_range_atr"])

    def _compact_diagnostics(self, avg_body: float, atr: float) -> dict:
        start = int(self.parameters["momentum_count"])
        end = int(self.parameters["momentum_count"]) + int(self.parameters["compact_search_bars"]) - 1
        immediate_end = min(end, int(self.parameters["momentum_count"]) + int(self.parameters["immediate_base_bars"]) - 1)
        compact_end = start
        compact_count = 0
        still_inside_base = True
        candles = []
        body_threshold = avg_body * float(self.parameters["compression_body_mult"])
        range_threshold = atr * float(self.parameters["compact_range_atr"])
        for offset in range(start, end + 1):
            bar = self._bar_at_offset(offset)
            body = abs(bar.close - bar.open)
            rng = bar.high - bar.low
            compressed = body <= body_threshold or rng <= range_threshold
            if still_inside_base:
                if compressed:
                    compact_end = offset
                    compact_count += 1
                else:
                    still_inside_base = False
            candles.append(
                {
                    "offset": offset,
                    "bar_index": bar.index,
                    "timestamp": bar.timestamp,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "body": body,
                    "range": rng,
                    "body_threshold": body_threshold,
                    "range_threshold": range_threshold,
                    "is_compression": compressed,
                    "included": offset <= compact_end and compact_count >= int(self.parameters["compact_base_min_bars"]),
                }
            )
        fallback = compact_count < int(self.parameters["compact_base_min_bars"])
        selected_end = immediate_end if fallback else compact_end
        return {
            "start_offset": start,
            "end_offset": selected_end,
            "raw_compact_end_offset": compact_end,
            "immediate_end_offset": immediate_end,
            "compact_count": compact_count,
            "mode": "FALLBACK_IMMEDIATE" if fallback else "COMPACT",
            "candles": candles,
        }

    def _compact_end(self, avg_body: float, atr: float) -> int:
        return int(self._compact_diagnostics(avg_body, atr)["end_offset"])

    def _range_high(self, start: int, end: int) -> float:
        return max(self._bar_at_offset(offset).high for offset in range(start, end + 1))

    def _range_low(self, start: int, end: int) -> float:
        return min(self._bar_at_offset(offset).low for offset in range(start, end + 1))

    def _last_opposing(self, demand: bool, start: int, end: int) -> int:
        best = start
        for offset in range(start, end + 1):
            bar = self._bar_at_offset(offset)
            if bar.close < bar.open if demand else bar.close > bar.open:
                best = offset
                break
        return best

    def _smallest_range(self, start: int, end: int) -> int:
        best = start
        best_range = self._bar_at_offset(start).high - self._bar_at_offset(start).low
        for offset in range(start, end + 1):
            bar = self._bar_at_offset(offset)
            rng = bar.high - bar.low
            if rng < best_range:
                best = offset
                best_range = rng
        return best

    def _bounds(self, demand: bool, avg_body: float, atr: float) -> tuple[float, float, int, int, dict]:
        compact = self._compact_diagnostics(avg_body, atr)
        start = int(compact["start_offset"])
        end = int(compact["end_offset"])
        method = str(self.parameters["zone_refinement"])
        if not bool(self.parameters["use_refinement"]) or method == "Full Base (Boundary 9)":
            return self._range_high(start, end), self._range_low(start, end), end, end, compact
        if method == "Last Opposing Candle":
            off = self._last_opposing(demand, start, end)
            bar = self._bar_at_offset(off)
            return bar.high, bar.low, end, off, compact
        if method == "Extreme Wick + Tight Body":
            off = self._smallest_range(start, end)
            bar = self._bar_at_offset(off)
            if demand:
                return max(bar.open, bar.close), self._range_low(start, end), end, off, compact
            return self._range_high(start, end), min(bar.open, bar.close), end, off, compact
        off = self._smallest_range(start, end)
        bar = self._bar_at_offset(off)
        return bar.high, bar.low, end, off, compact

    def _valid_width(self, width: float, atr: float) -> bool:
        return width >= atr * float(self.parameters["min_zone_width_atr"])

    def _max_width_allowed(self, atr: float) -> float:
        return atr * float(self.parameters["max_zone_width_atr"])

    def _min_width_required(self, atr: float) -> float:
        return atr * float(self.parameters["min_zone_width_atr"])

    def _clamp_zone_bounds(self, top: float, bottom: float, atr: float) -> tuple[float, float]:
        max_width = atr * float(self.parameters["max_zone_width_atr"])
        width = top - bottom
        if width > max_width:
            excess = width - max_width
            top -= excess / 2.0
            bottom += excess / 2.0
        return top, bottom

    def _has_overlap(self, demand: bool, top: float, bottom: float) -> bool:
        if bool(self.parameters["allow_overlap"]):
            return False
        tol = float(self.parameters["overlap_tolerance"])
        side = "DEMAND" if demand else "SUPPLY"
        for zone in self._zones:
            if zone.side == side and zone.terminal_state is None:
                if top >= zone.zone_bottom - tol and bottom <= zone.zone_top + tol:
                    return True
        return False

    def _buffer(self) -> float:
        ticker = self.symbol.upper()
        for idx in range(1, 7):
            custom_symbol = str(self.parameters.get(f"custom_symbol{idx}", "") or "").upper()
            if custom_symbol and (ticker == custom_symbol or custom_symbol in ticker):
                return float(self.parameters.get(f"custom_buffer{idx}", 0.0) or 0.0)
        if "XAU" in ticker or "GOLD" in ticker:
            return float(self.parameters["gold_buffer"])
        if "XAG" in ticker or "SILVER" in ticker:
            return float(self.parameters["silver_buffer"])
        if "US30" in ticker or "DJI" in ticker:
            return float(self.parameters["us30_buffer"])
        if "NAS" in ticker or "USTEC" in ticker or "US100" in ticker:
            return float(self.parameters["nas_buffer"])
        if "WTI" in ticker or "USOIL" in ticker:
            return float(self.parameters["wti_buffer"])
        if is_forex_symbol(ticker):
            return float(self.parameters["forex_jpy_buffer"] if "JPY" in ticker else self.parameters["forex_buffer"])
        return float(self.parameters["fallback_buffer"])

    def _first_swing_low_after_zone(self, source_bar_index: int, fallback: float) -> tuple[float, int | None, str | None]:
        for bar_index, timestamp, value in self._swing_lows:
            if bar_index > source_bar_index and bar_index - source_bar_index <= MAX_AFTER_ZONE_SWING_BARS:
                return value, bar_index, timestamp
        return fallback, None, None

    def _first_swing_high_after_zone(self, source_bar_index: int, fallback: float) -> tuple[float, int | None, str | None]:
        for bar_index, timestamp, value in self._swing_highs:
            if bar_index > source_bar_index and bar_index - source_bar_index <= MAX_AFTER_ZONE_SWING_BARS:
                return value, bar_index, timestamp
        return fallback, None, None

    def _structural_stop(self, demand: bool, top: float, bottom: float, source_bar_index: int) -> tuple[float, dict]:
        buffer = self._buffer()
        if demand:
            swing, swing_bar, swing_time = self._first_swing_low_after_zone(source_bar_index, bottom)
            stop = min(swing, bottom) - buffer
            kind = "LOW"
        else:
            swing, swing_bar, swing_time = self._first_swing_high_after_zone(source_bar_index, top)
            stop = max(swing, top) + buffer
            kind = "HIGH"
        return stop, {"structural_swing_price": swing, "structural_swing_bar_index": swing_bar, "structural_swing_time": swing_time, "structural_swing_kind": kind}

    def _entry_stop_target(self, demand: bool, top: float, bottom: float, source_bar_index: int) -> tuple[float, float, float, float, dict]:
        entry = top if demand else bottom
        stop, swing = self._structural_stop(demand, top, bottom, source_bar_index)
        risk = abs(entry - stop)
        tp1_r, tp2_r = self._tp_multiples()
        tp1 = entry + risk * tp1_r if demand else entry - risk * tp1_r
        tp2 = entry + risk * tp2_r if demand else entry - risk * tp2_r
        return entry, stop, tp1, tp2, swing

    @staticmethod
    def _body_breaks_zone(bar: Candle, demand: bool, top: float, bottom: float) -> bool:
        return min(bar.open, bar.close) < bottom if demand else max(bar.open, bar.close) > top

    def _post_limit_invalidated(self, bar: Candle, demand: bool, top: float, bottom: float) -> bool:
        """Pine ``invalidAfterCreated``: after LIMIT CREATED, Close mode cancels on the close, not the body."""
        if self.parameters["invalidation"] == "Wick":
            return bar.low < bottom if demand else bar.high > top
        return bar.close < bottom if demand else bar.close > top

    def _pre_limit_invalidated(self, bar: Candle, demand: bool, top: float, bottom: float) -> bool:
        wick_break = bar.low < bottom if demand else bar.high > top
        return wick_break if self.parameters["invalidation"] == "Wick" else self._body_breaks_zone(bar, demand, top, bottom)

    @staticmethod
    def _stop_confirmed(bar: Candle, demand: bool, stop_level: float) -> bool:
        body_top = max(bar.open, bar.close)
        body_bottom = min(bar.open, bar.close)
        body_size = body_top - body_bottom
        if body_size <= 0.0:
            return False
        if demand:
            body_beyond = max(0.0, min(body_top, stop_level) - body_bottom)
            return bar.close < stop_level and body_beyond >= body_size * 0.5
        body_beyond = max(0.0, body_top - max(body_bottom, stop_level))
        return bar.close > stop_level and body_beyond >= body_size * 0.5

    @staticmethod
    def _departure_ready(bar: Candle, demand: bool, top: float, bottom: float) -> bool:
        reference = top if demand else bottom
        distance_percent = abs(bar.close - reference) / abs(bar.close) * 100.0 if bar.close else 0.0
        outside = bar.low > top and bar.close > top if demand else bar.high < bottom and bar.close < bottom
        return outside and distance_percent >= MIN_DEPARTURE_PERCENT

    @staticmethod
    def _departure_diagnostics(bar: Candle, demand: bool, top: float, bottom: float) -> dict:
        reference = top if demand else bottom
        departure_percent = abs(bar.close - reference) / abs(bar.close) * 100.0 if bar.close else 0.0
        completely_outside = bar.low > top and bar.close > top if demand else bar.high < bottom and bar.close < bottom
        return {
            "reference_edge": reference,
            "departure_percent": departure_percent,
            "completely_outside": completely_outside,
            "departure_ready": completely_outside and departure_percent >= MIN_DEPARTURE_PERCENT,
        }

    def _new_candidate_diagnostic(
        self,
        demand: bool,
        raw_top: float,
        raw_bottom: float,
        top: float,
        bottom: float,
        source_index: int,
        target_offset: int,
        compact: dict,
        zone_id: str,
        bar: Candle,
        atr: float,
        context: dict,
        *,
        same_side_spacing_ok: bool,
        initial_overlap_ok: bool,
        width_ok: bool,
    ) -> dict:
        source = self._bars[source_index]
        target = self._bar_at_offset(target_offset)
        delay = int(self.parameters["zone_confirm_delay"])
        due_bar = bar.index + delay
        due_time = bar.timestamp if delay == 0 else None
        item = {
            "candidate_id": zone_id,
            "side": "BUY" if demand else "SELL",
            "candidate_bar_index": bar.index,
            "candidate_time": bar.timestamp,
            "source_bar_index": source.index,
            "source_time": source.timestamp,
            "raw_zone_top": raw_top,
            "raw_zone_bottom": raw_bottom,
            "clamped_zone_top": top,
            "clamped_zone_bottom": bottom,
            "zone_width": top - bottom,
            "ATR": atr,
            "min_width_required": self._min_width_required(atr),
            "max_width_allowed": self._max_width_allowed(atr),
            "compact_base_start": context.get("base_start_bar_index"),
            "compact_base_end": context.get("base_end_bar_index"),
            "compact_base_start_offset": int(compact["start_offset"]),
            "compact_base_end_offset": int(compact["end_offset"]),
            "refinement_method": str(self.parameters["zone_refinement"]),
            "refined_bar_index": target.index,
            "refined_bar_time": target.timestamp,
            "same_side_spacing_ok": same_side_spacing_ok,
            "initial_overlap_ok": initial_overlap_ok,
            "confirmation_due_bar": due_bar,
            "confirmation_due_time": due_time,
            "width_ok": width_ok,
            "pending_bars_json": [],
            "outcome": None,
            "outcome_reason": None,
            "limit_created_bar_index": None,
            "limit_created_time": None,
            "zone_created": False,
            "limit_created": False,
        }
        self._candidate_diagnostics.append(item)
        self._candidate_diagnostics_by_id[zone_id] = item
        return item

    def _candidate_pending_row(self, pending: dict, bar: Candle) -> dict:
        demand = bool(pending["demand"])
        top = float(pending["top"])
        bottom = float(pending["bottom"])
        source = self._bars[int(pending["source_index"])]
        body_invalidated = self._body_breaks_zone(bar, demand, top, bottom)
        wick_invalidated = bar.low < bottom if demand else bar.high > top
        departure = self._departure_diagnostics(bar, demand, top, bottom)
        old_enough = bar.index - source.index >= int(self.parameters["min_bars_after_source"])
        return {
            "current_bar_time": bar.timestamp,
            "current_bar_index": bar.index,
            "source_age_bars": bar.index - source.index,
            "body_invalidated": body_invalidated,
            "wick_invalidated": wick_invalidated,
            "final_pre_limit_invalidated": self._pre_limit_invalidated(bar, demand, top, bottom),
            "reference_edge": departure["reference_edge"],
            "close": bar.close,
            "high": bar.high,
            "low": bar.low,
            "departure_percent": departure["departure_percent"],
            "completely_outside": departure["completely_outside"],
            "departure_ready": departure["departure_ready"],
            "old_enough": old_enough,
            "confirmation_complete": bar.index >= int(pending["due"]),
            "overlap_at_creation": not bool((pending.get("diagnostic") or {}).get("initial_overlap_ok", True)),
            "live_capacity_available": sum(1 for zone in self._zones if zone.side == ("DEMAND" if demand else "SUPPLY") and zone.terminal_state is None) < int(self.parameters["max_zones"]),
        }

    @staticmethod
    def _set_candidate_outcome(diagnostic: dict | None, outcome: str, reason: str | None = None, **updates: object) -> None:
        if not diagnostic or diagnostic.get("outcome"):
            return
        diagnostic["outcome"] = outcome
        diagnostic["outcome_reason"] = reason or outcome
        diagnostic.update(updates)

    def _limit_creation_ready(self, bar: Candle, demand: bool, top: float, bottom: float, source_index: int) -> bool:
        source = self._bars[source_index]
        return bar.index - source.index >= int(self.parameters["min_bars_after_source"]) and self._departure_ready(bar, demand, top, bottom)

    def _update_swings(self) -> None:
        if len(self._bars) < SL_SWING_PIVOT_LEN * 2 + 1:
            return
        pivot_pos = len(self._bars) - 1 - SL_SWING_PIVOT_LEN
        pivot = self._bars[pivot_pos]
        left = self._bars[pivot_pos - SL_SWING_PIVOT_LEN:pivot_pos]
        right = self._bars[pivot_pos + 1:pivot_pos + 1 + SL_SWING_PIVOT_LEN]
        if all(pivot.high > item.high for item in left + right):
            self._swing_highs.append((pivot.index, pivot.timestamp, pivot.high))
            self._swing_highs = self._swing_highs[-300:]
        if all(pivot.low < item.low for item in left + right):
            self._swing_lows.append((pivot.index, pivot.timestamp, pivot.low))
            self._swing_lows = self._swing_lows[-300:]

    def _momentum_candles(self, demand: bool, avg_body: float) -> list[dict]:
        body_threshold = avg_body * float(self.parameters["body_mult"])
        candles = []
        for offset in range(int(self.parameters["momentum_count"]) - 1, -1, -1):
            item = self._bar_at_offset(offset)
            body = abs(item.close - item.open)
            candles.append(
                {
                    "offset": offset,
                    "bar_index": item.index,
                    "timestamp": item.timestamp,
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "close": item.close,
                    "body": body,
                    "body_threshold": body_threshold,
                    "qualifies": self._is_momentum(item, avg_body, demand),
                }
            )
        return candles

    def _zone_context(self, demand: bool, candidate: Candle, source_offset: int, target_offset: int, compact: dict, avg_body: float, atr: float, bull_count: int, bear_count: int, overlap_detected: bool) -> dict:
        source = self._bar_at_offset(source_offset)
        target = self._bar_at_offset(target_offset)
        start_bar = self._bar_at_offset(int(compact["start_offset"]))
        end_bar = self._bar_at_offset(int(compact["end_offset"]))
        candidate_body = abs(candidate.close - candidate.open)
        body_threshold = avg_body * float(self.parameters["body_mult"])
        base_bars = abs(int(compact["end_offset"]) - int(compact["start_offset"])) + 1
        compact_body_ratio = None
        compact_range_ratio = None
        compact_candles = [item for item in compact["candles"] if int(item["offset"]) <= int(compact["end_offset"])]
        if compact_candles:
            max_body = max(float(item["body"]) for item in compact_candles)
            max_range = max(float(item["range"]) for item in compact_candles)
            compact_body_ratio = max_body / avg_body if avg_body else None
            compact_range_ratio = max_range / atr if atr else None
        return {
            "same_direction_count": bull_count if demand else bear_count,
            "bull_count": bull_count,
            "bear_count": bear_count,
            "body_mult_actual": candidate_body,
            "body_mult_threshold": body_threshold,
            "body_threshold": body_threshold,
            "candidate_open": candidate.open,
            "candidate_high": candidate.high,
            "candidate_low": candidate.low,
            "candidate_close": candidate.close,
            "candidate_body": candidate_body,
            "candidate_range": candidate.high - candidate.low,
            "atr_at_candidate": atr,
            "average_body_at_candidate": avg_body,
            "momentum_body": candidate_body,
            "momentum_body_ratio": candidate_body / body_threshold if body_threshold else None,
            "momentum_candles": self._momentum_candles(demand, avg_body),
            "compact_base_candles": compact["candles"],
            "compact_base_start_offset": int(compact["start_offset"]),
            "compact_base_end_offset": int(compact["end_offset"]),
            "compact_base_start_bar_index": start_bar.index,
            "compact_base_end_bar_index": end_bar.index,
            "compact_base_start_time": start_bar.timestamp,
            "compact_base_end_time": end_bar.timestamp,
            "base_start_bar_index": start_bar.index,
            "base_end_bar_index": end_bar.index,
            "base_start_time": start_bar.timestamp,
            "base_end_time": end_bar.timestamp,
            "base_bar_count": base_bars,
            "base_detection_mode": compact["mode"],
            "compact_count": int(compact["compact_count"]),
            "compact_body_ratio": compact_body_ratio,
            "compact_range_atr_ratio": compact_range_ratio,
            "refinement_target_offset": int(target_offset),
            "refined_bar_index": target.index,
            "refined_time": target.timestamp,
            "refined_open": target.open,
            "refined_high": target.high,
            "refined_low": target.low,
            "refined_close": target.close,
            "refined_range": target.high - target.low,
            "source_anchor_offset": int(source_offset),
            "source_anchor_bar_index": source.index,
            "source_anchor_time": source.timestamp,
            "compact_search_bars_used": self.parameters["compact_search_bars"],
            "immediate_base_bars_used": self.parameters["immediate_base_bars"],
            "compact_base_min_bars_required": self.parameters["compact_base_min_bars"],
            "overlap_detected": overlap_detected,
            "overlap_decision": "ACCEPTED" if overlap_detected and bool(self.parameters["allow_overlap"]) else ("REJECTED" if overlap_detected else "NONE"),
        }

    def _create_zone(self, demand: bool, top: float, bottom: float, source_index: int, zone_id: str, bar: Candle, start_armed: bool, atr: float, avg_body: float | None = None, context: dict | None = None, candidate: Candle | None = None) -> bool:
        diagnostic = (context or {}).get("candidate_diagnostic")
        context = {key: value for key, value in (context or {}).items() if key != "candidate_diagnostic"}
        live_count = sum(1 for zone in self._zones if zone.side == ("DEMAND" if demand else "SUPPLY") and zone.terminal_state is None)
        if top <= bottom or live_count >= int(self.parameters["max_zones"]):
            self._emit_orphan(zone_id, bar, "ZONE_REJECTED", reason="invalid_bounds_or_capacity")
            self._set_candidate_outcome(diagnostic, "CAPACITY_REJECTED" if live_count >= int(self.parameters["max_zones"]) else "OTHER", "invalid_bounds_or_capacity")
            return False
        if not self._valid_width(top - bottom, atr):
            self._emit_orphan(zone_id, bar, "ZONE_REJECTED", reason="width_filter", metadata={"width": top - bottom, "atr": atr})
            self._set_candidate_outcome(diagnostic, "WIDTH_REJECTED", "width_filter")
            return False
        source = self._bars[source_index]
        entry, stop, tp1, tp2, swing = self._entry_stop_target(demand, top, bottom, source.index)
        candidate = candidate or bar
        buffer = self._buffer()
        risk = abs(entry - stop)
        metadata = {
            "pine_refinement": self.parameters["zone_refinement"],
            "momentum_direction": "DEMAND" if demand else "SUPPLY",
            "zone_confirm_delay_used": self.parameters["zone_confirm_delay"],
            "min_same_side_gap_used": self.parameters["min_same_side_gap"],
            "min_bars_after_source_used": self.parameters["min_bars_after_source"],
            "max_zone_width_atr_used": self.parameters["max_zone_width_atr"],
            "min_zone_width_atr_used": self.parameters["min_zone_width_atr"],
            "allow_overlap_used": self.parameters["allow_overlap"],
            "overlap_tolerance_used": self.parameters["overlap_tolerance"],
            "use_refinement": self.parameters["use_refinement"],
            "zone_refinement_method": self.parameters["zone_refinement"],
            "original_zone_top": top,
            "original_zone_bottom": bottom,
            "refined_zone_top": top,
            "refined_zone_bottom": bottom,
            "invalidation_method": self.parameters["invalidation"],
            "retest_cooldown_used": self.parameters["retest_cooldown"],
            "buffer_used": buffer,
            "tp1_r_used": self._tp_multiples()[0],
            "tp2_r_used": self._tp_multiples()[1],
            "indicator_version": LEBPRIME_INDICATOR_VERSION,
            **context,
            **swing,
        }
        zone = ZoneRecord(
            engine_zone_id=zone_id,
            side="DEMAND" if demand else "SUPPLY",
            source_bar_index=source.index,
            source_time=source.timestamp,
            zone_top=top,
            zone_bottom=bottom,
            created_bar_index=bar.index,
            created_time=bar.timestamp,
            symbol=self.symbol,
            timeframe=self.timeframe,
            zone_type="LEBPRIME_BOUNDARY_9",
            indicator_version=LEBPRIME_INDICATOR_VERSION,
            source_open=source.open,
            source_high=source.high,
            source_low=source.low,
            source_close=source.close,
            source_body=abs(source.close - source.open),
            source_range=source.high - source.low,
            candidate_bar_index=candidate.index,
            candidate_time=candidate.timestamp,
            candidate_open=context.get("candidate_open"),
            candidate_high=context.get("candidate_high"),
            candidate_low=context.get("candidate_low"),
            candidate_close=context.get("candidate_close"),
            candidate_body=context.get("candidate_body"),
            candidate_range=context.get("candidate_range"),
            atr_at_candidate=context.get("atr_at_candidate"),
            average_body_at_candidate=context.get("average_body_at_candidate"),
            body_threshold=context.get("body_threshold"),
            bull_count=context.get("bull_count"),
            bear_count=context.get("bear_count"),
            compact_base_start_offset=context.get("compact_base_start_offset"),
            compact_base_end_offset=context.get("compact_base_end_offset"),
            base_start_bar_index=context.get("base_start_bar_index"),
            base_end_bar_index=context.get("base_end_bar_index"),
            base_start_time=context.get("base_start_time"),
            base_end_time=context.get("base_end_time"),
            base_bar_count=context.get("base_bar_count"),
            refinement_target_offset=context.get("refinement_target_offset"),
            refined_bar_index=context.get("refined_bar_index", source.index),
            refined_time=context.get("refined_time", source.timestamp),
            refined_open=context.get("refined_open"),
            refined_high=context.get("refined_high"),
            refined_low=context.get("refined_low"),
            refined_close=context.get("refined_close"),
            refined_range=context.get("refined_range"),
            refinement_method=str(self.parameters["zone_refinement"]),
            zone_mid=(top + bottom) / 2.0,
            zone_width_price=top - bottom,
            zone_width_points=top - bottom,
            zone_width_atr=(top - bottom) / atr if atr else None,
            atr_at_source=atr,
            average_body_at_source=avg_body,
            momentum_body=context.get("momentum_body"),
            momentum_body_ratio=context.get("momentum_body_ratio"),
            momentum_count=int(self.parameters["momentum_count"]),
            momentum_span=int(self.parameters["momentum_count"]),
            compact_body_ratio=context.get("compact_body_ratio"),
            compact_range_atr_ratio=context.get("compact_range_atr_ratio"),
            confirmed_bar_index=bar.index,
            confirmed_time=bar.timestamp,
            bars_source_to_candidate=candidate.index - source.index,
            bars_candidate_to_created=bar.index - candidate.index,
            entry1=entry,
            entry2=bottom if demand else top,
            entry_price=entry,
            sl_price=stop,
            structural_swing_bar_index=swing.get("structural_swing_bar_index"),
            structural_swing_time=swing.get("structural_swing_time"),
            structural_swing_price=swing.get("structural_swing_price"),
            structural_swing_kind=swing.get("structural_swing_kind"),
            tp1_price=tp1,
            tp2_price=tp2,
            tp_price=tp2,
            risk_distance=risk,
            reward_distance=abs(tp2 - entry),
            rr=self._tp_multiples()[1],
            buffer_used=buffer,
            creation_reason="momentum_boundary_zone",
            metadata=metadata,
        )
        self._zones.append(zone)
        self._emit(zone, bar, "ZONE_CREATED")
        if start_armed:
            boundary = top if demand else bottom
            distance = abs(bar.close - boundary)
            zone.armed_bar_index = bar.index
            zone.armed_time = bar.timestamp
            zone.limit_created_bar_index = bar.index
            zone.limit_created_time = bar.timestamp
            zone.departure_price = bar.close
            zone.departure_close = bar.close
            zone.departure_boundary = boundary
            zone.departure_distance = distance
            zone.departure_distance_price = distance
            zone.departure_distance_atr = distance / atr if atr else None
            self._emit(zone, bar, "LIMIT_CREATED", price=entry)
            self._set_candidate_outcome(
                diagnostic,
                "LIMIT_CREATED",
                "limit_created",
                zone_created=True,
                limit_created=True,
                limit_created_bar_index=bar.index,
                limit_created_time=bar.timestamp,
            )
        return True

    def _queue_or_create(self, demand: bool, top: float, bottom: float, source_index: int, zone_id: str, bar: Candle, atr: float, avg_body: float | None = None, context: dict | None = None, diagnostic: dict | None = None) -> bool:
        delay = int(self.parameters["zone_confirm_delay"])
        if context is not None:
            # Market values when the candidate was found; copied onto the zone if it becomes a LIMIT.
            context.update(flatten_snapshot("candidate", self._features.snapshot()))
        self._pending.append({"demand": demand, "top": top, "bottom": bottom, "source_index": source_index, "due": bar.index + delay, "id": zone_id, "avg_body": avg_body, "context": context or {}, "candidate": bar, "diagnostic": diagnostic})
        return True

    def _process_pending(self, bar: Candle, atr: float) -> None:
        # Pine walks the hidden candidates from newest to oldest; order matters when two
        # overlapping candidates become ready on the same bar.
        remaining: list[dict] = []
        for pending in reversed(self._pending):
            demand = bool(pending["demand"])
            diagnostic = pending.get("diagnostic")
            pending_row = self._candidate_pending_row(pending, bar)
            if diagnostic is not None:
                diagnostic["confirmation_due_time"] = diagnostic.get("confirmation_due_time") or (bar.timestamp if bar.index >= int(pending["due"]) else None)
                diagnostic["pending_bars_json"].append(pending_row)
            invalid = self._pre_limit_invalidated(bar, demand, float(pending["top"]), float(pending["bottom"]))
            if invalid:
                self._set_candidate_outcome(diagnostic, "PRE_LIMIT_INVALIDATED", "pre_limit_invalidated")
            elif bar.index >= int(pending["due"]) and self._limit_creation_ready(bar, demand, float(pending["top"]), float(pending["bottom"]), int(pending["source_index"])):
                filter_reason = self._limit_filter_reason(demand, float(pending["top"]), float(pending["bottom"]), int(pending["source_index"]))
                if filter_reason is not None:
                    self._set_candidate_outcome(diagnostic, "FILTERED", filter_reason)
                elif not self._has_overlap(demand, float(pending["top"]), float(pending["bottom"])):
                    self._create_zone(demand, pending["top"], pending["bottom"], pending["source_index"], pending["id"], bar, True, atr, pending.get("avg_body"), pending.get("context"), candidate=pending.get("candidate"))
                else:
                    self._set_candidate_outcome(diagnostic, "OVERLAP_REJECTED", "overlap_at_limit_creation")
            else:
                remaining.append(pending)
        self._pending = list(reversed(remaining))

    def finalize(self) -> dict:
        for pending in self._pending:
            diagnostic = pending.get("diagnostic")
            if not diagnostic or diagnostic.get("outcome"):
                continue
            rows = diagnostic.get("pending_bars_json") or []
            waited_for_departure = any(row.get("confirmation_complete") and row.get("old_enough") for row in rows)
            self._set_candidate_outcome(diagnostic, "NEVER_DEPARTED" if waited_for_departure else "END_OF_DATA_PENDING", "end_of_data")
        result = super().finalize()
        result["candidate_diagnostics"] = [dict(item) for item in self._candidate_diagnostics]
        return result

    # ------------------------------------------------------------------
    # Live broker execution model
    #
    # Mirrors what the MT5 runtime actually does with a LebPrime signal:
    #   * Entry 1 / Entry 2 are resting LIMIT orders: any touch after the
    #     LIMIT CREATED candle fills them (at the open if price gaps through).
    #   * SL and TP2 are broker-side levels that trigger on a touch, including
    #     on the candle that fills the entry.
    #   * When SL and a target are both inside one candle, SL is assumed first.
    #   * TP1 and the post-TP1 return to Entry 1 are acted on by the bot at
    #     candle close, so those exits use the candle close price.
    #   * Each entry carries half of the configured risk, so results are in R
    #     of the whole signal (both entries stopped = -1R).
    # ------------------------------------------------------------------

    def _live_mode(self) -> bool:
        return str(self.parameters.get("execution_model", EXECUTION_LIVE)) == EXECUTION_LIVE

    @staticmethod
    def _live_legs(zone: ZoneRecord) -> dict[str, dict]:
        legs = zone.metadata.get("live_legs")
        if not legs:
            legs = {
                "1": {"planned_price": float(zone.entry1 if zone.entry1 is not None else zone.entry_price), "filled": False, "fill_price": None, "fill_bar_index": None, "open_fraction": 0.0, "realized_r": 0.0, "canceled": False},
                "2": {"planned_price": float(zone.entry2), "filled": False, "fill_price": None, "fill_bar_index": None, "open_fraction": 0.0, "realized_r": 0.0, "canceled": False},
            }
            # A zone restored mid-lifecycle keeps the fills it already has.
            for leg_no, bar_index in (("1", zone.activated_bar_index), ("2", zone.entry2_activated_bar_index)):
                if bar_index is not None:
                    legs[leg_no].update({"filled": True, "fill_price": legs[leg_no]["planned_price"], "fill_bar_index": bar_index, "open_fraction": 1.0})
            zone.metadata["live_legs"] = legs
            zone.metadata["execution_model"] = EXECUTION_LIVE
        return legs

    @staticmethod
    def _limit_fill_price(bar: Candle, demand: bool, planned: float) -> float:
        if demand:
            return bar.open if bar.open < planned else planned
        return bar.open if bar.open > planned else planned

    @staticmethod
    def _stop_exit_price(bar: Candle, demand: bool, stop: float) -> float:
        if demand:
            return bar.open if bar.open < stop else stop
        return bar.open if bar.open > stop else stop

    @staticmethod
    def _target_exit_price(bar: Candle, demand: bool, target: float) -> float:
        if demand:
            return bar.open if bar.open > target else target
        return bar.open if bar.open < target else target

    def _fill_live_leg(self, zone: ZoneRecord, leg_no: str, bar: Candle, demand: bool) -> float:
        leg = self._live_legs(zone)[leg_no]
        fill = self._limit_fill_price(bar, demand, float(leg["planned_price"]))
        leg.update({"filled": True, "fill_price": fill, "fill_bar_index": bar.index, "open_fraction": 1.0})
        if leg_no == "1":
            zone.activated_bar_index = bar.index
            zone.activated_time = bar.timestamp
            zone.entry1_activated_bar_index = bar.index
            zone.entry1_activated_time = bar.timestamp
            zone.first_retest_bar_index = zone.first_retest_bar_index or bar.index
            zone.first_retest_time = zone.first_retest_time or bar.timestamp
            zone.retest_count = int(zone.retest_count or 0) + 1
            zone.activation_reason = "limit_touched"
            self._emit(zone, bar, "ENTRY1_ACTIVATED", price=fill)
        else:
            zone.entry2_activated_bar_index = bar.index
            zone.entry2_activated_time = bar.timestamp
            self._emit(zone, bar, "ENTRY2_ACTIVATED", price=fill)
        return fill

    def _close_live_legs(self, zone: ZoneRecord, exit_price: float, demand: bool, fraction: float = 1.0) -> None:
        stop = float(zone.sl_price)
        for leg in self._live_legs(zone).values():
            if not leg["filled"] or leg["open_fraction"] <= 0.0:
                continue
            sizing_price = float(leg["planned_price"])
            if self._confirm_entry():
                sizing_price = float(leg["fill_price"])  # sized on the real distance from the confirmation close
            elif str(self.parameters.get("leg_sizing", LEG_SIZING_OWN)) == LEG_SIZING_EQUAL:
                sizing_price = float(self._live_legs(zone)["1"]["planned_price"])
            risk = abs(sizing_price - stop)
            if risk <= 0.0:
                continue
            portion = float(leg["open_fraction"]) * fraction
            pnl = (exit_price - float(leg["fill_price"])) if demand else (float(leg["fill_price"]) - exit_price)
            weight = 1.0 if self._single_entry() else LEG_RISK_WEIGHT
            leg["realized_r"] = float(leg["realized_r"]) + weight * portion * pnl / risk
            leg["open_fraction"] = float(leg["open_fraction"]) - portion

    def _live_result_r(self, zone: ZoneRecord) -> float:
        return sum(float(leg["realized_r"]) for leg in self._live_legs(zone).values())

    def _finish_live(self, zone: ZoneRecord, bar: Candle, state: str, exit_price: float, event_price: float, reason: str) -> None:
        demand = zone.side == "DEMAND"
        self._close_live_legs(zone, exit_price, demand)
        zone.terminal_state = state
        zone.terminal_bar_index = bar.index
        zone.terminal_time = bar.timestamp
        zone.result_r = self._live_result_r(zone)
        zone.result_price = exit_price
        zone.exit_price = exit_price
        zone.result_points = abs(exit_price - float(zone.entry_price))
        zone.terminal_reason = reason
        if state == "TP2_HIT":
            zone.tp2_hit_bar_index = bar.index
            zone.tp2_hit_time = bar.timestamp
        elif state == "TP1_FINAL":
            zone.tp1_final_bar_index = bar.index
            zone.tp1_final_time = bar.timestamp
        self._emit(zone, bar, state, price=event_price, reason=reason)

    def _update_excursions(self, zone: ZoneRecord, bar: Candle, demand: bool) -> None:
        risk = abs(float(zone.entry_price) - float(zone.sl_price)) if zone.entry_price is not None and zone.sl_price is not None else None
        entry = float(zone.entry_price)
        if demand:
            zone.mfe_price = max(float(zone.mfe_price) if zone.mfe_price is not None else bar.high, bar.high)
            zone.mae_price = min(float(zone.mae_price) if zone.mae_price is not None else bar.low, bar.low)
            zone.mfe_points = max(0.0, float(zone.mfe_price) - entry)
            zone.mae_points = max(0.0, entry - float(zone.mae_price))
        else:
            zone.mfe_price = min(float(zone.mfe_price) if zone.mfe_price is not None else bar.low, bar.low)
            zone.mae_price = max(float(zone.mae_price) if zone.mae_price is not None else bar.high, bar.high)
            zone.mfe_points = max(0.0, entry - float(zone.mfe_price))
            zone.mae_points = max(0.0, float(zone.mae_price) - entry)
        if risk:
            zone.mfe_r = zone.mfe_points / risk
            zone.mae_r = zone.mae_points / risk

    def _tp_multiples(self) -> tuple[float, float]:
        if not self._live_mode():
            return TP1_R, TP2_R
        return float(self.parameters.get("tp1_r", TP1_R)), float(self.parameters.get("tp2_r", TP2_R))

    def _limit_filter_reason(self, demand: bool, top: float, bottom: float, source_index: int) -> str | None:
        """Optional filters applied when a LIMIT would be created (Live broker model only; all off by default)."""
        if not self._live_mode():
            return None
        snap = self._features.snapshot()
        max_risk_atr = float(self.parameters.get("max_risk_atr", 0.0) or 0.0)
        if max_risk_atr > 0.0:
            entry, stop, _, _, _ = self._entry_stop_target(demand, top, bottom, self._bars[source_index].index)
            atr = snap.get("atr14")
            if atr and abs(entry - stop) / float(atr) > max_risk_atr:
                return "risk_too_wide"
        trend = str(self.parameters.get("trend_filter", "None"))
        if trend != "None":
            dist = snap.get("ema200_dist_atr")
            if dist is not None:
                with_trend = dist > 0 if demand else dist < 0
                if (trend == "With EMA200" and not with_trend) or (trend == "Against EMA200" and with_trend):
                    return "trend_filter"
        session = str(self.parameters.get("session_filter", "All"))
        if session != "All" and snap.get("session") is not None:
            allowed = {"London + New York": {"LONDON", "LONDON_NY_OVERLAP", "NEW_YORK"}, "London only": {"LONDON", "LONDON_NY_OVERLAP"}}.get(session)
            if allowed is not None and snap.get("session") not in allowed:
                return "session_filter"
        return None

    def _confirm_entry(self) -> bool:
        return str(self.parameters.get("entry_mode", "Limit")) == "Close confirmation"

    def _single_entry(self) -> bool:
        return self._confirm_entry() or str(self.parameters.get("entry2_mode", "Far edge")) == "Off"

    def _close_stop_mode(self) -> bool:
        return str(self.parameters.get("stop_rule", STOP_CLOSE)) == STOP_CLOSE

    def hard_stop_price(self, zone: ZoneRecord) -> float | None:
        """Emergency broker stop used with the candle-close SL: ``hard_stop_beyond_r`` x risk beyond the SL.

        It protects against gaps and the bot being offline, because the real SL is only acted on at
        candle close. None when the SL itself is the broker stop (touch rule) or the stop is disabled.
        """
        beyond = float(self.parameters.get("hard_stop_beyond_r", 1.0) or 0.0)
        if not self._close_stop_mode() or beyond <= 0.0 or zone.sl_price is None:
            return None
        entry1 = float(zone.entry1 if zone.entry1 is not None else zone.entry_price)
        stop = float(zone.sl_price)
        risk = abs(entry1 - stop)
        return stop - beyond * risk if zone.side == "DEMAND" else stop + beyond * risk

    def _manage_zone_live(self, zone: ZoneRecord, bar: Candle) -> None:
        demand = zone.side == "DEMAND"
        legs = self._live_legs(zone)
        stop = float(zone.sl_price)
        close_mode = self._close_stop_mode()
        hard_stop = self.hard_stop_price(zone)
        if hard_stop is not None:
            zone.metadata["hard_stop_price"] = hard_stop
        entry1 = float(legs["1"]["planned_price"])
        entry2 = float(legs["2"]["planned_price"])
        tp1 = float(zone.tp1_price if zone.tp1_price is not None else zone.tp_price)
        tp2 = float(zone.tp2_price if zone.tp2_price is not None else zone.tp_price)

        def touched(level: float, against: bool) -> bool:
            # against=True: price moving against the trade (entries, stop); False: in favour (targets).
            if demand:
                return bar.low <= level if against else bar.high >= level
            return bar.high >= level if against else bar.low <= level

        if zone.activated_bar_index is None:
            if bar.index <= int(zone.limit_created_bar_index):
                return
            if self._confirm_entry():
                # Rule: enter only after price has tested the zone and a candle CLOSES back outside it.
                if touched(entry1, True):
                    zone.metadata["zone_tested"] = True
                closes_outside = bar.close > entry1 if demand else bar.close < entry1
                if zone.metadata.get("zone_tested") and closes_outside:
                    leg = legs["1"]
                    leg.update({"filled": True, "fill_price": bar.close, "fill_bar_index": bar.index, "open_fraction": 1.0})
                    zone.activated_bar_index = bar.index
                    zone.activated_time = bar.timestamp
                    zone.entry1_activated_bar_index = bar.index
                    zone.entry1_activated_time = bar.timestamp
                    zone.activation_reason = "close_confirmation"
                    self._emit(zone, bar, "ENTRY1_ACTIVATED", price=bar.close)
                    return
                if self._post_limit_invalidated(bar, demand, zone.zone_top, zone.zone_bottom):
                    zone.terminal_state = "CANCELED"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = 0.0
                    zone.terminal_reason = "Zone broken before confirmation"
                    self._emit(zone, bar, "CANCELED", reason=zone.terminal_reason)
                return
            if touched(entry1, True):
                self._fill_live_leg(zone, "1", bar, demand)
                if not self._single_entry() and touched(entry2, True):
                    self._fill_live_leg(zone, "2", bar, demand)
                self._update_excursions(zone, bar, demand)
                # The broker-side stop is live from the moment Entry 1 fills. With the candle-close
                # rule the SL itself is only judged from the next candle (as in Pine); the emergency
                # stop still protects the fill candle.
                broker_stop = hard_stop if close_mode else stop
                if broker_stop is not None and touched(broker_stop, True):
                    exit_price = self._stop_exit_price(bar, demand, broker_stop)
                    reason = "Emergency broker stop touched on the entry candle" if close_mode else "SL touched on the entry candle"
                    self._finish_live(zone, bar, "SL_HIT", exit_price, exit_price, reason)
                return
            approach = float(self.parameters.get("approach_cancel_atr", 0.0) or 0.0)
            atr_now = self._features.snapshot().get("atr14")
            toward = bar.close < bar.open if demand else bar.close > bar.open
            if approach > 0.0 and atr_now and toward and abs(bar.close - bar.open) >= approach * float(atr_now) and abs(bar.close - entry1) <= float(atr_now):
                # Rule: a large candle racing toward the zone, closing within 1 ATR of it, cancels the limit.
                zone.terminal_state = "CANCELED"
                zone.terminal_bar_index = bar.index
                zone.terminal_time = bar.timestamp
                zone.result_r = 0.0
                zone.terminal_reason = "Fast approach into the zone"
                self._emit(zone, bar, "CANCELED", reason=zone.terminal_reason)
                return
            if self._post_limit_invalidated(bar, demand, zone.zone_top, zone.zone_bottom):
                zone.terminal_state = "CANCELED"
                zone.terminal_bar_index = bar.index
                zone.terminal_time = bar.timestamp
                zone.result_r = 0.0
                zone.terminal_reason = "Zone invalidated before activation"
                self._emit(zone, bar, "CANCELED", reason="Zone invalidated before activation")
            return

        if bar.index <= int(zone.activated_bar_index):
            return
        if zone.metadata.get("be_armed_after_bar") is not None and bar.index > int(zone.metadata["be_armed_after_bar"]):
            zone.metadata["be_armed"] = True
        self._update_excursions(zone, bar, demand)
        # Rule: break-even. Once a candle has reached be_trigger_r, the stop sits at Entry 1 from the next candle.
        if zone.metadata.get("be_armed") and touched(entry1, True):
            exit_price = self._stop_exit_price(bar, demand, entry1)
            self._finish_live(zone, bar, "BE_EXIT", exit_price, entry1, "Stop at break-even")
            return
        be_trigger = float(self.parameters.get("be_trigger_r", 0.0) or 0.0)
        if be_trigger > 0.0 and not zone.metadata.get("be_armed"):
            risk1 = abs(entry1 - stop)
            reached = bar.high >= entry1 + be_trigger * risk1 if demand else bar.low <= entry1 - be_trigger * risk1
            if reached:
                zone.metadata["be_armed_after_bar"] = bar.index

        if zone.tp1_hit_bar_index is None:
            if not self._single_entry() and not legs["2"]["filled"] and not legs["2"]["canceled"] and touched(entry2, True):
                self._fill_live_leg(zone, "2", bar, demand)
            if close_mode and hard_stop is not None and touched(hard_stop, True):
                exit_price = self._stop_exit_price(bar, demand, hard_stop)
                self._finish_live(zone, bar, "SL_HIT", exit_price, exit_price, "Emergency broker stop")
            elif not close_mode and touched(stop, True):
                exit_price = self._stop_exit_price(bar, demand, stop)
                self._finish_live(zone, bar, "SL_HIT", exit_price, exit_price, "SL touched")
            elif touched(tp2, False):
                zone.tp1_hit_bar_index = bar.index
                zone.tp1_hit_time = bar.timestamp
                self._emit(zone, bar, "TP1_HIT", price=tp1)
                exit_price = self._target_exit_price(bar, demand, tp2)
                self._finish_live(zone, bar, "TP2_HIT", exit_price, tp2, "TP2")
            elif touched(tp1, False):
                zone.tp1_hit_bar_index = bar.index
                zone.tp1_hit_time = bar.timestamp
                if not legs["2"]["filled"]:
                    legs["2"]["canceled"] = True
                # The runtime closes half of every open position after the TP1 candle closes.
                self._close_live_legs(zone, bar.close, demand, fraction=float(self.parameters.get("tp1_close_fraction", 0.5)))
                zone.metadata["tp1_partial_price"] = bar.close
                self._emit(zone, bar, "TP1_HIT", price=tp1)
            elif close_mode and self._stop_confirmed(bar, demand, stop):
                # Pine rule: a candle closes with at least half its body beyond the SL; the bot exits at that close.
                self._finish_live(zone, bar, "SL_HIT", bar.close, stop, "Candle closed beyond SL")
            return

        if bar.index <= int(zone.tp1_hit_bar_index):
            return
        broker_stop = hard_stop if close_mode else stop
        if broker_stop is not None and touched(broker_stop, True):
            exit_price = self._stop_exit_price(bar, demand, broker_stop)
            reason = "Remaining position hit the emergency broker stop after TP1" if close_mode else "Remaining position stopped at SL after TP1"
            self._finish_live(zone, bar, "TP1_FINAL", exit_price, tp1, reason)
        elif touched(tp2, False):
            exit_price = self._target_exit_price(bar, demand, tp2)
            self._finish_live(zone, bar, "TP2_HIT", exit_price, tp2, "TP2")
        elif touched(entry1, True) or (close_mode and self._stop_confirmed(bar, demand, stop)):
            self._finish_live(zone, bar, "TP1_FINAL", bar.close, tp1, "Price returned to Entry 1 after TP1; closed at candle close")

    def _manage_zones(self, bar: Candle) -> None:
        if self._live_mode():
            for zone in self._zones:
                if not zone.terminal_state and zone.limit_created_bar_index is not None:
                    self._manage_zone_live(zone, bar)
            return
        for zone in self._zones:
            if zone.terminal_state:
                continue
            demand = zone.side == "DEMAND"
            invalid_after_created = self._post_limit_invalidated(bar, demand, zone.zone_top, zone.zone_bottom)
            if zone.limit_created_bar_index is None:
                continue
            if zone.activated_bar_index is None:
                old_enough = bar.index - zone.source_bar_index >= int(self.parameters["min_bars_after_source"])
                after_arming = bar.index > int(zone.limit_created_bar_index)
                overlaps = bar.high >= zone.zone_bottom and bar.low <= zone.zone_top
                prev = self._bars[-2] if len(self._bars) >= 2 else None
                approached = (prev.low > zone.zone_top if demand else prev.high < zone.zone_bottom) if prev else False
                retest = old_enough and after_arming and overlaps and approached
                if retest:
                    zone.activated_bar_index = bar.index
                    zone.activated_time = bar.timestamp
                    zone.entry1_activated_bar_index = bar.index
                    zone.entry1_activated_time = bar.timestamp
                    zone.first_retest_bar_index = zone.first_retest_bar_index or bar.index
                    zone.first_retest_time = zone.first_retest_time or bar.timestamp
                    zone.retest_count = int(zone.retest_count or 0) + 1
                    zone.activation_reason = "qualified_retest"
                    self._emit(zone, bar, "ENTRY1_ACTIVATED", price=zone.entry_price)
                    entry2 = float(zone.entry2)
                    entry2_touched = bar.low <= entry2 if demand else bar.high >= entry2
                    if entry2_touched:
                        zone.entry2_activated_bar_index = bar.index
                        zone.entry2_activated_time = bar.timestamp
                        self._emit(zone, bar, "ENTRY2_ACTIVATED", price=entry2)
                elif invalid_after_created:
                    zone.terminal_state = "CANCELED"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = 0.0
                    zone.terminal_reason = "Zone invalidated before activation"
                    self._emit(zone, bar, "CANCELED", reason="Zone invalidated before activation")
                continue
            if bar.index <= int(zone.activated_bar_index):
                continue
            risk = abs(float(zone.entry_price) - float(zone.sl_price)) if zone.entry_price is not None and zone.sl_price is not None else None
            entry = float(zone.entry_price)
            if demand:
                zone.mfe_price = max(float(zone.mfe_price) if zone.mfe_price is not None else bar.high, bar.high)
                zone.mae_price = min(float(zone.mae_price) if zone.mae_price is not None else bar.low, bar.low)
                zone.mfe_points = max(0.0, float(zone.mfe_price) - entry)
                zone.mae_points = max(0.0, entry - float(zone.mae_price))
            else:
                zone.mfe_price = min(float(zone.mfe_price) if zone.mfe_price is not None else bar.low, bar.low)
                zone.mae_price = max(float(zone.mae_price) if zone.mae_price is not None else bar.high, bar.high)
                zone.mfe_points = max(0.0, entry - float(zone.mfe_price))
                zone.mae_points = max(0.0, float(zone.mae_price) - entry)
            if risk:
                zone.mfe_r = zone.mfe_points / risk
                zone.mae_r = zone.mae_points / risk
            tp1 = float(zone.tp1_price if zone.tp1_price is not None else zone.tp_price)
            tp2 = float(zone.tp2_price if zone.tp2_price is not None else zone.tp_price)
            if zone.entry2_activated_bar_index is None and zone.tp1_hit_bar_index is None:
                entry2 = float(zone.entry2)
                entry2_touched = bar.low <= entry2 if demand else bar.high >= entry2
                if entry2_touched:
                    zone.entry2_activated_bar_index = bar.index
                    zone.entry2_activated_time = bar.timestamp
                    self._emit(zone, bar, "ENTRY2_ACTIVATED", price=entry2)
            tp1_touched = bar.high >= tp1 if demand else bar.low <= tp1
            tp2_touched = bar.high >= tp2 if demand else bar.low <= tp2
            if zone.tp1_hit_bar_index is None:
                if tp2_touched:
                    zone.tp1_hit_bar_index = bar.index
                    zone.tp1_hit_time = bar.timestamp
                    self._emit(zone, bar, "TP1_HIT", price=tp1)
                    zone.tp2_hit_bar_index = bar.index
                    zone.tp2_hit_time = bar.timestamp
                    zone.terminal_state = "TP2_HIT"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = TP2_R
                    zone.result_price = tp2
                    zone.exit_price = tp2
                    zone.result_points = abs(tp2 - entry)
                    zone.terminal_reason = "TP2"
                    self._emit(zone, bar, "TP2_HIT", price=tp2)
                elif tp1_touched:
                    zone.tp1_hit_bar_index = bar.index
                    zone.tp1_hit_time = bar.timestamp
                    self._emit(zone, bar, "TP1_HIT", price=tp1)
                elif self._stop_confirmed(bar, demand, float(zone.sl_price)):
                    zone.terminal_state = "SL_HIT"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = -1.0
                    zone.result_price = float(zone.sl_price)
                    zone.exit_price = zone.result_price
                    zone.result_points = abs(float(zone.result_price) - entry)
                    zone.terminal_reason = "SL"
                    self._emit(zone, bar, "SL_HIT", price=zone.result_price)
            elif bar.index > int(zone.tp1_hit_bar_index):
                returned_to_entry1 = bar.low <= entry if demand else bar.high >= entry
                old_stop_confirmed = self._stop_confirmed(bar, demand, float(zone.sl_price))
                if tp2_touched:
                    zone.tp2_hit_bar_index = bar.index
                    zone.tp2_hit_time = bar.timestamp
                    zone.terminal_state = "TP2_HIT"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = TP2_R
                    zone.result_price = tp2
                    zone.exit_price = tp2
                    zone.result_points = abs(tp2 - entry)
                    zone.terminal_reason = "TP2"
                    self._emit(zone, bar, "TP2_HIT", price=tp2)
                elif returned_to_entry1 or old_stop_confirmed:
                    zone.tp1_final_bar_index = bar.index
                    zone.tp1_final_time = bar.timestamp
                    zone.terminal_state = "TP1_FINAL"
                    zone.terminal_bar_index = bar.index
                    zone.terminal_time = bar.timestamp
                    zone.result_r = TP1_R
                    zone.result_price = tp1
                    zone.exit_price = tp1
                    zone.result_points = abs(tp1 - entry)
                    zone.terminal_reason = "Price returned to Entry 1 after TP1" if returned_to_entry1 else "Price reached the previous Stop Loss condition after TP1"
                    self._emit(zone, bar, "TP1_FINAL", price=tp1, reason=zone.terminal_reason)

    def _emit(self, zone: ZoneRecord, bar: Candle, event_type: str, *, price: float | None = None, reason: str | None = None, metadata: dict | None = None) -> None:
        super()._emit(zone, bar, event_type, price=price, reason=reason, metadata=metadata)
        moment = EVENT_MOMENTS.get(event_type)
        if moment is not None:
            # Record the market values at this lifecycle moment (closed candle, no look-ahead).
            zone.metadata.update(flatten_snapshot(moment, self._features.snapshot()))
            if moment == "exit":
                zone.metadata["exit_reason"] = zone.terminal_reason or event_type
                legs = zone.metadata.get("live_legs") or {}
                for leg_no, leg in legs.items():
                    zone.metadata[f"entry{leg_no}_fill_price"] = leg.get("fill_price")
                    zone.metadata[f"entry{leg_no}_result_r"] = round(float(leg.get("realized_r") or 0.0), 4)

    def run(self, frame) -> dict:
        # Pine's inScanRange needs last_bar_index, which is only known for a complete dataset.
        self._last_bar_index = len(frame) - 1
        return super().run(frame)

    def _in_scan_range(self, bar: Candle) -> bool:
        """Pine ``inScanRange``: only the last ``history_bars`` chart bars may create zones (opt-in)."""
        last = getattr(self, "_last_bar_index", None)
        if not bool(self.parameters.get("apply_history_window", False)) or last is None:
            return True
        return bar.index > last - int(self.parameters["history_bars"])

    def process_bar(self, bar: Candle) -> None:
        self._bars.append(bar)
        self._update_swings()
        self._features.update(bar)
        body_len = int(self.parameters["body_len"])
        atr_len = int(self.parameters["atr_len"])
        avg_body = self._sma_body(body_len)
        atr = self._atr(atr_len)
        if avg_body is None or atr is None:
            return
        history_required = int(self.parameters["momentum_count"]) + int(self.parameters["compact_search_bars"]) + 2
        recent = self._bars[-int(self.parameters["momentum_count"]):]
        bull_count = sum(1 for item in recent if self._is_momentum(item, avg_body, True)) if bar.index >= int(self.parameters["momentum_count"]) - 1 else 0
        bear_count = sum(1 for item in recent if self._is_momentum(item, avg_body, False)) if bar.index >= int(self.parameters["momentum_count"]) - 1 else 0
        can_detect = bar.index >= history_required and self._in_scan_range(bar)
        bull_qual = bull_count >= int(self.parameters["momentum_count"])
        bear_qual = bear_count >= int(self.parameters["momentum_count"])
        bull_edge = can_detect and bull_qual and not self._prev_bull_qual
        bear_edge = can_detect and bear_qual and not self._prev_bear_qual
        if self.parameters["leg_reset_mode"] == "Any opposite close":
            if bar.close < bar.open:
                self._bull_leg = False
            if bar.close > bar.open:
                self._bear_leg = False
        else:
            if self._is_momentum(bar, avg_body, False):
                self._bull_leg = False
            if self._is_momentum(bar, avg_body, True):
                self._bear_leg = False
        day = bar.timestamp[:10]
        if self._last_day and day != self._last_day:
            self._sequence = 0
        self._last_day = day
        if bull_edge and not self._bull_leg:
            same_side_spacing_ok = self._last_demand_candidate is None or bar.index - self._last_demand_candidate >= int(self.parameters["min_same_side_gap"])
            if same_side_spacing_ok:
                raw_top, raw_bottom, source_offset, target_offset, compact = self._bounds(True, avg_body, atr)
                top, bottom = self._clamp_zone_bounds(raw_top, raw_bottom, atr)
                zone_id = self._zone_id(True, bar)
                width_ok = self._valid_width(top - bottom, atr)
                overlap = self._has_overlap(True, top, bottom)
                context = self._zone_context(True, bar, source_offset, target_offset, compact, avg_body, atr, bull_count, bear_count, overlap)
                context.update({"raw_zone_top": raw_top, "raw_zone_bottom": raw_bottom, "clamped_zone_top": top, "clamped_zone_bottom": bottom})
                diagnostic = self._new_candidate_diagnostic(True, raw_top, raw_bottom, top, bottom, max(0, len(self._bars) - 1 - source_offset), target_offset, compact, zone_id, bar, atr, context, same_side_spacing_ok=True, initial_overlap_ok=not overlap, width_ok=width_ok)
                context["candidate_diagnostic"] = diagnostic
                if width_ok and not overlap:
                    if self._queue_or_create(True, top, bottom, max(0, len(self._bars) - 1 - source_offset), zone_id, bar, atr, avg_body, context, diagnostic):
                        self._sequence += 1
                        self._last_demand_candidate = bar.index
                else:
                    reason = "overlap" if overlap else "width_filter"
                    self._emit_orphan(zone_id, bar, "ZONE_REJECTED", reason=reason, metadata={**context, "width": top - bottom, "atr": atr})
                    self._set_candidate_outcome(diagnostic, "OVERLAP_REJECTED" if overlap else "WIDTH_REJECTED", reason)
            else:
                raw_top, raw_bottom, source_offset, target_offset, compact = self._bounds(True, avg_body, atr)
                top, bottom = self._clamp_zone_bounds(raw_top, raw_bottom, atr)
                zone_id = self._zone_id(True, bar)
                width_ok = self._valid_width(top - bottom, atr)
                overlap = self._has_overlap(True, top, bottom)
                context = self._zone_context(True, bar, source_offset, target_offset, compact, avg_body, atr, bull_count, bear_count, overlap)
                context.update({"raw_zone_top": raw_top, "raw_zone_bottom": raw_bottom, "clamped_zone_top": top, "clamped_zone_bottom": bottom})
                diagnostic = self._new_candidate_diagnostic(True, raw_top, raw_bottom, top, bottom, max(0, len(self._bars) - 1 - source_offset), target_offset, compact, zone_id, bar, atr, context, same_side_spacing_ok=False, initial_overlap_ok=not overlap, width_ok=width_ok)
                self._set_candidate_outcome(diagnostic, "OTHER", "same_side_spacing")
            self._bull_leg = True
        if bear_edge and not self._bear_leg:
            same_side_spacing_ok = self._last_supply_candidate is None or bar.index - self._last_supply_candidate >= int(self.parameters["min_same_side_gap"])
            if same_side_spacing_ok:
                raw_top, raw_bottom, source_offset, target_offset, compact = self._bounds(False, avg_body, atr)
                top, bottom = self._clamp_zone_bounds(raw_top, raw_bottom, atr)
                zone_id = self._zone_id(False, bar)
                width_ok = self._valid_width(top - bottom, atr)
                overlap = self._has_overlap(False, top, bottom)
                context = self._zone_context(False, bar, source_offset, target_offset, compact, avg_body, atr, bull_count, bear_count, overlap)
                context.update({"raw_zone_top": raw_top, "raw_zone_bottom": raw_bottom, "clamped_zone_top": top, "clamped_zone_bottom": bottom})
                diagnostic = self._new_candidate_diagnostic(False, raw_top, raw_bottom, top, bottom, max(0, len(self._bars) - 1 - source_offset), target_offset, compact, zone_id, bar, atr, context, same_side_spacing_ok=True, initial_overlap_ok=not overlap, width_ok=width_ok)
                context["candidate_diagnostic"] = diagnostic
                if width_ok and not overlap:
                    if self._queue_or_create(False, top, bottom, max(0, len(self._bars) - 1 - source_offset), zone_id, bar, atr, avg_body, context, diagnostic):
                        self._sequence += 1
                        self._last_supply_candidate = bar.index
                else:
                    reason = "overlap" if overlap else "width_filter"
                    self._emit_orphan(zone_id, bar, "ZONE_REJECTED", reason=reason, metadata={**context, "width": top - bottom, "atr": atr})
                    self._set_candidate_outcome(diagnostic, "OVERLAP_REJECTED" if overlap else "WIDTH_REJECTED", reason)
            else:
                raw_top, raw_bottom, source_offset, target_offset, compact = self._bounds(False, avg_body, atr)
                top, bottom = self._clamp_zone_bounds(raw_top, raw_bottom, atr)
                zone_id = self._zone_id(False, bar)
                width_ok = self._valid_width(top - bottom, atr)
                overlap = self._has_overlap(False, top, bottom)
                context = self._zone_context(False, bar, source_offset, target_offset, compact, avg_body, atr, bull_count, bear_count, overlap)
                context.update({"raw_zone_top": raw_top, "raw_zone_bottom": raw_bottom, "clamped_zone_top": top, "clamped_zone_bottom": bottom})
                diagnostic = self._new_candidate_diagnostic(False, raw_top, raw_bottom, top, bottom, max(0, len(self._bars) - 1 - source_offset), target_offset, compact, zone_id, bar, atr, context, same_side_spacing_ok=False, initial_overlap_ok=not overlap, width_ok=width_ok)
                self._set_candidate_outcome(diagnostic, "OTHER", "same_side_spacing")
            self._bear_leg = True
        self._prev_bull_qual = bull_qual
        self._prev_bear_qual = bear_qual
        self._process_pending(bar, atr)
        self._manage_zones(bar)
