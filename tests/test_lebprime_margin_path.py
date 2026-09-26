from __future__ import annotations

from types import SimpleNamespace

import pytest

import mt5_connector as connector_module
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from services.lebprime_live.service import LebPrimeLiveService


class _Logger:
    def structured(self, *args, **kwargs):
        return None


def _manager(**risk_overrides) -> RiskManager:
    manager = RiskManager.__new__(RiskManager)
    manager.logger = _Logger()
    manager.config = {
        "risk": {
            "min_lot": 0.05,
            "min_practical_volume": 0.01,
            "max_lot": 2.0,
            "min_margin_level": 300.0,
            "min_free_margin_buffer": 0.0,
            "min_fitted_volume_ratio": 0.0,
            "margin_protection_enabled": True,
            **risk_overrides,
        }
    }
    return manager


def _account(*, equity=10_000.0, margin=0.0, margin_free=10_000.0):
    return SimpleNamespace(equity=equity, margin=margin, margin_free=margin_free, margin_level=0.0)


def _spec(**overrides):
    return {"volume_min": 0.01, "volume_max": 2.0, "volume_step": 0.01, **overrides}


def test_margin_succeeds_at_requested_volume():
    result = _manager().fit_volume_to_margin(0.50, _account(), _spec(), lambda volume: volume * 100)
    assert result["valid"] is True
    assert result["volume"] == pytest.approx(0.50)
    assert result["required_margin"] == pytest.approx(50.0)
    assert result["fit_candidates"][0]["accepted"] is True


def test_margin_succeeds_after_reducing_volume():
    result = _manager().fit_volume_to_margin(
        0.50,
        _account(margin_free=20.0),
        _spec(),
        lambda volume: volume * 100,
    )
    assert result["valid"] is True
    assert result["volume"] == pytest.approx(0.20)
    assert result["adjusted"] is True
    assert result["required_margin"] == pytest.approx(20.0)
    assert any(row["rejection_reason"] == "MARGIN_FREE_INSUFFICIENT" for row in result["fit_candidates"][:-1])


def test_broker_minimum_larger_than_permitted_maximum_is_permanent_spec_failure():
    result = _manager(max_lot=0.10).fit_volume_to_margin(
        0.50,
        _account(),
        _spec(volume_min=0.20),
        lambda volume: volume * 100,
    )
    assert result["valid"] is False
    assert result["reason_code"] == "BROKER_MIN_VOLUME_TOO_LARGE"
    assert LebPrimeLiveService._retryable_failure(result["reason_code"]) is False


def test_projected_margin_level_failure_is_exact_and_retryable():
    result = _manager().fit_volume_to_margin(
        0.05,
        _account(equity=1_000.0, margin=330.0, margin_free=670.0),
        _spec(),
        lambda volume: 5.0,
    )
    assert result["valid"] is False
    assert result["reason_code"] == "PROJECTED_MARGIN_LEVEL_TOO_LOW"
    assert result["fit_candidates"][-1]["projected_margin_level"] < 300.0
    assert LebPrimeLiveService._retryable_failure(result["reason_code"]) is True


def test_insufficient_free_margin_is_exact_and_retryable():
    result = _manager().fit_volume_to_margin(
        0.05,
        _account(margin_free=4.0),
        _spec(),
        lambda volume: 5.0,
    )
    assert result["valid"] is False
    assert result["reason_code"] == "MARGIN_FREE_INSUFFICIENT"
    assert result["fit_candidates"][-1]["free_margin_after"] == pytest.approx(-1.0)
    assert LebPrimeLiveService._retryable_failure(result["reason_code"]) is True


def test_order_calc_margin_failure_is_exact_and_retryable():
    def broken(_volume):
        raise RuntimeError("terminal temporarily unavailable")

    result = _manager().fit_volume_to_margin(0.05, _account(), _spec(), broken)
    assert result["valid"] is False
    assert result["reason_code"] == "ORDER_CALC_MARGIN_FAILED"
    assert "terminal temporarily unavailable" in result["fit_candidates"][0]["order_calc_margin_error"]
    assert LebPrimeLiveService._retryable_failure(result["reason_code"]) is True


@pytest.mark.parametrize(
    ("pending_type", "expected_side"),
    [
        (connector_module.mt5.ORDER_TYPE_BUY_LIMIT, connector_module.mt5.ORDER_TYPE_BUY),
        (connector_module.mt5.ORDER_TYPE_SELL_LIMIT, connector_module.mt5.ORDER_TYPE_SELL),
    ],
)
def test_pending_margin_calculation_uses_buy_or_sell_position_side(pending_type, expected_side):
    connector = MT5Connector.__new__(MT5Connector)
    connector.symbol = "TEST"
    connector.logger = _Logger()
    captured = []

    def fake_call(label, function, *args):
        captured.append((label, function, args))
        return 12.5

    connector._call_mt5 = fake_call
    assert connector.calculate_margin(pending_type, 0.05, 100.0) == pytest.approx(12.5)
    assert captured[0][2][0] == expected_side
    assert captured[0][2][1:] == ("TEST", 0.05, 100.0)


def test_invalid_volume_and_symbol_spec_are_permanent():
    assert LebPrimeLiveService._retryable_failure("VOLUME_BELOW_MIN") is False
    assert LebPrimeLiveService._retryable_failure("INVALID_SYMBOL_SPEC") is False
    assert LebPrimeLiveService._retryable_failure("ORDER_CALC_MARGIN_FAILED") is True
    assert LebPrimeLiveService._retryable_failure("MARGIN_FREE_INSUFFICIENT", 10019) is True
    assert LebPrimeLiveService._retryable_failure("INVALID_PENDING_REQUEST:Order blocked: SL too close") is False
