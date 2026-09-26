# Pine Parity Notes

## LebPrime Lifecycle Inventory

LebPrime lifecycle:

`CANDIDATE -> ZONE_CREATED -> ZONE_ARMED -> LIMIT_CREATED -> ACTIVATED -> TP_HIT | SL_HIT`

Alternative terminal paths:

- `CANDIDATE -> ZONE_REJECTED` during confirmation invalidation.
- `ZONE_CREATED -> INVALIDATED` before arming.
- `LIMIT_CREATED -> CANCELED` when invalidated before activation.

Implemented rules include momentum edge detection, leg reset, compact-base detection, four refinement modes, ATR width filters, same-side overlap checks, confirmation delay, departure arming, minimum source age, one-touch activation, instrument SL buffers, TP/R, and same-bar TP/SL priority.

## VSS3 Lifecycle Inventory

VSS3 lifecycle:

`ZONE_CREATED -> LIMIT_CREATED -> ACTIVATED -> TP_HIT | SL_HIT | BE_EXIT`

Alternative terminal paths:

- `ZONE_CREATED -> INVALIDATED`
- `LIMIT_CREATED -> CANCELED`
- `SL_HIT -> FLIPPED -> LIMIT_CREATED` for one opposite flipped zone

Implemented rules include raw supply/demand momentum detection, zone creation from `momentumSpan + 1`, min distance between same-side zones, ATR max-width clamp, age-based limit visibility, distance-from-zone limit creation, limit activation, candle-close 50% body stop behavior, TP, BE exit tracking, and one-generation flips with flipped-zone distance protection.

## Known Gaps

- TradingView drawing object behavior, labels, and alert formatting are not reproduced because the Lab stores normalized data rather than visual Pine objects.
- VSS3 multi-timeframe `request.security` behavior is represented on the selected frozen dataset timeframe only in this pass.
- VSS3 `combineSDs` is exposed as a parameter but exact cross-timeframe combination rendering is not yet reproduced.
- Pine's `last_bar_index` scan-window semantics are approximated by processing the provided frozen dataset from the first bar; campaigns should freeze a range that includes adequate warmup.
- Pivot-derived VSS3 close stops are implemented with delayed confirmation, but exact TradingView tie behavior can vary on equal highs/lows.

These gaps are intentionally documented rather than silently approximated.
