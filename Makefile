COMPOSE      := docker compose
COMPOSE_PROD := docker compose -f docker-compose.yml
RUN          := $(COMPOSE) run --rm --no-deps api

.PHONY: help up down restart build logs ps migrate revision test lint fmt typecheck check shell psql redis secrets

help:
	@echo "up         — поднять стек (dev)"
	@echo "down       — остановить стек"
	@echo "build      — пересобрать образы"
	@echo "logs       — логи api и worker"
	@echo "migrate    — применить миграции"
	@echo "revision   — создать миграцию: make revision m=\"описание\""
	@echo "test       — прогнать тесты"
	@echo "check      — lint + typecheck + test"
	@echo "secrets    — сгенерировать значения для .env"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart api worker

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f --tail=100 api worker

ps:
	$(COMPOSE) ps

migrate:
	$(COMPOSE) run --rm migrate

revision:
	@test -n "$(m)" || (echo "usage: make revision m=\"описание\"" && exit 1)
	$(COMPOSE) run --rm --no-deps api alembic revision --autogenerate -m "$(m)"

# Отдельная база Redis: тесты чистят реестр воркеров и не должны мешать
# живому воркеру, поднятому этим же стеком.
test:
	$(COMPOSE) run --rm -e REDIS_URL=redis://redis:6379/15 api pytest

lint:
	$(RUN) ruff check app tests

fmt:
	$(RUN) ruff format app tests
	$(RUN) ruff check --fix app tests

typecheck:
	$(RUN) mypy app

check: lint typecheck test

shell:
	$(COMPOSE) exec api bash

psql:
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-tgai} -d $${POSTGRES_DB:-tgai}

redis:
	$(COMPOSE) exec redis redis-cli

secrets:
	@echo "SESSION_ENCRYPTION_KEY=$$(openssl rand -base64 32)"
	@echo "JWT_SECRET=$$(openssl rand -hex 32)"
	@echo "POSTGRES_PASSWORD=$$(openssl rand -hex 16)"
