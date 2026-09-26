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

from resthub.modules.accounts.domain.entities import normalize_email

# 32 bytes al azar: 256 bits, inalcanzable por fuerza bruta aunque no haya
# límite de intentos. En texto URL-safe son 43 caracteres.
PREVIEW_CODE_BYTES = 32
# Lo que tarda la pestaña nueva en abrirse y canjearlo, con margen.
PREVIEW_CODE_TTL_SECONDS = 60
# Vencido o usado, un código ya no sirve para nada; se guarda un día por si hay
# que mirar qué pasó y después se borra. La bitácora de la plataforma conserva
# quién abrió cada vista previa.
PREVIEW_CODE_RETENTION_SECONDS = 24 * 60 * 60
# `.invalid` es un dominio reservado que no existe (RFC 2606): a estos correos
# no llega nada, y el acceso con contraseña ni siquiera los acepta.
SANDBOX_EMAIL_DOMAIN = "muestra.resthub.invalid"
# Lo que admite la parte local de un correo (RFC 5321). Con el sufijo y el
# dominio de muestra queda muy por debajo de los 254 de la columna.
_MAX_LOCAL_PART = 64


def new_preview_code() -> str:
    return secrets.token_urlsafe(PREVIEW_CODE_BYTES)


def preview_code_hash(code: str) -> str:
    """SHA-256 en hexadecimal. Sin sal ni bcrypt: el código ya es aleatorio y largo."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def sandbox_email(email: str, restaurant_id: int) -> str:
    """El correo con el que queda una cuenta del local de muestra.

    El correo es único en todo el sistema, las cuentas no se borran y a un
    local archivado ya no entra nadie: una cuenta creada desde la vista previa
    con un correo real lo ocuparía para siempre. Por eso se conserva lo que va
    antes de la arroba, se le agrega el id del local y se pasa al dominio de
    muestra, que nunca coincide con uno real: `ana@gmail.com` en el local 7 es
    `ana-7@muestra.resthub.invalid`.
    """
    local = normalize_email(email).partition("@")[0][:_MAX_LOCAL_PART]
    return f"{local}-{restaurant_id}@{SANDBOX_EMAIL_DOMAIN}"


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
