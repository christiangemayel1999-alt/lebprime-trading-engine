"""Shared models for Indicator Lab engines and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


IndicatorName = Literal["LEBPRIME", "VSS3"]
ZoneSide = Literal["DEMAND", "SUPPLY"]


@dataclass(slots=True)
class Candle:
    index: int
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    tick_volume: float = 0.0
    spread: float = 0.0
    real_volume: float = 0.0

    @classmethod
    def from_row(cls, index: int, row: dict[str, Any]) -> "Candle":
        ts = row.get("timestamp_utc") or row.get("timestamp") or row.get("time")
        return cls(
            index=int(index),
            timestamp=str(ts),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=float(row.get("tick_volume", 0.0) or 0.0),
            spread=float(row.get("spread", 0.0) or 0.0),
            real_volume=float(row.get("real_volume", row.get("tick_volume", 0.0)) or 0.0),
        )


@dataclass(slots=True)
class ZoneEvent:
    zone_engine_id: str
    bar_index: int
    timestamp: str
    event_type: str
    price: float | None = None
    zone_top: float | None = None
    zone_bottom: float | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ZoneRecord:
    engine_zone_id: str
    side: ZoneSide
    source_bar_index: int
    source_time: str
    zone_top: float
    zone_bottom: float
    created_bar_index: int
    created_time: str
    symbol: str | None = None
    timeframe: str | None = None
    zone_type: str | None = None
    indicator_version: str | None = None
    flip_generation: int | None = None
    source_open: float | None = None
    source_high: float | None = None
    source_low: float | None = None
    source_close: float | None = None
    source_body: float | None = None
    source_range: float | None = None
    candidate_bar_index: int | None = None
    candidate_time: str | None = None
    candidate_open: float | None = None
    candidate_high: float | None = None
    candidate_low: float | None = None
    candidate_close: float | None = None
    candidate_body: float | None = None
    candidate_range: float | None = None
    atr_at_candidate: float | None = None
    average_body_at_candidate: float | None = None
    body_threshold: float | None = None
    bull_count: int | None = None
    bear_count: int | None = None
    compact_base_start_offset: int | None = None
    compact_base_end_offset: int | None = None
    base_start_bar_index: int | None = None
    base_end_bar_index: int | None = None
    base_start_time: str | None = None
    base_end_time: str | None = None
    base_bar_count: int | None = None
    refinement_target_offset: int | None = None
    refined_bar_index: int | None = None
    refined_time: str | None = None
    refined_open: float | None = None
    refined_high: float | None = None
    refined_low: float | None = None
    refined_close: float | None = None
    refined_range: float | None = None
    refinement_method: str | None = None
    zone_mid: float | None = None
    zone_width_price: float | None = None
    zone_width_points: float | None = None
    zone_width_atr: float | None = None
    atr_at_source: float | None = None
    average_body_at_source: float | None = None
    momentum_body: float | None = None
    momentum_body_ratio: float | None = None
    momentum_count: int | None = None
    momentum_span: int | None = None
    compact_body_ratio: float | None = None
    compact_range_atr_ratio: float | None = None
    confirmed_bar_index: int | None = None
    confirmed_time: str | None = None
    armed_bar_index: int | None = None
    armed_time: str | None = None
    limit_created_bar_index: int | None = None
    limit_created_time: str | None = None
    activated_bar_index: int | None = None
    activated_time: str | None = None
    entry1_activated_bar_index: int | None = None
    entry1_activated_time: str | None = None
    entry2_activated_bar_index: int | None = None
    entry2_activated_time: str | None = None
    bars_source_to_candidate: int | None = None
    bars_candidate_to_created: int | None = None
    bars_source_to_created: int | None = None
    bars_source_to_limit: int | None = None
    bars_source_to_activation: int | None = None
    zone_age_at_limit: int | None = None
    zone_age_at_activation: int | None = None
    zone_age_at_terminal: int | None = None
    entry1: float | None = None
    entry2: float | None = None
    entry_price: float | None = None
    sl_price: float | None = None
    structural_swing_bar_index: int | None = None
    structural_swing_time: str | None = None
    structural_swing_price: float | None = None
    structural_swing_kind: str | None = None
    tp1_price: float | None = None
    tp2_price: float | None = None
    tp1_hit_bar_index: int | None = None
    tp1_hit_time: str | None = None
    tp1_final_bar_index: int | None = None
    tp1_final_time: str | None = None
    tp2_hit_bar_index: int | None = None
    tp2_hit_time: str | None = None
    tp_price: float | None = None
    risk_distance: float | None = None
    reward_distance: float | None = None
    rr: float | None = None
    buffer_used: float | None = None
    departure_price: float | None = None
    departure_close: float | None = None
    departure_boundary: float | None = None
    departure_distance_price: float | None = None
    departure_distance: float | None = None
    departure_distance_atr: float | None = None
    first_retest_bar_index: int | None = None
    first_retest_time: str | None = None
    retest_count: int | None = None
    is_flipped: bool = False
    parent_zone_id: str | None = None
    terminal_state: str | None = None
    terminal_bar_index: int | None = None
    terminal_time: str | None = None
    exit_price: float | None = None
    result_r: float | None = None
    result_price: float | None = None
    result_points: float | None = None
    result_pips: float | None = None
    mfe_price: float | None = None
    mae_price: float | None = None
    mfe_points: float | None = None
    mae_points: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    holding_bars: int | None = None
    holding_minutes: float | None = None
    creation_reason: str | None = None
    rejection_reason: str | None = None
    activation_reason: str | None = None
    terminal_reason: str | None = None
    parameter_hash: str | None = None
    dataset_hash: str | None = None
    pine_reference_hash: str | None = None
    engine_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def zone_width(self) -> float:
        return float(self.zone_top) - float(self.zone_bottom)
