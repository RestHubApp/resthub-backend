"""Puertos de las cuentas de plataforma: su persistencia y el cifrado."""

from __future__ import annotations

from typing import Protocol

from resthub.modules.platform.domain.entities import PlatformAdmin


class PlatformAdminRepository(Protocol):
    async def add(self, admin: PlatformAdmin) -> PlatformAdmin:
        """Lanza `EmailAlreadyRegistered` si el correo ya tiene cuenta de plataforma."""
        ...

    async def get(self, admin_id: int) -> PlatformAdmin | None: ...

    async def get_by_email(self, email: str) -> PlatformAdmin | None: ...


class PasswordHasher(Protocol):
    def hash(self, plain_password: str) -> str: ...

    def verify(self, plain_password: str, password_hash: str) -> bool: ...

    def dummy_hash(self) -> str:
        """Hash que ninguna contraseña reproduce, para que un correo desconocido tarde lo mismo."""
        ...
