"""Incremental market features recorded at each LebPrime lifecycle moment.

Values are recorded only; nothing here filters or changes trades. Every indicator updates on
closed candles in order, so a snapshot never contains information from later candles.

Recorded per snapshot:
  RSI(14), MACD(12, 26, 9) line / signal / histogram (+ histogram rising), ADX(14) with +DI / -DI,
  ATR(14) and its percentile over the last ~100 trading days of candles, EMA 50 / EMA 200 and the
  close's distance to them in ATR, EMA 50 slope over 5 candles, H1 and H4 trend (EMA 50 of the
  hourly / 4-hour closes, slope sign), Bollinger band width (20, 2) in ATR, tick volume vs its
  20-candle average, spread, session, hour (UTC) and weekday.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from datetime import datetime
from typing import Any

from services.indicator_lab.models import Candle


class _Wilder:
    """Pine ta.rma: seeded with the SMA of the first ``length`` values."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) >= self.length:
                self.value = sum(self._seed) / self.length
                self._seed = []
        else:
            self.value = (self.value * (self.length - 1) + x) / self.length
        return self.value


class _Ema:
    """Pine ta.ema: seeded with the SMA of the first ``length`` values."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.alpha = 2.0 / (length + 1.0)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) >= self.length:
                self.value = sum(self._seed) / self.length
                self._seed = []
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


class _HigherTimeframeTrend:
    """EMA 50 of completed higher-timeframe closes, built from the base candles."""

    def __init__(self, minutes: int, length: int = 50) -> None:
        self.minutes = minutes
        self.ema = _Ema(length)
        self.bucket: int | None = None
        self.last_close: float | None = None
        self.previous_ema: float | None = None

    def update(self, ts: datetime, close: float) -> None:
        bucket = int(ts.timestamp() // (self.minutes * 60))
        if self.bucket is not None and bucket != self.bucket and self.last_close is not None:
            self.previous_ema = self.ema.value
            self.ema.update(self.last_close)  # the previous bucket just completed
        self.bucket = bucket
        self.last_close = close

    def trend(self) -> str | None:
        if self.ema.value is None or self.previous_ema is None:
            return None
        return "UP" if self.ema.value > self.previous_ema else ("DOWN" if self.ema.value < self.previous_ema else "FLAT")


def session_name(ts: datetime) -> str:
    hour = ts.hour
    if 7 <= hour < 12:
        return "LONDON"
    if 12 <= hour < 16:
        return "LONDON_NY_OVERLAP"
    if 16 <= hour < 21:
        return "NEW_YORK"
    return "ASIA"


class FeatureTracker:
    """Feeds on every closed candle; ``snapshot()`` returns the values as of the latest candle."""

    def __init__(self, atr_percentile_window: int = 2400) -> None:
        self._prev: Candle | None = None
        self._rsi_gain = _Wilder(14)
        self._rsi_loss = _Wilder(14)
        self._ema12 = _Ema(12)
        self._ema26 = _Ema(26)
        self._macd_signal = _Ema(9)
        self._prev_hist: float | None = None
        self._hist: float | None = None
        self._tr = _Wilder(14)
        self._plus_dm = _Wilder(14)
        self._minus_dm = _Wilder(14)
        self._adx = _Wilder(14)
        self._ema50 = _Ema(50)
        self._ema200 = _Ema(200)
        self._ema50_hist: deque[float] = deque(maxlen=6)
        self._closes20: deque[float] = deque(maxlen=20)
        self._volumes20: deque[float] = deque(maxlen=20)
        self._atr_window: deque[float] = deque(maxlen=atr_percentile_window)
        self._atr_sorted: list[float] = []
        self._h1 = _HigherTimeframeTrend(60)
        self._h4 = _HigherTimeframeTrend(240)
        self._d1 = _HigherTimeframeTrend(1440, 20)
        self._m5 = _HigherTimeframeTrend(5)
        self._m15 = _HigherTimeframeTrend(15)
        self._last: Candle | None = None
        self._values: dict[str, Any] = {}

    def update(self, bar: Candle) -> None:
        prev = self._prev
        close = bar.close
        # RSI
        if prev is not None:
            change = close - prev.close
            gain = self._rsi_gain.update(max(change, 0.0))
            loss = self._rsi_loss.update(max(-change, 0.0))
        else:
            gain = loss = None
        rsi = None
        if gain is not None and loss is not None:
            rsi = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
        # MACD
        fast, slow = self._ema12.update(close), self._ema26.update(close)
        macd = signal = hist = None
        if fast is not None and slow is not None:
            macd = fast - slow
            signal = self._macd_signal.update(macd)
            if signal is not None:
                self._prev_hist = self._hist
                hist = macd - signal
                self._hist = hist
        # ATR / DMI / ADX (Wilder, as ta.dmi)
        tr = bar.high - bar.low if prev is None else max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        atr = self._tr.update(tr)
        plus_di = minus_di = adx = None
        if prev is not None:
            up, down = bar.high - prev.high, prev.low - bar.low
            pdm = up if up > down and up > 0 else 0.0
            mdm = down if down > up and down > 0 else 0.0
            spdm, smdm = self._plus_dm.update(pdm), self._minus_dm.update(mdm)
            if atr and spdm is not None and smdm is not None:
                plus_di, minus_di = 100.0 * spdm / atr, 100.0 * smdm / atr
                total = plus_di + minus_di
                adx = self._adx.update(100.0 * abs(plus_di - minus_di) / total if total else 0.0)
        # ATR percentile
        atr_pct = None
        if atr is not None:
            if len(self._atr_window) == self._atr_window.maxlen:
                old = self._atr_window[0]
                del self._atr_sorted[bisect_left(self._atr_sorted, old)]
            self._atr_window.append(atr)
            insort(self._atr_sorted, atr)
            atr_pct = 100.0 * bisect_left(self._atr_sorted, atr) / max(len(self._atr_sorted) - 1, 1)
        # Trend
        ema50, ema200 = self._ema50.update(close), self._ema200.update(close)
        if ema50 is not None:
            self._ema50_hist.append(ema50)
        ts = _parse_ts(bar.timestamp)
        if ts is not None:
            self._h1.update(ts, close)
            self._h4.update(ts, close)
            self._d1.update(ts, close)
            self._m5.update(ts, close)
            self._m15.update(ts, close)
        # Bollinger width, volume ratio
        self._closes20.append(close)
        bb_width = None
        if len(self._closes20) == 20:
            mean = sum(self._closes20) / 20.0
            std = (sum((c - mean) ** 2 for c in self._closes20) / 20.0) ** 0.5
            bb_width = 4.0 * std
        vol_ratio = None
        if len(self._volumes20) == 20:
            avg = sum(self._volumes20) / 20.0
            vol_ratio = bar.tick_volume / avg if avg else None
        self._volumes20.append(bar.tick_volume)

        def per_atr(x: float | None) -> float | None:
            return round(x / atr, 4) if x is not None and atr else None

        self._values = {
            "rsi14": _r(rsi),
            "macd": _r(macd, 6),
            "macd_signal": _r(signal, 6),
            "macd_hist": _r(hist, 6),
            "macd_hist_rising": None if hist is None or self._prev_hist is None else hist > self._prev_hist,
            "adx14": _r(adx),
            "plus_di14": _r(plus_di),
            "minus_di14": _r(minus_di),
            "atr14": _r(atr, 6),
            "atr14_percentile": _r(atr_pct, 1),
            "ema50_dist_atr": per_atr(None if ema50 is None else close - ema50),
            "ema200_dist_atr": per_atr(None if ema200 is None else close - ema200),
            "ema50_slope_atr": per_atr(self._ema50_hist[-1] - self._ema50_hist[0]) if len(self._ema50_hist) == 6 else None,
            "h1_trend": self._h1.trend(),
            "h4_trend": self._h4.trend(),
            "d1_trend": self._d1.trend(),
            "m5_trend": self._m5.trend(),
            "m15_trend": self._m15.trend(),
            "bb_width_atr": per_atr(bb_width),
            "tick_volume_ratio": _r(vol_ratio, 3),
            "spread": bar.spread,
            "session": session_name(ts) if ts else None,
            "hour_utc": ts.hour if ts else None,
            "weekday": ts.strftime("%a") if ts else None,
        }
        self._prev = bar
        self._last = bar

    def snapshot(self) -> dict[str, Any]:
        if self._last is None:
            return {}
        return {"time": self._last.timestamp, "close": self._last.close, **self._values}


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


FEATURE_KEYS = [
    "rsi14", "macd", "macd_signal", "macd_hist", "macd_hist_rising", "adx14", "plus_di14", "minus_di14",
    "atr14", "atr14_percentile", "ema50_dist_atr", "ema200_dist_atr", "ema50_slope_atr", "h1_trend", "h4_trend",
    "bb_width_atr", "tick_volume_ratio", "spread", "session", "hour_utc", "weekday",
]
# Lifecycle moments: candidate found, LIMIT CREATED, Entry 1 fill, Entry 2 fill, TP1, exit (final result).
MOMENTS = ["candidate", "limit", "entry1", "entry2", "tp1", "exit"]
EVENT_MOMENTS = {"LIMIT_CREATED": "limit", "ENTRY1_ACTIVATED": "entry1", "ENTRY2_ACTIVATED": "entry2", "TP1_HIT": "tp1",
                 "TP2_HIT": "exit", "TP1_FINAL": "exit", "SL_HIT": "exit", "CANCELED": "exit"}


def feature_columns() -> list[str]:
    return [f"{moment}_{key}" for moment in MOMENTS for key in ("time", "close", *FEATURE_KEYS)]


def flatten_snapshot(moment: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {f"{moment}_{key}": snapshot.get(key) for key in ("time", "close", *FEATURE_KEYS)}
