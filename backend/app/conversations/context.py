"""Сборка контекста для модели (ТЗ §14).

Отправлять модели всю историю чата нельзя: это дорого, медленно и быстро
упирается в лимит контекста. Вместо этого собирается компактный набор:

    последние N сообщений + пересказ старого + память о собеседнике

Числа приходят из сценария, а если он их не задаёт — из настроек. Порядок
сообщений — от старых к новым, как в обычной переписке: модель, получившая
историю в обратном порядке, отвечает не на то, на что нужно.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.ai.provider import ChatMessage
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.repositories.conversations import ConversationRepository, UserMemoryRepository
from app.database.repositories.messages import MessageRepository
from app.database.session import Database
from app.models import Message

logger = get_logger(__name__)

# Длинные сообщения обрезаются: одно чужое полотно не должно вытеснить из
# контекста всю остальную переписку.
MAX_CONTEXT_MESSAGE_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Готовый контекст для анализатора и генератора."""

    history: list[ChatMessage] = field(default_factory=list)
    summary: str | None = None
    memory: dict[str, str] = field(default_factory=dict)
    recent_replies: tuple[str, ...] = ()
    ai_replies_in_row: int = 0
    conversation_id: uuid.UUID | None = None

    @property
    def is_empty(self) -> bool:
        return not self.history and not self.summary and not self.memory


class ContextBuilder:
    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    async def build(
        self,
        *,
        account_id: uuid.UUID,
        tg_chat_id: int,
        peer_tg_id: int | None,
        current_tg_message_id: int,
        context_messages: int | None = None,
    ) -> PromptContext:
        limit = context_messages or self._settings.max_context_messages

        async with self._database.session() as db:
            rows = await MessageRepository(db).recent_in_chat(account_id, tg_chat_id, limit + 1)
            conversations = ConversationRepository(db)
            recent_replies = tuple(
                await conversations.recent_bot_replies(account_id, tg_chat_id, limit=5)
            )

            summary: str | None = None
            memory: dict[str, str] = {}
            conversation_id: uuid.UUID | None = None
            replies_in_row = 0

            if peer_tg_id is not None:
                conversation = await conversations.get_or_create(account_id, peer_tg_id)
                summary = conversation.summary
                conversation_id = conversation.id
                # Пришло сообщение собеседника — цепочка наших ответов прервана,
                # счётчик самоответов обнуляется. Без этого он рос бы вечно и
                # после N ответов блокировал бы аккаунт навсегда.
                await conversations.register_incoming(conversation.id)
                replies_in_row = 0
                memory = await UserMemoryRepository(db).load(account_id, peer_tg_id)

        history = _to_chat_messages(rows, exclude_tg_message_id=current_tg_message_id, limit=limit)

        return PromptContext(
            history=history,
            summary=summary,
            memory=memory,
            recent_replies=recent_replies,
            ai_replies_in_row=replies_in_row,
            conversation_id=conversation_id,
        )


def _to_chat_messages(
    rows: list[Message], *, exclude_tg_message_id: int, limit: int
) -> list[ChatMessage]:
    """Превращает записи базы в реплики промпта, от старых к новым."""
    messages: list[ChatMessage] = []
    for row in rows:
        if row.tg_message_id == exclude_tg_message_id:
            # Текущее сообщение передаётся отдельно, в контексте оно было бы
            # продублировано.
            continue
        text = (row.text or "").strip()
        if not text:
            continue
        messages.append(
            ChatMessage(
                role="assistant" if row.is_outgoing or row.is_bot_reply else "user",
                content=text[:MAX_CONTEXT_MESSAGE_LENGTH],
            )
        )
        if len(messages) >= limit:
            break

    messages.reverse()
    return messages
