"""Verificación en dos pasos con una aplicación de códigos (TOTP, RFS-22).

El código de seis dígitos que cambia cada 30 segundos (RFC 6238, HMAC-SHA1, como
lo esperan Google Authenticator, Microsoft Authenticator o Aegis). Sirve a las
dos clases de cuenta, por eso vive en el núcleo como `account_sessions`.

- El secreto se guarda cifrado con la llave de datos personales (`core/crypto.py`).
- Se activa en dos tiempos: el alta genera el secreto (la interfaz muestra el QR
  de `otpauth_uri`) y queda pendiente hasta que la cuenta escribe un código que
  lo confirma. Entonces se entregan diez códigos de recuperación, una sola vez.
- Un código ya usado no vuelve a servir (se guarda el último paso aceptado), y
  cada código de recuperación sirve una vez; se guardan solo como hash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, status
from sqlalchemy import JSON, DateTime, Integer, String, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.clock import Clock, utc_now
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.crypto import DataCipher, get_cipher
from resthub.core.database import Base
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError

ISSUER = "RestHub"
STEP_SECONDS = 30
DIGITS = 6
# Un paso antes y uno después: tolera el reloj del celular algo corrido.
DRIFT_STEPS = 1
SECRET_BYTES = 20
RECOVERY_CODES = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class TwoFactorCredentialRow(Base):
    __tablename__ = "two_factor_credentials"

    account_kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    secret: Mapped[str] = mapped_column(String(255))
    # Nulo mientras el alta espera el primer código que la confirme.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_code_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # El último paso de 30 s aceptado: un código ya usado no vuelve a servir.
    last_step: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


def totp(secret: str, step: int) -> str:
    """El código de seis dígitos del paso `step` (RFC 6238 sobre RFC 4226)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now: datetime) -> int:
    return int(now.timestamp()) // STEP_SECONDS


def matching_step(secret: str, code: str, now: datetime, after: int = 0) -> int | None:
    """El paso cuyo código es `code`, dentro de la tolerancia y posterior a `after`."""
    if not (code.isascii() and code.isdigit() and len(code) == DIGITS):
        return None
    step = current_step(now)
    for candidate in range(step - DRIFT_STEPS, step + DRIFT_STEPS + 1):
        if candidate > after and hmac.compare_digest(totp(secret, candidate), code):
            return candidate
    return None


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def otpauth_uri(secret: str, account: str) -> str:
    """Lo que codifica el QR que lee la aplicación de códigos."""
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&digits={DIGITS}&period={STEP_SECONDS}"


def new_recovery_codes() -> list[str]:
    """Diez códigos de diez caracteres, sin letras que se confundan (`0`/`o`, `1`/`l`)."""
    return [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        for _ in range(RECOVERY_CODES)
    ]


def _normalize_recovery(code: str) -> str:
    return code.replace("-", "").replace(" ", "").strip().lower()


def hash_recovery(code: str) -> str:
    """Los códigos son largos y aleatorios: un SHA-256 alcanza, no hace falta Argon2."""
    return hashlib.sha256(_normalize_recovery(code).encode()).hexdigest()


class SecondFactor(StrEnum):
    TOTP = "totp"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class TwoFactorStatus:
    enabled: bool
    pending: bool
    recovery_codes_left: int


@dataclass(slots=True)
class SqlTwoFactorStore:
    clock: Clock
    cipher: DataCipher

    async def _row(
        self, session: AsyncSession, account_kind: str, account_id: int
    ) -> TwoFactorCredentialRow | None:
        return await session.get(TwoFactorCredentialRow, (account_kind, account_id))

    async def status(
        self, session: AsyncSession, account_kind: str, account_id: int
    ) -> TwoFactorStatus:
        row = await self._row(session, account_kind, account_id)
        if row is None:
            return TwoFactorStatus(enabled=False, pending=False, recovery_codes_left=0)
        return TwoFactorStatus(
            enabled=row.confirmed_at is not None,
            pending=row.confirmed_at is None,
            recovery_codes_left=len(row.recovery_code_hashes or []),
        )

    async def is_enabled(self, session: AsyncSession, account_kind: str, account_id: int) -> bool:
        return (await self.status(session, account_kind, account_id)).enabled

    async def begin(self, session: AsyncSession, account_kind: str, account_id: int) -> str:
        """Un secreto nuevo, pendiente de confirmar. Reemplaza un alta a medio hacer."""
        secret = new_secret()
        await session.execute(
            delete(TwoFactorCredentialRow).where(
                TwoFactorCredentialRow.account_kind == account_kind,
                TwoFactorCredentialRow.account_id == account_id,
            )
        )
        session.add(
            TwoFactorCredentialRow(
                account_kind=account_kind,
                account_id=account_id,
                secret=self.cipher.encrypt(secret),
                confirmed_at=None,
                recovery_code_hashes=[],
                last_step=0,
                created_at=self.clock.now(),
            )
        )
        await session.flush()
        return secret

    async def confirm(
        self, session: AsyncSession, account_kind: str, account_id: int, code: str
    ) -> list[str] | None:
        """Activa el alta pendiente si el código es bueno y devuelve los códigos de recuperación."""
        row = await self._row(session, account_kind, account_id)
        if row is None or row.confirmed_at is not None:
            return None
        step = matching_step(self.cipher.decrypt(row.secret), code, self.clock.now())
        if step is None:
            return None
        codes = new_recovery_codes()
        row.confirmed_at = self.clock.now()
        row.last_step = step
        row.recovery_code_hashes = [hash_recovery(each) for each in codes]
        await session.flush()
        return codes

    async def renew_recovery_codes(
        self, session: AsyncSession, account_kind: str, account_id: int
    ) -> list[str] | None:
        row = await self._row(session, account_kind, account_id)
        if row is None or row.confirmed_at is None:
            return None
        codes = new_recovery_codes()
        row.recovery_code_hashes = [hash_recovery(each) for each in codes]
        await session.flush()
        return codes

    async def verify(
        self, session: AsyncSession, account_kind: str, account_id: int, code: str
    ) -> SecondFactor | None:
        """Qué factor sirvió: el código de la aplicación o uno de recuperación (que se gasta)."""
        row = await self._row(session, account_kind, account_id)
        if row is None or row.confirmed_at is None:
            return None
        secret = self.cipher.decrypt(row.secret)
        step = matching_step(secret, code.strip(), self.clock.now(), after=row.last_step)
        if step is not None:
            row.last_step = step
            await session.flush()
            return SecondFactor.TOTP
        hashed = hash_recovery(code)
        remaining = list(row.recovery_code_hashes or [])
        if not any(hmac.compare_digest(hashed, each) for each in remaining):
            return None
        row.recovery_code_hashes = [each for each in remaining if each != hashed]
        await session.flush()
        return SecondFactor.RECOVERY

    async def disable(self, session: AsyncSession, account_kind: str, account_id: int) -> bool:
        row = await self._row(session, account_kind, account_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True


async def check_second_factor(
    store: SqlTwoFactorStore,
    session: AsyncSession,
    account: tuple[str, int],
    code: str | None,
    *,
    on_failure: Callable[[], Awaitable[object]],
) -> SecondFactor | None:
    """El segundo paso del acceso (RFS-22); `None` si la cuenta no lo tiene.

    Corre después de la contraseña: pedir el código antes diría que la cuenta
    existe. El error deshace la petición, así que el acceso no queda anotado
    como correcto ni en la bitácora.
    """
    kind, account_id = account
    if not await store.is_enabled(session, kind, account_id):
        return None
    if not code:
        message = "Escribe el código de tu aplicación de verificación."
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            message,
            ErrorCode.TWO_FACTOR_REQUIRED,
            {"WWW-Authenticate": "Bearer"},
        )
    factor = await store.verify(session, kind, account_id, code)
    if factor is None:
        await on_failure()
        message = "El código de verificación no es correcto."
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            message,
            ErrorCode.INVALID_TWO_FACTOR_CODE,
            {"WWW-Authenticate": "Bearer"},
        )
    return factor


def get_two_factor_store(clock: ClockDep) -> SqlTwoFactorStore:
    return SqlTwoFactorStore(clock=clock, cipher=get_cipher())


TwoFactorStoreDep = Annotated[SqlTwoFactorStore, Depends(get_two_factor_store)]


def get_platform_two_factor_policy() -> bool:
    """Si la administración del sistema está obligada a usar la verificación en dos pasos."""
    return get_settings().platform_two_factor_required


PlatformTwoFactorPolicyDep = Annotated[bool, Depends(get_platform_two_factor_policy)]


def two_factor_setup_required() -> ApiError:
    message = "Activa la verificación en dos pasos para usar la administración del sistema."
    return ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.TWO_FACTOR_SETUP_REQUIRED)
