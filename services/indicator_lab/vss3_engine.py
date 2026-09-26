"""VSS3 Pine-derived candle engine."""

from __future__ import annotations

from services.indicator_lab.engine import IndicatorEngine
from services.indicator_lab.models import Candle, ZoneRecord


class VSS3Engine(IndicatorEngine):
    """Candle-by-candle VSS3 S/D zone and LIMIT lifecycle engine."""

    indicator = "VSS3"

    def __init__(self, parameters: dict | None = None, *, symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
        super().__init__(parameters, symbol=symbol, timeframe=timeframe)
        self._last_demand_zone = -10**9
        self._last_supply_zone = -10**9
        self._swing_highs: list[tuple[int, str, float]] = []
        self._swing_lows: list[tuple[int, str, float]] = []
        self._converted: set[str] = set()

    def _is_momentum(self, bar: Candle, avg_body: float, demand: bool) -> bool:
        return (bar.close > bar.open if demand else bar.close < bar.open) and abs(bar.close - bar.open) >= avg_body * float(self.parameters["momentum_body_mult"])

    def _guid(self, source: Candle, demand: bool, flipped: bool = False) -> str:
        base = f"VSS3-{source.timestamp}-{('D' if demand else 'S')}-{source.index}"
        return f"FLIP-{base}" if flipped else base

    def _buffer(self) -> float:
        return self._symbol_buffer(self.parameters["gold_sl"], self.parameters["us30_sl"], self.parameters["nasdaq_sl"], self.parameters["wti_sl"])

    def _first_swing_high_after(self, source_index: int, fallback: float) -> float:
        max_bars = int(self.parameters["max_after_box_swing_bars"])
        for idx, _ts, value in self._swing_highs:
            if idx > source_index and idx - source_index <= max_bars:
                return value
        return fallback

    def _first_swing_low_after(self, source_index: int, fallback: float) -> float:
        max_bars = int(self.parameters["max_after_box_swing_bars"])
        for idx, _ts, value in self._swing_lows:
            if idx > source_index and idx - source_index <= max_bars:
                return value
        return fallback

    def _close_stop_sell(self, level: float, bar: Candle) -> bool:
        top = max(bar.open, bar.close)
        bottom = min(bar.open, bar.close)
        body = top - bottom
        above = max(0.0, top - max(bottom, level))
        return bar.close > level and body > 0 and above >= body * 0.5

    def _close_stop_buy(self, level: float, bar: Candle) -> bool:
        top = max(bar.open, bar.close)
        bottom = min(bar.open, bar.close)
        body = top - bottom
        below = max(0.0, min(top, level) - bottom)
        return bar.close < level and body > 0 and below >= body * 0.5

    def _record_pivots(self) -> None:
        length = int(self.parameters["swing_pivot_len"])
        if len(self._bars) < length * 2 + 1:
            return
        pivot = self._bars[-1 - length]
        left = self._bars[-1 - length * 2 : -1 - length]
        right = self._bars[-length:]
        if all(pivot.high > item.high for item in left + right):
            self._swing_highs.append((pivot.index, pivot.timestamp, pivot.high))
            self._swing_highs = self._swing_highs[-300:]
        if all(pivot.low < item.low for item in left + right):
            self._swing_lows.append((pivot.index, pivot.timestamp, pivot.low))
            self._swing_lows = self._swing_lows[-300:]

    def _clamp_width(self, top: float, bottom: float, atr: float) -> tuple[float, float]:
        width = top - bottom
        max_width = atr * float(self.parameters["max_zone_size_atr"])
        if width <= max_width or max_width <= 0:
            return top, bottom
        mid = (top + bottom) / 2.0
        return mid + max_width / 2.0, mid - max_width / 2.0

    def _create_zone(self, demand: bool, source: Candle, bar: Candle, atr: float, *, flipped: bool = False, parent: ZoneRecord | None = None) -> ZoneRecord:
        raw_top, raw_bottom = source.high, source.low
        top, bottom = self._clamp_width(source.high, source.low, atr)
        entry = top if demand else bottom
        buffer = self._buffer()
        stop = min(self._first_swing_low_after(source.index, bottom), bottom) - buffer if demand else max(self._first_swing_high_after(source.index, top), top) + buffer
        risk = abs(entry - stop)
        target = entry + risk * float(self.parameters["tp_rr"]) if demand else entry - risk * float(self.parameters["tp_rr"])
        metadata = {
            "raw_zone_top": raw_top,
            "raw_zone_bottom": raw_bottom,
            "max_zone_size_atr_used": self.parameters["max_zone_size_atr"],
            "min_distance_between_zones_used": self.parameters["min_distance_between_zones"],
            "momentum_span_used": self.parameters["momentum_span"],
            "flip_created": flipped,
            "flip_parent_zone_id": parent.engine_zone_id if parent else None,
            "min_flipped_order_distance_percent_used": self.parameters["min_flipped_order_distance_percent"],
            "retest_cooldown_used": self.parameters["retest_cooldown"],
            "swing_lookback_used": self.parameters["swing_pivot_len"],
            "spread_buffer_used": self.parameters["spread_buffer"],
            "buffer_used": buffer,
            "tp_rr_used": self.parameters["tp_rr"],
        }
        zone = ZoneRecord(
            engine_zone_id=self._guid(source, demand, flipped=flipped),
            side="DEMAND" if demand else "SUPPLY",
            source_bar_index=source.index,
            source_time=source.timestamp,
            zone_top=top,
            zone_bottom=bottom,
            created_bar_index=bar.index,
            created_time=bar.timestamp,
            symbol=self.symbol,
            timeframe=self.timeframe,
            zone_type="VSS3_SUPPLY_DEMAND",
            flip_generation=(int(parent.flip_generation or 0) + 1) if parent else (1 if flipped else 0),
            source_open=source.open,
            source_high=source.high,
            source_low=source.low,
            source_close=source.close,
            source_body=abs(source.close - source.open),
            source_range=source.high - source.low,
            candidate_bar_index=bar.index,
            candidate_time=bar.timestamp,
            zone_mid=(top + bottom) / 2.0,
            zone_width_price=top - bottom,
            zone_width_points=top - bottom,
            zone_width_atr=(top - bottom) / atr if atr else None,
            atr_at_source=atr,
            momentum_count=int(self.parameters["momentum_count"]),
            momentum_span=int(self.parameters["momentum_span"]),
            entry1=entry,
            entry2=bottom if demand else top,
            entry_price=entry,
            sl_price=stop,
            tp_price=target,
            risk_distance=risk,
            reward_distance=abs(target - entry),
            rr=float(self.parameters["tp_rr"]),
            buffer_used=buffer,
            is_flipped=flipped,
            parent_zone_id=parent.engine_zone_id if parent else None,
            creation_reason="flipped_from_stop" if flipped else "momentum_supply_demand",
            metadata=metadata,
        )
        self._zones.append(zone)
        self._emit(zone, bar, "ZONE_CREATED")
        if flipped:
            self._emit(zone, bar, "FLIPPED", metadata={"parent_zone_id": parent.engine_zone_id if parent else None})
        return zone

    def _zone_age_ok(self, zone: ZoneRecord, bar: Candle) -> bool:
        return bar.index - zone.source_bar_index >= int(self.parameters["min_zone_size"])

    def _inside_flip_protection(self, candidate: ZoneRecord, flip: ZoneRecord) -> bool:
        gap = 0.0
        if candidate.zone_bottom > flip.zone_top:
            gap = (candidate.zone_bottom - flip.zone_top) / flip.zone_top * 100.0
        elif candidate.zone_top < flip.zone_bottom:
            gap = (flip.zone_bottom - candidate.zone_top) / flip.zone_bottom * 100.0
        return gap < float(self.parameters["min_flipped_order_distance_percent"])

    def _blocked_by_flips(self, zone: ZoneRecord) -> bool:
        for other in self._zones:
            if other.is_flipped and other.engine_zone_id != zone.engine_zone_id and other.parent_zone_id != zone.engine_zone_id:
                if self._inside_flip_protection(zone, other):
                    return True
        return False

    def _manage_zone(self, zone: ZoneRecord, bar: Candle) -> None:
        if zone.terminal_state:
            return
        demand = zone.side == "DEMAND"
        close_break = min(bar.open, bar.close) < zone.zone_bottom if demand else max(bar.open, bar.close) > zone.zone_top
        wick_break = bar.low < zone.zone_bottom if demand else bar.high > zone.zone_top
        invalid = wick_break if self.parameters["sd_end_method"] == "Wick" else close_break
        if zone.limit_created_bar_index is None:
            if invalid:
                zone.terminal_state = "INVALIDATED"
                zone.terminal_bar_index = bar.index
                zone.terminal_time = bar.timestamp
                zone.terminal_reason = "invalid_before_limit"
                self._emit(zone, bar, "INVALIDATED")
                return
            if not self._zone_age_ok(zone, bar):
                return
            if self._blocked_by_flips(zone):
                zone.terminal_state = "CANCELED"
                zone.terminal_bar_index = bar.index
                zone.terminal_time = bar.timestamp
                zone.terminal_reason = "flipped_zone_spacing"
                self._emit(zone, bar, "CANCELED", reason="flipped_zone_spacing")
                return
            dist = abs(bar.close - (zone.zone_top if demand else zone.zone_bottom)) / max(abs(bar.close), 1e-9) * 100.0
            moved_away = (bar.low > zone.zone_top and bar.close > zone.zone_top) if demand else (bar.high < zone.zone_bottom and bar.close < zone.zone_bottom)
            if moved_away and dist >= float(self.parameters["min_distance_percent"]):
                zone.limit_created_bar_index = bar.index
                zone.limit_created_time = bar.timestamp
                zone.departure_price = bar.close
                zone.departure_distance = abs(bar.close - float(zone.entry_price))
                zone.departure_distance_atr = zone.departure_distance / float(zone.atr_at_source) if zone.atr_at_source else None
                self._emit(zone, bar, "LIMIT_CREATED", price=zone.entry_price)
            return
        if zone.activated_bar_index is None:
            touch = bar.high >= float(zone.entry_price) and bar.low <= float(zone.entry_price)
            if touch:
                zone.activated_bar_index = bar.index
                zone.activated_time = bar.timestamp
                zone.first_retest_bar_index = zone.first_retest_bar_index or bar.index
                zone.first_retest_time = zone.first_retest_time or bar.timestamp
                zone.retest_count = int(zone.retest_count or 0) + 1
                zone.activation_reason = "entry_touched"
                self._emit(zone, bar, "ACTIVATED", price=zone.entry_price)
            elif invalid:
                zone.terminal_state = "CANCELED"
                zone.terminal_bar_index = bar.index
                zone.terminal_time = bar.timestamp
                zone.result_r = 0.0
                zone.terminal_reason = "invalid_before_activation"
                self._emit(zone, bar, "CANCELED", reason="invalid_before_activation")
            return
        hit_tp = bar.high >= float(zone.tp_price) if demand else bar.low <= float(zone.tp_price)
        hit_sl = self._close_stop_buy(float(zone.sl_price), bar) if demand else self._close_stop_sell(float(zone.sl_price), bar)
        be_done = zone.metadata.get("be_done") is True
        entry2_filled = zone.metadata.get("entry2_filled") is True
        hit_be = be_done and not entry2_filled and ((bar.low <= float(zone.entry1)) if demand else (bar.high >= float(zone.entry1)))
        if bar.index == zone.activated_bar_index and hit_sl:
            hit_tp = False
        if not be_done and not entry2_filled:
            half_r = float(zone.entry_price) + (abs(float(zone.entry_price) - float(zone.sl_price)) * 0.5 * (1 if demand else -1))
            if (bar.high >= half_r if demand else bar.low <= half_r) and bar.index > zone.activated_bar_index:
                zone.metadata["be_done"] = True
                zone.metadata["be_trigger_price"] = half_r
                zone.metadata["be_armed_bar"] = bar.index
                zone.metadata["be_moved"] = True
        if not entry2_filled and ((bar.low <= float(zone.entry2)) if demand else (bar.high >= float(zone.entry2))):
            zone.metadata["entry2_filled"] = True
            zone.metadata["entry2_bar"] = bar.index
        if hit_tp or hit_sl or hit_be:
            if hit_tp:
                state, result, price = "TP_HIT", float(self.parameters["tp_rr"]), float(zone.tp_price)
            elif hit_be:
                state, result, price = "BE_EXIT", 0.0, float(zone.entry1)
            else:
                state, result, price = "SL_HIT", -1.0, float(zone.sl_price)
            zone.terminal_state = state
            zone.terminal_bar_index = bar.index
            zone.terminal_time = bar.timestamp
            zone.result_r = result
            zone.result_price = price
            zone.exit_price = price
            zone.result_points = abs(price - float(zone.entry_price))
            zone.terminal_reason = state
            self._emit(zone, bar, state, price=price)
            if state == "SL_HIT" and not zone.is_flipped and zone.engine_zone_id not in self._converted:
                self._converted.add(zone.engine_zone_id)
                self._create_zone(not demand, Candle(bar.index, bar.timestamp, zone.zone_bottom if demand else zone.zone_top, zone.zone_top, zone.zone_bottom, zone.zone_bottom if demand else zone.zone_top), bar, self._atr(20) or 1.0, flipped=True, parent=zone)

    def process_bar(self, bar: Candle) -> None:
        self._bars.append(bar)
        self._record_pivots()
        avg_body = self._sma_body(20)
        atr = self._atr(20)
        if avg_body is None or atr is None:
            return
        for zone in list(self._zones):
            self._manage_zone(zone, bar)
        span = int(self.parameters["momentum_span"])
        count = int(self.parameters["momentum_count"])
        if len(self._bars) < span + 2:
            return
        recent = self._bars[-span:]
        bullish = sum(1 for item in recent if self._is_momentum(item, avg_body, True))
        bearish = sum(1 for item in recent if self._is_momentum(item, avg_body, False))
        if bullish >= count and bar.index - self._last_demand_zone > int(self.parameters["min_distance_between_zones"]):
            source = self._bars[-span - 1]
            zone = self._create_zone(True, source, bar, atr)
            self._last_demand_zone = bar.index
            self._emit(zone, bar, "CANDIDATE", reason="bullish_momentum")
        if bearish >= count and bar.index - self._last_supply_zone > int(self.parameters["min_distance_between_zones"]):
            source = self._bars[-span - 1]
            zone = self._create_zone(False, source, bar, atr)
            self._last_supply_zone = bar.index
            self._emit(zone, bar, "CANDIDATE", reason="bearish_momentum")
