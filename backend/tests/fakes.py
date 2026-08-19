"""Подделки Telegram-клиента для тестов.

Реальные действия в Telegram из автотестов запрещены (ТЗ §37), поэтому весь
слой клиента подменяется здесь. Подделка повторяет только то поведение, на
которое опирается код: состояние подключения, авторизацию, вход по коду и
отправку сообщений.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.ai.provider import (
    AIResponse,
    AnalysisResult,
    AnalyzeRequest,
    GeneratedReply,
    GenerateRequest,
    Intent,
    SummarizeRequest,
    Summary,
    Usage,
)
from app.telegram.session_manager import SessionCredentials


def make_session_string(dc_id: int = 2) -> str:
    session = StringSession()
    session.set_dc(dc_id, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=bytes(256))
    return session.save()


@dataclass
class FakeUser:
    id: int = 424242
    username: str | None = "tester"
    first_name: str | None = "Тест"
    last_name: str | None = "Аккаунт"


@dataclass
class FakeSentMessage:
    id: int


@dataclass
class FakeDialog:
    id: int
    name: str
    entity: Any = None
    is_group: bool = False
    is_channel: bool = False
    is_user: bool = False


@dataclass
class FakeCodeRequest:
    phone_code_hash: str = "hash-not-a-secret"


class FakeTypingAction:
    """Подделка `client.action(entity, 'typing')` — просто async context manager."""

    def __init__(self, client: FakeTelegramClient, entity: Any, action: str) -> None:
        self._client = client
        self._entity = entity
        self._action = action

    async def __aenter__(self) -> FakeTypingAction:
        self._client.typing_actions.append((int(self._entity), self._action))
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


@dataclass
class FakeTelegramClient:
    """Управляемый клиент: каждое поведение задаётся полем."""

    authorized: bool = True
    connect_failures: int = 0
    connect_error: BaseException | None = None
    password_required: bool = False
    flood_wait_seconds: int | None = None
    dialogs: list[FakeDialog] = field(default_factory=list)

    connected: bool = False
    connect_calls: int = 0
    disconnect_calls: int = 0
    logged_out: bool = False
    handlers: list[tuple[Any, Any]] = field(default_factory=list)
    sent: list[tuple[int, str, int | None]] = field(default_factory=list)
    read_acknowledged: list[tuple[int, int | None]] = field(default_factory=list)
    typing_actions: list[tuple[int, str]] = field(default_factory=list)
    signed_in_with: dict[str, Any] = field(default_factory=dict)
    session: StringSession = field(default_factory=StringSession)
    _next_message_id: int = 1000
    _flood_raised: bool = False

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise self.connect_error or ConnectionError("network is down")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> FakeUser | None:
        return FakeUser() if self.authorized else None

    async def log_out(self) -> bool:
        self.logged_out = True
        self.authorized = False
        return True

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers.append((callback, event))

    def is_connected(self) -> bool:
        return self.connected

    async def send_message(
        self, entity: Any, message: str, *, reply_to: int | None = None
    ) -> FakeSentMessage:
        if self.flood_wait_seconds is not None and not self._flood_raised:
            self._flood_raised = True
            raise FloodWaitError(request=None, capture=self.flood_wait_seconds)
        self.sent.append((int(entity), message, reply_to))
        self._next_message_id += 1
        return FakeSentMessage(id=self._next_message_id)

    async def get_dialogs(self, limit: int | None = None) -> list[FakeDialog]:
        return self.dialogs[:limit] if limit else self.dialogs

    async def send_read_acknowledge(self, entity: Any, *, max_id: int | None = None) -> None:
        self.read_acknowledged.append((int(entity), max_id))

    def action(self, entity: Any, action: str) -> FakeTypingAction:
        return FakeTypingAction(self, entity, action)

    async def get_entity(self, entity: Any) -> FakeUser:
        return FakeUser(id=int(entity) if str(entity).lstrip("-").isdigit() else 1)

    async def send_code_request(self, phone: str) -> FakeCodeRequest:
        return FakeCodeRequest()

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        *,
        password: str | None = None,
        phone_code_hash: str | None = None,
    ) -> FakeUser:
        if password is None and self.password_required:
            raise SessionPasswordNeededError(request=None)
        self.signed_in_with = {
            "phone": phone,
            "has_code": code is not None,
            "has_password": password is not None,
        }
        self.authorized = True
        # После успешного входа сессия становится сохраняемой.
        self.session.set_dc(2, "149.154.167.51", 443)
        self.session.auth_key = AuthKey(data=bytes(256))
        return FakeUser()


class FakeClientFactory:
    """Выдаёт заранее подготовленные клиенты, по одному на аккаунт."""

    def __init__(self, template: FakeTelegramClient | None = None) -> None:
        self._template = template
        self.created: dict[uuid.UUID, FakeTelegramClient] = {}
        self.per_account: dict[uuid.UUID, FakeTelegramClient] = {}

    def preset(self, account_id: uuid.UUID, client: FakeTelegramClient) -> None:
        self.per_account[account_id] = client

    async def create(
        self,
        account_id: uuid.UUID,
        credentials: SessionCredentials,
        proxy: dict[str, Any] | None,
    ) -> FakeTelegramClient:
        client = self.per_account.get(account_id)
        if client is None:
            client = self._template or FakeTelegramClient()
            self._template = None
        self.created[account_id] = client
        return client


@dataclass
class FakeAIProvider:
    """Поставщик AI без сети: результаты задаются полями."""

    analysis: AnalysisResult | None = None
    reply: GeneratedReply | None = None
    summary_text: str = "краткий пересказ"
    fail_analyze: Exception | None = None
    fail_generate: Exception | None = None
    analyze_calls: list[str] = field(default_factory=list)
    generate_calls: list[GenerateRequest] = field(default_factory=list)
    closed: bool = False

    @property
    def name(self) -> str:
        return "fake"

    async def close(self) -> None:
        self.closed = True

    async def analyze(self, request: AnalyzeRequest) -> AIResponse[AnalysisResult]:
        self.analyze_calls.append(request.message_text)
        if self.fail_analyze is not None:
            raise self.fail_analyze
        result = self.analysis or AnalysisResult(
            relevant=True,
            confidence=0.95,
            intent=Intent.SERVICE_REQUEST,
            should_reply=True,
            lead_score=80,
            reason="прямой запрос услуги",
        )
        return AIResponse(
            result=result, usage=Usage(prompt_tokens=120, completion_tokens=30, model="fake")
        )

    async def generate(self, request: GenerateRequest) -> AIResponse[GeneratedReply]:
        self.generate_calls.append(request)
        if self.fail_generate is not None:
            raise self.fail_generate
        result = self.reply or GeneratedReply(
            text="Здравствуйте! Да, поможем с дизайном. Расскажите про задачу.",
            used_knowledge=False,
        )
        return AIResponse(
            result=result, usage=Usage(prompt_tokens=300, completion_tokens=60, model="fake")
        )

    async def summarize(self, request: SummarizeRequest) -> AIResponse[Summary]:
        return AIResponse(
            result=Summary(text=self.summary_text, facts=[]),
            usage=Usage(prompt_tokens=200, completion_tokens=40, model="fake"),
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> AIResponse[list[list[float]]]:
        vectors = [[0.0] * 1536 for _ in texts]
        return AIResponse(result=vectors, usage=Usage(prompt_tokens=len(texts), model="fake"))
