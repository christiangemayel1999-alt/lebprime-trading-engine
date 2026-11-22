"""Contract tests for the Linux MT5 gateway transport, using a fake MetaTrader5 module."""

from __future__ import annotations

import importlib
import socket
import sys
import threading
import types
from collections import namedtuple
from datetime import datetime, timezone

import numpy as np
import pytest

from trading_bot.mt5_transport import protocol
from trading_bot.mt5_transport.constants import MT5_CONSTANTS

KEY = b"k" * 64
AccountInfo = namedtuple("AccountInfo", "login server trade_mode balance")
TerminalInfo = namedtuple("TerminalInfo", "connected trade_allowed ping_last")
Tick = namedtuple("Tick", "time time_msc bid ask last volume")
TradeRequest = namedtuple("TradeRequest", "action symbol volume type price")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price comment request")
TradePosition = namedtuple("TradePosition", "ticket symbol volume type")


def _fake_mt5(trade_mode: int = 0, login: int = 1001) -> types.ModuleType:
    module = types.ModuleType("MetaTrader5")
    module.__dict__.update(MT5_CONSTANTS)
    module.__version__ = "fake"
    module.sent = []
    state = {"error": (1, "Success")}
    module.initialize = lambda **kw: True
    module.shutdown = lambda: None
    module.version = lambda: (500, 4000, "fake")
    module.login = lambda login, **kw: True
    module.last_error = lambda: state["error"]
    module.terminal_info = lambda: TerminalInfo(True, True, 12000)
    module.account_info = lambda: AccountInfo(login, "OneRoyal-Demo", trade_mode, 1000.0)
    module.symbol_info_tick = lambda s: Tick(1, 1000, 2000.1, 2000.3, 0.0, 0)
    module.symbol_select = lambda s, e=True: True
    module.positions_get = lambda **kw: (TradePosition(5, "XAUUSD", 0.01, 0),)
    dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
    module.copy_rates_from_pos = lambda s, tf, start, count: np.array([(1700000000 + i * 60, 1.0, 2.0, 0.5, 1.5, 10, 3, 0) for i in range(count)], dtype=dtype)
    module.history_deals_get = lambda a, b, **kw: ()

    def order_send(request):
        module.sent.append(dict(request))
        return OrderSendResult(10009, 77, 88, request["volume"], 2000.3, "done", TradeRequest(request["action"], request["symbol"], request["volume"], request["type"], request.get("price", 0.0)))

    module.order_send = order_send
    return module


@pytest.fixture
def gateway_factory(tmp_path, monkeypatch):
    servers = []

    def _start(trading: str = "enabled", symbols: str = "XAUUSD", max_volume: str = "1.0", **fake_kwargs):
        fake = _fake_mt5(**fake_kwargs)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        key_file = tmp_path / "gateway.key"
        key_file.write_text(KEY.decode())
        monkeypatch.setenv("LEBPRIME_MT5_GATEWAY_KEY_FILE", str(key_file))
        monkeypatch.setenv("LEBPRIME_GATEWAY_TRADING", trading)
        monkeypatch.setenv("LEBPRIME_GATEWAY_JOURNAL", str(tmp_path / "executions.jsonl"))
        monkeypatch.setenv("LEBPRIME_MT5_EXPECTED_LOGIN", "1001")
        monkeypatch.setenv("LEBPRIME_MT5_GATEWAY_PORT", "0")
        monkeypatch.setenv("LEBPRIME_GATEWAY_SYMBOLS", symbols)
        monkeypatch.setenv("LEBPRIME_GATEWAY_MAX_VOLUME", max_volume)
        gateway_server = importlib.reload(importlib.import_module("trading_bot.mt5_transport.gateway_server"))
        gateway = gateway_server.Gateway(gateway_server.GatewayConfig())
        server = gateway_server._Server(("127.0.0.1", 0), gateway)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server)
        from trading_bot.mt5_transport.client import GatewayMT5

        client = GatewayMT5("127.0.0.1", server.server_address[1], KEY, call_timeout=5, execution_timeout=5, reconcile_timeout=3)
        return client, fake, gateway, server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _buy():
    return {"action": MT5_CONSTANTS["TRADE_ACTION_DEAL"], "symbol": "XAUUSD", "volume": 0.01, "type": 0, "price": 2000.3, "deviation": 10, "magic": 234567, "comment": "t"}


def test_read_calls_return_native_shapes(gateway_factory):
    client, _fake, _gw, _srv = gateway_factory()
    assert client.initialize(login=1001, password="x", server="OneRoyal-Demo", path=r"C:\ignored\terminal64.exe") is True
    assert client.TIMEFRAME_H1 == 16385 and client.TRADE_RETCODE_DONE == 10009
    tick = client.symbol_info_tick("XAUUSD")
    assert tick.ask == pytest.approx(2000.3) and tick._asdict()["bid"] == pytest.approx(2000.1)
    rates = client.copy_rates_from_pos("XAUUSD", client.TIMEFRAME_M5, 0, 5)
    assert rates.dtype.names[0] == "time" and len(rates) == 5 and rates["close"][0] == 1.5
    positions = client.positions_get(symbol="XAUUSD")
    assert isinstance(positions, tuple) and positions[0].ticket == 5
    assert client.history_deals_get(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)) == ()
    assert client.last_error() == (1, "Success")


def test_order_send_once_and_nested_request(gateway_factory):
    client, fake, gateway, _srv = gateway_factory()
    result = client.order_send(_buy())
    assert result.retcode == 10009 and result.request.symbol == "XAUUSD"
    assert len(fake.sent) == 1
    assert gateway.journal.counts() == {"done": 1}


def test_trading_gate_disabled_blocks_and_reports(gateway_factory):
    client, fake, _gw, _srv = gateway_factory(trading="disabled")
    assert client.order_send(_buy()) is None
    assert client.last_error()[0] == protocol.ERR_TRADING_DISABLED
    assert fake.sent == []


def test_wrong_account_and_non_demo_block_execution(gateway_factory):
    client, fake, _gw, _srv = gateway_factory(login=999)
    assert client.order_send(_buy()) is None and client.last_error()[0] == protocol.ERR_WRONG_ACCOUNT
    client2, fake2, _gw2, _srv2 = gateway_factory(trade_mode=2)
    assert client2.order_send(_buy()) is None and client2.last_error()[0] == protocol.ERR_NOT_DEMO
    assert fake.sent == [] and fake2.sent == []


def test_reduce_only_allows_closing(gateway_factory):
    client, fake, _gw, _srv = gateway_factory(trading="reduce_only")
    assert client.order_send(_buy()) is None
    close = dict(_buy(), type=1, position=5)
    assert client.order_send(close).retcode == 10009 and len(fake.sent) == 1


def test_repeated_request_id_is_never_resent(gateway_factory):
    _client, fake, gateway, _srv = gateway_factory()
    body = {"op": "order_send", "request_id": "a" * 32, "args": [_buy()], "kwargs": {}, "deadline": 4102444800}
    first = gateway.handle(body)
    second = gateway.handle(body)
    assert first == second and len(fake.sent) == 1


def test_in_flight_after_restart_is_ambiguous(gateway_factory, tmp_path):
    _client, fake, gateway, _srv = gateway_factory()
    gateway.journal.claim("b" * 32, _buy())  # simulate a crash between claim and result
    from trading_bot.mt5_transport.gateway_server import ExecutionJournal

    reloaded = ExecutionJournal(gateway.journal.path)
    assert reloaded.status("b" * 32)["state"] == "ambiguous"
    gateway.journal = reloaded
    reply = gateway.handle({"op": "order_send", "request_id": "b" * 32, "args": [_buy()], "kwargs": {}, "deadline": 4102444800})
    assert reply["ok"] is False and reply["code"] == protocol.ERR_EXECUTION_AMBIGUOUS and fake.sent == []


def test_lost_reply_reconciles_without_duplicate(gateway_factory, monkeypatch):
    client, fake, _gw, _srv = gateway_factory()
    real_recv = protocol.recv_frame
    calls = {"n": 0}

    def flaky_recv(sock, key, nonces=None):
        calls["n"] += 1
        if calls["n"] == 1:  # drop the first reply (the order_send result)
            real_recv(sock, key, nonces)
            raise ConnectionError("reply lost")
        return real_recv(sock, key, nonces)

    monkeypatch.setattr("trading_bot.mt5_transport.client.protocol.recv_frame", flaky_recv)
    result = client.order_send(_buy())
    assert result is not None and result.retcode == 10009
    assert len(fake.sent) == 1


def test_expired_deadline_is_not_sent(gateway_factory):
    _client, fake, gateway, _srv = gateway_factory()
    reply = gateway.handle({"op": "order_send", "request_id": "c" * 32, "args": [_buy()], "kwargs": {}, "deadline": 1})
    assert reply["code"] == protocol.ERR_DEADLINE_EXCEEDED and fake.sent == []


def test_unsigned_and_replayed_frames_are_dropped(gateway_factory):
    _client, _fake, _gw, server = gateway_factory()
    port = server.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        protocol.send_frame(sock, b"x" * 64, {"op": "account_info", "id": "1", "args": [], "kwargs": {}, "deadline": 4102444800})
        sock.settimeout(2)
        assert sock.recv(4) == b""  # wrong key: connection closed, no reply
    envelope = protocol.sign(KEY, {"op": "account_info", "id": "2", "args": [], "kwargs": {}, "deadline": 4102444800})
    nonces = protocol.NonceCache()
    protocol.verify(KEY, envelope, nonces)
    with pytest.raises(protocol.ProtocolError):
        protocol.verify(KEY, envelope, nonces)


def test_unknown_operation_rejected(gateway_factory):
    _client, _fake, gateway, _srv = gateway_factory()
    reply = gateway.handle({"op": "eval", "args": ["1+1"], "kwargs": {}, "deadline": 4102444800})
    assert reply["ok"] is False and "not allowed" in reply["error"]


def test_auto_transport_keeps_native_behaviour(monkeypatch):
    from trading_bot import mt5_transport

    monkeypatch.delenv("LEBPRIME_MT5_TRANSPORT", raising=False)
    module, _err = mt5_transport.load_mt5_module()
    assert module is None or not hasattr(module, "transport_name")
    monkeypatch.setenv("LEBPRIME_MT5_TRANSPORT", "gateway")
    module, err = mt5_transport.load_mt5_module()
    assert err is None and module.transport_name == "gateway"


def test_symbol_allow_list_and_per_symbol_volume_caps(gateway_factory):
    client, fake, _gw, _srv = gateway_factory(symbols="XAUUSD,XAGUSD,US30,USTEC", max_volume="XAUUSD=0.5,US30=5,*=1")
    assert client.order_send(dict(_buy(), symbol="BTCUSD")) is None and "allow-listed" in client.last_error()[1]
    assert client.order_send(dict(_buy(), volume=0.6)) is None and "0.5" in client.last_error()[1]
    assert client.order_send(dict(_buy(), symbol="US30", volume=4.0)).retcode == 10009
    assert client.order_send(dict(_buy(), symbol="USTEC", volume=1.5)) is None
    assert client.order_send(dict(_buy(), symbol="XAGUSD", volume=1.0)).retcode == 10009
    assert [r["symbol"] for r in fake.sent] == ["US30", "XAGUSD"]
