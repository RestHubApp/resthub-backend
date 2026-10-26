"""Verificación en dos pasos con una aplicación de códigos (TOTP, RFS-22).

El código de seis dígitos que cambia cada 30 segundos (RFC 6238, HMAC-SHA1, como
lo esperan Google Authenticator, Microsoft Authenticator o Aegis). Sirve a las
dos clases de cuenta, por eso vive en el núcleo como `account_sessions`.

- El secreto se guarda cifrado con la llave de datos personales (`EncryptedText`,
  `core/crypto.py`), así que la rotación de la llave también lo re-cifra.
- Se activa en dos tiempos: el alta genera el secreto (la interfaz muestra el QR
  de `otpauth_uri`) y queda pendiente hasta que la cuenta escribe un código que
  lo confirma. Entonces se entregan diez códigos de recuperación, una sola vez.
- Un código ya usado no vuelve a servir (se guarda el último paso aceptado), y
  cada código de recuperación sirve una vez; se guardan solo como hash.
- Los códigos de recuperación son 28 caracteres al azar de un alfabeto de 31
  (unos 139 bits, sobre los 128 de ASVS 11.5.1), en siete grupos: `K7QM-4XWP-...`.
  Con ese azar un SHA-256 sin sal basta (ASVS 6.5.2): no hay diccionario que
  recorrer. Los de antes (24 caracteres, unos 119 bits, y los primeros, de 10)
  siguen sirviendo hasta que la cuenta los renueve; `recovery_codes_outdated` lo
  avisa.
- Gastar un código es una actualización condicionada (el paso tiene que ser
  posterior al guardado y la lista de recuperación la misma que se leyó), y la
  fila se lee con `FOR UPDATE`: dos accesos simultáneos con el mismo código no
  entran los dos (ASVS 6.5.1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, status
from sqlalchemy import (
    JSON,
    ColumnElement,
    CursorResult,
    DateTime,
    Integer,
    Select,
    String,
    Text,
    cast,
    delete,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.util import identity_key

from resthub.core.clock import Clock, utc_now
from resthub.core.clock_dependency import ClockDep
from resthub.core.config import get_settings
from resthub.core.database import Base
from resthub.core.encrypted_types import EncryptedText
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError

ISSUER = "RestHub"
STEP_SECONDS = 30
DIGITS = 6
# Un paso antes y uno después: tolera el reloj del celular algo corrido.
DRIFT_STEPS = 1
SECRET_BYTES = 20
RECOVERY_CODES = 10
# Sin las letras ni los números que se confunden al copiarlos a mano (`0`/`o`, `1`/`l`/`i`).
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_GROUPS = 7
RECOVERY_GROUP_LENGTH = 4
# Los hashes de los códigos de 28 caracteres llevan este prefijo; los de 24, `v2:`, y
# los de 10, ninguno.
_RECOVERY_HASH_PREFIX = "v3:"
_OLD_RECOVERY_HASH_PREFIXES = ("v2:",)
# Lo que se ignora al escribir un código: guiones (también los tipográficos) y espacios.
_RECOVERY_SEPARATORS = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2212_")


class TwoFactorCredentialRow(Base):
    __tablename__ = "two_factor_credentials"

    account_kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Cifrado con la llave de datos personales como toda columna `EncryptedText`: entra en
    # la rotación de la llave (`scripts/rotate_data_key.py`).
    secret: Mapped[str] = mapped_column(EncryptedText)
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


def new_recovery_code() -> str:
    """Un código de 28 caracteres en siete grupos de cuatro (`K7QM-4XWP-...`), unos 139 bits."""
    groups = (
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LENGTH))
        for _ in range(RECOVERY_GROUPS)
    )
    return "-".join(groups).upper()


def new_recovery_codes() -> list[str]:
    return [new_recovery_code() for _ in range(RECOVERY_CODES)]


def normalize_recovery(code: str) -> str:
    """Sin guiones, espacios ni mayúsculas: `k7qm 4xwp…` es el mismo código que `K7QM-4XWP-…`."""
    return "".join(
        char for char in code.lower() if not char.isspace() and char not in _RECOVERY_SEPARATORS
    )


def _digest(code: str) -> str:
    return hashlib.sha256(normalize_recovery(code).encode()).hexdigest()


def hash_recovery(code: str) -> str:
    """El hash que se guarda de un código nuevo.

    Los códigos tienen unos 139 bits de azar: un SHA-256 alcanza (ASVS 6.5.2), no
    hace falta Argon2 ni sal. El prefijo distingue los nuevos de los de antes, que
    tienen el mismo hash con otro prefijo o sin él.
    """
    return _RECOVERY_HASH_PREFIX + _digest(code)


def recovery_hash_matches(code: str, stored: str) -> bool:
    """Si `code` es el del hash guardado, sea un código nuevo o uno de antes."""
    digest = _digest(code)
    for prefix in (_RECOVERY_HASH_PREFIX, *_OLD_RECOVERY_HASH_PREFIXES):
        stored = stored.removeprefix(prefix)
    return hmac.compare_digest(stored, digest)


def is_outdated_recovery_hash(stored: str) -> bool:
    """Un hash de un código de antes (de 24 o de 10 caracteres), que la cuenta debería renovar."""
    return not stored.startswith(_RECOVERY_HASH_PREFIX)


def _credential_key(account_kind: str, account_id: int) -> tuple[ColumnElement[bool], ...]:
    return (
        TwoFactorCredentialRow.account_kind == account_kind,
        TwoFactorCredentialRow.account_id == account_id,
    )


def locked_credential(account_kind: str, account_id: int) -> Select[str, datetime | None, int, str]:
    """La fila de la cuenta, bloqueada hasta el fin de la transacción.

    Los hashes de recuperación se leen también como el texto guardado: la
    actualización que gasta uno se condiciona a que sigan siendo ese mismo texto.
    """
    return (
        select(
            TwoFactorCredentialRow.secret,
            TwoFactorCredentialRow.confirmed_at,
            TwoFactorCredentialRow.last_step,
            cast(TwoFactorCredentialRow.recovery_code_hashes, Text).label("raw_hashes"),
        )
        .where(*_credential_key(account_kind, account_id))
        .with_for_update()
    )


async def _spend(
    session: AsyncSession,
    account_kind: str,
    account_id: int,
    *,
    guard: ColumnElement[bool],
    values: dict[str, Any],
) -> bool:
    """Gasta un código si la fila sigue como se leyó; falso si otro acceso se adelantó."""
    result = await session.execute(
        update(TwoFactorCredentialRow)
        .where(*_credential_key(account_kind, account_id), guard)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    spent = isinstance(result, CursorResult) and result.rowcount == 1
    # La fila que la sesión ya tenía cargada (el estado de recién) queda al día.
    loaded = session.identity_map.get(
        identity_key(TwoFactorCredentialRow, (account_kind, account_id))
    )
    if loaded is not None:
        await session.refresh(loaded)
    return spent


class SecondFactor(StrEnum):
    TOTP = "totp"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class TwoFactorStatus:
    enabled: bool
    pending: bool
    recovery_codes_left: int
    # Le quedan códigos de los cortos de antes: conviene renovarlos.
    recovery_codes_outdated: bool = False


@dataclass(slots=True)
class SqlTwoFactorStore:
    clock: Clock

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
        hashes = row.recovery_code_hashes or []
        return TwoFactorStatus(
            enabled=row.confirmed_at is not None,
            pending=row.confirmed_at is None,
            recovery_codes_left=len(hashes),
            recovery_codes_outdated=any(is_outdated_recovery_hash(each) for each in hashes),
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
                secret=secret,
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
        step = matching_step(row.secret, code, self.clock.now())
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
        """Qué factor sirvió: el código de la aplicación o uno de recuperación (que se gasta).

        La fila se lee con `FOR UPDATE` (en PostgreSQL, el segundo acceso simultáneo
        espera al primero) y se gasta con una actualización condicionada a lo que se
        leyó: si otro acceso ya usó ese paso o ese código, no cambia ninguna fila y
        el código no sirve. Así ningún código entra dos veces (ASVS 6.5.1).
        """
        found = (await session.execute(locked_credential(account_kind, account_id))).one_or_none()
        if found is None or found.confirmed_at is None:
            return None
        step = matching_step(found.secret, code.strip(), self.clock.now(), after=found.last_step)
        if step is not None:
            spent = await _spend(
                session,
                account_kind,
                account_id,
                guard=TwoFactorCredentialRow.last_step < step,
                values={"last_step": step},
            )
            return SecondFactor.TOTP if spent else None
        remaining: list[str] = json.loads(found.raw_hashes or "[]")
        used = next((each for each in remaining if recovery_hash_matches(code, each)), None)
        if used is None:
            return None
        spent = await _spend(
            session,
            account_kind,
            account_id,
            guard=cast(TwoFactorCredentialRow.recovery_code_hashes, Text) == found.raw_hashes,
            values={"recovery_code_hashes": [each for each in remaining if each != used]},
        )
        return SecondFactor.RECOVERY if spent else None

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
    return SqlTwoFactorStore(clock=clock)


TwoFactorStoreDep = Annotated[SqlTwoFactorStore, Depends(get_two_factor_store)]


def get_platform_two_factor_policy() -> bool:
    """Si la administración del sistema está obligada a usar la verificación en dos pasos."""
    return get_settings().platform_two_factor_required


PlatformTwoFactorPolicyDep = Annotated[bool, Depends(get_platform_two_factor_policy)]


def two_factor_setup_required() -> ApiError:
    message = "Activa la verificación en dos pasos para usar la administración del sistema."
    return ApiError(status.HTTP_403_FORBIDDEN, message, ErrorCode.TWO_FACTOR_SETUP_REQUIRED)
