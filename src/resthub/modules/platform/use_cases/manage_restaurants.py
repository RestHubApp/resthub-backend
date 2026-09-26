"""Casos de uso de los restaurantes vistos desde la administración del sistema.

A diferencia del resto de la API, acá el restaurante sí viene en la URL: la
plataforma no pertenece a ningún local y los administra a todos. Lo que la
protege es que solo una credencial de plataforma llega a estos casos de uso.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page, PageRequest
from resthub.modules.platform.domain.entities import PlatformActivityKind
from resthub.modules.platform.domain.exceptions import RestaurantNotFound
from resthub.modules.platform.ports.activity_log import PlatformActivityLog
from resthub.modules.platform.ports.restaurants import (
    NewOwner,
    NewRestaurant,
    OwnerAccount,
    RestaurantCatalog,
    RestaurantChanges,
    RestaurantProvisioning,
    RestaurantSummary,
)


@dataclass(frozen=True, slots=True)
class RestaurantDetail:
    summary: RestaurantSummary
    owners: list[OwnerAccount]


async def _detail(catalog: RestaurantCatalog, restaurant_id: int) -> RestaurantDetail:
    summary = await catalog.get(restaurant_id)
    if summary is None:
        raise RestaurantNotFound(restaurant_id)
    return RestaurantDetail(summary=summary, owners=await catalog.owners(restaurant_id))


@dataclass(frozen=True, slots=True)
class ListRestaurantsQuery:
    search: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class ListRestaurants:
    def __init__(self, catalog: RestaurantCatalog) -> None:
        self._catalog = catalog

    async def __call__(self, query: ListRestaurantsQuery) -> Page[RestaurantSummary]:
        text = (query.search or "").strip() or None
        return await self._catalog.search(text, PageRequest(limit=query.limit, offset=query.offset))


class ReadRestaurant:
    def __init__(self, catalog: RestaurantCatalog) -> None:
        self._catalog = catalog

    async def __call__(self, restaurant_id: int) -> RestaurantDetail:
        return await _detail(self._catalog, restaurant_id)


@dataclass(frozen=True, slots=True)
class CreateRestaurantCommand:
    admin_id: int
    name: str
    slug: str
    timezone: str
    owner: NewOwner


class CreateRestaurant:
    """Restaurante, roles Encargado y Mesero y primer encargado, en una transacción."""

    def __init__(
        self,
        provisioning: RestaurantProvisioning,
        catalog: RestaurantCatalog,
        activity: PlatformActivityLog,
    ) -> None:
        self._provisioning = provisioning
        self._catalog = catalog
        self._activity = activity

    async def __call__(self, command: CreateRestaurantCommand) -> RestaurantDetail:
        restaurant_id = await self._provisioning.create(
            NewRestaurant(
                name=command.name,
                slug=command.slug,
                timezone=command.timezone,
                owner=command.owner,
            )
        )
        detail = await _detail(self._catalog, restaurant_id)
        owner = detail.owners[0].email if detail.owners else ""
        await self._activity.record(
            command.admin_id,
            PlatformActivityKind.RESTAURANT_CREATED,
            f"{detail.summary.name} ({detail.summary.slug}), encargado {owner}",
        )
        return detail


@dataclass(frozen=True, slots=True)
class UpdateRestaurantCommand:
    admin_id: int
    restaurant_id: int
    changes: RestaurantChanges


class UpdateRestaurant:
    """Nombre, zona horaria y si está activo.

    Desactivar corta en la petición siguiente el acceso de todo su personal: la
    identidad de cada cuenta se relee de la base y un local inactivo la vuelve
    inactiva (`core/auth.py`). Reactivar lo devuelve igual de rápido.
    """

    def __init__(
        self,
        provisioning: RestaurantProvisioning,
        catalog: RestaurantCatalog,
        activity: PlatformActivityLog,
    ) -> None:
        self._provisioning = provisioning
        self._catalog = catalog
        self._activity = activity

    async def __call__(self, command: UpdateRestaurantCommand) -> RestaurantDetail:
        before = await self._catalog.get(command.restaurant_id)
        if before is None:
            raise RestaurantNotFound(command.restaurant_id)

        await self._provisioning.update(command.restaurant_id, command.changes)
        detail = await _detail(self._catalog, command.restaurant_id)

        changes = _describe_changes(before, detail.summary)
        if changes:
            await self._activity.record(
                command.admin_id,
                PlatformActivityKind.RESTAURANT_UPDATED,
                f"{detail.summary.slug}: {', '.join(changes)}",
            )
        return detail


def _describe_changes(before: RestaurantSummary, after: RestaurantSummary) -> list[str]:
    changes: list[str] = []
    if after.name != before.name:
        changes.append(f"nombre «{after.name}»")
    if after.timezone != before.timezone:
        changes.append(f"zona horaria {after.timezone}")
    if after.is_active != before.is_active:
        changes.append("activado" if after.is_active else "desactivado")
    return changes


@dataclass(frozen=True, slots=True)
class AddOwnerCommand:
    admin_id: int
    restaurant_id: int
    owner: NewOwner


class AddOwner:
    """Otro encargado para un local, por ejemplo cuando el primero se fue o perdió el acceso."""

    def __init__(
        self,
        provisioning: RestaurantProvisioning,
        catalog: RestaurantCatalog,
        activity: PlatformActivityLog,
    ) -> None:
        self._provisioning = provisioning
        self._catalog = catalog
        self._activity = activity

    async def __call__(self, command: AddOwnerCommand) -> OwnerAccount:
        restaurant = await self._catalog.get(command.restaurant_id)
        if restaurant is None:
            raise RestaurantNotFound(command.restaurant_id)

        owner = await self._provisioning.add_owner(command.restaurant_id, command.owner)
        await self._activity.record(
            command.admin_id,
            PlatformActivityKind.OWNER_ADDED,
            f"{owner.email} en {restaurant.slug}",
        )
        return owner
