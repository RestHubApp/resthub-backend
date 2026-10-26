"""Cifrado en reposo de datos personales (Ley N.º 29733).

AES-256-GCM con un nonce aleatorio por valor. El texto cifrado se guarda como
`enc1:<id de llave>:<base64url(nonce + cifrado + etiqueta)>`: el id dice con qué
llave se cifró, así que una llave nueva convive con las anteriores mientras se
re-cifra la base (`scripts/rotate_data_key.py`).

Las búsquedas exactas (teléfono) no pueden mirar un valor cifrado con un nonce
distinto cada vez; usan un índice ciego, un HMAC-SHA256 del valor normalizado con
una llave derivada aparte de la de cifrado. El índice no se puede revertir, pero
sí compararse: quien tiene la llave calcula el de «987654321» y lo busca.

Ningún mensaje de error de este módulo lleva el dato ni la llave.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from resthub.core.config import get_settings
from resthub.core.data_keys import (
    DEV_KEY,
    NONCE_BYTES,
    DataEncryptionError,
    key_id,
    parse_key,
)

PREFIX = "enc1"
_PARTS = 3

_BLIND_INDEX_CONTEXT = b"resthub:blind-index:v1"


class DataCipher:
    def __init__(self, primary: bytes, previous: Sequence[bytes] = ()) -> None:
        self._primary_id = key_id(primary)
        self._ciphers = {key_id(key): AESGCM(key) for key in (*previous, primary)}
        self._blind_key = hmac.new(primary, _BLIND_INDEX_CONTEXT, hashlib.sha256).digest()

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return value.startswith(f"{PREFIX}:")

    def encrypt(self, plain: str) -> str:
        if plain == "":
            return ""
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._ciphers[self._primary_id].encrypt(nonce, plain.encode(), None)
        token = base64.urlsafe_b64encode(nonce + sealed).decode("ascii")
        return f"{PREFIX}:{self._primary_id}:{token}"

    def decrypt(self, value: str) -> str:
        """El texto en claro; sin prefijo se devuelve tal cual (fila anterior al cifrado)."""
        if not self.is_encrypted(value):
            return value
        parts = value.split(":", _PARTS - 1)
        if len(parts) != _PARTS:
            message = "Un dato cifrado está mal formado."
            raise DataEncryptionError(message)
        cipher = self._ciphers.get(parts[1])
        if cipher is None:
            message = "Un dato cifrado usa una llave que no está configurada."
            raise DataEncryptionError(message)
        try:
            raw = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
            return cipher.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None).decode()
        except (InvalidTag, binascii.Error, ValueError):
            message = "No se pudo descifrar un dato: la llave no corresponde o está alterado."
            raise DataEncryptionError(message) from None

    def needs_rotation(self, value: str) -> bool:
        """Verdadero si está en claro o cifrado con una llave que ya no es la vigente."""
        if value == "":
            return False
        return not value.startswith(f"{PREFIX}:{self._primary_id}:")

    def blind_index(self, value: str) -> str:
        """HMAC del valor ya normalizado, para buscar por igualdad sin descifrar."""
        if value == "":
            return ""
        return hmac.new(self._blind_key, value.encode(), hashlib.sha256).hexdigest()


@lru_cache
def _build(primary: str, previous: str) -> DataCipher:
    primary_key = parse_key(primary) if primary else DEV_KEY
    older = tuple(parse_key(part) for part in previous.split(",") if part.strip())
    return DataCipher(primary_key, older)


def get_cipher() -> DataCipher:
    settings = get_settings()
    return _build(
        settings.data_encryption_key.get_secret_value(),
        settings.data_encryption_previous_keys.get_secret_value(),
    )


def email_index(email: str) -> str:
    """Índice ciego de un correo: el mismo para «Ana@X.pe » y «ana@x.pe»."""
    return get_cipher().blind_index(email.strip().lower())
