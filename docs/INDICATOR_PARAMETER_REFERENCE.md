# Indicator Parameter Reference

## LebPrime

| Pine name | Python name | Type | Default | Bounds / Values |
|---|---|---:|---:|---|
| `i_historyBars` | `history_bars` | int | 500 | 100..5000 |
| `i_bodyLen` | `body_len` | int | 20 | 5..100 |
| `i_bodyMult` | `body_mult` | float | 0.5 | 0.10..3.00 step 0.05 |
| `i_momentumCount` | `momentum_count` | int | 4 | 2..10 |
| `i_atrLen` | `atr_len` | int | 20 | 5..100 |
| `i_compactSearchBars` | `compact_search_bars` | int | 6 | 2..30 |
| `i_immediateBaseBars` | `immediate_base_bars` | int | 2 | 1..6 |
| `i_compressionBodyMult` | `compression_body_mult` | float | 0.60 | 0.05..1.50 |
| `i_compactRangeAtr` | `compact_range_atr` | float | 0.90 | 0.10..3.00 |
| `i_compactBaseMinBars` | `compact_base_min_bars` | int | 2 | 1..8 |
| `i_legResetMode` | `leg_reset_mode` | enum | Opposite momentum | Opposite momentum, Any opposite close |
| `i_minSameSideGap` | `min_same_side_gap` | int | 5 | 0..50 |
| `i_zoneConfirmDelay` | `zone_confirm_delay` | int | 5 | 0..30 |
| `i_zoneRefinement` | `zone_refinement` | enum | Smallest Range Candle | Full Base, Last Opposing, Smallest Range, Extreme Wick |
| `i_useRefinement` | `use_refinement` | bool | true | true/false |
| `i_maxZoneWidthATR` | `max_zone_width_atr` | float | 1.5 | 0.1..5.0 |
| `i_minZoneWidthATR` | `min_zone_width_atr` | float | 0.1 | 0.01..1.0 |
| `i_allowOverlap` | `allow_overlap` | bool | false | true/false |
| `i_overlapTolerance` | `overlap_tolerance` | float | 0.0 | min 0.0 |
| `i_invalidation` | `invalidation` | enum | Close | Wick, Close |
| `i_minBarsAfterSource` | `min_bars_after_source` | int | 10 | 0..100 |
| `i_retestCooldown` | `retest_cooldown` | int | 5 | 0..100 |
| `i_maxZones` | `max_zones` | int | 30 | 1..100 |
| `i_goldBuffer` | `gold_buffer` | float | 1.0 | min 0.0 |
| `i_us30Buffer` | `us30_buffer` | float | 50.0 | min 0.0 |
| `i_nasBuffer` | `nas_buffer` | float | 20.0 | min 0.0 |
| `i_wtiBuffer` | `wti_buffer` | float | 0.3 | min 0.0 |
| `i_rr` | `rr` | float | 2.0 | 0.5..10.0 |
| `i_sameBarPriority` | `same_bar_priority` | enum | SL first (conservative) | SL first, TP first |

Cosmetic alert/style inputs are intentionally excluded.

## VSS3

| Pine name | Python name | Type | Default | Bounds / Values |
|---|---|---:|---:|---|
| `zoneStateWarmupBars` | `zone_state_warmup_bars` | int | 5000 | exposed hidden constant |
| `tradeStateWarmupBars` | `trade_state_warmup_bars` | int | 2000 | exposed hidden constant |
| `maxSDZones` | `max_sd_zones` | int | 30 | 1..100 |
| `minZoneSize` | `min_zone_size` | int | 10 | 1..200 |
| `RETEST_COOLDOWN` | `retest_cooldown` | int | 5 | 0..100 |
| `minDistanceBetweenZones` | `min_distance_between_zones` | int | 5 | 0..100 |
| `maxZoneSizeATR` | `max_zone_size_atr` | float | 1.5 | 0.1..10.0 |
| `minFlippedOrderDistancePercent` | `min_flipped_order_distance_percent` | float | 0.10 | 0.0..5.0 |
| `combineSDs` | `combine_sds` | bool | true | true/false |
| `momentumBodyMult` | `momentum_body_mult` | float | 0.5 | 0.1..3.0 |
| `momentumCount` | `momentum_count` | int | 4 | 1..20 |
| `momentumSpan` | `momentum_span` | int | 4 | 1..20 |
| `zoneCount` | `zone_count` | enum | High | High, Medium, Low, One |
| `sdEndMethod` | `sd_end_method` | enum | Close | Wick, Close |
| `minDistancePercent` | `min_distance_percent` | float | 0.15 | 0.0..5.0 |
| `applyMinDistanceToZones` | `apply_min_distance_to_zones` | bool | true | true/false |
| `slMethod` | `sl_method` | enum | Previous Box Swing | Previous Box Swing |
| `goldSL` | `gold_sl` | float | 1.0 | min 0.0 |
| `us30SL` | `us30_sl` | float | 30.0 | min 0.0 |
| `nasdaqSL` | `nasdaq_sl` | float | 15.0 | min 0.0 |
| `wtiSL` | `wti_sl` | float | 0.15 | min 0.0 |
| `tpRR` | `tp_rr` | float | 1.0 | 0.1..10.0 |
| `swingPivotLen` | `swing_pivot_len` | int | 2 | 1..20 |
| `maxAfterBoxSwingBars` | `max_after_box_swing_bars` | int | 60 | 1..500 |
| `beTriggerR` | `be_trigger_r` | float | 0.85 | 0.1..5.0 |
| `spreadBuffer` | `spread_buffer` | float | 0.0 | min 0.0 |
