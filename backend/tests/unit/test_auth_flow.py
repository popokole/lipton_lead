"""Вход в аккаунт: код, 2FA и обращение с секретами (ТЗ §4)."""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import InvalidInputError
from app.telegram.auth_flow import AuthFlow
from tests.conftest import make_settings
from tests.fakes import FakeClientFactory, FakeTelegramClient

PHONE = "+79991234567"
API_ID = 12345
API_HASH = "api-hash-value"


def build(client: FakeTelegramClient) -> tuple[AuthFlow, FakeClientFactory]:
    factory = FakeClientFactory(client)
    return AuthFlow(make_settings(), factory), factory


async def test_full_login_without_two_factor() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient(authorized=False)
    flow, _ = build(client)

    await flow.send_code(account_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    assert flow.is_pending(account_id) is True

    result = await flow.sign_in(account_id, "12345")

    assert result is not None
    assert result.tg_user_id == 424242
    assert result.username == "tester"
    assert result.display_name == "Тест Аккаунт"
    assert result.credentials.api_id == API_ID
    assert result.credentials.session_string.startswith("1")
    # Незавершённых входов не остаётся: клиент передан ClientManager.
    assert flow.is_pending(account_id) is False
    assert client.disconnect_calls == 1


async def test_two_factor_requires_second_step() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient(authorized=False, password_required=True)
    flow, _ = build(client)

    await flow.send_code(account_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    assert await flow.sign_in(account_id, "12345") is None
    assert flow.is_pending(account_id) is True, "попытка входа должна сохраниться"

    result = await flow.sign_in_password(account_id, "secret-2fa")

    assert result.tg_user_id == 424242
    assert client.signed_in_with["has_password"] is True
    assert flow.is_pending(account_id) is False


async def test_sign_in_without_code_request_is_rejected() -> None:
    flow, _ = build(FakeTelegramClient())

    with pytest.raises(InvalidInputError, match="запросите код"):
        await flow.sign_in(uuid.uuid4(), "12345")


async def test_second_code_request_replaces_the_first() -> None:
    account_id = uuid.uuid4()
    first = FakeTelegramClient(authorized=False)
    second = FakeTelegramClient(authorized=False)
    factory = FakeClientFactory()
    factory.preset(account_id, first)
    flow = AuthFlow(make_settings(), factory)

    await flow.send_code(account_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    factory.preset(account_id, second)
    await flow.send_code(account_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)

    assert first.disconnect_calls == 1, "прошлое соединение должно закрыться"
    assert flow.is_pending(account_id) is True


async def test_cancel_releases_the_connection() -> None:
    account_id = uuid.uuid4()
    client = FakeTelegramClient(authorized=False)
    flow, _ = build(client)

    await flow.send_code(account_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    await flow.cancel(account_id)

    assert flow.is_pending(account_id) is False
    assert client.disconnect_calls == 1


async def test_pending_repr_hides_phone_and_code_hash() -> None:
    """Трейсбек не должен раскрывать phone_code_hash и полный номер."""
    from app.telegram.auth_flow import _Pending

    pending = _Pending(
        account_id=uuid.uuid4(),
        phone=PHONE,
        phone_code_hash="very-secret-hash",
        client=FakeTelegramClient(),
        api_id=API_ID,
        api_hash=API_HASH,
        started_at=0.0,
    )

    text = repr(pending)
    assert "very-secret-hash" not in text
    assert PHONE not in text
    assert "+79" in text


async def test_shutdown_closes_all_pending_logins() -> None:
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    first, second = FakeTelegramClient(authorized=False), FakeTelegramClient(authorized=False)
    factory = FakeClientFactory()
    factory.preset(first_id, first)
    factory.preset(second_id, second)
    flow = AuthFlow(make_settings(), factory)

    await flow.send_code(first_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    await flow.send_code(second_id, phone=PHONE, api_id=API_ID, api_hash=API_HASH)
    await flow.shutdown()

    assert first.disconnect_calls == 1
    assert second.disconnect_calls == 1
    assert flow.is_pending(first_id) is False
    assert flow.is_pending(second_id) is False
