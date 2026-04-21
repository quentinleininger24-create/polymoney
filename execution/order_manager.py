"""Order manager: runs the trade loop.

Cycle:
1. Check circuit breakers.
2. Collect TradeIntents from each strategy.
3. Dedupe and size via risk layer.
4. Submit to Polymarket (paper or live).
5. Persist Bet rows and send Telegram alerts.
"""

import asyncio
from decimal import Decimal

from sqlalchemy import select

from execution.polymarket_client import PolymarketExecutor
from ingestion.polymarket import PolymarketReader
from reflection import confluence, orchestrator, strategy_scorer
from risk.circuit_breaker import any_tripped, check_daily_drawdown
from risk.position_sizing import size_intent
from shared.db import session_scope
from shared.logging import configure_logging, get_logger
from shared.models import Bet, BetStatus, Market, Order, OrderSide, OrderStatus
from strategy.base import Strategy, TradeIntent
from strategy.llm_conviction import LLMConvictionStrategy
from strategy.news_arbitrage import NewsArbitrageStrategy
from strategy.smart_flow import SmartFlowStrategy
from strategy.smart_whale import SmartWhaleStrategy
from strategy.whale_copy import WhaleCopyStrategy

log = get_logger(__name__)


class OrderManager:
    def __init__(self, strategies: list[Strategy] | None = None) -> None:
        self.strategies = strategies or [
            SmartWhaleStrategy(),   # per-whale trigger, 60 pct allocation
            SmartFlowStrategy(),    # cumulative-flow dominance, 40 pct allocation
            LLMConvictionStrategy(),
            WhaleCopyStrategy(),
            NewsArbitrageStrategy(),
        ]
        self.executor = PolymarketExecutor()
        self.reader = PolymarketReader()

    async def tick(self) -> None:
        await check_daily_drawdown()

        # Reflection loop: rescore strategies, maybe trigger a self-review.
        await strategy_scorer.recompute_all()
        reflection_state = await orchestrator.maybe_reflect()

        tripped, name = await any_tripped()
        if tripped:
            log.warning("trade.skipped_circuit_breaker", breaker=name)
            return

        enabled_strats = await strategy_scorer.enabled_strategies()

        all_intents: list[TradeIntent] = []
        for strat in self.strategies:
            # Skip strategies the scorer has disabled (either unseen yet -> allow, or explicitly off)
            if enabled_strats and strat.name not in enabled_strats:
                continue
            try:
                intents = await strat.generate_intents()
                all_intents.extend(intents)
            except Exception as e:  # noqa: BLE001
                log.error("strategy.failed", strategy=strat.name, err=str(e))

        # Dedupe: same market+outcome -> keep highest confidence
        best: dict[tuple[str, str], TradeIntent] = {}
        for i in all_intents:
            k = (i.market_id, i.outcome.value)
            if k not in best or i.confidence > best[k].confidence:
                best[k] = i

        for intent in best.values():
            # Confluence gate
            passes, supporting = await confluence.has_confluence(
                intent.market_id, intent.outcome, stressed=reflection_state.stressed
            )
            if not passes:
                log.info("trade.skipped_confluence",
                         market=intent.market_id[:10],
                         supporting=list(supporting),
                         stressed=reflection_state.stressed)
                continue
            await self._execute_intent(intent)

    async def _execute_intent(self, intent: TradeIntent) -> None:
        async with session_scope() as db:
            market = (await db.execute(
                select(Market).where(Market.id == intent.market_id)
            )).scalar_one_or_none()
        if not market:
            log.warning("trade.market_missing", id=intent.market_id)
            return

        token_id = self.executor.token_for_outcome(market.tokens, intent.outcome)
        if not token_id:
            return

        try:
            yes_price = await self.reader.get_midpoint(market.tokens.get("YES"))
        except Exception:  # noqa: BLE001
            yes_price = None
        if not yes_price:
            log.warning("trade.no_price", market=market.id)
            return

        size_usdc = await size_intent(
            market_id=intent.market_id,
            outcome=intent.outcome,
            edge_bps=intent.edge_bps,
            confidence=intent.confidence,
            current_yes_price=float(yes_price),
        )
        if intent.max_size_usdc is not None:
            size_usdc = min(size_usdc, intent.max_size_usdc)
        if size_usdc <= Decimal("1"):
            return

        price_for_side = yes_price if intent.outcome.value == "YES" else Decimal("1") - yes_price
        shares = self.executor.shares_from_usdc(size_usdc, price_for_side)
        resp = await self.executor.place_limit(token_id, OrderSide.BUY, shares, price_for_side)

        async with session_scope() as db:
            order = Order(
                external_id=resp.get("orderID"),
                market_id=intent.market_id,
                strategy=intent.strategy,
                side=OrderSide.BUY,
                outcome=intent.outcome,
                size_usdc=size_usdc,
                limit_price=price_for_side,
                filled_price=price_for_side,
                filled_size=size_usdc,
                status=OrderStatus.FILLED if resp.get("success") else OrderStatus.REJECTED,
                meta=resp,
            )
            db.add(order)
            if resp.get("success"):
                db.add(Bet(
                    market_id=intent.market_id,
                    strategy=intent.strategy,
                    outcome=intent.outcome,
                    cost_basis_usdc=size_usdc,
                    size_shares=shares,
                    entry_price=price_for_side,
                    edge_bps_at_entry=intent.edge_bps,
                    confidence_at_entry=intent.confidence,
                    status=BetStatus.OPEN,
                    reasoning=intent.reasoning,
                ))

        log.info("trade.placed",
                 strategy=intent.strategy,
                 market=intent.market_id[:10],
                 outcome=intent.outcome.value,
                 usdc=str(size_usdc),
                 price=str(price_for_side),
                 conf=intent.confidence)


async def main() -> None:
    configure_logging()
    mgr = OrderManager()
    log.info("order_manager.started")
    while True:
        try:
            await mgr.tick()
        except Exception as e:  # noqa: BLE001
            log.error("tick.crashed", err=str(e))
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
