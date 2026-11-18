from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router


class _Operator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def _record(self, name: str, value: object = None) -> dict[str, object]:
        self.calls.append((name, value))
        return {"ok": True, "message": f"{name} accepted"}

    def start_bot(self, actor: str) -> dict[str, object]: return self._record("start", actor)
    def stop_bot(self, actor: str, force: bool = False) -> dict[str, object]: return self._record("stop", force)
    def pause_execution(self, actor: str) -> dict[str, object]: return self._record("pause", actor)
    def resume_execution(self, actor: str) -> dict[str, object]: return self._record("resume", actor)
    def set_kill_switch(self, actor: str, enabled: bool, reason: str | None = None) -> dict[str, object]: return self._record("kill", (enabled, reason))
    def switch_mode(self, actor: str, trading_mode: str, confirmation_text: str | None = None) -> dict[str, object]: return self._record("mode", (trading_mode, confirmation_text))
    def apply_preset(self, actor: str, preset_name: str) -> dict[str, object]: return self._record("preset", preset_name)


def _post_json(app: FastAPI, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    async def request() -> tuple[int, dict[str, object]]:
        messages: list[dict[str, object]] = []
        payload = json.dumps(body).encode("utf-8")

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        await app(
            {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
                "query_string": b"", "headers": [(b"content-type", b"application/json")],
                "client": ("testclient", 50000), "server": ("testserver", 80), "root_path": "",
            },
            receive,
            send,
        )
        status = next(int(message["status"]) for message in messages if message["type"] == "http.response.start")
        raw = b"".join(bytes(message.get("body", b"")) for message in messages if message["type"] == "http.response.body")
        return status, json.loads(raw.decode("utf-8") or "{}")

    return asyncio.run(request())


def test_all_visible_runtime_actions_reach_their_backend_handlers(tmp_path: Path) -> None:
    operator = _Operator()
    config_manager = SimpleNamespace(dashboard_view=lambda: {"dashboard": {"readonly_mode": False, "stale_heartbeat_seconds": 120}})
    services = {
        "config_manager": config_manager,
        "control_state": SimpleNamespace(summarize=lambda **_kwargs: {}),
        "control_plane": SimpleNamespace(),
        "bot_control": SimpleNamespace(),
        "operator_service": operator,
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": SimpleNamespace(),
        "database": SimpleNamespace(),
        "audit_service": SimpleNamespace(),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {}),
        "base_dir": tmp_path,
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "dashboard" / "templates"))
    app.include_router(create_router(services, templates))

    requests = [
        ("/api/control/start", {"actor": "test", "enabled": True}),
        ("/api/control/stop", {"actor": "test", "enabled": True}),
        ("/api/control/pause", {"actor": "test", "enabled": True}),
        ("/api/control/resume", {"actor": "test", "enabled": True}),
        ("/api/control/mode", {"actor": "test", "trading_mode": "DRY_RUN"}),
        ("/api/control/mode", {"actor": "test", "trading_mode": "LIVE", "confirmation_text": "LIVE"}),
        ("/api/control/kill-switch", {"actor": "test", "enabled": True, "reason": "test"}),
        ("/api/config/preset", {"actor": "test", "preset_name": "SAFE_MODE"}),
    ]
    for path, body in requests:
        status, payload = _post_json(app, path, body)
        assert status == 200
        assert payload["ok"] is True

    assert [name for name, _ in operator.calls] == ["start", "stop", "pause", "resume", "mode", "mode", "kill", "preset"]
    assert operator.calls[4][1] == ("DRY_RUN", None)
    assert operator.calls[5][1] == ("LIVE", "LIVE")
    assert operator.calls[7][1] == "SAFE_MODE"
