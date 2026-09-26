"""Contrato HTTP de la administración del sistema."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from resthub.core.credentials import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from resthub.modules.platform.domain.entities import MAX_FULL_NAME_LENGTH, PlatformAdmin
from resthub.modules.platform.ports.activity_log import PlatformActivityEntry
from resthub.modules.platform.ports.restaurants import OwnerAccount, RestaurantSummary
from resthub.modules.platform.use_cases.manage_restaurants import RestaurantDetail

# Los mismos topes que el módulo `restaurants`; el dominio de ese módulo es el
# que decide, esto solo corta antes lo que seguro no entra.
MAX_RESTAURANT_NAME_LENGTH = 120
MAX_SLUG_LENGTH = 60
MAX_TIMEZONE_LENGTH = 64


class PlatformLoginRequest(BaseModel):
    email: EmailStr
    # Sin mínimo, como en el acceso del personal: no se anuncia cuánto mide una válida.
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class PlatformAdminResponse(BaseModel):
    id: int
    full_name: str
    email: str

    @classmethod
    def from_entity(cls, admin: PlatformAdmin) -> PlatformAdminResponse:
        return cls(id=admin.id or 0, full_name=admin.full_name, email=admin.email)


class PlatformSessionResponse(BaseModel):
    admin: PlatformAdminResponse


class PlatformAccessTokenResponse(PlatformSessionResponse):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RestaurantSummaryResponse(BaseModel):
    id: int
    name: str
    slug: str
    timezone: str
    is_active: bool
    created_at: datetime
    staff_count: int
    active_staff_count: int

    @classmethod
    def from_summary(cls, summary: RestaurantSummary) -> RestaurantSummaryResponse:
        return cls(
            id=summary.id,
            name=summary.name,
            slug=summary.slug,
            timezone=summary.timezone,
            is_active=summary.is_active,
            created_at=summary.created_at,
            staff_count=summary.staff_count,
            active_staff_count=summary.active_staff_count,
        )


class RestaurantPageResponse(BaseModel):
    items: list[RestaurantSummaryResponse]
    total: int


class OwnerResponse(BaseModel):
    id: int
    full_name: str
    email: str
    is_active: bool

    @classmethod
    def from_account(cls, owner: OwnerAccount) -> OwnerResponse:
        return cls(
            id=owner.id, full_name=owner.full_name, email=owner.email, is_active=owner.is_active
        )


class RestaurantDetailResponse(RestaurantSummaryResponse):
    # Las cuentas cuyo rol es el Encargado del local.
    owners: list[OwnerResponse]

    @classmethod
    def from_detail(cls, detail: RestaurantDetail) -> RestaurantDetailResponse:
        return cls(
            **RestaurantSummaryResponse.from_summary(detail.summary).model_dump(),
            owners=[OwnerResponse.from_account(owner) for owner in detail.owners],
        )


class NewOwnerRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=MAX_FULL_NAME_LENGTH)
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class CreateRestaurantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_RESTAURANT_NAME_LENGTH)
    # La forma (minúsculas, números y guiones) la comprueba el dominio de `restaurants`.
    slug: str = Field(min_length=1, max_length=MAX_SLUG_LENGTH)
    timezone: str = Field(min_length=1, max_length=MAX_TIMEZONE_LENGTH)
    owner: NewOwnerRequest


class PlatformUpdateRestaurantRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_RESTAURANT_NAME_LENGTH)
    timezone: str | None = Field(default=None, min_length=1, max_length=MAX_TIMEZONE_LENGTH)
    is_active: bool | None = None


class PlatformActivityResponse(BaseModel):
    id: int
    admin_id: int
    admin_name: str
    kind: str
    kind_label: str
    detail: str
    created_at: datetime

    @classmethod
    def from_entry(cls, entry: PlatformActivityEntry) -> PlatformActivityResponse:
        return cls(
            id=entry.record.id or 0,
            admin_id=entry.record.admin_id,
            admin_name=entry.admin_name,
            kind=entry.record.kind.value,
            kind_label=entry.record.kind.label,
            detail=entry.record.detail,
            created_at=entry.record.created_at,
        )


class PlatformActivityPageResponse(BaseModel):
    items: list[PlatformActivityResponse]
    total: int
