"""MetaTrader 5 connectivity, market data, and trade execution utilities."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    MT5_IMPORT_ERROR = exc

    class _UnavailableMT5:
        """Import-safe MT5 placeholder that fails only when MT5 APIs are used."""

        TIMEFRAME_M1 = 1
        TIMEFRAME_M3 = 3
        TIMEFRAME_M5 = 5
        TIMEFRAME_M15 = 15
        TIMEFRAME_M30 = 30
        TIMEFRAME_H1 = 16385
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_SELL_LIMIT = 3
        ORDER_FILLING_FOK = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_TIME_GTC = 0
        ORDER_TIME_SPECIFIED = 2
        TRADE_ACTION_DEAL = 1
        TRADE_ACTION_PENDING = 5
        TRADE_ACTION_SLTP = 6
        TRADE_ACTION_REMOVE = 8
        TRADE_RETCODE_REQUOTE = 10004
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008
        TRADE_RETCODE_TIMEOUT = 10012
        TRADE_RETCODE_INVALID_VOLUME = 10014
        TRADE_RETCODE_INVALID_PRICE = 10015
        TRADE_RETCODE_INVALID_STOPS = 10016
        TRADE_RETCODE_TRADE_DISABLED = 10017
        TRADE_RETCODE_MARKET_CLOSED = 10018
        TRADE_RETCODE_NO_MONEY = 10019
        TRADE_RETCODE_PRICE_CHANGED = 10020
        TRADE_RETCODE_PRICE_OFF = 10021
        TRADE_RETCODE_INVALID_FILL = 10030
        TRADE_RETCODE_CONNECTION = 10031
        POSITION_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        SYMBOL_FILLING_FOK = 1
        SYMBOL_FILLING_IOC = 2
        SYMBOL_TRADE_EXECUTION_INSTANT = 0
        SYMBOL_TRADE_EXECUTION_REQUEST = 1
        SYMBOL_TRADE_EXECUTION_MARKET = 2
        SYMBOL_TRADE_MODE_DISABLED = 0

        def __getattr__(self, name: str) -> Callable[..., Any]:
            def _missing_mt5(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError(
                    "MetaTrader5 package is required for MT5 live connectivity. "
                    "Install MetaTrader5 and run on a supported Windows MT5 environment "
                    f"before calling mt5.{name}."
                ) from MT5_IMPORT_ERROR

            return _missing_mt5

    mt5 = _UnavailableMT5()

from logger import BotLogger
from utils import floor_volume_to_step, infer_pip_size, normalize_price, timeframe_duration, to_utc, utc_now, validate_ohlc_dataframe


def _ensure_utc_datetime(value: Any, label: str) -> datetime:
    """Return a timezone-aware UTC datetime for MT5 historical requests."""
    converted = to_utc(value)
    if converted is None:
        raise ValueError(f"{label} is required")
    return converted


def _range_to_estimated_bar_count(
    timeframe_label: str,
    start_utc: datetime,
    end_utc: datetime,
    min_rows: int,
) -> int:
    """Estimate a count-based fallback size for a requested historical window."""
    candle_seconds = max(timeframe_duration(timeframe_label).total_seconds(), 1.0)
    range_seconds = max((end_utc - start_utc).total_seconds(), candle_seconds)
    estimated = int(range_seconds // candle_seconds) + 2
    buffer = max(50, min(500, estimated // 5))
    return max(int(min_rows), estimated + buffer)


class MT5Connector:
    """Handle MT5 initialization, diagnostics, data retrieval, and trade operations."""

    TIMEFRAME_MAP = {
        "1min": mt5.TIMEFRAME_M1,
        "3min": mt5.TIMEFRAME_M3,
        "5min": mt5.TIMEFRAME_M5,
        "15min": mt5.TIMEFRAME_M15,
        "30min": mt5.TIMEFRAME_M30,
        "60min": mt5.TIMEFRAME_H1,
        "M1": mt5.TIMEFRAME_M1,
        "M3": mt5.TIMEFRAME_M3,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }

    def __init__(self, config: dict[str, Any], logger: BotLogger) -> None:
        self.config = config
        self.logger = logger
        self.mt5_config = config["mt5"]
        self.symbol = str(self.mt5_config["symbol"])
        self.timeout_ms = int(float(self.mt5_config["timeout"]) * 1000)
        self.connected = False
        self.account_info: Any = None
        self.symbol_info: Any = None
        self.terminal_info: Any = None
        self.symbol_spec: dict[str, Any] | None = None
        self.server_time_offset_seconds = 0
        self.execution_verbosity = str(config.get("logging", {}).get("execution_verbosity", "detailed")).lower()
        self.reconnect_cooldown_seconds = max(0.0, float(self.mt5_config.get("reconnect_cooldown_seconds", 8)))
        self.last_reconnect_at: datetime | None = None
        self.market_data_degraded = False
        self.market_data_source = "mt5"

    def _serialize_log_value(self, value: Any) -> Any:
        """Convert MT5 payloads into structured log-friendly values."""
        if hasattr(value, "_asdict"):
            return {str(key): self._serialize_log_value(item) for key, item in value._asdict().items()}
        if isinstance(value, dict):
            return {str(key): self._serialize_log_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._serialize_log_value(item) for item in value]
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _update_server_time_offset(self, broker_epoch_seconds: int | float | None) -> int:
        """Infer and cache the broker-server timestamp offset versus UTC."""
        if broker_epoch_seconds in (None, 0):
            return self.server_time_offset_seconds

        raw_offset = float(broker_epoch_seconds) - utc_now().timestamp()
        rounded_offset = int(round(raw_offset / 3600.0) * 3600)
        if abs(rounded_offset) <= 1800:
            rounded_offset = 0
        if abs(rounded_offset) > 14 * 3600:
            return self.server_time_offset_seconds

        if rounded_offset != self.server_time_offset_seconds:
            self.server_time_offset_seconds = rounded_offset
            hours = self.server_time_offset_seconds / 3600.0
            self.logger.info(f"MT5 server time offset detected: {hours:+.1f}h vs UTC")
        return self.server_time_offset_seconds

    def _normalize_broker_timestamp_series(self, series: pd.Series) -> pd.Series:
        """Normalize broker timestamps to UTC using the detected server offset."""
        timestamps = pd.to_datetime(series, unit="s", utc=True)
        if self.server_time_offset_seconds:
            timestamps = timestamps - pd.to_timedelta(self.server_time_offset_seconds, unit="s")
        return timestamps

    def _call_mt5(
        self,
        label: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call an MT5 API function and wrap exceptions with context."""
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"MT5 call failed for {label}: {exc}") from exc

    def _synthetic_rates_from_tick(self, timeframe_label: str, bars: int, min_rows: int) -> pd.DataFrame:
        """Build a safe placeholder candle series from the latest tick when MT5 bars are unavailable."""
        timeframe = self.TIMEFRAME_MAP[timeframe_label]
        candle_duration = timeframe_duration(timeframe_label)
        required_rows = max(int(min_rows), int(bars) + 10)
        tick, _ = self.get_tick_with_retry()
        if tick is None:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        price = (bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask, 0.0)
        spread = abs(ask - bid) if bid > 0 and ask > 0 else 0.0
        if price <= 0.0:
            price = float(getattr(tick, "last", 0.0) or 0.0)
        if price <= 0.0:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])

        now_utc = utc_now().replace(microsecond=0)
        aligned_seconds = int(candle_duration.total_seconds())
        end_ts = int(now_utc.timestamp())
        end_ts -= end_ts % max(1, aligned_seconds)
        start_ts = end_ts - (required_rows - 1) * aligned_seconds
        rows: list[dict[str, Any]] = []
        for idx in range(required_rows):
            bar_time = datetime.utcfromtimestamp(start_ts + idx * aligned_seconds)
            rows.append(
                {
                    "time": int(bar_time.replace(tzinfo=None).timestamp()),
                    "open": float(price),
                    "high": float(price + spread / 2.0),
                    "low": float(max(price - spread / 2.0, 0.0)),
                    "close": float(price),
                    "tick_volume": int(max(1, getattr(tick, "volume", 1) or 1)),
                    "spread": float(spread),
                    "real_volume": int(max(1, getattr(tick, "volume_real", 1) or 1)),
                }
            )

        frame = pd.DataFrame(rows)
        self.market_data_degraded = True
        self.market_data_source = f"synthetic_{timeframe_label}"
        self.logger.structured(
            "mt5_rates_fallback",
            {
                "symbol": self.symbol,
                "timeframe": timeframe_label,
                "timeframe_minutes": int(candle_duration.total_seconds() // 60),
                "rows": len(frame),
                "required_rows": required_rows,
                "reason": "mt5_history_unavailable",
                "last_error": self._mt5_last_error(),
                "source": self.market_data_source,
            },
            level="WARNING",
        )
        self._update_server_time_offset(frame["time"].iloc[-1])
        frame["time"] = self._normalize_broker_timestamp_series(frame["time"])
        frame = frame.sort_values("time").reset_index(drop=True)
        valid, reason = validate_ohlc_dataframe(frame, min_rows=min_rows)
        if not valid:
            raise RuntimeError(f"{timeframe_label} synthetic fallback validation failed: {reason}")
        return frame

    def execution_path_summary(self) -> dict[str, Any]:
        """Return a small regression-safe summary of the MT5 execution path."""
        return {
            "order_check_style": "positional_request_dict",
            "order_send_style": "direct_positional_request_dict",
            "order_send_wrapper_used": False,
        }

    def _mt5_last_error(self) -> dict[str, Any]:
        """Return the latest MT5 last_error payload as a structured dict."""
        try:
            error_code, error_message = mt5.last_error()
        except Exception:
            error_code, error_message = None, "unavailable"
        return {
            "code": error_code,
            "message": error_message,
        }

    def _validate_order_request(self, request: dict[str, Any]) -> None:
        """Block unsafe orders before they reach MT5."""
        request_dict = dict(request or {})
        symbol_spec = self.symbol_spec or self.get_symbol_spec()
        point = float(symbol_spec.get("point") or 0.01)
        broker_stops_points = float(symbol_spec.get("stops_level") or 0.0)
        pip_size = float(symbol_spec.get("pip_size") or infer_pip_size(int(symbol_spec.get("digits") or 2), point))
        min_sl_price = max(
            broker_stops_points * point,
            float(self.config.get("exit", {}).get("sl_min_pips", 30.0) or 30.0) * pip_size,
        )
        price = float(request_dict.get("price") or 0.0)
        sl = float(request_dict.get("sl") or 0.0)
        tp = float(request_dict.get("tp") or 0.0)
        if price <= 0.0:
            raise ValueError("Order blocked: entry price is missing or invalid")
        if sl <= 0.0:
            raise ValueError("Order blocked: SL is missing or zero")
        if tp <= 0.0:
            raise ValueError("Order blocked: TP is missing or zero")
        dist = abs(price - sl)
        if dist < min_sl_price:
            raise ValueError(f"Order blocked: SL too close ({dist:.5f} < {min_sl_price:.5f})")

    def _stage_result(
        self,
        stage: str,
        ok: bool,
        reason_code: str,
        human_reason: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a normalized execution-stage payload."""
        return {
            "ok": bool(ok),
            "stage": stage,
            "reason_code": reason_code,
            "human_reason": human_reason,
            "payload": payload or {},
        }

    def _account_is_authorized(self, account_info: Any | None) -> bool:
        """Determine whether the MT5 account is available and matches the configured login."""
        if account_info is None:
            return False
        login = getattr(account_info, "login", None)
        expected_login = self.mt5_config.get("login")
        if login in (None, 0):
            return False
        if expected_login not in (None, 0) and int(login) != int(expected_login):
            return False
        return True

    def _runtime_credentials_present(self) -> bool:
        """Return whether the connector has enough credentials to perform a fresh login."""
        login_value = self.mt5_config.get("login")
        password_value = self.mt5_config.get("password")
        server_value = self.mt5_config.get("server")
        return bool(login_value not in (None, "", 0) and password_value and server_value)

    def _probe_connection_readiness(self, attempts: int = 3, delay_seconds: float = 0.5) -> tuple[bool, str, dict[str, Any]]:
        """Wait briefly for terminal/account state to settle before declaring failure."""
        attempts = max(1, int(attempts))
        delay_seconds = max(0.0, float(delay_seconds))
        last_snapshot: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            snapshot = self._connection_snapshot()
            last_snapshot = snapshot
            if snapshot.get("terminal_error"):
                return False, "ipc_failure", snapshot
            if snapshot.get("account_error"):
                return False, "account_info_failure", snapshot
            if snapshot.get("terminal_connected") and snapshot.get("tradeapi_disabled"):
                return False, "trade_api_disabled", snapshot
            if snapshot.get("terminal_connected") and snapshot.get("account_authorized"):
                return True, "connected", snapshot
            if attempt < attempts and delay_seconds > 0:
                time.sleep(delay_seconds)

        if not last_snapshot.get("terminal_connected"):
            return False, "ipc_unavailable", last_snapshot
        if not self._runtime_credentials_present():
            return False, "missing_runtime_credentials", last_snapshot
        return False, "authorization_pending", last_snapshot

    def _connection_snapshot(self) -> dict[str, Any]:
        """Capture a compact terminal/account connection snapshot for diagnostics."""
        snapshot: dict[str, Any] = {
            "connected_flag": bool(self.connected),
            "terminal_connected": False,
            "trade_allowed": None,
            "tradeapi_disabled": None,
            "account_authorized": False,
            "account_login": None,
            "server": None,
            "last_error": self._mt5_last_error(),
        }
        try:
            self.terminal_info = self._call_mt5("terminal_info", mt5.terminal_info)
            if self.terminal_info is not None:
                snapshot["terminal_connected"] = bool(getattr(self.terminal_info, "connected", False))
                snapshot["trade_allowed"] = bool(getattr(self.terminal_info, "trade_allowed", False))
                snapshot["tradeapi_disabled"] = bool(getattr(self.terminal_info, "tradeapi_disabled", False))
                snapshot["terminal_path"] = getattr(self.terminal_info, "path", None)
        except Exception as exc:
            snapshot["terminal_error"] = str(exc)

        try:
            self.account_info = self._call_mt5("account_info", mt5.account_info)
            if self.account_info is not None:
                snapshot["account_login"] = getattr(self.account_info, "login", None)
                snapshot["server"] = getattr(self.account_info, "server", None)
                snapshot["trade_mode"] = getattr(self.account_info, "trade_mode", None)
                snapshot["balance"] = float(getattr(self.account_info, "balance", 0.0))
                snapshot["equity"] = float(getattr(self.account_info, "equity", 0.0))
                snapshot["margin"] = float(getattr(self.account_info, "margin", 0.0))
                snapshot["margin_free"] = float(getattr(self.account_info, "margin_free", 0.0))
                snapshot["margin_level"] = float(getattr(self.account_info, "margin_level", 0.0))
                snapshot["account_authorized"] = self._account_is_authorized(self.account_info)
        except Exception as exc:
            snapshot["account_error"] = str(exc)
        return snapshot

    def verify_connection_state(self) -> tuple[bool, str, dict[str, Any]]:
        """Verify MT5 initialization, connectivity, and account authorization."""
        snapshot = self._connection_snapshot()
        if not snapshot.get("terminal_connected"):
            self.connected = False
            return False, "terminal_disconnected", snapshot
        if not snapshot.get("account_authorized"):
            self.connected = False
            return False, "account_not_authorized", snapshot
        if snapshot.get("trade_allowed") is False:
            self.connected = False
            return False, "trade_not_allowed", snapshot
        if snapshot.get("tradeapi_disabled"):
            self.connected = False
            return False, "trade_api_disabled", snapshot
        self.connected = True
        return True, "connected", snapshot

    def _log_connection_state(self, category: str, level: str = "INFO") -> dict[str, Any]:
        """Emit a structured connection snapshot."""
        snapshot = self._connection_snapshot()
        self.logger.structured(category, snapshot, level=level)
        return snapshot

    def _symbol_snapshot(self, symbol_info: Any | None = None) -> dict[str, Any]:
        """Capture a compact symbol specification snapshot."""
        info = symbol_info if symbol_info is not None else self.symbol_info
        if info is None:
            return {"symbol": self.symbol, "available": False}
        return {
            "symbol": self.symbol,
            "available": True,
            "visible": bool(getattr(info, "visible", False)),
            "trade_mode": int(getattr(info, "trade_mode", -1)),
            "trade_exemode": int(getattr(info, "trade_exemode", -1)),
            "digits": int(getattr(info, "digits", 0)),
            "point": float(getattr(info, "point", 0.0)),
            "volume_min": float(getattr(info, "volume_min", 0.0)),
            "volume_max": float(getattr(info, "volume_max", 0.0)),
            "volume_step": float(getattr(info, "volume_step", 0.0)),
            "stops_level": int(getattr(info, "trade_stops_level", 0)),
            "freeze_level": int(getattr(info, "trade_freeze_level", 0)),
            "filling_mode_raw": int(getattr(info, "filling_mode", 0)),
            "order_mode": int(getattr(info, "order_mode", 0)),
            "filling_modes": self.get_supported_filling_modes(info),
        }

    def initialize(self) -> bool:
        """Initialize the MT5 terminal with retry logic and account login."""
        initialize_kwargs: dict[str, Any] = {"timeout": self.timeout_ms}
        if self.mt5_config.get("terminal_path"):
            initialize_kwargs["path"] = str(self.mt5_config["terminal_path"])

        retries = max(1, int(self.mt5_config.get("reconnect_retries", 3)))
        retry_delay = max(0.0, float(self.mt5_config.get("reconnect_retry_delay_seconds", 5)))
        readiness_attempts = max(1, min(5, int(self.mt5_config.get("startup_readiness_attempts", 3))))
        readiness_delay = max(0.1, min(1.0, float(self.mt5_config.get("startup_readiness_delay_seconds", 0.5))))

        for attempt in range(1, retries + 1):
            try:
                self.logger.info(f"MT5 initialize attempt {attempt}/{retries}")
                self._log_connection_state("mt5_connection_state_pre_initialize")
                initialized = self._call_mt5("initialize", mt5.initialize, **initialize_kwargs)
                if not initialized:
                    last_error = self._mt5_last_error()
                    self.logger.warning(
                        f"MT5 initialize attempt {attempt}/{retries} failed: "
                        f"initialize_failure | {last_error['code']} {last_error['message']}"
                    )
                else:
                    ready, readiness_reason, readiness_snapshot = self._probe_connection_readiness(
                        attempts=readiness_attempts,
                        delay_seconds=readiness_delay,
                    )
                    if ready:
                        self.connected = True
                    elif not self._login():
                        self.logger.warning(
                            f"MT5 login attempt {attempt}/{retries} failed: {readiness_reason}"
                        )
                    else:
                        ready, readiness_reason, readiness_snapshot = self._probe_connection_readiness(
                            attempts=readiness_attempts,
                            delay_seconds=readiness_delay,
                        )
                    healthy, connection_reason, snapshot = self.verify_connection_state()
                    self.logger.structured("mt5_connection_state_post_initialize", snapshot)
                    if not healthy:
                        self.logger.warning(
                            f"MT5 connection verification failed after login: {connection_reason}"
                        )
                    elif not self.ensure_symbol(log_details=True):
                        self.logger.warning("MT5 symbol readiness failed during initialize")
                    else:
                        tick, _ = self.get_tick_with_retry()
                        if tick is not None:
                            self._update_server_time_offset(getattr(tick, "time", None))
                        self.connected = True
                        self.logger.info(
                            f"MT5 connected | Login: {self.account_info.login} | Server: {self.account_info.server}"
                        )
                        return True
            except Exception as exc:
                self.logger.error(f"MT5 initialize exception on attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(retry_delay)
        self.connected = False
        return False

    def _login(self) -> bool:
        """Authenticate to the configured MT5 account."""
        login_value = self.mt5_config.get("login")
        password_value = self.mt5_config.get("password")
        server_value = self.mt5_config.get("server")
        readiness_attempts = max(1, min(5, int(self.mt5_config.get("startup_readiness_attempts", 3))))
        readiness_delay = max(0.1, min(1.0, float(self.mt5_config.get("startup_readiness_delay_seconds", 0.5))))

        ready, readiness_reason, snapshot = self._probe_connection_readiness(
            attempts=readiness_attempts,
            delay_seconds=readiness_delay,
        )
        if ready:
            self.logger.info("MT5 terminal session is already authorized; login not required")
            return True

        if not self._runtime_credentials_present():
            if snapshot.get("terminal_connected"):
                self.logger.warning(
                    "MT5 credentials are missing from runtime configuration; waiting for the existing terminal session to authorize"
                )
                ready, readiness_reason, snapshot = self._probe_connection_readiness(
                    attempts=readiness_attempts,
                    delay_seconds=readiness_delay,
                )
                if ready:
                    self.logger.info("MT5 credentials missing; reusing existing authorized terminal session")
                    return True
            self.logger.error(
                f"MT5 credentials are missing from runtime configuration | readiness_reason={readiness_reason}"
            )
            return False

        logged_in = self._call_mt5(
            "login",
            mt5.login,
            int(login_value),
            password=str(password_value),
            server=str(server_value),
            timeout=self.timeout_ms,
        )
        if not logged_in:
            last_error = self._mt5_last_error()
            self.logger.warning(
                f"MT5 authorization failed: {last_error['code']} {last_error['message']}"
            )
            return False
        ready, readiness_reason, snapshot = self._probe_connection_readiness(
            attempts=readiness_attempts,
            delay_seconds=readiness_delay,
        )
        if not ready:
            self.logger.warning(
                f"MT5 login completed but readiness probe did not settle: {readiness_reason}"
            )
            return False
        return True

    def shutdown(self) -> None:
        """Close the MT5 connection."""
        try:
            mt5.shutdown()
        finally:
            self.connected = False

    def reconnect(self) -> bool:
        """Reconnect to the MT5 terminal with bounded retries and visibility."""
        retries = max(1, int(self.mt5_config.get("reconnect_retries", 3)))
        retry_delay = max(0.0, float(self.mt5_config.get("reconnect_retry_delay_seconds", 5)))
        self.logger.warning("MT5 connection lost. Attempting reconnect...")
        self._log_connection_state("mt5_connection_state_before_reconnect", level="WARNING")

        for attempt in range(1, retries + 1):
            if self.initialize():
                self.last_reconnect_at = utc_now()
                self.logger.info(f"MT5 reconnect successful on attempt {attempt}/{retries}")
                self._log_connection_state("mt5_connection_state_after_reconnect")
                return True
            self.logger.warning(f"MT5 reconnect attempt {attempt}/{retries} failed")
            if attempt < retries:
                time.sleep(retry_delay)

        self.logger.error("MT5 reconnect failed after retry attempts")
        self._log_connection_state("mt5_connection_state_reconnect_failed", level="ERROR")
        return False

    def is_connected(self) -> bool:
        """Check if the MT5 connection is healthy."""
        connected, _, _ = self.verify_connection_state()
        return connected

    def ensure_connection(self) -> bool:
        """Re-establish the MT5 connection when needed."""
        connected, _, _ = self.verify_connection_state()
        if connected:
            return True
        return self.reconnect()

    def reconnect_cooldown_remaining(self) -> float:
        """Return seconds remaining before trading after a reconnect."""
        if self.last_reconnect_at is None or self.reconnect_cooldown_seconds <= 0:
            return 0.0
        elapsed = (utc_now() - self.last_reconnect_at).total_seconds()
        return max(0.0, self.reconnect_cooldown_seconds - elapsed)

    def connection_health_check(self, reconnect_if_needed: bool = True) -> dict[str, Any]:
        """Run a strict connection and terminal health check."""
        connected, reason, snapshot = self.verify_connection_state()
        if connected:
            return self._stage_result(
                "connection_health_check",
                True,
                "connected",
                "MT5 terminal connected and authorized",
                {"connection": snapshot, "reconnect_cooldown_remaining": self.reconnect_cooldown_remaining()},
            )

        if reconnect_if_needed and self.reconnect():
            _, reason, snapshot = self.verify_connection_state()
            return self._stage_result(
                "connection_health_check",
                reason == "connected",
                "reconnected" if reason == "connected" else reason,
                "MT5 reconnected successfully" if reason == "connected" else f"MT5 reconnect ended with {reason}",
                {"connection": snapshot, "reconnect_cooldown_remaining": self.reconnect_cooldown_remaining()},
            )

        return self._stage_result(
            "connection_health_check",
            False,
            reason,
            f"MT5 health check failed: {reason}",
            {"connection": snapshot, "last_error": self._mt5_last_error()},
        )

    def symbol_prepare_stage(self, log_details: bool = True) -> dict[str, Any]:
        """Validate symbol visibility and tradability as an explicit execution stage."""
        symbol_ready = self.ensure_symbol(log_details=log_details)
        snapshot = self._symbol_snapshot()
        return self._stage_result(
            "symbol_prepare",
            symbol_ready,
            "symbol_ready" if symbol_ready else "symbol_unavailable",
            "Symbol selected and tradeable" if symbol_ready else f"Symbol {self.symbol} is unavailable for trading",
            {"symbol": snapshot, "last_error": self._mt5_last_error() if not symbol_ready else None},
        )

    def ensure_symbol(self, log_details: bool = False) -> bool:
        """Validate that the configured symbol exists, is visible, and is tradeable."""
        symbol_selected = self._call_mt5("symbol_select", mt5.symbol_select, self.symbol, True)
        if not symbol_selected:
            self.logger.error(f"Unable to select symbol: {self.symbol}")
            self.logger.structured(
                "mt5_symbol_prepare",
                {
                    "symbol": self.symbol,
                    "reason": "symbol_select_failed",
                    "last_error": self._mt5_last_error(),
                },
                level="ERROR",
            )
            return False

        symbol_info = self._call_mt5("symbol_info", mt5.symbol_info, self.symbol)
        if symbol_info is None:
            self.logger.error(f"Symbol not found: {self.symbol}")
            self.logger.structured(
                "mt5_symbol_prepare",
                {
                    "symbol": self.symbol,
                    "reason": "symbol_info_missing",
                    "last_error": self._mt5_last_error(),
                },
                level="ERROR",
            )
            return False
        if int(symbol_info.trade_mode) == mt5.SYMBOL_TRADE_MODE_DISABLED:
            self.logger.error(f"Symbol is not tradeable: {self.symbol}")
            self.logger.structured("mt5_symbol_prepare", self._symbol_snapshot(symbol_info), level="ERROR")
            return False

        self.symbol_info = self._call_mt5("symbol_info_refresh", mt5.symbol_info, self.symbol)
        if self.symbol_info is None:
            self.logger.error(f"Unable to refresh symbol info for {self.symbol}")
            return False
        pip_size = infer_pip_size(self.symbol, int(self.symbol_info.digits), float(self.symbol_info.point))
        tick_size = float(self.symbol_info.trade_tick_size or self.symbol_info.point or 1)
        tick_value = float(self.symbol_info.trade_tick_value or 0.0)
        pip_value_per_lot = tick_value * (pip_size / tick_size) if tick_size > 0 else 0.0

        self.symbol_spec = {
            "symbol": self.symbol,
            "point": float(self.symbol_info.point),
            "digits": int(self.symbol_info.digits),
            "pip_size": pip_size,
            "volume_min": float(self.symbol_info.volume_min),
            "volume_max": float(self.symbol_info.volume_max),
            "volume_step": float(self.symbol_info.volume_step),
            "contract_size": float(self.symbol_info.trade_contract_size),
            "trade_tick_size": tick_size,
            "trade_tick_value": tick_value,
            "pip_value_per_lot": pip_value_per_lot,
            "filling_mode": int(self.symbol_info.filling_mode),
            "stops_level": int(self.symbol_info.trade_stops_level),
            "freeze_level": int(getattr(self.symbol_info, "trade_freeze_level", 0)),
            "trade_mode": int(self.symbol_info.trade_mode),
            "trade_exemode": int(getattr(self.symbol_info, "trade_exemode", 0)),
            "order_mode": int(getattr(self.symbol_info, "order_mode", 0)),
            "filling_modes": self.get_supported_filling_modes(self.symbol_info),
        }
        if log_details or self.execution_verbosity == "detailed":
            self.logger.structured("mt5_symbol_prepare", self._symbol_snapshot(self.symbol_info))
        return True

    def validate_startup(self, bars: int = 120, require_live_tick: bool = True) -> tuple[bool, str]:
        """Validate startup readiness.

        When `require_live_tick` is False, stale/no tick does not fail startup. This is used
        for BACKTEST mode and for graceful weekend startup in LIVE/DRY_RUN.
        """
        try:
            if not self.ensure_symbol(log_details=True):
                return False, "Symbol validation failed"
            tick, tick_reason = self.get_tick_with_retry()
            if tick is None and require_live_tick:
                return False, f"Tick prices unavailable: {tick_reason}"
            if tick is None and not require_live_tick:
                self.logger.warning(
                    f"Startup proceeding without fresh live tick: {tick_reason}. "
                    "Execution will wait for valid market data in runtime gating."
                )
            self.fetch_rates("1min", max(120, bars), min_rows=50)
            self.fetch_rates("3min", max(120, bars), min_rows=50)
            self.fetch_rates("15min", max(120, bars), min_rows=50)
            if tick is None:
                return True, "Startup market-data validation passed (tick deferred)"
            return True, "Startup market-data validation passed"
        except Exception as exc:
            return False, str(exc)

    def get_account_info(self) -> Any:
        """Return the latest account info object."""
        self.account_info = self._call_mt5("account_info", mt5.account_info)
        return self.account_info

    def get_terminal_info(self) -> Any:
        """Return the latest terminal info object."""
        self.terminal_info = self._call_mt5("terminal_info", mt5.terminal_info)
        return self.terminal_info

    def clear_market_data_degraded(self) -> None:
        """Explicitly clear the market_data_degraded flag when data is known to be fresh.
        
        Call this when market data has been successfully refreshed and you want to
        resume normal execution that was previously blocked due to degraded market data.
        """
        was_degraded = self.market_data_degraded
        self.market_data_degraded = False
        self.market_data_source = "mt5"
        if was_degraded:
            self.logger.structured(
                "market_data_recovery",
                {
                    "symbol": self.symbol,
                    "previous_source": self.market_data_source,
                    "current_source": "mt5",
                },
                level="INFO",
            )

    def get_symbol_spec(self, force_refresh: bool = False) -> dict[str, Any]:
        """Return symbol specifications, with optional forced refresh from broker.
        
        Args:
            force_refresh: If True, query MT5 for current symbol info even if cached.
                          Use this before critical operations (position sizing, order submission)
                          to ensure lot_size, leverage, and other parameters match current broker state.
        
        Returns:
            Dictionary containing volume constraints, pip size, and other trading parameters.
            
        Raises:
            RuntimeError if symbol specification cannot be retrieved.
        """
        if force_refresh or self.symbol_spec is None:
            # Force refresh from MT5 to catch any parameter changes (leverage, lot sizes, etc)
            if not self.ensure_symbol(log_details=force_refresh):
                raise RuntimeError(f"Symbol specification unavailable (force_refresh={force_refresh})")
        return dict(self.symbol_spec or {})

    def ensure_initialized(self) -> bool:
        """Ensure the MT5 terminal is initialized and reachable."""
        if self.connected:
            connected, _, _ = self.verify_connection_state()
            if connected:
                return True
        return self.initialize()

    def ensure_logged_in(self) -> bool:
        """Ensure the MT5 session is authenticated and connected."""
        if self.ensure_connection():
            return True
        return self.ensure_initialized() and self.ensure_connection()

    def ensure_symbol_selected(self, symbol: str | None = None, log_details: bool = False) -> bool:
        """Ensure a symbol is selected and visible in Market Watch."""
        requested_symbol = str(symbol or self.symbol)
        if requested_symbol != self.symbol:
            raise ValueError(f"MT5Connector is bound to {self.symbol}, not {requested_symbol}")
        return self.ensure_symbol(log_details=log_details)

    def normalize_volume(self, volume: float, symbol_spec: dict[str, Any] | None = None) -> float:
        """Normalize a raw lot size to the broker min/max/step constraints."""
        spec = symbol_spec or self.get_symbol_spec()
        return floor_volume_to_step(
            float(volume),
            float(spec["volume_min"]),
            float(spec["volume_max"]),
            float(spec["volume_step"]),
        )

    def get_supported_filling_modes(self, symbol_info: Any | None = None) -> list[int]:
        """Return supported filling modes in fallback order for the current symbol."""
        info = symbol_info if symbol_info is not None else self.symbol_info
        if info is None:
            return [mt5.ORDER_FILLING_RETURN]

        order_fok = int(getattr(mt5, "ORDER_FILLING_FOK", 0))
        order_ioc = int(getattr(mt5, "ORDER_FILLING_IOC", 1))
        order_return = int(getattr(mt5, "ORDER_FILLING_RETURN", 2))
        symbol_fok = int(getattr(mt5, "SYMBOL_FILLING_FOK", 1))
        symbol_ioc = int(getattr(mt5, "SYMBOL_FILLING_IOC", 2))
        raw_mode = int(getattr(info, "filling_mode", 0))
        execution_mode = int(getattr(info, "trade_exemode", 0))
        market_execution = int(getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2))
        request_execution = int(getattr(mt5, "SYMBOL_TRADE_EXECUTION_REQUEST", 1))
        instant_execution = int(getattr(mt5, "SYMBOL_TRADE_EXECUTION_INSTANT", 0))

        candidates: list[int] = []
        if raw_mode in {order_fok, order_ioc, order_return}:
            candidates.append(raw_mode)
        if raw_mode & symbol_ioc:
            candidates.append(order_ioc)
        if raw_mode & symbol_fok:
            candidates.append(order_fok)
        if execution_mode in {instant_execution, request_execution}:
            candidates.extend([order_return, order_ioc, order_fok])
        elif execution_mode != market_execution:
            candidates.append(order_return)

        unique_modes: list[int] = []
        for mode in candidates:
            if mode not in unique_modes:
                unique_modes.append(mode)
        return unique_modes or [order_ioc, order_fok, order_return]

    def get_filling_mode(self) -> int:
        """Return the preferred filling mode for the symbol."""
        return self.choose_filling_mode()

    def choose_filling_mode(self, preferred: int | None = None, symbol_info: Any | None = None) -> int:
        """Choose a broker-supported filling mode, honoring a preferred mode when possible."""
        supported_modes = self.get_supported_filling_modes(symbol_info)
        if preferred is not None and int(preferred) in supported_modes:
            return int(preferred)
        return int(supported_modes[0])

    def fetch_rates(self, timeframe_label: str, bars: int, min_rows: int = 100) -> pd.DataFrame:
        """Fetch rates and return a validated dataframe of closed candles."""
        timeframe = self.TIMEFRAME_MAP[timeframe_label]
        self.market_data_degraded = False
        self.market_data_source = "mt5"
        try:
            rates = self._call_mt5("copy_rates_from_pos", mt5.copy_rates_from_pos, self.symbol, timeframe, 0, bars + 10)
        except Exception as exc:
            self.logger.warning(
                f"MT5 historical bars unavailable for {self.symbol} {timeframe_label}: {exc}"
            )
            return self._synthetic_rates_from_tick(timeframe_label, bars, min_rows)
        if rates is None:
            error_code, error_message = mt5.last_error()
            self.logger.warning(
                f"MT5 historical bars unavailable for {self.symbol} {timeframe_label}: {error_code} {error_message}"
            )
            return self._synthetic_rates_from_tick(timeframe_label, bars, min_rows)

        df = pd.DataFrame(rates)
        if not df.empty:
            self._update_server_time_offset(df["time"].iloc[-1])
        df["time"] = self._normalize_broker_timestamp_series(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

        candle_duration = timeframe_duration(timeframe_label)
        now_utc = utc_now()
        if not df.empty:
            last_open = pd.to_datetime(df.iloc[-1]["time"], utc=True).to_pydatetime()
            if last_open + candle_duration > now_utc - pd.Timedelta(seconds=2).to_pytimedelta():
                df = df.iloc[:-1].copy()

        df = df.tail(bars).reset_index(drop=True)
        valid, reason = validate_ohlc_dataframe(df, min_rows=min_rows)
        if not valid:
            raise RuntimeError(f"{timeframe_label} data validation failed: {reason}")
        return df

    def fetch_rates_range(self, timeframe_label: str, start_utc: datetime, end_utc: datetime, min_rows: int = 100) -> pd.DataFrame:
        """Fetch rates over an explicit UTC range and return validated closed candles."""
        start_utc = _ensure_utc_datetime(start_utc, "start_utc")
        end_utc = _ensure_utc_datetime(end_utc, "end_utc")
        if end_utc <= start_utc:
            raise ValueError(
                f"Invalid historical range for {timeframe_label}: "
                f"end_utc ({end_utc.isoformat()}) must be after start_utc ({start_utc.isoformat()})"
            )

        timeframe = self.TIMEFRAME_MAP[timeframe_label]
        self.market_data_degraded = False
        self.market_data_source = "mt5"
        error_code: Any = None
        error_message: Any = None
        fallback_used = False
        try:
            rates = self._call_mt5("copy_rates_range", mt5.copy_rates_range, self.symbol, timeframe, start_utc, end_utc)
        except Exception as exc:
            self.logger.warning(
                f"MT5 primary range fetch failed for {self.symbol} {timeframe_label} | "
                f"start={start_utc.isoformat()} | end={end_utc.isoformat()} | error={exc}"
            )
            rates = None
            error_message = str(exc)
        if rates is None:
            if error_code is None:
                error_code, error_message = mt5.last_error()
            self.logger.warning(
                f"MT5 primary range fetch failed for {self.symbol} {timeframe_label} | "
                f"start={start_utc.isoformat()} | end={end_utc.isoformat()} | "
                f"last_error={error_code} {error_message}"
            )
            if int(error_code or 0) in {-2, -1}:
                estimated_bars = _range_to_estimated_bar_count(timeframe_label, start_utc, end_utc, min_rows)
                self.logger.warning(
                    f"MT5 count-based historical fallback used for {self.symbol} {timeframe_label} | "
                    f"start={start_utc.isoformat()} | end={end_utc.isoformat()} | bars={estimated_bars} | "
                    f"primary_error={error_code} {error_message}"
                )
                fallback_used = True
                try:
                    rates = self._call_mt5(
                        "copy_rates_from_pos",
                        mt5.copy_rates_from_pos,
                        self.symbol,
                        timeframe,
                        0,
                        estimated_bars,
                    )
                except Exception as exc:
                    self.logger.warning(
                        f"MT5 count-based historical fallback failed for {self.symbol} {timeframe_label} | "
                        f"start={start_utc.isoformat()} | end={end_utc.isoformat()} | error={exc}"
                    )
                    rates = None
            if rates is None:
                raise RuntimeError(f"copy_rates_range failed: {error_code} {error_message}")

        df = pd.DataFrame(rates)
        if df.empty:
            detail = " after count-based fallback" if fallback_used else ""
            self.logger.warning(
                f"MT5 historical range returned no bars{detail} for {self.symbol} {timeframe_label} | "
                f"start={start_utc.isoformat()} | end={end_utc.isoformat()}"
            )
            raise RuntimeError(f"No {timeframe_label} bars returned in requested range")
        self._update_server_time_offset(df["time"].iloc[-1])
        df["time"] = self._normalize_broker_timestamp_series(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        df = df[(df["time"] >= pd.Timestamp(start_utc)) & (df["time"] <= pd.Timestamp(end_utc))].reset_index(drop=True)
        if df.empty:
            detail = " after count-based fallback" if fallback_used else ""
            self.logger.warning(
                f"MT5 historical range empty after filtering{detail} for {self.symbol} {timeframe_label} | "
                f"start={start_utc.isoformat()} | end={end_utc.isoformat()}"
            )
            raise RuntimeError(f"No {timeframe_label} bars returned in requested range")
        valid, reason = validate_ohlc_dataframe(df, min_rows=min_rows)
        if not valid:
            raise RuntimeError(f"{timeframe_label} range data validation failed: {reason}")
        return df

    def _tick_snapshot(self, tick: Any | None) -> dict[str, Any]:
        """Return a compact tick snapshot."""
        if tick is None:
            return {"symbol": self.symbol, "available": False}
        tick_time = getattr(tick, "time_msc", None)
        if tick_time:
            tick_timestamp = datetime.utcfromtimestamp(float(tick_time) / 1000.0).isoformat()
        elif getattr(tick, "time", None):
            tick_timestamp = datetime.utcfromtimestamp(float(tick.time)).isoformat()
        else:
            tick_timestamp = None
        return {
            "symbol": self.symbol,
            "available": True,
            "bid": float(getattr(tick, "bid", 0.0) or 0.0),
            "ask": float(getattr(tick, "ask", 0.0) or 0.0),
            "last": float(getattr(tick, "last", 0.0) or 0.0),
            "volume": float(getattr(tick, "volume", 0.0) or 0.0),
            "time": tick_timestamp,
        }

    def get_tick_with_retry(self) -> tuple[Any | None, str | None]:
        """Fetch a valid trade tick with retries and explicit failure reasons."""
        retries = max(1, int(self.mt5_config.get("tick_retries", 5)))
        retry_delay = max(0.0, float(self.mt5_config.get("tick_retry_delay_seconds", 0.4)))
        max_tick_age_seconds = max(0.0, float(self.mt5_config.get("max_tick_age_seconds", 5)))

        for attempt in range(1, retries + 1):
            tick = self._call_mt5("symbol_info_tick", mt5.symbol_info_tick, self.symbol)
            if tick is None:
                self.logger.structured(
                    "mt5_tick_fetch",
                    {
                        "symbol": self.symbol,
                        "attempt": attempt,
                        "retries": retries,
                        "execution_blocked_reason": "no_tick_data",
                        "last_error": self._mt5_last_error(),
                    },
                    level="WARNING",
                )
            else:
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                tick_time = getattr(tick, "time_msc", None)
                tick_age_seconds: float | None = None
                if tick_time:
                    tick_age_seconds = max(0.0, (utc_now().timestamp() * 1000.0 - float(tick_time)) / 1000.0)
                elif getattr(tick, "time", None):
                    tick_age_seconds = max(0.0, utc_now().timestamp() - float(tick.time))

                if bid <= 0 or ask <= 0:
                    self.logger.structured(
                        "mt5_tick_fetch",
                        {
                            "symbol": self.symbol,
                            "attempt": attempt,
                            "retries": retries,
                            "execution_blocked_reason": "invalid_tick",
                            "tick": self._tick_snapshot(tick),
                        },
                        level="WARNING",
                    )
                elif tick_age_seconds is not None and tick_age_seconds > max_tick_age_seconds:
                    self.logger.structured(
                        "mt5_tick_fetch",
                        {
                            "symbol": self.symbol,
                            "attempt": attempt,
                            "retries": retries,
                            "execution_blocked_reason": "stale_tick",
                            "tick_age_seconds": round(tick_age_seconds, 3),
                            "tick": self._tick_snapshot(tick),
                        },
                        level="WARNING",
                    )
                else:
                    self._update_server_time_offset(getattr(tick, "time", None))
                    if self.execution_verbosity == "detailed":
                        self.logger.structured(
                            "mt5_tick_fetch",
                            {
                                "symbol": self.symbol,
                                "attempt": attempt,
                                "retries": retries,
                                "tick": self._tick_snapshot(tick),
                            },
                        )
                    return tick, None

            if attempt < retries:
                time.sleep(retry_delay)

        return None, "no_tick_data"

    def get_tick(self) -> Any:
        """Return the latest valid tick for the configured symbol."""
        tick, reason = self.get_tick_with_retry()
        if tick is None:
            raise RuntimeError(f"symbol_info_tick failed: {reason}")
        return tick

    def tick_fetch_stage(self, refresh: bool = False) -> dict[str, Any]:
        """Fetch a fresh tick as an explicit execution stage."""
        if refresh:
            price_refresh_delay = max(0.0, float(self.mt5_config.get("price_refresh_delay_seconds", 0.15)))
            if price_refresh_delay > 0:
                time.sleep(price_refresh_delay)
        tick, tick_reason = self.get_tick_with_retry()
        tick_snapshot = self._tick_snapshot(tick)
        if tick is None:
            return self._stage_result(
                "fresh_tick_check",
                False,
                tick_reason or "no_tick_data",
                "No valid fresh tick available",
                {"tick": tick_snapshot, "last_error": self._mt5_last_error()},
            )
        return self._stage_result(
            "fresh_tick_check",
            True,
            "tick_ready",
            "Fresh tick retrieved",
            {"tick": tick_snapshot},
        )

    def spread_check_stage(self, max_spread_points: float, tick: Any | None = None) -> dict[str, Any]:
        """Validate spread against the configured execution ceiling."""
        reference_tick = tick if tick is not None else self.get_tick()
        point = float(self.get_symbol_spec()["point"])
        spread_points = (float(reference_tick.ask) - float(reference_tick.bid)) / point if point > 0 else 0.0
        ok = spread_points <= float(max_spread_points)
        return self._stage_result(
            "spread_check",
            ok,
            "spread_ok" if ok else "spread_too_wide",
            "Spread within limit" if ok else f"Spread {spread_points:.2f} exceeds {float(max_spread_points):.2f}",
            {
                "spread_points": round(spread_points, 2),
                "max_spread_points": float(max_spread_points),
                "tick": self._tick_snapshot(reference_tick),
            },
        )

    def get_spread_points(self) -> float:
        """Return current spread in broker points."""
        tick = self.get_tick()
        point = float(self.get_symbol_spec()["point"])
        return (float(tick.ask) - float(tick.bid)) / point

    def get_bot_positions(self) -> list[Any]:
        """Return current bot-managed positions for the configured symbol."""
        positions = self._call_mt5("positions_get", mt5.positions_get, symbol=self.symbol) or []
        return [p for p in positions if int(getattr(p, "magic", 0)) == int(self.config["mt5"]["magic_number"])]

    def get_position_by_ticket(self, ticket: int | str) -> Any | None:
        """Return a live position by ticket."""
        try:
            positions = self._call_mt5("positions_get_ticket", mt5.positions_get, ticket=int(ticket)) or []
            return positions[0] if positions else None
        except Exception:
            return None

    def get_pending_orders(self) -> list[Any]:
        """Return current bot-managed pending orders for the configured symbol."""
        orders = self._call_mt5("orders_get", mt5.orders_get, symbol=self.symbol) or []
        return [o for o in orders if int(getattr(o, "magic", 0)) == int(self.config["mt5"]["magic_number"])]

    def calculate_margin(self, order_type: int, volume: float, price: float) -> float:
        """Calculate required margin for a hypothetical order."""
        margin = self._call_mt5("order_calc_margin", mt5.order_calc_margin, order_type, self.symbol, volume, price)
        if margin is None:
            error_code, error_message = mt5.last_error()
            raise RuntimeError(f"order_calc_margin failed: {error_code} {error_message}")
        return float(margin)

    def safe_order_send(self, request: dict[str, Any], retry: bool = True) -> Any:
        """Submit a trade request using a single MT5 request dict with safe transient retries."""
        request_dict = dict(request)
        max_attempts = max(1, int(self.mt5_config.get("order_send_transient_retries", 2 if retry else 1)))
        if not retry:
            max_attempts = 1
        retriable_codes = {
            int(getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004)),
            int(getattr(mt5, "TRADE_RETCODE_INVALID_PRICE", 10015)),
            int(getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020)),
            int(getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021)),
            int(getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012)),
            int(getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031)),
        }

        for attempt in range(1, max_attempts + 1):
            request_payload = self._serialize_log_value(request_dict)
            self.logger.structured(
                "mt5_order_send_request",
                {
                    "symbol": self.symbol,
                    "request": request_payload,
                    "request_type": type(request_dict).__name__,
                    "request_keys": sorted(str(key) for key in request_dict.keys()),
                    "retry_allowed": retry,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "order_send_call_style": "direct_positional_request_dict",
                },
            )
            self.logger.structured(
                "order_send_attempt",
                {
                    "symbol": self.symbol,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "request": request_payload,
                    "order_send_call_style": "direct_positional_request_dict",
                },
            )
            try:
                # This MT5 build accepts the trade request only as a positional
                # argument on order_send; named/wrapped variants can fail with
                # "Unnamed arguments not allowed" despite an otherwise valid request.
                result = mt5.order_send(request_dict)
            except Exception as exc:
                raise RuntimeError(f"MT5 call failed for order_send: {exc}") from exc
            if result is None:
                last_error = self._mt5_last_error()
                payload = {
                    "symbol": self.symbol,
                    "request": request_payload,
                    "request_type": type(request_dict).__name__,
                    "request_keys": sorted(str(key) for key in request_dict.keys()),
                    "response": None,
                    "result_is_none": True,
                    "retcode": None,
                    "mt5_last_error": last_error,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                }
                self.logger.structured("mt5_order_send_response", payload, level="ERROR")
                self.logger.structured("order_send_result", payload, level="ERROR")
                raise RuntimeError(f"order_send failed: {last_error['code']} {last_error['message']}")

            retcode = int(getattr(result, "retcode", -1))
            response_payload = self._serialize_log_value(result)
            payload = {
                "symbol": self.symbol,
                "request": request_payload,
                "request_type": type(request_dict).__name__,
                "request_keys": sorted(str(key) for key in request_dict.keys()),
                "response": response_payload,
                "result_is_none": False,
                "retcode": retcode,
                "retcode_comment": getattr(result, "comment", ""),
                "attempt": attempt,
                "max_attempts": max_attempts,
            }
            log_level = "INFO" if retcode == mt5.TRADE_RETCODE_DONE else "WARNING"
            self.logger.structured("mt5_order_send_response", payload, level=log_level)
            self.logger.structured("order_send_result", payload, level=log_level)

            if retcode in {
                int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
                int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
                int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
            }:
                return result

            if attempt >= max_attempts or retcode not in retriable_codes:
                return result

            self.logger.warning(
                f"Trade request retcode {retcode}. Retrying with a fresh tick ({attempt}/{max_attempts})..."
            )
            time.sleep(max(0.2, float(self.mt5_config.get("tick_retry_delay_seconds", 0.4))))
            fresh_tick, _ = self.get_tick_with_retry()
            if fresh_tick is None:
                return result
            if "price" in request_dict and int(request_dict.get("action", -1)) == int(getattr(mt5, "TRADE_ACTION_DEAL", 1)):
                if int(request_dict.get("type", -1)) == int(getattr(mt5, "ORDER_TYPE_BUY", 0)):
                    request_dict["price"] = normalize_price(float(fresh_tick.ask), int(self.get_symbol_spec()["digits"]))
                else:
                    request_dict["price"] = normalize_price(float(fresh_tick.bid), int(self.get_symbol_spec()["digits"]))
        return result

    def order_check_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run order_check as an explicit pipeline stage."""
        details = self.safe_order_check(request)
        payload = {
            "request": self._serialize_log_value(details["request"]),
            "result": self._serialize_log_value(details["result"]),
            "retcode": details["retcode"],
            "comment": details["comment"],
            "last_error": details["last_error"],
            "environment": {
                "connection": self._connection_snapshot(),
                "symbol": self._symbol_snapshot(),
            },
        }
        self.logger.structured("order_precheck", payload, level="INFO" if details["ok"] else "WARNING")
        if details["ok"]:
            return self._stage_result(
                "order_check",
                True,
                "order_check_ok",
                "MT5 order_check accepted the request",
                payload,
            )

        if details["result_is_none"]:
            return self._stage_result(
                "order_check",
                False,
                "order_check_none",
                "MT5 order_check returned None",
                payload,
            )

        mapped_reason, blocked_reason = self._map_order_check_failure(details["result"], details["request"])
        payload["mapped_reason"] = mapped_reason
        return self._stage_result(
            "order_check",
            False,
            blocked_reason,
            f"MT5 order_check rejected the request: {details['comment'] or blocked_reason}",
            payload,
        )

    def order_send_stage(self, request: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Run order_send or paper acceptance as an explicit pipeline stage."""
        request_payload = self._serialize_log_value(request)
        try:
            self._validate_order_request(request)
        except ValueError as exc:
            return self._stage_result(
                "order_send_or_paper_accept",
                False,
                "invalid_protective_levels",
                str(exc),
                {"request": request_payload},
            )
        if dry_run:
            self.logger.structured(
                "order_send_result",
                {"request": request_payload, "dry_run": True, "reason_code": "paper_execution"},
            )
            return self._stage_result(
                "order_send_or_paper_accept",
                True,
                "paper_execution",
                "Dry-run validated order without sending it",
                {"request": request_payload, "dry_run": True},
            )

        try:
            result = self.safe_order_send(request)
        except RuntimeError as exc:
            return self._stage_result(
                "order_send_or_paper_accept",
                False,
                "order_send_runtime_failure",
                str(exc),
                {
                    "request": request_payload,
                    "last_error": self._mt5_last_error(),
                    "environment": {
                        "connection": self._connection_snapshot(),
                        "symbol": self._symbol_snapshot(),
                    },
                },
            )

        retcode = int(getattr(result, "retcode", -1)) if result is not None else None
        payload = {
            "request": request_payload,
            "result": self._serialize_log_value(result),
            "retcode": retcode,
            "comment": getattr(result, "comment", "") if result is not None else "",
            "environment": {
                "connection": self._connection_snapshot(),
                "symbol": self._symbol_snapshot(),
            },
        }
        if retcode in {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        }:
            return self._stage_result(
                "order_send_or_paper_accept",
                True,
                "order_send_done",
                "MT5 accepted the order_send request",
                payload,
            )

        return self._stage_result(
            "order_send_or_paper_accept",
            False,
            self._map_retcode_to_reason(retcode),
            f"MT5 order_send rejected the request: {payload['comment'] or retcode}",
            payload,
        )

    def _find_verified_position(
        self,
        direction: str,
        volume: float,
        entry_price: float,
    ) -> Any | None:
        """Try to find the resulting live position after a successful market order."""
        direction_type = int(getattr(mt5, "POSITION_TYPE_BUY", 0)) if direction == "LONG" else int(getattr(mt5, "POSITION_TYPE_SELL", 1))
        tolerance = max(float(self.get_symbol_spec()["point"]) * 50, 0.05)
        for position in self.get_bot_positions():
            if int(getattr(position, "type", -1)) != direction_type:
                continue
            if abs(float(getattr(position, "volume", 0.0)) - float(volume)) > float(self.get_symbol_spec()["volume_step"]) + 1e-9:
                continue
            if abs(float(getattr(position, "price_open", 0.0)) - float(entry_price)) <= tolerance:
                return position
        return None

    def post_send_verify_stage(
        self,
        result: Any,
        direction: str,
        volume: float,
        entry_price: float,
    ) -> dict[str, Any]:
        """Verify that a successful market order produced an observable broker state change."""
        retries = max(1, int(self.config.get("execution", {}).get("post_send_verify_retries", 4)))
        delay_seconds = max(0.1, float(self.config.get("execution", {}).get("post_send_verify_seconds", 2.0)) / retries)
        order_id = int(getattr(result, "order", 0) or 0)
        deal_id = int(getattr(result, "deal", 0) or 0)

        for attempt in range(1, retries + 1):
            position = self._find_verified_position(direction, volume, entry_price)
            if position is not None:
                return self._stage_result(
                    "post_send_verify",
                    True,
                    "post_send_verified",
                    "Resulting position verified after order_send",
                    {
                        "attempt": attempt,
                        "order": order_id,
                        "deal": deal_id,
                        "position": self._serialize_log_value(position),
                    },
                )
            if attempt < retries:
                time.sleep(delay_seconds)

        return self._stage_result(
            "post_send_verify",
            False,
            "post_send_verify_unconfirmed",
            "order_send returned success but the resulting position could not be verified",
            {
                "order": order_id,
                "deal": deal_id,
                "direction": direction,
                "volume": float(volume),
                "entry_price": float(entry_price),
                "positions_snapshot": self._serialize_log_value(self.get_bot_positions()),
                "orders_snapshot": self._serialize_log_value(self.get_pending_orders()),
            },
        )

    def _validate_and_adjust_stops(
        self,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        symbol_spec: dict[str, Any],
    ) -> tuple[bool, dict[str, float] | None, str]:
        """Validate stop levels against broker minimum stop distance."""
        digits = int(symbol_spec["digits"])
        point = float(symbol_spec["point"])
        stops_level_points = int(symbol_spec.get("stops_level", 0))
        freeze_level_points = int(symbol_spec.get("freeze_level", 0))
        min_stop_distance_points = max(stops_level_points, freeze_level_points)
        min_stop_distance = max(0.0, min_stop_distance_points * point)
        auto_adjust = bool(self.mt5_config.get("auto_adjust_stops", True))

        normalized_entry = normalize_price(entry_price, digits)
        normalized_sl = normalize_price(sl, digits)
        normalized_tp = normalize_price(tp, digits)

        if direction == "LONG":
            sl_distance = normalized_entry - normalized_sl
            tp_distance = normalized_tp - normalized_entry
            direction_valid = normalized_sl < normalized_entry and normalized_tp > normalized_entry
        else:
            sl_distance = normalized_sl - normalized_entry
            tp_distance = normalized_entry - normalized_tp
            direction_valid = normalized_sl > normalized_entry and normalized_tp < normalized_entry

        sl_distance_points = sl_distance / point if point > 0 else 0.0
        tp_distance_points = tp_distance / point if point > 0 else 0.0
        self.logger.structured(
            "mt5_stop_validation",
            {
                "symbol": self.symbol,
                "direction": direction,
                "entry": normalized_entry,
                "sl": normalized_sl,
                "tp": normalized_tp,
                "stops_level_points": stops_level_points,
                "freeze_level_points": freeze_level_points,
                "min_required_distance_points": min_stop_distance_points,
                "sl_distance_points": round(sl_distance_points, 3),
                "tp_distance_points": round(tp_distance_points, 3),
                "direction_valid": direction_valid,
            },
        )

        if not direction_valid:
            return False, None, "invalid_stops"

        if min_stop_distance <= 0:
            return True, {"entry": normalized_entry, "sl": normalized_sl, "tp": normalized_tp}, ""

        sl_invalid = sl_distance < min_stop_distance
        tp_invalid = tp_distance < min_stop_distance
        if not sl_invalid and not tp_invalid:
            return True, {"entry": normalized_entry, "sl": normalized_sl, "tp": normalized_tp}, ""

        if not auto_adjust:
            return False, None, "invalid_stops"

        adjusted_sl = normalized_sl
        adjusted_tp = normalized_tp
        if direction == "LONG":
            if sl_invalid:
                adjusted_sl = normalize_price(normalized_entry - min_stop_distance, digits)
            if tp_invalid:
                adjusted_tp = normalize_price(normalized_entry + min_stop_distance, digits)
        else:
            if sl_invalid:
                adjusted_sl = normalize_price(normalized_entry + min_stop_distance, digits)
            if tp_invalid:
                adjusted_tp = normalize_price(normalized_entry - min_stop_distance, digits)

        self.logger.warning(
            f"Adjusted SL/TP to satisfy broker minimum stop distance | "
            f"Entry: {normalized_entry:.5f} | SL: {normalized_sl:.5f}->{adjusted_sl:.5f} | "
            f"TP: {normalized_tp:.5f}->{adjusted_tp:.5f} | StopsLevelPoints: {stops_level_points} | "
            f"FreezeLevelPoints: {freeze_level_points}"
        )
        return True, {"entry": normalized_entry, "sl": adjusted_sl, "tp": adjusted_tp}, ""

    def build_order_request(
        self,
        direction: str,
        volume: float,
        entry_price: float,
        sl: float,
        tp: float,
        comment: str,
        type_filling: int,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Create a normalized MT5 market-order request."""
        symbol_spec = self.get_symbol_spec()
        min_volume = float(symbol_spec["volume_min"])
        normalized_volume = self.normalize_volume(float(volume), symbol_spec)
        if normalized_volume <= 0 or normalized_volume + 1e-12 < min_volume:
            return None, "invalid_volume", {}

        stops_valid, stop_prices, stop_reason = self._validate_and_adjust_stops(
            direction=direction,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            symbol_spec=symbol_spec,
        )
        if not stops_valid or stop_prices is None:
            return None, stop_reason or "Invalid stop levels", {}

        order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(normalized_volume),
            "type": order_type,
            "price": float(stop_prices["entry"]),
            "sl": float(stop_prices["sl"]),
            "tp": float(stop_prices["tp"]),
            "deviation": int(self.mt5_config["deviation"]),
            "magic": int(self.mt5_config["magic_number"]),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": int(self.choose_filling_mode(type_filling)),
        }
        return request, "", {
            **stop_prices,
            "volume": float(normalized_volume),
            "filling_mode": int(request["type_filling"]),
        }

    def get_trade_request(
        self,
        direction: str,
        volume: float,
        entry_price: float,
        sl: float,
        tp: float,
        comment: str,
        type_filling: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Build the canonical market-order request used by both order_check and order_send."""
        return self.build_order_request(
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            sl=sl,
            tp=tp,
            comment=comment,
            type_filling=self.choose_filling_mode(type_filling),
        )

    def precheck_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run the canonical MT5 order_check stage for a prepared request."""
        return self.order_check_stage(request)

    def send_order(self, request: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Run the canonical MT5 order_send stage for a prepared request."""
        return self.order_send_stage(request, dry_run=dry_run)

    def safe_order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run MT5 order_check using a single request dict and return a structured result."""
        request_dict = dict(request)
        self._validate_order_request(request_dict)
        order_type = int(request_dict.get("type", -1))
        side = "BUY" if order_type == int(getattr(mt5, "ORDER_TYPE_BUY", 0)) else "SELL"
        self.logger.structured(
            "mt5_order_check_preflight",
            {
                "symbol": self.symbol,
                "side": side,
                "request": self._serialize_log_value(request_dict),
                "request_type": type(request_dict).__name__,
                "request_keys": sorted(str(key) for key in request_dict.keys()),
                "price": float(request_dict.get("price", 0.0)),
                "sl": float(request_dict.get("sl", 0.0)),
                "tp": float(request_dict.get("tp", 0.0)),
                "volume": float(request_dict.get("volume", 0.0)),
                "type": int(request_dict.get("type", -1)),
                "type_filling": int(request_dict.get("type_filling", -1)),
                "symbol_constraints": self._symbol_snapshot(),
            },
        )
        result = mt5.order_check(request_dict)
        retcode = int(getattr(result, "retcode", -1)) if result is not None else None
        last_error = self._mt5_last_error()
        self.logger.structured(
            "mt5_order_check",
            {
                "symbol": self.symbol,
                "side": side,
                "request": self._serialize_log_value(request_dict),
                "request_type": type(request_dict).__name__,
                "request_keys": sorted(str(key) for key in request_dict.keys()),
                "result_is_none": result is None,
                "price": float(request_dict.get("price", 0.0)),
                "sl": float(request_dict.get("sl", 0.0)),
                "tp": float(request_dict.get("tp", 0.0)),
                "volume": float(request_dict.get("volume", 0.0)),
                "type": int(request_dict.get("type", -1)),
                "type_filling": int(request_dict.get("type_filling", -1)),
                "result": self._serialize_log_value(result),
                "retcode": retcode,
                "comment": getattr(result, "comment", "") if result is not None else "",
                "last_error": last_error,
            },
            level="INFO" if retcode in {0, int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))} else "WARNING",
        )
        return {
            "ok": bool(result is not None and retcode in {0, int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))}),
            "result": result,
            "retcode": retcode,
            "comment": getattr(result, "comment", "") if result is not None else "",
            "result_is_none": result is None,
            "last_error": last_error,
            "side": side,
            "request": request_dict,
        }

    def _map_retcode_to_reason(self, retcode: int | None) -> str:
        """Map MT5 trade retcodes to stable execution reasons."""
        if retcode in {
            int(getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004)),
            int(getattr(mt5, "TRADE_RETCODE_INVALID_PRICE", 10015)),
            int(getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020)),
            int(getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021)),
        }:
            return "broker_requote_or_price_change"
        mapping = {
            int(getattr(mt5, "TRADE_RETCODE_INVALID_VOLUME", 10014)): "blocked_invalid_volume",
            int(getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)): "blocked_invalid_stops",
            int(getattr(mt5, "TRADE_RETCODE_NO_MONEY", 10019)): "blocked_margin_constraint",
            int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)): "blocked_invalid_fill_mode",
            int(getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)): "blocked_market_closed",
            int(getattr(mt5, "TRADE_RETCODE_TRADE_DISABLED", 10017)): "blocked_symbol_not_tradeable",
        }
        return mapping.get(int(retcode) if retcode is not None else -1, "order_send_rejected")

    def _map_order_check_failure(self, result: Any, request: dict[str, Any]) -> tuple[str, str]:
        """Map an order_check failure to a specific execution reason and a compact blocked reason."""
        retcode = int(getattr(result, "retcode", -1)) if result is not None else None
        comment = str(getattr(result, "comment", "") or "").strip().lower()
        trade_mode = int(self.get_symbol_spec().get("trade_mode", -1))

        if result is None:
            return "order_check_api_error", "order_check_none"
        if trade_mode == int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 1)):
            return "blocked_symbol_not_tradeable", "symbol_not_tradeable"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)) or "stop" in comment:
            return "blocked_invalid_stops", "invalid_stops"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_INVALID_VOLUME", 10014)) or "volume" in comment:
            return "blocked_invalid_volume", "invalid_volume"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)) or "fill" in comment:
            return "blocked_invalid_fill_mode", "invalid_fill_mode"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)) or "market closed" in comment:
            return "blocked_market_closed", "market_closed"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_TRADE_DISABLED", 10017)) or "trade disabled" in comment:
            return "blocked_symbol_not_tradeable", "symbol_not_tradeable"
        if retcode == int(getattr(mt5, "TRADE_RETCODE_NO_MONEY", 10019)) or "money" in comment or "margin" in comment:
            return "blocked_insufficient_margin", "insufficient_margin"
        if "trade mode" in comment or "trade disabled" in comment:
            return "blocked_symbol_not_tradeable", "symbol_not_tradeable"
        return "unknown_order_check_failure", "unknown_order_check_failure"

    def send_market_order(
        self,
        direction: str,
        volume: float,
        sl: float,
        tp: float,
        comment: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Open or dry-run a market order for the configured symbol through explicit execution stages."""
        execution_result: dict[str, Any] = {
            "executed": False,
            "reason": "order_send_failed",
            "execution_blocked_reason": None,
            "result": None,
            "order_check": None,
            "request": None,
            "symbol_snapshot": None,
            "tick_snapshot": None,
            "account_snapshot": None,
            "entry": None,
            "sl": None,
            "tp": None,
            "volume": float(volume),
            "dry_run": bool(dry_run),
            "pipeline_stages": [],
            "failure_class": None,
        }
        connection_stage = self.connection_health_check(reconnect_if_needed=True)
        execution_result["pipeline_stages"].append(connection_stage)
        execution_result["account_snapshot"] = connection_stage["payload"].get("connection")
        if not connection_stage["ok"]:
            execution_result["reason"] = "blocked_no_connection"
            execution_result["execution_blocked_reason"] = connection_stage["reason_code"]
            execution_result["failure_class"] = "connection_failure"
            self.logger.structured("execution_result", execution_result, level="ERROR")
            return execution_result

        reconnect_cooldown_remaining = float(connection_stage["payload"].get("reconnect_cooldown_remaining", 0.0))
        if reconnect_cooldown_remaining > 0:
            cooldown_stage = self._stage_result(
                "connection_health_check",
                False,
                "reconnect_cooldown",
                f"Reconnect cooldown active for another {reconnect_cooldown_remaining:.1f}s",
                {"reconnect_cooldown_remaining": reconnect_cooldown_remaining},
            )
            execution_result["pipeline_stages"].append(cooldown_stage)
            execution_result["reason"] = "blocked_no_connection"
            execution_result["execution_blocked_reason"] = "reconnect_cooldown"
            execution_result["failure_class"] = "connection_failure"
            self.logger.structured("execution_result", execution_result, level="WARNING")
            return execution_result

        symbol_stage = self.symbol_prepare_stage(log_details=bool(self.mt5_config.get("symbol_prepare_each_trade", True)))
        execution_result["pipeline_stages"].append(symbol_stage)
        execution_result["symbol_snapshot"] = symbol_stage["payload"].get("symbol")
        if not symbol_stage["ok"]:
            execution_result["reason"] = "blocked_symbol_unavailable"
            execution_result["execution_blocked_reason"] = symbol_stage["reason_code"]
            execution_result["failure_class"] = "connection_failure"
            self.logger.structured("execution_result", execution_result, level="ERROR")
            return execution_result

        tick_stage = self.tick_fetch_stage(refresh=True)
        execution_result["pipeline_stages"].append(tick_stage)
        execution_result["tick_snapshot"] = tick_stage["payload"].get("tick")
        if not tick_stage["ok"]:
            execution_result["reason"] = "blocked_no_tick"
            execution_result["execution_blocked_reason"] = tick_stage["reason_code"]
            execution_result["failure_class"] = "connection_failure"
            self.logger.structured("execution_result", execution_result, level="ERROR")
            return execution_result

        tick_snapshot = tick_stage["payload"].get("tick", {}) or {}
        entry_price = float(tick_snapshot.get("ask", 0.0)) if direction == "LONG" else float(tick_snapshot.get("bid", 0.0))
        fill_modes = self.get_supported_filling_modes()
        last_order_check: Any = None

        for fill_mode in fill_modes:
            request, request_reason, stop_prices = self.build_order_request(
                direction=direction,
                volume=volume,
                entry_price=entry_price,
                sl=sl,
                tp=tp,
                comment=comment,
                type_filling=fill_mode,
            )
            stop_stage = self._stage_result(
                "stop_distance_validate",
                request is not None,
                "stop_distance_valid" if request is not None else str(request_reason or "invalid_stops"),
                "Stops validated and normalized" if request is not None else "Request normalization failed before order_check",
                {
                    "entry": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "normalized": stop_prices,
                    "volume": volume,
                },
            )
            execution_result["pipeline_stages"].append(stop_stage)
            if request is None:
                execution_result["reason"] = (
                    "blocked_invalid_volume" if request_reason == "invalid_volume" else "blocked_invalid_stops"
                )
                execution_result["execution_blocked_reason"] = request_reason or "invalid_stops"
                execution_result["failure_class"] = "broker_rejection"
                self.logger.structured("execution_result", execution_result, level="ERROR")
                return execution_result

            self.logger.structured(
                "mt5_order_request_candidate",
                {
                    "symbol": self.symbol,
                    "side": "BUY" if direction == "LONG" else "SELL",
                    "filling_mode": int(fill_mode),
                    "request": self._serialize_log_value(request),
                    "symbol_constraints": self._symbol_snapshot(),
                },
            )
            execution_result["request"] = self._serialize_log_value(request)
            execution_result["entry"] = float(stop_prices["entry"])
            execution_result["sl"] = float(stop_prices["sl"])
            execution_result["tp"] = float(stop_prices["tp"])
            execution_result["volume"] = float(stop_prices.get("volume", request["volume"]))
            execution_result["filling_mode"] = int(stop_prices.get("filling_mode", fill_mode))

            order_check_stage = self.order_check_stage(request) if bool(self.mt5_config.get("order_check_enabled", True)) else self._stage_result(
                "order_check",
                True,
                "order_check_skipped",
                "MT5 order_check skipped by configuration",
                {"request": self._serialize_log_value(request)},
            )
            execution_result["pipeline_stages"].append(order_check_stage)
            last_order_check = order_check_stage["payload"].get("result")
            execution_result["order_check"] = order_check_stage["payload"].get("result")
            if not order_check_stage["ok"]:
                retcode = order_check_stage["payload"].get("retcode")
                if order_check_stage["reason_code"] == "invalid_fill_mode" or retcode == int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)):
                    continue
                execution_result["reason"] = (
                    "order_check_api_error"
                    if order_check_stage["reason_code"] == "order_check_none"
                    else str(order_check_stage["payload"].get("mapped_reason") or "order_check_failed")
                )
                execution_result["execution_blocked_reason"] = order_check_stage["reason_code"]
                execution_result["order_check_last_error"] = order_check_stage["payload"].get("last_error")
                execution_result["failure_class"] = (
                    "runtime_api_failure" if order_check_stage["reason_code"] == "order_check_none" else "broker_rejection"
                )
                self.logger.structured("execution_result", execution_result, level="ERROR")
                return execution_result

            order_send_stage = self.order_send_stage(request, dry_run=dry_run)
            execution_result["pipeline_stages"].append(order_send_stage)
            execution_result["result"] = order_send_stage["payload"].get("result")
            if order_send_stage["ok"]:
                verify_stage = self._stage_result(
                    "post_send_verify",
                    True,
                    "paper_execution",
                    "Dry-run accepted without live broker state change",
                    {},
                ) if dry_run else self.post_send_verify_stage(
                    result=order_send_stage["payload"].get("result"),
                    direction=direction,
                    volume=float(stop_prices.get("volume", request["volume"])),
                    entry_price=float(stop_prices["entry"]),
                )
                execution_result["pipeline_stages"].append(verify_stage)
                execution_result["executed"] = (not dry_run) and bool(verify_stage["ok"])
                execution_result["reason"] = "dry_run_validated" if dry_run else ("executed" if verify_stage["ok"] else str(verify_stage["reason_code"]))
                execution_result["execution_blocked_reason"] = "paper_execution" if dry_run else (None if verify_stage["ok"] else str(verify_stage["reason_code"]))
                execution_result["failure_class"] = "paper_execution" if dry_run else (None if verify_stage["ok"] else "runtime_api_failure")
                self.logger.structured("execution_result", execution_result)
                if dry_run:
                    self.logger.info(
                        f"Dry-run validated order | {direction} | Volume: {volume:.2f} | "
                        f"Entry: {float(stop_prices['entry']):.5f} | SL: {float(stop_prices['sl']):.5f} | "
                        f"TP: {float(stop_prices['tp']):.5f}"
                    )
                return execution_result

            if order_send_stage["reason_code"] == "blocked_invalid_fill_mode":
                continue

            execution_result["reason"] = str(order_send_stage["reason_code"])
            execution_result["execution_blocked_reason"] = order_send_stage["payload"].get("comment") or order_send_stage["reason_code"]
            execution_result["account_snapshot"] = self._connection_snapshot()
            execution_result["failure_class"] = (
                "runtime_api_failure" if order_send_stage["reason_code"] == "order_send_runtime_failure" else "broker_rejection"
            )
            self.logger.structured("execution_result", execution_result, level="ERROR")
            return execution_result

        execution_result["order_check"] = self._serialize_log_value(last_order_check)
        execution_result["reason"] = "blocked_invalid_fill_mode"
        execution_result["execution_blocked_reason"] = "invalid_fill_mode"
        execution_result["failure_class"] = "broker_rejection"
        self.logger.structured("execution_result", execution_result, level="ERROR")
        return execution_result

    def health_report(self) -> dict[str, Any]:
        """Return a compact diagnostics snapshot for the MT5 environment."""
        connection_stage = self.connection_health_check(reconnect_if_needed=True)
        connected = bool(connection_stage["ok"])
        connection_reason = connection_stage["reason_code"]
        connection_snapshot = connection_stage["payload"].get("connection", {})
        symbol_stage = self.symbol_prepare_stage(log_details=True) if connected else self._stage_result(
            "symbol_prepare",
            False,
            "symbol_not_ready",
            "Symbol preparation skipped because connection is unavailable",
            {},
        )
        symbol_ok = bool(symbol_stage["ok"])
        tick_stage = self.tick_fetch_stage(refresh=False) if symbol_ok else self._stage_result(
            "tick_fetch",
            False,
            "symbol_not_ready",
            "Tick fetch skipped because symbol is unavailable",
            {},
        )
        tick_ok = bool(tick_stage["ok"])
        report: dict[str, Any] = {
            "symbol": self.symbol,
            "connection_ok": connected,
            "connection_reason": connection_reason,
            "connection": connection_snapshot,
            "symbol_ok": symbol_ok,
            "symbol_snapshot": symbol_stage["payload"].get("symbol", self._symbol_snapshot()),
            "tick_ok": tick_ok,
            "tick_reason": tick_stage["reason_code"],
            "tick_snapshot": tick_stage["payload"].get("tick", {"symbol": self.symbol, "available": False}),
            "reconnect_cooldown_remaining": self.reconnect_cooldown_remaining(),
        }
        tick_snapshot = report.get("tick_snapshot", {}) or {}
        tick_ask = float(tick_snapshot.get("ask", 0.0) or 0.0)
        if symbol_ok and tick_ok and tick_ask > 0 and bool(self.mt5_config.get("diagnostics_order_check", True)):
            sample_volume = float(self.get_symbol_spec()["volume_min"])
            point = float(self.get_symbol_spec()["point"])
            pip_size = float(self.get_symbol_spec()["pip_size"])
            stop_distance = max(
                float(self.get_symbol_spec()["stops_level"]) * point,
                pip_size * float(self.config.get("exit", {}).get("sl_min_pips", 18)),
                point * 50,
            )
            sample_request, _, _ = self.build_order_request(
                direction="LONG",
                volume=sample_volume,
                entry_price=tick_ask,
                sl=tick_ask - stop_distance,
                tp=tick_ask + (stop_distance * 2),
                comment="MT5_DIAGNOSTIC",
                type_filling=self.get_filling_mode(),
            )
            if sample_request is not None:
                report["sample_normalized_request"] = self._serialize_log_value(sample_request)
                order_check_stage = self.order_check_stage(sample_request)
                report["sample_order_check_ok"] = bool(order_check_stage["ok"])
                report["sample_order_check"] = self._serialize_log_value(order_check_stage["payload"].get("result"))
                report["sample_order_check_last_error"] = order_check_stage["payload"].get("last_error")
        report["execution_path"] = self.execution_path_summary()
        return report

    def modify_position_sltp(
        self,
        ticket: int | str,
        sl: float | None,
        tp: float | None,
        comment: str | None = None,
    ) -> tuple[bool, Any]:
        """Modify stop loss or take profit for an existing position."""
        position = self.get_position_by_ticket(ticket)
        if position is None:
            raise RuntimeError(f"Position not found for SL/TP modification: {ticket}")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(ticket),
            "sl": float(sl if sl is not None else position.sl),
            "tp": float(tp if tp is not None else position.tp),
            "magic": int(self.config["mt5"]["magic_number"]),
            "comment": str(comment or "MTF_Sniper_Bot_Modify")[:31],
        }
        result = self.safe_order_send(request, retry=False)
        if result is None:
            last_error = self._mt5_last_error()
            raise RuntimeError(f"modify order_send failed: {last_error['code']} {last_error['message']}")
        return int(result.retcode) == mt5.TRADE_RETCODE_DONE, result

    def close_partial(self, ticket: int | str, volume: float, comment: str | None = None) -> tuple[bool, Any]:
        """Close part of an open position."""
        position = self.get_position_by_ticket(ticket)
        if position is None:
            raise RuntimeError(f"Position not found for partial close: {ticket}")

        direction_is_buy = int(position.type) == mt5.POSITION_TYPE_BUY
        close_type = mt5.ORDER_TYPE_SELL if direction_is_buy else mt5.ORDER_TYPE_BUY
        tick_stage = self.tick_fetch_stage(refresh=True)
        if not tick_stage["ok"]:
            raise RuntimeError(f"Unable to fetch close tick: {tick_stage['reason_code']}")
        tick_payload = tick_stage["payload"].get("tick", {}) or {}
        price = float(tick_payload.get("bid", 0.0)) if direction_is_buy else float(tick_payload.get("ask", 0.0))
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(ticket),
            "volume": float(volume),
            "type": close_type,
            "price": normalize_price(price, int(self.get_symbol_spec()["digits"])),
            "deviation": int(self.config["mt5"]["deviation"]),
            "magic": int(self.config["mt5"]["magic_number"]),
            "comment": str(comment or "MTF_Sniper_Bot_TP1")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.choose_filling_mode(),
        }
        result = self.safe_order_send(request)
        return int(result.retcode) == mt5.TRADE_RETCODE_DONE, result

    def close_position(self, ticket: int | str, comment: str | None = None) -> tuple[bool, Any]:
        """Close a full position by ticket."""
        position = self.get_position_by_ticket(ticket)
        if position is None:
            return False, None
        return self.close_partial(ticket, float(position.volume), comment=comment)

    def close_all_bot_positions(self) -> None:
        """Close every bot-managed live position."""
        for position in self.get_bot_positions():
            success, result = self.close_position(int(position.ticket))
            if success:
                self.logger.trade_console(f"Closed position {position.ticket} for session end")
            else:
                self.logger.warning(f"Failed to close position {position.ticket}: {result}")

    def get_position_deals(self, ticket: int | str, opened_at: datetime | str | None) -> list[Any]:
        """Return historical deals associated with a specific position."""
        start_time = to_utc(opened_at) or (utc_now() - pd.Timedelta(days=7).to_pytimedelta())
        deals = self._call_mt5("history_deals_get", mt5.history_deals_get, start_time, utc_now()) or []
        return [deal for deal in deals if int(getattr(deal, "position_id", 0)) == int(ticket)]

    def get_position_history_bundle(
        self,
        ticket: int | str,
        opened_at: datetime | str | None,
        lookback_minutes: int = 1440,
    ) -> dict[str, list[Any]]:
        """Return both order and deal history for a position."""
        start_time = to_utc(opened_at) or (utc_now() - pd.Timedelta(minutes=int(lookback_minutes)).to_pytimedelta())
        orders = self._call_mt5("history_orders_get", mt5.history_orders_get, start_time, utc_now()) or []
        deals = self._call_mt5("history_deals_get", mt5.history_deals_get, start_time, utc_now()) or []
        position_id = int(ticket)
        return {
            "orders": [order for order in orders if int(getattr(order, "position_id", 0)) == position_id],
            "deals": [deal for deal in deals if int(getattr(deal, "position_id", 0)) == position_id],
        }

    def resolve_trade_close_history(
        self,
        ticket: int | str,
        opened_at: datetime | str | None,
        *,
        expected: dict[str, Any] | None = None,
        lookback_minutes: int = 1440,
    ) -> dict[str, Any]:
        """Resolve close-history evidence using staged MT5 matching with fallbacks."""
        expected_payload = dict(expected or {})
        start_time = to_utc(opened_at) or (utc_now() - pd.Timedelta(minutes=int(lookback_minutes)).to_pytimedelta())
        end_time = utc_now()
        orders = self._call_mt5("history_orders_get", mt5.history_orders_get, start_time, end_time) or []
        deals = self._call_mt5("history_deals_get", mt5.history_deals_get, start_time, end_time) or []

        expected_ticket = str(ticket or expected_payload.get("position_id") or "").strip()
        expected_order_id = str(expected_payload.get("order_id") or "").strip()
        expected_symbol = str(expected_payload.get("symbol") or self.symbol).strip().upper()
        expected_volume = float(expected_payload.get("expected_volume", 0.0) or 0.0)
        expected_close_at = to_utc(expected_payload.get("expected_close_at"))
        expected_entry = float(expected_payload.get("entry_price", 0.0) or 0.0)
        tolerance_points = float(expected_payload.get("entry_price_tolerance_points", 25.0) or 25.0)
        point_size = float(self.get_symbol_spec()["point"])
        price_tolerance = max(point_size, point_size * tolerance_points)

        def _ids(item: Any) -> set[str]:
            values = {
                str(getattr(item, "position_id", "") or "").strip(),
                str(getattr(item, "position", "") or "").strip(),
                str(getattr(item, "ticket", "") or "").strip(),
                str(getattr(item, "order", "") or "").strip(),
                str(getattr(item, "identifier", "") or "").strip(),
                str(getattr(item, "deal", "") or "").strip(),
            }
            return {value for value in values if value}

        def _symbol(item: Any) -> str:
            return str(getattr(item, "symbol", "") or "").strip().upper()

        def _volume(item: Any) -> float:
            try:
                return float(getattr(item, "volume", 0.0) or 0.0)
            except Exception:
                return 0.0

        def _price(item: Any) -> float:
            try:
                return float(getattr(item, "price", getattr(item, "price_current", getattr(item, "price_open", 0.0))) or 0.0)
            except Exception:
                return 0.0

        def _time(item: Any) -> datetime | None:
            for field in ("time", "time_done", "time_update"):
                value = getattr(item, field, None)
                converted = to_utc(value)
                if converted is not None:
                    return converted
            for field in ("time_msc", "time_done_msc", "time_update_msc"):
                value = getattr(item, field, None)
                if value:
                    converted = to_utc(float(value) / 1000.0)
                    if converted is not None:
                        return converted
            return None

        def _score(item: Any) -> float:
            score = 0.0
            item_ids = _ids(item)
            if expected_ticket and expected_ticket in item_ids:
                score += 100.0
            if expected_order_id and expected_order_id in item_ids:
                score += 90.0
            if expected_symbol and _symbol(item) == expected_symbol:
                score += 25.0
            item_volume = _volume(item)
            if expected_volume > 0 and item_volume > 0:
                if abs(item_volume - expected_volume) <= max(0.01, expected_volume * 0.05):
                    score += 18.0
                elif abs(item_volume - expected_volume) <= max(0.05, expected_volume * 0.35):
                    score += 9.0
            item_time = _time(item)
            if expected_close_at is not None and item_time is not None:
                delta_seconds = abs((item_time - expected_close_at).total_seconds())
                if delta_seconds <= 90:
                    score += 18.0
                elif delta_seconds <= 900:
                    score += 10.0
            item_price = _price(item)
            if expected_entry > 0 and item_price > 0 and abs(item_price - expected_entry) <= price_tolerance:
                score += 6.0
            return score

        exact_orders = [order for order in orders if expected_ticket and expected_ticket in _ids(order)]
        exact_deals = [deal for deal in deals if expected_ticket and expected_ticket in _ids(deal)]
        order_id_orders = [order for order in orders if expected_order_id and expected_order_id in _ids(order)]
        order_id_deals = [deal for deal in deals if expected_order_id and expected_order_id in _ids(deal)]

        fallback_orders = sorted(
            [order for order in orders if _symbol(order) == expected_symbol and _score(order) >= 25.0],
            key=_score,
            reverse=True,
        )
        fallback_deals = sorted(
            [deal for deal in deals if _symbol(deal) == expected_symbol and _score(deal) >= 25.0],
            key=_score,
            reverse=True,
        )

        matched_orders = exact_orders or order_id_orders or fallback_orders[:3]
        matched_deals = exact_deals or order_id_deals or fallback_deals[:3]
        close_deal = max(matched_deals, key=lambda item: _time(item) or start_time, default=None)
        matched_volume = sum(_volume(item) for item in matched_deals)
        top_confidence = max([_score(item) for item in [*matched_orders, *matched_deals]], default=0.0)

        stage = "CLOSE_HISTORY_PENDING"
        resolved = False
        reason = "history_unavailable"
        if close_deal is not None and top_confidence >= 60.0:
            stage = "FINALIZED"
            resolved = True
            reason = "matched_close_history"
        elif matched_orders or matched_deals:
            stage = "CLOSE_HISTORY_PARTIAL"
            reason = "partial_history_match"

        evidence = {
            "ticket": expected_ticket,
            "order_id": expected_order_id,
            "symbol": expected_symbol,
            "expected_volume": expected_volume,
            "matched_volume": matched_volume,
            "close_price": _price(close_deal) if close_deal is not None else None,
            "close_time": (_time(close_deal).isoformat() if close_deal is not None and _time(close_deal) is not None else None),
            "matched_order_ids": sorted({value for item in matched_orders for value in _ids(item)}),
            "matched_deal_ids": sorted({value for item in matched_deals for value in _ids(item)}),
            "confidence": top_confidence,
            "stage": stage,
            "orders": [self._serialize_log_value(order) for order in matched_orders],
            "deals": [self._serialize_log_value(deal) for deal in matched_deals],
            "window": {
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "order_count": len(orders),
                "deal_count": len(deals),
            },
            "broker_comment": str(
                getattr(close_deal, "comment", "")
                or getattr(close_deal, "reason", "")
                or (getattr(matched_orders[-1], "comment", "") if matched_orders else "")
            ),
        }
        return {
            "resolved": resolved,
            "status": stage,
            "reason": reason,
            "confidence": top_confidence,
            "orders": matched_orders,
            "deals": matched_deals,
            "close_deal": close_deal,
            "evidence": evidence,
        }
