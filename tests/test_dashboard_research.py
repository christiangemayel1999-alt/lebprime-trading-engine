"""Read-only Research page: statistics, path safety, auth and no write routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from dashboard.auth import DashboardAuth
from dashboard.research import ResearchReports, create_research_router

TEMPLATES = Path(__file__).resolve().parents[1] / "dashboard" / "templates"


def _write_reports(root: Path) -> Path:
    phase = root / "reports" / "phase4"
    phase.mkdir(parents=True)
    (phase / "demo_trades.csv").write_text(
        "setting,side,exit_time,net_r\n"
        "Gold M5,BUY,2021-01-05T10:00:00+00:00,2.0\n"
        "Gold M5,SELL,2021-03-05T10:00:00+00:00,-1.0\n"
        "Gold M5,BUY,2022-02-01T10:00:00+00:00,-1.0\n"
        "Gold M5,SELL,2022-06-01T10:00:00+00:00,1.5\n"
        "US30 M3,BUY,2022-01-01T10:00:00+00:00,-0.5\n",
        encoding="utf-8",
    )
    (phase / "yearly.csv").write_text("symbol,tf,setting,side,year,net_r\nXAUUSD,M5,a,BUY,2020,1\nXAUUSD,M5,a,SELL,2021,-2\n", encoding="utf-8")
    (phase / "summary.csv").write_text("name,value\nx,1\n", encoding="utf-8")
    (phase / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "secret.txt").write_text("outside", encoding="utf-8")
    return root


def _client(tmp_path: Path, auth: DashboardAuth | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(create_research_router(_write_reports(tmp_path), (auth or DashboardAuth()).dependency, Jinja2Templates(directory=str(TEMPLATES))))
    return TestClient(app)


def test_trade_statistics_per_setting_year_and_side(tmp_path: Path) -> None:
    client = _client(tmp_path)
    summary = client.get("/api/research/trades", params={"path": "phase4/demo_trades.csv"}).json()
    gold = next(g for g in summary["groups"] if g["group"] == "Gold M5")
    assert gold["trades"] == 4 and gold["net_r"] == 1.5 and gold["win_rate"] == 50.0
    assert gold["buys"]["net_r"] == 1.0 and gold["sells"]["net_r"] == 0.5
    assert gold["max_dd_r"] == 2.0  # peak 2.0 -> trough 0.0
    detail = client.get("/api/research/trades/detail", params={"path": "phase4/demo_trades.csv", "group": "Gold M5"}).json()
    assert [(y["year"], y["net_r"]) for y in detail["years"]] == [(2021, 1.0), (2022, 0.5)]
    assert detail["equity"][-1] == 1.5 and detail["profitable_years"] == 2


def test_year_column_files_and_listing(tmp_path: Path) -> None:
    client = _client(tmp_path)
    files = {f["path"]: f["kind"] for f in client.get("/api/research/files").json()["files"]}
    assert files["phase4/demo_trades.csv"] == "trades" and files["phase4/yearly.csv"] == "trades" and files["phase4/summary.csv"] == "csv"
    detail = client.get("/api/research/trades/detail", params={"path": "phase4/yearly.csv", "group": "XAUUSD | M5 | a"}).json()
    assert [(y["year"], y["net_r"]) for y in detail["years"]] == [(2020, 1.0), (2021, -2.0)]
    preview = client.get("/api/research/preview", params={"path": "phase4/summary.csv"}).json()
    assert preview["columns"] == ["name", "value"]
    assert client.get("/research").status_code == 200


def test_paths_outside_reports_are_refused(tmp_path: Path) -> None:
    reports = ResearchReports(_write_reports(tmp_path) / "reports")
    for bad in ("../secret.txt", "/etc/passwd", "phase4/../../secret.txt"):
        with pytest.raises(HTTPException):
            reports.resolve(bad)


def test_requires_login_and_has_no_write_routes(tmp_path: Path) -> None:
    client = _client(tmp_path, DashboardAuth("user", "a-long-enough-password"))
    assert client.get("/api/research/files").status_code == 401
    assert client.get("/api/research/files", auth=("user", "a-long-enough-password")).status_code == 200
    app_routes = client.app.routes
    methods = {m for route in app_routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}
