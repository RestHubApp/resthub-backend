"""Contrato HTTP del módulo de cuentas.

Estos esquemas son del adaptador, no del dominio. Ninguno acepta un
`restaurant_id`: el restaurante sale siempre de la credencial.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field

from resthub.core.access_history import AccessEntry, AccessOutcome
from resthub.core.account_sessions import AccountSession
from resthub.core.api_params import EntityId
from resthub.core.pagination import Page
from resthub.core.permissions import (
    CATALOG,
    PLATFORM_CATALOG,
    Permission,
    PlatformPermission,
    RoleKind,
    ordered_catalog,
    platform_permissions,
)
from resthub.modules.accounts.domain.entities import (
    MAX_DEACTIVATION_REASON_LENGTH,
    MAX_FULL_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PRIVACY_VERSION,
    TERMS_VERSION,
    User,
)
from resthub.modules.accounts.domain.roles import MAX_ROLE_NAME_LENGTH
from resthub.modules.accounts.ports.platform_sign_in import PlatformAccount
from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary
from resthub.modules.accounts.use_cases.manage_roles import RoleView
from resthub.modules.accounts.use_cases.read_session import CurrentSession
from resthub.modules.accounts.use_cases.review_access import UnusedAccount


class LoginRequest(BaseModel):
    email: EmailStr
    # Sin longitud mínima: validar aquí diría cuánto mide una contraseña válida
    # y convertiría el formulario de acceso en un oráculo.
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class PreviewExchangeRequest(BaseModel):
    # El código de `POST /platform/preview`. Sin forma exigida más allá del
    # largo: uno mal escrito es uno inválido, con el mismo 401.
    code: str = Field(min_length=1, max_length=128)


class ChangeOwnPasswordRequest(BaseModel):
    # Sin mínimo, por el mismo motivo que en el acceso.
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class ReauthenticateRequest(BaseModel):
    # Sin mínimo, por el mismo motivo que en el acceso.
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ReauthenticationResponse(BaseModel):
    """Hasta cuándo valen las acciones críticas en esta sesión (RFS-21)."""

    reauthenticated_at: datetime
    valid_until: datetime


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


class StaffSessionResponse(BaseModel):
    """La cuenta del personal, su restaurante y lo que su rol le deja hacer.

    La interfaz arma la navegación con `permissions`; la API igual rechaza lo
    no permitido, así que ocultar es comodidad y no seguridad.
    """

    kind: Literal["staff"] = "staff"
    user: SessionUserResponse
    restaurant: SessionRestaurantResponse
    permissions: list[Permission]
    # La administración del sistema mirando como esta cuenta del local de
    # muestra. La interfaz muestra la franja de vista previa y no ofrece
    # cerrar sesión ni cambiar la contraseña. Sin valor por omisión para que el
    # esquema lo marque obligatorio: viaja siempre, `false` en una sesión común.
    preview: bool
    # Los términos y la política de privacidad vigentes, y si esta cuenta ya
    # los aceptó. Sin aceptarlos, la interfaz no deja trabajar.
    terms_version: str
    terms_accepted: bool
    # Lo mismo para el tratamiento de datos personales, que se acepta aparte.
    privacy_version: str
    privacy_accepted: bool
    # La contraseña es temporal (la puso el encargado o la plataforma): la
    # interfaz pide cambiarla antes de nada y el resto de la API responde 403
    # `password_change_required`. Siempre `false` en una vista previa.
    must_change_password: bool
    # El valor para `X-CSRF-Token` cuando la sesión va en la cookie (S2). Lo traen
    # el login, la renovación y `GET /auth/me` (que lo lee de la cookie, por si la
    # interfaz lo perdió); nulo sin cookie, como en la vista previa.
    csrf_token: str | None = None

    @classmethod
    def from_session(
        cls, session: CurrentSession, *, preview: bool = False
    ) -> StaffSessionResponse:
        return cls(
            user=SessionUserResponse.from_entity(session.user),
            restaurant=SessionRestaurantResponse.from_summary(session.restaurant),
            permissions=sorted(session.permissions),
            preview=preview,
            terms_version=TERMS_VERSION,
            terms_accepted=session.user.has_current_terms,
            privacy_version=PRIVACY_VERSION,
            privacy_accepted=session.user.has_current_privacy,
            must_change_password=session.user.must_change_password and not preview,
        )


class SessionAdminResponse(BaseModel):
    id: int
    email: str
    full_name: str

    @classmethod
    def from_account(cls, account: PlatformAccount) -> SessionAdminResponse:
        return cls(id=account.id, email=account.email, full_name=account.full_name)


class PlatformAdminSessionResponse(BaseModel):
    """La cuenta de la administración del sistema y lo que puede hacer.

    No pertenece a ningún restaurante: no trae `restaurant` ni términos. La
    interfaz arma la navegación con `permissions`, como con el personal.
    """

    kind: Literal["platform"] = "platform"
    admin: SessionAdminResponse
    permissions: list[PlatformPermission]
    # Una cuenta de plataforma nunca está en vista previa: esa sesión es la del
    # personal del local de muestra. Viaja para que la unión se lea igual.
    preview: bool = False
    # El valor para `X-CSRF-Token` cuando la sesión va en la cookie (S2).
    csrf_token: str | None = None

    @classmethod
    def from_account(cls, account: PlatformAccount) -> PlatformAdminSessionResponse:
        return cls(
            admin=SessionAdminResponse.from_account(account),
            permissions=_platform_in_catalog_order(platform_permissions()),
        )


# Lo que responden `/auth/me`, `/auth/login` y `/auth/refresh`: según la
# cuenta, una sesión del personal o una de plataforma, distinguidas por `kind`.
SessionResponse = Annotated[
    StaffSessionResponse | PlatformAdminSessionResponse, Field(discriminator="kind")
]


class AcceptTermsRequest(BaseModel):
    # Dos consentimientos explícitos y separados: la versión de los términos y
    # condiciones y la del tratamiento de datos personales que se mostraron. Si
    # alguna ya cambió, el servidor responde 409; sin una de las dos, 422.
    terms_version: str = Field(min_length=1, max_length=20)
    privacy_version: str = Field(min_length=1, max_length=20)


class StaffAccessTokenResponse(StaffSessionResponse):
    """El token y, en la misma respuesta, la sesión que abre.

    Así la interfaz no necesita una segunda petición a `/auth/me` para
    dibujarse después de entrar.
    """

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - tipo de token de OAuth2, no una contraseña
    expires_in: int

    @classmethod
    def issued(
        cls, session: CurrentSession, access_token: str, expires_in: int, *, preview: bool = False
    ) -> StaffAccessTokenResponse:
        return cls(
            **StaffSessionResponse.from_session(session, preview=preview).model_dump(),
            access_token=access_token,
            expires_in=expires_in,
        )


class PlatformAdminAccessTokenResponse(PlatformAdminSessionResponse):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - tipo de token de OAuth2, no una contraseña
    expires_in: int

    @classmethod
    def issued(
        cls, account: PlatformAccount, access_token: str, expires_in: int
    ) -> PlatformAdminAccessTokenResponse:
        return cls(
            **PlatformAdminSessionResponse.from_account(account).model_dump(),
            access_token=access_token,
            expires_in=expires_in,
        )


AccessTokenResponse = Annotated[
    StaffAccessTokenResponse | PlatformAdminAccessTokenResponse, Field(discriminator="kind")
]


class StaffMemberResponse(BaseModel):
    id: int
    full_name: str
    email: str
    role_id: int
    role_label: str
    is_active: bool
    created_at: datetime
    # Si la cuenta está desactivada: cuándo, quién (id de su cuenta) y por qué.
    deactivated_at: datetime | None = None
    deactivated_by: int | None = None
    deactivation_reason: str = ""
    # La contraseña es temporal: la cuenta todavía no eligió la suya.
    must_change_password: bool = False

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
            deactivated_at=user.deactivated_at,
            deactivated_by=user.deactivated_by,
            deactivation_reason=user.deactivation_reason,
            must_change_password=user.must_change_password,
        )


class StaffPageResponse(BaseModel):
    items: list[StaffMemberResponse]
    total: int


class RegisterStaffRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=MAX_FULL_NAME_LENGTH)
    role_id: EntityId
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class UpdateStaffRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=MAX_FULL_NAME_LENGTH)
    role_id: EntityId | None = None


class ChangeStaffStatusRequest(BaseModel):
    is_active: bool
    # Por qué se desactiva (RFS-18): obligatorio al desactivar, 422 si falta.
    reason: str | None = Field(default=None, max_length=MAX_DEACTIVATION_REASON_LENGTH)


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


class AccessEntryResponse(BaseModel):
    """Un intento de acceso de una cuenta (RFS-15). La IP va sin su último bloque."""

    id: int
    occurred_at: datetime
    outcome: AccessOutcome
    device: str
    network: str
    city: str
    country: str
    new_device: bool
    new_country: bool

    @classmethod
    def from_entry(cls, entry: AccessEntry) -> AccessEntryResponse:
        return cls(
            id=entry.id,
            occurred_at=entry.occurred_at,
            outcome=entry.outcome,
            device=entry.device,
            network=entry.network,
            city=entry.city,
            country=entry.country,
            new_device=entry.new_device,
            new_country=entry.new_country,
        )


class AccessHistoryPageResponse(BaseModel):
    items: list[AccessEntryResponse]
    total: int

    @classmethod
    def from_page(cls, page: Page[AccessEntry]) -> AccessHistoryPageResponse:
        return cls(
            items=[AccessEntryResponse.from_entry(entry) for entry in page.items],
            total=page.total,
        )


class UnusedStaffResponse(BaseModel):
    """Una cuenta activa que nadie usa hace tiempo (RFS-24)."""

    member: StaffMemberResponse
    # `None` si nunca entró: los días cuentan desde el alta.
    last_activity_at: datetime | None
    unused_days: int

    @classmethod
    def from_account(cls, account: UnusedAccount) -> UnusedStaffResponse:
        return cls(
            member=StaffMemberResponse.from_entity(account.user),
            last_activity_at=account.last_activity_at,
            unused_days=account.unused_days,
        )


class DeviceSessionResponse(BaseModel):
    """Una sesión abierta en un dispositivo (RFS-17). La IP va sin su último bloque."""

    # Nombre público para cerrarla; no es el identificador que lleva el token.
    id: str
    device: str
    network: str
    city: str
    country: str
    created_at: datetime
    last_seen_at: datetime
    # La sesión de quien pregunta.
    current: bool

    @classmethod
    def from_session(cls, state: AccountSession, *, current: bool) -> DeviceSessionResponse:
        return cls(
            id=state.ref,
            device=state.device,
            network=state.network,
            city=state.city,
            country=state.country,
            created_at=state.created_at,
            last_seen_at=state.last_seen_at,
            current=current,
        )


def _in_catalog_order(permissions: frozenset[Permission]) -> list[Permission]:
    return [permission for permission in ordered_catalog() if permission in permissions]


def _platform_in_catalog_order(
    permissions: frozenset[PlatformPermission],
) -> list[PlatformPermission]:
    return [permission for permission in PLATFORM_CATALOG if permission in permissions]


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
