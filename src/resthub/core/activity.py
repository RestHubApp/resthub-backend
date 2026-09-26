"""Bitácora de lo que hace cada cuenta.

Vive en el núcleo y no en un módulo de dominio porque la escriben todos: los
pedidos, el menú, el inventario y las propias cuentas. Si la poseyera uno de
ellos, el resto tendría que importarlo y dejarían de ser independientes.

Cada asiento lleva el restaurante, y toda consulta lo exige: la bitácora de un
local nunca muestra lo que pasó en otro.

Este archivo es Python puro a propósito: lo importan los casos de uso, que son
los que deciden qué se registra. El adaptador que escribe en la base vive en
`activity_log.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page

MAX_DETAIL_LENGTH = 200


class ActivityKind(StrEnum):
    """Qué ocurrió.

    El tipo es un código estable y la frase legible se arma al mostrarla. Si se
    guardara la frase, cada variante del texto sería un valor distinto y no se
    podría filtrar ni contar; así, cambiar la redacción no rompe el historial.
    """

    SIGNED_IN = "signed_in"
    PASSWORD_CHANGED = "password_changed"
    STAFF_REGISTERED = "staff_registered"
    STAFF_UPDATED = "staff_updated"
    STAFF_STATUS_CHANGED = "staff_status_changed"
    STAFF_PASSWORD_RESET = "staff_password_reset"
    RESTAURANT_UPDATED = "restaurant_updated"
    MENU_CATEGORY_CREATED = "menu_category_created"
    MENU_CATEGORY_UPDATED = "menu_category_updated"
    MENU_CATEGORY_DELETED = "menu_category_deleted"
    MENU_ITEM_CREATED = "menu_item_created"
    MENU_ITEM_UPDATED = "menu_item_updated"
    MENU_ITEM_AVAILABILITY_CHANGED = "menu_item_availability"
    TABLE_CREATED = "table_created"
    TABLE_UPDATED = "table_updated"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_CHARGED = "order_charged"
    PAYMENT_RECEIVED = "payment_received"
    ORDER_DISCOUNTED = "order_discounted"
    ORDER_COURTESY = "order_courtesy"
    ORDER_MOVED = "order_moved"
    ORDER_MERGED = "order_merged"
    CASH_OPENED = "cash_opened"
    CASH_CLOSED = "cash_closed"
    INGREDIENT_CREATED = "ingredient_created"
    INGREDIENT_UPDATED = "ingredient_updated"
    STOCK_PURCHASE = "stock_purchase"
    STOCK_WASTE = "stock_waste"
    STOCK_ADJUSTMENT = "stock_adjustment"
    RECIPE_UPDATED = "recipe_updated"
    RESTOCK_REFRESHED = "restock_refreshed"
    ORDER_NOTES_CLASSIFIED = "order_notes_classified"
    WASTE_CLASSIFIED = "waste_classified"
    SUPPLIER_CREATED = "supplier_created"
    SUPPLIER_UPDATED = "supplier_updated"
    PURCHASE_ORDER_CREATED = "purchase_order_created"
    PURCHASE_ORDER_SENT = "purchase_order_sent"
    PURCHASE_ORDER_RECEIVED = "purchase_order_received"
    PURCHASE_ORDER_CANCELLED = "purchase_order_cancelled"
    INVOICE_ISSUED = "invoice_issued"
    BILLING_SETTINGS_UPDATED = "billing_settings_updated"
    CUSTOMER_CREATED = "customer_created"
    CUSTOMER_UPDATED = "customer_updated"
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_UPDATED = "reservation_updated"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[ActivityKind, str] = {
    ActivityKind.SIGNED_IN: "Inició sesión",
    ActivityKind.PASSWORD_CHANGED: "Cambió su contraseña",
    ActivityKind.STAFF_REGISTERED: "Dio de alta a un miembro del personal",
    ActivityKind.STAFF_UPDATED: "Editó a un miembro del personal",
    ActivityKind.STAFF_STATUS_CHANGED: "Activó o desactivó una cuenta",
    ActivityKind.STAFF_PASSWORD_RESET: "Restableció la contraseña de una cuenta",
    ActivityKind.RESTAURANT_UPDATED: "Editó los datos del restaurante",
    ActivityKind.MENU_CATEGORY_CREATED: "Creó una categoría del menú",
    ActivityKind.MENU_CATEGORY_UPDATED: "Editó una categoría del menú",
    ActivityKind.MENU_CATEGORY_DELETED: "Eliminó una categoría del menú",
    ActivityKind.MENU_ITEM_CREATED: "Agregó un plato al menú",
    ActivityKind.MENU_ITEM_UPDATED: "Editó un plato del menú",
    ActivityKind.MENU_ITEM_AVAILABILITY_CHANGED: "Cambió la disponibilidad de un plato",
    ActivityKind.TABLE_CREATED: "Agregó una mesa",
    ActivityKind.TABLE_UPDATED: "Editó una mesa",
    ActivityKind.ORDER_CANCELLED: "Canceló un pedido",
    ActivityKind.ORDER_CHARGED: "Cobró un pedido",
    ActivityKind.PAYMENT_RECEIVED: "Cobró una parte de un pedido",
    ActivityKind.ORDER_DISCOUNTED: "Aplicó un descuento",
    ActivityKind.ORDER_COURTESY: "Invitó o dejó de invitar un plato",
    ActivityKind.ORDER_MOVED: "Cambió un pedido de mesa",
    ActivityKind.ORDER_MERGED: "Unió dos mesas",
    ActivityKind.CASH_OPENED: "Abrió la caja",
    ActivityKind.CASH_CLOSED: "Cerró la caja",
    ActivityKind.INGREDIENT_CREATED: "Dio de alta un insumo",
    ActivityKind.INGREDIENT_UPDATED: "Editó un insumo",
    ActivityKind.STOCK_PURCHASE: "Registró una compra",
    ActivityKind.STOCK_WASTE: "Registró una merma",
    ActivityKind.STOCK_ADJUSTMENT: "Ajustó el stock",
    ActivityKind.RECIPE_UPDATED: "Editó una receta",
    ActivityKind.RESTOCK_REFRESHED: "Actualizó las sugerencias de compra",
    ActivityKind.ORDER_NOTES_CLASSIFIED: "Revisó las notas de los pedidos en curso",
    ActivityKind.WASTE_CLASSIFIED: "Clasificó las causas de merma",
    ActivityKind.SUPPLIER_CREATED: "Dio de alta un proveedor",
    ActivityKind.SUPPLIER_UPDATED: "Editó un proveedor",
    ActivityKind.PURCHASE_ORDER_CREATED: "Creó una orden de compra",
    ActivityKind.PURCHASE_ORDER_SENT: "Envió una orden de compra",
    ActivityKind.PURCHASE_ORDER_RECEIVED: "Recibió una orden de compra",
    ActivityKind.PURCHASE_ORDER_CANCELLED: "Canceló una orden de compra",
    ActivityKind.INVOICE_ISSUED: "Emitió un comprobante",
    ActivityKind.BILLING_SETTINGS_UPDATED: "Editó los datos fiscales",
    ActivityKind.CUSTOMER_CREATED: "Dio de alta un cliente",
    ActivityKind.CUSTOMER_UPDATED: "Editó un cliente",
    ActivityKind.RESERVATION_CREATED: "Tomó una reserva",
    ActivityKind.RESERVATION_UPDATED: "Cambió una reserva",
}


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    restaurant_id: int
    user_id: int
    kind: ActivityKind
    detail: str = ""
    id: int | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", self.detail.strip()[:MAX_DETAIL_LENGTH])


@dataclass(frozen=True, slots=True)
class ActivityQuery:
    # Obligatorio y sin valor por omisión: una consulta que se olvide de
    # acotarlo no compila, en vez de devolver la bitácora de todos los locales.
    restaurant_id: int
    user_ids: frozenset[int] | None = None
    kinds: frozenset[ActivityKind] | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


class ActivityRecorder(Protocol):
    """Escribe en la bitácora.

    Lo usan los casos de uso, que son los que saben qué ocurrió. Registrar es
    parte de la transacción: si la operación se deshace, el asiento también.
    """

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None: ...


class ActivityReader(Protocol):
    async def search(self, query: ActivityQuery) -> Page[ActivityRecord]: ...
