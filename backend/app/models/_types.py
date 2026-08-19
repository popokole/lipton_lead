"""Общие определения колонок.

Держим их в одном месте, чтобы одинаковые по смыслу поля не разъезжались по
типам между таблицами.
"""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> sa.Enum:
    """Нативный enum PostgreSQL, хранящий значения (а не имена) членов."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


# Telegram-идентификаторы давно вышли за пределы int32.
TelegramId = sa.BigInteger
# Ключ шифрования и nonce для полей, зашифрованных приложением.
Ciphertext = sa.LargeBinary
