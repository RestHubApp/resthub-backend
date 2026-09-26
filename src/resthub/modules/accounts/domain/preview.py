"""Códigos de un solo uso para entrar a la vista previa.

La administración del sistema abre la aplicación como una cuenta del local de
muestra: pide un código, la pestaña nueva lo canjea por un token de vista
previa y el código deja de servir. El código viaja una sola vez, en la URL de
esa pestaña, así que tiene que ser imposible de adivinar, durar poco y no
quedar guardado en claro.

Python puro: `secrets` y `hashlib` son de la biblioteca estándar.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

# 32 bytes al azar: 256 bits, inalcanzable por fuerza bruta aunque no haya
# límite de intentos. En texto URL-safe son 43 caracteres.
PREVIEW_CODE_BYTES = 32
# Lo que tarda la pestaña nueva en abrirse y canjearlo, con margen.
PREVIEW_CODE_TTL_SECONDS = 60


def new_preview_code() -> str:
    return secrets.token_urlsafe(PREVIEW_CODE_BYTES)


def preview_code_hash(code: str) -> str:
    """SHA-256 en hexadecimal. Sin sal ni bcrypt: el código ya es aleatorio y largo."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def unusable_password_secret() -> str:
    """Un valor aleatorio para cifrar como contraseña y descartar.

    Las cuentas del local de muestra no tienen contraseña utilizable: nadie
    conoce el valor que produjo su hash.
    """
    return secrets.token_urlsafe(PREVIEW_CODE_BYTES)


@dataclass(frozen=True, slots=True)
class PreviewCode:
    code_hash: str
    user_id: int
    platform_admin_id: int
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreviewGrant:
    """Lo que habilita un código canjeado: entrar como esta cuenta, a pedido de esta persona."""

    user_id: int
    platform_admin_id: int
