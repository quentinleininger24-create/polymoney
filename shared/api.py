"""FastAPI backend powering the dashboard."""

from decimal import Decimal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import desc, func, select

from ingestion.polymarket import PolymarketReader
from risk.position_sizing import compute_bankroll
from shared.config import settings
from shared.db import session_scope
from shared.models import Bet, BetStatus, Market, Signal

app = FastAPI(title="polymoney")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/status")
async def status() -> dict:
    cash = await compute_bankroll()
    async with session_scope() as db:
        open_n = (await db.execute(
            select(func.count()).select_from(Bet).where(Bet.status == BetStatus.OPEN)
        )).scalar_one()
        realized = (await db.execute(
            select(func.coalesce(func.sum(Bet.pnl_usdc), 0))
            .where(Bet.status != BetStatus.OPEN)
        )).scalar_one()
    return {
        "mode": settings.mode.value,
        "cash_usdc": float(cash),
        "open_positions": int(open_n),
        "realized_pnl": float(Decimal(realized)),
        "unrealized_pnl": 0.0,
        "bankroll": float(settings.initial_bankroll_usdc),
    }


@app.get("/positions")
async def positions() -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Bet, Market)
            .join(Market, Bet.market_id == Market.id)
            .where(Bet.status == BetStatus.OPEN)
            .order_by(desc(Bet.opened_at))
        )).all()
    reader = PolymarketReader()
    out: list[dict] = []
    try:
        for bet, market in rows:
            yes_token = (market.tokens or {}).get("YES")
            current = None
            if yes_token:
                try:
                    mid = await reader.get_midpoint(yes_token)
                    current = float(mid) if mid else None
                except Exception:  # noqa: BLE001
                    current = None
            price_for_side = (
                current if bet.outcome.value == "YES" else (1 - current if current is not None else None)
            )
            upnl = 0.0
            if price_for_side is not None:
                upnl = float((Decimal(str(price_for_side)) - bet.entry_price) * bet.size_shares)
            out.append({
                "market_id": bet.market_id,
                "question": market.question,
                "outcome": bet.outcome.value,
                "strategy": bet.strategy,
                "entry_price": float(bet.entry_price),
                "cost_basis_usdc": float(bet.cost_basis_usdc),
                "current_price": price_for_side,
                "unrealized_pnl": upnl,
                "reasoning": bet.reasoning,
            })
    finally:
        await reader.close()
    return out


@app.get("/signals")
async def signals_endpoint(limit: int = 50) -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Signal).order_by(desc(Signal.ts)).limit(limit)
        )).scalars().all()
    return [
        {
            "market_id": s.market_id,
            "strategy": s.strategy,
            "direction": s.direction.value,
            "edge_bps": s.edge_bps,
            "confidence": float(s.confidence),
            "reasoning": s.reasoning,
            "ts": s.ts.isoformat(),
        }
        for s in rows
    ]
