from __future__ import annotations

import base64

import pytest

from app.core.crypto import ALGORITHM, DecryptionError, EncryptedBlob, SecretBox, build_secret_box
from tests.conftest import TEST_ENCRYPTION_KEY, make_settings

KEY_A = b"A" * 32
KEY_B = b"B" * 32


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(active_key_id="k1", keys={"k1": KEY_A})


class TestRoundTrip:
    def test_encrypt_decrypt_bytes(self, box: SecretBox) -> None:
        blob = box.encrypt(b"session-payload")
        assert box.decrypt(blob) == b"session-payload"

    def test_encrypt_decrypt_str(self, box: SecretBox) -> None:
        blob = box.encrypt("1BVtsOHkBu0")
        assert box.decrypt_str(blob) == "1BVtsOHkBu0"

    def test_ciphertext_differs_from_plaintext(self, box: SecretBox) -> None:
        blob = box.encrypt(b"session-payload")
        assert b"session-payload" not in blob.ciphertext
        assert blob.alg == ALGORITHM
        assert blob.key_id == "k1"

    def test_nonce_is_unique_per_call(self, box: SecretBox) -> None:
        nonces = {box.encrypt(b"same").nonce for _ in range(50)}
        assert len(nonces) == 50


class TestAad:
    """AAD привязывает шифротекст к владельцу: подмена строки в БД не сработает."""

    def test_matching_aad_decrypts(self, box: SecretBox) -> None:
        blob = box.encrypt(b"payload", aad="account:1")
        assert box.decrypt(blob, aad="account:1") == b"payload"

    def test_foreign_aad_rejected(self, box: SecretBox) -> None:
        blob = box.encrypt(b"payload", aad="account:1")
        with pytest.raises(DecryptionError):
            box.decrypt(blob, aad="account:2")

    def test_missing_aad_rejected(self, box: SecretBox) -> None:
        blob = box.encrypt(b"payload", aad="account:1")
        with pytest.raises(DecryptionError):
            box.decrypt(blob)


class TestTampering:
    def test_modified_ciphertext_rejected(self, box: SecretBox) -> None:
        blob = box.encrypt(b"payload")
        tampered = EncryptedBlob(
            ciphertext=bytes([blob.ciphertext[0] ^ 0xFF]) + blob.ciphertext[1:],
            nonce=blob.nonce,
            key_id=blob.key_id,
        )
        with pytest.raises(DecryptionError):
            box.decrypt(tampered)

    def test_wrong_key_rejected(self) -> None:
        blob = SecretBox("k1", {"k1": KEY_A}).encrypt(b"payload")
        with pytest.raises(DecryptionError):
            SecretBox("k1", {"k1": KEY_B}).decrypt(blob)

    def test_unknown_key_id_rejected(self, box: SecretBox) -> None:
        blob = EncryptedBlob(ciphertext=b"x", nonce=b"y" * 12, key_id="k99")
        with pytest.raises(DecryptionError, match="unknown key_id"):
            box.decrypt(blob)

    def test_unknown_algorithm_rejected(self, box: SecretBox) -> None:
        blob = EncryptedBlob(ciphertext=b"x", nonce=b"y" * 12, key_id="k1", alg="ROT13")
        with pytest.raises(DecryptionError, match="unsupported algorithm"):
            box.decrypt(blob)


class TestRotation:
    def test_old_key_still_decrypts(self) -> None:
        old_blob = SecretBox("k0", {"k0": KEY_A}).encrypt(b"payload")
        rotated = SecretBox("k1", {"k1": KEY_B, "k0": KEY_A})
        assert rotated.decrypt(old_blob) == b"payload"
        assert rotated.needs_rotation(old_blob) is True

    def test_new_writes_use_active_key(self) -> None:
        rotated = SecretBox("k1", {"k1": KEY_B, "k0": KEY_A})
        blob = rotated.encrypt(b"payload")
        assert blob.key_id == "k1"
        assert rotated.needs_rotation(blob) is False

    def test_active_key_must_be_present(self) -> None:
        with pytest.raises(ValueError, match="missing from the key set"):
            SecretBox("k9", {"k1": KEY_A})


class TestBuildFromSettings:
    def test_builds_with_active_key_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SESSION_ENCRYPTION_KEYS_OLD", raising=False)
        box = build_secret_box(make_settings())
        assert box.active_key_id == "k1"
        assert box.decrypt(box.encrypt(b"x")) == b"x"

    def test_loads_retired_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        retired = base64.b64encode(KEY_B).decode()
        monkeypatch.setenv("SESSION_ENCRYPTION_KEYS_OLD", f"k0:{retired}")
        box = build_secret_box(make_settings(session_encryption_key=TEST_ENCRYPTION_KEY))
        old_blob = SecretBox("k0", {"k0": KEY_B}).encrypt(b"payload")
        assert box.decrypt(old_blob) == b"payload"

    def test_retired_key_cannot_reuse_active_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_ENCRYPTION_KEYS_OLD", f"k1:{base64.b64encode(KEY_B).decode()}")
        with pytest.raises(ValueError, match="collides"):
            build_secret_box(make_settings())

    def test_malformed_retired_entry_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_ENCRYPTION_KEYS_OLD", "garbage")
        with pytest.raises(ValueError, match="key_id:base64"):
            build_secret_box(make_settings())


def test_blob_repr_hides_ciphertext(box: SecretBox) -> None:
    blob = box.encrypt(b"very-secret-session")
    assert "very-secret-session" not in repr(blob)
    assert str(blob.ciphertext) not in repr(blob)
