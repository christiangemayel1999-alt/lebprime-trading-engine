from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import pytz

from trading_bot.core.reasons import JournalEventType, ManagementAction, ReasonCode
from trading_bot.execution.pending_policy import PendingOrderPolicy
from utils import to_utc, utc_now


class LivePositionLifecycleManager:
    """Own live pending-order sync and live position action application.

    Runtime code should decide when to evaluate positions; this component owns
    the lifecycle effects once the risk manager has emitted normalized actions.
    """

    def __init__(
        self,
        *,
        config: dict[str, Any],
        state: dict[str, Any],
        connector: Any,
        execution_service: Any,
        risk_manager: Any,
        trade_journal: Any,
        logger: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.connector = connector
        self.execution_service = execution_service
        self.risk_manager = risk_manager
        self.trade_journal = trade_journal
        self.logger = logger
        self.pending_policy = PendingOrderPolicy(config)

    def refresh(
        self,
        *,
        config: dict[str, Any],
        state: dict[str, Any],
        connector: Any,
        execution_service: Any,
        risk_manager: Any,
        trade_journal: Any,
        logger: Any,
    ) -> None:
        """Refresh mutable runtime dependencies after config reloads."""
        self.config = config
        self.state = state
        self.connector = connector
        self.execution_service = execution_service
        self.risk_manager = risk_manager
        self.trade_journal = trade_journal
        self.logger = logger
        self.pending_policy = PendingOrderPolicy(config)

    def apply_management_actions(
        self,
        position: dict[str, Any],
        actions: list[dict[str, Any]],
        finalize_live_trade: Callable[[dict[str, Any], str], None],
    ) -> bool:
        """Apply normalized management actions through the execution service.

        Returns True when a full close was requested and finalized.
        """
        for action in actions:
            self.logger.structured("trade_management_action", {"ticket": position["ticket"], **action})
            action_name = str(action.get("action") or "")
            reason_code = str(action.get("reason_code") or action_name or ReasonCode.CLOSE_FULL.value)

            if action_name == ManagementAction.PARTIAL_CLOSE.value:
                response = self.execution_service.partial_close(
                    str(position["ticket"]),
                    float(action["volume"]),
                    reason_code,
                )
                if bool(response.get("ok")):
                    position["remaining_volume"] = max(
                        0.0,
                        float(position["remaining_volume"]) - float(action["volume"]),
                    )
                    self.trade_journal.record_trade_managed(
                        {
                            **position,
                            "event_type": JournalEventType.TRADE_MANAGED.value,
                            "status": "OPENED",
                            "note": reason_code,
                            "comment": action.get("human_reason"),
                            "volume": position["remaining_volume"],
                            "metadata": {"action": action, "execution_response": response},
                        }
                    )
                continue

            if action_name == ManagementAction.MOVE_STOP.value:
                response = self.execution_service.modify_position(
                    str(position["ticket"]),
                    sl=float(action["sl"]),
                    reason=reason_code,
                )
                if bool(response.get("ok")):
                    position["sl"] = float(action["sl"])
                    normalized_reason = self._normalize_stop_reason(reason_code)
                    self.trade_journal.record_trade_managed(
                        {
                            **position,
                            "event_type": JournalEventType.TRADE_MANAGED.value,
                            "status": "OPENED",
                            "note": normalized_reason,
                            "comment": action.get("human_reason"),
                            "stop_loss": position["sl"],
                            "metadata": {"action": action, "execution_response": response},
                        }
                    )
                continue

            if action_name == ManagementAction.CLOSE_FULL.value:
                response = self.execution_service.close_position(str(position["ticket"]), reason_code)
                if bool(response.get("ok")):
                    finalize_live_trade(position, reason_code)
                    return True
        return False

    def sync_pending_order(self, market_context: dict[str, Any], mode_ctx: dict[str, Any]) -> None:
        """Promote filled pending orders or clear expired/cancelled ones."""
        pending = self.state.get("pending_order")
        if not pending:
            return

        now = utc_now()
        order_id = str(pending.get("order_id") or "")
        symbol = str(pending.get("symbol") or self.config["mt5"]["symbol"])
        try:
            open_positions = self.execution_service.get_open_positions()
            pending_orders = self.execution_service.get_pending_orders()
        except Exception as exc:
            self.logger.structured(
                "pending_order_sync_failed",
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "reason_code": ReasonCode.SYNC_FAILURE.value,
                    "error": str(exc),
                },
                level="WARNING",
            )
            return

        match_result = self._match_pending_fill(pending, open_positions, now)
        if match_result.get("status") == "matched" and match_result.get("position") is not None:
            self._promote_pending_fill(
                pending,
                match_result["position"],
                market_context,
                mode_ctx,
                now,
                order_id,
                symbol,
                match_result,
            )
            return
        if match_result.get("status") == "ambiguous":
            pending["match_status"] = "AMBIGUOUS"
            pending["last_match_attempt_at"] = now.isoformat()
            pending["match_candidates"] = list(match_result.get("candidates") or [])
            self.logger.structured(
                "pending_order_fill_ambiguous",
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "reason_code": "ambiguous_pending_fill_match",
                    "candidates": match_result.get("candidates") or [],
                },
                level="WARNING",
            )
            if pending.get("last_match_status") != "AMBIGUOUS":
                self.trade_journal.record_trade_event(
                    {
                        "timestamp": now.isoformat(),
                        "trade_id": order_id,
                        "mt5_ticket": order_id,
                        "position_id": order_id,
                        "order_id": order_id,
                        "event_type": "ORDER_FILL_AMBIGUOUS",
                        "status": "AMBIGUOUS",
                        "symbol": symbol,
                        "side": pending.get("direction"),
                        "setup": pending.get("setup_fingerprint"),
                        "setup_family": pending.get("setup_family"),
                        "entry_mode": pending.get("entry_mode"),
                        "execution_reason": "ambiguous_pending_fill_match",
                        "metadata": {"pending_order": pending, "candidates": match_result.get("candidates") or []},
                    }
                )
            pending["last_match_status"] = "AMBIGUOUS"
            return

        order_still_pending = any(
            str(getattr(order, "ticket", getattr(order, "order", ""))) == order_id
            for order in pending_orders
        )
        expires_at = to_utc(pending.get("expires_at"))
        latest_bar = None
        if isinstance(market_context, dict):
            latest_bar = (market_context.get("latest_bar") if isinstance(market_context.get("latest_bar"), dict) else None)
        pending_decision = self.pending_policy.assess(pending, market_context, latest_bar)
        if pending_decision.action == "cancel":
            response = (
                self.execution_service.cancel_order(order_id, str(pending_decision.reason_code))
                if order_id
                else {"ok": False, "reason": "missing_order_id"}
            )
            self._clear_pending_order(
                pending,
                order_id,
                symbol,
                JournalEventType.ORDER_CANCELLED.value,
                str(pending_decision.reason_code),
                {"cancel_response": response, **dict(pending_decision.metadata or {})},
            )
            return
        if expires_at and now >= expires_at:
            response = (
                self.execution_service.cancel_order(order_id, ReasonCode.ORDER_EXPIRED.value)
                if order_id
                else {"ok": False, "reason": "missing_order_id"}
            )
            self._clear_pending_order(
                pending,
                order_id,
                symbol,
                JournalEventType.ORDER_EXPIRED.value,
                ReasonCode.ORDER_EXPIRED.value,
                {"cancel_response": response},
            )
            return

        if order_id and not order_still_pending:
            self._clear_pending_order(
                pending,
                order_id,
                symbol,
                JournalEventType.ORDER_CANCELLED.value,
                ReasonCode.ORDER_MISSING.value,
                {},
            )

    def _promote_pending_fill(
        self,
        pending: dict[str, Any],
        matching_position: Any,
        market_context: dict[str, Any],
        mode_ctx: dict[str, Any],
        now: datetime,
        order_id: str,
        symbol: str,
        match_result: dict[str, Any] | None = None,
    ) -> None:
        ticket = str(getattr(matching_position, "ticket", order_id or int(now.timestamp())))
        entry_price = float(
            getattr(matching_position, "price_open", pending.get("entry_price", 0.0))
            or pending.get("entry_price", 0.0)
        )
        trade_plan = self.risk_manager.rebase_trade_levels(
            dict(pending.get("trade_plan") or {}),
            entry_price,
            self.connector.get_symbol_spec(),
        )
        candidate = dict(pending.get("candidate") or {})
        entry_assessment = dict(pending.get("entry_assessment") or {})
        opened_at = datetime.fromtimestamp(
            int(getattr(matching_position, "time", now.timestamp())),
            tz=pytz.UTC,
        )
        expected_entry = float(pending.get("entry_price", entry_price))
        slippage = entry_price - expected_entry if str(pending.get("direction")) == "LONG" else expected_entry - entry_price
        position_state = self.risk_manager.build_position_state(
            trade_plan,
            ticket,
            symbol,
            float(getattr(matching_position, "volume", pending.get("volume", 0.0)) or pending.get("volume", 0.0)),
            candidate,
            entry_assessment,
            opened_at,
            mode_ctx["mode"],
            float((pending.get("market_context") or market_context).get("spread_points") or 0.0),
            float(pending.get("requested_volume", pending.get("volume", 0.0)) or 0.0),
            slippage,
        )
        match_payload = dict(match_result or {})
        position_state["order_id"] = order_id
        position_state["fill_match_confidence"] = float(match_payload.get("confidence", 0.0) or 0.0)
        position_state["fill_match_fields"] = list(match_payload.get("matched_fields") or [])
        position_state["fill_match_candidates"] = list(match_payload.get("candidates") or [])
        self.state["open_position"] = position_state
        self.state.pop("pending_order", None)
        self.trade_journal.record_trade_open(
            {
                "timestamp": now.isoformat(),
                "trade_id": ticket,
                "mt5_ticket": ticket,
                "position_id": ticket,
                "order_id": order_id,
                "event_type": JournalEventType.TRADE_OPENED.value,
                "status": "OPENED",
                "symbol": symbol,
                "side": position_state["direction"],
                "setup": position_state["setup_fingerprint"],
                "setup_family": position_state["setup_family"],
                "regime": position_state["regime_at_entry"],
                "session": position_state["session_at_entry"],
                "entry_mode": position_state["entry_mode"],
                "execution_reason": ReasonCode.ORDER_FILLED.value,
                "entry_price": position_state["entry_price"],
                "stop_loss": position_state["sl"],
                "take_profit": position_state["tp2"],
                "volume": position_state["volume"],
                "magic_number": self.config["mt5"]["magic_number"],
                "matched_volume": float(getattr(matching_position, "volume", pending.get("volume", 0.0)) or pending.get("volume", 0.0)),
                "reconciliation_confidence": position_state["fill_match_confidence"],
                "resolution_metadata": match_payload,
                "metadata": position_state,
            }
        )
        self.logger.structured(
            ReasonCode.ORDER_FILLED.value,
            {
                "order_id": order_id,
                "ticket": ticket,
                "symbol": symbol,
                "entry_price": entry_price,
                "setup_family": pending.get("setup_family"),
                "reason_code": ReasonCode.ORDER_FILLED.value,
                "match_confidence": position_state["fill_match_confidence"],
                "match_fields": position_state["fill_match_fields"],
            },
        )

    def _clear_pending_order(
        self,
        pending: dict[str, Any],
        order_id: str,
        symbol: str,
        event_type: str,
        reason_code: str,
        extra_metadata: dict[str, Any],
    ) -> None:
        self.state.pop("pending_order", None)
        lifecycle_event = "order_expired_unfilled" if event_type == JournalEventType.ORDER_EXPIRED.value else (
            "order_missed_state_block" if reason_code in {ReasonCode.ORDER_MISSING.value, ReasonCode.ORDER_CANCELLED.value} else "order_missed_state_block"
        )
        self.trade_journal.record_signal_event(
            {
                "timestamp": utc_now().isoformat(),
                "event_type": lifecycle_event,
                "trade_id": order_id,
                "mt5_ticket": order_id,
                "position_id": order_id,
                "symbol": symbol,
                "side": pending.get("direction"),
                "setup_fingerprint": pending.get("setup_fingerprint"),
                "setup_family": pending.get("setup_family"),
                "entry_mode": pending.get("entry_mode"),
                "executed": False,
                "reason_code": reason_code,
                "reason": reason_code,
                "metadata": {"pending_order": pending, **extra_metadata},
            }
        )
        self.trade_journal.record_trade_event(
            {
                "timestamp": utc_now().isoformat(),
                "trade_id": order_id,
                "mt5_ticket": order_id,
                "position_id": order_id,
                "order_id": order_id,
                "event_type": event_type,
                "status": event_type.replace("ORDER_", ""),
                "symbol": symbol,
                "side": pending.get("direction"),
                "setup": pending.get("setup_fingerprint"),
                "setup_family": pending.get("setup_family"),
                "entry_mode": pending.get("entry_mode"),
                "execution_reason": reason_code,
                "metadata": {"pending_order": pending, **extra_metadata},
            }
        )
        self.logger.structured(reason_code, {"order_id": order_id, "symbol": symbol, **extra_metadata})

    @staticmethod
    def _normalize_stop_reason(reason_code: str) -> str:
        lowered = str(reason_code or "").lower()
        if "breakeven" in lowered or "break_even" in lowered:
            return ReasonCode.BREAKEVEN_ACTIVATED.value
        if "trailing" in lowered:
            return ReasonCode.TRAILING_ACTIVATED.value
        return reason_code or ReasonCode.STOP_MODIFIED.value

    def _match_pending_fill(self, pending: dict[str, Any], open_positions: list[Any], now: datetime) -> dict[str, Any]:
        """Return the strongest pending-order -> live-position match, or flag ambiguity."""
        order_id = str(pending.get("order_id") or "").strip()
        expected_symbol = str(pending.get("symbol") or self.config["mt5"]["symbol"]).strip().upper()
        expected_side = str(pending.get("direction") or "").strip().upper()
        expected_volume = float(pending.get("volume", 0.0) or 0.0)
        expected_entry = float(pending.get("entry_price", 0.0) or 0.0)
        submitted_at = to_utc(pending.get("submitted_at"))
        tolerance_points = float(self.config.get("execution", {}).get("pending_fill_entry_tolerance_points", 25.0) or 25.0)
        point_size = float(self.connector.get_symbol_spec()["point"])
        price_tolerance = max(point_size, point_size * tolerance_points)
        expected_magic = int(self.config["mt5"]["magic_number"])

        candidates: list[dict[str, Any]] = []
        for position in open_positions:
            symbol = str(getattr(position, "symbol", "") or "").strip().upper()
            if symbol != expected_symbol:
                continue
            score = 0.0
            matched_fields: list[str] = ["symbol"]
            raw_ids = {
                str(getattr(position, "ticket", "") or "").strip(),
                str(getattr(position, "identifier", "") or "").strip(),
                str(getattr(position, "order", "") or "").strip(),
                str(getattr(position, "position_id", "") or "").strip(),
            }
            if order_id and order_id in raw_ids:
                score += 100.0
                matched_fields.append("order_or_identifier")
            position_magic = int(getattr(position, "magic", 0) or 0)
            if position_magic and position_magic == expected_magic:
                score += 20.0
                matched_fields.append("magic")
            position_side = self._position_side(position)
            if position_side and position_side == expected_side:
                score += 20.0
                matched_fields.append("side")
            actual_volume = float(getattr(position, "volume", 0.0) or 0.0)
            if expected_volume > 0 and actual_volume > 0:
                if abs(actual_volume - expected_volume) <= max(0.01, expected_volume * 0.05):
                    score += 18.0
                    matched_fields.append("volume_exactish")
                elif abs(actual_volume - expected_volume) <= max(0.05, expected_volume * 0.35):
                    score += 8.0
                    matched_fields.append("volume_close")
            actual_entry = float(getattr(position, "price_open", 0.0) or 0.0)
            if expected_entry > 0 and actual_entry > 0 and abs(actual_entry - expected_entry) <= price_tolerance:
                score += 14.0
                matched_fields.append("entry_price")
            position_opened_at = to_utc(getattr(position, "time", None))
            if submitted_at is not None and position_opened_at is not None:
                delta_seconds = abs((position_opened_at - submitted_at).total_seconds())
                if delta_seconds <= 120:
                    score += 14.0
                    matched_fields.append("time_near")
                elif delta_seconds <= 900:
                    score += 6.0
                    matched_fields.append("time_window")
            candidates.append(
                {
                    "position": position,
                    "confidence": score,
                    "matched_fields": matched_fields,
                    "ticket": str(getattr(position, "ticket", "") or ""),
                    "volume": actual_volume,
                    "entry_price": actual_entry,
                }
            )

        if not candidates:
            return {"status": "none", "confidence": 0.0, "candidates": []}
        candidates.sort(key=lambda item: float(item.get("confidence", 0.0) or 0.0), reverse=True)
        top = candidates[0]
        top_confidence = float(top.get("confidence", 0.0) or 0.0)
        second_confidence = float(candidates[1].get("confidence", 0.0) or 0.0) if len(candidates) > 1 else 0.0
        if top_confidence >= 55.0 and (len(candidates) == 1 or (top_confidence - second_confidence) >= 12.0):
            return {
                "status": "matched",
                "position": top["position"],
                "confidence": top_confidence,
                "matched_fields": top["matched_fields"],
                "candidates": [{key: value for key, value in item.items() if key != "position"} for item in candidates[:3]],
            }
        return {
            "status": "ambiguous",
            "confidence": top_confidence,
            "candidates": [{key: value for key, value in item.items() if key != "position"} for item in candidates[:3]],
        }

    @staticmethod
    def _position_side(position: Any) -> str:
        """Return normalized side for an MT5 position-like object."""
        raw_type = getattr(position, "type", None)
        if raw_type is None:
            return ""
        try:
            return "LONG" if int(raw_type) == 0 else "SHORT"
        except Exception:
            return ""
