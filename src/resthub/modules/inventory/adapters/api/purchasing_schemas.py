"""Contrato HTTP de proveedores y órdenes de compra."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from resthub.modules.inventory.domain.entities import Ingredient, Unit
from resthub.modules.inventory.domain.purchasing import (
    MAX_CONTACT_LENGTH,
    MAX_LINES,
    MAX_PHONE_LENGTH,
    MAX_PURCHASE_NOTES_LENGTH,
    MAX_SUPPLIER_NAME_LENGTH,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseSuggestion,
    Supplier,
)


class SupplierRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_SUPPLIER_NAME_LENGTH)
    contact: str = Field(default="", max_length=MAX_CONTACT_LENGTH)
    phone: str = Field(default="", max_length=MAX_PHONE_LENGTH)
    notes: str = Field(default="", max_length=MAX_PURCHASE_NOTES_LENGTH)
    is_active: bool = True


class SupplierResponse(BaseModel):
    id: int
    name: str
    contact: str
    phone: str
    notes: str
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, supplier: Supplier) -> SupplierResponse:
        return cls(
            id=supplier.id or 0,
            name=supplier.name,
            contact=supplier.contact,
            phone=supplier.phone,
            notes=supplier.notes,
            is_active=supplier.is_active,
            created_at=supplier.created_at,
        )


class PurchaseLineRequest(BaseModel):
    ingredient_id: int = Field(ge=1)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(ge=0, max_digits=14, decimal_places=6)


class CreatePurchaseOrderRequest(BaseModel):
    supplier_id: int = Field(ge=1)
    lines: list[PurchaseLineRequest] = Field(min_length=1, max_length=MAX_LINES)
    notes: str = Field(default="", max_length=MAX_PURCHASE_NOTES_LENGTH)


class EditPurchaseOrderRequest(BaseModel):
    lines: list[PurchaseLineRequest] = Field(min_length=1, max_length=MAX_LINES)
    notes: str | None = Field(default=None, max_length=MAX_PURCHASE_NOTES_LENGTH)


class ReceiptRequest(BaseModel):
    line_id: int = Field(ge=1)
    quantity: Decimal = Field(ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(ge=0, max_digits=14, decimal_places=6)


class ReceivePurchaseOrderRequest(BaseModel):
    # Una línea que no aparece no llegó.
    lines: list[ReceiptRequest] = Field(default_factory=list, max_length=MAX_LINES)


class PurchaseLineResponse(BaseModel):
    id: int
    ingredient_id: int
    ingredient_name: str
    unit: Unit | None
    quantity: Decimal
    unit_cost: Decimal
    estimated_total: Decimal
    received_quantity: Decimal | None
    received_unit_cost: Decimal | None

    @classmethod
    def from_entity(
        cls, line: PurchaseOrderLine, ingredients: Mapping[int, Ingredient]
    ) -> PurchaseLineResponse:
        ingredient = ingredients.get(line.ingredient_id)
        return cls(
            id=line.id or 0,
            ingredient_id=line.ingredient_id,
            ingredient_name=ingredient.name if ingredient else "",
            unit=ingredient.unit if ingredient else None,
            quantity=line.quantity,
            unit_cost=line.unit_cost,
            estimated_total=line.estimated_total,
            received_quantity=line.received_quantity,
            received_unit_cost=line.received_unit_cost,
        )


class PurchaseOrderResponse(BaseModel):
    id: int
    number: int
    supplier_id: int
    supplier_name: str
    status: PurchaseOrderStatus
    status_label: str
    notes: str
    lines: list[PurchaseLineResponse]
    estimated_total: Decimal
    received_total: Decimal
    created_at: datetime
    sent_at: datetime | None
    received_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def build(
        cls,
        order: PurchaseOrder,
        suppliers: Mapping[int, Supplier],
        ingredients: Mapping[int, Ingredient],
    ) -> PurchaseOrderResponse:
        supplier = suppliers.get(order.supplier_id)
        return cls(
            id=order.id or 0,
            number=order.number,
            supplier_id=order.supplier_id,
            supplier_name=supplier.name if supplier else "",
            status=order.status,
            status_label=order.status.label,
            notes=order.notes,
            lines=[PurchaseLineResponse.from_entity(line, ingredients) for line in order.lines],
            estimated_total=order.estimated_total,
            received_total=order.received_total,
            created_at=order.created_at,
            sent_at=order.sent_at,
            received_at=order.received_at,
            cancelled_at=order.cancelled_at,
        )


class PurchaseOrderPageResponse(BaseModel):
    items: list[PurchaseOrderResponse]
    total: int


class PurchaseSuggestionResponse(BaseModel):
    ingredient_id: int
    ingredient_name: str
    unit: Unit
    stock: Decimal
    min_stock: Decimal
    average_daily_use: Decimal
    # Lo que conviene pedir: una semana de consumo o el doble del mínimo.
    quantity: Decimal
    unit_cost: Decimal

    @classmethod
    def build(
        cls, suggestion: PurchaseSuggestion, ingredient: Ingredient
    ) -> PurchaseSuggestionResponse:
        return cls(
            ingredient_id=suggestion.ingredient_id,
            ingredient_name=ingredient.name,
            unit=ingredient.unit,
            stock=suggestion.stock,
            min_stock=suggestion.min_stock,
            average_daily_use=suggestion.average_daily_use,
            quantity=suggestion.quantity,
            unit_cost=suggestion.unit_cost,
        )
