"""Движок правил (ТЗ §8).

Правила проверяются по убыванию приоритета, первое совпадение с
`stop_on_match` завершает подбор. Так сделано потому, что одно сообщение
должно порождать одно действие: без этого правила «Поиск клиентов» и
«Поддержка» ответили бы на одно и то же сообщение дважды.

Порядок проверок внутри правила — от дешёвого к дорогому: принадлежность
аккаунту и чату, затем условия по сообщению, затем слова, и только потом
регулярное выражение. Обращение к AI здесь не происходит вообще: движок лишь
сообщает, что правило требует проверки моделью.

Скомпилированные правила кешируются: перечитывать таблицу и компилировать
регулярки на каждое сообщение — заметная нагрузка при десятках правил.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.clock import get_clock
from app.core.logging import get_logger
from app.database.repositories.rules import RuleRepository
from app.database.session import Database
from app.models import ActionType, Rule, RuleScope
from app.rules.filters import (
    FilterVerdict,
    MessageFilterSpec,
    check_keywords,
    check_message,
    check_regex,
)
from app.rules.keywords import Hit, KeywordMatcher, KeywordSpec, compile_regex
from app.telegram.messages import NormalizedMessage

logger = get_logger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CooldownSpec:
    """Задержки повторного действия, в секундах (ТЗ §20)."""

    user: int = 0
    chat: int = 0
    account: int = 0
    rule: int = 0
    scenario: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, default_user: int) -> CooldownSpec:
        data = data or {}
        return cls(
            user=_seconds(data.get("user"), default_user),
            chat=_seconds(data.get("chat"), 0),
            account=_seconds(data.get("account"), 0),
            rule=_seconds(data.get("rule"), 0),
            scenario=_seconds(data.get("scenario"), 0),
        )


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """Правило, готовое к применению."""

    id: uuid.UUID
    name: str
    priority: int
    stop_on_match: bool
    scope: RuleScope
    scenario_id: uuid.UUID | None
    action: ActionType
    action_config: dict[str, Any]
    filters: MessageFilterSpec
    keywords: KeywordSpec
    regex: str | None
    ai_enabled: bool
    ai_threshold: float | None
    cooldown: CooldownSpec
    account_ids: frozenset[uuid.UUID] = frozenset()
    chat_ids: frozenset[uuid.UUID] = frozenset()

    def covers_account(self, account_id: uuid.UUID) -> bool:
        return not self.account_ids or account_id in self.account_ids

    def covers_chat(self, chat_id: uuid.UUID | None) -> bool:
        if not self.chat_ids:
            return True
        return chat_id is not None and chat_id in self.chat_ids


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: CompiledRule
    hits: tuple[Hit, ...] = ()

    @property
    def matched_terms(self) -> tuple[str, ...]:
        return tuple(hit.term for hit in self.hits)


@dataclass
class _Cache:
    rules: list[CompiledRule] = field(default_factory=list)
    loaded_at: float = -1.0


class RuleEngine:
    def __init__(
        self,
        database: Database,
        *,
        default_user_cooldown: int,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._database = database
        self._default_user_cooldown = default_user_cooldown
        self._ttl = cache_ttl_seconds
        self._cache = _Cache()

    def invalidate(self) -> None:
        """Сбрасывает кеш — вызывается после правки правил в панели."""
        self._cache = _Cache()

    async def rules(self, scope: RuleScope | None = None) -> list[CompiledRule]:
        await self._refresh_if_stale()
        if scope is None:
            return list(self._cache.rules)
        # Правило со скоупом ALL работает в обоих конвейерах, поэтому подходит
        # под любой запрошенный скоуп.
        return [
            rule for rule in self._cache.rules if rule.scope is scope or rule.scope is RuleScope.ALL
        ]

    async def match_all(
        self,
        message: NormalizedMessage,
        *,
        chat_id: uuid.UUID | None,
        scope: RuleScope = RuleScope.CHAT_MONITOR,
    ) -> list[RuleMatch]:
        """Совпавшие правила в порядке приоритета.

        Подбор прекращается на первом сработавшем правиле с `stop_on_match`.
        Правило с `stop_on_match=False` не прерывает перебор — так к ответу
        можно добавить, например, уведомление администратора. Следить за тем,
        чтобы в чат ушёл ровно один ответ, — задача Action Engine.
        """
        return select_matches(await self.rules(scope), message, chat_id)

    async def match(
        self,
        message: NormalizedMessage,
        *,
        chat_id: uuid.UUID | None,
        scope: RuleScope = RuleScope.CHAT_MONITOR,
    ) -> RuleMatch | None:
        """Правило с наибольшим приоритетом из совпавших."""
        matches = await self.match_all(message, chat_id=chat_id, scope=scope)
        return matches[0] if matches else None

    async def _refresh_if_stale(self) -> None:
        now = get_clock().monotonic()
        if self._cache.loaded_at >= 0 and now - self._cache.loaded_at < self._ttl:
            return

        async with self._database.session() as db:
            repository = RuleRepository(db)
            rows = await repository.load_enabled()
            accounts, chats = await repository.load_scopes()

        compiled: list[CompiledRule] = []
        for row in rows:
            try:
                compiled.append(
                    compile_rule(
                        row,
                        account_ids=accounts.get(row.id, set()),
                        chat_ids=chats.get(row.id, set()),
                        default_user_cooldown=self._default_user_cooldown,
                    )
                )
            except ValueError as exc:
                # Битое правило не должно останавливать все остальные: оператор
                # увидит его в логе и починит, а система продолжит работать.
                logger.error(
                    "rule_compile_failed",
                    rule_id=str(row.id),
                    rule_name=row.name,
                    detail=str(exc),
                )

        self._cache = _Cache(rules=compiled, loaded_at=now)
        logger.debug("rules_reloaded", count=len(compiled))


def compile_rule(
    row: Rule,
    *,
    account_ids: set[uuid.UUID],
    chat_ids: set[uuid.UUID],
    default_user_cooldown: int,
) -> CompiledRule:
    """Превращает строку таблицы в готовое к применению правило."""
    keywords = KeywordSpec.from_dict(row.keywords)
    # Компилируем regex сразу: невалидный шаблон должен выявиться при загрузке,
    # а не на первом же сообщении.
    compile_regex(row.regex)

    return CompiledRule(
        id=row.id,
        name=row.name,
        priority=row.priority,
        stop_on_match=row.stop_on_match,
        scope=row.scope,
        scenario_id=row.scenario_id,
        action=row.action,
        action_config=dict(row.action_config or {}),
        filters=MessageFilterSpec.from_dict(row.filters),
        keywords=keywords,
        regex=row.regex,
        ai_enabled=row.ai_enabled,
        ai_threshold=float(row.ai_threshold) if row.ai_threshold is not None else None,
        cooldown=CooldownSpec.from_dict(row.cooldown, default_user_cooldown),
        account_ids=frozenset(account_ids),
        chat_ids=frozenset(chat_ids),
    )


def _seconds(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def evaluate_rule(
    rule: CompiledRule, message: NormalizedMessage, chat_id: uuid.UUID | None
) -> tuple[FilterVerdict, list[Hit]]:
    """Проверяет одно правило. Порядок — от дешёвых условий к дорогим."""
    if not rule.covers_account(message.account_id):
        return FilterVerdict.reject("account not covered"), []
    if not rule.covers_chat(chat_id):
        return FilterVerdict.reject("chat not covered"), []

    message_verdict = check_message(message, rule.filters)
    if not message_verdict:
        return message_verdict, []

    keyword_verdict = check_keywords(message.text, rule.keywords)
    if not keyword_verdict:
        return keyword_verdict, []

    regex_verdict = check_regex(message.text, rule.regex)
    if not regex_verdict:
        return regex_verdict, []

    hits = KeywordMatcher(rule.keywords).find(message.text) if not rule.keywords.is_empty else []
    return FilterVerdict.ok(), hits


def select_matches(
    rules: Sequence[CompiledRule], message: NormalizedMessage, chat_id: uuid.UUID | None
) -> list[RuleMatch]:
    """Совпавшие правила по порядку, до первого `stop_on_match`."""
    matches: list[RuleMatch] = []
    for rule in rules:
        verdict, hits = evaluate_rule(rule, message, chat_id)
        if not verdict:
            continue

        logger.debug("rule_matched", rule_id=str(rule.id), rule_name=rule.name, **message.for_log())
        matches.append(RuleMatch(rule=rule, hits=tuple(hits)))
        if rule.stop_on_match:
            break
    return matches
