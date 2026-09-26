"""Puerto de persistencia de cuentas.

Define *qué* necesita el negocio, nunca *cómo* se guarda. La implementación con
SQLAlchemy vive en `adapters/persistence` y esta capa no la conoce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.modules.accounts.domain.entities import User


@dataclass(frozen=True, slots=True)
class UserQuery:
    """Criterios de búsqueda del personal.

    `restaurant_id` es obligatorio y va primero: una búsqueda sin él no se
    puede escribir, que es la forma más barata de que ninguna devuelva cuentas
    de otro local.
    """

    restaurant_id: int
    ids: frozenset[int] | None = None
    role_ids: frozenset[int] | None = None
    search: str | None = None
    is_active: bool | None = None
    ordering: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class UserRepository(Protocol):
    async def add(self, user: User) -> User: ...

    async def get(self, user_id: int) -> User | None:
        """Sin acotar al restaurante: solo para la cuenta del propio principal."""
        ...

    async def get_in_restaurant(self, restaurant_id: int, user_id: int) -> User | None:
        """La cuenta, si pertenece a ese restaurante; si no, `None`."""
        ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def exists_with_email(self, email: str) -> bool: ...

    async def search(self, query: UserQuery) -> Page[User]: ...

    async def save(self, user: User) -> User: ...


class PasswordHasher(Protocol):
    """Puerto de cifrado. El dominio nunca ve la librería concreta."""

    def hash(self, plain_password: str) -> str: ...

    def verify(self, plain_password: str, password_hash: str) -> bool: ...

    def dummy_hash(self) -> str:
        """Hash válido que ninguna contraseña reproduce.

        Lo pide el negocio, no la criptografía: al autenticar un correo que no
        existe hay que gastar el mismo tiempo que con uno real, o la latencia
        de la respuesta delata qué cuentas están registradas.
        """
        ...
