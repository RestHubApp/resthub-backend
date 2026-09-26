"""Lo que la plataforma necesita de los restaurantes y de su personal.

Esos datos los poseen `restaurants` y `accounts`, y este módulo no puede
importarlos. Leer se hace por SQL desde `adapters/persistence/directories.py`;
escribir (el alta con sus roles y su encargado, la edición, otro encargado) lo
hace la raíz de composición con los casos de uso de esos módulos, detrás de
`RestaurantProvisioning`. Todo corre en la sesión de la petición, así que un
alta que falla a mitad de camino no deja nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from resthub.core.pagination import Page, PageRequest


@dataclass(frozen=True, slots=True)
class RestaurantSummary:
    id: int
    name: str
    slug: str
    timezone: str
    is_active: bool
    created_at: datetime
    # Toda cuenta del local, activa o no.
    staff_count: int
    active_staff_count: int


@dataclass(frozen=True, slots=True)
class OwnerAccount:
    id: int
    full_name: str
    email: str
    is_active: bool


class RestaurantCatalog(Protocol):
    async def search(self, search_text: str | None, page: PageRequest) -> Page[RestaurantSummary]:
        """Por nombre o identificador corto sin distinguir mayúsculas, lo más nuevo primero."""
        ...

    async def get(self, restaurant_id: int) -> RestaurantSummary | None: ...

    async def owners(self, restaurant_id: int) -> list[OwnerAccount]:
        """Las cuentas del local cuyo rol es el de encargado."""
        ...


@dataclass(frozen=True, slots=True)
class NewOwner:
    full_name: str
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class NewRestaurant:
    name: str
    slug: str
    timezone: str
    owner: NewOwner


@dataclass(frozen=True, slots=True)
class RestaurantChanges:
    # `None` deja el campo como está.
    name: str | None = None
    timezone: str | None = None
    is_active: bool | None = None


class RestaurantProvisioning(Protocol):
    """Errores: los de `domain/exceptions.py`, nunca los de los módulos que lo implementan."""

    async def create(self, restaurant: NewRestaurant) -> int:
        """Restaurante, roles base y primer encargado; devuelve el id del restaurante."""
        ...

    async def update(self, restaurant_id: int, changes: RestaurantChanges) -> None: ...

    async def add_owner(self, restaurant_id: int, owner: NewOwner) -> OwnerAccount: ...
