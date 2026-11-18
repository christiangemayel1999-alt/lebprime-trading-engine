# Indicator Lab Architecture

Indicator Lab is an additive subsystem for testing Pine-derived LebPrime and VSS3 indicator behavior on immutable historical candles. It does not reuse `XAU_LEBPRIM`, does not mutate `config.json`, and has no broker execution path.

## Components

- `services/indicator_lab/dataset_service.py`: creates and verifies frozen candle CSV datasets under `storage/indicator_datasets/`.
- `services/indicator_lab/parameter_registry.py`: parameter schemas and Pine defaults.
- `services/indicator_lab/engine.py`: common deterministic engine contract.
- `services/indicator_lab/lebprime_engine.py`: LebPrime Boundary 9 lifecycle implementation.
- `services/indicator_lab/vss3_engine.py`: VSS3 S/D lifecycle implementation.
- `services/indicator_lab/campaign_service.py`: campaign matrix generation, test claims, result persistence, and replay payloads.
- Existing `services/worker.py`: now supports `INDICATOR_CAMPAIGN` jobs in the normal persistent worker loop.
- Existing dashboard router/UI: exposes `/api/indicator-lab/...` endpoints and a top-level Indicator Lab tab.

## Database Tables

- `indicator_datasets`: immutable dataset metadata, requested/effective range, file path, SHA256, status.
- `indicator_campaigns`: campaign lifecycle, dataset/indicator, fixed parameters, optimization grid, progress, best test.
- `indicator_campaign_tests`: one immutable parameter snapshot per generated configuration.
- `indicator_zones`: normalized zone lifecycle records.
- `indicator_zone_events`: normalized per-bar lifecycle events.

Tables are created with safe `CREATE TABLE IF NOT EXISTS` logic by `IndicatorLabService` and `IndicatorDatasetService`.

## Dataset Lifecycle

`CREATING -> READY` when MT5 candles are fetched, normalized to UTC, written as CSV, and hashed. `CREATING -> FAILED` if acquisition or validation fails. A `READY` dataset is immutable; tests load it from disk and verify the stored SHA256 before use.

## Campaign Lifecycle

Campaign states: `DRAFT`, `QUEUED`, `RUNNING`, `PAUSED`, `COMPLETED`, `PARTIAL`, `CANCELED`, `FAILED`.

Test states: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELED`.

The worker claims pending tests atomically, runs each with a fresh engine instance, persists zones/events/metrics, and continues after individual test failures. Completed tests are not rerun automatically.

## Engine Interface

Engines expose `default_parameters()`, `parameter_schema()`, `validate_parameters()`, `process_bar()`, `finalize()`, `zones()`, `events()`, and `metrics()`. They process candles in order only, so bar `N` never sees future candles except where Pine pivot confirmation itself requires delayed recognition.

## Parameter Snapshots

Every test stores:

- `parameter_snapshot_json`
- `parameter_hash`
- `engine_version`
- Pine reference hash
- dataset hash

This makes each result reproducible without touching global live configuration.

## Ranking Metrics

Win rate is `TP_HIT / resolved_trades`, where resolved trades are `TP_HIT`, `SL_HIT`, and `BE_EXIT`. Pending and canceled zones are excluded. Results also store `net_r`, `gross_r_won`, `gross_r_lost`, `profit_factor`, `average_r`, and drawdown over resolved R.

## Safety

Indicator Lab uses MT5 only for historical candle acquisition. Campaign execution loads frozen CSV files and writes SQLite rows; it never calls live broker order placement and never edits global bot configuration.

## Run Flow

1. Start the dashboard.
2. Open Indicator Lab.
3. Create an MT5 dataset.
4. Choose dataset and indicator.
5. Adjust fixed parameters and optional optimization ranges.
6. Preview generated configuration count.
7. Start campaign.
8. Keep the normal worker running; browser closure does not stop the campaign.

## Pine Parity Validation

The parameter registry is derived from `docs/indicator_reference/LebPrime_Final.pine` and `docs/indicator_reference/VSS3_KOSOVI_limit.pine`. The engines implement strategy-relevant lifecycle transitions and emit normalized zone events for replay inspection.
