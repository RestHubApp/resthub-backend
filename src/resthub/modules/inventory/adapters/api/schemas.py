"""Contrato HTTP del inventario.

Cantidades y montos viajan como `Decimal`, que en JSON se escribe como texto.
Las cantidades van en la unidad del insumo, con tres decimales; el costo
unitario, por esa misma unidad, con seis.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from resthub.core.api_params import EntityId
from resthub.modules.inventory.domain.entities import (
    MAX_NAME_LENGTH,
    MAX_REASON_LENGTH,
    MovementKind,
    StockLevel,
    StockMovement,
    Unit,
)
from resthub.modules.inventory.domain.packaging import (
    MAX_PACKAGE_LABEL_LENGTH,
    EntryMode,
    Packaging,
)
from resthub.modules.inventory.use_cases.recipes import DishCost, RecipeView
from resthub.modules.inventory.use_cases.register_movements import MovementEntry, StockChange

_LINE_COST_STEP = Decimal("0.0001")


class PackagingResponse(BaseModel):
    # Cuánto trae un envase, en la unidad del insumo.
    size: Decimal
    label: str
    full_weight_g: Decimal | None
    empty_weight_g: Decimal | None
    # Tiene los dos pesos: se puede contar pesando un envase abierto.
    weighable: bool

    @classmethod
    def from_entity(cls, packaging: Packaging | None) -> PackagingResponse | None:
        if packaging is None:
            return None
        return cls(
            size=packaging.size,
            label=packaging.label,
            full_weight_g=packaging.full_weight_g,
            empty_weight_g=packaging.empty_weight_g,
            weighable=packaging.weighable,
        )


class IngredientResponse(BaseModel):
    id: int
    name: str
    unit: Unit
    unit_label: str
    min_stock: Decimal
    unit_cost: Decimal
    is_active: bool
    # `null` si se maneja solo en su unidad.
    packaging: PackagingResponse | None
    # Suma de los movimientos del libro.
    stock: Decimal
    is_low: bool
    is_negative: bool
    created_at: datetime

    @classmethod
    def from_level(cls, level: StockLevel) -> IngredientResponse:
        ingredient = level.ingredient
        return cls(
            id=ingredient.id or 0,
            name=ingredient.name,
            unit=ingredient.unit,
            unit_label=ingredient.unit.label,
            min_stock=ingredient.min_stock,
            unit_cost=ingredient.unit_cost,
            is_active=ingredient.is_active,
            packaging=PackagingResponse.from_entity(ingredient.packaging),
            stock=level.stock,
            is_low=level.is_low,
            is_negative=level.is_negative,
            created_at=ingredient.created_at,
        )


class MovementResponse(BaseModel):
    id: int
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    kind: MovementKind
    kind_label: str
    # Con signo: positivo entra, negativo sale.
    quantity: Decimal
    unit_cost: Decimal | None
    reason: str
    order_id: int | None
    # El número del día del pedido, el que se ve en el salón y en la comanda.
    order_number: int | None
    order_item_id: int | None
    # Cómo se ingresó la cantidad y el número que se escribió (en envases,
    # por ejemplo); `null` en los movimientos que registra el sistema.
    entry_mode: EntryMode | None
    entry_quantity: Decimal | None
    # El conteo físico que originó un ajuste.
    stock_count_id: int | None
    created_by: int
    created_at: datetime

    @classmethod
    def build(
        cls, movement: StockMovement, name: str, unit: Unit, order_number: int | None = None
    ) -> MovementResponse:
        return cls(
            id=movement.id or 0,
            ingredient_id=movement.ingredient_id,
            ingredient_name=name,
            unit=unit,
            kind=movement.kind,
            kind_label=movement.kind.label,
            quantity=movement.quantity,
            unit_cost=movement.unit_cost,
            reason=movement.reason,
            order_id=movement.order_id,
            order_number=order_number,
            order_item_id=movement.order_item_id,
            entry_mode=movement.entry_mode,
            entry_quantity=movement.entry_quantity,
            stock_count_id=movement.stock_count_id,
            created_by=movement.created_by,
            created_at=movement.created_at,
        )

    @classmethod
    def from_entry(cls, entry: MovementEntry) -> MovementResponse:
        return cls.build(
            entry.movement, entry.ingredient.name, entry.ingredient.unit, entry.order_number
        )


class MovementPageResponse(BaseModel):
    items: list[MovementResponse]
    total: int


class StockChangeResponse(BaseModel):
    """El movimiento registrado y el insumo como quedó."""

    movement: MovementResponse
    ingredient: IngredientResponse

    @classmethod
    def from_change(cls, change: StockChange) -> StockChangeResponse:
        ingredient = change.level.ingredient
        return cls(
            movement=MovementResponse.build(change.movement, ingredient.name, ingredient.unit),
            ingredient=IngredientResponse.from_level(change.level),
        )


class DishCostResponse(BaseModel):
    menu_item_id: int
    menu_item_name: str
    price: Decimal
    is_active: bool
    has_recipe: bool
    # En soles, a dos decimales; `null` si el plato no tiene receta.
    cost: Decimal | None
    margin: Decimal | None
    margin_percent: Decimal | None

    @classmethod
    def from_cost(cls, dish_cost: DishCost) -> DishCostResponse:
        return cls(
            menu_item_id=dish_cost.dish.id,
            menu_item_name=dish_cost.dish.name,
            price=dish_cost.dish.price,
            is_active=dish_cost.dish.is_active,
            has_recipe=dish_cost.has_recipe,
            cost=dish_cost.rounded_cost,
            margin=dish_cost.margin,
            margin_percent=dish_cost.margin_percent,
        )


class RecipeLineResponse(BaseModel):
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    quantity: Decimal
    unit_cost: Decimal
    # A cuatro decimales: cinco gramos de sal cuestan menos de un céntimo.
    cost: Decimal


class RecipeResponse(DishCostResponse):
    lines: list[RecipeLineResponse]

    @classmethod
    def from_view(cls, view: RecipeView) -> RecipeResponse:
        summary = DishCostResponse.from_cost(view.cost)
        return cls(
            **summary.model_dump(),
            lines=[
                RecipeLineResponse(
                    ingredient_id=line.ingredient.id or 0,
                    ingredient_name=line.ingredient.name,
                    unit=line.ingredient.unit,
                    quantity=line.line.quantity,
                    unit_cost=line.ingredient.unit_cost,
                    cost=line.cost.quantize(_LINE_COST_STEP, ROUND_HALF_UP),
                )
                for line in view.lines
            ],
        )


class PackagingRequest(BaseModel):
    # Cuánto trae un envase, en la unidad del insumo (750 para una botella de 750 ml).
    size: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    label: str = Field(default="", max_length=MAX_PACKAGE_LABEL_LENGTH)
    # Para contar por peso: los dos, en gramos, con lleno > vacío > 0.
    full_weight_g: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)
    empty_weight_g: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=3)

    def to_entity(self) -> Packaging:
        return Packaging(
            size=self.size,
            label=self.label,
            full_weight_g=self.full_weight_g,
            empty_weight_g=self.empty_weight_g,
        )


class CreateIngredientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    unit: Unit
    min_stock: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=6)
    packaging: PackagingRequest | None = None


class UpdateIngredientRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    min_stock: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    is_active: bool | None = None
    # Omitida la deja como está; `null` la quita.
    packaging: PackagingRequest | None = None


class PurchaseRequest(BaseModel):
    ingredient_id: EntityId
    # En la unidad del insumo, o en envases con `entry_mode: "packages"`.
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    entry_mode: EntryMode = EntryMode.BASE
    # Por unidad del insumo: 5 kg de arroz a S/ 21 son 5000 g a 0.0042.
    unit_cost: Decimal = Field(ge=0, max_digits=14, decimal_places=6)
    reason: str = Field(default="", max_length=MAX_REASON_LENGTH)


class WasteRequest(BaseModel):
    ingredient_id: EntityId
    # Lo que se perdió, en positivo.
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class AdjustmentRequest(BaseModel):
    ingredient_id: EntityId
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    # Uno de los dos: la diferencia con signo, o el stock contado en el almacén.
    quantity: Decimal | None = Field(default=None, max_digits=12, decimal_places=3)
    counted_stock: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)


class RecipeLineRequest(BaseModel):
    ingredient_id: EntityId
    # Por porción, en la unidad del insumo.
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)


class ReplaceRecipeRequest(BaseModel):
    # Vacía borra la receta.
    lines: list[RecipeLineRequest]
