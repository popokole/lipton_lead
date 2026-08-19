"""Хеширование паролей (ТЗ §35).

Argon2id — текущий рекомендованный алгоритм для паролей. Параметры по
умолчанию из argon2-cffi подобраны под серверное железо; занижать их ради
скорости входа нельзя, в этом весь смысл.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.errors import InvalidInputError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Параметры Argon2 могли ужесточиться — пароль стоит перехешировать."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def validate_password_strength(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise InvalidInputError(f"Пароль должен быть не короче {min_length} символов")
    if password.isdigit() or password.isalpha():
        raise InvalidInputError("Пароль должен содержать и буквы, и цифры")
