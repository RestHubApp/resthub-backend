"""Contrato HTTP de los conteos físicos y de las mermas por aprobar.

Cantidades en la unidad del insumo con tres decimales; montos en soles con dos.
Los insumos no se borran (solo se desactivan), así que todo conteo y toda merma
encuentra el suyo en el mapa que arma el router.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from resthub.core.api_params import EntityId
from resthub.modules.inventory.adapters.api.schemas import PackagingResponse
from resthub.modules.inventory.domain.entities import Ingredient, Unit
from resthub.modules.inventory.domain.packaging import EntryMode, QuantityEntry
from resthub.modules.inventory.domain.stock_counts import (
    MAX_COUNT_LINES,
    MAX_COUNT_NOTE_LENGTH,
    CountDifference,
    DifferenceReport,
    StockCount,
    StockCountLine,
    StockCountScope,
    StockCountStatus,
)
from resthub.modules.inventory.domain.waste_approvals import (
    MAX_REJECTION_LENGTH,
    WasteApproval,
    WasteApprovalStatus,
)
from resthub.modules.inventory.use_cases.stock_counts import HistoryEntry

# -- Conteos físicos --------------------------------------------------------------------


class OpenStockCountRequest(BaseModel):
    scope: StockCountScope = StockCountScope.ALL
    # Solo con `scope: "selected"`.
    ingredient_ids: list[EntityId] = Field(default_factory=list, max_length=MAX_COUNT_LINES)
    note: str = Field(default="", max_length=MAX_COUNT_NOTE_LENGTH)


class CountLineRequest(BaseModel):
    ingredient_id: EntityId
    # `null` deja el insumo sin contar. Según `mode`: la cantidad en la unidad
    # del insumo, el número de envases o el peso en gramos de un envase abierto.
    quantity: Decimal | None = Field(ge=0, max_digits=12, decimal_places=3)
    mode: EntryMode = EntryMode.BASE
    # Solo con `mode: "weight"`: los envases cerrados que hay además del pesado.
    full_packages: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=3)

    def to_entry(self) -> QuantityEntry | None:
        if self.quantity is None:
            return None
        return QuantityEntry(
            mode=self.mode, quantity=self.quantity, full_packages=self.full_packages
        )


class RecordCountLinesRequest(BaseModel):
    lines: list[CountLineRequest] = Field(min_length=1, max_length=MAX_COUNT_LINES)


class StockCountLineResponse(BaseModel):
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    packaging: PackagingResponse | None
    # Stock del sistema al abrir el conteo.
    opening_stock: Decimal
    # Stock del sistema al cargar lo contado; contra esto se mide la diferencia.
    expected: Decimal
    counted: Decimal | None
    entry_mode: EntryMode | None
    entry_quantity: Decimal | None
    entry_full_packages: Decimal | None
    counted_by: int | None
    counted_at: datetime | None
    # Lo que se ajustó al cerrar; `null` antes o si no se contó.
    difference: Decimal | None

    @classmethod
    def build(
        cls, line: StockCountLine, ingredients: Mapping[int, Ingredient]
    ) -> StockCountLineResponse:
        ingredient = ingredients[line.ingredient_id]
        entry = line.entry
        return cls(
            ingredient_id=line.ingredient_id,
            ingredient_name=ingredient.name,
            unit=ingredient.unit,
            packaging=PackagingResponse.from_entity(ingredient.packaging),
            opening_stock=line.opening_stock,
            expected=line.expected,
            counted=line.counted,
            entry_mode=entry.mode if entry else None,
            entry_quantity=entry.quantity if entry else None,
            entry_full_packages=(
                entry.full_packages if entry and entry.mode is EntryMode.WEIGHT else None
            ),
            counted_by=line.counted_by,
            counted_at=line.counted_at,
            difference=line.difference,
        )


class StockCountSummaryResponse(BaseModel):
    id: int
    number: int
    status: StockCountStatus
    status_label: str
    scope: StockCountScope
    note: str
    created_by: int
    created_at: datetime
    # Quién lo cerró o canceló, y cuándo.
    closed_by: int | None
    closed_at: datetime | None
    total_lines: int
    counted_lines: int

    @classmethod
    def from_entity(cls, count: StockCount) -> StockCountSummaryResponse:
        return cls(
            id=count.id or 0,
            number=count.number,
            status=count.status,
            status_label=count.status.label,
            scope=count.scope,
            note=count.note,
            created_by=count.created_by,
            created_at=count.created_at,
            closed_by=count.closed_by,
            closed_at=count.closed_at,
            total_lines=len(count.lines),
            counted_lines=count.counted_lines,
        )


class StockCountResponse(StockCountSummaryResponse):
    lines: list[StockCountLineResponse]

    @classmethod
    def build(cls, count: StockCount, ingredients: Mapping[int, Ingredient]) -> StockCountResponse:
        return cls(
            **StockCountSummaryResponse.from_entity(count).model_dump(),
            lines=[StockCountLineResponse.build(line, ingredients) for line in count.lines],
        )


class StockCountPageResponse(BaseModel):
    items: list[StockCountSummaryResponse]
    total: int


class DifferenceValuesResponse(BaseModel):
    """Lo común a una diferencia del reporte y a una del histórico."""

    expected: Decimal
    counted: Decimal
    # Contado - esperado: negativo es pérdida.
    difference: Decimal
    # Sobre lo esperado; `null` si no se esperaba nada.
    difference_percent: Decimal | None
    # Costo vigente al cerrar, por unidad del insumo.
    unit_cost: Decimal
    # En soles, a dos decimales.
    difference_value: Decimal

    @staticmethod
    def fields_of(difference: CountDifference) -> dict[str, Any]:
        return {
            "expected": difference.expected,
            "counted": difference.counted,
            "difference": difference.difference,
            "difference_percent": difference.percent,
            "unit_cost": difference.unit_cost,
            "difference_value": difference.value,
        }


class CountDifferenceResponse(DifferenceValuesResponse):
    ingredient_id: int
    ingredient_name: str
    unit: Unit


class CountDifferencesResponse(BaseModel):
    count: StockCountSummaryResponse
    # De la mayor pérdida a la mayor sobra; solo los insumos contados.
    lines: list[CountDifferenceResponse]
    # Suma de todas las diferencias valorizadas, y por separado pérdidas y sobras.
    total_value: Decimal
    loss_value: Decimal
    gain_value: Decimal

    @classmethod
    def build(
        cls, count: StockCount, report: DifferenceReport, ingredients: Mapping[int, Ingredient]
    ) -> CountDifferencesResponse:
        lines = []
        for difference in report.lines:
            ingredient = ingredients[difference.ingredient_id]
            lines.append(
                CountDifferenceResponse(
                    ingredient_id=difference.ingredient_id,
                    ingredient_name=ingredient.name,
                    unit=ingredient.unit,
                    **DifferenceValuesResponse.fields_of(difference),
                )
            )
        return cls(
            count=StockCountSummaryResponse.from_entity(count),
            lines=lines,
            total_value=report.total_value,
            loss_value=report.loss_value,
            gain_value=report.gain_value,
        )


class CountHistoryItemResponse(DifferenceValuesResponse):
    count_id: int
    count_number: int
    closed_at: datetime


class IngredientCountHistoryResponse(BaseModel):
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    # Del conteo más nuevo al más viejo.
    items: list[CountHistoryItemResponse]

    @classmethod
    def build(
        cls, ingredient: Ingredient, entries: list[HistoryEntry]
    ) -> IngredientCountHistoryResponse:
        return cls(
            ingredient_id=ingredient.id or 0,
            ingredient_name=ingredient.name,
            unit=ingredient.unit,
            items=[
                CountHistoryItemResponse(
                    count_id=entry.counted.count_id,
                    count_number=entry.counted.count_number,
                    closed_at=entry.counted.closed_at,
                    **DifferenceValuesResponse.fields_of(entry.difference),
                )
                for entry in entries
            ],
        )


# -- Mermas por aprobar -----------------------------------------------------------------


class WasteApprovalResponse(BaseModel):
    id: int
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    # Lo que se perdió, en positivo.
    quantity: Decimal
    # Costo vigente al registrarla y lo que vale (soles, dos decimales).
    unit_cost: Decimal
    value: Decimal
    reason: str
    status: WasteApprovalStatus
    status_label: str
    created_by: int
    # Nombre completo de quien la registró y de quien la decidió.
    created_by_name: str
    created_at: datetime
    decided_by: int | None
    decided_by_name: str | None
    decided_at: datetime | None
    rejection_reason: str
    # El movimiento de merma que dejó al aprobarse.
    movement_id: int | None

    @classmethod
    def build(
        cls,
        approval: WasteApproval,
        ingredients: Mapping[int, Ingredient],
        staff_names: Mapping[int, str],
    ) -> WasteApprovalResponse:
        ingredient = ingredients[approval.ingredient_id]
        return cls(
            id=approval.id or 0,
            ingredient_id=approval.ingredient_id,
            ingredient_name=ingredient.name,
            unit=ingredient.unit,
            quantity=approval.quantity,
            unit_cost=approval.unit_cost,
            value=approval.value,
            reason=approval.reason,
            status=approval.status,
            status_label=approval.status.label,
            created_by=approval.created_by,
            created_by_name=staff_names.get(approval.created_by, ""),
            created_at=approval.created_at,
            decided_by=approval.decided_by,
            decided_by_name=(
                None if approval.decided_by is None else staff_names.get(approval.decided_by, "")
            ),
            decided_at=approval.decided_at,
            rejection_reason=approval.rejection_reason,
            movement_id=approval.movement_id,
        )


class WasteApprovalPageResponse(BaseModel):
    items: list[WasteApprovalResponse]
    total: int


class RejectWasteRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REJECTION_LENGTH)
