"""Dashboard strategy tests: requests are validated and queued as files; the dashboard never runs them."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.strategy_tests import create_strategy_test_router


def _client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    store = tmp_path / "M1"
    store.mkdir()
    (store / "XAUUSD.csv").write_text("timestamp_utc,open\n2026-11-24T19:16:00+00:00,1\n")
    app = FastAPI()
    app.include_router(create_strategy_test_router(tmp_path, lambda: None, None, dispatch, store))
    return TestClient(app), tmp_path / "reports" / "dashboard_tests", dispatch


BODY = {"symbols": ["XAUUSD", "us30"], "timeframe": "M5", "start": "2024-01-01", "end": "2025-01-01", "parameters": {"momentum_count": 3}}


def test_create_writes_request_and_lists_as_queued(tmp_path):
    client, root, _ = _client(tmp_path)
    out = client.post("/api/strategy-tests", json=BODY).json()
    test_id = out["test"]["id"]
    request = json.loads((root / test_id / "request.json").read_text())
    assert request["symbols"] == ["XAUUSD", "US30"] and request["parameters"] == {"momentum_count": 3}
    listing = client.get("/api/strategy-tests").json()
    assert listing["tests"][0]["state"] == "QUEUED"
    assert listing["data_until"]["XAUUSD"].startswith("2026-11-24T19:16")


def test_rejects_bad_input(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.post("/api/strategy-tests", json={**BODY, "symbols": ["BTCUSD"]}).status_code == 400
    assert client.post("/api/strategy-tests", json={**BODY, "timeframe": "M7"}).status_code == 400
    assert client.post("/api/strategy-tests", json={**BODY, "start": "2025-02-01"}).status_code == 400
    assert client.post("/api/strategy-tests", json={**BODY, "parameters": {"rm -rf": 1}}).status_code == 400
    assert client.post("/api/strategy-tests", json={**BODY, "parameters": {"momentum_count": 99}}).status_code == 400


def test_queue_limit_and_states(tmp_path):
    client, root, dispatch = _client(tmp_path)
    ids = [client.post("/api/strategy-tests", json=BODY).json()["test"]["id"] for _ in range(3)]
    assert client.post("/api/strategy-tests", json=BODY).status_code == 429
    (dispatch / f"{ids[0]}.unit").write_text("lebprime-research-20261124-200000")
    (root / ids[1] / "status.json").write_text(json.dumps({"state": "COMPLETED"}))
    (dispatch / f"{ids[1]}.done").write_text("success 2026-11-24T20:00:00Z")
    (dispatch / f"{ids[2]}.done").write_text("oom-kill 2026-11-24T20:00:00Z")
    states = {t["id"]: t["state"] for t in client.get("/api/strategy-tests").json()["tests"]}
    assert states == {ids[0]: "RUNNING", ids[1]: "COMPLETED", ids[2]: "FAILED"}


def test_cancel_and_unknown_ids(tmp_path):
    client, root, _ = _client(tmp_path)
    test_id = client.post("/api/strategy-tests", json=BODY).json()["test"]["id"]
    assert client.post(f"/api/strategy-tests/{test_id}/cancel").json()["test"]["state"] == "CANCELING"
    assert (root / test_id / "cancel").exists()
    assert client.get("/api/strategy-tests/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/strategy-tests/20260101-000000-zzzzzz").status_code == 404
