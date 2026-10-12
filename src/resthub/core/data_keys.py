"""Llaves del cifrado en reposo: formato, generación e identificador.

Aparte de `crypto.py` porque `config.py` las valida al arrancar y `crypto.py`
lee la configuración. Ningún mensaje lleva el valor de una llave.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets

KEY_BYTES = 32
NONCE_BYTES = 12

# Llave de desarrollo y pruebas: pública a propósito, solo se admite con DEBUG.
DEV_KEY = hashlib.sha256(b"resthub-dev-only-data-key").digest()


class DataEncryptionError(Exception):
    """Un dato no se pudo cifrar o descifrar. El mensaje nunca lleva el dato."""


def parse_key(text: str) -> bytes:
    """Una llave de 32 bytes en base64 (normal o segura para URL)."""
    cleaned = text.strip()
    try:
        raw = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        message = "La llave de cifrado no es base64 válido."
        raise DataEncryptionError(message) from None
    if len(raw) != KEY_BYTES:
        message = f"La llave de cifrado debe tener {KEY_BYTES} bytes en base64."
        raise DataEncryptionError(message)
    return raw


def generate_key() -> str:
    """Una llave nueva lista para `DATA_ENCRYPTION_KEY`."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def key_id(key: bytes) -> str:
    """Identificador corto de una llave; no permite recuperarla."""
    return hashlib.sha256(b"resthub:key-id:" + key).hexdigest()[:8]


if __name__ == "__main__":  # pragma: no cover - utilidad de línea de comandos
    print(generate_key())  # noqa: T201 - la salida es la llave nueva
