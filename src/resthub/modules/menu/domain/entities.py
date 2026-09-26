"""Entidades de dominio del menú.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic. Los contratos de Import
Linter lo verifican.

Un plato tiene dos interruptores y no uno, porque responden a preguntas
distintas. `is_active` es si el plato sigue en la carta; `is_available` es si
hoy se puede pedir (se acabó el pescado, no llegó la chicha). El primero lo
toca el encargado de vez en cuando; el segundo, varias veces por turno.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from resthub.modules.menu.domain.exceptions import (
    InvalidMenuCategory,
    InvalidMenuItem,
    InvalidOrdering,
)
from resthub.modules.menu.domain.modifiers import ModifierGroup, validate_modifier_groups

MAX_CATEGORY_NAME_LENGTH = 60
MAX_ITEM_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 300
# Lo que entra en la columna `Numeric(10, 2)`.
MAX_PRICE = Decimal("99999999.99")
CENT = Decimal("0.01")


@dataclass(slots=True)
class MenuCategory:
    restaurant_id: int
    name: str
    position: int = 0
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = validate_category_name(self.name)

    def rename(self, name: str) -> None:
        self.name = validate_category_name(name)


@dataclass(slots=True)
class MenuItem:
    restaurant_id: int
    category_id: int
    name: str
    price: Decimal
    description: str = ""
    is_available: bool = True
    is_active: bool = True
    position: int = 0
    # Tamaño, término, extras: lo que el mesero elige al pedir el plato.
    modifier_groups: tuple[ModifierGroup, ...] = ()
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # No se guarda: lo calcula la lectura del menú con el stock del momento.
    # Un plato cuya receta pide más de lo que hay no se puede pedir.
    out_of_stock: bool = False

    def __post_init__(self) -> None:
        self.name = validate_item_name(self.name)
        self.description = validate_description(self.description)
        self.price = validate_price(self.price)
        self.modifier_groups = validate_modifier_groups(self.modifier_groups)

    def set_modifiers(self, groups: Sequence[ModifierGroup]) -> None:
        self.modifier_groups = validate_modifier_groups(groups)

    def rename(self, name: str) -> None:
        self.name = validate_item_name(name)

    def describe(self, description: str) -> None:
        self.description = validate_description(description)

    def reprice(self, price: Decimal) -> None:
        self.price = validate_price(price)

    @property
    def can_be_ordered(self) -> bool:
        return self.is_active and self.is_available and not self.out_of_stock


@dataclass(frozen=True, slots=True)
class MenuSection:
    """Una categoría con sus platos, en el orden en que se muestran."""

    category: MenuCategory
    items: list[MenuItem]


def validate_category_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidMenuCategory("El nombre de la categoría no puede quedar vacío.")
    if len(name) > MAX_CATEGORY_NAME_LENGTH:
        raise InvalidMenuCategory(
            f"El nombre de la categoría no puede pasar de {MAX_CATEGORY_NAME_LENGTH} caracteres."
        )
    return name


def validate_item_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidMenuItem("El nombre del plato no puede quedar vacío.")
    if len(name) > MAX_ITEM_NAME_LENGTH:
        raise InvalidMenuItem(
            f"El nombre del plato no puede pasar de {MAX_ITEM_NAME_LENGTH} caracteres."
        )
    return name


def validate_description(raw: str) -> str:
    description = raw.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise InvalidMenuItem(
            f"La descripción no puede pasar de {MAX_DESCRIPTION_LENGTH} caracteres."
        )
    return description


def validate_price(raw: Decimal) -> Decimal:
    # Se rechaza en vez de redondear: si alguien escribió 12.345, redondear en
    # silencio cobraría un precio que nadie decidió.
    if not raw.is_finite() or raw != raw.quantize(CENT):
        raise InvalidMenuItem("El precio admite como máximo dos decimales.")
    if raw <= 0:
        raise InvalidMenuItem("El precio tiene que ser mayor que cero.")
    if raw > MAX_PRICE:
        raise InvalidMenuItem("El precio es demasiado alto.")
    return raw.quantize(CENT)


def reorder[T: (MenuCategory, MenuItem)](elements: Sequence[T], ids: Sequence[int]) -> list[T]:
    """Asigna posiciones siguiendo el orden de `ids`.

    Exige una permutación exacta de la lista actual: un identificador de más
    (quizá de otro restaurante) o de menos dejaría posiciones repetidas o
    elementos fuera del orden que eligió el encargado.
    """
    by_id = {element.id: element for element in elements}
    if len(ids) != len(by_id) or set(ids) != set(by_id):
        raise InvalidOrdering()
    ordered = [by_id[element_id] for element_id in ids]
    for position, element in enumerate(ordered):
        element.position = position
    return ordered
