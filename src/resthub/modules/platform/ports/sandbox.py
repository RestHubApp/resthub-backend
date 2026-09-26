"""Lo que la vista previa necesita del local de muestra y de sus cuentas.

El local de muestra es un restaurante de `restaurants` con cuentas de
`accounts`, y los códigos de vista previa son credenciales de esas cuentas, así
que también los posee `accounts`. Como con el alta de restaurantes, leer se
hace por SQL (`adapters/persistence/directories.py`) y escribir lo hace la raíz
de composición detrás de `SandboxProvisioning` (`wiring/sandbox.py`), en la
sesión de la petición.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from resthub.core.permissions import RoleKind
from resthub.modules.platform.ports.restaurants import RestaurantSummary


class PreviewAs(StrEnum):
    """Como quién se abre la vista previa: la cuenta de muestra de ese tipo de rol."""

    OWNER = "owner"
    WAITER = "waiter"


@dataclass(frozen=True, slots=True)
class SandboxAccount:
    user_id: int
    kind: RoleKind
    role_label: str
    full_name: str


@dataclass(frozen=True, slots=True)
class IssuedPreviewCode:
    code: str
    expires_in_seconds: int


class SandboxCatalog(Protocol):
    async def current(self) -> RestaurantSummary | None:
        """El local de muestra vigente (activo), o `None` si todavía no hay."""
        ...

    async def accounts(self, restaurant_id: int) -> list[SandboxAccount]:
        """Sus cuentas activas: el encargado primero, el mesero después y el resto."""
        ...


class SandboxProvisioning(Protocol):
    """Errores: los de `domain/exceptions.py`, nunca los de los módulos que lo implementan."""

    async def create(self) -> int:
        """Un local de muestra nuevo con los datos de muestra; devuelve su id."""
        ...

    async def archive(self, restaurant_id: int) -> None:
        """Lo desactiva y le cambia el identificador corto, para que el nuevo use el suyo."""
        ...

    async def issue_preview_code(self, user_id: int, admin_id: int) -> IssuedPreviewCode:
        """Un código de un solo uso para entrar como esa cuenta del local de muestra.

        Lanza `SandboxAccountUnavailable` si la cuenta no es del local de muestra.
        """
        ...
