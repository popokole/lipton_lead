# syntax=docker/dockerfile:1.7
# Python 3.12 выбран сознательно: под 3.13+ у части зависимостей (telethon,
# tiktoken, asyncpg) нет колёс, и сборка уезжает в компиляцию C-расширений.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/vendor \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libglib2.0-0 нужен Qt, на котором работает opentele — им читается tdata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS deps
COPY pyproject.toml ./
# Ставим только зависимости. Сам дистрибутив удаляем сразу после установки:
# код приезжает в /app следующим слоем и подхватывается через PYTHONPATH,
# а копия пакета в site-packages только создавала бы путаницу с шадоуингом.
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y telegram-ai-platform \
    && rm -rf app

# opentele ставится без зависимостей намеренно: он тянет tgcrypto — C-расширение
# без колёс под Python 3.12, которое здесь просто не собирается. Вместо него в
# vendor/ лежит чистый Python-эквивалент двух нужных функций AES-IGE.
RUN --mount=type=cache,target=/root/.cache/pip pip install opentele==1.15.1 --no-deps


FROM deps AS runtime
COPY alembic.ini ./
COPY alembic ./alembic
COPY vendor ./vendor
COPY app ./app

# Непривилегированный пользователь: процесс не должен иметь права писать в свой код.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM runtime AS dev
USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ".[dev]" && pip uninstall -y telegram-ai-platform
COPY tests ./tests
RUN chown -R appuser:appuser /app
USER appuser
