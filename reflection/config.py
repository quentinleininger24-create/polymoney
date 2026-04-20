"""Thresholds that govern when reflection kicks in and how aggressive it is."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReflectionConfig:
    # Trigger conditions (any one fires reflection)
    consecutive_losses_trigger: int = 5
    rolling_winrate_window: int = 20
    rolling_winrate_floor: float = 0.35
    drawdown_from_7d_peak_pct: float = 0.15

    # Strategy-level kill switch
    strategy_consecutive_losses: int = 6
    strategy_rolling_winrate_floor: float = 0.30
    strategy_rolling_window: int = 15

    # Confluence requirements (overridden up when reflection detects trouble)
    base_min_distinct_sources: int = 1    # default: LLM alone is enough
    stressed_min_distinct_sources: int = 2  # after a bad streak: require corroboration
    confluence_window_minutes: int = 45

    # Source scoring
    min_signals_to_weight: int = 5           # below this, source gets default weight
    weight_floor: float = 0.1                 # never zero out entirely (stay observable)
    weight_ceiling: float = 2.0               # cap on boosting
    lead_time_bonus_per_hour: float = 0.1    # sources that fire earlier get a bonus

    # Backtest comparison thresholds (post-adaptation must beat pre)
    min_sharpe_improvement: float = 0.2
    min_winrate_improvement: float = 0.05


config = ReflectionConfig()
