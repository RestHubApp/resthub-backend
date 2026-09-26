"""Contrato HTTP del módulo de cuentas.

Estos esquemas son del adaptador, no del dominio. Ninguno acepta un
`restaurant_id`: el restaurante sale siempre de la credencial.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from resthub.core.permissions import CATALOG, Permission, RoleKind, ordered_catalog
from resthub.modules.accounts.domain.entities import (
    MAX_FULL_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    User,
)
from resthub.modules.accounts.domain.roles import MAX_ROLE_NAME_LENGTH
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.use_cases.manage_roles import RoleView
from resthub.modules.accounts.use_cases.read_session import CurrentSession


class LoginRequest(BaseModel):
    email: EmailStr
    # Sin longitud mínima: validar aquí diría cuánto mide una contraseña válida
    # y convertiría el formulario de acceso en un oráculo.
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangeOwnPasswordRequest(BaseModel):
    # Sin mínimo, por el mismo motivo que en el acceso.
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class SessionUserResponse(BaseModel):
    id: int
    full_name: str
    email: str
    role_id: int
    # El nombre del rol, que pone cada restaurante, listo para mostrar.
    role_label: str

    @classmethod
    def from_entity(cls, user: User) -> SessionUserResponse:
        return cls(
            id=user.id or 0,
            full_name=user.full_name,
            email=user.email,
            role_id=user.role.id or 0,
            role_label=user.role.name,
        )


class SessionRestaurantResponse(BaseModel):
    id: int
    name: str
    slug: str
    # Zona IANA del local. Viaja con la sesión para que la interfaz muestre
    # horas y días del restaurante desde el primer dibujo, sin otra petición.
    timezone: str

    @classmethod
    def from_summary(cls, restaurant: RestaurantSummary) -> SessionRestaurantResponse:
        return cls(
            id=restaurant.id,
            name=restaurant.name,
            slug=restaurant.slug,
            timezone=restaurant.timezone,
        )


class SessionResponse(BaseModel):
    """La cuenta propia, su restaurante y lo que su rol le deja hacer.

    La interfaz arma la navegación con `permissions`; la API igual rechaza lo
    no permitido, así que ocultar es comodidad y no seguridad.
    """

    user: SessionUserResponse
    restaurant: SessionRestaurantResponse
    permissions: list[Permission]

    @classmethod
    def from_session(cls, session: CurrentSession) -> SessionResponse:
        return cls(
            user=SessionUserResponse.from_entity(session.user),
            restaurant=SessionRestaurantResponse.from_summary(session.restaurant),
            permissions=sorted(session.permissions),
        )


class AccessTokenResponse(SessionResponse):
    """El token y, en la misma respuesta, la sesión que abre.

    Así la interfaz no necesita una segunda petición a `/auth/me` para
    dibujarse después de entrar.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int

    @classmethod
    def issued(
        cls, session: CurrentSession, access_token: str, expires_in: int
    ) -> AccessTokenResponse:
        return cls(
            user=SessionUserResponse.from_entity(session.user),
            restaurant=SessionRestaurantResponse.from_summary(session.restaurant),
            permissions=sorted(session.permissions),
            access_token=access_token,
            expires_in=expires_in,
        )


class StaffMemberResponse(BaseModel):
    id: int
    full_name: str
    email: str
    role_id: int
    role_label: str
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, user: User) -> StaffMemberResponse:
        return cls(
            id=user.id or 0,
            full_name=user.full_name,
            email=user.email,
            role_id=user.role.id or 0,
            role_label=user.role.name,
            is_active=user.is_active,
            created_at=user.created_at,
        )


class StaffPageResponse(BaseModel):
    items: list[StaffMemberResponse]
    total: int


class RegisterStaffRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=MAX_FULL_NAME_LENGTH)
    role_id: int
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class UpdateStaffRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=MAX_FULL_NAME_LENGTH)
    role_id: int | None = None


class ChangeStaffStatusRequest(BaseModel):
    is_active: bool


class ResetStaffPasswordRequest(BaseModel):
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class ActivityResponse(BaseModel):
    id: int
    kind: str
    kind_label: str
    detail: str
    occurred_at: datetime
    user_id: int
    user_name: str
    user_role_id: int
    user_role_label: str


class ActivityPageResponse(BaseModel):
    items: list[ActivityResponse]
    total: int


def _in_catalog_order(permissions: frozenset[Permission]) -> list[Permission]:
    return [permission for permission in ordered_catalog() if permission in permissions]


class PermissionResponse(BaseModel):
    code: Permission
    label: str
    group: str

    @classmethod
    def catalog(cls) -> list[PermissionResponse]:
        return [
            cls(code=permission, label=CATALOG[permission].label, group=CATALOG[permission].group)
            for permission in ordered_catalog()
        ]


class RoleResponse(BaseModel):
    id: int
    name: str
    kind: RoleKind
    # Los que rigen de verdad: el encargado siempre trae el catálogo entero.
    permissions: list[Permission]
    member_count: int
    is_editable: bool
    # Solo un rol propio y sin personal; la interfaz lo usa para ofrecer el botón.
    is_deletable: bool

    @classmethod
    def from_view(cls, view: RoleView) -> RoleResponse:
        return cls(
            id=view.role.id or 0,
            name=view.role.name,
            kind=view.role.kind,
            permissions=_in_catalog_order(view.role.permissions),
            member_count=view.member_count,
            is_editable=view.role.is_editable,
            is_deletable=view.is_deletable,
        )


class RoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_ROLE_NAME_LENGTH)
    permissions: list[Permission]
