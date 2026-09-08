from __future__ import annotations

from datetime import datetime
from typing import Any

from mt5_connector import MT5Connector, mt5
from trading_bot.core.models import AccountState, ExecutionRequest
from trading_bot.core.reasons import ReasonCode
from trading_bot.execution.broker_base import BrokerBase
from utils import normalize_price, to_utc


class MT5Broker(BrokerBase):
    """Live/demo broker adapter boundary for the unified execution contract."""

    mode = "LIVE"

    def __init__(self, connector: MT5Connector) -> None:
        self.connector = connector

    def submit_order(self, request: ExecutionRequest) -> dict[str, Any]:
        order_type = str(request.order_type or "market").lower()
        if order_type in {"market", "deal"}:
            result = self.connector.send_market_order(
                direction=str(request.side).upper(),
                volume=float(request.volume),
                sl=float(request.stop_loss),
                tp=float(request.take_profit),
                comment=self._comment(request),
                dry_run=bool(request.metadata.get("dry_run", False)),
            )
            result["ok"] = bool(result.get("executed")) or str(result.get("reason")) == "dry_run_validated"
            result["status"] = "filled" if result.get("executed") else ("validated" if result.get("ok") else "rejected")
            return result
        if order_type in {"limit", "pending", "lebprim_limit", "limit_value"}:
            return self._submit_limit_order(request)
        return {
            "ok": False,
            "status": "rejected",
            "reason": "unsupported_order_type",
            "request": self._request_payload(request),
        }

    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(order_id),
            "symbol": self.connector.symbol,
            "magic": int(self.connector.config["mt5"]["magic_number"]),
            "comment": str(reason or "cancel_order")[:31],
        }
        try:
            result = self.connector.safe_order_send(request, retry=False)
            ok = self._is_done(result)
            return {"ok": ok, "status": "cancelled" if ok else "rejected", "reason": reason, "order_id": str(order_id), "result": self._serialize(result)}
        except Exception as exc:
            return {"ok": False, "status": "failed", "reason": "cancel_order_failed", "order_id": str(order_id), "error": str(exc)}

    def modify_stop(self, position_id: str, new_stop: float, reason: str) -> dict[str, Any]:
        return self.modify_position(position_id, sl=float(new_stop), reason=reason)

    def modify_position(
        self,
        position_id: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        try:
            success, result = self.connector.modify_position_sltp(
                ticket=position_id,
                sl=sl,
                tp=tp,
                comment=str(reason or "modify_position")[:31],
            )
            return {
                "ok": bool(success),
                "status": "modified" if success else "rejected",
                "reason": reason,
                "position_id": str(position_id),
                "result": self._serialize(result),
            }
        except Exception as exc:
            return {"ok": False, "status": "failed", "reason": "modify_position_failed", "position_id": str(position_id), "error": str(exc)}

    def partial_close(self, position_id: str, volume: float, reason: str) -> dict[str, Any]:
        try:
            success, result = self.connector.close_partial(
                ticket=position_id,
                volume=float(volume),
                comment=str(reason or "partial_close")[:31],
            )
            return {
                "ok": bool(success),
                "status": "partial_closed" if success else "rejected",
                "reason": reason,
                "position_id": str(position_id),
                "volume": float(volume),
                "result": self._serialize(result),
            }
        except Exception as exc:
            return {"ok": False, "status": "failed", "reason": "partial_close_failed", "position_id": str(position_id), "error": str(exc)}

    def close_position(self, position_id: str, reason: str) -> dict[str, Any]:
        try:
            success, result = self.connector.close_position(
                ticket=position_id,
                comment=str(reason or "close_position")[:31],
            )
            return {
                "ok": bool(success),
                "status": "closed" if success else "rejected",
                "reason": reason,
                "position_id": str(position_id),
                "result": self._serialize(result),
            }
        except Exception as exc:
            return {"ok": False, "status": "failed", "reason": "close_position_failed", "position_id": str(position_id), "error": str(exc)}

    def get_open_positions(self) -> list[Any]:
        return list(self.connector.get_bot_positions())

    def get_pending_orders(self) -> list[Any]:
        return list(self.connector.get_pending_orders())

    def get_account_state(self) -> dict[str, Any]:
        account = self.connector.get_account_info()
        return AccountState.from_mt5(account).to_dict() if account is not None else {}

    def poll_events(self) -> list[dict[str, Any]]:
        # MT5 state is polled by callers; order/deal history enrichment remains in the connector.
        return []

    def _submit_limit_order(self, request: ExecutionRequest) -> dict[str, Any]:
        symbol_spec = self.connector.get_symbol_spec()
        direction = str(request.side).upper()
        normalized_volume = self.connector.normalize_volume(float(request.volume), symbol_spec)
        if normalized_volume <= 0:
            return {"ok": False, "status": "rejected", "reason": "invalid_volume", "request": self._request_payload(request)}
        stops_valid, stop_prices, stop_reason = self.connector._validate_and_adjust_stops(
            direction=direction,
            entry_price=float(request.entry_price),
            sl=float(request.stop_loss),
            tp=float(request.take_profit),
            symbol_spec=symbol_spec,
        )
        if not stops_valid or stop_prices is None:
            return {"ok": False, "status": "rejected", "reason": stop_reason or "invalid_stops", "request": self._request_payload(request)}

        mt5_order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction in {"LONG", "BUY"} else mt5.ORDER_TYPE_SELL_LIMIT
        pending_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": request.symbol,
            "volume": float(normalized_volume),
            "type": mt5_order_type,
            "price": normalize_price(float(stop_prices["entry"]), int(symbol_spec["digits"])),
            "sl": float(stop_prices["sl"]),
            "tp": float(stop_prices["tp"]),
            "deviation": int(self.connector.config["mt5"]["deviation"]),
            "magic": int(self.connector.config["mt5"]["magic_number"]),
            "comment": self._comment(request),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.connector.choose_filling_mode(),
        }
        expires_at = to_utc(request.expires_at)
        if expires_at is not None:
            pending_request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            pending_request["expiration"] = int(expires_at.timestamp())

        dry_run = bool(request.metadata.get("dry_run", False))
        pipeline: list[dict[str, Any]] = []
        if bool(self.connector.mt5_config.get("order_check_enabled", True)):
            check_stage = self.connector.order_check_stage(pending_request)
            pipeline.append(check_stage)
            if not check_stage.get("ok"):
                return {
                    "ok": False,
                    "executed": False,
                    "status": "rejected",
                    "reason": str(check_stage.get("reason_code") or "order_check_failed"),
                    "execution_blocked_reason": str(check_stage.get("reason_code") or "order_check_failed"),
                    "request": self._serialize(pending_request),
                    "pipeline_stages": pipeline,
                    "failure_class": "broker_rejection",
                }
        send_stage = self.connector.order_send_stage(pending_request, dry_run=dry_run)
        pipeline.append(send_stage)
        ok = bool(send_stage.get("ok"))
        payload = send_stage.get("payload", {}) or {}
        result = payload.get("result")
        order_id = str((result or {}).get("order") or "") if isinstance(result, dict) else ""
        return {
            "ok": ok,
            "executed": False,
            "status": "validated" if dry_run and ok else ("pending" if ok else "rejected"),
            "reason": "dry_run_validated" if dry_run and ok else (ReasonCode.ORDER_PENDING.value if ok else str(send_stage.get("reason_code") or "order_send_failed")),
            "execution_blocked_reason": None if ok else str(send_stage.get("reason_code") or "order_send_failed"),
            "order_id": order_id,
            "result": result,
            "entry": float(stop_prices["entry"]),
            "sl": float(stop_prices["sl"]),
            "tp": float(stop_prices["tp"]),
            "volume": float(normalized_volume),
            "request": self._serialize(pending_request),
            "pipeline_stages": pipeline,
            "failure_class": None if ok else "broker_rejection",
        }

    @staticmethod
    def _serialize(value: Any) -> Any:
        if hasattr(value, "_asdict"):
            return {str(key): MT5Broker._serialize(item) for key, item in value._asdict().items()}
        if isinstance(value, dict):
            return {str(key): MT5Broker._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [MT5Broker._serialize(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _is_done(result: Any) -> bool:
        retcode = int(getattr(result, "retcode", -1)) if result is not None else -1
        return retcode in {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        }

    @staticmethod
    def _request_payload(request: ExecutionRequest) -> dict[str, Any]:
        return {
            "strategy_name": request.strategy_name,
            "symbol": request.symbol,
            "side": request.side,
            "order_type": request.order_type,
            "entry_price": request.entry_price,
            "volume": request.volume,
            "stop_loss": request.stop_loss,
            "take_profit": request.take_profit,
            "expires_at": request.expires_at,
            "metadata": dict(request.metadata),
        }

    @staticmethod
    def _comment(request: ExecutionRequest) -> str:
        comment = str(request.metadata.get("comment") or request.metadata.get("order_comment") or request.strategy_name or "XAU_BOT")
        return comment[:31]
