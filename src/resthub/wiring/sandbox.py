"""Administración del sistema → el local de muestra y los códigos de vista previa.

`platform` declara el puerto `SandboxProvisioning` sin saber cómo se guarda un
restaurante, sus datos ni un código de vista previa; `restaurants` y `accounts`
exponen sus casos de uso sin saber que existe una plataforma. Este adaptador
traduce uno al otro, errores incluidos, y `main.py` lo instala en lugar de la
dependencia por omisión de `platform`, que no está conectada.

Los códigos de vista previa los posee `accounts`: son credenciales de sus
cuentas y los canjea su `POST /auth/preview`. Así el canje, que no lleva
autenticación previa, no cruza ningún módulo; la plataforma solo pide emitirlos,
por acá.

Corre en la sesión de la petición: archivar el local de muestra, crear el nuevo
y sembrar sus datos es una sola transacción.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.auth import SessionDep
from resthub.modules.accounts.adapters.api.dependencies import ClockDep, PasswordHasherDep
from resthub.modules.accounts.adapters.persistence.directories import SqlRestaurantDirectory
from resthub.modules.accounts.adapters.persistence.sqlalchemy_preview_codes import (
    SqlAlchemyPreviewCodeRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.domain.exceptions import NotASandboxAccount
from resthub.modules.accounts.domain.preview import (
    SANDBOX_EMAIL_DOMAIN,
    sandbox_email,
    unusable_password_secret,
)
from resthub.modules.accounts.ports.preview_codes import Clock
from resthub.modules.accounts.ports.user_repository import PasswordHasher
from resthub.modules.accounts.use_cases.preview import IssuePreviewCode, IssuePreviewCodeCommand
from resthub.modules.platform.domain import exceptions as platform_errors
from resthub.modules.platform.ports.sandbox import IssuedPreviewCode, SandboxProvisioning
from resthub.modules.restaurants.adapters.persistence.sqlalchemy_restaurant_repository import (
    SqlAlchemyRestaurantRepository,
)
from resthub.modules.restaurants.domain import exceptions as restaurants_errors
from resthub.modules.restaurants.use_cases.create_restaurant import (
    CreateRestaurant,
    CreateRestaurantCommand,
)
from resthub.wiring.restaurant_provisioning import as_platform_errors
from resthub.wiring.sample_restaurant import SampleAccount, sample_accounts, seed_sample_restaurant

SANDBOX_NAME = "Restaurante de muestra"
SANDBOX_SLUG = "muestra"


def sandbox_accounts(restaurant_id: int) -> tuple[SampleAccount, ...]:
    """Las cuentas de muestra de un local de muestra.

    El correo lleva el id del local porque es único en todo el sistema y las
    cuentas de los locales archivados siguen existiendo. Es la misma forma que
    toma una cuenta que alguien crea desde la vista previa.
    """
    return sample_accounts(
        emails=[
            sandbox_email(f"{name}@{SANDBOX_EMAIL_DOMAIN}", restaurant_id)
            for name in ("encargado", "mesero", "cocina")
        ],
        names=("Encargado de muestra", "Mesero de muestra", "Cocinero de muestra"),
    )


def archived_slug(restaurant_id: int) -> str:
    return f"{SANDBOX_SLUG}-archivado-{restaurant_id}"


class ModuleSandboxProvisioning:
    """Implementa el puerto de `platform` con los casos de uso de `restaurants` y `accounts`."""

    def __init__(self, session: AsyncSession, hasher: PasswordHasher, clock: Clock) -> None:
        self._session = session
        self._restaurants = SqlAlchemyRestaurantRepository(session)
        self._users = SqlAlchemyUserRepository(session)
        self._hasher = hasher
        self._clock = clock

    async def create(self) -> int:
        with as_platform_errors():
            created = await CreateRestaurant(self._restaurants)(
                CreateRestaurantCommand(name=SANDBOX_NAME, slug=SANDBOX_SLUG, is_sandbox=True)
            )
        restaurant_id = created.id or 0
        # Una contraseña que nadie conoce: el valor se descarta al salir de acá.
        password_hash = await asyncio.to_thread(self._hasher.hash, unusable_password_secret())
        await seed_sample_restaurant(
            self._session, restaurant_id, sandbox_accounts(restaurant_id), password_hash
        )
        return restaurant_id

    async def archive(self, restaurant_id: int) -> None:
        with as_platform_errors():
            restaurant = await self._restaurants.get(restaurant_id)
            # Solo un local de muestra: esto no desactiva restaurantes reales.
            if restaurant is None or not restaurant.is_sandbox:
                raise restaurants_errors.RestaurantNotFound(restaurant_id)
            restaurant.archive(archived_slug(restaurant_id))
            await self._restaurants.save(restaurant)

    async def issue_preview_code(self, user_id: int, admin_id: int) -> IssuedPreviewCode:
        try:
            issued = await IssuePreviewCode(
                SqlAlchemyPreviewCodeRepository(self._session),
                self._users,
                SqlRestaurantDirectory(self._session),
                self._clock,
            )(IssuePreviewCodeCommand(user_id=user_id, platform_admin_id=admin_id))
        except NotASandboxAccount as error:
            raise platform_errors.SandboxAccountUnavailable(str(user_id)) from error
        return IssuedPreviewCode(code=issued.code, expires_in_seconds=issued.expires_in_seconds)


def get_sandbox_provisioning(
    session: SessionDep, hasher: PasswordHasherDep, clock: ClockDep
) -> SandboxProvisioning:
    return ModuleSandboxProvisioning(session, hasher, clock)
