from services.execution_flow import normalize_final_outcome, resolve_spread_limit, should_mark_duplicate_or_cooldown


def test_setup_score_below_threshold_maps_to_risk_rejected_default():
    assert normalize_final_outcome("setup_score_below_threshold") == "risk_rejected"


def test_spread_too_wide_is_final_and_does_not_consume_duplicate_state():
    assert normalize_final_outcome("spread_too_wide") == "spread_too_wide"
    assert should_mark_duplicate_or_cooldown("precheck_failed") is False


def test_spread_limit_resolution_prefers_setup_then_dynamic():
    limit, source = resolve_spread_limit(
        execution_cfg={
            "spread": {
                "max_points": {"default": 25.0},
                "max_points_by_setup": {"compression_release": 28.0},
                "max_points_by_regime": {"HIGH_VOLATILITY_BREAKOUT": 30.0},
                "enable_dynamic_limit": True,
            }
        },
        setup_name="compression_release",
        regime_name="HIGH_VOLATILITY_BREAKOUT",
        fallback_limit=25.0,
    )
    assert source == "dynamic"
    assert limit >= 25.0


def test_margin_insufficient_mapping():
    assert normalize_final_outcome("blocked_insufficient_margin") == "margin_insufficient"


def test_order_send_rejected_mapping():
    assert normalize_final_outcome(
        "order_send_failed",
        failure_class="broker_rejection",
        execution_state="send_rejected",
        order_send_attempted=True,
    ) == "order_send_rejected"


def test_successful_send_state_applies_guards():
    assert should_mark_duplicate_or_cooldown("send_accepted") is True


def test_exception_before_send_mapping():
    assert normalize_final_outcome("exception_before_send") == "exception_before_send"


def test_exception_after_send_unknown_state_mapping():
    assert normalize_final_outcome("exception_after_send_unknown_state") == "exception_after_send_unknown_state"


def test_broker_requote_reason_mapping():
    assert normalize_final_outcome("broker_requote_or_price_change") == "broker_requote_or_price_change"


def test_precheck_blocked_expanded_trigger_preserved():
    assert normalize_final_outcome(
        "blocked_expanded_trigger",
        execution_state="precheck_failed",
        order_send_attempted=False,
    ) == "blocked_expanded_trigger"


def test_precheck_position_exists_preserved():
    assert normalize_final_outcome(
        "position_exists",
        execution_state="precheck_failed",
        order_send_attempted=False,
    ) == "position_exists"
