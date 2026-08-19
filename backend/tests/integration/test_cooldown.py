"""Задержки повторного ответа на живом Redis (ТЗ §20)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from redis.asyncio import Redis

from app.actions.cooldown import CooldownGuard, CooldownKeys
from app.rules.engine import CooldownSpec

pytestmark = pytest.mark.integration

ACCOUNT = uuid.uuid4()
CHAT = -1001234567890
PEER = 55555


def target(**overrides: object) -> CooldownKeys:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "tg_chat_id": CHAT,
        "peer_tg_id": PEER,
    }
    values.update(overrides)
    return CooldownKeys(**values)  # type: ignore[arg-type]


class TestNoCooldownConfigured:
    async def test_empty_spec_always_allows(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec()

        assert (await guard.check(target(), spec)).allowed is True
        verdict, claim = await guard.claim(target(), spec)
        assert verdict.allowed is True
        assert claim is not None


class TestUserCooldown:
    async def test_first_reply_allowed_second_blocked(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=60)

        first, claim = await guard.claim(target(), spec)
        assert first.allowed is True
        assert claim is not None

        second, second_claim = await guard.claim(target(), spec)
        assert second.allowed is False
        assert second.blocked_by == "user"
        assert second_claim is None
        assert second.retry_after_seconds is not None

    async def test_cheap_check_sees_the_block_too(self, redis_client: Redis) -> None:
        """Проверка до AI должна отсекать заведомо неотправляемый ответ."""
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=60)

        await guard.claim(target(), spec)
        verdict = await guard.check(target(), spec)

        assert verdict.allowed is False
        assert verdict.blocked_by == "user"

    async def test_another_user_is_not_affected(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=60)

        await guard.claim(target(), spec)
        other, claim = await guard.claim(target(peer_tg_id=PEER + 1, tg_chat_id=None), spec)

        assert other.allowed is True
        assert claim is not None

    async def test_release_allows_the_next_attempt(self, redis_client: Redis) -> None:
        """Неудачная отправка не должна запирать ответы на всё время задержки."""
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=60)

        _verdict, claim = await guard.claim(target(), spec)
        await guard.release(claim)

        retry, retry_claim = await guard.claim(target(), spec)
        assert retry.allowed is True
        assert retry_claim is not None

    async def test_expiry_frees_the_scope(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=1)

        await guard.claim(target(), spec)
        await asyncio.sleep(1.2)

        assert (await guard.claim(target(), spec))[0].allowed is True


class TestMultipleScopes:
    async def test_all_scopes_are_claimed_together(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        rule_id, scenario_id = uuid.uuid4(), uuid.uuid4()
        spec = CooldownSpec(user=60, chat=60, account=60, rule=60, scenario=60)

        _verdict, claim = await guard.claim(target(rule_id=rule_id, scenario_id=scenario_id), spec)

        assert claim is not None
        assert len(claim.redis_keys) == 5

    async def test_chat_scope_blocks_other_users_in_the_same_chat(
        self, redis_client: Redis
    ) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(chat=60)

        await guard.claim(target(), spec)
        verdict, _claim = await guard.claim(target(peer_tg_id=PEER + 99), spec)

        assert verdict.allowed is False
        assert verdict.blocked_by == "chat"

    async def test_partial_conflict_claims_nothing(self, redis_client: Redis) -> None:
        """Либо заняты все области, либо ни одна — иначе остаются висеть ключи."""
        guard = CooldownGuard(redis_client)

        await guard.claim(target(), CooldownSpec(chat=60))
        verdict, claim = await guard.claim(target(), CooldownSpec(user=60, chat=60))

        assert verdict.allowed is False
        assert claim is None
        # Пользовательская область осталась свободной: значит, частичного
        # захвата не произошло.
        assert (await guard.check(target(tg_chat_id=None), CooldownSpec(user=60))).allowed is True


class TestConcurrency:
    async def test_only_one_of_many_simultaneous_claims_wins(self, redis_client: Redis) -> None:
        guard = CooldownGuard(redis_client)
        spec = CooldownSpec(user=60)

        results = await asyncio.gather(*(guard.claim(target(), spec) for _ in range(15)))

        winners = [verdict for verdict, _claim in results if verdict.allowed]
        assert len(winners) == 1, "два ответа одному человеку одновременно недопустимы"
