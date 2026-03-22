"""
Whitelist middleware — блокирует всех пользователей кроме ALLOWED_IDS.
Заблокированные сообщения игнорируются молча (silent ignore).
"""

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.config import config

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает только пользователей из config.ALLOWED_IDS."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Извлекаем user_id из апдейта
        user_id: int | None = None

        if isinstance(event, Update):
            if event.message and event.message.from_user:
                user_id = event.message.from_user.id
            elif event.callback_query and event.callback_query.from_user:
                user_id = event.callback_query.from_user.id
            elif event.edited_message and event.edited_message.from_user:
                user_id = event.edited_message.from_user.id

        if user_id is not None and user_id not in config.ALLOWED_IDS:
            logger.warning("Заблокирован доступ для user_id=%s", user_id)
            return  # Молча игнорируем

        return await handler(event, data)
