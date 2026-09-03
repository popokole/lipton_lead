"""Настройки бота-уведомлений (ТЗ §36, отдельный отчётный бот).

Отдельный бот (НЕ пользовательский аккаунт) шлёт результаты в форум-группу:
на каждый сценарий — свой топик. Токен бота хранится зашифрованным тем же
ключом, что и сессии; наружу в API не отдаётся.

Одна строка на всю систему: настройка глобальная. Фиксированный id избавляет
от гонки «создать/обновить» — всегда апсертим один и тот же ряд.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin
from app.models._types import Ciphertext, TelegramId

# Единственная строка настроек: детерминированный ключ вместо автоинкремента.
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class NotifySettings(TimestampMixin, Base):
    __tablename__ = "notify_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=SINGLETON_ID
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # Токен бота @BotFather — секрет, шифруется.
    bot_token_ct: Mapped[bytes | None] = mapped_column(Ciphertext)
    bot_token_nonce: Mapped[bytes | None] = mapped_column(Ciphertext)
    bot_token_key_id: Mapped[str | None] = mapped_column(sa.String(32))

    # id форум-группы, куда бот шлёт (с включёнными топиками).
    group_id: Mapped[int | None] = mapped_column(TelegramId)

    # Общий поток «Общение ИИ»: все ответы (личка+группы) и карточки на
    # подтверждение, для которых правило/сценарий не завёл свой топик.
    # Создаётся лениво. Свой топик — только у правил с notify_topic_enabled.
    ai_chat_topic_id: Mapped[int | None] = mapped_column(TelegramId)
    # Топик «Дайджест»: ежедневная сводка по лидам.
    digest_topic_id: Mapped[int | None] = mapped_column(TelegramId)

    bot_username: Mapped[str | None] = mapped_column(sa.String(64))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
