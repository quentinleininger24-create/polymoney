.PHONY: help install up down db-migrate db-revision ingest bot dashboard test lint fmt clean

help:
	@echo "install      - install python deps via uv"
	@echo "up           - start postgres + redis"
	@echo "down         - stop postgres + redis"
	@echo "db-migrate   - apply alembic migrations"
	@echo "db-revision  - autogenerate migration (NAME=...)"
	@echo "ingest       - run ingestion scheduler"
	@echo "bot          - run telegram bot"
	@echo "dashboard    - run Next.js dashboard"
	@echo "test         - run pytest"
	@echo "lint         - ruff + mypy"
	@echo "fmt          - ruff format"

install:
	uv venv
	uv pip install -e ".[dev]"

up:
	docker compose up -d
	@echo "postgres: localhost:5432  redis: localhost:6379"

down:
	docker compose down

db-migrate:
	alembic upgrade head

db-revision:
	alembic revision --autogenerate -m "$(NAME)"

ingest:
	python -m ingestion.scheduler

bot:
	python -m bot.telegram_bot

dashboard:
	cd dashboard && pnpm dev

backtest:
	@if [ -z "$(START)" ] || [ -z "$(END)" ]; then \
	  echo "usage: make backtest START=2026-04-01 END=2026-04-15 [MODE=bets|signals]"; \
	  exit 1; \
	fi
	python -m strategy.backtest --start $(START) --end $(END) --mode $${MODE:-bets}

test:
	pytest

lint:
	ruff check .
	mypy shared ingestion signals strategy execution risk bot

fmt:
	ruff format .
	ruff check --fix .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
