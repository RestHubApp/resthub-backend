"""Cableado del adaptador HTTP de cuentas.

Solo provee lo que este módulo posee: los repositorios de usuarios y de roles,
el cifrado de contraseñas y el lector hacia la tabla de restaurantes. La identidad de
quien hace la petición la resuelve `resthub.core.auth`, que es compartido, para
que ningún módulo tenga que importar a otro con tal de saber quién está del
otro lado.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

from resthub.core.auth import SessionDep
from resthub.core.security import BcryptPasswordHasher
from resthub.modules.accounts.adapters.persistence.directories import (
    SqlPlatformAdminDirectory,
    SqlRestaurantDirectory,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_preview_codes import (
    SqlAlchemyPreviewCodeRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_role_repository import (
    SqlAlchemyRoleRepository,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_user_repository import (
    SqlAlchemyUserRepository,
)
from resthub.modules.accounts.ports.platform_admin_directory import PlatformAdminDirectory
from resthub.modules.accounts.ports.preview_codes import Clock, PreviewCodeRepository
from resthub.modules.accounts.ports.restaurant_directory import RestaurantDirectory
from resthub.modules.accounts.ports.role_repository import RoleRepository
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository


def get_user_repository(session: SessionDep) -> UserRepository:
    return SqlAlchemyUserRepository(session)


def get_role_repository(session: SessionDep) -> RoleRepository:
    return SqlAlchemyRoleRepository(session)


def get_password_hasher() -> PasswordHasher:
    return BcryptPasswordHasher()


def get_restaurant_directory(session: SessionDep) -> RestaurantDirectory:
    return SqlRestaurantDirectory(session)


def get_platform_admin_directory(session: SessionDep) -> PlatformAdminDirectory:
    return SqlPlatformAdminDirectory(session)


def get_preview_codes(session: SessionDep) -> PreviewCodeRepository:
    return SqlAlchemyPreviewCodeRepository(session)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Clock:
    return _utc_now


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
RoleRepositoryDep = Annotated[RoleRepository, Depends(get_role_repository)]
PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
RestaurantDirectoryDep = Annotated[RestaurantDirectory, Depends(get_restaurant_directory)]
PlatformAdminDirectoryDep = Annotated[PlatformAdminDirectory, Depends(get_platform_admin_directory)]
PreviewCodesDep = Annotated[PreviewCodeRepository, Depends(get_preview_codes)]
ClockDep = Annotated[Clock, Depends(get_clock)]
