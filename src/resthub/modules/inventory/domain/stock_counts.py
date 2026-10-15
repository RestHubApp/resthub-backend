"""Conteos físicos: comprobar que lo que dice el sistema es lo que hay.

Python puro. Un conteo se abre como borrador con la lista de insumos a contar
y una foto del stock de cada uno. Mientras está abierto se cargan y corrigen
las cantidades; al cerrarlo, cada diferencia entra al libro como un ajuste.

Regla de la diferencia: cada línea guarda, además de la foto de apertura, el
stock del sistema en el momento en que se cargó lo contado (`expected`). Al
cerrar, el ajuste es `contado - expected`. Así, lo que se sirve o se compra
mientras dura el conteo no se confunde con una pérdida: si a las 10 se
contaron 3000 ml y a las 11 se sirvieron 200 ml, el cierre de las 12 ajusta
la diferencia de las 10 y el stock queda en 2800 ml, que es lo que hay. Restar
el stock del cierre daría 3000 ml y borraría lo servido. Una línea sin contar
no se ajusta.

Solo hay un conteo abierto a la vez por local: dos conteos del mismo insumo
sumarían su diferencia dos veces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from resthub.core.clock import utc_now
from resthub.modules.inventory.domain.exceptions import (
    InvalidStockCountError,
    StockCountNotOpenError,
)
from resthub.modules.inventory.domain.packaging import QuantityEntry

MAX_COUNT_NOTE_LENGTH = 300
# Lo que cabe en una sola carga de líneas o en una lista elegida a mano.
MAX_COUNT_LINES = 500
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)


class StockCountStatus(StrEnum):
    DRAFT = "draft"
    CLOSED = "closed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[StockCountStatus, str] = {
    StockCountStatus.DRAFT: "En curso",
    StockCountStatus.CLOSED: "Cerrado",
    StockCountStatus.CANCELLED: "Cancelado",
}


class StockCountScope(StrEnum):
    # Todos los insumos activos al abrir.
    ALL = "all"
    # Una lista elegida (la cámara de frío, las bebidas).
    SELECTED = "selected"


@dataclass(slots=True)
class StockCountLine:
    ingredient_id: int
    # Stock del sistema al abrir el conteo: la foto.
    opening_stock: Decimal
    # Stock del sistema al cargar lo contado; hasta entonces, la foto.
    expected: Decimal
    # En la unidad del insumo; `None` mientras no se cuenta.
    counted: Decimal | None = None
    # Cómo lo ingresó quien contó (en envases, por peso…).
    entry: QuantityEntry | None = None
    counted_by: int | None = None
    counted_at: datetime | None = None
    # Al cerrar: el ajuste que entró al libro y el costo con que se valoriza.
    difference: Decimal | None = None
    unit_cost: Decimal | None = None
    id: int | None = None

    @property
    def is_counted(self) -> bool:
        return self.counted is not None


@dataclass(slots=True)
class StockCount:
    restaurant_id: int
    # Correlativo por local: «Conteo físico #4».
    number: int
    created_by: int
    lines: list[StockCountLine]
    scope: StockCountScope = StockCountScope.ALL
    note: str = ""
    status: StockCountStatus = StockCountStatus.DRAFT
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    # Quién lo cerró o lo canceló, y cuándo.
    closed_by: int | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.note = validate_note(self.note)
        self.lines = _validate_lines(self.lines)

    @property
    def reason(self) -> str:
        """El motivo de los ajustes que deja al cerrar."""
        return f"Conteo físico #{self.number}"

    @property
    def counted_lines(self) -> int:
        return sum(1 for line in self.lines if line.is_counted)

    def record(
        self,
        ingredient_id: int,
        counted: Decimal,
        entry: QuantityEntry,
        *,
        expected: Decimal,
        actor_id: int,
        now: datetime,
    ) -> StockCountLine:
        """Carga o corrige lo contado de un insumo, con el stock del sistema de ese momento."""
        self._ensure_open()
        if counted < 0:
            message = "Lo contado no puede ser negativo."
            raise InvalidStockCountError(message)
        line = self._line(ingredient_id)
        line.counted = counted
        line.entry = entry
        line.expected = expected
        line.counted_by = actor_id
        line.counted_at = now
        return line

    def clear(self, ingredient_id: int) -> StockCountLine:
        """Deja un insumo sin contar: al cerrar no se ajusta."""
        self._ensure_open()
        line = self._line(ingredient_id)
        line.counted = None
        line.entry = None
        line.expected = line.opening_stock
        line.counted_by = None
        line.counted_at = None
        return line

    def close(
        self, unit_costs: Mapping[int, Decimal], actor_id: int, now: datetime
    ) -> list[StockCountLine]:
        """Cierra el conteo; devuelve las líneas con diferencia, las que se ajustan."""
        self._ensure_open()
        counted = [line for line in self.lines if line.counted is not None]
        if not counted:
            message = "No hay ningún insumo contado: carga al menos uno o cancela el conteo."
            raise InvalidStockCountError(message)
        for line in counted:
            line.difference = (line.counted or Decimal(0)) - line.expected
            line.unit_cost = unit_costs.get(line.ingredient_id, Decimal(0))
        self.status = StockCountStatus.CLOSED
        self.closed_by = actor_id
        self.closed_at = now
        return [line for line in counted if line.difference]

    def cancel(self, actor_id: int, now: datetime) -> None:
        """Lo descarta sin tocar el stock."""
        self._ensure_open()
        self.status = StockCountStatus.CANCELLED
        self.closed_by = actor_id
        self.closed_at = now

    def _line(self, ingredient_id: int) -> StockCountLine:
        for line in self.lines:
            if line.ingredient_id == ingredient_id:
                return line
        message = f"El insumo {ingredient_id} no está en el conteo #{self.number}."
        raise InvalidStockCountError(message)

    def _ensure_open(self) -> None:
        if self.status is not StockCountStatus.DRAFT:
            raise StockCountNotOpenError(self.number, self.status.label)


def validate_note(raw: str) -> str:
    note = " ".join(raw.split())
    if len(note) > MAX_COUNT_NOTE_LENGTH:
        message = f"La nota admite {MAX_COUNT_NOTE_LENGTH} caracteres."
        raise InvalidStockCountError(message)
    return note


def _validate_lines(lines: Iterable[StockCountLine]) -> list[StockCountLine]:
    listed = list(lines)
    if not listed:
        message = "El conteo necesita al menos un insumo."
        raise InvalidStockCountError(message)
    ids = [line.ingredient_id for line in listed]
    if len(set(ids)) != len(ids):
        message = "Un insumo aparece dos veces en el conteo."
        raise InvalidStockCountError(message)
    return listed


# -- Reporte de diferencias -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CountDifference:
    """Lo que se esperaba, lo que había y cuánto vale la diferencia."""

    ingredient_id: int
    expected: Decimal
    counted: Decimal
    # Contado - esperado, en la unidad del insumo: negativo es pérdida.
    difference: Decimal
    unit_cost: Decimal

    @property
    def percent(self) -> Decimal | None:
        """La diferencia sobre lo esperado; `None` si no se esperaba nada."""
        if self.expected <= 0:
            return None
        return (self.difference / self.expected * _HUNDRED).quantize(_CENT, ROUND_HALF_UP)

    @property
    def value(self) -> Decimal:
        """En soles, al costo vigente al cerrar."""
        return (self.difference * self.unit_cost).quantize(_CENT, ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class DifferenceReport:
    # De la mayor pérdida a la mayor sobra.
    lines: list[CountDifference]

    @property
    def total_value(self) -> Decimal:
        return sum((line.value for line in self.lines), Decimal("0.00"))

    @property
    def loss_value(self) -> Decimal:
        return sum((line.value for line in self.lines if line.value < 0), Decimal("0.00"))

    @property
    def gain_value(self) -> Decimal:
        return sum((line.value for line in self.lines if line.value > 0), Decimal("0.00"))


def line_difference(line: StockCountLine) -> CountDifference | None:
    """La diferencia de una línea cerrada; `None` si no se contó."""
    if line.counted is None or line.difference is None:
        return None
    return CountDifference(
        ingredient_id=line.ingredient_id,
        expected=line.expected,
        counted=line.counted,
        difference=line.difference,
        unit_cost=line.unit_cost or Decimal(0),
    )


def difference_report(count: StockCount) -> DifferenceReport:
    differences = [
        difference for line in count.lines if (difference := line_difference(line)) is not None
    ]
    return DifferenceReport(
        lines=sorted(differences, key=lambda line: (line.value, line.ingredient_id))
    )
