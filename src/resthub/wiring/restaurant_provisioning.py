"""Administración del sistema → alta y edición de restaurantes y encargados.

`platform` declara el puerto `RestaurantProvisioning` sin saber cómo se guarda
un restaurante ni una cuenta, y `restaurants` y `accounts` exponen sus casos de
uso sin saber que existe una plataforma. Este adaptador traduce uno al otro,
errores incluidos, y `main.py` lo instala en lugar de la dependencia por
omisión de `platform`, que no está conectada.

Corre en la sesión de la petición: restaurante, roles base y primer encargado
quedan en una sola transacción, y si algo falla a mitad de camino no queda un
restaurante sin nadie que lo administre. Es lo mismo que hace
`scripts/create_restaurant.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from resthub.core.auth import SessionDep
from resthub.modules.accounts.adapters.api.dependencies import PasswordHasherDep
from resthub.modules.accounts.adapters.persistence.sqlalchemy_role_repository import (
    SqlAlchemyRoleRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain import exceptions as accounts_errors
from resthub.modules.accounts.ports.user_repository import PasswordHasher
from resthub.modules.accounts.use_cases.manage_staff import (
    RegisterFirstAdmin,
    RegisterOwner,
    RegisterOwnerCommand,
)
from resthub.modules.platform.domain import exceptions as platform_errors
from resthub.modules.platform.ports.restaurants import (
    NewOwner,
    NewRestaurant,
    OwnerAccount,
    RestaurantChanges,
    RestaurantProvisioning,
)
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain import exceptions as restaurants_errors
from resthub.modules.restaurants.use_cases.create_restaurant import (
    CreateRestaurant,
    CreateRestaurantCommand,
)


@contextmanager
def _as_platform_errors() -> Iterator[None]:
    """Los errores de `restaurants` y `accounts`, dichos en los términos de `platform`."""
    try:
        yield
    except restaurants_errors.SlugAlreadyTaken as error:
        raise platform_errors.SlugAlreadyTaken(error.slug) from error
    except restaurants_errors.RestaurantNotFound as error:
        raise platform_errors.RestaurantNotFound(error.restaurant_id) from error
    except (
        restaurants_errors.InvalidRestaurantName,
        restaurants_errors.InvalidSlug,
        restaurants_errors.InvalidTimezone,
    ) as error:
        raise platform_errors.InvalidRestaurantData(str(error)) from error
    except accounts_errors.EmailAlreadyRegistered as error:
        raise platform_errors.EmailAlreadyRegistered(error.email) from error
    except (
        accounts_errors.InvalidEmail,
        accounts_errors.InvalidFullName,
        accounts_errors.WeakPassword,
    ) as error:
        raise platform_errors.InvalidAccountData(str(error)) from error


def _owner_command(restaurant_id: int, owner: NewOwner) -> RegisterOwnerCommand:
    return RegisterOwnerCommand(
        restaurant_id=restaurant_id,
        email=owner.email,
        full_name=owner.full_name,
        password=owner.password,
    )


class ModuleRestaurantProvisioning:
    """Implementa el puerto de `platform` con los casos de uso de `restaurants` y `accounts`."""

    def __init__(
        self,
        restaurants: SqlAlchemyRestaurantRepository,
        users: SqlAlchemyUserRepository,
        roles: SqlAlchemyRoleRepository,
        hasher: PasswordHasher,
    ) -> None:
        self._restaurants = restaurants
        self._users = users
        self._roles = roles
        self._hasher = hasher

    async def create(self, restaurant: NewRestaurant) -> int:
        with _as_platform_errors():
            created = await CreateRestaurant(self._restaurants)(
                CreateRestaurantCommand(
                    name=restaurant.name, slug=restaurant.slug, timezone=restaurant.timezone
                )
            )
            restaurant_id = created.id or 0
            await RegisterFirstAdmin(self._users, self._roles, self._hasher)(
                _owner_command(restaurant_id, restaurant.owner)
            )
        return restaurant_id

    async def update(self, restaurant_id: int, changes: RestaurantChanges) -> None:
        with _as_platform_errors():
            restaurant = await self._restaurants.get(restaurant_id)
            if restaurant is None:
                raise restaurants_errors.RestaurantNotFound(restaurant_id)
            if changes.name is not None:
                restaurant.rename(changes.name)
            if changes.timezone is not None:
                restaurant.move_to_timezone(changes.timezone)
            if changes.is_active is not None:
                restaurant.is_active = changes.is_active
            # Sin el caso de uso `UpdateRestaurant`: ese deja asiento en la
            # bitácora del local a nombre de una cuenta del personal, y quien
            # edita acá no lo es. Lo registra la bitácora de la plataforma.
            await self._restaurants.save(restaurant)

    async def add_owner(self, restaurant_id: int, owner: NewOwner) -> OwnerAccount:
        with _as_platform_errors():
            if await self._restaurants.get(restaurant_id) is None:
                raise restaurants_errors.RestaurantNotFound(restaurant_id)
            created = await RegisterOwner(self._users, self._roles, self._hasher)(
                _owner_command(restaurant_id, owner)
            )
        return OwnerAccount(
            id=created.id or 0,
            full_name=created.full_name,
            email=created.email,
            is_active=created.is_active,
        )


def get_restaurant_provisioning(
    session: SessionDep, hasher: PasswordHasherDep
) -> RestaurantProvisioning:
    return ModuleRestaurantProvisioning(
        SqlAlchemyRestaurantRepository(session),
        SqlAlchemyUserRepository(session),
        SqlAlchemyRoleRepository(session),
        hasher,
    )
