from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class BetStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_WIN = "CLOSED_WIN"
    CLOSED_LOSS = "CLOSED_LOSS"
    CLOSED_MANUAL = "CLOSED_MANUAL"


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # polymarket condition_id
    slug: Mapped[str] = mapped_column(String, index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String, index=True)
    end_date: Mapped[datetime | None] = mapped_column(index=True)
    resolved: Mapped[bool] = mapped_column(default=False, index=True)
    resolution: Mapped[str | None] = mapped_column(String)
    tokens: Mapped[dict] = mapped_column(JSONB, default=dict)  # {YES: token_id, NO: token_id}
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


class PriceTick(Base):
    __tablename__ = "price_ticks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    yes_bid: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    yes_ask: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    yes_mid: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    volume_24h: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    liquidity: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    __table_args__ = (Index("ix_price_ticks_market_ts", "market_id", "ts"),)


class Event(Base):
    """Raw ingested information from any channel."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, index=True)  # newsapi, twitter, reddit, onchain...
    source_id: Mapped[str | None] = mapped_column(String, index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    entities: Mapped[list] = mapped_column(JSONB, default=list)
    embedding: Mapped[list | None] = mapped_column(JSON)  # pgvector later
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_event_source_id"),)

    signals: Mapped[list["Signal"]] = relationship(back_populates="event")


class Signal(Base):
    """Interpretation of an event toward a specific market."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    strategy: Mapped[str] = mapped_column(String, index=True)
    direction: Mapped[Outcome]  # which outcome this signal supports
    edge_bps: Mapped[int]  # estimated edge in basis points
    confidence: Mapped[float] = mapped_column(Numeric(4, 3))
    reasoning: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)

    event: Mapped[Event] = relationship(back_populates="signals")


class WhaleWallet(Base):
    __tablename__ = "whale_wallets"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str | None] = mapped_column(String)
    total_pnl_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    sharpe_estimate: Mapped[float | None]
    trades_count: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    last_seen: Mapped[datetime | None]
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class WhaleTrade(Base):
    __tablename__ = "whale_trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("whale_wallets.address"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    side: Mapped[OrderSide]
    outcome: Mapped[Outcome]
    size_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    price: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    tx_hash: Mapped[str] = mapped_column(String, unique=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String, unique=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    strategy: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[OrderSide]
    outcome: Mapped[Outcome]
    size_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    filled_size: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class Bet(Base):
    """An open or closed position. Aggregates orders on the same market+outcome."""

    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    strategy: Mapped[str] = mapped_column(String, index=True)
    outcome: Mapped[Outcome]
    cost_basis_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    size_shares: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    edge_bps_at_entry: Mapped[int]
    confidence_at_entry: Mapped[float] = mapped_column(Numeric(4, 3))
    status: Mapped[BetStatus] = mapped_column(default=BetStatus.OPEN, index=True)
    pnl_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    opened_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    closed_at: Mapped[datetime | None]
    reasoning: Mapped[str | None] = mapped_column(Text)


class BankrollSnapshot(Base):
    __tablename__ = "bankroll_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    cash_usdc: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    open_positions_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    total_equity: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class CircuitBreakerState(Base):
    __tablename__ = "circuit_breakers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    tripped: Mapped[bool] = mapped_column(default=False)
    tripped_at: Mapped[datetime | None]
    cleared_at: Mapped[datetime | None]
    reason: Mapped[str | None] = mapped_column(Text)
