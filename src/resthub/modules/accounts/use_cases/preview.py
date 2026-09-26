"""Casos de uso de la vista previa: emitir un código y canjearlo por una sesión.

La vista previa solo entra al local de muestra. Se comprueba en cada paso, no
solo en el primero: al emitir el código (la cuenta tiene que ser del local de
muestra), al canjearlo (lo mismo, releído de la base) y en cada petición
(`core/auth.py` rechaza un token de vista previa de una cuenta real).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import TokenService
from resthub.modules.accounts.domain.exceptions import (
    InvalidPreviewCode,
    NotASandboxAccount,
    UserNotFound,
)
from resthub.modules.accounts.domain.preview import (
    PREVIEW_CODE_TTL_SECONDS,
    PreviewCode,
    new_preview_code,
    preview_code_hash,
)
from resthub.modules.accounts.ports.preview_codes import Clock, PreviewCodeRepository
from resthub.modules.accounts.ports.restaurant_directory import RestaurantDirectory
from resthub.modules.accounts.ports.user_repository import UserRepository
from resthub.modules.accounts.use_cases.authenticate_user import AuthenticatedSession
from resthub.modules.accounts.use_cases.read_session import build_session


@dataclass(frozen=True, slots=True)
class IssuePreviewCodeCommand:
    user_id: int
    platform_admin_id: int


@dataclass(frozen=True, slots=True)
class IssuedPreviewCode:
    # En claro solo acá, para devolverlo una vez; la base guarda su hash.
    code: str
    expires_in_seconds: int


class IssuePreviewCode:
    """Un código de un solo uso para entrar como una cuenta del local de muestra.

    No hay endpoint de este módulo que lo llame: lo pide la administración del
    sistema, por su puerto, desde `wiring/sandbox.py`.
    """

    def __init__(
        self,
        codes: PreviewCodeRepository,
        users: UserRepository,
        restaurants: RestaurantDirectory,
        clock: Clock,
    ) -> None:
        self._codes = codes
        self._users = users
        self._restaurants = restaurants
        self._clock = clock

    async def __call__(self, command: IssuePreviewCodeCommand) -> IssuedPreviewCode:
        user = await self._users.get(command.user_id)
        if user is None or user.id is None or not user.is_active:
            raise NotASandboxAccount(command.user_id)
        restaurant = await self._restaurants.get(user.restaurant_id)
        if restaurant is None or not restaurant.is_sandbox or not restaurant.is_active:
            raise NotASandboxAccount(command.user_id)

        code = new_preview_code()
        now = self._clock()
        await self._codes.add(
            PreviewCode(
                code_hash=preview_code_hash(code),
                user_id=user.id,
                platform_admin_id=command.platform_admin_id,
                expires_at=now + timedelta(seconds=PREVIEW_CODE_TTL_SECONDS),
                created_at=now,
            )
        )
        return IssuedPreviewCode(code=code, expires_in_seconds=PREVIEW_CODE_TTL_SECONDS)


class ExchangePreviewCode:
    """Canjea un código por un token de vista previa y la sesión que abre.

    Todo fallo responde `InvalidPreviewCode`, el mismo para un código que no
    existe, que venció, que ya se usó o cuya cuenta dejó de ser del local de
    muestra: distinguirlos solo le serviría a quien prueba códigos.
    """

    def __init__(
        self,
        codes: PreviewCodeRepository,
        users: UserRepository,
        restaurants: RestaurantDirectory,
        tokens: TokenService,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._codes = codes
        self._users = users
        self._restaurants = restaurants
        self._tokens = tokens
        self._activity = activity
        self._clock = clock

    async def __call__(self, code: str) -> AuthenticatedSession:
        grant = await self._codes.consume(preview_code_hash(code), self._clock())
        if grant is None:
            raise InvalidPreviewCode()
        user = await self._users.get(grant.user_id)
        if user is None or user.id is None or not user.is_active:
            raise InvalidPreviewCode()
        try:
            session = await build_session(user, self._restaurants)
        except UserNotFound as error:
            raise InvalidPreviewCode() from error
        if not session.restaurant.is_sandbox or not session.restaurant.is_active:
            raise InvalidPreviewCode()

        await self._activity.record(
            user.restaurant_id, user.id, ActivityKind.SIGNED_IN, "Vista previa"
        )
        token = self._tokens.issue_preview(user.id, user.restaurant_id, grant.platform_admin_id)
        return AuthenticatedSession(session=session, token=token)
