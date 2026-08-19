"""REST API: вход, роли и работа с ресурсами (ТЗ §26, §35)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import Settings
from app.database.session import Database
from app.main import create_app
from app.models import User, UserRole
from app.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "PanelTest2026"


@pytest.fixture
async def app(integration_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(integration_settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def users(integration_settings: Settings) -> AsyncIterator[dict[UserRole, str]]:
    """Три пользователя с разными ролями и известным паролем."""
    database = Database(integration_settings)
    await database.connect()
    suffix = uuid.uuid4().hex[:8]
    emails = {
        role: f"{role.value.lower()}-{suffix}@example.com"
        for role in (UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    }

    async with database.session() as db:
        for role, email in emails.items():
            db.add(User(email=email, password_hash=hash_password(PASSWORD), role=role))

    yield emails

    async with database.session() as db:
        await db.execute(delete(User).where(User.email.in_(emails.values())))
    await database.disconnect()


async def token_for(client: AsyncClient, email: str) -> str:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAuthentication:
    async def test_login_returns_token_pair(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        response = await client.post(
            "/api/auth/login", json={"email": users[UserRole.ADMIN], "password": PASSWORD}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["access_token"] and body["refresh_token"]
        assert body["expires_in"] > 0

    async def test_wrong_password_is_rejected(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        response = await client.post(
            "/api/auth/login", json={"email": users[UserRole.ADMIN], "password": "nope"}
        )
        assert response.status_code == 401

    async def test_unknown_email_looks_the_same_as_wrong_password(
        self, client: AsyncClient
    ) -> None:
        """Иначе форма входа превращается в проверялку существующих адресов."""
        response = await client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Неверный email или пароль"

    async def test_me_returns_profile_without_password_hash(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.OPERATOR])
        response = await client.get("/api/auth/me", headers=auth(token))

        body = response.json()
        assert response.status_code == 200
        assert body["role"] == "OPERATOR"
        assert "password_hash" not in body

    async def test_refresh_issues_a_new_access_token(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        login = await client.post(
            "/api/auth/login", json={"email": users[UserRole.ADMIN], "password": PASSWORD}
        )
        refresh = login.json()["refresh_token"]

        response = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 200
        assert response.json()["access_token"]

    async def test_access_token_is_not_accepted_as_refresh(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.ADMIN])
        response = await client.post("/api/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401

    async def test_request_without_token_is_rejected(self, client: AsyncClient) -> None:
        assert (await client.get("/api/accounts")).status_code == 401

    async def test_garbage_token_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/api/accounts", headers=auth("not-a-token"))
        assert response.status_code == 401


class TestRoles:
    async def test_viewer_can_read(self, client: AsyncClient, users: dict[UserRole, str]) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        assert (await client.get("/api/accounts", headers=auth(token))).status_code == 200

    async def test_viewer_cannot_create_accounts(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        response = await client.post("/api/accounts", json={"label": "нельзя"}, headers=auth(token))
        assert response.status_code == 403

    async def test_operator_cannot_manage_users(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.OPERATOR])
        assert (await client.get("/api/auth/users", headers=auth(token))).status_code == 403

    async def test_admin_can_manage_users(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.ADMIN])
        assert (await client.get("/api/auth/users", headers=auth(token))).status_code == 200


class TestAccounts:
    async def test_create_and_delete(
        self, client: AsyncClient, users: dict[UserRole, str], integration_settings: Settings
    ) -> None:
        token = await token_for(client, users[UserRole.ADMIN])

        created = await client.post(
            "/api/accounts", json={"label": f"api-test-{uuid.uuid4().hex[:6]}"}, headers=auth(token)
        )
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert created.json()["status"] == "CREATED"
        # Полей сессии в ответе нет и быть не должно.
        assert "session" not in created.text.lower()

        listed = await client.get("/api/accounts", headers=auth(token))
        assert any(item["id"] == account_id for item in listed.json())

        removed = await client.delete(f"/api/accounts/{account_id}", headers=auth(token))
        assert removed.status_code == 200

    async def test_missing_account_returns_404(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.ADMIN])
        response = await client.get(f"/api/accounts/{uuid.uuid4()}", headers=auth(token))
        assert response.status_code == 404

    async def test_auth_command_without_worker_reports_clearly(
        self, client: AsyncClient, users: dict[UserRole, str], integration_settings: Settings
    ) -> None:
        """Команду некому доставить — ответ должен объяснять это, а не падать."""
        token = await token_for(client, users[UserRole.ADMIN])
        created = await client.post(
            "/api/accounts", json={"label": f"orphan-{uuid.uuid4().hex[:6]}"}, headers=auth(token)
        )
        account_id = created.json()["id"]

        response = await client.post(
            f"/api/accounts/{account_id}/send-code",
            json={"phone": "+79990000000", "api_id": 12345, "api_hash": "hash"},
            headers=auth(token),
        )

        assert response.status_code in {409, 503}
        await client.delete(f"/api/accounts/{account_id}", headers=auth(token))


class TestCatalog:
    async def test_scenario_and_rule_lifecycle(
        self, client: AsyncClient, users: dict[UserRole, str], integration_settings: Settings
    ) -> None:
        token = await token_for(client, users[UserRole.OPERATOR])
        suffix = uuid.uuid4().hex[:6]

        scenario = await client.post(
            "/api/scenarios",
            json={"name": f"Сценарий {suffix}", "system_prompt": "Отвечай коротко."},
            headers=auth(token),
        )
        assert scenario.status_code == 201
        scenario_id = scenario.json()["id"]

        rule = await client.post(
            "/api/rules",
            json={
                "name": f"Правило {suffix}",
                "scenario_id": scenario_id,
                "keywords": {"terms": ["нужен дизайнер"], "mode": "substring"},
                "ai_enabled": True,
                "ai_threshold": 0.8,
                "cooldown": {"user": 600},
            },
            headers=auth(token),
        )
        assert rule.status_code == 201, rule.text
        rule_id = rule.json()["id"]

        detail = await client.get(f"/api/rules/{rule_id}", headers=auth(token))
        assert detail.json()["account_ids"] == []

        disabled = await client.patch(
            f"/api/rules/{rule_id}", json={"enabled": False}, headers=auth(token)
        )
        assert disabled.json()["enabled"] is False

        await client.delete(f"/api/rules/{rule_id}", headers=auth(token))
        await client.delete(f"/api/scenarios/{scenario_id}", headers=auth(token))

    async def test_rule_with_ai_but_without_threshold_is_rejected(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.OPERATOR])
        response = await client.post(
            "/api/rules",
            json={"name": f"Плохое {uuid.uuid4().hex[:6]}", "ai_enabled": True},
            headers=auth(token),
        )
        assert response.status_code == 422


class TestListings:
    async def test_messages_are_paginated(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        response = await client.get("/api/messages?limit=5", headers=auth(token))

        body = response.json()
        assert response.status_code == 200
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["limit"] == 5

    async def test_dashboard_counters(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        response = await client.get("/api/analytics/dashboard", headers=auth(token))

        body = response.json()
        assert response.status_code == 200
        assert body["workers_healthy"] >= 0
        assert body["accounts_total"] >= 0

    async def test_series_returns_all_five_charts(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        response = await client.get("/api/analytics/series?days=7", headers=auth(token))

        assert response.status_code == 200
        assert set(response.json()) == {"messages", "matches", "replies", "leads", "errors"}

    async def test_workers_listing(self, client: AsyncClient, users: dict[UserRole, str]) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        assert (await client.get("/api/workers", headers=auth(token))).status_code == 200

    async def test_logs_leads_conversations_respond(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.VIEWER])
        for path in ("/api/logs?limit=10", "/api/leads", "/api/conversations", "/api/actions"):
            assert (await client.get(path, headers=auth(token))).status_code == 200, path


class TestRateLimit:
    async def test_login_attempts_are_throttled(self, integration_settings: Settings) -> None:
        """Перебор пароля должен упираться в лимит гораздо раньше обычной работы."""
        strict = integration_settings.model_copy(update={"rate_limit_login_per_minute": 3})
        application = create_app(strict)

        async with LifespanManager(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                statuses = [
                    (
                        await http.post(
                            "/api/auth/login",
                            json={"email": "attacker@example.com", "password": "guess"},
                        )
                    ).status_code
                    for _ in range(6)
                ]

        assert 429 in statuses, "лимит на вход не сработал"
        assert statuses.count(401) <= 3


class TestRuleValidationMessages:
    async def test_broken_regex_explains_the_problem(
        self, client: AsyncClient, users: dict[UserRole, str]
    ) -> None:
        token = await token_for(client, users[UserRole.OPERATOR])
        response = await client.post(
            "/api/rules",
            json={"name": f"Регэксп {uuid.uuid4().hex[:6]}", "regex": "(unclosed"},
            headers=auth(token),
        )

        assert response.status_code == 422
        assert "regex" in response.json()["error"]["message"].lower()
