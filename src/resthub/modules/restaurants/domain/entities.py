"""Entidad de dominio de restaurantes.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic. Los contratos de Import
Linter lo verifican.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from resthub.core.local_time import DEFAULT_TIMEZONE, is_valid_timezone
from resthub.modules.restaurants.domain.exceptions import (
    InvalidDiscountLimit,
    InvalidRestaurantName,
    InvalidSlug,
    InvalidTimezone,
)

MAX_NAME_LENGTH = 120
MAX_SLUG_LENGTH = 60
# El identificador corto sirve en URLs y en el nombre de archivos exportados,
# así que se limita a lo que no necesita escaparse en ninguno de los dos.
_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Lo que un mesero puede descontar sin el encargado, si nadie lo cambió.
DEFAULT_WAITER_DISCOUNT_PERCENT = Decimal("10.00")
_CENT = Decimal("0.01")


@dataclass(slots=True)
class Restaurant:
    name: str
    slug: str
    timezone: str = DEFAULT_TIMEZONE
    # Tope del descuento que aplica un mesero al cobrar; por encima, y las
    # cortesías, solo el encargado.
    max_waiter_discount_percent: Decimal = DEFAULT_WAITER_DISCOUNT_PERCENT
    # Un plato cuya receta pide más insumo del que hay se muestra agotado y no
    # se puede pedir. Se apaga si el local todavía no lleva el stock al día.
    auto_out_of_stock: bool = True
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = validate_name(self.name)
        self.slug = validate_slug(self.slug)
        self.timezone = validate_timezone(self.timezone)
        self.max_waiter_discount_percent = validate_discount_limit(self.max_waiter_discount_percent)

    def rename(self, name: str) -> None:
        self.name = validate_name(name)

    def move_to_timezone(self, timezone: str) -> None:
        self.timezone = validate_timezone(timezone)

    def limit_waiter_discount(self, percent: Decimal) -> None:
        self.max_waiter_discount_percent = validate_discount_limit(percent)


def validate_name(raw: str) -> str:
    # Espacios repetidos en el medio también se colapsan: el nombre sale en el
    # encabezado de la aplicación y en los comprobantes.
    name = " ".join(raw.split())
    if not name:
        raise InvalidRestaurantName("El nombre del restaurante no puede quedar vacío.")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidRestaurantName(
            f"El nombre del restaurante no puede pasar de {MAX_NAME_LENGTH} caracteres."
        )
    return name


def validate_slug(raw: str) -> str:
    slug = raw.strip().lower()
    if len(slug) > MAX_SLUG_LENGTH or not _SLUG_PATTERN.fullmatch(slug):
        raise InvalidSlug(raw)
    return slug


def validate_discount_limit(raw: Decimal) -> Decimal:
    percent = Decimal(raw)
    if percent < 0 or percent > 100:
        raise InvalidDiscountLimit("El descuento máximo del mesero va de 0 a 100 %.")
    if percent != percent.quantize(_CENT):
        raise InvalidDiscountLimit("El descuento máximo admite como máximo dos decimales.")
    return percent.quantize(_CENT)


def validate_timezone(raw: str) -> str:
    timezone = raw.strip()
    if not is_valid_timezone(timezone):
        raise InvalidTimezone(raw)
    return timezone
