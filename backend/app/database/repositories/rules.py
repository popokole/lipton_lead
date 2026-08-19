"""Загрузка правил и сценариев."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rule, RuleAccount, RuleChat, RuleScope, Scenario


class RuleRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load_enabled(self, scope: RuleScope | None = None) -> list[Rule]:
        """Включённые правила в порядке убывания приоритета."""
        stmt = select(Rule).where(Rule.enabled.is_(True))
        if scope is not None:
            stmt = stmt.where(Rule.scope == scope)
        stmt = stmt.order_by(Rule.priority.desc(), Rule.created_at)
        return list((await self._db.scalars(stmt)).all())

    async def load_scopes(
        self,
    ) -> tuple[dict[uuid.UUID, set[uuid.UUID]], dict[uuid.UUID, set[uuid.UUID]]]:
        """Привязки правил к аккаунтам и чатам.

        Пустая привязка означает «ко всем»: так правило можно завести один раз
        и не переписывать при добавлении нового аккаунта.
        """
        accounts: dict[uuid.UUID, set[uuid.UUID]] = {}
        for rule_id, account_id in await self._db.execute(
            select(RuleAccount.rule_id, RuleAccount.account_id)
        ):
            accounts.setdefault(rule_id, set()).add(account_id)

        chats: dict[uuid.UUID, set[uuid.UUID]] = {}
        for rule_id, chat_id in await self._db.execute(select(RuleChat.rule_id, RuleChat.chat_id)):
            chats.setdefault(rule_id, set()).add(chat_id)

        return accounts, chats

    async def get_scenario(self, scenario_id: uuid.UUID) -> Scenario | None:
        return await self._db.get(Scenario, scenario_id)

    async def load_enabled_scenarios(self) -> list[Scenario]:
        return list(
            (
                await self._db.scalars(
                    select(Scenario).where(Scenario.enabled.is_(True)).order_by(Scenario.name)
                )
            ).all()
        )
