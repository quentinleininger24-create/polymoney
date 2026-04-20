"""Kelly criterion for binary prediction markets."""

from decimal import Decimal


def kelly_fraction(edge_prob: float, current_price: float) -> float:
    """Kelly fraction for a binary contract priced in [0,1].

    Args:
        edge_prob: our estimated true probability of YES.
        current_price: current YES price on the market.

    Returns: fraction of bankroll to allocate. Clamped to [0, 1]. Negative edge -> 0.
    """
    if current_price <= 0 or current_price >= 1:
        return 0.0
    if edge_prob <= current_price:
        return 0.0
    # Binary contract: payoff = (1 / price) per $1, so:
    # f* = p - (1-p)/b where b = (1/price - 1) = (1-price)/price
    p = edge_prob
    b = (1 - current_price) / current_price
    q = 1 - p
    f = (b * p - q) / b
    return max(0.0, min(1.0, f))


def sized_bet_usdc(
    edge_prob: float,
    current_price: float,
    bankroll_usdc: Decimal,
    kelly_multiplier: float = 0.33,
    max_pct: float = 0.05,
) -> Decimal:
    """Return USDC amount to bet. Combines Kelly with global caps."""
    k = kelly_fraction(edge_prob, current_price) * kelly_multiplier
    k = min(k, max_pct)
    return (bankroll_usdc * Decimal(str(k))).quantize(Decimal("0.01"))


def edge_prob_from_bps(current_price: float, edge_bps: int) -> float:
    """Convert an edge-in-basis-points signal back into an implied true probability."""
    return max(0.0, min(1.0, current_price + edge_bps / 10000))
