#!/usr/bin/env bash
set -euo pipefail

echo "==> polymoney setup"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Install Docker Desktop first."
  exit 1
fi

cp -n .env.example .env || true
echo "==> .env created (edit it with your keys)"

echo "==> installing python deps"
uv venv
uv pip install -e ".[dev]"

echo "==> starting postgres + redis"
docker compose up -d

echo "==> waiting for postgres"
for i in {1..30}; do
  if docker exec polymoney_pg pg_isready -U polymoney >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "==> creating initial migration"
alembic revision --autogenerate -m "init" || true
alembic upgrade head

echo "==> installing dashboard deps"
(cd dashboard && (command -v pnpm >/dev/null && pnpm install || npm install))

echo
echo "Done. Fill in .env, then:"
echo "  make ingest     # ingestion scheduler"
echo "  python -m execution.order_manager   # trade loop"
echo "  make bot        # telegram bot"
echo "  uvicorn shared.api:app --port 8000  # backend API"
echo "  make dashboard  # Next.js UI"
