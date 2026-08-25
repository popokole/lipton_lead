"""Единственный источник конфигурации приложения.

Все параметры читаются из окружения. В коде не должно быть ни одного
захардкоженного секрета, лимита или таймаута — только ссылка на settings.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["dev", "staging", "prod"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Приложение --------------------------------------------------------
    app_name: str = Field(default="telegram-ai-platform", alias="APP_NAME")
    env: Environment = Field(default="dev", alias="ENV")
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: LogFormat = Field(default="json", alias="LOG_FORMAT")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    # NoDecode: без него pydantic-settings пытается разобрать значение как JSON
    # и падает на обычном "http://a,http://b" ещё до нашего валидатора.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="CORS_ORIGINS")

    # --- PostgreSQL --------------------------------------------------------
    database_url: str = Field(alias="DATABASE_URL")
    db_pool_size: int = Field(default=10, ge=1, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, ge=0, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: float = Field(default=10.0, gt=0, alias="DB_POOL_TIMEOUT")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    # --- Redis -------------------------------------------------------------
    redis_url: str = Field(alias="REDIS_URL")
    redis_max_connections: int = Field(default=50, ge=1, alias="REDIS_MAX_CONNECTIONS")

    # --- Безопасность ------------------------------------------------------
    jwt_secret: SecretStr = Field(alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_ttl_minutes: int = Field(default=30, ge=1, alias="ACCESS_TOKEN_TTL_MINUTES")
    refresh_token_ttl_days: int = Field(default=14, ge=1, alias="REFRESH_TOKEN_TTL_DAYS")
    secure_cookies: bool = Field(default=True, alias="SECURE_COOKIES")
    password_min_length: int = Field(default=10, ge=8, alias="PASSWORD_MIN_LENGTH")
    rate_limit_per_minute: int = Field(default=120, ge=1, alias="RATE_LIMIT_PER_MINUTE")
    rate_limit_login_per_minute: int = Field(default=5, ge=1, alias="RATE_LIMIT_LOGIN_PER_MINUTE")

    # Ключ шифрования Telegram-сессий. Хранится вне БД (env / docker secret).
    session_encryption_key: SecretStr = Field(alias="SESSION_ENCRYPTION_KEY")
    session_encryption_key_id: str = Field(default="k1", alias="SESSION_ENCRYPTION_KEY_ID")

    # Первичный администратор, создаётся при первом запуске.
    admin_email: str = Field(alias="ADMIN_EMAIL")
    admin_password: SecretStr = Field(alias="ADMIN_PASSWORD")

    # --- Telegram ----------------------------------------------------------
    # Собственные credentials с my.telegram.org. Дефолтов нет намеренно:
    # использование чужого api_id со стороннего клиента нарушает ToS Telegram.
    telegram_api_id: int | None = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: SecretStr | None = Field(default=None, alias="TELEGRAM_API_HASH")
    telegram_device_model: str = Field(default="TG AI Platform", alias="TELEGRAM_DEVICE_MODEL")
    telegram_system_version: str = Field(default="1.0", alias="TELEGRAM_SYSTEM_VERSION")
    telegram_app_version: str = Field(default="1.0", alias="TELEGRAM_APP_VERSION")
    telegram_connect_timeout: float = Field(default=15.0, gt=0, alias="TELEGRAM_CONNECT_TIMEOUT")
    telegram_reconnect_base_delay: float = Field(
        default=2.0, gt=0, alias="TELEGRAM_RECONNECT_BASE_DELAY"
    )
    telegram_reconnect_max_delay: float = Field(
        default=300.0, gt=0, alias="TELEGRAM_RECONNECT_MAX_DELAY"
    )
    # Минимальный интервал между отправками одного аккаунта — защита самого
    # аккаунта от FloodWait, а не средство обхода ограничений Telegram.
    send_min_interval_seconds: float = Field(default=3.0, ge=0, alias="SEND_MIN_INTERVAL_SECONDS")
    # Перед отправкой ответа аккаунт отмечает сообщение прочитанным и
    # какое-то время «печатает» — мгновенный ответ от живого человека
    # выглядит подозрительно. Диапазон, а не фиксированное число, чтобы
    # пауза не была одинаковой каждый раз.
    reply_typing_delay_min_seconds: float = Field(
        default=5.0, ge=0, alias="REPLY_TYPING_DELAY_MIN_SECONDS"
    )
    reply_typing_delay_max_seconds: float = Field(
        default=8.0, ge=0, alias="REPLY_TYPING_DELAY_MAX_SECONDS"
    )
    flood_wait_max_seconds: int = Field(default=600, ge=0, alias="FLOOD_WAIT_MAX_SECONDS")
    # tdata-папка Telegram Desktop обычно занимает единицы мегабайт;
    # запас нужен на профили с несколькими аккаунтами и кешем.
    tdata_max_upload_mb: int = Field(default=64, ge=1, alias="TDATA_MAX_UPLOAD_MB")

    # --- AI ----------------------------------------------------------------
    ai_provider: str = Field(default="openai", alias="AI_PROVIDER")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    # chat — обычный /chat/completions; responses — только /responses.
    # У агрегаторов вроде codex.sale доступен лишь второй, и отвечает он
    # потоком SSE, поэтому транспорт принципиально разный.
    ai_wire_api: Literal["chat", "responses"] = Field(default="chat", alias="AI_WIRE_API")
    # Глубина рассуждений у моделей, которые её поддерживают. Для отбора
    # сообщений хватает низкой: там нужна скорость и воспроизводимость.
    ai_reasoning_effort: str | None = Field(default=None, alias="AI_REASONING_EFFORT")
    ai_retry_attempts: int = Field(default=3, ge=1, le=10, alias="AI_RETRY_ATTEMPTS")
    # Предельное время на один запрос к AI со всеми повторами и запасными
    # моделями. Собеседник не станет ждать минуты ради ответа в чате.
    ai_total_budget_seconds: float = Field(default=60.0, gt=0, alias="AI_TOTAL_BUDGET_SECONDS")
    # Отдельный прокси для запросов к AI. Нужен там, где провайдер режется
    # по пути: в контейнере это виднее всего — TCP открывается, а HTTP виснет.
    ai_proxy_url: str | None = Field(default=None, alias="AI_PROXY_URL")
    # Запасные модели на случай, когда основная занята. Перебираются по
    # порядку; пустой список означает «работать только основной».
    ai_fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="AI_FALLBACK_MODELS"
    )
    default_ai_model: str = Field(default="gpt-4o-mini", alias="DEFAULT_AI_MODEL")
    default_ai_temperature: float = Field(default=0.6, ge=0, le=2, alias="DEFAULT_AI_TEMPERATURE")
    default_ai_max_tokens: int = Field(default=600, ge=1, alias="DEFAULT_AI_MAX_TOKENS")
    ai_timeout_seconds: float = Field(default=45.0, gt=0, alias="AI_TIMEOUT_SECONDS")
    ai_max_concurrency: int = Field(default=8, ge=1, alias="AI_MAX_CONCURRENCY")
    ai_daily_budget_usd: float = Field(default=0.0, ge=0, alias="AI_DAILY_BUDGET_USD")
    # Цены модели задаются настройкой, а не таблицей в коде: прайс-листы
    # меняются, и зашитые числа быстро начинают врать в отчётах.
    ai_price_input_per_1m_usd: float = Field(default=0.0, ge=0, alias="AI_PRICE_INPUT_PER_1M_USD")
    ai_price_output_per_1m_usd: float = Field(default=0.0, ge=0, alias="AI_PRICE_OUTPUT_PER_1M_USD")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1536, ge=1, alias="EMBEDDING_DIM")

    # --- Pipeline ----------------------------------------------------------
    max_context_messages: int = Field(default=20, ge=1, alias="MAX_CONTEXT_MESSAGES")
    max_message_length: int = Field(default=4000, ge=1, alias="MAX_MESSAGE_LENGTH")
    default_cooldown_seconds: int = Field(default=600, ge=0, alias="DEFAULT_COOLDOWN_SECONDS")
    # Анти-бан: минимум секунд между нашими ответами в ОДИН чат (группу). 0 —
    # выключено. Чаты с флагом cooldown_exempt не ограничиваются.
    chat_reply_cooldown_seconds: int = Field(
        default=300, ge=0, alias="CHAT_REPLY_COOLDOWN_SECONDS"
    )
    max_consecutive_ai_replies: int = Field(default=3, ge=1, alias="MAX_CONSECUTIVE_AI_REPLIES")
    # Рабочие часы (по-человечески: не отвечаем ночью). Час начала и конца в
    # часовом поясе work_hours_tz_offset. Если start == end — режим выключен.
    work_hours_start: int = Field(default=0, ge=0, le=23, alias="WORK_HOURS_START")
    work_hours_end: int = Field(default=0, ge=0, le=24, alias="WORK_HOURS_END")
    work_hours_tz_offset: int = Field(default=3, ge=-12, le=14, alias="WORK_HOURS_TZ_OFFSET")
    # Антидубликат: не слать один и тот же текст с аккаунта чаще, чем раз в
    # это окно — иначе повтор палит бота. При совпадении текст слегка меняется.
    anti_duplicate_ttl_seconds: int = Field(
        default=21600, ge=0, alias="ANTI_DUPLICATE_TTL_SECONDS"
    )
    # Час (в work_hours_tz_offset), когда бот шлёт дневной дайджест в лог-чат.
    digest_hour: int = Field(default=21, ge=0, le=23, alias="DIGEST_HOUR")
    summary_every_n_messages: int = Field(default=15, ge=1, alias="SUMMARY_EVERY_N_MESSAGES")
    duplicate_window_seconds: int = Field(default=86400, ge=1, alias="DUPLICATE_WINDOW_SECONDS")

    # --- Knowledge base ----------------------------------------------------
    kb_max_file_size_mb: int = Field(default=20, ge=1, alias="KB_MAX_FILE_SIZE_MB")
    kb_chunk_size: int = Field(default=800, ge=100, alias="KB_CHUNK_SIZE")
    kb_chunk_overlap: int = Field(default=120, ge=0, alias="KB_CHUNK_OVERLAP")
    kb_similarity_threshold: float = Field(
        default=0.75, ge=0, le=1, alias="KB_SIMILARITY_THRESHOLD"
    )
    kb_max_chunks: int = Field(default=5, ge=1, alias="KB_MAX_CHUNKS")

    # --- Worker ------------------------------------------------------------
    worker_name: str = Field(default="worker-1", alias="WORKER_NAME")
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, alias="WORKER_HEARTBEAT_SECONDS")
    account_lease_ttl_seconds: int = Field(default=30, ge=5, alias="ACCOUNT_LEASE_TTL_SECONDS")
    worker_max_accounts: int = Field(default=25, ge=1, alias="WORKER_MAX_ACCOUNTS")
    worker_stale_after_seconds: int = Field(default=45, ge=5, alias="WORKER_STALE_AFTER_SECONDS")

    # --- Retention ---------------------------------------------------------
    message_retention_days: int = Field(default=90, ge=1, alias="MESSAGE_RETENTION_DAYS")
    event_log_retention_days: int = Field(default=30, ge=1, alias="EVENT_LOG_RETENTION_DAYS")

    # --- Валидация ---------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _drop_empty_values(cls, data: Any) -> Any:
        """Пустое значение в .env означает «не задано», а не пустую строку.

        Без этого `TELEGRAM_API_ID=` из шаблона .env.example ломает разбор
        конфигурации, вместо того чтобы оставить поле незаполненным.
        """
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    @field_validator("cors_origins", "ai_fallback_models", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported LOG_LEVEL: {value}")
        return level

    @field_validator("session_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        decode_session_key(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _validate_invariants(self) -> Settings:
        if self.kb_chunk_overlap >= self.kb_chunk_size:
            raise ValueError("KB_CHUNK_OVERLAP must be smaller than KB_CHUNK_SIZE")
        if self.telegram_reconnect_max_delay < self.telegram_reconnect_base_delay:
            raise ValueError(
                "TELEGRAM_RECONNECT_MAX_DELAY must be >= TELEGRAM_RECONNECT_BASE_DELAY"
            )
        if self.account_lease_ttl_seconds <= self.worker_heartbeat_seconds:
            raise ValueError("ACCOUNT_LEASE_TTL_SECONDS must exceed WORKER_HEARTBEAT_SECONDS")
        if self.env == "prod":
            if len(self.jwt_secret.get_secret_value()) < 32:
                raise ValueError("JWT_SECRET must be at least 32 characters in production")
            if self.debug:
                raise ValueError("DEBUG must be disabled in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    @property
    def kb_max_file_size_bytes(self) -> int:
        return self.kb_max_file_size_mb * 1024 * 1024

    @property
    def tdata_max_upload_bytes(self) -> int:
        return self.tdata_max_upload_mb * 1024 * 1024


def decode_session_key(raw: str) -> bytes:
    """Разбирает SESSION_ENCRYPTION_KEY в 32 байта для AES-256-GCM.

    Принимает base64 (обычный или urlsafe) и hex. Любой другой формат — ошибка
    конфигурации: молча урезать или дополнять ключ нельзя.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("SESSION_ENCRYPTION_KEY is empty")

    for decoder in (_b64_decode, _hex_decode):
        try:
            key = decoder(candidate)
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key

    raise ValueError(
        "SESSION_ENCRYPTION_KEY must decode to exactly 32 bytes (base64 or hex); "
        "generate one with: openssl rand -base64 32"
    )


def _b64_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def _hex_decode(value: str) -> bytes:
    return bytes.fromhex(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
