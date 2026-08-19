"""Взято из прототипа tg-monitor и оставлено без изменений.

Pure-Python drop-in for the two `tgcrypto` functions that `opentele` needs
(AES-256 IGE encrypt/decrypt).

The real `tgcrypto` is a C extension with no wheel for recent Python versions,
so opentele's `import tgcrypto` fails. This shim lives on sys.path next to the
app and satisfies that import without a compiler. Only IGE-256 is implemented —
that's all opentele's TData reader uses.

Implemented directly on top of pyaes (bundled with Telethon) because Telethon's
own pure-Python `decrypt_ige` mishandles multi-block input.
"""
import pyaes


def _to_bytes(x) -> bytes:
    """Coerce bytes / bytearray / memoryview / PyQt5 QByteArray to plain bytes."""
    if isinstance(x, (bytes, bytearray, memoryview)):
        return bytes(x)
    data = getattr(x, "data", None)          # QByteArray.data() -> bytes
    if callable(data):
        return bytes(data())
    return bytes(x)


def _ecb_encrypt_block(aes: "pyaes.AES", block: bytes) -> bytes:
    return bytes(aes.encrypt(list(block)))


def _ecb_decrypt_block(aes: "pyaes.AES", block: bytes) -> bytes:
    return bytes(aes.decrypt(list(block)))


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def ige256_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data, key, iv = _to_bytes(data), _to_bytes(key), _to_bytes(iv)
    aes = pyaes.AES(key)
    prev_c, prev_m = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        m = data[i:i + 16]
        c = _xor(_ecb_encrypt_block(aes, _xor(m, prev_c)), prev_m)
        out += c
        prev_c, prev_m = c, m
    return bytes(out)


def ige256_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data, key, iv = _to_bytes(data), _to_bytes(key), _to_bytes(iv)
    aes = pyaes.AES(key)
    prev_c, prev_m = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        c = data[i:i + 16]
        m = _xor(_ecb_decrypt_block(aes, _xor(c, prev_m)), prev_c)
        out += m
        prev_c, prev_m = c, m
    return bytes(out)
