"""Runtime orchestration for the explicit ``lebprime_v3_4_0`` strategy."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.models import Candle, ZoneEvent, ZoneRecord
from services.lebprime_live.repository import LebPrimeLiveRepository
from services.lebprime_live.symbols import SYMBOLS, SymbolMapping, resolve_symbol
from services.lebprime_live.timeframes import TIMEFRAMES, aggregate_m45, normalize_lebprime_timeframe
from trading_bot.execution.gateway import LebPrimeBrokerGateway
from trading_bot.risk.contracts import LebPrimeRiskService


STRATEGY_NAME = "lebprime_v3_4_0"
ACCOUNT_NOT_CONFIRMED_DEMO = "ACCOUNT_NOT_CONFIRMED_DEMO"
AUDIT_EVENTS = {
    "CANDIDATE",
    "LIMIT_CREATED",
    "DUPLICATE_SUPPRESSED",
    "ENTRY1_ORDER_SENT",
    "ENTRY1_FILLED",
    "ENTRY1_ACTIVATED",
    "ENTRY2_ORDER_SENT",
    "ENTRY2_FILLED",
    "ENTRY2_ACTIVATED",
    "TP1_HIT",
    "TP2_HIT",
    "TP1_FINAL",
    "SL_HIT",
    "CANCELED",
    "EXPIRED",
    "ORDER_REJECTED",
    "REJECTED_BY_BROKER",
    "BLOCKED_PRECHECK",
    "RISK_BLOCKED",
}


class LiveLebPrimeEngine(LebPrimeEngine):
    """LebPrime engine with live logical-symbol ID prefixes only."""

    def _zone_id(self, demand: bool, bar: Candle) -> str:
        mapping = resolve_symbol(self.symbol)
        day = bar.timestamp[5:10].replace("-", "")
        side = "B" if demand else "S"
        timeframe = self.timeframe.replace("M", "") + "M" if self.timeframe.startswith("M") else self.timeframe
        return f"{mapping.id_prefix}-{timeframe}-{side}-{day}-LEB{self._sequence + 1:02d}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    timestamp = pd.to_datetime(value, utc=True)
    return timestamp.isoformat()


@dataclass(slots=True)
class LiveContext:
    logical_symbol: str
    mt5_symbol: str
    timeframe: str
    engine: LebPrimeEngine
    last_closed_candle: str | None = None
    last_scan_time: str | None = None
    last_error: str | None = None
    heartbeat: str | None = None
    status: str = "IDLE"
    event_cursor: int = 0
    candidate_cursor: int = 0
    bootstrapped: bool = False
    historical_active_trades: int = 0
    realtime_activations: int = 0
    last_execution_error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.logical_symbol}-{self.timeframe}"


class LebPrimeLiveService:
    """Own isolated engine contexts and bridge validated events to execution."""

    def __init__(
        self,
        config: dict[str, Any],
        db_path: str | Path,
        *,
        logger: Any | None = None,
        connector_factory: Callable[[SymbolMapping], LebPrimeBrokerGateway] | None = None,
        broker_gateway_factory: Callable[[SymbolMapping], LebPrimeBrokerGateway] | None = None,
        risk_manager: LebPrimeRiskService | None = None,
        risk_state: dict[str, Any] | None = None,
        notifier: Any | None = None,
        trade_journal: Any | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        # connector_factory remains as a compatibility alias for tests and older callers.
        self.connector_factory = connector_factory
        self.broker_gateway_factory = broker_gateway_factory or connector_factory
        self.risk_manager = risk_manager
        self.risk_state = risk_state if risk_state is not None else {}
        self.notifier = notifier
        self.trade_journal = trade_journal
        self.repository = LebPrimeLiveRepository(db_path)
        self.broker_gateways: dict[str, LebPrimeBrokerGateway] = {}
        self.contexts: dict[str, LiveContext] = {}
        self.started = False
        self.startup_block_reason: str | None = None
        self._build_contexts()

    def _strategy_config(self) -> dict[str, Any]:
        value = self.config.get(STRATEGY_NAME, {})
        return value if isinstance(value, dict) else {}

    def _parameters(self) -> dict[str, Any]:
        configured = self._strategy_config().get("parameters", {})
        parameters = dict(configured) if isinstance(configured, dict) else {}
        parameters.update({"gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0})
        return parameters

    def _selected_symbols(self) -> list[str]:
        configured = self._strategy_config().get("symbols", list(SYMBOLS))
        return [resolve_symbol(item).logical_symbol for item in configured]

    def _selected_timeframes(self) -> list[str]:
        configured = self._strategy_config().get("timeframes", list(TIMEFRAMES))
        return [normalize_lebprime_timeframe(item) for item in configured]

    def _build_contexts(self) -> None:
        existing = self.contexts
        contexts: dict[str, LiveContext] = {}
        parameters = self._parameters()
        for logical_symbol in self._selected_symbols():
            mapping = resolve_symbol(logical_symbol)
            for timeframe in self._selected_timeframes():
                key = f"{logical_symbol}-{timeframe}"
                contexts[key] = existing.get(key) or LiveContext(
                    logical_symbol=logical_symbol,
                    mt5_symbol=mapping.mt5_symbol,
                    timeframe=timeframe,
                    engine=LiveLebPrimeEngine(copy.deepcopy(parameters), symbol=logical_symbol, timeframe=timeframe),
                )
        self.contexts = contexts

    def refresh_config(self, config: dict[str, Any]) -> None:
        was_enabled = self.enabled
        self.config = config
        self._build_contexts()
        if was_enabled and not self.enabled:
            self.started = False
            self.startup_block_reason = None

    @property
    def enabled(self) -> bool:
        return bool(self._strategy_config().get("enabled", False))

    @property
    def live_requested(self) -> bool:
        return str(self._strategy_config().get("execution_mode", "DRY")).strip().upper() == "LIVE"

    @staticmethod
    def account_is_demo(account_info: Any, demo_trade_mode: int = 0) -> bool:
        if account_info is None:
            return False
        value = getattr(account_info, "trade_mode", None)
        return value is not None and int(value) == int(demo_trade_mode)

    def _connector(self, logical_symbol: str) -> LebPrimeBrokerGateway:
        mapping = resolve_symbol(logical_symbol)
        if logical_symbol not in self.broker_gateways:
            if self.broker_gateway_factory is None:
                raise RuntimeError("LebPrime broker gateway factory is not configured")
            self.broker_gateways[logical_symbol] = self.broker_gateway_factory(mapping)
        return self.broker_gateways[logical_symbol]

    def start(self) -> tuple[bool, str]:
        if not self.enabled:
            self.started = False
            self._persist_runtime_state(False, "STRATEGY_DISABLED")
            return False, "STRATEGY_DISABLED"
        failures: list[str] = []
        for logical_symbol in self._selected_symbols():
            mapping = resolve_symbol(logical_symbol)
            try:
                connector = self._connector(logical_symbol)
                if not connector.ensure_initialized():
                    failures.append(f"{mapping.mt5_symbol}: MT5_NOT_CONNECTED")
                    continue
                if not connector.ensure_symbol_selected(mapping.mt5_symbol, log_details=True):
                    failures.append(f"{mapping.mt5_symbol}: SYMBOL_NOT_AVAILABLE")
                    continue
                if self.live_requested:
                    account = connector.get_account_info()
                    demo_mode = int(getattr(getattr(connector, "mt5", None), "ACCOUNT_TRADE_MODE_DEMO", 0))
                    if not self.account_is_demo(account, demo_mode):
                        failures.append(f"{mapping.mt5_symbol}: {ACCOUNT_NOT_CONFIRMED_DEMO}")
            except Exception as exc:
                failures.append(f"{mapping.mt5_symbol}: {exc}")
        if failures:
            self.started = False
            self.startup_block_reason = "; ".join(failures)
            self._persist_runtime_state(False, self.startup_block_reason)
            self._log("lebprime_v3_4_0_start_blocked", {"reason": self.startup_block_reason}, level="ERROR")
            return False, self.startup_block_reason
        self.started = True
        self.startup_block_reason = None
        self._persist_runtime_state(True, None)
        self._log("lebprime_v3_4_0_started", {"contexts": len(self.contexts), "execution_mode": "LIVE" if self.live_requested else "DRY"})
        return True, "STARTED"

    def _log(self, event_type: str, payload: dict[str, Any], level: str = "INFO") -> None:
        if hasattr(self.logger, "structured"):
            self.logger.structured(event_type, payload, level=level)

    def _persist_runtime_state(self, started: bool, reason: str | None, now: datetime | None = None) -> None:
        timestamp = (now or _utc_now()).isoformat()
        self.repository.upsert_runtime_state(
            {
                "started": int(bool(started)),
                "pid": os.getpid() if started else None,
                "execution_mode": "LIVE" if self.live_requested else "DRY",
                "startup_block_reason": reason,
                "heartbeat": timestamp,
                "updated_at": timestamp,
            }
        )

    def _fetch_frame(self, context: LiveContext, now_utc: datetime) -> pd.DataFrame:
        connector = self._connector(context.logical_symbol)
        bars = max(120, int(self._strategy_config().get("bars_to_fetch", 360)))
        if context.timeframe == "M45":
            source = connector.fetch_rates("M15", bars * 3 + 6, min_rows=3)
            return aggregate_m45(source, now_utc=now_utc).tail(bars).reset_index(drop=True)
        return connector.fetch_rates(context.timeframe, bars, min_rows=1)

    def scan_once(
        self,
        now_utc: datetime | None = None,
        *,
        execution_enabled: bool = True,
        execution_block_reason: str | None = None,
    ) -> dict[str, Any]:
        now = now_utc or _utc_now()
        if not self.enabled:
            return self.status_snapshot()
        if not self.started:
            started, _ = self.start()
            if not started:
                return self.status_snapshot()
        for context in self.contexts.values():
            try:
                frame = self._fetch_frame(context, now)
                self.process_frame(
                    context.key,
                    frame,
                    execute_new=execution_enabled,
                    execution_block_reason=execution_block_reason,
                )
                context.status = "HEALTHY"
                context.last_error = None
            except Exception as exc:
                context.status = "ERROR"
                context.last_error = str(exc)
                self._log("lebprime_context_error", {"context": context.key, "error": str(exc)}, level="ERROR")
            finally:
                context.last_scan_time = now.isoformat()
                context.heartbeat = now.isoformat()
                self._persist_context(context, now)
        self._refresh_spread_status(now)
        if self.live_requested:
            self._reconcile_broker_execution(now)
            if execution_enabled:
                self._retry_due_placements(now)
        self._persist_runtime_state(True, None, now)
        return self.status_snapshot()

    def process_frame(
        self,
        context_key: str,
        frame: pd.DataFrame,
        *,
        execute_new: bool = True,
        execution_block_reason: str | None = None,
    ) -> None:
        context = self.contexts[context_key]
        if frame is None or frame.empty:
            return
        source = frame.copy()
        time_column = "time" if "time" in source.columns else "timestamp"
        source["time"] = pd.to_datetime(source[time_column], utc=True)
        source = source.sort_values("time").drop_duplicates("time", keep="last")
        if context.last_closed_candle:
            source = source[source["time"] > pd.Timestamp(context.last_closed_candle)]
        initial_bootstrap = not context.bootstrapped
        for _, row in source.iterrows():
            index = len(context.engine._bars)
            candle = Candle.from_row(index, {**row.to_dict(), "timestamp": row["time"].isoformat()})
            context.engine.process_bar(candle)
            context.last_closed_candle = candle.timestamp
            if not initial_bootstrap:
                self._consume_candidates(context)
                self._consume_events(context, execute=execute_new, execution_block_reason=execution_block_reason)
        if initial_bootstrap:
            self._consume_historical_events(context)
            context.historical_active_trades = int(context.engine.metrics().get("active_trades", 0) or 0)
            context.candidate_cursor = len(context.engine._candidate_diagnostics)
            context.event_cursor = len(context.engine.events())
        context.bootstrapped = True

    def process_closed_candle(self, context_key: str, candle: Candle, *, execute: bool = True) -> None:
        """Process one known-closed candle; useful for runtime adapters and tests."""
        context = self.contexts[context_key]
        context.bootstrapped = True
        normalized = Candle(
            index=len(context.engine._bars),
            timestamp=_iso(candle.timestamp),
            open=float(candle.open),
            high=float(candle.high),
            low=float(candle.low),
            close=float(candle.close),
            tick_volume=float(candle.tick_volume),
            spread=float(candle.spread),
            real_volume=float(candle.real_volume),
        )
        context.engine.process_bar(normalized)
        context.last_closed_candle = normalized.timestamp
        self._consume_candidates(context)
        self._consume_events(context, execute=execute, execution_block_reason=None if execute else "EXECUTION_DISABLED")

    def _consume_candidates(self, context: LiveContext) -> None:
        diagnostics = context.engine._candidate_diagnostics
        for candidate in diagnostics[context.candidate_cursor :]:
            self._record_event(
                context,
                str(candidate.get("candidate_id") or candidate.get("zone_id") or "UNKNOWN"),
                "CANDIDATE",
                str(candidate.get("candidate_time") or context.last_closed_candle or _utc_now().isoformat()),
                metadata={key: value for key, value in candidate.items() if key != "pending_bars_json"},
            )
        context.candidate_cursor = len(diagnostics)

    def _consume_historical_events(self, context: LiveContext) -> None:
        zones = {zone.engine_zone_id: zone for zone in context.engine.zones()}
        for event in context.engine.events():
            zone = zones.get(event.zone_engine_id)
            if zone is None:
                continue
            if event.event_type == "LIMIT_CREATED":
                self._register_limit(
                    context, zone, event, execute=False, historical=True,
                    execution_block_reason="HISTORICAL_REPLAY",
                )
            else:
                self._track_lifecycle(context, zone, event, execute=False, historical=True, execution_block_reason="HISTORICAL_REPLAY")

    def _consume_events(self, context: LiveContext, *, execute: bool, execution_block_reason: str | None) -> None:
        events = context.engine.events()
        zones = {zone.engine_zone_id: zone for zone in context.engine.zones()}
        for event in events[context.event_cursor :]:
            zone = zones.get(event.zone_engine_id)
            if zone is None:
                continue
            if event.event_type == "LIMIT_CREATED":
                self._register_limit(
                    context, zone, event, execute=execute, historical=False,
                    execution_block_reason=execution_block_reason,
                )
            else:
                self._track_lifecycle(
                    context,
                    zone,
                    event,
                    execute=execute,
                    historical=False,
                    execution_block_reason=execution_block_reason,
                )
        context.event_cursor = len(events)

    @staticmethod
    def _family_id(zone: ZoneRecord) -> str:
        digest = hashlib.sha1(zone.engine_zone_id.encode("utf-8")).hexdigest()[:12].upper()
        side = "B" if zone.side == "DEMAND" else "S"
        return f"LPF-{zone.symbol}-{side}-{digest}"

    def _zone_payload(self, context: LiveContext, zone: ZoneRecord, now: str) -> dict[str, Any]:
        existing = self.repository.zone(zone.engine_zone_id) or {}
        return {
            "zone_id": zone.engine_zone_id,
            "logical_symbol": context.logical_symbol,
            "mt5_symbol": context.mt5_symbol,
            "timeframe": context.timeframe,
            "side": "BUY" if zone.side == "DEMAND" else "SELL",
            "zone_bottom": zone.zone_bottom,
            "zone_top": zone.zone_top,
            "entry1": zone.entry1,
            "entry2": zone.entry2,
            "stop_loss": zone.sl_price,
            "tp1": zone.tp1_price,
            "tp2": zone.tp2_price,
            "buffer_used": zone.buffer_used,
            "family_id": existing.get("family_id"),
            "execution_primary": int(existing.get("execution_primary", 0)),
            "execution_status": existing.get("execution_status", "THEORETICAL"),
            "suppressed_by_zone_id": existing.get("suppressed_by_zone_id"),
            "duplicate_reason": existing.get("duplicate_reason"),
            "entry1_order_id": existing.get("entry1_order_id"),
            "entry2_order_id": existing.get("entry2_order_id"),
            "theoretical_state": zone.terminal_state or ("ACTIVE" if zone.activated_bar_index is not None else "PENDING"),
            "created_at": existing.get("created_at", zone.limit_created_time or now),
            "updated_at": now,
            "metadata": {
                "indicator_version": zone.indicator_version,
                "engine_version": zone.engine_version,
                "atr_at_source": zone.atr_at_source,
            },
        }

    def _register_limit(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        event: ZoneEvent,
        *,
        execute: bool,
        historical: bool = False,
        execution_block_reason: str | None = None,
    ) -> None:
        now = event.timestamp
        payload = self._zone_payload(context, zone, now)
        existing_zone = self.repository.zone(zone.engine_zone_id)
        if existing_zone is not None:
            if historical and not self.repository.broker_entries_for_zone(zone.engine_zone_id):
                if existing_zone.get("execution_status") == "PRIMARY_PENDING":
                    existing_zone["execution_status"] = "HISTORICAL_REPLAY"
                    existing_zone["updated_at"] = now
                    existing_zone["metadata"] = {"migrated_historical_replay": True}
                    self.repository.upsert_zone(existing_zone)
                self._persist_ineligible_entries(context, zone, existing_zone, "HISTORICAL_REPLAY", historical=True)
                return
            if (
                not historical
                and self.live_requested
                and execute
                and self.started
                and context.status in {"HEALTHY", "IDLE"}
            ):
                retry_payload = self._zone_payload(context, zone, now)
                retry_payload.update(
                    {
                        "family_id": existing_zone.get("family_id"),
                        "execution_primary": existing_zone.get("execution_primary", 1),
                        "execution_status": existing_zone.get("execution_status", "PRIMARY_PENDING"),
                        "entry1_order_id": existing_zone.get("entry1_order_id"),
                        "entry2_order_id": existing_zone.get("entry2_order_id"),
                        "created_at": existing_zone.get("created_at"),
                    }
                )
                self._execute_primary_safely(context, zone, retry_payload)
            else:
                self._log_pending_skips_for_existing(context, zone, existing_zone)
            return
        side = payload["side"]
        family_id = self._family_id(zone)
        family_payload = {
            "family_id": family_id,
            "logical_symbol": context.logical_symbol,
            "side": side,
            "zone_bottom": zone.zone_bottom,
            "zone_top": zone.zone_top,
            "primary_zone_id": zone.engine_zone_id,
            "status": "PENDING",
            "created_at": now,
            "updated_at": now,
        }
        self.repository.upsert_family(family_payload)
        initial_status = "HISTORICAL_REPLAY" if historical else "PRIMARY_PENDING"
        payload.update({"family_id": family_id, "execution_primary": 1, "execution_status": initial_status})
        self.repository.upsert_zone(payload)
        self._record_event(
            context, zone.engine_zone_id, "LIMIT_CREATED", now, price=event.price, family_id=family_id,
            metadata={"historical": bool(historical), "execution_eligible": bool(not historical and execute and self.live_requested)},
        )
        if historical:
            self._persist_ineligible_entries(context, zone, payload, "HISTORICAL_REPLAY", historical=True)
            return
        self._log(
            "lebprime_limit_created",
            {
                "context": context.key,
                "zone_id": zone.engine_zone_id,
                "symbol": context.logical_symbol,
                "timeframe": context.timeframe,
                "side": payload["side"],
                "entry1": zone.entry1,
                "entry2": zone.entry2,
                "sl": zone.sl_price,
                "tp1": zone.tp1_price,
                "tp2": zone.tp2_price,
                "historical": False,
            },
        )
        self._send_created_notification_once(context, zone)
        if not self.live_requested:
            payload["execution_status"] = "DRY_RUN_PRIMARY"
            self.repository.upsert_zone(payload)
            self._persist_ineligible_entries(context, zone, payload, "DRY_MODE")
            return
        if not execute:
            reason = execution_block_reason or "EXECUTION_DISABLED"
            payload["execution_status"] = "EXECUTION_SKIPPED"
            self.repository.upsert_zone(payload)
            context.last_execution_error = reason
            self._persist_ineligible_entries(context, zone, payload, reason)
            return
        if not self.started:
            payload["execution_status"] = "EXECUTION_SKIPPED"
            self.repository.upsert_zone(payload)
            context.last_execution_error = "RUNTIME_NOT_STARTED"
            self._persist_ineligible_entries(context, zone, payload, "RUNTIME_NOT_STARTED")
            return
        if context.status not in {"HEALTHY", "IDLE"}:
            payload["execution_status"] = "EXECUTION_SKIPPED"
            self.repository.upsert_zone(payload)
            context.last_execution_error = "CONTEXT_NOT_HEALTHY"
            self._persist_ineligible_entries(context, zone, payload, "CONTEXT_NOT_HEALTHY")
            return
        self._execute_primary_safely(context, zone, payload)

    def _notification_enabled(self, setting: str) -> bool:
        telegram = self.config.get("telegram", {})
        return bool(isinstance(telegram, dict) and telegram.get("enabled", False) and telegram.get(setting, False))

    def _send_notification_once(
        self,
        context: LiveContext,
        zone_id: str,
        notification_type: str,
        callback: Callable[[], bool],
        *,
        sent_event: str,
        failed_event: str,
        details: dict[str, Any],
    ) -> bool:
        key = f"{STRATEGY_NAME}:{context.key}:{zone_id}:{notification_type}"
        now = _utc_now().isoformat()
        try:
            reserved = self.repository.record_notification(
                {
                    "notification_key": key,
                    "context_key": context.key,
                    "zone_id": zone_id,
                    "notification_type": notification_type,
                    "status": "ATTEMPTING",
                    "failure_reason": None,
                    "attempted_at": now,
                    "sent_at": None,
                }
            )
        except Exception as exc:
            self._log(failed_event, {**details, "notification_key": key, "reason": f"NOTIFICATION_STATE_ERROR:{exc}"}, level="ERROR")
            return False
        if not reserved:
            try:
                existing = self.repository.notification(key) or {}
                return existing.get("status") == "SENT"
            except Exception as exc:
                self._log(failed_event, {**details, "notification_key": key, "reason": f"NOTIFICATION_LOOKUP_ERROR:{exc}"}, level="ERROR")
                return False
        try:
            sent = bool(callback())
            failure = None if sent else str(getattr(self.notifier, "last_error", None) or "TELEGRAM_DELIVERY_FAILED")
        except Exception as exc:
            sent = False
            failure = str(exc)
        try:
            self.repository.update_notification_outcome(
                key,
                status="SENT" if sent else "FAILED",
                failure_reason=failure,
                sent_at=_utc_now().isoformat() if sent else None,
            )
        except Exception as exc:
            failure = f"NOTIFICATION_OUTCOME_STATE_ERROR:{exc}"
        event_payload = {**details, "notification_key": key}
        if failure:
            event_payload["reason"] = failure
        self._log(sent_event if sent else failed_event, event_payload, level="INFO" if sent else "WARNING")
        return sent

    def _send_created_notification_once(self, context: LiveContext, zone: ZoneRecord) -> bool:
        details = {"context": context.key, "zone_id": zone.engine_zone_id}
        if not self._notification_enabled("signal_alerts") or self.notifier is None:
            return self._send_notification_once(
                context,
                zone.engine_zone_id,
                "CREATED",
                lambda: False,
                sent_event="lebprime_telegram_created_sent",
                failed_event="lebprime_telegram_created_failed",
                details={**details, "configured": False},
            )
        data = {
            "symbol": context.logical_symbol,
            "timeframe": context.timeframe,
            "order_type": "BUY LIMIT" if zone.side == "DEMAND" else "SELL LIMIT",
            "entry1": zone.entry1,
            "entry2": zone.entry2,
            "sl": zone.sl_price,
            "tp1": zone.tp1_price,
            "tp2": zone.tp2_price,
            "zone_id": zone.engine_zone_id,
        }
        return self._send_notification_once(
            context,
            zone.engine_zone_id,
            "CREATED",
            lambda: self.notifier.send_lebprime_zone_created(data),
            sent_event="lebprime_telegram_created_sent",
            failed_event="lebprime_telegram_created_failed",
            details=details,
        )

    def _preflight(self, context: LiveContext, connector: Any) -> tuple[bool, str, Any | None]:
        if not connector.is_connected() and not connector.ensure_connection():
            return False, "MT5_NOT_CONNECTED", None
        account = connector.get_account_info()
        demo_mode = int(getattr(getattr(connector, "mt5", None), "ACCOUNT_TRADE_MODE_DEMO", 0))
        if not self.account_is_demo(account, demo_mode):
            return False, ACCOUNT_NOT_CONFIRMED_DEMO, account
        if not bool(getattr(account, "trade_allowed", False)):
            return False, "ACCOUNT_TRADING_NOT_ALLOWED", account
        terminal = connector.get_terminal_info()
        if terminal is not None and not bool(getattr(terminal, "trade_allowed", True)):
            return False, "TERMINAL_TRADING_NOT_ALLOWED", account
        if not connector.ensure_symbol_selected(context.mt5_symbol):
            return False, "SYMBOL_NOT_TRADABLE", account
        max_symbol_exposure = int(self.config.get("risk", {}).get("max_trades_per_symbol", 0) or 0)
        if max_symbol_exposure > 0:
            positions = connector.get_bot_positions()
            pending_orders = connector.get_pending_orders()
            if len(positions) + len(pending_orders) >= max_symbol_exposure:
                return False, "SYMBOL_EXPOSURE_LIMIT", account
        return True, "OK", account

    def _spread_limit(self, logical_symbol: str, broker_symbol: str | None = None) -> float:
        strategy = self._strategy_config()
        configured = strategy.get("max_spread_points_by_symbol", {})
        if isinstance(configured, dict):
            for key in (logical_symbol, broker_symbol):
                if key and configured.get(key) not in (None, ""):
                    return float(configured[key])
        return float(strategy.get("max_spread_points", self.config.get("execution", {}).get("max_spread_points", 25.0)))

    def _persist_spread_status(self, context: LiveContext, connector: Any, stage: dict[str, Any], limit: float) -> None:
        payload = stage.get("payload", {}) if isinstance(stage, dict) else {}
        tick = payload.get("tick", {}) if isinstance(payload.get("tick", {}), dict) else {}
        try:
            spec = connector.get_symbol_spec()
        except Exception:
            spec = {}
        bid = tick.get("bid")
        ask = tick.get("ask")
        point = float(spec.get("point", 0.0) or 0.0)
        spread_price = (float(ask) - float(bid)) if ask is not None and bid is not None else None
        self.repository.upsert_spread_status(
            {
                "logical_symbol": context.logical_symbol,
                "broker_symbol": context.mt5_symbol,
                "bid": bid,
                "ask": ask,
                "point": point,
                "digits": spec.get("digits"),
                "spread_price": spread_price,
                "spread_points": payload.get("spread_points"),
                "max_spread_points": float(limit),
                "status": "PASS" if stage.get("ok") else "TELEMETRY_WIDE",
                "rejection_reason": None if stage.get("ok") else str(stage.get("reason_code") or "SPREAD_CHECK_FAILED"),
                "updated_at": _utc_now().isoformat(),
            }
        )

    def _refresh_spread_status(self, now: datetime) -> None:
        for logical_symbol in self._selected_symbols():
            context = next((item for item in self.contexts.values() if item.logical_symbol == logical_symbol), None)
            if context is None:
                continue
            try:
                connector = self._connector(logical_symbol)
                limit = self._spread_limit(logical_symbol, context.mt5_symbol)
                stage = connector.spread_check_stage(limit)
                self._persist_spread_status(context, connector, stage, limit)
            except Exception as exc:
                self.repository.upsert_spread_status(
                    {
                        "logical_symbol": logical_symbol, "broker_symbol": context.mt5_symbol,
                        "status": "UNAVAILABLE", "rejection_reason": str(exc),
                        "max_spread_points": self._spread_limit(logical_symbol, context.mt5_symbol),
                        "updated_at": now.isoformat(),
                    }
                )

    @staticmethod
    def _entry_specs(zone: ZoneRecord) -> list[tuple[int, str, float]]:
        values = [(1, "ENTRY1", zone.entry1), (2, "ENTRY2", zone.entry2)]
        return [(number, label, float(value)) for number, label, value in values if value is not None]

    @staticmethod
    def _idempotency_key(context_key: str, zone_id: str, entry_number: int) -> str:
        return f"{STRATEGY_NAME}:{context_key}:{zone_id}:{int(entry_number)}"

    def _broker_entry_payload(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        entry_number: int,
        entry: float,
        *,
        status: str,
        historical: bool = False,
        eligible: bool = False,
        failure_reason: str | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        existing = self.repository.broker_entry(zone.engine_zone_id, entry_number) or {}
        label = f"ENTRY{entry_number}"
        now = _utc_now().isoformat()
        payload = {
            "idempotency_key": self._idempotency_key(context.key, zone.engine_zone_id, entry_number),
            "zone_id": zone.engine_zone_id,
            "context_key": context.key,
            "entry_number": entry_number,
            "logical_symbol": context.logical_symbol,
            "broker_symbol": context.mt5_symbol,
            "side": "BUY" if zone.side == "DEMAND" else "SELL",
            "order_type": "BUY_LIMIT" if zone.side == "DEMAND" else "SELL_LIMIT",
            "entry_price": entry,
            "normalized_price": existing.get("normalized_price"),
            "volume": existing.get("volume"),
            "raw_calculated_volume": existing.get("raw_calculated_volume"),
            "requested_volume": existing.get("requested_volume"),
            "minimum_volume": existing.get("minimum_volume"),
            "required_margin": existing.get("required_margin"),
            "free_margin": existing.get("free_margin"),
            "projected_margin_level": existing.get("projected_margin_level"),
            "required_margin_level": existing.get("required_margin_level"),
            "margin_diagnostics_json": existing.get("margin_diagnostics_json"),
            "sl": existing.get("sl", zone.sl_price),
            "tp": existing.get("tp", zone.tp2_price),
            "mt5_order_ticket": existing.get("mt5_order_ticket"),
            "mt5_deal_ticket": existing.get("mt5_deal_ticket"),
            "mt5_position_ticket": existing.get("mt5_position_ticket"),
            "fill_price": existing.get("fill_price"),
            "broker_status": status,
            "broker_created_at": existing.get("broker_created_at"),
            "broker_filled_at": existing.get("broker_filled_at"),
            "broker_closed_at": existing.get("broker_closed_at"),
            "close_price": existing.get("close_price"),
            "realized_pnl": existing.get("realized_pnl"),
            "partial_realized_pnl": existing.get("partial_realized_pnl"),
            "broker_profit": existing.get("broker_profit"),
            "broker_commission": existing.get("broker_commission"),
            "broker_swap": existing.get("broker_swap"),
            "broker_fee": existing.get("broker_fee"),
            "final_exit_reason": existing.get("final_exit_reason"),
            "broker_retcode": existing.get("broker_retcode"),
            "broker_submitted_at": existing.get("broker_submitted_at"),
            "canceled_at": existing.get("canceled_at"),
            "expired_at": existing.get("expired_at"),
            "filled_volume": existing.get("filled_volume"),
            "closed_volume": existing.get("closed_volume"),
            "remaining_volume": existing.get("remaining_volume"),
            "partial_close_count": int(existing.get("partial_close_count", 0) or 0),
            "outcome_complete": int(existing.get("outcome_complete", 0) or 0),
            "position_model": existing.get("position_model"),
            "close_deal_tickets_json": existing.get("close_deal_tickets_json"),
            "lifecycle_details_json": existing.get("lifecycle_details_json"),
            "failure_reason": failure_reason,
            "historical": int(bool(historical)),
            "execution_eligible": int(bool(eligible)),
            "comment": self._order_comment(zone.engine_zone_id, label),
            "attempt_count": int(existing.get("attempt_count", 0) or 0),
            "last_attempt_at": existing.get("last_attempt_at"),
            "last_failure": existing.get("last_failure"),
            "retryable": int(existing.get("retryable", 0) or 0),
            "next_retry_at": existing.get("next_retry_at"),
            "updated_at": now,
        }
        payload.update(updates)
        return payload

    def _persist_ineligible_entries(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        zone_payload: dict[str, Any],
        reason: str,
        *,
        historical: bool = False,
    ) -> None:
        status = "HISTORICAL_REPLAY" if historical else "NOT_PLACED"
        for entry_number, _, entry in self._entry_specs(zone):
            self.repository.upsert_broker_entry(
                self._broker_entry_payload(
                    context, zone, entry_number, entry, status=status, historical=historical,
                    eligible=False, failure_reason=reason,
                )
            )
            self._log(
                "lebprime_pending_order_skipped",
                {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "reason": reason},
                level="INFO" if historical or reason == "DRY_MODE" else "WARNING",
            )

    def _log_pending_skips_for_existing(self, context: LiveContext, zone: ZoneRecord, row: dict[str, Any]) -> None:
        for entry_number, _, _ in self._entry_specs(zone):
            existing = self.repository.broker_entry(zone.engine_zone_id, entry_number)
            key = self._idempotency_key(context.key, zone.engine_zone_id, entry_number)
            self._log(
                "lebprime_duplicate_check",
                {
                    "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                    "idempotency_key": key, "matched_existing_key": (existing or {}).get("idempotency_key"),
                    "matched_ticket": (existing or {}).get("mt5_order_ticket"), "decision": "SUPPRESS",
                    "reason": "EXACT_ZONE_EVENT_ALREADY_PERSISTED",
                },
            )
            self._log(
                "lebprime_pending_order_skipped",
                {
                    "context": context.key,
                    "zone_id": zone.engine_zone_id,
                    "entry_number": entry_number,
                    "reason": "IDEMPOTENCY_DUPLICATE",
                    "broker_status": (existing or {}).get("broker_status") or row.get("execution_status"),
                },
            )

    @staticmethod
    def _item_value(item: Any, name: str, default: Any = None) -> Any:
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    def _find_existing_broker_evidence(self, connector: Any, comment: str) -> dict[str, Any] | None:
        sources = (
            ("PENDING", "get_pending_orders"),
            ("ORDER_HISTORY", "get_bot_order_history"),
            ("FILLED", "get_bot_deal_history"),
        )
        for status, method_name in sources:
            if not hasattr(connector, method_name):
                continue
            for item in getattr(connector, method_name)() or []:
                if str(self._item_value(item, "comment", "") or "") != comment:
                    continue
                ticket = self._item_value(item, "order") or self._item_value(item, "ticket")
                evidence = {"broker_status": status, "mt5_order_ticket": str(ticket or "")}
                if status == "FILLED":
                    evidence.update(
                        {
                            "mt5_deal_ticket": str(self._item_value(item, "ticket", "") or ""),
                            "mt5_position_ticket": str(self._item_value(item, "position_id", "") or ""),
                            "fill_price": self._item_value(item, "price"),
                            "volume": self._item_value(item, "volume"),
                        }
                    )
                return evidence
        return None

    def _pending_price_reason(self, connector: Any, direction: str, entry: float) -> str | None:
        if not hasattr(connector, "get_tick"):
            return "BROKER_TICK_UNAVAILABLE"
        tick = connector.get_tick()
        if tick is None:
            return "BROKER_TICK_UNAVAILABLE"
        if direction == "LONG" and entry >= float(self._item_value(tick, "ask", 0.0) or 0.0):
            return "ENTRY_ALREADY_CROSSED"
        if direction == "SHORT" and entry <= float(self._item_value(tick, "bid", 0.0) or 0.0):
            return "ENTRY_ALREADY_CROSSED"
        return None

    def _execute_primary_safely(self, context: LiveContext, zone: ZoneRecord, payload: dict[str, Any]) -> None:
        try:
            self._execute_primary(context, zone, payload)
        except Exception as exc:
            reason = f"PLACEMENT_EXCEPTION:{type(exc).__name__}:{exc}"
            can_retry = self._retryable_failure(reason)
            for entry_number, _, entry in self._entry_specs(zone):
                existing = self.repository.broker_entry(zone.engine_zone_id, entry_number) or {}
                if existing.get("mt5_order_ticket") or existing.get("mt5_deal_ticket") or existing.get("historical"):
                    continue
                self._record_placement_failure(context, zone, entry_number, entry, reason, retryable=can_retry)
            self._update_zone_placement_status(context, zone, payload)
            self._log(
                "lebprime_pending_order_failed",
                {"context": context.key, "zone_id": zone.engine_zone_id, "reason": reason, "retryable": can_retry},
                level="ERROR",
            )

    def _retry_delay_seconds(self, attempt_count: int) -> float:
        strategy = self._strategy_config()
        base = max(1.0, float(strategy.get("placement_retry_base_seconds", 15.0)))
        maximum = max(base, float(strategy.get("placement_retry_max_seconds", 300.0)))
        return min(maximum, base * (2 ** min(max(0, int(attempt_count) - 1), 8)))

    @staticmethod
    def _retryable_failure(reason: str, retcode: Any = None) -> bool:
        normalized = str(reason or "").strip().upper()
        permanent = {
            ACCOUNT_NOT_CONFIRMED_DEMO,
            "SYMBOL_NOT_TRADABLE",
            "ENTRY_ALREADY_CROSSED",
            "INVALID_STOPS",
            "BLOCKED_INVALID_STOPS",
            "INVALID_VOLUME",
            "BLOCKED_INVALID_VOLUME",
            "INVALID_FILL_MODE",
            "BLOCKED_INVALID_FILL_MODE",
            "VOLUME_BELOW_MIN",
            "BROKER_MIN_VOLUME_TOO_LARGE",
            "INVALID_SYMBOL_SPEC",
            "FITTED_VOLUME_RATIO_BELOW_MIN",
            "INVALID_PENDING_REQUEST",
            "RISK_MANAGER_UNAVAILABLE",
        }
        if normalized in permanent:
            return False
        if any(marker in normalized for marker in ("SL TOO CLOSE", "INVALID STOPS", "INVALID VOLUME", "INVALID SYMBOL")):
            return False
        if retcode is not None:
            try:
                if int(retcode) in {10014, 10016, 10017, 10030}:
                    return False
            except (TypeError, ValueError):
                pass
        transient_markers = (
            "CONNECTION", "DISCONNECTED", "TIMEOUT", "TICK_UNAVAILABLE", "ORDER_CHECK_NONE",
            "ORDER_CHECK_FAILED", "UNKNOWN_ORDER_CHECK", "ORDER_SEND", "REQUOTE", "PRICE_CHANGE",
            "PRICE_OFF", "MARKET_CLOSED", "TEMPORARY", "TERMINAL_TRADING_NOT_ALLOWED",
            "ACCOUNT_TRADING_NOT_ALLOWED", "SYMBOL_EXPOSURE_LIMIT", "PLACEMENT_EXCEPTION",
            "MARGIN_FREE_INSUFFICIENT", "INSUFFICIENT_MARGIN",
            "PROJECTED_MARGIN_LEVEL_TOO_LOW", "ORDER_CALC_MARGIN_FAILED", "ACCOUNT_STATE",
        )
        return any(marker in normalized for marker in transient_markers)

    def _margin_diagnostic_payload(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        entry_number: int,
        entry: float,
        direction: str,
        account: Any,
        symbol_spec: dict[str, Any],
        sizing: dict[str, Any],
        margin: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the persisted, dashboard-visible audit record for one margin decision."""
        risk_cfg = self.config.get("risk", {})
        diagnostics = {
            "context": context.key,
            "zone_id": zone.engine_zone_id,
            "entry_number": int(entry_number),
            "idempotency_key": self._idempotency_key(context.key, zone.engine_zone_id, entry_number),
            "symbol": context.mt5_symbol,
            "direction": direction,
            "account": {
                name: self._item_value(account, name)
                for name in ("balance", "equity", "margin", "margin_free", "margin_level", "leverage")
            },
            "entry_price": float(entry),
            "sl_price": float(zone.sl_price),
            "risk_distance": abs(float(entry) - float(zone.sl_price)),
            "configured_risk_percent": float(risk_cfg.get("risk_percent", 0.0)),
            "risk_adjustment": float(sizing.get("risk_adjustment_multiplier", 0.5)),
            "effective_entry_risk_percent": float(sizing.get("risk_percent", 0.0)),
            "raw_calculated_volume": sizing.get("raw_lot"),
            "normalized_requested_volume": sizing.get("volume"),
            "broker_volume_min": symbol_spec.get("volume_min"),
            "broker_volume_max": symbol_spec.get("volume_max"),
            "broker_volume_step": symbol_spec.get("volume_step"),
            "configured_min_lot": risk_cfg.get("min_lot"),
            "effective_minimum_volume": margin.get("effective_minimum_volume", sizing.get("min_volume")),
            "configured_min_margin_level": margin.get("min_margin_level", risk_cfg.get("min_margin_level")),
            "configured_min_free_margin_buffer": margin.get("min_free_margin_buffer", risk_cfg.get("min_free_margin_buffer")),
            "min_fitted_volume_ratio": margin.get("min_fitted_volume_ratio", risk_cfg.get("min_fitted_volume_ratio")),
            "fit_candidates": margin.get("fit_candidates", []),
            "requested_margin": margin.get("requested_margin"),
            "required_margin": margin.get("required_margin"),
            "projected_margin": margin.get("projected_margin"),
            "projected_margin_level": margin.get("projected_margin_level"),
            "free_margin_after": margin.get("free_margin_after"),
            "accepted": bool(margin.get("valid")),
            "decision": margin.get("reason_code"),
            "reason": margin.get("reason"),
        }
        return diagnostics

    @staticmethod
    def _margin_entry_updates(diagnostics: dict[str, Any]) -> dict[str, Any]:
        account = diagnostics.get("account") or {}
        return {
            "volume": diagnostics.get("normalized_requested_volume"),
            "raw_calculated_volume": diagnostics.get("raw_calculated_volume"),
            "requested_volume": diagnostics.get("normalized_requested_volume"),
            "minimum_volume": diagnostics.get("effective_minimum_volume"),
            "required_margin": diagnostics.get("required_margin") or diagnostics.get("requested_margin"),
            "free_margin": account.get("margin_free"),
            "projected_margin_level": diagnostics.get("projected_margin_level"),
            "required_margin_level": diagnostics.get("configured_min_margin_level"),
            "margin_diagnostics": diagnostics,
        }

    def _entry_details(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        entry_number: int,
        entry: float,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "context": context.key,
            "zone_id": zone.engine_zone_id,
            "entry_number": entry_number,
            "idempotency_key": self._idempotency_key(context.key, zone.engine_zone_id, entry_number),
            "symbol": context.mt5_symbol,
            "order_type": "BUY_LIMIT" if zone.side == "DEMAND" else "SELL_LIMIT",
            "price": entry,
            "sl": zone.sl_price,
            "tp": zone.tp2_price,
            **extra,
        }

    def _record_placement_failure(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        entry_number: int,
        entry: float,
        reason: str,
        *,
        retcode: Any = None,
        comment: Any = None,
        retryable: bool | None = None,
        attempted: bool = True,
        updates: dict[str, Any] | None = None,
    ) -> None:
        existing = self.repository.broker_entry(zone.engine_zone_id, entry_number) or {}
        can_retry = self._retryable_failure(reason, retcode) if retryable is None else bool(retryable)
        attempts = int(existing.get("attempt_count", 0) or 0) + (1 if attempted else 0)
        now = _utc_now()
        next_retry = (now + timedelta(seconds=self._retry_delay_seconds(attempts))).isoformat() if can_retry else None
        row = self._broker_entry_payload(
            context,
            zone,
            entry_number,
            entry,
            status="RETRY_SCHEDULED" if can_retry else "PERMANENT_FAILURE",
            eligible=can_retry,
            failure_reason=str(reason),
            attempt_count=attempts,
            last_attempt_at=now.isoformat(),
            last_failure=str(reason),
            retryable=int(can_retry),
            next_retry_at=next_retry,
            broker_retcode=str(retcode) if retcode is not None else existing.get("broker_retcode"),
            **(updates or {}),
        )
        self.repository.upsert_broker_entry(row)
        if row.get("final_exit_reason") == "REJECTED" and existing.get("final_exit_reason") != "REJECTED":
            self._journal_broker_transition(row, "ORDER_REJECTED", now.isoformat())
        details = self._entry_details(
            context,
            zone,
            entry_number,
            float(row.get("normalized_price") or entry),
            volume=row.get("volume"),
            retcode=retcode,
            comment=comment,
            reason=str(reason),
            attempt_count=attempts,
            retryable=can_retry,
            next_retry_at=next_retry,
        )
        self._log("lebprime_pending_order_failed", details, level="WARNING" if can_retry else "ERROR")
        if can_retry:
            self._log("lebprime_pending_order_retry_scheduled", details, level="WARNING")
        else:
            if self._notification_enabled("execution_blocked_alerts") and self.notifier is not None:
                self._send_notification_once(
                    context,
                    zone.engine_zone_id,
                    f"PENDING_FAILED_ENTRY{entry_number}",
                    lambda: self.notifier.send_lebprime_pending_failed(
                        {
                            "symbol": context.logical_symbol,
                            "timeframe": context.timeframe,
                            "zone_id": zone.engine_zone_id,
                            "entry_number": entry_number,
                            "reason": str(reason),
                        }
                    ),
                    sent_event="lebprime_telegram_pending_failed_sent",
                    failed_event="lebprime_telegram_pending_failed_failed",
                    details={"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number},
                )

    def _update_zone_placement_status(self, context: LiveContext, zone: ZoneRecord, payload: dict[str, Any]) -> None:
        entries = self.repository.broker_entries_for_zone(zone.engine_zone_id)
        successful = [row for row in entries if row.get("mt5_order_ticket") or row.get("mt5_deal_ticket")]
        retrying = [row for row in entries if bool(row.get("retryable")) and not row.get("mt5_order_ticket")]
        permanent = [row for row in entries if row.get("broker_status") == "PERMANENT_FAILURE"]
        if entries and len(successful) == len(entries):
            status = "BROKER_PENDING"
            context.last_execution_error = None
        elif retrying:
            status = "PLACEMENT_RETRY_PENDING"
            context.last_execution_error = str(retrying[0].get("last_failure") or retrying[0].get("failure_reason") or "RETRY_SCHEDULED")
        elif successful and permanent:
            status = "PARTIAL_PENDING_FAILED"
            context.last_execution_error = str(permanent[0].get("last_failure") or permanent[0].get("failure_reason"))
        else:
            status = "PLACEMENT_FAILED"
            context.last_execution_error = str((permanent[0] if permanent else entries[0]).get("failure_reason")) if entries else "NO_ENTRY_OUTCOME"
        payload["execution_status"] = status
        payload["updated_at"] = _utc_now().isoformat()
        for row in successful:
            payload[f"entry{int(row['entry_number'])}_order_id"] = row.get("mt5_order_ticket")
        self.repository.upsert_zone(payload)

    def _zone_for_retry(self, context: LiveContext, row: dict[str, Any]) -> ZoneRecord | None:
        zone_id = str(row.get("zone_id") or "")
        engine_zone = next((item for item in context.engine.zones() if item.engine_zone_id == zone_id), None)
        if engine_zone is not None:
            return engine_zone
        stored = self.repository.zone(zone_id)
        if stored is None:
            return None
        try:
            metadata = json.loads(stored.get("metadata_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        created_at = str(stored.get("created_at") or _utc_now().isoformat())
        return ZoneRecord(
            engine_zone_id=zone_id,
            side="DEMAND" if str(stored.get("side")) == "BUY" else "SUPPLY",
            source_bar_index=0,
            source_time=created_at,
            zone_top=float(stored["zone_top"]),
            zone_bottom=float(stored["zone_bottom"]),
            created_bar_index=0,
            created_time=created_at,
            symbol=str(stored.get("logical_symbol") or context.logical_symbol),
            timeframe=str(stored.get("timeframe") or context.timeframe),
            indicator_version=metadata.get("indicator_version"),
            engine_version=metadata.get("engine_version"),
            atr_at_source=metadata.get("atr_at_source"),
            limit_created_time=created_at,
            entry1=stored.get("entry1"),
            entry2=stored.get("entry2"),
            entry_price=stored.get("entry1"),
            sl_price=stored.get("stop_loss"),
            tp1_price=stored.get("tp1"),
            tp2_price=stored.get("tp2"),
            buffer_used=stored.get("buffer_used"),
            terminal_state=stored.get("theoretical_state") if stored.get("theoretical_state") in {"CANCELED", "EXPIRED", "CLOSED"} else None,
        )

    def _retry_due_placements(self, now: datetime) -> None:
        processed_zones: set[str] = set()
        for entry in self.repository.retryable_broker_entries(now.isoformat()):
            zone_id = str(entry.get("zone_id") or "")
            if not zone_id or zone_id in processed_zones:
                continue
            processed_zones.add(zone_id)
            context = self.contexts.get(str(entry.get("context_key") or ""))
            if context is None or context.status not in {"HEALTHY", "IDLE"}:
                continue
            zone = self._zone_for_retry(context, entry)
            stored = self.repository.zone(zone_id)
            if zone is None or stored is None:
                self._log(
                    "lebprime_pending_order_failed",
                    {"context": context.key, "zone_id": zone_id, "reason": "RETRY_ZONE_STATE_MISSING", "retryable": False},
                    level="ERROR",
                )
                continue
            if zone.terminal_state in {"CANCELED", "EXPIRED", "CLOSED"}:
                continue
            payload = self._zone_payload(context, zone, now.isoformat())
            payload.update(
                {
                    "family_id": stored.get("family_id"),
                    "execution_primary": stored.get("execution_primary", 1),
                    "execution_status": stored.get("execution_status", "PLACEMENT_RETRY_PENDING"),
                    "entry1_order_id": stored.get("entry1_order_id"),
                    "entry2_order_id": stored.get("entry2_order_id"),
                    "created_at": stored.get("created_at"),
                }
            )
            self._execute_primary_safely(context, zone, payload)

    def _execute_primary(self, context: LiveContext, zone: ZoneRecord, payload: dict[str, Any]) -> None:
        connector = self._connector(context.logical_symbol)
        candidates: list[tuple[int, str, float]] = []
        successful_statuses = {"PENDING", "ORDER_HISTORY", "FILLED", "CLOSED", "CANCELLED", "CANCELED", "EXPIRED"}
        for entry_number, label, entry in self._entry_specs(zone):
            key = self._idempotency_key(context.key, zone.engine_zone_id, entry_number)
            existing = self.repository.broker_entry(zone.engine_zone_id, entry_number)
            if existing and (
                existing.get("broker_status") in successful_statuses
                or existing.get("mt5_order_ticket")
                or existing.get("mt5_deal_ticket")
                or existing.get("historical")
            ):
                self._log(
                    "lebprime_duplicate_check",
                    {
                        "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                        "idempotency_key": key, "matched_existing_key": existing.get("idempotency_key"),
                        "matched_ticket": existing.get("mt5_order_ticket"), "decision": "SUPPRESS",
                        "reason": "EXACT_IDEMPOTENCY_KEY_ALREADY_PROCESSED",
                    },
                )
                continue
            if existing and existing.get("broker_status") == "PERMANENT_FAILURE" and not bool(existing.get("retryable")):
                self._log(
                    "lebprime_duplicate_check",
                    {
                        "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                        "idempotency_key": key, "matched_existing_key": existing.get("idempotency_key"),
                        "matched_ticket": None, "decision": "SUPPRESS",
                        "reason": "EXACT_ENTRY_PERMANENTLY_INVALID",
                    },
                )
                continue
            if existing and bool(existing.get("retryable")) and existing.get("next_retry_at"):
                try:
                    retry_due = pd.to_datetime(existing["next_retry_at"], utc=True) <= pd.Timestamp.now(tz="UTC")
                except Exception:
                    retry_due = True
                if not retry_due:
                    self._log(
                        "lebprime_duplicate_check",
                        {
                            "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                            "idempotency_key": key, "matched_existing_key": existing.get("idempotency_key"),
                            "matched_ticket": None, "decision": "WAIT_RETRY",
                            "reason": "RETRY_BACKOFF_ACTIVE", "next_retry_at": existing.get("next_retry_at"),
                        },
                    )
                    continue
            comment = self._order_comment(zone.engine_zone_id, label)
            evidence = self._find_existing_broker_evidence(connector, comment)
            if evidence is not None:
                recovered = self._broker_entry_payload(context, zone, entry_number, entry, status=evidence["broker_status"], eligible=True, **{key: value for key, value in evidence.items() if key != "broker_status"})
                self.repository.upsert_broker_entry(recovered)
                payload[f"{label.lower()}_order_id"] = recovered.get("mt5_order_ticket")
                self._log(
                    "lebprime_duplicate_check",
                    {
                        "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                        "idempotency_key": key, "matched_existing_key": recovered["idempotency_key"],
                        "matched_ticket": recovered.get("mt5_order_ticket"), "decision": "SUPPRESS",
                        "reason": "EXACT_BROKER_COMMENT_RECOVERED",
                    },
                )
                continue
            self._log(
                "lebprime_duplicate_check",
                {
                    "context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number,
                    "idempotency_key": key, "matched_existing_key": None, "matched_ticket": None,
                    "decision": "PASS", "reason": "NO_EXACT_ENTRY_MATCH",
                },
            )
            candidates.append((entry_number, label, entry))
        if not candidates:
            return
        ok, reason, account = self._preflight(context, connector)
        if not ok:
            for entry_number, _, entry in candidates:
                self._log("lebprime_pending_order_attempt", self._entry_details(context, zone, entry_number, entry, reason="PREFLIGHT"))
                self._record_placement_failure(context, zone, entry_number, entry, reason)
            self._update_zone_placement_status(context, zone, payload)
            return
        reduced_risk = False
        if self.risk_manager is None:
            reason = "RISK_MANAGER_UNAVAILABLE"
            risk_gate = {"live_allowed": False, "reasons": [reason]}
        else:
            risk_gate = self.risk_manager.evaluate_risk_gate(
                self.risk_state,
                account,
                _utc_now(),
                "LEBPRIME_24H",
                live_requested=True,
            )
            reduced_risk = bool(risk_gate.get("reduced_risk"))
        if not bool(risk_gate.get("live_allowed")):
            reasons = risk_gate.get("reasons", [])
            reason = ",".join(str(item) for item in reasons) or "RISK_BLOCKED"
            for entry_number, _, entry in candidates:
                self._log("lebprime_pending_order_attempt", self._entry_details(context, zone, entry_number, entry, reason="RISK_GATE"))
                self._record_placement_failure(context, zone, entry_number, entry, reason, retryable=False)
            self._update_zone_placement_status(context, zone, payload)
            return

        symbol_spec = connector.get_symbol_spec(force_refresh=True)
        direction = "LONG" if zone.side == "DEMAND" else "SHORT"
        for entry_number, label, entry in candidates:
            self._log(
                "lebprime_pending_order_attempt",
                self._entry_details(
                    context,
                    zone,
                    entry_number,
                    entry,
                    attempt_count=int((self.repository.broker_entry(zone.engine_zone_id, entry_number) or {}).get("attempt_count", 0) or 0) + 1,
                ),
            )
            risk_price = abs(entry - float(zone.sl_price))
            trade_plan = {
                "entry_price": entry,
                "stop_loss": float(zone.sl_price),
                "tp1": float(zone.tp1_price),
                "tp2": float(zone.tp2_price),
                "sl_distance_pips": risk_price / max(float(symbol_spec["pip_size"]), 1e-12),
                "atr_at_entry": float(zone.atr_at_source or risk_price),
                "atr_median": float(zone.atr_at_source or risk_price),
            }
            sizing = self.risk_manager.calculate_position_size(
                account,
                trade_plan,
                symbol_spec,
                live_requested=True,
                reduced_risk=reduced_risk,
                entry_mode="limit",
                risk_adjustment=0.5,
            )
            if not sizing.get("valid"):
                reason = str(sizing.get("reason_code") or sizing.get("reason") or "RISK_SIZING_FAILED").upper()
                self._record_placement_failure(context, zone, entry_number, entry, reason, retryable=False)
                continue
            volume = float(sizing["volume"])
            order_type = connector.pending_order_type(direction)
            margin = self.risk_manager.fit_volume_to_margin(
                volume,
                account,
                symbol_spec,
                lambda candidate_volume: connector.calculate_margin(order_type, candidate_volume, entry),
                diagnostic_context={
                    "context": context.key,
                    "zone_id": zone.engine_zone_id,
                    "entry_number": entry_number,
                    "idempotency_key": self._idempotency_key(context.key, zone.engine_zone_id, entry_number),
                    "symbol": context.mt5_symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "sl_price": float(zone.sl_price),
                },
            )
            margin_diagnostics = self._margin_diagnostic_payload(
                context, zone, entry_number, entry, direction, account, symbol_spec, sizing, margin
            )
            margin_updates = self._margin_entry_updates(margin_diagnostics)
            self._log(
                "lebprime_margin_sizing",
                margin_diagnostics,
                level="INFO" if margin.get("valid") else "WARNING",
            )
            if not margin.get("valid"):
                reason = str(margin.get("reason_code") or margin.get("reason") or "MARGIN_CHECK_FAILED")
                self._record_placement_failure(
                    context,
                    zone,
                    entry_number,
                    entry,
                    reason,
                    updates=margin_updates,
                )
                continue
            try:
                request, request_reason, _ = connector.build_pending_limit_request(
                    direction=direction,
                    volume=float(margin["volume"]),
                    entry_price=entry,
                    sl=float(zone.sl_price),
                    tp=float(zone.tp2_price),
                    comment=self._order_comment(zone.engine_zone_id, label),
                )
            except ValueError as exc:
                self._record_placement_failure(
                    context,
                    zone,
                    entry_number,
                    entry,
                    f"INVALID_PENDING_REQUEST:{exc}",
                    retryable=False,
                    updates=margin_updates,
                )
                continue
            if request is None:
                self._record_placement_failure(context, zone, entry_number, entry, request_reason, retryable=False)
                continue
            normalized_entry = float(request["price"])
            normalized_sl = float(request["sl"])
            normalized_tp = float(request["tp"])
            volume = float(request["volume"])
            crossed_reason = self._pending_price_reason(connector, direction, normalized_entry)
            if crossed_reason:
                self._record_placement_failure(
                    context, zone, entry_number, entry, crossed_reason, retryable=False,
                    updates={"normalized_price": normalized_entry, "volume": volume, "sl": normalized_sl, "tp": normalized_tp},
                )
                continue
            prior = self.repository.broker_entry(zone.engine_zone_id, entry_number) or {}
            attempted_at = _utc_now()
            attempt_count = int(prior.get("attempt_count", 0) or 0) + 1
            reserved = self._broker_entry_payload(
                context, zone, entry_number, entry, status="PLACING", eligible=True,
                normalized_price=normalized_entry, volume=volume, sl=normalized_sl, tp=normalized_tp,
                attempt_count=attempt_count, last_attempt_at=attempted_at.isoformat(), last_failure=None,
                retryable=1, next_retry_at=(attempted_at + timedelta(seconds=self._retry_delay_seconds(attempt_count))).isoformat(),
                **{key: value for key, value in margin_updates.items() if key != "volume"},
            )
            self.repository.upsert_broker_entry(reserved)
            precheck = connector.precheck_order(request)
            self._log(
                "lebprime_pending_order_check",
                {
                    "context": context.key,
                    "zone_id": zone.engine_zone_id,
                    "symbol": context.mt5_symbol,
                    "entry_number": entry_number,
                    "idempotency_key": reserved["idempotency_key"],
                    "price": normalized_entry,
                    "sl": normalized_sl,
                    "tp": normalized_tp,
                    "volume": volume,
                    "retcode": precheck.get("payload", {}).get("retcode"),
                    "comment": precheck.get("payload", {}).get("comment") or precheck.get("message") or precheck.get("reason_code"),
                },
                level="INFO" if precheck.get("ok") else "WARNING",
            )
            if not precheck.get("ok"):
                check_payload = precheck.get("payload", {})
                reason = str(precheck.get("reason_code") or "ORDER_CHECK_FAILED")
                self._record_placement_failure(
                    context, zone, entry_number, entry, reason,
                    retcode=check_payload.get("retcode"),
                    comment=check_payload.get("comment") or precheck.get("message"),
                    attempted=False,
                    updates={"normalized_price": normalized_entry, "volume": volume, "sl": normalized_sl, "tp": normalized_tp},
                )
                continue
            result = connector.send_order(request, dry_run=False)
            raw_result = (result.get("details") or {}).get("result") or result.get("result") or {}
            if hasattr(raw_result, "_asdict"):
                raw_result = raw_result._asdict()
            self._log(
                "lebprime_order_send",
                {
                    "context": context.key,
                    "zone_id": zone.engine_zone_id,
                    "symbol": context.mt5_symbol,
                    "entry_number": entry_number,
                    "idempotency_key": reserved["idempotency_key"],
                    "price": normalized_entry,
                    "sl": normalized_sl,
                    "tp": normalized_tp,
                    "volume": volume,
                    "retcode": raw_result.get("retcode") if isinstance(raw_result, dict) else getattr(raw_result, "retcode", None),
                    "comment": raw_result.get("comment") if isinstance(raw_result, dict) else getattr(raw_result, "comment", None),
                    "order": raw_result.get("order") if isinstance(raw_result, dict) else getattr(raw_result, "order", None),
                    "deal": raw_result.get("deal") if isinstance(raw_result, dict) else getattr(raw_result, "deal", None),
                },
                level="INFO" if result.get("ok") else "ERROR",
            )
            if not result.get("ok"):
                reason = str(result.get("reason_code") or "ORDER_SEND_FAILED")
                retcode = raw_result.get("retcode") if isinstance(raw_result, dict) else getattr(raw_result, "retcode", None)
                comment = raw_result.get("comment") if isinstance(raw_result, dict) else getattr(raw_result, "comment", None)
                self._record_placement_failure(
                    context, zone, entry_number, entry, reason, retcode=retcode, comment=comment,
                    attempted=False,
                    updates={
                        "normalized_price": normalized_entry,
                        "volume": volume,
                        "sl": normalized_sl,
                        "tp": normalized_tp,
                        "final_exit_reason": "REJECTED",
                        "broker_submitted_at": attempted_at.isoformat(),
                    },
                )
                continue
            order_id = self._order_id(result)
            if not order_id:
                self._record_placement_failure(
                    context, zone, entry_number, entry, "ORDER_SEND_MISSING_TICKET", attempted=False,
                    updates={"normalized_price": normalized_entry, "volume": volume, "sl": normalized_sl, "tp": normalized_tp},
                )
                continue
            payload[f"{label.lower()}_order_id"] = order_id
            accepted_at = _utc_now().isoformat()
            result_retcode = raw_result.get("retcode") if isinstance(raw_result, dict) else getattr(raw_result, "retcode", None)
            reserved.update({"broker_status": "PENDING", "mt5_order_ticket": order_id, "broker_created_at": accepted_at, "broker_submitted_at": accepted_at, "broker_retcode": str(result_retcode) if result_retcode is not None else None, "failure_reason": None, "last_failure": None, "retryable": 0, "next_retry_at": None, "updated_at": accepted_at})
            self.repository.upsert_broker_entry(reserved)
            self._journal_broker_transition(reserved, "ORDER_ACCEPTED", accepted_at)
            self._log(
                "lebprime_pending_order_placed",
                {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "idempotency_key": reserved["idempotency_key"], "ticket": order_id, "symbol": context.mt5_symbol, "price": normalized_entry, "sl": normalized_sl, "tp": normalized_tp, "volume": volume, "retcode": raw_result.get("retcode") if isinstance(raw_result, dict) else getattr(raw_result, "retcode", None)},
            )
            if self._notification_enabled("execution_success_alerts") and self.notifier is not None:
                self._send_notification_once(
                    context,
                    zone.engine_zone_id,
                    f"PENDING_ACCEPTED_ENTRY{entry_number}",
                    lambda entry_number=entry_number, order_id=order_id, normalized_entry=normalized_entry, normalized_sl=normalized_sl, normalized_tp=normalized_tp: self.notifier.send_lebprime_pending_accepted(
                        {"symbol": context.logical_symbol, "timeframe": context.timeframe, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "price": normalized_entry, "sl": normalized_sl, "tp": normalized_tp, "ticket": order_id}
                    ),
                    sent_event="lebprime_telegram_pending_accepted_sent",
                    failed_event="lebprime_telegram_pending_accepted_failed",
                    details={"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "ticket": order_id},
                )
            self._record_event(context, zone.engine_zone_id, f"{label}_ORDER_SENT", payload["updated_at"], price=entry, family_id=payload["family_id"], broker_order_id=order_id, metadata={"volume": margin["volume"], "required_margin": margin.get("required_margin")})
        self._update_zone_placement_status(context, zone, payload)

    @staticmethod
    def _order_id(result: dict[str, Any]) -> str:
        details = result.get("details") or result.get("payload") or {}
        raw = details.get("result") or result.get("result") or {}
        if hasattr(raw, "_asdict"):
            raw = raw._asdict()
        if isinstance(raw, dict):
            return str(raw.get("order") or raw.get("deal") or raw.get("ticket") or "")
        return str(getattr(raw, "order", None) or getattr(raw, "deal", None) or "")

    @staticmethod
    def _order_comment(zone_id: str, label: str) -> str:
        digest = hashlib.sha1(zone_id.encode("utf-8")).hexdigest()[:8].upper()
        return f"LP340-{digest}-{label}"

    def _execution_rejected(self, context: LiveContext, zone: ZoneRecord, payload: dict[str, Any], event_type: str, reason: str, sent: list[tuple[int, str]], connector: Any) -> None:
        entry_status = {
            "BLOCKED_PRECHECK": "BLOCKED_PRECHECK",
            "RISK_BLOCKED": "RISK_BLOCKED",
            "REJECTED_BY_BROKER": "REJECTED_BY_BROKER",
        }.get(event_type, "BLOCKED_PRECHECK")
        for entry_number, order_id in sent:
            if order_id:
                ok, result = connector.cancel_pending_order(order_id)
                row = self.repository.broker_entry(zone.engine_zone_id, entry_number)
                if row:
                    terminal_at = _utc_now().isoformat()
                    row.update(
                        {
                            "broker_status": "CANCELED" if ok else "CANCEL_FAILED",
                            "broker_closed_at": terminal_at if ok else None,
                            "canceled_at": terminal_at if ok else None,
                            "final_exit_reason": "CANCELED" if ok else row.get("final_exit_reason"),
                            "broker_retcode": str(self._item_value(result, "retcode", "") or row.get("broker_retcode") or ""),
                            "failure_reason": reason if ok else f"ROLLBACK_FAILED:{reason}",
                            "updated_at": terminal_at,
                        }
                    )
                    self.repository.upsert_broker_entry(row)
                    if ok:
                        self._journal_broker_transition(row, "ORDER_CANCELED", terminal_at)
                self._log(
                    "lebprime_pending_order_cancelled" if ok else "lebprime_pending_order_skipped",
                    {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "ticket": order_id, "reason": f"PAIR_ROLLBACK:{reason}", "result": str(result)},
                    level="INFO" if ok else "ERROR",
                )
        for entry_number, _, entry_price in self._entry_specs(zone):
            existing_entry = self.repository.broker_entry(zone.engine_zone_id, entry_number)
            if existing_entry is None:
                self.repository.upsert_broker_entry(
                    self._broker_entry_payload(
                        context, zone, entry_number, entry_price, status=entry_status,
                        eligible=False, failure_reason=str(reason),
                    )
                )
            elif existing_entry.get("broker_status") not in {"PLACING", "PENDING", "ORDER_HISTORY", "FILLED", "CLOSED", "CANCELLED", "CANCELED"}:
                existing_entry.update(
                    {
                        "broker_status": entry_status,
                        "failure_reason": str(reason),
                        "execution_eligible": 0,
                        "updated_at": _utc_now().isoformat(),
                    }
                )
                self.repository.upsert_broker_entry(existing_entry)
            self._log(
                "lebprime_pending_order_skipped",
                {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry_number, "reason": str(reason)},
                level="WARNING",
            )
        payload["execution_status"] = event_type
        self.repository.upsert_zone(payload)
        self._record_event(context, zone.engine_zone_id, event_type, payload["updated_at"], family_id=payload["family_id"], metadata={"reason": reason})
        context.last_execution_error = str(reason)
        self._log(
            "lebprime_pending_order_skipped",
            {"context": context.key, "zone_id": zone.engine_zone_id, "symbol": context.mt5_symbol, "reason": str(reason)},
            level="WARNING",
        )

    def _track_lifecycle(
        self,
        context: LiveContext,
        zone: ZoneRecord,
        event: ZoneEvent,
        *,
        execute: bool = True,
        historical: bool = False,
        execution_block_reason: str | None = None,
    ) -> None:
        row = self.repository.zone(zone.engine_zone_id)
        if event.event_type == "ENTRY1_ACTIVATED":
            self._log(
                "lebprime_entry1_activated",
                {
                    "context": context.key,
                    "zone_id": zone.engine_zone_id,
                    "historical": bool(historical),
                    "execution_mode": "LIVE" if self.live_requested else "DRY",
                    "execution_eligible": False,
                    "authority": "CANDLE_TOUCH_ONLY",
                },
            )
        if row is None:
            if event.event_type == "ENTRY1_ACTIVATED" and not historical:
                context.last_execution_error = "MISSING_LIFECYCLE_STATE"
                self._log(
                    "lebprime_execution_skipped",
                    {"context": context.key, "zone_id": zone.engine_zone_id, "symbol": context.mt5_symbol, "reason": "MISSING_LIFECYCLE_STATE"},
                    level="ERROR",
                )
            return
        event_type = event.event_type
        payload = self._zone_payload(context, zone, event.timestamp)
        payload.update(
            {
                "family_id": row.get("family_id"),
                "execution_primary": row.get("execution_primary", 0),
                "execution_status": row.get("execution_status", "THEORETICAL"),
                "suppressed_by_zone_id": row.get("suppressed_by_zone_id"),
                "duplicate_reason": row.get("duplicate_reason"),
                "entry1_order_id": row.get("entry1_order_id"),
                "entry2_order_id": row.get("entry2_order_id"),
                "created_at": row.get("created_at"),
            }
        )
        if event_type == "ENTRY1_ACTIVATED":
            if historical and payload["execution_primary"] and payload["execution_status"] == "PRIMARY_PENDING":
                payload["execution_status"] = "HISTORICAL_REPLAY"
            elif not historical:
                self._log(
                    "lebprime_pending_order_skipped",
                    {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": 1, "reason": "CANDLE_TOUCH_NOT_BROKER_AUTHORITY"},
                )
        elif event_type == "ENTRY2_ACTIVATED" and not historical:
            self._log(
                "lebprime_pending_order_skipped",
                {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": 2, "reason": "CANDLE_TOUCH_NOT_BROKER_AUTHORITY"},
            )
        broker_actions: list[dict[str, Any]] = []
        if not historical and payload["execution_primary"] and payload["execution_status"] not in {"DRY_RUN_PRIMARY", "HISTORICAL_REPLAY", "EXECUTION_SKIPPED", "RISK_BLOCKED", "ORDER_REJECTED", "BROKER_PENDING_NOT_PLACED"}:
            broker_actions = self._manage_broker_lifecycle(context, zone, event_type, payload)
        if event_type in {"TP2_HIT", "TP1_FINAL", "SL_HIT", "CANCELED", "EXPIRED"}:
            cancellation_confirmed = event_type not in {"CANCELED", "EXPIRED"} or all(action.get("ok", True) for action in broker_actions)
            payload["execution_status"] = ("CLOSED" if cancellation_confirmed else "CANCEL_PENDING_FAILED") if payload["execution_primary"] else payload["execution_status"]
            if payload["execution_primary"]:
                self.repository.upsert_family(
                    {
                        "family_id": payload["family_id"],
                        "logical_symbol": context.logical_symbol,
                        "side": payload["side"],
                        "zone_bottom": payload["zone_bottom"],
                        "zone_top": payload["zone_top"],
                        "primary_zone_id": zone.engine_zone_id,
                        "status": "CLOSED" if cancellation_confirmed else "ACTIVE",
                        "created_at": payload["created_at"],
                        "updated_at": event.timestamp,
                    }
                )
        self.repository.upsert_zone(payload)
        if event_type in AUDIT_EVENTS:
            broker_order_id = payload.get("entry1_order_id") if event_type == "ENTRY1_ACTIVATED" else payload.get("entry2_order_id") if event_type == "ENTRY2_ACTIVATED" else None
            self._record_event(
                context,
                zone.engine_zone_id,
                event_type,
                event.timestamp,
                price=event.price,
                family_id=payload["family_id"],
                broker_order_id=broker_order_id,
                metadata={"theoretical": not bool(payload["execution_primary"]), "historical": bool(historical), "broker_actions": broker_actions},
            )

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _number(item: Any, name: str) -> float:
        try:
            return float(LebPrimeLiveService._item_value(item, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _position_model(self, connector: Any) -> str:
        try:
            account = connector.get_account_info()
            mode = int(self._item_value(account, "margin_mode", -1))
        except Exception:
            return "UNKNOWN"
        mt5_api = getattr(connector, "mt5", None)
        if mode == int(getattr(mt5_api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)):
            return "HEDGING"
        netting_modes = {
            int(getattr(mt5_api, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", 0)),
            int(getattr(mt5_api, "ACCOUNT_MARGIN_MODE_EXCHANGE", 1)),
        }
        return "NETTING" if mode in netting_modes else "UNKNOWN"

    def _deal_is_entry(self, connector: Any, deal: Any) -> bool:
        raw_value = self._item_value(deal, "entry", None)
        if raw_value is None:
            return False
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return False
        mt5_api = getattr(connector, "mt5", None)
        return value in {
            int(getattr(mt5_api, "DEAL_ENTRY_IN", 0)),
            int(getattr(mt5_api, "DEAL_ENTRY_INOUT", 2)),
        }

    def _deal_is_close(self, connector: Any, deal: Any) -> bool:
        raw_value = self._item_value(deal, "entry", None)
        if raw_value is None:
            return False
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return False
        mt5_api = getattr(connector, "mt5", None)
        return value in {
            int(getattr(mt5_api, "DEAL_ENTRY_OUT", 1)),
            int(getattr(mt5_api, "DEAL_ENTRY_OUT_BY", 3)),
            int(getattr(mt5_api, "DEAL_ENTRY_INOUT", 2)),
        }

    def _classify_exit_reason(self, connector: Any, deal: Any | None) -> str:
        if deal is None:
            return "UNKNOWN"
        mt5_api = getattr(connector, "mt5", None)
        comment = str(self._item_value(deal, "comment", "") or "").upper()
        if "SL_HIT" in comment:
            return "SL"
        if "TP1" in comment or "TP2" in comment:
            return "TP"
        raw_reason = self._item_value(deal, "reason", None)
        if raw_reason is None:
            return "UNKNOWN"
        try:
            reason = int(raw_reason)
        except (TypeError, ValueError):
            return "UNKNOWN"
        if reason == int(getattr(mt5_api, "DEAL_REASON_SL", 4)):
            return "SL"
        if reason == int(getattr(mt5_api, "DEAL_REASON_TP", 5)):
            return "TP"
        if reason in {
            int(getattr(mt5_api, "DEAL_REASON_CLIENT", 0)),
            int(getattr(mt5_api, "DEAL_REASON_MOBILE", 1)),
            int(getattr(mt5_api, "DEAL_REASON_WEB", 2)),
        }:
            return "MANUAL_CLOSE"
        broker_reasons = {
            int(getattr(mt5_api, "DEAL_REASON_EXPERT", 3)),
            int(getattr(mt5_api, "DEAL_REASON_SO", 6)),
            int(getattr(mt5_api, "DEAL_REASON_ROLLOVER", 7)),
            int(getattr(mt5_api, "DEAL_REASON_VMARGIN", 8)),
            int(getattr(mt5_api, "DEAL_REASON_SPLIT", 9)),
        }
        return "BROKER_CLOSE" if reason in broker_reasons else "UNKNOWN"

    def _deal_snapshot(self, deal: Any) -> dict[str, Any]:
        return {
            "ticket": str(self._item_value(deal, "ticket", "") or ""),
            "order": str(self._item_value(deal, "order", "") or ""),
            "position_id": str(self._item_value(deal, "position_id", "") or ""),
            "entry": self._item_value(deal, "entry"),
            "reason": self._item_value(deal, "reason"),
            "time": self._broker_time(deal, _utc_now()),
            "price": self._item_value(deal, "price"),
            "volume": self._item_value(deal, "volume"),
            "profit": self._item_value(deal, "profit"),
            "commission": self._item_value(deal, "commission"),
            "swap": self._item_value(deal, "swap"),
            "fee": self._item_value(deal, "fee"),
            "comment": self._item_value(deal, "comment"),
        }

    def _journal_broker_transition(
        self,
        entry: dict[str, Any],
        event_type: str,
        timestamp: str,
        *,
        event_suffix: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.trade_journal is None:
            return
        event_key = f"lebprime:{entry.get('idempotency_key')}:{event_type}:{event_suffix or 'once'}"
        try:
            self.trade_journal.record_trade_event(
                {
                    "timestamp": timestamp,
                    "trade_id": entry.get("idempotency_key"),
                    "mt5_ticket": entry.get("mt5_position_ticket") or entry.get("mt5_order_ticket"),
                    "position_id": entry.get("mt5_position_ticket"),
                    "order_id": entry.get("mt5_order_ticket"),
                    "event_type": event_type,
                    "event_key": event_key,
                    "event_bucket": "lebprime_broker_lifecycle",
                    "status": entry.get("broker_status"),
                    "symbol": entry.get("broker_symbol") or entry.get("logical_symbol"),
                    "side": entry.get("side"),
                    "setup": STRATEGY_NAME,
                    "setup_family": STRATEGY_NAME,
                    "entry_price": entry.get("fill_price") or entry.get("normalized_price") or entry.get("entry_price"),
                    "exit_price": entry.get("close_price"),
                    "stop_loss": entry.get("sl"),
                    "take_profit": entry.get("tp"),
                    "volume": entry.get("volume"),
                    "pnl": entry.get("realized_pnl"),
                    "commissions": entry.get("broker_commission"),
                    "swap": entry.get("broker_swap"),
                    "fees": entry.get("broker_fee"),
                    "close_reason": entry.get("final_exit_reason"),
                    "execution_reason": entry.get("failure_reason"),
                    "closed_at": entry.get("broker_closed_at"),
                    "metadata": {
                        "zone_id": entry.get("zone_id"),
                        "entry_number": entry.get("entry_number"),
                        "broker_status": entry.get("broker_status"),
                        **(metadata or {}),
                    },
                }
            )
        except Exception as exc:
            self._log(
                "lebprime_lifecycle_journal_failed",
                {"event_key": event_key, "error": str(exc)},
                level="WARNING",
            )

    def _reconcile_broker_execution(self, now: datetime) -> None:
        """Reconcile LebPrime lifecycle fields exclusively from current MT5 state/history."""
        for logical_symbol in self._selected_symbols():
            entries = self.repository.broker_entries_for_reconciliation(logical_symbol)
            if not entries:
                continue
            connector = self._connector(logical_symbol)
            open_orders = connector.get_pending_orders() if hasattr(connector, "get_pending_orders") else []
            positions = connector.get_bot_positions() if hasattr(connector, "get_bot_positions") else []
            orders_history = connector.get_bot_order_history() if hasattr(connector, "get_bot_order_history") else []
            bot_deals = connector.get_bot_deal_history() if hasattr(connector, "get_bot_deal_history") else []
            position_model = self._position_model(connector)
            mt5_api = getattr(connector, "mt5", None)
            terminal_order_states = {
                int(getattr(mt5_api, "ORDER_STATE_CANCELED", -991)): ("CANCELED", "CANCELED"),
                int(getattr(mt5_api, "ORDER_STATE_EXPIRED", -992)): ("EXPIRED", "EXPIRED"),
                int(getattr(mt5_api, "ORDER_STATE_REJECTED", -993)): ("REJECTED", "REJECTED"),
            }

            for entry in entries:
                previous_status = str(entry.get("broker_status") or "")
                ticket = str(entry.get("mt5_order_ticket") or "")
                comment = str(entry.get("comment") or "")

                def matches_order(item: Any) -> bool:
                    item_ids = {
                        str(self._item_value(item, "ticket", "") or ""),
                        str(self._item_value(item, "order", "") or ""),
                    }
                    return bool(comment and str(self._item_value(item, "comment", "") or "") == comment) or bool(ticket and ticket in item_ids)

                pending = next((item for item in open_orders if matches_order(item)), None)
                matching_deals = [item for item in bot_deals if matches_order(item)]
                fill_deals = [item for item in matching_deals if self._deal_is_entry(connector, item)]
                fill_deal = min(fill_deals, key=lambda item: self._broker_time(item, now), default=None)
                history_order = next((item for item in orders_history if matches_order(item)), None)

                if pending is not None:
                    accepted_at = entry.get("broker_created_at") or self._broker_time(pending, now)
                    entry.update(
                        {
                            "broker_status": "PENDING",
                            "mt5_order_ticket": str(self._item_value(pending, "ticket", ticket) or ticket),
                            "broker_created_at": accepted_at,
                            "failure_reason": None,
                            "last_failure": None,
                            "retryable": 0,
                            "next_retry_at": None,
                            "execution_eligible": 1,
                            "updated_at": now.isoformat(),
                        }
                    )
                    self.repository.upsert_broker_entry(entry)
                    if previous_status not in {"PENDING", "FILLED", "CLOSED"}:
                        self._journal_broker_transition(entry, "ORDER_ACCEPTED", accepted_at)
                    continue

                position_ticket = str(
                    (self._item_value(fill_deal, "position_id", "") if fill_deal is not None else "")
                    or entry.get("mt5_position_ticket")
                    or ""
                )
                position = next(
                    (
                        item for item in positions
                        if position_ticket and str(self._item_value(item, "ticket", "") or "") == position_ticket
                    ),
                    None,
                )
                if position is None and comment:
                    position = next(
                        (item for item in positions if str(self._item_value(item, "comment", "") or "") == comment),
                        None,
                    )
                    if position is not None:
                        position_ticket = str(self._item_value(position, "ticket", "") or position_ticket)

                position_deals: list[Any] = []
                if position_ticket and hasattr(connector, "get_position_deals"):
                    try:
                        position_deals = list(
                            connector.get_position_deals(
                                position_ticket,
                                entry.get("broker_created_at") or entry.get("broker_filled_at"),
                            )
                            or []
                        )
                    except Exception as exc:
                        self._log(
                            "lebprime_position_history_unavailable",
                            {"position_ticket": position_ticket, "error": str(exc)},
                            level="WARNING",
                        )
                if not position_deals and position_ticket:
                    position_deals = [
                        item for item in bot_deals
                        if str(self._item_value(item, "position_id", "") or "") == position_ticket
                    ]
                if fill_deal is None and position_deals:
                    entry_deals = [item for item in position_deals if self._deal_is_entry(connector, item)]
                    fill_deal = min(entry_deals, key=lambda item: self._broker_time(item, now), default=None)

                if fill_deal is not None or position_ticket:
                    fill_price = (
                        self._item_value(fill_deal, "price", None)
                        if fill_deal is not None
                        else entry.get("fill_price")
                    )
                    filled_volume = (
                        self._number(fill_deal, "volume")
                        if fill_deal is not None
                        else float(entry.get("filled_volume") or entry.get("volume") or 0.0)
                    )
                    filled_at = entry.get("broker_filled_at") or (
                        self._broker_time(fill_deal, now) if fill_deal is not None else None
                    )
                    if fill_deal is not None:
                        entry["mt5_deal_ticket"] = str(self._item_value(fill_deal, "ticket", "") or entry.get("mt5_deal_ticket") or "")
                        entry["mt5_order_ticket"] = ticket or str(self._item_value(fill_deal, "order", "") or "")
                    entry.update(
                        {
                            "mt5_position_ticket": position_ticket,
                            "fill_price": fill_price,
                            "broker_filled_at": filled_at,
                            "filled_volume": filled_volume or entry.get("filled_volume"),
                            "volume": filled_volume or entry.get("volume"),
                            "position_model": position_model,
                            "failure_reason": None,
                            "last_failure": None,
                            "retryable": 0,
                            "next_retry_at": None,
                            "execution_eligible": 1,
                            "updated_at": now.isoformat(),
                        }
                    )

                    closing_deals = sorted(
                        [item for item in position_deals if self._deal_is_close(connector, item)],
                        key=lambda item: self._broker_time(item, now),
                    )
                    close_tickets = [
                        str(self._item_value(item, "ticket", "") or "")
                        for item in closing_deals
                        if self._item_value(item, "ticket", None) is not None
                    ]
                    prior_close_tickets = {
                        str(item) for item in self._json_list(entry.get("close_deal_tickets_json"))
                    }
                    new_closing_deals = [
                        item for item in closing_deals
                        if str(self._item_value(item, "ticket", "") or "") not in prior_close_tickets
                    ]
                    closed_volume = sum(self._number(item, "volume") for item in closing_deals)
                    remaining_volume = (
                        self._number(position, "volume")
                        if position is not None
                        else max(0.0, filled_volume - closed_volume)
                    )
                    entry.update(
                        {
                            "closed_volume": closed_volume,
                            "remaining_volume": remaining_volume,
                            "partial_close_count": len(closing_deals) if position is not None else max(0, len(closing_deals) - 1),
                            "close_deal_tickets": close_tickets,
                            "lifecycle_details": {
                                "position_model": position_model,
                                "attribution": "PER_ENTRY" if position_model == "HEDGING" else "INCOMPLETE_SHARED_OR_UNKNOWN",
                                "deals": [self._deal_snapshot(item) for item in position_deals],
                            },
                        }
                    )

                    if previous_status not in {"FILLED", "CLOSED"}:
                        entry["broker_status"] = "FILLED"
                        self.repository.upsert_broker_entry(entry)
                        self.repository.set_zone_execution_status(str(entry["zone_id"]), "ACTIVE", now.isoformat())
                        transition_time = filled_at or now.isoformat()
                        self._journal_broker_transition(entry, "ORDER_FILLED", transition_time)
                        self._journal_broker_transition(entry, "POSITION_OPENED", transition_time)
                        context = self.contexts.get(str(entry["context_key"]))
                        if context is not None:
                            context.realtime_activations += 1
                            self._record_event(
                                context,
                                str(entry["zone_id"]),
                                f"ENTRY{entry['entry_number']}_ACTIVATED",
                                transition_time,
                                price=float(fill_price or entry.get("normalized_price") or 0.0),
                                broker_order_id=entry.get("mt5_order_ticket"),
                                metadata={
                                    "broker_confirmed": True,
                                    "deal_ticket": entry.get("mt5_deal_ticket"),
                                    "position_ticket": position_ticket,
                                    "volume": filled_volume,
                                },
                            )
                        self._log(
                            "lebprime_pending_order_filled",
                            {
                                "zone_id": entry["zone_id"],
                                "entry_number": entry["entry_number"],
                                "order_ticket": entry.get("mt5_order_ticket"),
                                "deal_ticket": entry.get("mt5_deal_ticket"),
                                "position_ticket": position_ticket,
                                "requested_price": entry.get("entry_price"),
                                "fill_price": fill_price,
                            },
                        )

                    if position is not None:
                        entry["broker_status"] = "FILLED"
                        entry["outcome_complete"] = 0
                        if position_model == "HEDGING" and closing_deals:
                            entry["partial_realized_pnl"] = sum(
                                self._number(item, "profit")
                                + self._number(item, "commission")
                                + self._number(item, "swap")
                                + self._number(item, "fee")
                                for item in closing_deals
                            )
                        self.repository.upsert_broker_entry(entry)
                        if position_model == "HEDGING":
                            for close_deal in new_closing_deals:
                                deal_ticket = str(self._item_value(close_deal, "ticket", "") or "")
                                self._journal_broker_transition(
                                    entry,
                                    "POSITION_PARTIAL_CLOSE",
                                    self._broker_time(close_deal, now),
                                    event_suffix=deal_ticket,
                                    metadata={"deal": self._deal_snapshot(close_deal)},
                                )
                        continue

                    if closing_deals:
                        final_deal = closing_deals[-1]
                        tolerance = max(1e-8, filled_volume * 1e-6)
                        complete = bool(
                            position_model == "HEDGING"
                            and filled_volume > 0
                            and closed_volume + tolerance >= filled_volume
                        )
                        close_time = self._broker_time(final_deal, now)
                        entry.update(
                            {
                                "broker_status": "CLOSED",
                                "broker_closed_at": close_time,
                                "remaining_volume": 0.0,
                                "outcome_complete": int(complete),
                                "final_exit_reason": self._classify_exit_reason(connector, final_deal) if complete else "UNKNOWN",
                            }
                        )
                        if complete:
                            total_volume = sum(self._number(item, "volume") for item in closing_deals)
                            entry["close_price"] = (
                                sum(self._number(item, "price") * self._number(item, "volume") for item in closing_deals) / total_volume
                                if total_volume > 0
                                else None
                            )
                            entry["broker_profit"] = sum(self._number(item, "profit") for item in position_deals)
                            entry["broker_commission"] = sum(self._number(item, "commission") for item in position_deals)
                            entry["broker_swap"] = sum(self._number(item, "swap") for item in position_deals)
                            entry["broker_fee"] = sum(self._number(item, "fee") for item in position_deals)
                            entry["realized_pnl"] = (
                                entry["broker_profit"]
                                + entry["broker_commission"]
                                + entry["broker_swap"]
                                + entry["broker_fee"]
                            )
                        else:
                            entry.update(
                                {
                                    "close_price": None,
                                    "realized_pnl": None,
                                    "broker_profit": None,
                                    "broker_commission": None,
                                    "broker_swap": None,
                                    "broker_fee": None,
                                }
                            )
                        self.repository.upsert_broker_entry(entry)
                        if position_model == "HEDGING":
                            partial_deals = new_closing_deals[:-1] if previous_status != "CLOSED" else new_closing_deals
                            for close_deal in partial_deals:
                                deal_ticket = str(self._item_value(close_deal, "ticket", "") or "")
                                self._journal_broker_transition(
                                    entry,
                                    "POSITION_PARTIAL_CLOSE",
                                    self._broker_time(close_deal, now),
                                    event_suffix=deal_ticket,
                                    metadata={"deal": self._deal_snapshot(close_deal)},
                                )
                        if previous_status != "CLOSED":
                            self._journal_broker_transition(
                                entry,
                                "POSITION_CLOSED",
                                close_time,
                                metadata={"outcome_complete": complete, "position_model": position_model},
                            )
                            self._log(
                                "lebprime_broker_position_closed",
                                {
                                    "zone_id": entry["zone_id"],
                                    "entry_number": entry["entry_number"],
                                    "position_ticket": position_ticket,
                                    "close_deal_tickets": close_tickets,
                                    "close_price": entry.get("close_price"),
                                    "realized_pnl": entry.get("realized_pnl"),
                                    "final_exit_reason": entry.get("final_exit_reason"),
                                    "outcome_complete": complete,
                                    "position_model": position_model,
                                },
                            )
                        continue

                    entry["broker_status"] = "FILLED"
                    self.repository.upsert_broker_entry(entry)
                    continue

                if history_order is not None and previous_status in {"PLACING", "RETRY_SCHEDULED", "PENDING", "ORDER_HISTORY"}:
                    try:
                        order_state = int(self._item_value(history_order, "state", -1))
                    except (TypeError, ValueError):
                        order_state = -1
                    terminal = terminal_order_states.get(order_state)
                    if terminal is None:
                        entry.update({"broker_status": "ORDER_HISTORY", "updated_at": now.isoformat()})
                        self.repository.upsert_broker_entry(entry)
                        continue
                    status, exit_reason = terminal
                    terminal_at = self._broker_time(history_order, now)
                    failure_reason = None
                    if status == "REJECTED":
                        failure_reason = str(
                            self._item_value(history_order, "comment", "")
                            or self._item_value(history_order, "external_id", "")
                            or "BROKER_REJECTED"
                        )
                    entry.update(
                        {
                            "broker_status": status,
                            "broker_closed_at": terminal_at,
                            "canceled_at": terminal_at if status == "CANCELED" else entry.get("canceled_at"),
                            "expired_at": terminal_at if status == "EXPIRED" else entry.get("expired_at"),
                            "final_exit_reason": exit_reason,
                            "failure_reason": failure_reason,
                            "retryable": 0,
                            "next_retry_at": None,
                            "execution_eligible": 0,
                            "outcome_complete": 0,
                            "lifecycle_details": {
                                "order_state": order_state,
                                "order_comment": self._item_value(history_order, "comment"),
                                "order_reason": self._item_value(history_order, "reason"),
                            },
                            "updated_at": now.isoformat(),
                        }
                    )
                    self.repository.upsert_broker_entry(entry)
                    if previous_status != status:
                        event_type = {
                            "CANCELED": "ORDER_CANCELED",
                            "EXPIRED": "ORDER_EXPIRED",
                            "REJECTED": "ORDER_REJECTED",
                        }[status]
                        self._journal_broker_transition(entry, event_type, terminal_at)


    @staticmethod
    def _broker_time(item: Any, fallback: datetime) -> str:
        for field in ("time_done_msc", "time_update_msc", "time_msc", "time_done", "time_update", "time", "time_expiration"):
            raw = LebPrimeLiveService._item_value(item, field)
            if raw in (None, "", 0):
                continue
            try:
                unit = "ms" if field.endswith("_msc") or float(raw) > 10_000_000_000 else "s"
                return pd.to_datetime(raw, unit=unit, utc=True).isoformat()
            except Exception:
                continue
        return fallback.isoformat()

    def _manage_broker_lifecycle(self, context: LiveContext, zone: ZoneRecord, event_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply engine lifecycle decisions to the primary family's broker orders."""
        connector = self._connector(context.logical_symbol)
        actions: list[dict[str, Any]] = []
        positions: list[Any] = []
        for label in ("ENTRY1", "ENTRY2"):
            order_id = payload.get(f"{label.lower()}_order_id")
            position = None
            if hasattr(connector, "find_bot_position"):
                position = connector.find_bot_position(order_id, self._order_comment(zone.engine_zone_id, label))
            if position is not None and all(str(getattr(item, "ticket", "")) != str(getattr(position, "ticket", "")) for item in positions):
                positions.append(position)

        if event_type in {"ENTRY1_ACTIVATED", "ENTRY2_ACTIVATED"}:
            actions.append({"action": "candle_touch_observed", "broker_authoritative": True, "proven_positions": [str(getattr(item, "ticket", "")) for item in positions]})
            return actions
        if event_type == "TP1_HIT":
            entry2 = self.repository.broker_entry(zone.engine_zone_id, 2)
            if entry2 and entry2.get("broker_status") in {"PLACING", "PENDING"} and entry2.get("mt5_order_ticket"):
                actions.append(self._cancel_broker_entry(context, zone, entry2, "TP1_HIT"))
            for position in positions:
                volume = float(getattr(position, "volume", 0.0) or 0.0)
                close_volume = volume / 2.0
                try:
                    ok, result = connector.close_partial(getattr(position, "ticket"), close_volume, comment="LP340-TP1")
                    actions.append({"action": "partial_close", "ticket": str(getattr(position, "ticket", "")), "ok": bool(ok), "result": str(result)})
                except Exception as exc:
                    actions.append({"action": "partial_close", "ticket": str(getattr(position, "ticket", "")), "ok": False, "error": str(exc)})
            return actions
        if event_type in {"CANCELED", "EXPIRED"}:
            for entry in self.repository.broker_entries_for_zone(zone.engine_zone_id):
                if entry.get("broker_status") in {"PLACING", "PENDING"} and entry.get("mt5_order_ticket"):
                    actions.append(self._cancel_broker_entry(context, zone, entry, event_type))
                elif bool(entry.get("retryable")) and not entry.get("mt5_order_ticket"):
                    terminal_at = _utc_now().isoformat()
                    entry.update(
                        {
                            "broker_status": "NOT_PLACED",
                            "final_exit_reason": "UNKNOWN",
                            "failure_reason": f"{event_type}_BEFORE_BROKER_ACCEPTANCE",
                            "last_failure": f"{event_type}_BEFORE_BROKER_ACCEPTANCE",
                            "retryable": 0,
                            "next_retry_at": None,
                            "execution_eligible": 0,
                            "broker_closed_at": terminal_at,
                            "updated_at": terminal_at,
                        }
                    )
                    self.repository.upsert_broker_entry(entry)
                    actions.append({"action": "stop_retry", "entry_number": entry["entry_number"], "ok": True, "reason": event_type})
            return actions
        if event_type in {"TP2_HIT", "TP1_FINAL", "SL_HIT"}:
            for position in positions:
                try:
                    ok, result = connector.close_position(getattr(position, "ticket"), comment=f"LP340-{event_type}")
                    actions.append({"action": "close_position", "ticket": str(getattr(position, "ticket", "")), "ok": bool(ok), "result": str(result)})
                except Exception as exc:
                    actions.append({"action": "close_position", "ticket": str(getattr(position, "ticket", "")), "ok": False, "error": str(exc)})
        return actions

    def _cancel_broker_entry(self, context: LiveContext, zone: ZoneRecord, entry: dict[str, Any], reason: str) -> dict[str, Any]:
        connector = self._connector(context.logical_symbol)
        ticket = str(entry.get("mt5_order_ticket") or "")
        try:
            ok, result = connector.cancel_pending_order(ticket)
            confirmed = bool(ok)
            if confirmed and hasattr(connector, "get_pending_orders"):
                confirmed = not any(str(self._item_value(item, "ticket", "") or "") == ticket for item in connector.get_pending_orders())
            if confirmed:
                terminal_at = _utc_now().isoformat()
                entry.update({"broker_status": "CANCELED", "broker_closed_at": terminal_at, "canceled_at": terminal_at, "final_exit_reason": "CANCELED", "broker_retcode": str(self._item_value(result, "retcode", "") or entry.get("broker_retcode") or ""), "failure_reason": None, "updated_at": terminal_at})
                self.repository.upsert_broker_entry(entry)
                self._journal_broker_transition(entry, "ORDER_CANCELED", terminal_at)
                self._log("lebprime_pending_order_cancelled", {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry["entry_number"], "ticket": ticket, "reason": reason})
            else:
                entry.update({"failure_reason": f"CANCEL_NOT_CONFIRMED:{reason}", "updated_at": _utc_now().isoformat()})
                self.repository.upsert_broker_entry(entry)
                self._log("lebprime_pending_order_skipped", {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry["entry_number"], "ticket": ticket, "reason": "CANCEL_NOT_CONFIRMED"}, level="ERROR")
            return {"action": "cancel_pending", "order_id": ticket, "entry_number": entry["entry_number"], "ok": confirmed, "result": str(result)}
        except Exception as exc:
            entry.update({"failure_reason": f"CANCEL_FAILED:{exc}", "updated_at": _utc_now().isoformat()})
            self.repository.upsert_broker_entry(entry)
            self._log("lebprime_pending_order_skipped", {"context": context.key, "zone_id": zone.engine_zone_id, "entry_number": entry["entry_number"], "ticket": ticket, "reason": f"CANCEL_FAILED:{exc}"}, level="ERROR")
            return {"action": "cancel_pending", "order_id": ticket, "entry_number": entry["entry_number"], "ok": False, "error": str(exc)}

    def _record_event(
        self,
        context: LiveContext,
        zone_id: str,
        event_type: str,
        event_time: str,
        *,
        price: float | None = None,
        family_id: str | None = None,
        broker_order_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.repository.append_event(
            {
                "logical_symbol": context.logical_symbol,
                "mt5_symbol": context.mt5_symbol,
                "timeframe": context.timeframe,
                "zone_id": zone_id,
                "family_id": family_id,
                "event_type": event_type,
                "event_time": event_time,
                "price": price,
                "broker_order_id": broker_order_id,
                "metadata": metadata or {},
            }
        )

    def _persist_context(self, context: LiveContext, now: datetime) -> None:
        metrics = context.engine.metrics()
        self.repository.upsert_context(
            {
                "context_key": context.key,
                "logical_symbol": context.logical_symbol,
                "mt5_symbol": context.mt5_symbol,
                "timeframe": context.timeframe,
                "status": context.status,
                "last_closed_candle": context.last_closed_candle,
                "last_scan_time": context.last_scan_time,
                "candidate_count": len(context.engine._candidate_diagnostics),
                "live_zones": sum(1 for zone in context.engine.zones() if not zone.terminal_state),
                "pending_limits": metrics.get("pending_limits", 0),
                "active_trades": metrics.get("active_trades", 0),
                "historical_active_trades": context.historical_active_trades,
                "realtime_activations": context.realtime_activations,
                "last_error": context.last_error or context.last_execution_error,
                "heartbeat": context.heartbeat,
                "updated_at": now.isoformat(),
            }
        )

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = self.repository.status_snapshot()
        persisted = {row["context_key"]: row for row in snapshot["contexts"]}
        contexts = []
        for context in self.contexts.values():
            row = persisted.get(context.key, {})
            contexts.append(
                {
                    "context_key": context.key,
                    "logical_symbol": context.logical_symbol,
                    "mt5_symbol": context.mt5_symbol,
                    "timeframe": context.timeframe,
                    "status": context.status if context.status != "IDLE" else row.get("status", "IDLE"),
                    "last_closed_candle": context.last_closed_candle or row.get("last_closed_candle"),
                    "last_scan_time": context.last_scan_time or row.get("last_scan_time"),
                    "candidate_count": len(context.engine._candidate_diagnostics) or row.get("candidate_count", 0),
                    "live_zones": sum(1 for zone in context.engine.zones() if not zone.terminal_state) or row.get("live_zones", 0),
                    "pending_limits": context.engine.metrics().get("pending_limits", 0) or row.get("pending_limits", 0),
                    "active_trades": context.engine.metrics().get("active_trades", 0) or row.get("active_trades", 0),
                    "historical_active_trades": context.historical_active_trades or row.get("historical_active_trades", 0),
                    "realtime_activations": context.realtime_activations or row.get("realtime_activations", 0),
                    "last_error": context.last_error or context.last_execution_error or row.get("last_error"),
                    "heartbeat": context.heartbeat or row.get("heartbeat"),
                }
            )
        runtime_state = snapshot.get("runtime_state", {}) or {}
        heartbeat = pd.to_datetime(runtime_state.get("heartbeat"), utc=True, errors="coerce")
        stale_seconds = int(self.config.get("dashboard", {}).get("stale_heartbeat_seconds", 120) or 120)
        runtime_fresh = bool(not pd.isna(heartbeat) and (_utc_now() - heartbeat.to_pydatetime()).total_seconds() <= stale_seconds)
        operational = bool(self.started or (runtime_state.get("started") and runtime_fresh))
        return {
            "strategy": STRATEGY_NAME,
            "enabled": self.enabled,
            "execution_mode": "LIVE" if self.live_requested else "DRY",
            "started": operational,
            "startup_block_reason": self.startup_block_reason or runtime_state.get("startup_block_reason"),
            "service_heartbeat": runtime_state.get("heartbeat"),
            "service_pid": runtime_state.get("pid"),
            "contexts": contexts,
            "context_count": len(contexts),
            "symbols": self._selected_symbols(),
            "timeframes": self._selected_timeframes(),
            **{key: value for key, value in snapshot.items() if key != "contexts"},
        }

    def dashboard_trade_snapshot(self, limit_per_category: int = 200) -> dict[str, Any]:
        """Expose the persisted broker lifecycle without mutating runtime state."""
        return self.repository.dashboard_trade_snapshot(limit_per_category=limit_per_category)

    def dashboard_performance_snapshot(self) -> dict[str, Any]:
        """Expose persisted LebPrime lifecycle counts for dashboard reporting."""
        return self.repository.dashboard_performance_snapshot()

    def shutdown(self) -> None:
        for connector in self.broker_gateways.values():
            if hasattr(connector, "shutdown"):
                connector.shutdown()
        self.started = False
        self._persist_runtime_state(False, None)
