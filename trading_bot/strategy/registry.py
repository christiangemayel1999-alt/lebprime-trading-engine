from __future__ import annotations

from collections.abc import Iterable

from trading_bot.strategy.base import BaseStrategy


class StrategyRegistry:
    """Central registry for every concrete strategy implementation."""

    def __init__(self) -> None:
        self._strategies: dict[str, BaseStrategy] = {}
        self._family_to_strategy: dict[str, str] = {}

    def register(self, strategy: BaseStrategy) -> None:
        self._strategies[strategy.strategy_name] = strategy
        for family in strategy.setup_families:
            self._family_to_strategy[str(family).upper()] = strategy.strategy_name

    def all(self) -> list[BaseStrategy]:
        return list(self._strategies.values())

    def get_for_family(self, family: str) -> BaseStrategy | None:
        strategy_name = self._family_to_strategy.get(str(family).upper())
        return self._strategies.get(strategy_name or "")

    def strategy_names(self) -> list[str]:
        return sorted(self._strategies)

    def families(self) -> set[str]:
        return set(self._family_to_strategy)

    def enabled(self, selected_families: Iterable[str] | None = None) -> list[BaseStrategy]:
        if selected_families is None:
            return self.all()
        requested = {str(item).upper() for item in selected_families}
        return [strategy for strategy in self.all() if strategy.setup_families & requested]


def build_default_registry(config: dict) -> StrategyRegistry:
    from trading_bot.strategy.adapters import LebprimStrategyAdapter, LegacyStrategyAdapter

    registry = StrategyRegistry()
    registry.register(LegacyStrategyAdapter(config))
    registry.register(LebprimStrategyAdapter(config))
    return registry

