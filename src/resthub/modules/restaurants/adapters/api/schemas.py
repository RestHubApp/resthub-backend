"""Contrato HTTP del módulo de restaurantes.

No hay ningún `restaurant_id` en los cuerpos de entrada: el restaurante sale
siempre de la credencial de quien pregunta.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.core.opening_hours import DAYS_IN_WEEK, MAX_PERIODS, OpeningPeriod
from resthub.modules.restaurants.domain.entities import (
    MAX_IDLE_LOCK_MINUTES,
    MAX_NAME_LENGTH,
    Restaurant,
)


class OpeningPeriodResponse(BaseModel):
    """Un tramo del horario: el día (0 lunes … 6 domingo) y las horas del local.

    Un cierre igual o anterior a la apertura cruza la medianoche.
    """

    day: int = Field(ge=0, lt=DAYS_IN_WEEK)
    opens: time
    closes: time

    @classmethod
    def from_entity(cls, period: OpeningPeriod) -> OpeningPeriodResponse:
        return cls(day=period.day, opens=period.opens, closes=period.closes)


class OpeningPeriodRequest(BaseModel):
    day: int = Field(ge=0, lt=DAYS_IN_WEEK)
    opens: time
    closes: time

    def to_entity(self) -> OpeningPeriod:
        return OpeningPeriod(
            self.day,
            self.opens.replace(second=0, microsecond=0),
            self.closes.replace(second=0, microsecond=0),
        )


class RestaurantResponse(BaseModel):
    id: int
    name: str
    slug: str
    timezone: str
    # Lo que un mesero puede descontar al cobrar sin el encargado.
    max_waiter_discount_percent: Decimal
    # Agotar solos los platos cuya receta no alcanza con el stock.
    auto_out_of_stock: bool
    # Enviar notas y motivos de merma, sin datos personales, a la IA externa.
    external_ai_enabled: bool
    # Desde qué monto (soles, a costo vigente) una merma espera aprobación; `null`, nunca.
    waste_approval_threshold: Decimal | None
    # Minutos sin uso tras los que la sesión pide el PIN (RFS-29); 0, nunca.
    idle_lock_minutes: int
    # El horario de atención (RFS-32); vacío, sin horario.
    opening_hours: list[OpeningPeriodResponse]
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, restaurant: Restaurant) -> RestaurantResponse:
        return cls(
            id=restaurant.id or 0,
            name=restaurant.name,
            slug=restaurant.slug,
            timezone=restaurant.timezone,
            max_waiter_discount_percent=restaurant.max_waiter_discount_percent,
            auto_out_of_stock=restaurant.auto_out_of_stock,
            external_ai_enabled=restaurant.external_ai_enabled,
            waste_approval_threshold=restaurant.waste_approval_threshold,
            idle_lock_minutes=restaurant.idle_lock_minutes,
            opening_hours=[OpeningPeriodResponse.from_entity(p) for p in restaurant.opening_hours],
            is_active=restaurant.is_active,
            created_at=restaurant.created_at,
        )


class UpdateRestaurantRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    max_waiter_discount_percent: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )
    auto_out_of_stock: bool | None = None
    external_ai_enabled: bool | None = None
    # Omitido lo deja como está; `null` apaga la aprobación de mermas.
    waste_approval_threshold: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    # 0 apaga el bloqueo por inactividad.
    idle_lock_minutes: int | None = Field(default=None, ge=0, le=MAX_IDLE_LOCK_MINUTES)
    # Omitido lo deja como está; una lista vacía quita el horario.
    opening_hours: list[OpeningPeriodRequest] | None = Field(default=None, max_length=MAX_PERIODS)
