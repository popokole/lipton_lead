# Развёртывание на сервере

Telegram AI Automation Platform. Всё поднимается одним `docker compose`.

## Требования

- Linux-сервер с Docker и Docker Compose v2.
- Открытый порт для панели (по умолчанию 8080), лучше за reverse-proxy с HTTPS.
- Собственные `api_id` / `api_hash` с https://my.telegram.org.
- API-ключ AI-провайдера (OpenAI или совместимый агрегатор).

## Первый запуск

```bash
git clone https://github.com/popokole/lipton_lead.git
cd lipton_lead

# 1. Конфигурация
cp .env.example .env
# сгенерировать секреты:
#   openssl rand -base64 32   → SESSION_ENCRYPTION_KEY
#   openssl rand -hex 32      → JWT_SECRET
#   openssl rand -hex 16      → POSTGRES_PASSWORD
nano .env   # заполнить секреты, ADMIN_EMAIL/ADMIN_PASSWORD, ключи

# 2. Запуск (ТОЛЬКО базовый compose — без dev-override!)
docker compose -f docker-compose.yml up -d --build
```

Флаг `-f docker-compose.yml` обязателен: без него подхватится
`docker-compose.override.yml` с dev-настройками (hot-reload, открытые порты БД).

Порядок поднятия автоматический: postgres → migrate (миграции) → api, worker,
frontend, nginx. Первый администратор создаётся из `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

Панель: `http://<сервер>:8080` (или `HTTP_PORT` из .env).

## Сеть до Telegram и AI

Если сервер имеет прямой доступ к Telegram и провайдеру — ничего не нужно.
Если доступ режется — задайте прокси в `.env`:

```
AI_PROXY_URL=socks5://<host>:<port>     # запросы к AI через прокси
```

Прокси для Telegram-аккаунта настраивается в панели (Аккаунты → раздел «Прокси»).

## Обновление

```bash
git pull
docker compose -f docker-compose.yml up -d --build
```

Миграции применяются автоматически сервисом `migrate` перед стартом api/worker.

## Полезное

```bash
docker compose -f docker-compose.yml logs -f api worker    # логи
docker compose -f docker-compose.yml ps                    # статус
docker compose -f docker-compose.yml exec postgres \
  psql -U tgai -d tgai                                      # база
docker compose -f docker-compose.yml down                  # остановить
```

## Бэкапы

Данные — в volume `postgres-data`. Ключ шифрования сессий (`SESSION_ENCRYPTION_KEY`)
храните ОТДЕЛЬНО от бэкапа базы: вместе они сводят шифрование на нет.

```bash
docker compose -f docker-compose.yml exec postgres \
  pg_dump -U tgai tgai | gzip > backup-$(date +%F).sql.gz
```
