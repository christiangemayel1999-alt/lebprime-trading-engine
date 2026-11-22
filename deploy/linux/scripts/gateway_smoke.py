#!/usr/bin/env python3
"""Compatibility-gate smoke test for MT5 under Wine (MIGRATION_PLAN.md, phase 2).

    sudo /opt/lebprime/venv/bin/python /opt/lebprime/deploy/scripts/gateway_smoke.py
    sudo /opt/lebprime/venv/bin/python /opt/lebprime/deploy/scripts/gateway_smoke.py --demo-order

Reads account, terminal, symbol, tick and candles through the gateway. With --demo-order it also buys the
minimum volume on a DEMO account and closes it again (gateway gate must be "enabled"). It refuses to place
an order on anything but a demo account.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, "/opt/lebprime/app")

from trading_bot.mt5_transport.client import GatewayMT5  # noqa: E402

SMOKE_MAGIC = 990001


def check(label: str, ok: bool, detail: object = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label} {detail}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--demo-order", action="store_true")
    args = parser.parse_args()
    mt5 = GatewayMT5()
    results = []

    health = mt5.gateway_health()
    results.append(check("gateway answers", health is not None, mt5.last_error() if health is None else ""))
    if health is None:
        return 1
    results.append(check("terminal connected to broker", bool(health.get("terminal_connected")), f"ping {health.get('terminal_ping_ms')} ms"))
    account = mt5.account_info()
    results.append(check("account info", account is not None, f"{getattr(account, 'login', None)} @ {getattr(account, 'server', None)}"))
    is_demo = getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO
    results.append(check("account is demo", is_demo))
    results.append(check("symbol_select", bool(mt5.symbol_select(args.symbol, True))))
    info = mt5.symbol_info(args.symbol)
    results.append(check("symbol_info", info is not None, f"digits={getattr(info, 'digits', None)} volume_min={getattr(info, 'volume_min', None)}"))
    tick = mt5.symbol_info_tick(args.symbol)
    results.append(check("tick", tick is not None and tick.ask > 0, f"bid={getattr(tick, 'bid', None)} ask={getattr(tick, 'ask', None)}"))
    for name in ("TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_H1", "TIMEFRAME_H4"):
        rates = mt5.copy_rates_from_pos(args.symbol, getattr(mt5, name), 0, 500)
        count = 0 if rates is None else len(rates)
        results.append(check(f"candles {name[10:]}", count >= 100, f"{count} bars, last time={None if not count else int(rates['time'][-1])}"))

    if args.demo_order:
        if not is_demo:
            print("refusing to place a smoke order on a non-demo account")
            return 1
        volume = float(info.volume_min)
        buy = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": args.symbol, "volume": volume, "type": mt5.ORDER_TYPE_BUY,
            "price": float(mt5.symbol_info_tick(args.symbol).ask), "deviation": 30, "magic": SMOKE_MAGIC,
            "comment": "lebprime-smoke", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(buy)
        opened = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        results.append(check("demo buy", opened, getattr(result, "retcode", mt5.last_error())))
        if opened:
            time.sleep(1)
            position = next((p for p in (mt5.positions_get(symbol=args.symbol) or ()) if p.magic == SMOKE_MAGIC), None)
            results.append(check("position visible", position is not None))
            if position is not None:
                close = dict(buy, type=mt5.ORDER_TYPE_SELL, price=float(mt5.symbol_info_tick(args.symbol).bid), position=int(position.ticket))
                closed = mt5.order_send(close)
                results.append(check("demo close", closed is not None and closed.retcode == mt5.TRADE_RETCODE_DONE, getattr(closed, "retcode", mt5.last_error())))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
