"""Puerto de persistencia de los roles de cada restaurante."""

from __future__ import annotations

from typing import Protocol

from resthub.core.permissions import RoleKind
from resthub.modules.accounts.domain.roles import Role


class RoleRepository(Protocol):
    async def add(self, role: Role) -> Role:
        """Lanza `RoleNameTaken` si el restaurante ya tiene un rol con ese nombre."""
        ...

    async def get_in_restaurant(
        self, restaurant_id: int, role_id: int, *, for_update: bool = False
    ) -> Role | None:
        """El rol, si pertenece a ese restaurante; si no, `None`."""
        ...

    async def find_by_kind(self, restaurant_id: int, kind: RoleKind) -> Role | None: ...

    async def list_for_restaurant(self, restaurant_id: int) -> list[Role]: ...

    async def exists_with_name(
        self, restaurant_id: int, name_key: str, *, excluding_id: int | None = None
    ) -> bool: ...

    async def member_counts(self, restaurant_id: int) -> dict[int, int]:
        """Cuántas cuentas, activas o no, tiene cada rol del restaurante."""
        ...

    async def member_ids(self, restaurant_id: int, role_id: int) -> frozenset[int]: ...

    async def save(self, role: Role) -> Role: ...

    async def delete(self, role: Role) -> None:
        """Lanza `RoleInUse` si alguna cuenta todavía lo tiene."""
        ...
