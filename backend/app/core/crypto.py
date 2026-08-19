"""Шифрование секретов на уровне приложения (AES-256-GCM).

Модель угроз: защита от утечки дампа PostgreSQL и бэкапов. Ключ живёт в
окружении процесса (env / docker secret), шифротексты — в БД, поэтому дамп
без ключа бесполезен. От компрометации самого хоста это не защищает, и
притворяться иначе не нужно.

`key_id` хранится рядом с шифротекстом, чтобы ключ можно было ротировать:
старые записи расшифровываются прежним ключом, новые пишутся текущим.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, decode_session_key, get_settings

ALGORITHM = "AES-256-GCM"
NONCE_SIZE = 12


class DecryptionError(RuntimeError):
    """Шифротекст не расшифровывается текущим набором ключей."""


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    """Результат шифрования. Хранится тремя колонками + алгоритм."""

    ciphertext: bytes
    nonce: bytes
    key_id: str
    alg: str = ALGORITHM

    def __repr__(self) -> str:  # секрет не должен всплыть в трейсбеке
        return f"EncryptedBlob(key_id={self.key_id!r}, bytes={len(self.ciphertext)})"


class SecretBox:
    """Шифрует и расшифровывает секреты приложения.

    `aad` (additional authenticated data) связывает шифротекст с его владельцем:
    сессия, зашифрованная для аккаунта A, не расшифруется как сессия аккаунта B,
    даже если строку в БД подменить.
    """

    def __init__(self, active_key_id: str, keys: dict[str, bytes]) -> None:
        if active_key_id not in keys:
            raise ValueError(f"active key {active_key_id!r} is missing from the key set")
        self._active_key_id = active_key_id
        self._keys = {key_id: AESGCM(key) for key_id, key in keys.items()}

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def encrypt(self, plaintext: bytes | str, *, aad: bytes | str = b"") -> EncryptedBlob:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        aad_bytes = aad.encode("utf-8") if isinstance(aad, str) else aad
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = self._keys[self._active_key_id].encrypt(nonce, data, aad_bytes)
        return EncryptedBlob(
            ciphertext=ciphertext, nonce=nonce, key_id=self._active_key_id, alg=ALGORITHM
        )

    def decrypt(self, blob: EncryptedBlob, *, aad: bytes | str = b"") -> bytes:
        if blob.alg != ALGORITHM:
            raise DecryptionError(f"unsupported algorithm: {blob.alg}")
        aead = self._keys.get(blob.key_id)
        if aead is None:
            raise DecryptionError(f"unknown key_id: {blob.key_id}")
        aad_bytes = aad.encode("utf-8") if isinstance(aad, str) else aad
        try:
            return aead.decrypt(blob.nonce, blob.ciphertext, aad_bytes)
        except InvalidTag as exc:
            raise DecryptionError("ciphertext authentication failed") from exc

    def decrypt_str(self, blob: EncryptedBlob, *, aad: bytes | str = b"") -> str:
        return self.decrypt(blob, aad=aad).decode("utf-8")

    def needs_rotation(self, blob: EncryptedBlob) -> bool:
        return blob.key_id != self._active_key_id


def build_secret_box(settings: Settings) -> SecretBox:
    """Собирает SecretBox из настроек.

    Дополнительные ключи для ротации задаются как
    ``SESSION_ENCRYPTION_KEYS_OLD=k0:<base64>,k00:<base64>``.
    """
    active_id = settings.session_encryption_key_id
    keys = {active_id: decode_session_key(settings.session_encryption_key.get_secret_value())}

    retired = os.getenv("SESSION_ENCRYPTION_KEYS_OLD", "").strip()
    for entry in filter(None, (item.strip() for item in retired.split(","))):
        key_id, _, raw = entry.partition(":")
        if not key_id or not raw:
            raise ValueError("SESSION_ENCRYPTION_KEYS_OLD entries must look like 'key_id:base64'")
        if key_id == active_id:
            raise ValueError(f"retired key {key_id!r} collides with the active key id")
        keys[key_id] = decode_session_key(raw)

    return SecretBox(active_key_id=active_id, keys=keys)


@lru_cache(maxsize=1)
def get_secret_box() -> SecretBox:
    return build_secret_box(get_settings())
