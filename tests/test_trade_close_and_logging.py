import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from logger import SafeRotatingFileHandler
from services.database import DatabaseService


def test_trade_close_with_full_history_timestamp_writes_valid_timestamp():
    with TemporaryDirectory() as tmp:
        db = DatabaseService(Path(tmp) / "bot.db")
        db.record_trade_close(
            {
                "timestamp": "2026-04-10T12:00:00+00:00",
                "trade_id": "T100",
                "mt5_ticket": "T100",
                "position_id": "T100",
                "symbol": "XAUUSD",
                "side": "SHORT",
                "status": "FINALIZED",
                "event_type": "TRADE_FINALIZED",
            }
        )
        row = db.get_trade_by_identity(trade_id="T100")
        assert row is not None
        assert str(row.get("timestamp") or "").strip() != ""


def test_trade_close_missing_history_uses_fallback_timestamp_without_crash():
    with TemporaryDirectory() as tmp:
        db = DatabaseService(Path(tmp) / "bot.db")
        db.record_trade_close(
            {
                "trade_id": "T101",
                "mt5_ticket": "T101",
                "position_id": "T101",
                "symbol": "XAUUSD",
                "side": "LONG",
                "status": "PENDING_CLOSE_RESOLUTION",
                "event_type": "TRADE_CLOSED",
            }
        )
        row = db.get_trade_by_identity(trade_id="T101")
        assert row is not None
        assert str(row.get("timestamp") or "").strip() != ""


def test_pending_resolution_row_has_valid_timestamp():
    with TemporaryDirectory() as tmp:
        db = DatabaseService(Path(tmp) / "bot.db")
        db.mark_trade_pending_resolution(
            {
                "trade_id": "T102",
                "mt5_ticket": "T102",
                "position_id": "T102",
                "symbol": "XAUUSD",
                "side": "LONG",
                "unresolved_reason": "history_unavailable",
            }
        )
        row = db.get_trade_by_identity(trade_id="T102")
        assert row is not None
        assert str(row.get("timestamp") or "").strip() != ""
        assert str(row.get("status") or "") == "PENDING_CLOSE_RESOLUTION"


def test_logger_rollover_permission_error_does_not_raise():
    class FailingRolloverHandler(SafeRotatingFileHandler):
        def doRollover(self) -> None:  # pragma: no cover - this is the simulated failure
            raise PermissionError("simulated WinError 32")

    with TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "bot.log"
        handler = FailingRolloverHandler(log_path, maxBytes=1, backupCount=1, delay=True, encoding="utf-8")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="this should not crash runtime",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.close()
