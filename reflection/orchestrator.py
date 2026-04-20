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
    bt_before = await replay("current", now - timedelta(days=14), now - timedelta(days=7), Decimal(100))
    adjustments = await adapter.apply(diagnosis)
    bt_after = await replay("adapted", now - timedelta(days=14), now - timedelta(days=7), Decimal(100))

    # Decide whether to resume. Conservative: only resume if adapted > current.
    improved = bt_after.sharpe or 0 > (bt_before.sharpe or 0) and bt_after.total_pnl_usdc > bt_before.total_pnl_usdc
    resumed = bool(improved)
    if resumed:
        await clear(REFLECTION_BREAKER)

    async with session_scope() as db:
        run = ReflectionRun(
            triggered_at=now,
            trigger_reason="; ".join(decision.reasons),
            diagnosis=diagnosis,
            adjustments=adjustments,
            backtest_before=bt_before.__dict__,
            backtest_after=bt_after.__dict__,
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
        f"resumed: {resumed}"
    )

    _state.stressed = True
    _state.last_trigger_at = now
    _state.disabled_strategies = set(adjustments.get("disabled_strategies", []))
    return _state
