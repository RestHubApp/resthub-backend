"""Casos de uso de los roles de un restaurante.

El encargado arma los roles de su local: el mesero con los permisos que le
quiera dar y los que invente (un cocinero que marca pedidos listos pero no
cobra). Cada comando trae el restaurante del principal; un rol de otro local
responde como inexistente.

Nadie reparte lo que no tiene: crear o editar un rol exige tener cada permiso
que el rol va a llevar. Sin esa regla, `roles.manage` alcanzaría para darse
cualquier otro.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.permissions import Permission, RoleKind
from resthub.core.realtime import PERMISSIONS_TOPIC, EventPublisher, RealtimeEvent
from resthub.modules.accounts.domain.exceptions import (
    CannotManageStrongerRole,
    RoleNameTaken,
    RoleNotFound,
)
from resthub.modules.accounts.domain.roles import (
    Role,
    ensure_can_grant,
    holds_all,
    role_name_key,
)
from resthub.modules.accounts.ports.role_repository import RoleRepository

_KIND_ORDER = {RoleKind.OWNER: 0, RoleKind.WAITER: 1, RoleKind.CUSTOM: 2}


@dataclass(frozen=True, slots=True)
class BaseRoles:
    owner: Role
    waiter: Role


async def ensure_base_roles(roles: RoleRepository, restaurant_id: int) -> BaseRoles:
    """El encargado y el mesero de un restaurante, creándolos si faltan."""
    owner = await roles.find_by_kind(restaurant_id, RoleKind.OWNER)
    if owner is None:
        owner = await roles.add(Role.owner(restaurant_id))
    waiter = await roles.find_by_kind(restaurant_id, RoleKind.WAITER)
    if waiter is None:
        waiter = await roles.add(Role.waiter(restaurant_id))
    return BaseRoles(owner=owner, waiter=waiter)


async def find_role(
    roles: RoleRepository, restaurant_id: int, role_id: int, *, for_update: bool = False
) -> Role:
    role = await roles.get_in_restaurant(restaurant_id, role_id, for_update=for_update)
    if role is None:
        raise RoleNotFound(role_id)
    return role


@dataclass(frozen=True, slots=True)
class RoleView:
    role: Role
    member_count: int

    @property
    def is_deletable(self) -> bool:
        return self.role.kind is RoleKind.CUSTOM and self.member_count == 0


class ListRoles:
    def __init__(self, roles: RoleRepository) -> None:
        self._roles = roles

    async def __call__(self, restaurant_id: int) -> list[RoleView]:
        found = await self._roles.list_for_restaurant(restaurant_id)
        counts = await self._roles.member_counts(restaurant_id)
        views = [RoleView(role=role, member_count=counts.get(role.id or 0, 0)) for role in found]
        return sorted(views, key=lambda view: (_KIND_ORDER[view.role.kind], view.role.name_key))


@dataclass(frozen=True, slots=True)
class CreateRoleCommand:
    restaurant_id: int
    actor_id: int
    actor_permissions: frozenset[str]
    name: str
    permissions: frozenset[Permission]


class CreateRole:
    def __init__(self, roles: RoleRepository, activity: ActivityRecorder) -> None:
        self._roles = roles
        self._activity = activity

    async def __call__(self, command: CreateRoleCommand) -> RoleView:
        # Solo roles propios: el encargado y el mesero nacen con el restaurante.
        candidate = Role(
            restaurant_id=command.restaurant_id,
            name=command.name,
            kind=RoleKind.CUSTOM,
            stored_permissions=command.permissions,
        )
        ensure_can_grant(command.actor_permissions, candidate.permissions)
        if await self._roles.exists_with_name(command.restaurant_id, candidate.name_key):
            raise RoleNameTaken(candidate.name)

        created = await self._roles.add(candidate)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.ROLE_CREATED,
            _describe(created),
        )
        return RoleView(role=created, member_count=0)


@dataclass(frozen=True, slots=True)
class UpdateRoleCommand:
    restaurant_id: int
    actor_id: int
    actor_permissions: frozenset[str]
    role_id: int
    name: str
    permissions: frozenset[Permission]


class UpdateRole:
    def __init__(
        self, roles: RoleRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._roles = roles
        self._activity = activity
        self._events = events

    async def __call__(self, command: UpdateRoleCommand) -> RoleView:
        role = await find_role(self._roles, command.restaurant_id, command.role_id, for_update=True)
        before = (role.name, role.permissions)

        role.update(command.name, command.permissions)
        # Quitarle algo a un rol con más poder que uno también es tocarlo: un
        # rol que no se podría crear tampoco se puede recortar.
        if not holds_all(command.actor_permissions, before[1]):
            raise CannotManageStrongerRole()
        ensure_can_grant(command.actor_permissions, role.permissions)
        if role.name_key != role_name_key(before[0]) and await self._roles.exists_with_name(
            command.restaurant_id, role.name_key, excluding_id=role.id
        ):
            raise RoleNameTaken(role.name)

        saved = await self._roles.save(role)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.ROLE_UPDATED,
            _describe(saved),
        )
        members = await self._roles.member_ids(command.restaurant_id, saved.id or 0)
        if (saved.name, saved.permissions) != before and members:
            # Las sesiones abiertas de ese rol vuelven a pedir `/auth/me` y
            # rearman la navegación sin cerrar sesión.
            self._events.publish(
                RealtimeEvent(
                    restaurant_id=command.restaurant_id,
                    topic=PERMISSIONS_TOPIC,
                    user_ids=members,
                    reference_id=saved.id,
                )
            )
        return RoleView(role=saved, member_count=len(members))


@dataclass(frozen=True, slots=True)
class DeleteRoleCommand:
    restaurant_id: int
    actor_id: int
    role_id: int


class DeleteRole:
    def __init__(self, roles: RoleRepository, activity: ActivityRecorder) -> None:
        self._roles = roles
        self._activity = activity

    async def __call__(self, command: DeleteRoleCommand) -> None:
        role = await find_role(self._roles, command.restaurant_id, command.role_id, for_update=True)
        members = await self._roles.member_ids(command.restaurant_id, role.id or 0)
        role.ensure_deletable(len(members))

        await self._roles.delete(role)
        await self._activity.record(
            command.restaurant_id, command.actor_id, ActivityKind.ROLE_DELETED, role.name
        )


def _describe(role: Role) -> str:
    return f"{role.name} ({_count(role.permissions)})"


def _count(permissions: Iterable[Permission]) -> str:
    total = len(frozenset(permissions))
    return "1 permiso" if total == 1 else f"{total} permisos"
