from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Vertical(str, Enum):
    POLITICS = "politics"
    SPORTS = "sports"
    CRYPTO = "crypto"
    ALL = "all"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    initial_bankroll_usdc: float = 100.0
    mode: Mode = Mode.PAPER
    focus_vertical: Vertical = Vertical.POLITICS

    polygon_rpc_url: str = "https://polygon-rpc.com"
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    wallet_private_key: str = ""
    wallet_address: str = ""

    gemini_api_key: str = ""
    gemini_model_fast: str = "gemini-3-flash-preview"
    gemini_model_smart: str = "gemini-3-pro"

    newsapi_key: str = ""
    gdelt_enabled: bool = True
    twitter_bearer_token: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "polymoney/0.1"

    alchemy_polygon_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    database_url: str = "postgresql+psycopg://polymoney:polymoney@localhost:5432/polymoney"
    redis_url: str = "redis://localhost:6379/0"

    max_position_pct: float = Field(0.05, ge=0, le=1)
    max_event_exposure_pct: float = Field(0.20, ge=0, le=1)
    kelly_fraction: float = Field(0.33, ge=0, le=1)
    daily_drawdown_stop_pct: float = Field(0.15, ge=0, le=1)
    min_edge_bps: int = 300
    min_confidence: float = Field(0.65, ge=0, le=1)

    dashboard_port: int = 3000
    api_port: int = 8000

    @property
    def database_url_sync(self) -> str:
        # psycopg v3 supports both sync and async modes; keep the driver prefix
        # so alembic (sync) uses the same dbapi we install. Only rewrite asyncpg
        # URLs (which we do not actually use) to the psycopg equivalent.
        url = self.database_url.replace("postgresql+asyncpg", "postgresql+psycopg")
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
