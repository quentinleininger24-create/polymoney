"""Embedding-based matching: link events to candidate markets before LLM call.

Cuts LLM costs by ~90%: instead of sending every event against all markets,
we send only the top-K semantic matches.
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from shared.logging import get_logger

log = get_logger(__name__)

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _model


def embed(texts: list[str]) -> np.ndarray:
    return get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


def top_k(event_text: str, markets: list[dict], k: int = 10) -> list[dict]:
    """Return the k most relevant markets for an event by cosine similarity."""
    if not markets:
        return []
    event_vec = embed([event_text])[0]
    market_texts = [m["question"] for m in markets]
    market_vecs = embed(market_texts)
    sims = market_vecs @ event_vec
    idx = np.argsort(-sims)[:k]
    return [markets[i] | {"_sim": float(sims[i])} for i in idx]
