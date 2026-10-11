"""Acceso único → las cuentas de la administración del sistema.

`accounts` declara el puerto `PlatformSignInGateway` sin saber dónde viven las
cuentas de plataforma, y `platform` expone sus casos de uso sin saber que el
acceso es compartido. Este adaptador traduce uno al otro y `main.py` lo instala
en lugar de la dependencia por omisión de `accounts`, que no está conectada.

Corre en la sesión de la petición: el rastro del ingreso en la bitácora de la
plataforma y la migración del hash quedan en la misma transacción que el acceso.
"""

from __future__ import annotations

from resthub.core.auth import PlatformTokenServiceDep
from resthub.core.database import SessionDep
from resthub.core.identity import PlatformTokenService
from resthub.modules.accounts.adapters.api.dependencies import PasswordHasherDep
from resthub.modules.accounts.ports.platform_sign_in import (
    PlatformAccount,
    PlatformSignIn,
    PlatformSignInGateway,
)
from resthub.modules.accounts.ports.user_repository import PasswordHasher
from resthub.modules.platform.adapters.persistence.sqlalchemy_activity_log import (
    SqlAlchemyPlatformActivityLogRepository,
)
from resthub.modules.platform.adapters.persistence.sqlalchemy_admin_repository import (
    SqlAlchemyPlatformAdminRepository,
)
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import (
    AdminUnavailableError,
    InvalidCredentialsError,
)
from resthub.modules.platform.use_cases.manage_admins import (
    AuthenticateAdmin,
    AuthenticateAdminCommand,
    ReadCurrentAdmin,
)


def _account(admin: PlatformAdmin) -> PlatformAccount:
    return PlatformAccount(id=admin.id or 0, email=admin.email, full_name=admin.full_name)


class ModulePlatformSignInGateway:
    """Implementa el puerto de `accounts` con los casos de uso de `platform`."""

    def __init__(
        self,
        admins: SqlAlchemyPlatformAdminRepository,
        activity: SqlAlchemyPlatformActivityLogRepository,
        hasher: PasswordHasher,
        tokens: PlatformTokenService,
    ) -> None:
        self._admins = admins
        self._activity = activity
        self._hasher = hasher
        self._tokens = tokens

    async def authenticate(self, email: str, password: str) -> PlatformSignIn | None:
        try:
            result = await AuthenticateAdmin(
                self._admins, self._hasher, self._tokens, self._activity
            )(AuthenticateAdminCommand(email=email, password=password))
        except InvalidCredentialsError:
            return None
        return PlatformSignIn(account=_account(result.admin), token=result.token)

    async def read(self, admin_id: int, issued_at: float) -> PlatformAccount | None:
        try:
            admin = await ReadCurrentAdmin(self._admins)(admin_id, issued_at)
        except AdminUnavailableError:
            return None
        return _account(admin)


def get_platform_sign_in(
    session: SessionDep, hasher: PasswordHasherDep, tokens: PlatformTokenServiceDep
) -> PlatformSignInGateway:
    return ModulePlatformSignInGateway(
        SqlAlchemyPlatformAdminRepository(session),
        SqlAlchemyPlatformActivityLogRepository(session),
        hasher,
        tokens,
    )
