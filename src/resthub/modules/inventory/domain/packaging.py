"""Presentaciones de un insumo y cómo se pasa una cantidad a su unidad base.

Python puro. El libro de stock guarda todo en la unidad del insumo (g, ml o
unidades), pero en el almacén nadie cuenta mililitros: cuenta botellas, y una
botella abierta se pesa. La presentación dice cuánto trae un envase y, si se
quiere contar por peso, cuánto pesa lleno y vacío. La conversión se hace aquí,
en el backend, para que la pantalla y el reporte digan lo mismo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from resthub.modules.inventory.domain.exceptions import (
    InvalidPackagingError,
    InvalidQuantityEntryError,
)

MAX_PACKAGE_LABEL_LENGTH = 30
_QUANTITY_STEP = Decimal("0.001")
_MAX_QUANTITY = Decimal("999999999.999")


def _quantity(
    raw: Decimal, what: str, error: type[InvalidPackagingError | InvalidQuantityEntryError]
) -> Decimal:
    value = Decimal(raw)
    if not value.is_finite() or abs(value) > _MAX_QUANTITY:
        message = f"{what} no es válido."
        raise error(message)
    if value != value.quantize(_QUANTITY_STEP):
        message = f"{what} admite como máximo tres decimales."
        raise error(message)
    return value.quantize(_QUANTITY_STEP)


@dataclass(frozen=True, slots=True)
class Packaging:
    """Cómo viene un insumo: «botella de 750 ml», «saco de 50000 g».

    Los pesos del envase son opcionales y van juntos: sin ellos el insumo se
    cuenta por envases o en su unidad, pero no por peso.
    """

    # Cuánto trae un envase, en la unidad del insumo.
    size: Decimal
    label: str = ""
    full_weight_g: Decimal | None = None
    empty_weight_g: Decimal | None = None

    def __post_init__(self) -> None:
        size = _quantity(self.size, "El tamaño de la presentación", InvalidPackagingError)
        if size <= 0:
            message = "El tamaño de la presentación tiene que ser mayor que cero."
            raise InvalidPackagingError(message)
        object.__setattr__(self, "size", size)
        label = " ".join(self.label.split())
        if len(label) > MAX_PACKAGE_LABEL_LENGTH:
            message = f"El nombre de la presentación admite {MAX_PACKAGE_LABEL_LENGTH} caracteres."
            raise InvalidPackagingError(message)
        object.__setattr__(self, "label", label)
        self._check_weights()

    def _check_weights(self) -> None:
        if self.full_weight_g is None and self.empty_weight_g is None:
            return
        if self.full_weight_g is None or self.empty_weight_g is None:
            message = "Para contar por peso indica el peso del envase lleno y el vacío."
            raise InvalidPackagingError(message)
        full = _quantity(self.full_weight_g, "El peso lleno", InvalidPackagingError)
        empty = _quantity(self.empty_weight_g, "El peso vacío", InvalidPackagingError)
        if empty <= 0:
            message = "El peso del envase vacío tiene que ser mayor que cero."
            raise InvalidPackagingError(message)
        if full <= empty:
            message = "El envase lleno tiene que pesar más que vacío."
            raise InvalidPackagingError(message)
        object.__setattr__(self, "full_weight_g", full)
        object.__setattr__(self, "empty_weight_g", empty)

    @property
    def weighable(self) -> bool:
        return self.full_weight_g is not None and self.empty_weight_g is not None

    def content_by_weight(self, weight_g: Decimal) -> Decimal:
        """Lo que queda en un envase abierto, según lo que pesa.

        Contenido = (peso - vacío) / (lleno - vacío) x tamaño, acotado entre
        cero y el tamaño: una balanza que marca de más o de menos no inventa
        stock ni lo vuelve negativo.
        """
        if self.full_weight_g is None or self.empty_weight_g is None:
            message = "Este insumo no tiene los pesos del envase: no se puede contar por peso."
            raise InvalidQuantityEntryError(message)
        weight = _quantity(weight_g, "El peso", InvalidQuantityEntryError)
        if weight < 0:
            message = "El peso no puede ser negativo."
            raise InvalidQuantityEntryError(message)
        ratio = (weight - self.empty_weight_g) / (self.full_weight_g - self.empty_weight_g)
        content = (ratio * self.size).quantize(_QUANTITY_STEP, ROUND_HALF_UP)
        return min(max(content, Decimal("0.000")), self.size)


class EntryMode(StrEnum):
    """Cómo se ingresó una cantidad."""

    # En la unidad del insumo.
    BASE = "base"
    # En envases: n x tamaño de la presentación.
    PACKAGES = "packages"
    # Pesando un envase abierto (solo en conteos).
    WEIGHT = "weight"


@dataclass(frozen=True, slots=True)
class QuantityEntry:
    """Lo que escribió quien cargó la cantidad, antes de pasarla a la unidad base.

    En `WEIGHT`, `quantity` es el peso en gramos de un envase abierto y
    `full_packages` los envases cerrados que hay además; en los otros modos
    `full_packages` no se usa.
    """

    mode: EntryMode
    quantity: Decimal
    full_packages: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        quantity = _quantity(self.quantity, "La cantidad", InvalidQuantityEntryError)
        if quantity < 0:
            message = "La cantidad no puede ser negativa."
            raise InvalidQuantityEntryError(message)
        full = _quantity(self.full_packages, "Los envases cerrados", InvalidQuantityEntryError)
        if full < 0:
            message = "Los envases cerrados no pueden ser negativos."
            raise InvalidQuantityEntryError(message)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "full_packages", full)

    def to_base(self, packaging: Packaging | None) -> Decimal:
        if self.mode is EntryMode.BASE:
            return self.quantity
        if packaging is None:
            message = "Este insumo no tiene presentación: ingresa la cantidad en su unidad."
            raise InvalidQuantityEntryError(message)
        if self.mode is EntryMode.PACKAGES:
            total = self.quantity * packaging.size
        else:
            total = self.full_packages * packaging.size + packaging.content_by_weight(self.quantity)
        # Medio envase de 0.001 da más de tres decimales: se redondea, no se rechaza.
        return _quantity(
            total.quantize(_QUANTITY_STEP, ROUND_HALF_UP), "La cantidad", InvalidQuantityEntryError
        )
