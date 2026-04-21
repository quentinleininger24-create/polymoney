FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

# uv for fast installs
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

WORKDIR /app

# Install Python deps (layer cached)
COPY pyproject.toml ./
RUN uv venv /opt/venv \
    && . /opt/venv/bin/activate \
    && uv pip install --no-deps -e . \
    && uv pip install \
        py-clob-client web3 eth-account \
        google-genai \
        "sqlalchemy[asyncio]" "psycopg[binary]" alembic redis \
        fastapi "uvicorn[standard]" pydantic pydantic-settings \
        httpx tweepy praw feedparser pytrends youtube-transcript-api \
        sentence-transformers numpy pandas scikit-learn \
        python-telegram-bot apscheduler \
        structlog rich

ENV PATH="/opt/venv/bin:$PATH"

COPY . .

# Default: ingestion scheduler. Override with docker compose to run
# order_manager / bot / dashboard-api as separate services.
CMD ["python", "-m", "ingestion.scheduler"]
