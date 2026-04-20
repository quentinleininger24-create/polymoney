"""Claude-powered analyst: event + market context -> structured signal.

Cost strategy for small bankroll:
- Haiku first pass: filter events that look political-relevant and market-relevant.
- Sonnet second pass: only for high-relevance events, with prompt caching on the
  market context block (system prompt + market list stays stable for hours).
"""

import json
from decimal import Decimal

from anthropic import AsyncAnthropic

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None

SYSTEM_PROMPT = """You are a political betting analyst for Polymarket. You receive a news/social event and a list of active political markets. Your job: decide if the event materially changes the probability of any market outcome.

Output strict JSON, nothing else:
{
  "signals": [
    {
      "market_id": "<condition_id>",
      "direction": "YES" | "NO",
      "edge_bps": <int, estimated edge in basis points vs current price, -1000 to +1000>,
      "confidence": <float 0..1>,
      "reasoning": "<one sentence>"
    }
  ]
}

Rules:
- If the event has no impact, return {"signals": []}.
- edge_bps is how many basis points you think the TRUE probability differs from the CURRENT price shown.
- Be conservative. Only emit signals with confidence > 0.6 and abs(edge_bps) > 200.
- Never invent markets that are not in the provided list.
"""


async def triage_event_is_relevant(event_text: str) -> bool:
    """Cheap Haiku filter. Returns True if event looks politically market-relevant."""
    if not _client:
        return False
    resp = await _client.messages.create(
        model=settings.claude_model_fast,
        max_tokens=10,
        system="Answer only YES or NO. Is this text about US politics, elections, policy, or a political figure that could affect a prediction market?",
        messages=[{"role": "user", "content": event_text[:2000]}],
    )
    answer = resp.content[0].text.strip().upper() if resp.content else ""
    return answer.startswith("Y")


async def analyze_event(event_text: str, markets_context: list[dict]) -> list[dict]:
    """Sonnet analysis with prompt caching on market context."""
    if not _client or not markets_context:
        return []

    markets_block = "\n".join(
        f"- {m['id']} [{m.get('current_yes_price', '?')}] {m['question']}"
        for m in markets_context
    )

    resp = await _client.messages.create(
        model=settings.claude_model_smart,
        max_tokens=1024,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT},
            {
                "type": "text",
                "text": f"Active political markets (id [current YES price] question):\n{markets_block}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": f"EVENT:\n{event_text[:4000]}\n\nEmit signals JSON."}],
    )
    raw = resp.content[0].text if resp.content else "{}"
    try:
        data = json.loads(raw)
        signals = data.get("signals", [])
    except json.JSONDecodeError:
        log.warning("llm.parse_failed", raw=raw[:200])
        return []

    filtered = [
        s for s in signals
        if float(s.get("confidence", 0)) >= settings.min_confidence
        and abs(int(s.get("edge_bps", 0))) >= settings.min_edge_bps
    ]
    log.info("llm.signals", total=len(signals), kept=len(filtered))
    return filtered


async def load_market_context(limit: int = 50) -> list[dict]:
    """Build the markets block for the prompt from DB."""
    from sqlalchemy import select

    from ingestion.polymarket import PolymarketReader
    from shared.db import session_scope
    from shared.models import Market

    async with session_scope() as db:
        result = await db.execute(
            select(Market).where(Market.resolved == False).limit(limit)  # noqa: E712
        )
        markets = result.scalars().all()

    # Enrich with current prices (best-effort, parallelize later)
    reader = PolymarketReader()
    out: list[dict] = []
    try:
        for m in markets:
            yes_token = m.tokens.get("YES") if m.tokens else None
            price: Decimal | None = None
            if yes_token:
                try:
                    price = await reader.get_midpoint(yes_token)
                except Exception:  # noqa: BLE001
                    price = None
            out.append({
                "id": m.id,
                "question": m.question,
                "current_yes_price": float(price) if price else None,
            })
    finally:
        await reader.close()
    return out
