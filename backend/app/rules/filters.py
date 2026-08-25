"""Фильтры сообщений (ТЗ §9).

Дешёвые проверки, которые выполняются до правил и до AI. Каждый фильтр —
маленькая функция над NormalizedMessage: их удобно комбинировать и легко
проверять по отдельности.

Отдельно стоит SelfGuard. Его задача — не дать системе отвечать самой себе.
Проверки `is_outgoing` для этого недостаточно: если два наших аккаунта сидят в
одном чате, для аккаунта A сообщение аккаунта B выглядит как обычное входящее,
и без реестра собственных идентификаторов они начнут переписываться друг с
другом бесконечно.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.models import ChatType
from app.rules.keywords import KeywordMatcher, KeywordSpec, compile_regex
from app.telegram.messages import NormalizedMessage


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    passed: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> FilterVerdict:
        return cls(passed=True)

    @classmethod
    def reject(cls, reason: str) -> FilterVerdict:
        return cls(passed=False, reason=reason)

    def __bool__(self) -> bool:
        return self.passed


@dataclass(frozen=True, slots=True)
class MessageFilterSpec:
    """Условия отбора сообщений, заданные в правиле."""

    incoming_only: bool = True
    allow_outgoing: bool = False
    text_only: bool = False
    allow_forwarded: bool = True
    chat_types: tuple[ChatType, ...] = ()
    sender_ids: tuple[int, ...] = ()
    exclude_sender_ids: tuple[int, ...] = ()
    languages: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MessageFilterSpec:
        data = data or {}
        return cls(
            incoming_only=bool(data.get("incoming_only", True)),
            allow_outgoing=bool(data.get("outgoing", False)),
            text_only=bool(data.get("text_only", False)),
            allow_forwarded=bool(data.get("forwarded", True)),
            chat_types=tuple(_chat_types(data.get("chat_types"))),
            sender_ids=tuple(_ints(data.get("sender_ids"))),
            exclude_sender_ids=tuple(_ints(data.get("exclude_sender_ids"))),
            languages=tuple(str(value).lower() for value in _strings(data.get("languages"))),
        )


class SelfGuard:
    """Не пропускает наши собственные сообщения (ТЗ §9).

    Реестр идентификаторов обновляется при изменении состава аккаунтов, а не
    на каждое сообщение: это проверка на горячем пути.
    """

    def __init__(self, own_ids: Iterable[int] = ()) -> None:
        self._own_ids: set[int] = set(own_ids)

    def update(self, own_ids: Iterable[int]) -> None:
        self._own_ids = set(own_ids)

    @property
    def own_ids(self) -> frozenset[int]:
        return frozenset(self._own_ids)

    def check(self, message: NormalizedMessage) -> FilterVerdict:
        if message.is_outgoing:
            return FilterVerdict.reject("own outgoing message")
        if message.sender_tg_id is not None and message.sender_tg_id in self._own_ids:
            # Сообщение другого нашего же аккаунта: ответ на него запустил бы
            # бесконечную переписку двух аккаунтов между собой.
            return FilterVerdict.reject("sender is one of our own accounts")
        return FilterVerdict.ok()


class StopGuard:
    """Стоп-лист: отправители, которым никогда не отвечаем.

    Как и SelfGuard — проверка на горячем пути, поэтому набор держится в памяти
    и обновляется пачкой. Блокировка по tg-id или по @username.
    """

    def __init__(self) -> None:
        self._ids: set[int] = set()
        self._usernames: set[str] = set()

    def update(self, tg_ids: Iterable[int], usernames: Iterable[str]) -> None:
        self._ids = set(tg_ids)
        self._usernames = {u.lstrip("@").lower() for u in usernames if u}

    def blocked(self, message: NormalizedMessage) -> bool:
        if message.sender_tg_id is not None and message.sender_tg_id in self._ids:
            return True
        username = (message.sender_username or "").lstrip("@").lower()
        return bool(username) and username in self._usernames


def check_message(message: NormalizedMessage, spec: MessageFilterSpec) -> FilterVerdict:
    """Применяет условия правила к сообщению."""
    if spec.incoming_only and not message.is_incoming:
        return FilterVerdict.reject("incoming_only")
    if message.is_outgoing and not spec.allow_outgoing:
        return FilterVerdict.reject("outgoing not allowed")
    if spec.text_only and not message.has_text:
        return FilterVerdict.reject("text_only")
    if not spec.allow_forwarded and message.is_forwarded:
        return FilterVerdict.reject("forwarded not allowed")
    if spec.chat_types and message.chat_type not in spec.chat_types:
        return FilterVerdict.reject(f"chat type {message.chat_type.value} not allowed")
    if spec.sender_ids and message.sender_tg_id not in spec.sender_ids:
        return FilterVerdict.reject("sender not in allow list")
    if spec.exclude_sender_ids and message.sender_tg_id in spec.exclude_sender_ids:
        return FilterVerdict.reject("sender excluded")
    return FilterVerdict.ok()


def check_keywords(text: str, spec: KeywordSpec) -> FilterVerdict:
    if spec.is_empty:
        return FilterVerdict.ok()
    if KeywordMatcher(spec).matches(text):
        return FilterVerdict.ok()
    return FilterVerdict.reject("keywords did not match")


def check_regex(text: str, pattern: str | None) -> FilterVerdict:
    compiled = compile_regex(pattern)
    if compiled is None:
        return FilterVerdict.ok()
    if compiled.search(text):
        return FilterVerdict.ok()
    return FilterVerdict.reject("regex did not match")


def _ints(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _chat_types(values: Any) -> list[ChatType]:
    result: list[ChatType] = []
    for value in _strings(values):
        try:
            result.append(ChatType(value.upper()))
        except ValueError:
            continue
    return result
