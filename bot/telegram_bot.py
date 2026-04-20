"""Telegram bot: your phone-side control panel.

Commands:
  /status        - bankroll, P&L, top open positions
  /positions     - full position list
  /signals       - recent signals (even if they didn't trigger a trade)
  /panic         - trip manual circuit breaker (stops all trading)
  /resume        - clear manual circuit breaker
  /set <key> <v> - runtime tweaks (e.g. min_confidence 0.7)
"""

from decimal import Decimal

from sqlalchemy import desc, func, select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from risk.circuit_breaker import MANUAL_PANIC, clear, trip
from risk.position_sizing import compute_bankroll
from shared.config import settings
from shared.db import session_scope
from shared.logging import configure_logging, get_logger
from shared.models import Bet, BetStatus, Signal

log = get_logger(__name__)


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cash = await compute_bankroll()
    async with session_scope() as db:
        open_n = (await db.execute(
            select(func.count()).select_from(Bet).where(Bet.status == BetStatus.OPEN)
        )).scalar_one()
        realized = (await db.execute(
            select(func.coalesce(func.sum(Bet.pnl_usdc), 0))
            .where(Bet.status != BetStatus.OPEN)
        )).scalar_one()
    await update.message.reply_text(
        f"*Polymoney*\n"
        f"Mode: `{settings.mode.value}`\n"
        f"Cash: `{cash:.2f}` USDC\n"
        f"Open positions: `{open_n}`\n"
        f"Realized PnL: `{Decimal(realized):.2f}` USDC\n"
        f"Bankroll: `{settings.initial_bankroll_usdc:.2f}` USDC",
        parse_mode="Markdown",
    )


async def cmd_positions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Bet).where(Bet.status == BetStatus.OPEN).order_by(desc(Bet.opened_at)).limit(10)
        )).scalars().all()
    if not rows:
        await update.message.reply_text("No open positions.")
        return
    lines = [
        f"`{b.market_id[:10]}` {b.outcome.value} @{b.entry_price} = {b.cost_basis_usdc} ({b.strategy})"
        for b in rows
    ]
    await update.message.reply_text("*Open positions*\n" + "\n".join(lines), parse_mode="Markdown")


async def cmd_signals(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Signal).order_by(desc(Signal.ts)).limit(10)
        )).scalars().all()
    if not rows:
        await update.message.reply_text("No signals yet.")
        return
    lines = [
        f"`{s.market_id[:10]}` {s.direction.value} edge={s.edge_bps}bps conf={s.confidence:.2f} [{s.strategy}]"
        for s in rows
    ]
    await update.message.reply_text("*Recent signals*\n" + "\n".join(lines), parse_mode="Markdown")


async def cmd_panic(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await trip(MANUAL_PANIC, "user invoked /panic")
    await update.message.reply_text("Trading halted. /resume to clear.")


async def cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await clear(MANUAL_PANIC)
    await update.message.reply_text("Resumed.")


def main() -> None:
    configure_logging()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("panic", cmd_panic))
    app.add_handler(CommandHandler("resume", cmd_resume))
    log.info("telegram.polling")
    app.run_polling()


if __name__ == "__main__":
    main()
