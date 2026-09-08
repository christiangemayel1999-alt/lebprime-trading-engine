from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router
from dashboard.services import DashboardDataService
from services.backtest_runner import BacktestRunner
from services.database import DatabaseService
from services.history_loader import HistoryResolution, HistoricalDataLoader
from trading_bot.core.execution_mode import V1_BASELINE, V2_FULL, V2_NO_LEBPRIM


def _frame(start: datetime, periods: int = 3, step_minutes: int = 1, base: float = 100.0) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq=f"{step_minutes}min", tz="UTC")
    rows = []
    for index, ts in enumerate(times):
        price = base + index * 0.25
        rows.append(
            {
                "time": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.1,
                "tick_volume": 100 + index,
                "spread": 0.2,
                "real_volume": 200 + index,
            }
        )
    return pd.DataFrame(rows)


def _write_csv_export(path: Path, frame: pd.DataFrame, *, sep: str = ",") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    export = frame.copy()
    if "time" in export.columns:
        timestamps = pd.to_datetime(export["time"], utc=True, errors="coerce")
        export["date"] = timestamps.dt.strftime("%Y-%m-%d")
        export["time"] = timestamps.dt.strftime("%H:%M:%S")
    export.to_csv(path, index=False, sep=sep)


class _LoaderLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


def _get_json(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    async def _request() -> tuple[int, dict[str, object]]:
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        query_string = b""
        if "?" in path:
            query_string = path.split("?", 1)[1].encode("ascii")
            path_only = path.split("?", 1)[0]
        else:
            path_only = path
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path_only,
            "raw_path": path_only.encode("ascii"),
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
        await app(scope, receive, send)
        status = next(int(message["status"]) for message in messages if message["type"] == "http.response.start")
        body = b"".join(bytes(message.get("body", b"")) for message in messages if message["type"] == "http.response.body")
        payload = json.loads(body.decode("utf-8") or "{}")
        return status, payload

    return __import__("asyncio").run(_request())


def _history_loader(tmp_path: Path, csv_dirs: list[str] | None = None) -> tuple[DatabaseService, HistoricalDataLoader, _LoaderLogger]:
    db = DatabaseService(tmp_path / "bot.db")
    logger = _LoaderLogger()
    csv_dirs = list(csv_dirs or [])
    config = {"storage": {"history_csv_fallback_dirs": csv_dirs}, "backtest": {"history_csv_fallback_dirs": csv_dirs}}
    loader = HistoricalDataLoader(tmp_path, config, db, logger)
    return db, loader, logger


def test_history_loader_reports_empty_sources_and_missing_csv(tmp_path: Path, monkeypatch) -> None:
    _, loader, _logger = _history_loader(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    requests = {"15min": (start, end, 1)}

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: pd.DataFrame(), raising=False)

    with pytest.raises(RuntimeError) as exc:
        loader.resolve_bar_bundle(connector=None, symbol="xauusd", timeframe_requests=requests)

    message = str(exc.value)
    assert "Unable to resolve historical dataset for XAUUSD" in message
    assert "mt5=mt5:failure" in message
    assert "cache=cache:failure" in message
    assert "csv=csv:failure" in message
    assert "no_csv_candidate_files_found" in message


def test_history_loader_reports_csv_files_outside_requested_range(tmp_path: Path, monkeypatch) -> None:
    db, loader, _logger = _history_loader(tmp_path, csv_dirs=["market_data"])
    csv_dir = tmp_path / "market_data"
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    past_start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    past_frame = _frame(past_start, periods=5)
    _write_csv_export(csv_dir / "XAUUSD_15min.csv", past_frame)
    requests = {"15min": (start, end, 1)}

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: pd.DataFrame(), raising=False)

    with pytest.raises(RuntimeError) as exc:
        loader.resolve_bar_bundle(connector=None, symbol="XAUUSD", timeframe_requests=requests)

    message = str(exc.value)
    assert "csv=csv:failure reason=CSV fallback files matched but none overlapped the requested range" in message
    assert "candidate_does_not_overlap_requested_range" in message


def test_history_loader_normalizes_timeframe_aliases_and_caches_canonical_labels(tmp_path: Path, monkeypatch) -> None:
    db, loader, _logger = _history_loader(tmp_path, csv_dirs=["market_data"])
    csv_dir = tmp_path / "market_data"
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    csv_frame = _frame(start, periods=5, base=120.0)
    _write_csv_export(csv_dir / "XAUUSD_15min.csv", csv_frame)
    requests = {"15min": (start, end, 1)}

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: pd.DataFrame(), raising=False)

    result = loader.resolve_bar_bundle(connector=None, symbol="xauusd", timeframe_requests=requests)
    assert result.source_kind == "csv"
    assert list(result.bar_frames) == ["15min"]
    assert result.source_details["requested_timeframes"]["15min"]["canonical_timeframe"] == "M15"
    assert result.source_details["cache_db_path"] == str(db.db_path)
    assert result.source_details["source_attempts"]
    assert result.source_details["source_attempts"][-1]["source_kind"] == "csv"

    rows = db._query_dataframe(
        "SELECT symbol, timeframe FROM history_cache_bars WHERE symbol = ? AND timeframe = ? ORDER BY time ASC;",
        ("XAUUSD", "M15"),
    )
    assert len(rows) == 5
    assert all(row["symbol"] == "XAUUSD" for row in rows)
    assert all(row["timeframe"] == "M15" for row in rows)


def test_history_loader_falls_back_from_mt5_failure_to_csv(tmp_path: Path, monkeypatch) -> None:
    _, loader, _logger = _history_loader(tmp_path, csv_dirs=["market_data"])
    csv_dir = tmp_path / "market_data"
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    csv_frame = _frame(start, periods=5, base=130.0)
    _write_csv_export(csv_dir / "XAUUSD_15min.csv", csv_frame)
    requests = {"15min": (start, end, 1)}

    def failing_mt5(*_args, **_kwargs):
        raise RuntimeError("mt5 unavailable for regression test")

    monkeypatch.setattr(loader, "_load_mt5_bars", failing_mt5, raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: pd.DataFrame(), raising=False)

    result = loader.resolve_bar_bundle(connector=None, symbol="XAUUSD", timeframe_requests=requests)
    assert result.source_kind == "csv"
    assert result.source_details["source_attempts"][0]["source_kind"] == "mt5"
    assert "mt5 unavailable for regression test" in result.source_details["source_attempts"][0]["failure_reason"]


def test_history_loader_prefers_mt5_then_cache_then_csv(tmp_path: Path, monkeypatch) -> None:
    db, loader, _logger = _history_loader(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    requests = {"M1": (start, end, 1), "M5": (start, end, 1)}
    mt5_frame = _frame(start, periods=5)
    cache_frame = _frame(start, periods=5, base=110.0)
    csv_frame = _frame(start, periods=5, base=120.0)
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: mt5_frame.assign(time=mt5_frame["time"]), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: cache_frame.assign(time=cache_frame["time"]), raising=False)
    monkeypatch.setattr(loader, "_load_csv_bars", lambda *_args: csv_frame.assign(time=csv_frame["time"]), raising=False)

    result = loader.resolve_bar_bundle(connector=object(), symbol="XAUUSD", timeframe_requests=requests)
    assert result.source_kind == "mt5"
    assert list(result.bar_frames) == ["M1", "M5"]
    assert len(result.bar_frames["M1"]) == 5
    assert db.get_backtest_runs(limit=1) == []


def test_history_loader_falls_back_to_cache_when_mt5_unavailable(tmp_path: Path, monkeypatch) -> None:
    _, loader, _logger = _history_loader(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    requests = {"M1": (start, end, 1), "M5": (start, end, 1)}
    cache_frame = _frame(start, periods=5, base=110.0)

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: cache_frame.assign(time=cache_frame["time"]), raising=False)
    monkeypatch.setattr(loader, "_load_csv_bars", lambda *_args: pd.DataFrame(), raising=False)

    result = loader.resolve_bar_bundle(connector=None, symbol="XAUUSD", timeframe_requests=requests)
    assert result.source_kind == "cache"
    assert len(result.bar_frames["M1"]) == 5


def test_history_loader_uses_csv_when_mt5_and_cache_fail(tmp_path: Path, monkeypatch) -> None:
    _, loader, _logger = _history_loader(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    requests = {"M1": (start, end, 1), "M5": (start, end, 1)}
    csv_frame = _frame(start, periods=5, base=120.0)

    monkeypatch.setattr(loader, "_load_mt5_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", lambda *_args: pd.DataFrame(), raising=False)
    monkeypatch.setattr(loader, "_load_csv_bars", lambda *_args: csv_frame.assign(time=csv_frame["time"]), raising=False)

    result = loader.resolve_bar_bundle(connector=None, symbol="XAUUSD", timeframe_requests=requests)
    assert result.source_kind == "csv"
    assert len(result.bar_frames["M1"]) == 5


def test_history_loader_does_not_mix_sources_within_one_resolution(tmp_path: Path, monkeypatch) -> None:
    _, loader, _logger = _history_loader(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, 0, 4, tzinfo=timezone.utc)
    requests = {"M1": (start, end, 1), "M5": (start, end, 1)}
    cache_frame = _frame(start, periods=5, base=110.0)
    mt5_calls: list[str] = []
    cache_calls: list[str] = []

    def fake_mt5(_connector, _symbol, timeframe_label, *_args):
        mt5_calls.append(str(timeframe_label))
        if str(timeframe_label) == "M1":
            return _frame(start, periods=5, base=100.0)
        raise RuntimeError("mt5 unavailable")

    def fake_cache(_symbol, timeframe_label, *_args):
        cache_calls.append(str(timeframe_label))
        return cache_frame.assign(time=cache_frame["time"])

    monkeypatch.setattr(loader, "_load_mt5_bars", fake_mt5, raising=False)
    monkeypatch.setattr(loader, "_load_cached_bars", fake_cache, raising=False)
    monkeypatch.setattr(loader, "_load_csv_bars", lambda *_args: pd.DataFrame(), raising=False)

    result = loader.resolve_bar_bundle(connector=object(), symbol="XAUUSD", timeframe_requests=requests)
    assert result.source_kind == "cache"
    assert mt5_calls == ["M1", "M5"]
    assert cache_calls == ["M1", "M5"]


@pytest.mark.parametrize("execution_mode", [V1_BASELINE, V2_FULL, V2_NO_LEBPRIM])
def test_backtest_runner_emits_playback_frames_and_events(tmp_path: Path, monkeypatch, execution_mode: str) -> None:
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    config["bot"]["execution_mode"] = execution_mode
    config["backtest"]["warmup_bars"] = 10
    config["backtest"]["same_bar_sl_tp_rule"] = "tp_first"
    db = DatabaseService(tmp_path / "bot.db")

    class FakeConnector:
        def __init__(self, config, logger):
            self.config = config
            self.logger = logger

        def initialize(self):
            return True

        def shutdown(self):
            return None

        def get_symbol_spec(self):
            return {
                "point": 0.1,
                "contract_size": 1.0,
                "digits": 2,
                "volume_min": 0.1,
                "volume_max": 10.0,
                "volume_step": 0.1,
                "stops_level": 0,
                "freeze_level": 0,
                "filling_mode_raw": 0,
                "order_mode": 0,
                "filling_modes": [0],
            }

    class FakeDecisionEngine:
        def __init__(self, _config):
            pass

        def decide(self, candidate, entry, market_context):
            return SimpleNamespace(
                action="EXECUTE",
                order_type="market",
                entry_mode="confirmed",
                reason_code="entry_valid",
                reason="ok",
                entry_price=float(entry.get("entry_price", 100.0)),
                reference_price=float(entry.get("entry_price", 100.0)),
                pending_expiry_minutes=5,
                pending_expiry_bars=3,
                strategy_family=str(candidate["setup_family"]),
                quality_tier="A",
                management_profile="default",
                metadata={},
            )

    class FakeRiskEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def approve_order(self, candidate, entry, market_context, account_info, symbol_spec, spread_points, live_requested, reduced_risk):
            price = float(entry["entry_price"])
            return SimpleNamespace(
                approved=True,
                volume=0.5,
                reason_code="approved",
                metadata={
                    "trade_plan": {
                        "entry_price": price,
                        "stop_loss": price - 0.2,
                        "tp1": price + 0.2,
                        "tp2": price + 0.35,
                        "management_profile_key": "default",
                    },
                    "sizing": {"risk_amount": 50.0},
                },
            )

    class FakeRiskManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def rebase_trade_levels(self, trade_plan, final_entry_price, symbol_spec):
            rebased = dict(trade_plan)
            rebased["entry_price"] = float(final_entry_price)
            return rebased

        def evaluate_management_actions(self, **_kwargs):
            return []

        def fit_volume_to_margin(self, requested_volume, account_info, symbol_spec, margin_for_volume):
            return {
                "valid": True,
                "volume": float(requested_volume),
                "required_margin": float(margin_for_volume(float(requested_volume))),
            }

    class FakeExecutionService:
        def __init__(self, broker):
            self.broker = broker

        def submit_order(self, request):
            executor = self.broker.executor
            metadata = dict(request.metadata or {})
            executor.open_trade(
                strategy_name=request.strategy_name,
                symbol=request.symbol,
                side=request.side,
                open_time=metadata.get("open_time") or metadata.get("timestamp"),
                entry=float(request.entry_price),
                sl=float(request.stop_loss),
                tp=float(request.take_profit),
                volume=float(request.volume),
                setup=str(metadata.get("setup") or metadata.get("setup_family") or ""),
                metadata=metadata,
                tp1=float(metadata.get("tp1", request.take_profit)),
            )
            return {"ok": True, "order_id": "1"}

    class FakeReplayBroker:
        def __init__(self, executor):
            self.executor = executor

    class FakeStrategy:
        def __init__(self):
            self.emitted = False

        def refresh_config(self, *_args, **_kwargs):
            return None

        def prepare_trend_dataframe(self, df):
            return df.copy()

        def prepare_setup_dataframe(self, df):
            return df.copy()

        def prepare_trigger_dataframe(self, df):
            return df.copy()

        def analyze_market_context(self, now_utc, trend_df, setup_df, trigger_df, spread_points):
            return {
                "timestamp": now_utc.isoformat(),
                "session": {"session_name": "LONDON", "session_live_allowed": True, "session_quality_score": 90.0},
                "bias": {"direction": "LONG", "confidence": 80.0},
                "regime": {"regime_name": "TREND_CONTINUATION", "regime_confidence": 90.0, "live_allowed": True},
                "spread_points": spread_points,
            }

        def generate_setup_candidates(self, market_context, trend_df, setup_df, trigger_df, symbol_spec):
            if self.emitted:
                return []
            self.emitted = True
            return [
                {
                    "setup_valid": True,
                    "setup_family": "COMPRESSION_RELEASE",
                    "setup_score": 70.0,
                    "trend_score": 70.0,
                    "session_quality_score": 90.0,
                    "regime_confidence": 90.0,
                    "side": "LONG",
                    "atr_at_setup": 1.0,
                    "structure_level": 95.0,
                    "value_price": 100.0,
                    "setup_fingerprint": "replay-test",
                    "trigger_type": "COMPRESSION_RELEASE",
                    "strategy_control": {},
                    "regime_name": market_context["regime"]["regime_name"],
                    "session_name": market_context["session"]["session_name"],
                    "hour_utc": 13,
                }
            ]

        def choose_best_setup(self, candidates):
            return candidates[0] if candidates else None

        def evaluate_entry(self, candidate, trigger_df, symbol_spec, now_utc, live_profile):
            price = float(trigger_df.iloc[-1]["close"])
            return {
                "valid": True,
                "live_ready": True,
                "dry_run_ready": True,
                "entry_mode": "confirmed",
                "trigger_type": "COMPRESSION_RELEASE",
                "entry_price": price,
                "reason_code": "entry_valid",
                "reason": "ok",
                "blocked_reasons": [],
                "trigger_score": 60.0,
                "entry_score": 65.0,
                "trend_score": 70.0,
                "setup_score": 70.0,
                "session_quality_score": 90.0,
                "regime_quality_score": 90.0,
                "entry_threshold": 52.0,
                "min_score_to_trade": 52.0,
                "trigger_candle_atr": 1.0,
                "value_distance_atr": 0.1,
                "pending_entry_price": price,
            }

    monkeypatch.setattr("services.backtest_runner.MT5Connector", FakeConnector)
    monkeypatch.setattr("services.backtest_runner.ExecutionDecisionEngine", FakeDecisionEngine)
    monkeypatch.setattr("services.backtest_runner.RiskEngine", FakeRiskEngine)
    monkeypatch.setattr("services.backtest_runner.RiskManager", FakeRiskManager)
    monkeypatch.setattr("services.backtest_runner.ExecutionService", FakeExecutionService)
    monkeypatch.setattr("services.backtest_runner.ReplayBroker", FakeReplayBroker)
    monkeypatch.setattr("services.backtest_runner.build_strategy_engine", lambda *_args, **_kwargs: FakeStrategy())

    runner = BacktestRunner(tmp_path, config, db)

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    bars = _frame(start, periods=60)
    bars["high"] = bars["close"] + 0.6
    bars["low"] = bars["close"] - 0.4
    trend_tf = str(config["timeframes"]["trend"])
    setup_tf = str(config["timeframes"]["setup"])
    trigger_tf = str(config["timeframes"]["trigger"])

    resolution = HistoryResolution(
        source_kind="mt5",
        bar_frames={
            trend_tf: bars.copy(),
            setup_tf: bars.copy(),
            trigger_tf: bars.copy(),
        },
        source_details={"source_kind": "mt5", "timeframes": {trend_tf: {"rows": 60}, setup_tf: {"rows": 60}, trigger_tf: {"rows": 60}}},
    )
    runner.history_loader = SimpleNamespace(
        resolve_bar_bundle=lambda **_kwargs: resolution,
    )

    bundle = runner.run(
        {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-01",
            "end_date": "2026-04-01",
            "engine_mode": execution_mode,
            "enabled_strategies": ["XAU_BOT_COMPRESS"],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 0.0},
            "slippage_model": {"type": "fixed_points", "points": 0.0},
            "execution_model": "current_bar_close",
        }
    )

    assert bundle["playback_frames"]
    assert bundle["playback_events"]
    assert len(bundle["playback_frames"]) > 0
    assert len(bundle["playback_events"]) >= 0
    run_bundle = db.get_backtest_run(run_id=int(bundle["run"]["id"]))
    assert run_bundle is not None
    assert len(db.get_backtest_playback_frames(int(bundle["run"]["id"]))) > 0
    assert len(db.get_backtest_playback_events(int(bundle["run"]["id"]))) >= 0
    persisted_run = db.get_backtest_run(run_id=int(bundle["run"]["id"])) or {}
    persisted_metadata = json.loads(persisted_run.get("metadata_json") or "{}")
    assert persisted_metadata.get("history_resolution", {}).get("source_kind") == "mt5"
    event_types = {row["event_type"] for row in bundle["playback_events"]}
    assert "candidate_selected" in event_types
    assert "signal_detected" in event_types or "signal_blocked" in event_types
    assert "trade_opened" in event_types
    assert "trade_closed" in event_types


def test_replay_and_stream_apis_return_window_slices(tmp_path: Path) -> None:
    db = DatabaseService(tmp_path / "bot.db")
    run_id = db.create_backtest_run(
        {
            "created_at": "2026-04-01T00:00:00+00:00",
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-01T00:59:00+00:00",
            "enabled_strategies_json": ["XAU_BOT_COMPRESS"],
            "session_filter_json": {"enabled": False},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 0.0},
            "slippage_model": {"type": "fixed_points", "points": 0.0},
            "execution_model": "current_bar_close",
            "notes": "replay test",
            "state": "COMPLETED",
        }
    )
    db.update_backtest_run_state(run_id, "COMPLETED", progress_current=5, progress_total=5)
    for index in range(5):
        ts = f"2026-04-01T00:0{index}:00+00:00"
        db.insert_backtest_playback_frame(
            {
                "run_id": run_id,
                "bar_index": index,
                "timestamp": ts,
                "open": 100.0 + index,
                "high": 100.5 + index,
                "low": 99.5 + index,
                "close": 100.1 + index,
                "volume": 50 + index,
                "tick_volume": 50 + index,
                "spread": 0.2,
                "equity": 10000.0 + index,
                "balance": 10000.0,
                "floating_pnl": float(index),
                "open_positions_json": [],
                "pending_orders_json": [],
                "selected_candidate_json": {"bar_index": index},
                "candidate_summary_json": {"source_kind": "mt5"},
                "state_json": {"bar_index": index},
            }
        )
    db.insert_backtest_playback_event(
        {
            "run_id": run_id,
            "bar_index": 1,
            "timestamp": "2026-04-01T00:01:00+00:00",
            "event_type": "signal_detected",
            "strategy_name": "XAU_BOT_COMPRESS",
            "setup_family": "COMPRESSION_RELEASE",
            "side": "LONG",
            "price": 101.0,
            "reason_code": "entry_valid",
            "event_payload_json": {"reason_code": "entry_valid"},
        }
    )
    db.insert_backtest_playback_event(
        {
            "run_id": run_id,
            "bar_index": 3,
            "timestamp": "2026-04-01T00:03:00+00:00",
            "event_type": "trade_closed",
            "strategy_name": "XAU_BOT_COMPRESS",
            "setup_family": "COMPRESSION_RELEASE",
            "side": "LONG",
            "price": 103.0,
            "reason_code": "take_profit_exit",
            "event_payload_json": {"pnl": 1.2},
        }
    )

    dashboard_data = DashboardDataService(tmp_path, db)
    config = {
        "storage": {"database_path": "bot.db"},
        "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False},
        "bot": {"trading_mode": "BACKTEST"},
    }
    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: config),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {"runtime": {"mode": "BACKTEST"}}),
        "control_plane": SimpleNamespace(get_section=lambda *_args, **_kwargs: {"backtest": {}}, get_config=lambda: {}, update_config=lambda **_kwargs: {}, clear_pending_restart=lambda *_args, **_kwargs: {}),
        "bot_control": SimpleNamespace(request_config_reload=lambda *_args, **_kwargs: {"ok": True}),
        "operator_service": SimpleNamespace(),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": dashboard_data,
        "database": db,
        "audit_service": SimpleNamespace(log_action=lambda *_args, **_kwargs: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": tmp_path,
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _get_json(app, f"/api/backtests/{run_id}/replay/summary")
    assert status_code == 200
    assert payload["result"]["frames_count"] == 5
    assert payload["result"]["events_count"] == 2

    status_code, payload = _get_json(app, f"/api/backtests/{run_id}/replay/window?start_bar=1&end_bar=3")
    assert status_code == 200
    assert [row["bar_index"] for row in payload["result"]["frames"]] == [1, 2, 3]

    status_code, payload = _get_json(app, f"/api/backtests/{run_id}/replay/frame/2")
    assert status_code == 200
    assert payload["result"]["bar_index"] == 2

    db.update_backtest_run_state(run_id, "RUNNING", progress_current=3, progress_total=5)
    status_code, payload = _get_json(app, f"/api/backtests/{run_id}/stream/status")
    assert status_code == 200
    assert payload["result"]["latest_frame"]["bar_index"] == 4
    assert payload["result"]["latest_event"]["bar_index"] == 3

    status_code, payload = _get_json(app, f"/api/backtests/{run_id}/stream/window?after_bar=2")
    assert status_code == 200
    assert [row["bar_index"] for row in payload["result"]["frames"]] == [3, 4]
    assert payload["result"]["cursor"] == 4
