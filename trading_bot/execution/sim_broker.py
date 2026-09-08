from __future__ import annotations

from trading_bot.execution.replay_broker import ReplayBroker


class SimBroker(ReplayBroker):
    """Dry-run/demo simulated broker with the same order semantics as replay."""

    mode = "DRY_RUN"

