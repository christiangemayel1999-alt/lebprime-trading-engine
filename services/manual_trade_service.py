"""Shared manual trade execution and position-management service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from logger import BotLogger
from mt5_connector import MT5Connector, mt5
from services.audit_service import AuditService
from services.config_manager import ConfigManager
from services.control_state import ControlStateService
from services.database import DatabaseService
from services.trade_journal import TradeJournalService
from trading_bot.core.models import ExecutionRequest
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.mt5_broker import MT5Broker
from utils import normalize_price


@dataclass(slots=True)
class ManualActionResult:
    """Normalized response payload for manual trading actions."""

    ok: bool
    message: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dictionary payload."""
        return {"ok": self.ok, "message": self.message, **self.data}


class ManualTradeService:
    """Execute validated manual trade actions for dashboard and Telegram."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        control_state: ControlStateService,
        audit_service: AuditService,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.control_state = control_state
        self.audit_service = audit_service
        self.logger = BotLogger(self.base_dir, self.config_manager.load_runtime())
        self.database = DatabaseService(self.base_dir / self.config_manager.load_runtime()["storage"]["database_path"])
        self.trade_journal = TradeJournalService(self.base_dir, self.database, self.logger, self.config_manager.load_runtime())

    def list_positions(self, actor: str, source: str) -> dict[str, Any]:
        """Return current bot-managed positions for the configured symbol."""
        try:
            config = self.config_manager.load_runtime()
            connector = self._connector(config)
            self._ensure_connection(connector)
            positions = [self._format_position(position) for position in connector.get_bot_positions()]
            return {
                "ok": True,
                "positions": positions,
                "count": len(positions),
                "source": source,
            }
        except Exception as exc:
            self.audit_service.log_action(
                actor,
                "manual_positions_error",
                config.get("mt5", {}).get("symbol", "UNKNOWN") if "config" in locals() else "UNKNOWN",
                {"source": source, "error": str(exc)},
                level="WARNING",
            )
            return {"ok": False, "positions": [], "count": 0, "message": str(exc), "source": source}

    def open_market_trade(
        self,
        actor: str,
        source: str,
        symbol: str,
        side: str,
        volume: float,
        sl: float | None,
        tp: float | None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Open a manually requested market trade after safety validation."""
        action_name = "manual_open_trade"
        try:
            config = self.config_manager.load_runtime()
            control = self.control_state.summarize()
            connector = self._connector(config)
            self._validate_manual_open(config, control, connector, symbol, side, volume, sl, tp)
            normalized_side = self._normalize_side(side)
            normalized_volume = connector.normalize_volume(float(volume))
            tag = self._manual_comment_tag(config, source)
            trade_comment = self._compose_comment(tag, comment)
            result = self._execution_service(connector).submit_order(
                ExecutionRequest(
                    strategy_name="MANUAL",
                    symbol=str(symbol).upper(),
                    side=normalized_side,
                    order_type="market",
                    entry_price=0.0,
                    volume=normalized_volume,
                    stop_loss=float(sl) if sl is not None else 0.0,
                    take_profit=float(tp) if tp is not None else 0.0,
                    metadata={"comment": trade_comment, "dry_run": False, "source": source},
                )
            )
            payload = {
                "source": source,
                "symbol": symbol,
                "side": normalized_side,
                "requested_volume": float(volume),
                "final_volume": float(result.get("volume", normalized_volume)),
                "sl": float(sl) if sl is not None else None,
                "tp": float(tp) if tp is not None else None,
                "comment": trade_comment,
                "result": result,
                "ticket": self._extract_ticket(result.get("result")),
                "executed": bool(result.get("executed", False)),
                "reason": result.get("reason"),
                "execution_blocked_reason": result.get("execution_blocked_reason"),
            }
            level = "INFO" if result.get("executed") else "WARNING"
            self.audit_service.log_action(actor, action_name, symbol, payload, level=level)
            if result.get("executed"):
                ticket = str(payload.get("ticket") or result.get("result", {}).get("order") or result.get("result", {}).get("deal") or f"manual-{datetime.utcnow().timestamp()}")
                self.trade_journal.record_trade_open(
                    {
                        "timestamp": datetime.utcnow().isoformat(),
                        "trade_id": ticket,
                        "mt5_ticket": ticket,
                        "position_id": ticket,
                        "order_id": str(result.get("result", {}).get("order") or ""),
                        "event_type": "TRADE_OPENED",
                        "status": "OPENED",
                        "symbol": symbol,
                        "side": normalized_side,
                        "setup": f"MANUAL:{source}",
                        "setup_family": "MANUAL",
                        "regime": "MANUAL",
                        "session": "MANUAL",
                        "entry_mode": "manual",
                        "execution_reason": "manual_trade_executed",
                        "entry_price": result.get("result", {}).get("price"),
                        "stop_loss": sl,
                        "take_profit": tp,
                        "volume": float(result.get("volume", normalized_volume)),
                        "magic_number": config.get("mt5", {}).get("magic_number"),
                        "comment": trade_comment,
                        "metadata": payload,
                    }
                )
            message = "Manual trade executed" if result.get("executed") else f"Manual trade rejected: {result.get('reason')}"
            return ManualActionResult(bool(result.get("executed")), message, payload).as_dict()
        except Exception as exc:
            payload = {
                "source": source,
                "symbol": symbol,
                "side": side,
                "volume": float(volume),
                "sl": sl,
                "tp": tp,
                "error": str(exc),
            }
            self.audit_service.log_action(actor, action_name, symbol, payload, level="WARNING")
            return ManualActionResult(False, str(exc), payload).as_dict()

    def close_position(self, actor: str, source: str, ticket: int) -> dict[str, Any]:
        """Close a full position by ticket."""
        return self._close_position_volume(actor, source, ticket, volume=None, action_name="manual_close_position")

    def partial_close(self, actor: str, source: str, ticket: int, volume: float) -> dict[str, Any]:
        """Close a partial position volume."""
        return self._close_position_volume(actor, source, ticket, volume=float(volume), action_name="manual_partial_close")

    def modify_position(
        self,
        actor: str,
        source: str,
        ticket: int,
        sl: float | None,
        tp: float | None,
    ) -> dict[str, Any]:
        """Modify SL and/or TP for an open position."""
        action_name = "manual_modify_position"
        try:
            config = self.config_manager.load_runtime()
            connector = self._connector(config)
            position = self._require_position(connector, ticket)
            self._validate_position_targets(connector, position, sl, tp)
            response = self._execution_service(connector).modify_position(
                str(ticket),
                sl=sl,
                tp=tp,
                reason=self._compose_comment(self._manual_comment_tag(config, source), "MODIFY"),
            )
            success = bool(response.get("ok"))
            payload = {
                "source": source,
                "ticket": int(ticket),
                "symbol": getattr(position, "symbol", config.get("mt5", {}).get("symbol", "")),
                "sl": sl,
                "tp": tp,
                "result": self._serialize(response.get("result", response)),
            }
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="INFO" if success else "WARNING")
            return ManualActionResult(bool(success), "Position modified" if success else "Position modification failed", payload).as_dict()
        except Exception as exc:
            payload = {"source": source, "ticket": int(ticket), "sl": sl, "tp": tp, "error": str(exc)}
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="WARNING")
            return ManualActionResult(False, str(exc), payload).as_dict()

    def move_to_break_even(self, actor: str, source: str, ticket: int) -> dict[str, Any]:
        """Move stop-loss to the position entry price."""
        action_name = "manual_break_even"
        try:
            config = self.config_manager.load_runtime()
            connector = self._connector(config)
            position = self._require_position(connector, ticket)
            break_even_sl = normalize_price(float(getattr(position, "price_open", 0.0)), int(connector.get_symbol_spec()["digits"]))
            tp = float(getattr(position, "tp", 0.0) or 0.0)
            self._validate_position_targets(connector, position, break_even_sl, tp if tp > 0 else None)
            response = self._execution_service(connector).modify_position(
                str(ticket),
                sl=break_even_sl,
                tp=tp if tp > 0 else None,
                reason=self._compose_comment(self._manual_comment_tag(config, source), "BREAKEVEN"),
            )
            success = bool(response.get("ok"))
            payload = {
                "source": source,
                "ticket": int(ticket),
                "symbol": getattr(position, "symbol", config.get("mt5", {}).get("symbol", "")),
                "sl": break_even_sl,
                "tp": tp if tp > 0 else None,
                "result": self._serialize(response.get("result", response)),
            }
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="INFO" if success else "WARNING")
            return ManualActionResult(bool(success), "Break-even applied" if success else "Break-even update failed", payload).as_dict()
        except Exception as exc:
            payload = {"source": source, "ticket": int(ticket), "error": str(exc)}
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="WARNING")
            return ManualActionResult(False, str(exc), payload).as_dict()

    def close_all_positions(self, actor: str, source: str) -> dict[str, Any]:
        """Close all current bot-managed positions."""
        action_name = "manual_close_all_positions"
        try:
            config = self.config_manager.load_runtime()
            connector = self._connector(config)
            self._ensure_connection(connector)
            positions = list(connector.get_bot_positions())
            results: list[dict[str, Any]] = []
            execution_service = self._execution_service(connector)
            for position in positions:
                ticket = int(getattr(position, "ticket", 0))
                response = execution_service.close_position(
                    str(ticket),
                    self._compose_comment(self._manual_comment_tag(config, source), "CLOSEALL"),
                )
                success = bool(response.get("ok"))
                results.append(
                    {
                        "ticket": ticket,
                        "symbol": getattr(position, "symbol", config.get("mt5", {}).get("symbol", "")),
                        "ok": bool(success),
                        "result": self._serialize(response.get("result", response)),
                    }
                )
            overall_ok = all(item["ok"] for item in results) if results else True
            payload = {"source": source, "count": len(results), "results": results}
            self.audit_service.log_action(actor, action_name, config["mt5"]["symbol"], payload, level="INFO" if overall_ok else "WARNING")
            return ManualActionResult(overall_ok, f"Processed {len(results)} open positions", payload).as_dict()
        except Exception as exc:
            payload = {"source": source, "error": str(exc)}
            self.audit_service.log_action(actor, action_name, "all_positions", payload, level="WARNING")
            return ManualActionResult(False, str(exc), payload).as_dict()

    def _close_position_volume(
        self,
        actor: str,
        source: str,
        ticket: int,
        volume: float | None,
        action_name: str,
    ) -> dict[str, Any]:
        """Shared implementation for full and partial closes."""
        try:
            config = self.config_manager.load_runtime()
            connector = self._connector(config)
            position = self._require_position(connector, ticket)
            normalized_volume = float(getattr(position, "volume", 0.0)) if volume is None else connector.normalize_volume(float(volume))
            if normalized_volume <= 0:
                raise RuntimeError("Close volume must be greater than 0")
            if normalized_volume - float(getattr(position, "volume", 0.0)) > 1e-9:
                raise RuntimeError("Partial close volume cannot exceed the open position volume")
            response = self._execution_service(connector).partial_close(
                str(ticket),
                normalized_volume,
                self._compose_comment(self._manual_comment_tag(config, source), "CLOSE"),
            )
            success = bool(response.get("ok"))
            payload = {
                "source": source,
                "ticket": int(ticket),
                "symbol": getattr(position, "symbol", config.get("mt5", {}).get("symbol", "")),
                "volume": normalized_volume,
                "result": self._serialize(response.get("result", response)),
            }
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="INFO" if success else "WARNING")
            if success:
                self.trade_journal.record_trade_close(
                    {
                        "timestamp": datetime.utcnow().isoformat(),
                        "trade_id": str(ticket),
                        "mt5_ticket": str(ticket),
                        "position_id": str(ticket),
                        "event_type": "TRADE_CLOSED",
                        "status": "PENDING_CLOSE_RESOLUTION",
                        "symbol": getattr(position, "symbol", config.get("mt5", {}).get("symbol", "")),
                        "side": "LONG" if int(getattr(position, "type", 0)) == mt5.POSITION_TYPE_BUY else "SHORT",
                        "setup": "MANUAL",
                        "setup_family": "MANUAL",
                        "regime": "MANUAL",
                        "session": "MANUAL",
                        "entry_mode": "manual",
                        "execution_reason": action_name,
                        "close_reason": action_name,
                        "exit_price": getattr(response.get("result"), "price", None) if hasattr(response.get("result"), "price") else None,
                        "volume": normalized_volume,
                        "comment": self._compose_comment(self._manual_comment_tag(config, source), "CLOSE"),
                        "metadata": payload,
                    }
                )
            message = "Position closed" if volume is None else "Partial close completed"
            return ManualActionResult(bool(success), message if success else "Close request failed", payload).as_dict()
        except Exception as exc:
            payload = {"source": source, "ticket": int(ticket), "volume": volume, "error": str(exc)}
            self.audit_service.log_action(actor, action_name, str(ticket), payload, level="WARNING")
            return ManualActionResult(False, str(exc), payload).as_dict()

    def _connector(self, config: dict[str, Any]) -> MT5Connector:
        """Return a fresh MT5 connector using the shared service logger."""
        return MT5Connector(config, self.logger)

    @staticmethod
    def _execution_service(connector: MT5Connector) -> ExecutionService:
        """Route manual dashboard/Telegram operations through the broker contract."""
        return ExecutionService(MT5Broker(connector))

    def _ensure_connection(self, connector: MT5Connector) -> dict[str, Any]:
        """Ensure MT5 is connected and the configured symbol is ready."""
        connection_stage = connector.connection_health_check(reconnect_if_needed=True)
        if not bool(connection_stage.get("ok")):
            raise RuntimeError(f"MT5 connection unavailable: {connection_stage.get('reason_code')}")
        symbol_stage = connector.symbol_prepare_stage(log_details=False)
        if not bool(symbol_stage.get("ok")):
            raise RuntimeError("Configured symbol is not ready for trading")
        tick_stage = connector.tick_fetch_stage(refresh=False)
        if not bool(tick_stage.get("ok")):
            raise RuntimeError(f"Unable to fetch live tick: {tick_stage.get('reason_code')}")
        return {
            "connection_ok": True,
            "symbol_ok": True,
            "tick_ok": True,
            "connection": connection_stage.get("payload", {}).get("connection", {}) or {},
            "symbol": symbol_stage.get("payload", {}).get("symbol", {}) or {},
            "tick": tick_stage.get("payload", {}).get("tick", {}) or {},
        }

    def _validate_manual_open(
        self,
        config: dict[str, Any],
        control: dict[str, Any],
        connector: MT5Connector,
        symbol: str,
        side: str,
        volume: float,
        sl: float | None,
        tp: float | None,
    ) -> None:
        """Validate a manual market-order request before sending it to MT5."""
        execution_cfg = config.get("execution", {})
        bot_cfg = config.get("bot", {})
        symbols_cfg = config.get("symbols", {})
        target_symbol = str(config.get("mt5", {}).get("symbol", ""))

        if not bool(execution_cfg.get("manual_trading_enabled", True)):
            raise RuntimeError("Manual trading is disabled in config")
        if control.get("kill_switch"):
            raise RuntimeError(f"Kill switch is active: {control.get('kill_switch_reason') or 'manual trading blocked'}")
        if str(symbol).upper() != target_symbol.upper():
            raise RuntimeError(f"Manual trading currently supports only the configured symbol: {target_symbol}")
        symbol_cfg = symbols_cfg.get(target_symbol, {})
        if symbol_cfg and (not bool(symbol_cfg.get("enabled", True)) or not bool(symbol_cfg.get("tradeable", True))):
            raise RuntimeError(f"Symbol {target_symbol} is disabled for trading in config")
        if normalize_price(float(volume), 2) <= 0:
            raise RuntimeError("Volume must be greater than 0")
        if sl is None or tp is None:
            raise RuntimeError("Manual market trades require both stop loss and take profit")
        if str(bot_cfg.get("trading_mode", "")).upper() != "LIVE" or not bool(bot_cfg.get("allow_live_execution", False)):
            raise RuntimeError("Manual live trading requires bot.trading_mode=LIVE and allow_live_execution=true")

        report = self._ensure_connection(connector)
        if not bool((report.get("connection", {}) or {}).get("trade_allowed", False)):
            raise RuntimeError("The connected MT5 account is not currently allowed to trade")
        spread_stage = connector.spread_check_stage(float(execution_cfg.get("max_spread_points", 25.0)))
        if not spread_stage.get("ok"):
            raise RuntimeError(f"Spread too wide for manual execution: {spread_stage.get('human_reason')}")
        symbol_spec = connector.get_symbol_spec()
        normalized_volume = connector.normalize_volume(float(volume), symbol_spec)
        if normalized_volume < float(config.get("risk", {}).get("min_lot", symbol_spec.get("volume_min", 0.01))):
            raise RuntimeError("Requested volume is below the allowed minimum")
        if normalized_volume > float(config.get("risk", {}).get("max_lot", symbol_spec.get("volume_max", normalized_volume))):
            raise RuntimeError("Requested volume exceeds the configured max lot size")
        side_name = self._normalize_side(side)
        tick_stage = connector.tick_fetch_stage(refresh=True)
        if not tick_stage.get("ok"):
            raise RuntimeError(f"Unable to refresh tick for manual execution: {tick_stage.get('human_reason')}")
        tick = tick_stage.get("payload", {}).get("tick", {}) or {}
        entry_price = float(tick.get("ask", 0.0)) if side_name == "LONG" else float(tick.get("bid", 0.0))
        self._validate_entry_targets(side_name, entry_price, sl, tp)

    def _validate_entry_targets(self, side: str, entry_price: float, sl: float | None, tp: float | None) -> None:
        """Validate open-order SL/TP values for the given side."""
        if entry_price <= 0:
            raise RuntimeError("Unable to determine the current market price for manual trade validation")
        if side == "LONG":
            if sl is not None and float(sl) >= entry_price:
                raise RuntimeError("For BUY orders, stop loss must be below the current ask price")
            if tp is not None and float(tp) <= entry_price:
                raise RuntimeError("For BUY orders, take profit must be above the current ask price")
        else:
            if sl is not None and float(sl) <= entry_price:
                raise RuntimeError("For SELL orders, stop loss must be above the current bid price")
            if tp is not None and float(tp) >= entry_price:
                raise RuntimeError("For SELL orders, take profit must be below the current bid price")

    def _validate_position_targets(self, connector: MT5Connector, position: Any, sl: float | None, tp: float | None) -> None:
        """Validate SL/TP modifications for an open position."""
        if sl is None and tp is None:
            raise RuntimeError("At least one of SL or TP must be provided")
        tick_stage = connector.tick_fetch_stage(refresh=True)
        if not tick_stage.get("ok"):
            raise RuntimeError(f"Unable to refresh tick for position validation: {tick_stage.get('human_reason')}")
        tick = tick_stage.get("payload", {}).get("tick", {}) or {}
        is_buy = int(getattr(position, "type", -1)) == int(getattr(mt5, "POSITION_TYPE_BUY", 0))
        current_price = float(tick.get("bid", 0.0)) if is_buy else float(tick.get("ask", 0.0))
        side = "LONG" if is_buy else "SHORT"
        self._validate_entry_targets(side, current_price, sl, tp)

    def _require_position(self, connector: MT5Connector, ticket: int) -> Any:
        """Load a position or raise a useful error."""
        self._ensure_connection(connector)
        position = connector.get_position_by_ticket(ticket)
        if position is None:
            raise RuntimeError(f"Position {ticket} was not found")
        if str(getattr(position, "symbol", "")).upper() != str(connector.symbol).upper():
            raise RuntimeError(f"Position {ticket} is not on the configured symbol {connector.symbol}")
        return position

    def _format_position(self, position: Any) -> dict[str, Any]:
        """Serialize an MT5 position into a dashboard-friendly payload."""
        opened_at = datetime.utcfromtimestamp(int(getattr(position, "time", 0))).isoformat() + "Z"
        position_type = int(getattr(position, "type", -1))
        side = "BUY" if position_type == int(getattr(mt5, "POSITION_TYPE_BUY", 0)) else "SELL"
        comment = str(getattr(position, "comment", "") or "")
        return {
            "ticket": int(getattr(position, "ticket", 0)),
            "position_id": int(getattr(position, "identifier", getattr(position, "ticket", 0))),
            "symbol": str(getattr(position, "symbol", "")),
            "side": side,
            "volume": float(getattr(position, "volume", 0.0)),
            "open_price": float(getattr(position, "price_open", 0.0)),
            "sl": float(getattr(position, "sl", 0.0)),
            "tp": float(getattr(position, "tp", 0.0)),
            "profit": float(getattr(position, "profit", 0.0)),
            "opened_at": opened_at,
            "comment": comment,
            "source": self._infer_position_source(comment),
            "magic": int(getattr(position, "magic", 0)),
        }

    @staticmethod
    def _normalize_side(side: str) -> str:
        """Normalize BUY/SELL/LONG/SHORT into LONG/SHORT."""
        normalized = str(side or "").strip().upper()
        if normalized in {"BUY", "LONG"}:
            return "LONG"
        if normalized in {"SELL", "SHORT"}:
            return "SHORT"
        raise RuntimeError("Side must be BUY or SELL")

    @staticmethod
    def _infer_position_source(comment: str) -> str:
        """Infer the trade source from the MT5 comment field."""
        upper = str(comment or "").upper()
        if upper.startswith("MANUAL_DASH"):
            return "dashboard"
        if upper.startswith("MANUAL_TG"):
            return "telegram"
        return "bot"

    @staticmethod
    def _compose_comment(tag: str, comment: str | None) -> str:
        """Compose a compact MT5 comment string."""
        extra = str(comment or "").strip().replace("\n", " ").replace("\r", " ")
        if not extra:
            return tag[:31]
        combined = f"{tag}:{extra}"
        return combined[:31]

    @staticmethod
    def _extract_ticket(result: Any) -> int | None:
        """Extract a useful order/deal ticket from an MT5 response payload."""
        if isinstance(result, dict):
            for key in ["order", "deal", "position", "ticket"]:
                value = result.get(key)
                if value not in (None, "", 0):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
        if hasattr(result, "_asdict"):
            return ManualTradeService._extract_ticket(result._asdict())
        return None

    def _manual_comment_tag(self, config: dict[str, Any], source: str) -> str:
        """Return the configured manual-trade MT5 comment tag."""
        execution_cfg = config.get("execution", {})
        if source == "telegram":
            return str(execution_cfg.get("manual_trade_comment_tag_telegram", "MANUAL_TG"))
        return str(execution_cfg.get("manual_trade_comment_tag_dashboard", "MANUAL_DASH"))

    @staticmethod
    def _serialize(value: Any) -> Any:
        """Serialize MT5 values to JSON-friendly data."""
        if hasattr(value, "_asdict"):
            return {str(key): ManualTradeService._serialize(item) for key, item in value._asdict().items()}
        if isinstance(value, dict):
            return {str(key): ManualTradeService._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [ManualTradeService._serialize(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
