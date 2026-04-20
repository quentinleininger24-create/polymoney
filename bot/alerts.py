"""Outbound Telegram alerts from the backend (not bot commands)."""

import httpx

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)


async def send_alert(text: str, silent: bool = False) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_notification": silent,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("telegram.send_failed", err=str(e))
