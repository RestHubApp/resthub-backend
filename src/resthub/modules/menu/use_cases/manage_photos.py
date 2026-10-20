"""Casos de uso de la foto de un plato.

Subir o quitar la foto es editar el plato: pide `menu.manage`, queda en la
bitácora y avisa al resto para que vuelvan a pedir la carta. Lo que se guarda no
son los bytes que llegaron sino la imagen vuelta a codificar
(`PhotoEncodingGateway`): sin metadatos y a lo sumo de 800 px de lado.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.clock import Clock
from resthub.core.realtime import EventPublisher
from resthub.modules.menu.domain.entities import MenuItem
from resthub.modules.menu.domain.exceptions import InvalidMenuItemError, MenuItemNotFoundError
from resthub.modules.menu.domain.photos import MenuPhoto, validate_photo
from resthub.modules.menu.ports.menu_repository import MenuRepository
from resthub.modules.menu.ports.photo_encoding import PhotoEncodingGateway
from resthub.modules.menu.use_cases.shared import announce_menu_change, find_item

_MILLISECONDS = 1000


@dataclass(frozen=True, slots=True)
class SetMenuPhotoCommand:
    restaurant_id: int
    actor_id: int
    item_id: int
    data: bytes


class SetMenuPhoto:
    def __init__(
        self,
        menu: MenuRepository,
        activity: ActivityRecorder,
        events: EventPublisher,
        clock: Clock,
        encoder: PhotoEncodingGateway,
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events
        self._clock = clock
        self._encoder = encoder

    async def __call__(self, command: SetMenuPhotoCommand) -> MenuItem:
        item = await find_item(self._menu, command.restaurant_id, command.item_id)
        validate_photo(command.data)
        encoded = await self._encoder.reencode(command.data)
        if encoded is None:
            message = "La foto no se pudo leer como imagen."
            raise InvalidMenuItemError(message)
        await self._menu.save_photo(
            MenuPhoto(
                restaurant_id=command.restaurant_id,
                item_id=command.item_id,
                content_type=encoded.content_type,
                data=encoded.data,
            )
        )
        item.photo_version = int(self._clock.now().timestamp() * _MILLISECONDS)
        saved = await self._menu.save_item(item)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_ITEM_UPDATED,
            f"{saved.name}: foto nueva",
        )
        announce_menu_change(self._events, command.restaurant_id, saved.id)
        return saved


@dataclass(frozen=True, slots=True)
class RemoveMenuPhotoCommand:
    restaurant_id: int
    actor_id: int
    item_id: int


class RemoveMenuPhoto:
    def __init__(
        self, menu: MenuRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._menu = menu
        self._activity = activity
        self._events = events

    async def __call__(self, command: RemoveMenuPhotoCommand) -> MenuItem:
        item = await find_item(self._menu, command.restaurant_id, command.item_id)
        if item.photo_version is None:
            return item
        await self._menu.delete_photo(command.restaurant_id, command.item_id)
        item.photo_version = None
        saved = await self._menu.save_item(item)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.MENU_ITEM_UPDATED,
            f"{saved.name}: foto quitada",
        )
        announce_menu_change(self._events, command.restaurant_id, saved.id)
        return saved


class ReadMenuPhoto:
    def __init__(self, menu: MenuRepository) -> None:
        self._menu = menu

    async def __call__(
        self, restaurant_id: int, item_id: int, *, include_inactive: bool = False
    ) -> MenuPhoto:
        item = await find_item(self._menu, restaurant_id, item_id)
        if not include_inactive and not item.is_active:
            raise MenuItemNotFoundError(item_id)
        photo = await self._menu.get_photo(restaurant_id, item_id)
        if photo is None:
            raise MenuItemNotFoundError(item_id)
        return photo
