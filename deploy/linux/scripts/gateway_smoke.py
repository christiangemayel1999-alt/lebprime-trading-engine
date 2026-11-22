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
    parser.add_argument("--symbols", default="XAUUSD,XAGUSD,US30,USTEC", help="Version 1 symbols, broker spelling")
    parser.add_argument("--order-symbol", default="XAUUSD", help="symbol for the --demo-order round trip")
    parser.add_argument("--demo-order", action="store_true")
    args = parser.parse_args()
    mt5 = GatewayMT5()
    results = []

    health = mt5.gateway_health()
    results.append(check("gateway answers", health is not None, mt5.last_error() if health is None else ""))
    if health is None:
        return 1
    results.append(check("terminal connected to broker", bool(health.get("terminal_connected")), f"ping {health.get('terminal_ping_ms')} ms"))
    results.append(check("gateway trading gate", True, health.get("trading_gate")))
    account = mt5.account_info()
    results.append(check("account info", account is not None, f"{getattr(account, 'login', None)} @ {getattr(account, 'server', None)}"))
    is_demo = getattr(account, "trade_mode", None) == mt5.ACCOUNT_TRADE_MODE_DEMO
    results.append(check("account is demo", is_demo))
    infos = {}
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        selected = bool(mt5.symbol_select(symbol, True))
        info = mt5.symbol_info(symbol)
        results.append(check(f"{symbol}: exists at broker", selected and info is not None))
        if info is None:
            stem = "".join(ch for ch in symbol if ch.isalpha())[:5]
            matches = [x.name for x in (mt5.symbols_get(group=f"*{stem}*") or ())][:10]
            print(f"       broker symbols like {stem!r}: {matches}  (use the exact name in LEBPRIME_GATEWAY_SYMBOLS and config)")
            continue
        infos[symbol] = info
        print(f"       digits={info.digits} contract={info.trade_contract_size} volume min/step/max={info.volume_min}/{info.volume_step}/{info.volume_max} "
              f"trade_mode={info.trade_mode} filling={info.filling_mode}")
        tick = mt5.symbol_info_tick(symbol)
        results.append(check(f"{symbol}: tick", tick is not None and tick.ask > 0, f"bid={getattr(tick, 'bid', None)} ask={getattr(tick, 'ask', None)}"))
        for name in ("TIMEFRAME_M1", "TIMEFRAME_M3", "TIMEFRAME_M5", "TIMEFRAME_M30", "TIMEFRAME_H1", "TIMEFRAME_H4"):
            rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, name), 0, 500)
            count = 0 if rates is None else len(rates)
            results.append(check(f"{symbol}: candles {name[10:]}", count >= 100, f"{count} bars"))

    if args.demo_order:
        if not is_demo:
            print("refusing to place a smoke order on a non-demo account")
            return 1
        order_symbol = args.order_symbol
        info = infos.get(order_symbol)
        if info is None:
            print(f"{order_symbol} is not available, cannot place the smoke order")
            return 1
        args.symbol = order_symbol
        volume = float(info.volume_min)
        mask = int(getattr(info, "filling_mode", 0) or 0)
        filling = mt5.ORDER_FILLING_IOC if mask & 2 else mt5.ORDER_FILLING_FOK if mask & 1 else mt5.ORDER_FILLING_RETURN
        buy = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": args.symbol, "volume": volume, "type": mt5.ORDER_TYPE_BUY,
            "price": float(mt5.symbol_info_tick(args.symbol).ask), "deviation": 30, "magic": SMOKE_MAGIC,
            "comment": "lebprime-smoke", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
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
