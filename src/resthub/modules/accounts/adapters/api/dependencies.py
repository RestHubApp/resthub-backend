"""Cableado del adaptador HTTP de cuentas.

Solo provee lo que este módulo posee: los repositorios de usuarios y de roles,
el cifrado de contraseñas y el lector hacia la tabla de restaurantes. La identidad de
quien hace la petición la resuelve `resthub.core.auth`, que es compartido, para
que ningún módulo tenga que importar a otro con tal de saber quién está del
otro lado.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.config import get_settings
from resthub.core.database import SessionDep
from resthub.core.security import hasher_from_settings
from resthub.modules.accounts.adapters.persistence.directories import (
    SqlAlchemyPlatformAdminDirectory,
    SqlAlchemyRestaurantDirectory,
)
from resthub.modules.accounts.adapters.persistence.sqlalchemy_account_activity import (
    SqlAlchemyAccountActivityReader,
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
from resthub.modules.accounts.ports.account_activity import AccountActivityReader
from resthub.modules.accounts.ports.platform_admin_directory import PlatformAdminDirectory
from resthub.modules.accounts.ports.platform_sign_in import PlatformSignInGateway
from resthub.modules.accounts.ports.preview_codes import PreviewCodeRepository
from resthub.modules.accounts.ports.restaurant_directory import RestaurantDirectory
from resthub.modules.accounts.ports.role_repository import RoleRepository
from resthub.modules.accounts.ports.user_repository import PasswordHasher, UserRepository


def get_user_repository(session: SessionDep) -> UserRepository:
    return SqlAlchemyUserRepository(session)


def get_account_activity(session: SessionDep) -> AccountActivityReader:
    return SqlAlchemyAccountActivityReader(session)


def get_role_repository(session: SessionDep) -> RoleRepository:
    return SqlAlchemyRoleRepository(session)


def get_password_hasher() -> PasswordHasher:
    return hasher_from_settings(get_settings())


def get_restaurant_directory(session: SessionDep) -> RestaurantDirectory:
    return SqlAlchemyRestaurantDirectory(session)


def get_platform_admin_directory(session: SessionDep) -> PlatformAdminDirectory:
    return SqlAlchemyPlatformAdminDirectory(session)


def get_preview_codes(session: SessionDep) -> PreviewCodeRepository:
    return SqlAlchemyPreviewCodeRepository(session)


def get_platform_sign_in() -> PlatformSignInGateway:
    """Sin implementación propia: las cuentas de plataforma son de otro módulo.

    `main.py` la reemplaza por la de `wiring/platform_sign_in.py`.
    """
    message = "El acceso de la plataforma no está conectado a la aplicación."
    raise NotImplementedError(message)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
AccountActivityDep = Annotated[AccountActivityReader, Depends(get_account_activity)]
RoleRepositoryDep = Annotated[RoleRepository, Depends(get_role_repository)]
PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
RestaurantDirectoryDep = Annotated[RestaurantDirectory, Depends(get_restaurant_directory)]
PlatformAdminDirectoryDep = Annotated[PlatformAdminDirectory, Depends(get_platform_admin_directory)]
PreviewCodesDep = Annotated[PreviewCodeRepository, Depends(get_preview_codes)]
PlatformSignInDep = Annotated[PlatformSignInGateway, Depends(get_platform_sign_in)]
