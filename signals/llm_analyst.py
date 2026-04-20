"""Gemini-powered analyst: event + market context -> structured signal.

Cost strategy for small bankroll:
- Gemini 3 Flash Preview first pass: filter events that look political-relevant.
- Gemini 3 Pro second pass: only for high-relevance events.
- The market-context block is sent as `cached_content` (Gemini's explicit
  context-cache API) so the stable list of active markets is paid for once
  per cache TTL (~1h) instead of per request.
"""

import json
from datetime import timedelta
from decimal import Decimal

from google import genai
from google.genai import types

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

_client = (
    genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None
)

# Cache the market-context block so repeated analyses share it.
# Refreshed externally when the active market set changes materially.
_market_cache_name: str | None = None
_market_cache_signature: str | None = None

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
- Never invent markets that are not in the provided list."""


async def triage_event_is_relevant(event_text: str) -> bool:
    """Cheap Flash filter. Returns True if the event looks politically market-relevant."""
    if not _client:
        return False
    resp = await _client.aio.models.generate_content(
        model=settings.gemini_model_fast,
        contents=event_text[:2000],
        config=types.GenerateContentConfig(
            system_instruction=(
                "Answer only YES or NO. Is this text about US politics, "
                "elections, policy, or a political figure that could affect "
                "a prediction market?"
            ),
            max_output_tokens=8,
            temperature=0.0,
        ),
    )
    answer = (resp.text or "").strip().upper()
    return answer.startswith("Y")


def _markets_block(markets_context: list[dict]) -> str:
    return "\n".join(
        f"- {m['id']} [{m.get('current_yes_price', '?')}] {m['question']}"
        for m in markets_context
    )


async def _ensure_market_cache(markets_block: str) -> str | None:
    """Create or reuse a cached_content of the system prompt + market list."""
    global _market_cache_name, _market_cache_signature
    if not _client:
        return None
    signature = str(hash(markets_block))
    if _market_cache_name and _market_cache_signature == signature:
        return _market_cache_name
    try:
        cache = await _client.aio.caches.create(
            model=settings.gemini_model_smart,
            config=types.CreateCachedContentConfig(
                system_instruction=SYSTEM_PROMPT,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(
                            text=f"Active political markets (id [current YES price] question):\n{markets_block}"
                        )],
                    )
                ],
                ttl=str(int(timedelta(hours=1).total_seconds())) + "s",
            ),
        )
        _market_cache_name = cache.name
        _market_cache_signature = signature
        log.info("llm.cache_created", name=cache.name)
        return cache.name
    except Exception as e:  # noqa: BLE001
        log.warning("llm.cache_failed", err=str(e))
        _market_cache_name = None
        _market_cache_signature = None
        return None


async def analyze_event(event_text: str, markets_context: list[dict]) -> list[dict]:
    """Pro analysis with cached market context."""
    if not _client or not markets_context:
        return []

    block = _markets_block(markets_context)
    cache_name = await _ensure_market_cache(block)

    cfg_kwargs: dict = {
        "max_output_tokens": 1024,
        "temperature": 0.2,
        "response_mime_type": "application/json",
    }
    if cache_name:
        cfg_kwargs["cached_content"] = cache_name
    else:
        # Cache unavailable -- inline the system + context as a fallback.
        cfg_kwargs["system_instruction"] = (
            SYSTEM_PROMPT
            + "\n\nActive political markets (id [current YES price] question):\n"
            + block
        )

    resp = await _client.aio.models.generate_content(
        model=settings.gemini_model_smart,
        contents=f"EVENT:\n{event_text[:4000]}\n\nEmit signals JSON.",
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    raw = resp.text or "{}"
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
