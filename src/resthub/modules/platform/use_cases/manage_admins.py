"""Acceso y alta de las cuentas de la administración del sistema.

No hay registro público: la primera cuenta, y las que sigan, las crea
`scripts/create_platform_admin.py` con acceso directo a la base.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from resthub.core.identity import AccessToken, PlatformTokenService
from resthub.modules.platform.domain.entities import (
    PlatformActivityKind,
    PlatformAdmin,
    normalize_email,
    validate_new_password,
)
from resthub.modules.platform.domain.exceptions import (
    AdminUnavailable,
    EmailAlreadyRegistered,
    InvalidAccountData,
    InvalidCredentials,
)
from resthub.modules.platform.ports.activity_log import PlatformActivityLog
from resthub.modules.platform.ports.admin_repository import (
    PasswordHasher,
    PlatformAdminRepository,
)


@dataclass(frozen=True, slots=True)
class AuthenticateAdminCommand:
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    admin: PlatformAdmin
    token: AccessToken


class AuthenticateAdmin:
    def __init__(
        self,
        admins: PlatformAdminRepository,
        hasher: PasswordHasher,
        tokens: PlatformTokenService,
        activity: PlatformActivityLog,
    ) -> None:
        self._admins = admins
        self._hasher = hasher
        self._tokens = tokens
        self._activity = activity

    async def __call__(self, command: AuthenticateAdminCommand) -> AuthenticatedAdmin:
        admin = await self._find(command.email)
        # Igual que en el acceso del personal: se verifica siempre, en un hilo
        # aparte, para que un correo desconocido tarde lo mismo y bcrypt no
        # frene al resto de las peticiones.
        password_hash = admin.password_hash if admin else self._hasher.dummy_hash()
        matches = await asyncio.to_thread(self._hasher.verify, command.password, password_hash)
        # Una cuenta desactivada responde igual que una contraseña equivocada:
        # decir que existe ya es decir demasiado de una cuenta con este poder.
        if admin is None or admin.id is None or not matches or not admin.is_active:
            raise InvalidCredentials()

        await self._activity.record(admin.id, PlatformActivityKind.SIGNED_IN)
        return AuthenticatedAdmin(admin=admin, token=self._tokens.issue_platform(admin.id))

    async def _find(self, raw_email: str) -> PlatformAdmin | None:
        try:
            email = normalize_email(raw_email)
        except InvalidAccountData:
            return None
        return await self._admins.get_by_email(email)


class ReadCurrentAdmin:
    """La cuenta de quien pregunta, releída de la base en cada petición.

    Así desactivarla corta el acceso al instante, sin esperar a que venza el token.
    """

    def __init__(self, admins: PlatformAdminRepository) -> None:
        self._admins = admins

    async def __call__(self, admin_id: int) -> PlatformAdmin:
        admin = await self._admins.get(admin_id)
        if admin is None or not admin.is_active:
            raise AdminUnavailable(admin_id)
        return admin


@dataclass(frozen=True, slots=True)
class RegisterAdminCommand:
    email: str
    full_name: str
    password: str


class RegisterAdmin:
    def __init__(self, admins: PlatformAdminRepository, hasher: PasswordHasher) -> None:
        self._admins = admins
        self._hasher = hasher

    async def __call__(self, command: RegisterAdminCommand) -> PlatformAdmin:
        email = normalize_email(command.email)
        if await self._admins.get_by_email(email) is not None:
            raise EmailAlreadyRegistered(email)
        password_hash = await asyncio.to_thread(
            self._hasher.hash, validate_new_password(command.password)
        )
        return await self._admins.add(
            PlatformAdmin(email=email, full_name=command.full_name, password_hash=password_hash)
        )
