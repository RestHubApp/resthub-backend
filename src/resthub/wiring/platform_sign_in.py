"""Acceso único → las cuentas de la administración del sistema.

`accounts` declara el puerto `PlatformSignInGateway` sin saber dónde viven las
cuentas de plataforma, y `platform` expone sus casos de uso sin saber que el
acceso es compartido. Este adaptador traduce uno al otro y `main.py` lo instala
en lugar de la dependencia por omisión de `accounts`, que no está conectada.

Corre en la sesión de la petición: el rastro del ingreso en la bitácora de la
plataforma y la migración del hash quedan en la misma transacción que el acceso.
"""

from __future__ import annotations

from datetime import timedelta

from resthub.core.auth import PlatformTokenServiceDep, TemporaryPasswordLifetimeDep
from resthub.core.clock import Clock
from resthub.core.clock_dependency import ClockDep
from resthub.core.cpu_bound import run_cpu_bound
from resthub.core.database import SessionDep
from resthub.core.identity import PlatformTokenService
from resthub.modules.accounts.adapters.api.dependencies import PasswordHasherDep
from resthub.modules.accounts.domain import exceptions as accounts_errors
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
from resthub.modules.platform.domain import exceptions as platform_errors
from resthub.modules.platform.domain.entities import PlatformAdmin
from resthub.modules.platform.domain.exceptions import (
    AdminUnavailableError,
    InvalidCredentialsError,
)
from resthub.modules.platform.use_cases.manage_admins import (
    AuthenticateAdmin,
    AuthenticateAdminCommand,
    ChangeAdminPassword,
    ChangeAdminPasswordCommand,
    ReadCurrentAdmin,
)


def _account(admin: PlatformAdmin) -> PlatformAccount:
    return PlatformAccount(
        id=admin.id or 0,
        email=admin.email,
        full_name=admin.full_name,
        must_change_password=admin.must_change_password,
    )


class ModulePlatformSignInGateway:
    """Implementa el puerto de `accounts` con los casos de uso de `platform`."""

    def __init__(
        self,
        admins: SqlAlchemyPlatformAdminRepository,
        activity: SqlAlchemyPlatformActivityLogRepository,
        hasher: PasswordHasher,
        tokens: PlatformTokenService,
        *,
        clock: Clock,
        temporary_password_lifetime: timedelta,
    ) -> None:
        self._admins = admins
        self._activity = activity
        self._hasher = hasher
        self._tokens = tokens
        self._clock = clock
        self._temporary_password_lifetime = temporary_password_lifetime

    async def authenticate(self, email: str, password: str) -> PlatformSignIn | None:
        try:
            result = await AuthenticateAdmin(
                self._admins,
                self._hasher,
                self._tokens,
                self._activity,
                clock=self._clock,
                temporary_password_lifetime=self._temporary_password_lifetime,
            )(AuthenticateAdminCommand(email=email, password=password))
        except InvalidCredentialsError:
            return None
        except platform_errors.TemporaryPasswordExpiredError as error:
            raise accounts_errors.TemporaryPasswordExpiredError from error
        return PlatformSignIn(account=_account(result.admin), token=result.token)

    async def change_password(
        self, admin_id: int, current_password: str, new_password: str
    ) -> PlatformAccount:
        use_case = ChangeAdminPassword(
            self._admins,
            self._hasher,
            self._activity,
            clock=self._clock,
            temporary_password_lifetime=self._temporary_password_lifetime,
        )
        try:
            admin = await use_case(
                ChangeAdminPasswordCommand(
                    admin_id=admin_id,
                    current_password=current_password,
                    new_password=new_password,
                )
            )
        except AdminUnavailableError as error:
            raise accounts_errors.UserNotFoundError(admin_id) from error
        except InvalidCredentialsError as error:
            raise accounts_errors.WrongCurrentPasswordError from error
        except platform_errors.InvalidAccountDataError as error:
            raise accounts_errors.WeakPasswordError(str(error)) from error
        except platform_errors.TemporaryPasswordExpiredError as error:
            raise accounts_errors.TemporaryPasswordExpiredError from error
        return _account(admin)

    async def verify_password(self, admin_id: int, password: str) -> bool:
        admin = await self._admins.get(admin_id)
        if admin is None or not admin.is_active:
            return False
        matches, _ = await run_cpu_bound(
            self._hasher.verify_and_update, password, admin.password_hash
        )
        return matches

    async def read(self, admin_id: int, issued_at: float) -> PlatformAccount | None:
        try:
            admin = await ReadCurrentAdmin(self._admins)(admin_id, issued_at)
        except AdminUnavailableError:
            return None
        return _account(admin)


def get_platform_sign_in(
    session: SessionDep,
    hasher: PasswordHasherDep,
    tokens: PlatformTokenServiceDep,
    clock: ClockDep,
    lifetime: TemporaryPasswordLifetimeDep,
) -> PlatformSignInGateway:
    return ModulePlatformSignInGateway(
        SqlAlchemyPlatformAdminRepository(session),
        SqlAlchemyPlatformActivityLogRepository(session),
        hasher,
        tokens,
        clock=clock,
        temporary_password_lifetime=lifetime,
    )
