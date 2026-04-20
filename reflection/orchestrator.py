"""End-to-end reflection workflow. Call `maybe_reflect()` each tick from the
order manager; it returns a ReflectionState the manager uses to adjust behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, select

from bot.alerts import send_alert
from reflection import adapter, drawdown_trigger, retrospective, strategy_scorer
from risk.circuit_breaker import clear, trip
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import ReflectionRun
from strategy.backtest import replay

log = get_logger(__name__)

REFLECTION_BREAKER = "reflection_active"
_STRESS_COOLDOWN_HOURS = 24


@dataclass
class ReflectionState:
    stressed: bool = False
    last_trigger_at: datetime | None = None
    disabled_strategies: set[str] = field(default_factory=set)


_state = ReflectionState()


async def current_state() -> ReflectionState:
    """Refresh cached state from DB so a restart still remembers stress."""
    async with session_scope() as db:
        last_run = (await db.execute(
            select(ReflectionRun).order_by(desc(ReflectionRun.triggered_at)).limit(1)
        )).scalar_one_or_none()
    if not last_run:
        _state.stressed = False
        _state.last_trigger_at = None
        return _state
    _state.last_trigger_at = last_run.triggered_at
    cooldown = timedelta(hours=_STRESS_COOLDOWN_HOURS)
    _state.stressed = (
        not last_run.resumed
        or (datetime.utcnow() - last_run.triggered_at) < cooldown
    )
    return _state


async def maybe_reflect() -> ReflectionState:
    """Check triggers; if fired, run the full reflection cycle."""
    decision = await drawdown_trigger.evaluate()
    if not decision.fire:
        return await current_state()

    log.warning("reflection.triggered", reasons=decision.reasons, **decision.stats)
    await trip(REFLECTION_BREAKER, "; ".join(decision.reasons))
    await send_alert(
        f"*reflection triggered*\n" + "\n".join(f"- {r}" for r in decision.reasons) +
        "\ntrading halted; diagnosing...",
    )

    await strategy_scorer.recompute_all()
    diagnosis = await retrospective.analyze_recent_losses()

    now = datetime.utcnow()
    window_start = now - timedelta(days=14)
    window_end = now - timedelta(hours=1)  # exclude bets we just opened

    bt_before = await replay("current", window_start, window_end, Decimal(100))
    adjustments = await adapter.apply(diagnosis)
    bt_after = await replay("adapted", window_start, window_end, Decimal(100))

    # Conservative resume: adapted must beat current on BOTH Sharpe and PnL.
    sharpe_before = float(bt_before.sharpe or 0)
    sharpe_after = float(bt_after.sharpe or 0)
    pnl_before = bt_before.total_pnl_usdc
    pnl_after = bt_after.total_pnl_usdc
    resumed = (sharpe_after > sharpe_before) and (pnl_after > pnl_before)
    if resumed:
        await clear(REFLECTION_BREAKER)

    async with session_scope() as db:
        run = ReflectionRun(
            triggered_at=now,
            trigger_reason="; ".join(decision.reasons),
            diagnosis=diagnosis,
            adjustments=adjustments,
            backtest_before=bt_before.as_dict(),
            backtest_after=bt_after.as_dict(),
            resumed=resumed,
            resumed_at=now if resumed else None,
            notes=None,
        )
        db.add(run)

    await send_alert(
        f"*reflection complete*\n"
        f"disabled: {adjustments.get('disabled_strategies') or 'none'}\n"
        f"sources boosted: {adjustments.get('sources_boosted', 0)}\n"
        f"sources penalized: {adjustments.get('sources_penalized', 0)}\n"
        f"backtest before: pnl={pnl_before:.2f} sharpe={sharpe_before:.2f} "
        f"trades={bt_before.total_trades}\n"
        f"backtest after:  pnl={pnl_after:.2f} sharpe={sharpe_after:.2f} "
        f"trades={bt_after.total_trades}\n"
        f"resumed: {resumed}"
    )

    _state.stressed = True
    _state.last_trigger_at = now
    _state.disabled_strategies = set(adjustments.get("disabled_strategies", []))
    return _state
