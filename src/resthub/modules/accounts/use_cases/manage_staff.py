"""Casos de uso de gestión del personal.

Los usa el encargado desde la laptop: listar, dar de alta, editar, activar o
desactivar y restablecer contraseñas. Cada comando trae el restaurante del
principal, y toda cuenta se busca acotada a él: una cuenta de otro local
responde como inexistente.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.activity import ActivityKind, ActivityRecorder
from resthub.core.identity import Role
from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page
from resthub.core.realtime import PERMISSIONS_TOPIC, EventPublisher, RealtimeEvent
from resthub.modules.accounts.domain.entities import (
    User,
    ensure_can_change_role,
    ensure_can_deactivate,
    ensure_can_reset_password,
    normalize_email,
    validate_new_password,
)
from resthub.modules.accounts.domain.exceptions import (
    EmailAlreadyRegistered,
    RestaurantAlreadyHasStaff,
    UserNotFound,
)
from resthub.modules.accounts.ports.user_repository import (
    PasswordHasher,
    UserQuery,
    UserRepository,
)

# Solo estas columnas pueden ordenar el listado. Un valor desconocido cae al
# predeterminado en lugar de viajar hacia la base de datos.
ORDERABLE_FIELDS = frozenset({"email", "full_name", "role", "created_at", "is_active"})
DEFAULT_ORDERING = "full_name"


def resolve_ordering(ordering: str | None) -> str:
    if not ordering:
        return DEFAULT_ORDERING
    return ordering if ordering.removeprefix("-") in ORDERABLE_FIELDS else DEFAULT_ORDERING


async def _find_in_restaurant(users: UserRepository, restaurant_id: int, user_id: int) -> User:
    user = await users.get_in_restaurant(restaurant_id, user_id)
    if user is None:
        raise UserNotFound(user_id)
    return user


def _notify_account_changed(events: EventPublisher, user: User) -> None:
    # La sesión abierta de esa cuenta vuelve a pedir `/auth/me`: un mesero
    # ascendido ve el menú de encargado sin cerrar sesión, y uno desactivado
    # descubre al instante que ya no tiene acceso.
    events.publish(
        RealtimeEvent(
            restaurant_id=user.restaurant_id,
            topic=PERMISSIONS_TOPIC,
            user_ids=frozenset({user.id or 0}),
            reference_id=user.id,
        )
    )


@dataclass(frozen=True, slots=True)
class ListStaffQuery:
    restaurant_id: int
    roles: frozenset[Role] | None = None
    search: str | None = None
    is_active: bool | None = None
    ordering: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class ListStaff:
    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def __call__(self, query: ListStaffQuery) -> Page[User]:
        return await self._users.search(
            UserQuery(
                restaurant_id=query.restaurant_id,
                roles=query.roles,
                search=query.search,
                is_active=query.is_active,
                ordering=resolve_ordering(query.ordering),
                limit=query.limit,
                offset=query.offset,
            )
        )


class ReadStaffMember:
    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def __call__(self, restaurant_id: int, user_id: int) -> User:
        return await _find_in_restaurant(self._users, restaurant_id, user_id)


@dataclass(frozen=True, slots=True)
class RegisterStaffCommand:
    restaurant_id: int
    actor_id: int
    email: str
    full_name: str
    role: Role
    password: str


class RegisterStaff:
    """Alta de un mesero o de otro encargado.

    El restaurante de la cuenta nueva es el de quien la crea, nunca uno que
    venga en el cuerpo de la petición.
    """

    def __init__(
        self, users: UserRepository, hasher: PasswordHasher, activity: ActivityRecorder
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._activity = activity

    async def __call__(self, command: RegisterStaffCommand) -> User:
        candidate = User(
            restaurant_id=command.restaurant_id,
            email=command.email,
            full_name=command.full_name,
            role=command.role,
            password_hash=self._hasher.hash(validate_new_password(command.password)),
        )
        # El correo es único entre todos los restaurantes: es con lo que se
        # entra, y el acceso no pregunta de qué local sos.
        if await self._users.exists_with_email(candidate.email):
            raise EmailAlreadyRegistered(candidate.email)

        created = await self._users.add(candidate)
        # El asiento lo firma quien da el alta, no la cuenta recién creada.
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STAFF_REGISTERED,
            f"{created.full_name} ({created.role.label})",
        )
        return created


@dataclass(frozen=True, slots=True)
class UpdateStaffCommand:
    restaurant_id: int
    actor_id: int
    user_id: int
    # `None` deja el campo como está.
    full_name: str | None = None
    role: Role | None = None


class UpdateStaff:
    """Corrige el nombre o el rol. El correo no entra: es la identidad de acceso."""

    def __init__(
        self, users: UserRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._users = users
        self._activity = activity
        self._events = events

    async def __call__(self, command: UpdateStaffCommand) -> User:
        user = await _find_in_restaurant(self._users, command.restaurant_id, command.user_id)

        role_changed = command.role is not None and command.role is not user.role
        if command.role is not None:
            ensure_can_change_role(command.actor_id, user, command.role)
            user.role = command.role
        if command.full_name is not None:
            user.rename(command.full_name)

        saved = await self._users.save(user)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STAFF_UPDATED,
            f"{saved.full_name} ({saved.role.label})",
        )
        if role_changed:
            _notify_account_changed(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class ChangeStaffStatusCommand:
    restaurant_id: int
    actor_id: int
    user_id: int
    is_active: bool


class ChangeStaffStatus:
    def __init__(
        self, users: UserRepository, activity: ActivityRecorder, events: EventPublisher
    ) -> None:
        self._users = users
        self._activity = activity
        self._events = events

    async def __call__(self, command: ChangeStaffStatusCommand) -> User:
        user = await _find_in_restaurant(self._users, command.restaurant_id, command.user_id)

        if command.is_active:
            user.activate()
        else:
            ensure_can_deactivate(command.actor_id, user)
            user.deactivate()

        saved = await self._users.save(user)
        state = "activada" if command.is_active else "desactivada"
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STAFF_STATUS_CHANGED,
            f"{saved.full_name}: cuenta {state}",
        )
        _notify_account_changed(self._events, saved)
        return saved


@dataclass(frozen=True, slots=True)
class ResetStaffPasswordCommand:
    restaurant_id: int
    actor_id: int
    user_id: int
    new_password: str


class ResetStaffPassword:
    """El encargado le fija una contraseña nueva a quien la olvidó.

    No hay recuperación por correo: en un local chico, quien olvida la clave se
    la pide al encargado, que se la dicta en persona.
    """

    def __init__(
        self, users: UserRepository, hasher: PasswordHasher, activity: ActivityRecorder
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._activity = activity

    async def __call__(self, command: ResetStaffPasswordCommand) -> None:
        user = await _find_in_restaurant(self._users, command.restaurant_id, command.user_id)
        ensure_can_reset_password(command.actor_id, user)

        user.password_hash = self._hasher.hash(validate_new_password(command.new_password))
        await self._users.save(user)
        await self._activity.record(
            command.restaurant_id,
            command.actor_id,
            ActivityKind.STAFF_PASSWORD_RESET,
            user.full_name,
        )


@dataclass(frozen=True, slots=True)
class RegisterFirstAdminCommand:
    restaurant_id: int
    email: str
    full_name: str
    password: str


class RegisterFirstAdmin:
    """El primer encargado de un restaurante recién creado.

    Es el único alta sin un actor autenticado, así que se limita a un
    restaurante vacío: no sirve para meterle un encargado a un local que ya
    tiene dueño.
    """

    def __init__(self, users: UserRepository, hasher: PasswordHasher) -> None:
        self._users = users
        self._hasher = hasher

    async def __call__(self, command: RegisterFirstAdminCommand) -> User:
        existing = await self._users.search(UserQuery(restaurant_id=command.restaurant_id, limit=1))
        if existing.total:
            raise RestaurantAlreadyHasStaff(command.restaurant_id)

        email = normalize_email(command.email)
        if await self._users.exists_with_email(email):
            raise EmailAlreadyRegistered(email)

        return await self._users.add(
            User(
                restaurant_id=command.restaurant_id,
                email=email,
                full_name=command.full_name,
                role=Role.ADMIN,
                password_hash=self._hasher.hash(validate_new_password(command.password)),
            )
        )
