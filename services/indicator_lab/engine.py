"""Deterministic candle-by-candle engine contract and common mechanics."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from services.indicator_lab.models import Candle, ZoneEvent, ZoneRecord
from services.indicator_lab.parameter_registry import default_parameters, parameter_schema, validate_parameters


class IndicatorEngine:
    """Base contract for Pine-derived indicator engines."""

    indicator = "BASE"

    def __init__(self, parameters: dict[str, Any] | None = None, *, symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
        self.symbol = str(symbol or "XAUUSD").upper()
        self.timeframe = str(timeframe or "M1").upper()
        self.parameters = validate_parameters(self.indicator, parameters or {})
        self._bars: list[Candle] = []
        self._zones: list[ZoneRecord] = []
        self._events: list[ZoneEvent] = []

    @classmethod
    def default_parameters(cls) -> dict[str, Any]:
        return default_parameters(cls.indicator)

    @classmethod
    def parameter_schema(cls) -> list[dict[str, Any]]:
        return parameter_schema(cls.indicator)

    @classmethod
    def validate_parameters(cls, parameters: dict[str, Any] | None) -> dict[str, Any]:
        return validate_parameters(cls.indicator, parameters)

    def process_bar(self, bar: Candle) -> None:
        raise NotImplementedError

    def finalize(self) -> dict[str, Any]:
        return {"zones": self.zones(), "events": self.events(), "metrics": self.metrics()}

    def zones(self) -> list[ZoneRecord]:
        return list(self._zones)

    def events(self) -> list[ZoneEvent]:
        return list(self._events)

    def metrics(self) -> dict[str, Any]:
        events = Counter(event.event_type for event in self._events)
        closed = [zone for zone in self._zones if zone.terminal_state in {"TP_HIT", "TP1_FINAL", "TP2_HIT", "SL_HIT", "BE_EXIT"}]
        tp = sum(1 for zone in closed if zone.terminal_state in {"TP_HIT", "TP1_FINAL", "TP2_HIT"})
        sl = sum(1 for zone in closed if zone.terminal_state == "SL_HIT")
        be = sum(1 for zone in closed if zone.terminal_state == "BE_EXIT")
        r_values = [float(zone.result_r or 0.0) for zone in closed]
        gross_won = sum(value for value in r_values if value > 0)
        gross_lost = abs(sum(value for value in r_values if value < 0))
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for value in r_values:
            equity += value
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return {
            "zones_created": events.get("ZONE_CREATED", 0),
            "zones_rejected": events.get("ZONE_REJECTED", 0),
            "flipped_zones": events.get("FLIPPED", 0),
            "limit_created": events.get("LIMIT_CREATED", 0),
            "limits_created": events.get("LIMIT_CREATED", 0),
            "activated": events.get("ACTIVATED", 0) + events.get("ENTRY1_ACTIVATED", 0),
            "entry1_activated": events.get("ACTIVATED", 0) + events.get("ENTRY1_ACTIVATED", 0),
            "entry2_activated": events.get("ENTRY2_ACTIVATED", 0),
            "tp1_hit": events.get("TP1_HIT", 0),
            "tp2_hit": events.get("TP2_HIT", 0),
            "tp1_final": events.get("TP1_FINAL", 0),
            "tp_hit": tp,
            "sl_hit": sl,
            "be_exit": be,
            "canceled": events.get("CANCELED", 0),
            "invalidated": events.get("INVALIDATED", 0),
            "closed_trades": len(closed),
            "pending_limits": sum(1 for zone in self._zones if zone.limit_created_bar_index is not None and zone.activated_bar_index is None and not zone.terminal_state),
            "active_trades": sum(1 for zone in self._zones if zone.activated_bar_index is not None and not zone.terminal_state),
            "pending": sum(1 for zone in self._zones if zone.limit_created_bar_index is not None and zone.activated_bar_index is None and not zone.terminal_state),
            "active": sum(1 for zone in self._zones if zone.activated_bar_index is not None and not zone.terminal_state),
            "resolved_trades": len(closed),
            "win_rate": (tp / len(closed) * 100.0) if closed else 0.0,
            "net_r": sum(r_values),
            "gross_r_won": gross_won,
            "gross_r_lost": gross_lost,
            "profit_factor": (gross_won / gross_lost) if gross_lost > 0 else (999.0 if gross_won > 0 else 0.0),
            "average_r": (sum(r_values) / len(r_values)) if r_values else 0.0,
            "max_drawdown": max_dd,
        }

    def run(self, frame: pd.DataFrame) -> dict[str, Any]:
        for idx, row in frame.reset_index(drop=True).iterrows():
            self.process_bar(Candle.from_row(int(idx), row.to_dict()))
        return self.finalize()

    def _emit(self, zone: ZoneRecord, bar: Candle, event_type: str, *, price: float | None = None, reason: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self._events.append(
            ZoneEvent(
                zone.engine_zone_id,
                bar.index,
                bar.timestamp,
                event_type,
                price=price,
                zone_top=zone.zone_top,
                zone_bottom=zone.zone_bottom,
                entry=zone.entry_price,
                sl=zone.sl_price,
                tp=zone.tp_price,
                reason=reason,
                metadata=metadata or {},
            )
        )

    def _emit_orphan(self, zone_id: str, bar: Candle, event_type: str, *, price: float | None = None, reason: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self._events.append(ZoneEvent(zone_id, bar.index, bar.timestamp, event_type, price=price, reason=reason, metadata=metadata or {}))

    def _sma_body(self, length: int) -> float | None:
        if len(self._bars) < length:
            return None
        return sum(abs(bar.close - bar.open) for bar in self._bars[-length:]) / float(length)

    def _atr(self, length: int) -> float | None:
        if len(self._bars) < length + 1:
            return None
        trs: list[float] = []
        for i in range(len(self._bars) - length, len(self._bars)):
            bar = self._bars[i]
            prev = self._bars[i - 1]
            trs.append(max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close)))
        return sum(trs) / float(length)

    def _symbol_buffer(self, gold: float, us30: float, nas: float, wti: float) -> float:
        ticker = self.symbol.upper()
        if "US30" in ticker or "DJ" in ticker:
            return float(us30)
        if "NAS" in ticker or "USTEC" in ticker or "US100" in ticker:
            return float(nas)
        if "WTI" in ticker or "USOIL" in ticker:
            return float(wti)
        return float(gold)

    @staticmethod
    def pine_hash(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()
