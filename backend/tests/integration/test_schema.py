"""Проверки схемы: то, на что конвейер полагается как на гарантию базы.

Здесь проверяются не модели, а поведение PostgreSQL: ограничения, каскады и
атомарность «застолбить сообщение». Если эти гарантии сломаются, конвейер
начнёт отвечать дважды, и никакой код на уровне приложения этого не поймает.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    AccountStatus,
    Chat,
    Conversation,
    ConversationStatus,
    Lead,
    Message,
    ProcessedMessage,
    Rule,
    Scenario,
    TelegramSession,
)

pytestmark = pytest.mark.integration


def make_message(account: Account, chat: Chat, tg_message_id: int, **kwargs: object) -> Message:
    defaults: dict[str, object] = {
        "account_id": account.id,
        "chat_id": chat.id,
        "tg_chat_id": chat.tg_chat_id,
        "tg_message_id": tg_message_id,
        "sender_tg_id": 555,
        "text": "нужен дизайнер",
        "date": datetime.now(UTC),
        "is_incoming": True,
        "is_outgoing": False,
    }
    return Message(**{**defaults, **kwargs})


class TestMessageDeduplication:
    async def test_same_message_twice_is_rejected(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        db.add(make_message(account, chat, 1001))
        await db.flush()

        db.add(make_message(account, chat, 1001))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_same_id_in_another_chat_is_allowed(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        other = Chat(
            account_id=account.id, tg_chat_id=chat.tg_chat_id + 1, type=chat.type, title="other"
        )
        db.add(other)
        await db.flush()

        db.add(make_message(account, chat, 1001))
        db.add(make_message(account, other, 1001, chat_id=other.id, tg_chat_id=other.tg_chat_id))
        await db.flush()

        count = await db.scalar(
            select(func.count()).select_from(Message).where(Message.account_id == account.id)
        )
        assert count == 2

    async def test_direction_must_be_exclusive(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        db.add(make_message(account, chat, 1002, is_incoming=True, is_outgoing=True))
        with pytest.raises(IntegrityError):
            await db.flush()


class TestProcessedMessageClaim:
    """«Застолбить до обработки» — единственная защита от двойного ответа."""

    @staticmethod
    def _claim_stmt(account: Account, chat: Chat, tg_message_id: int) -> Any:
        return (
            pg_insert(ProcessedMessage)
            .values(
                account_id=account.id,
                tg_chat_id=chat.tg_chat_id,
                tg_message_id=tg_message_id,
                stage="claimed",
            )
            .on_conflict_do_nothing(index_elements=["account_id", "tg_chat_id", "tg_message_id"])
        )

    async def test_first_claim_wins_second_is_noop(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        first = await db.execute(self._claim_stmt(account, chat, 2001))
        second = await db.execute(self._claim_stmt(account, chat, 2001))

        assert first.rowcount == 1, "первая попытка обязана застолбить сообщение"
        assert second.rowcount == 0, "повторная доставка не должна пройти в обработку"

    async def test_different_messages_claim_independently(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        for message_id in (2002, 2003, 2004):
            result = await db.execute(self._claim_stmt(account, chat, message_id))
            assert result.rowcount == 1


class TestCascades:
    async def test_deleting_account_removes_its_data(
        self, db: AsyncSession, account: Account, chat: Chat
    ) -> None:
        db.add(make_message(account, chat, 3001))
        db.add(
            TelegramSession(account_id=account.id, ciphertext=b"x", nonce=b"y" * 12, key_id="k1")
        )
        await db.flush()

        account_id = account.id
        await db.execute(delete(Account).where(Account.id == account_id))
        await db.flush()

        async def count_for(model: Any) -> int | None:
            return await db.scalar(
                select(func.count()).select_from(model).where(model.account_id == account_id)
            )

        assert await count_for(Chat) == 0
        assert await count_for(Message) == 0
        assert await count_for(TelegramSession) == 0

    async def test_session_is_one_per_account(self, db: AsyncSession, account: Account) -> None:
        db.add(
            TelegramSession(account_id=account.id, ciphertext=b"a", nonce=b"n" * 12, key_id="k1")
        )
        await db.flush()

        db.add(
            TelegramSession(account_id=account.id, ciphertext=b"b", nonce=b"m" * 12, key_id="k1")
        )
        with pytest.raises(IntegrityError):
            await db.flush()


class TestConstraints:
    async def test_lead_score_bounded(self, db: AsyncSession, account: Account) -> None:
        db.add(Lead(account_id=account.id, tg_user_id=777, score=101))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_rule_with_ai_requires_threshold(self, db: AsyncSession) -> None:
        db.add(Rule(name="без порога", ai_enabled=True, ai_threshold=None))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_rule_with_ai_and_threshold_is_accepted(self, db: AsyncSession) -> None:
        db.add(Rule(name="с порогом", ai_enabled=True, ai_threshold=0.8))
        await db.flush()

    async def test_scenario_temperature_bounded(self, db: AsyncSession) -> None:
        db.add(Scenario(name="жара", system_prompt="p", temperature=3))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_one_conversation_per_peer(self, db: AsyncSession, account: Account) -> None:
        db.add(Conversation(account_id=account.id, peer_tg_id=42))
        await db.flush()

        db.add(Conversation(account_id=account.id, peer_tg_id=42))
        with pytest.raises(IntegrityError):
            await db.flush()


class TestEnumRoundTrip:
    async def test_enum_values_survive_reload(self, db: AsyncSession, account: Account) -> None:
        conversation = Conversation(
            account_id=account.id, peer_tg_id=99, status=ConversationStatus.HOT
        )
        db.add(conversation)
        await db.flush()
        # id читаем до expire_all: обращение к истёкшему атрибуту вне greenlet
        # SQLAlchemy обернуть уже не сможет.
        conversation_id = conversation.id
        account_id = account.id
        db.expire_all()

        loaded = await db.get(Conversation, conversation_id)
        assert loaded is not None
        assert loaded.status is ConversationStatus.HOT

        reloaded_account = await db.get(Account, account_id)
        assert reloaded_account is not None
        assert reloaded_account.status is AccountStatus.ONLINE


class TestDefaults:
    async def test_jsonb_defaults_are_empty_objects(self, db: AsyncSession) -> None:
        rule = Rule(name="дефолты")
        db.add(rule)
        await db.flush()
        rule_id = rule.id
        db.expire_all()

        loaded = await db.get(Rule, rule_id)
        assert loaded is not None
        assert loaded.filters == {}
        assert loaded.keywords == {}
        assert loaded.cooldown == {}
        assert loaded.priority == 100
        assert loaded.stop_on_match is True

    async def test_conversation_counters_start_at_zero(
        self, db: AsyncSession, account: Account
    ) -> None:
        conversation = Conversation(account_id=account.id, peer_tg_id=13)
        db.add(conversation)
        await db.flush()

        assert conversation.message_count == 0
        assert conversation.ai_replies_in_row == 0
        assert conversation.status is ConversationStatus.NEW
