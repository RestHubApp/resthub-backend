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
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from resthub.core.clock import utc_now
from resthub.core.pagination import DEFAULT_PAGE_SIZE, Page

MAX_DETAIL_LENGTH = 200


class ActivityKind(StrEnum):
    """Qué ocurrió.

    El tipo es un código estable y la frase legible se arma al mostrarla. Si se
    guardara la frase, cada variante del texto sería un valor distinto y no se
    podría filtrar ni contar; así, cambiar la redacción no rompe el historial.
    """

    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105  # nosec B105 # nombre del evento
    SESSION_CLOSED = "session_closed"
    SESSION_LOCKED_OUT = "session_locked_out"
    PIN_CHANGED = "pin_changed"
    TWO_FACTOR_ENABLED = "two_factor_enabled"
    TWO_FACTOR_DISABLED = "two_factor_disabled"
    RECOVERY_CODE_USED = "recovery_code_used"
    TERMS_ACCEPTED = "terms_accepted"
    PRIVACY_ACCEPTED = "privacy_accepted"
    STAFF_REGISTERED = "staff_registered"
    STAFF_UPDATED = "staff_updated"
    STAFF_STATUS_CHANGED = "staff_status_changed"
    STAFF_PASSWORD_RESET = "staff_password_reset"  # noqa: S105  # nosec B105 # nombre del evento
    STAFF_SESSION_CLOSED = "staff_session_closed"
    ROLE_CREATED = "role_created"
    ROLE_UPDATED = "role_updated"
    ROLE_DELETED = "role_deleted"
    RESTAURANT_UPDATED = "restaurant_updated"
    MENU_CATEGORY_CREATED = "menu_category_created"
    MENU_CATEGORY_UPDATED = "menu_category_updated"
    MENU_CATEGORY_DELETED = "menu_category_deleted"
    MENU_ITEM_CREATED = "menu_item_created"
    MENU_ITEM_UPDATED = "menu_item_updated"
    MENU_ITEM_AVAILABILITY_CHANGED = "menu_item_availability"
    TABLE_CREATED = "table_created"
    TABLE_UPDATED = "table_updated"
    ORDER_CREATED = "order_created"
    ORDER_ITEMS_ADDED = "order_items_added"
    ORDER_SERVED = "order_served"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_CHARGED = "order_charged"
    PAYMENT_RECEIVED = "payment_received"
    ORDER_DISCOUNTED = "order_discounted"
    ORDER_COURTESY = "order_courtesy"
    ORDER_MOVED = "order_moved"
    ORDER_MERGED = "order_merged"
    ORDER_ITEM_VOIDED = "order_item_voided"
    CASH_OPENED = "cash_opened"
    CASH_CLOSED = "cash_closed"
    INGREDIENT_CREATED = "ingredient_created"
    INGREDIENT_UPDATED = "ingredient_updated"
    STOCK_PURCHASE = "stock_purchase"
    STOCK_WASTE = "stock_waste"
    STOCK_ADJUSTMENT = "stock_adjustment"
    WASTE_APPROVAL_REQUESTED = "waste_approval_requested"
    WASTE_APPROVED = "waste_approved"
    WASTE_REJECTED = "waste_rejected"
    STOCK_COUNT_OPENED = "stock_count_opened"
    STOCK_COUNT_CLOSED = "stock_count_closed"
    STOCK_COUNT_CANCELLED = "stock_count_cancelled"
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
    INVOICE_EMAILED = "invoice_emailed"
    INVOICE_VOIDED = "invoice_voided"
    CREDIT_NOTE_ISSUED = "credit_note_issued"
    BILLING_SETTINGS_UPDATED = "billing_settings_updated"
    CUSTOMER_CREATED = "customer_created"
    CUSTOMER_UPDATED = "customer_updated"
    CUSTOMER_EXPORTED = "customer_exported"
    CUSTOMER_ANONYMIZED = "customer_anonymized"
    CUSTOMER_VIEWED = "customer_viewed"
    CUSTOMER_REVEALED = "customer_revealed"
    DATA_ACCESS_ALERT = "data_access_alert"
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_UPDATED = "reservation_updated"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS: dict[ActivityKind, str] = {
    ActivityKind.SIGNED_IN: "Inició sesión",
    ActivityKind.SIGNED_OUT: "Cerró sesión",
    ActivityKind.PASSWORD_CHANGED: "Cambió su contraseña",
    ActivityKind.SESSION_CLOSED: "Cerró su sesión en otro dispositivo",
    ActivityKind.SESSION_LOCKED_OUT: "Se cerró su sesión tras tres PIN equivocados",
    ActivityKind.PIN_CHANGED: "Cambió su PIN de desbloqueo",
    ActivityKind.TWO_FACTOR_ENABLED: "Activó la verificación en dos pasos",
    ActivityKind.TWO_FACTOR_DISABLED: "Desactivó la verificación en dos pasos",
    ActivityKind.RECOVERY_CODE_USED: "Entró con un código de recuperación",
    ActivityKind.TERMS_ACCEPTED: "Aceptó los términos y condiciones",
    ActivityKind.PRIVACY_ACCEPTED: "Aceptó el tratamiento de sus datos personales",
    ActivityKind.STAFF_REGISTERED: "Dio de alta a un miembro del personal",
    ActivityKind.STAFF_UPDATED: "Editó a un miembro del personal",
    ActivityKind.STAFF_STATUS_CHANGED: "Activó o desactivó una cuenta",
    ActivityKind.STAFF_PASSWORD_RESET: "Restableció la contraseña de una cuenta",
    ActivityKind.STAFF_SESSION_CLOSED: "Cerró una sesión de un miembro del personal",
    ActivityKind.ROLE_CREATED: "Creó un rol",
    ActivityKind.ROLE_UPDATED: "Editó un rol",
    ActivityKind.ROLE_DELETED: "Eliminó un rol",
    ActivityKind.RESTAURANT_UPDATED: "Editó los datos del restaurante",
    ActivityKind.MENU_CATEGORY_CREATED: "Creó una categoría del menú",
    ActivityKind.MENU_CATEGORY_UPDATED: "Editó una categoría del menú",
    ActivityKind.MENU_CATEGORY_DELETED: "Eliminó una categoría del menú",
    ActivityKind.MENU_ITEM_CREATED: "Agregó un plato al menú",
    ActivityKind.MENU_ITEM_UPDATED: "Editó un plato del menú",
    ActivityKind.MENU_ITEM_AVAILABILITY_CHANGED: "Cambió la disponibilidad de un plato",
    ActivityKind.TABLE_CREATED: "Agregó una mesa",
    ActivityKind.TABLE_UPDATED: "Editó una mesa",
    ActivityKind.ORDER_CREATED: "Tomó un pedido",
    ActivityKind.ORDER_ITEMS_ADDED: "Agregó platos a un pedido",
    ActivityKind.ORDER_SERVED: "Sirvió un pedido",
    ActivityKind.ORDER_CANCELLED: "Canceló un pedido",
    ActivityKind.ORDER_CHARGED: "Cobró un pedido",
    ActivityKind.PAYMENT_RECEIVED: "Cobró una parte de un pedido",
    ActivityKind.ORDER_DISCOUNTED: "Aplicó un descuento",
    ActivityKind.ORDER_COURTESY: "Invitó o dejó de invitar un plato",
    ActivityKind.ORDER_MOVED: "Cambió un pedido de mesa",
    ActivityKind.ORDER_MERGED: "Unió dos mesas",
    ActivityKind.ORDER_ITEM_VOIDED: "Anuló un plato de un pedido",
    ActivityKind.CASH_OPENED: "Abrió la caja",
    ActivityKind.CASH_CLOSED: "Cerró la caja",
    ActivityKind.INGREDIENT_CREATED: "Dio de alta un insumo",
    ActivityKind.INGREDIENT_UPDATED: "Editó un insumo",
    ActivityKind.STOCK_PURCHASE: "Registró una compra",
    ActivityKind.STOCK_WASTE: "Registró una merma",
    ActivityKind.STOCK_ADJUSTMENT: "Ajustó el stock",
    ActivityKind.WASTE_APPROVAL_REQUESTED: "Registró una merma que pide aprobación",
    ActivityKind.WASTE_APPROVED: "Aprobó una merma",
    ActivityKind.WASTE_REJECTED: "Rechazó una merma",
    ActivityKind.STOCK_COUNT_OPENED: "Abrió un conteo físico",
    ActivityKind.STOCK_COUNT_CLOSED: "Cerró un conteo físico",
    ActivityKind.STOCK_COUNT_CANCELLED: "Canceló un conteo físico",
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
    ActivityKind.INVOICE_EMAILED: "Envió un comprobante por correo",
    ActivityKind.INVOICE_VOIDED: "Anuló un comprobante",
    ActivityKind.CREDIT_NOTE_ISSUED: "Emitió una nota de crédito",
    ActivityKind.BILLING_SETTINGS_UPDATED: "Editó los datos fiscales",
    ActivityKind.CUSTOMER_CREATED: "Dio de alta un cliente",
    ActivityKind.CUSTOMER_UPDATED: "Editó un cliente",
    ActivityKind.CUSTOMER_EXPORTED: "Exportó los datos de un cliente",
    ActivityKind.CUSTOMER_ANONYMIZED: "Borró los datos de un cliente",
    ActivityKind.CUSTOMER_VIEWED: "Abrió la ficha de un cliente",
    ActivityKind.CUSTOMER_REVEALED: "Vio completos los datos de un cliente",
    ActivityKind.DATA_ACCESS_ALERT: "Alerta: consultó muchas fichas de clientes en poco tiempo",
    ActivityKind.RESERVATION_CREATED: "Tomó una reserva",
    ActivityKind.RESERVATION_UPDATED: "Cambió una reserva",
}


class ActivityGroup(StrEnum):
    """Familias de acciones para filtrar la bitácora sin enumerar cada tipo.

    Un grupo es solo una lista de `ActivityKind`: no se guarda, así que agregar
    un tipo nuevo a un grupo no toca el historial. Un tipo puede estar en más
    de un grupo y algunos no están en ninguno.
    """

    SESSIONS = "sessions"
    ORDERS = "orders"
    CASH = "cash"
    CUSTOMERS_ACCESS = "customers_access"
    INVENTORY = "inventory"
    MENU = "menu"
    STAFF = "staff"

    @property
    def kinds(self) -> frozenset[ActivityKind]:
        return _GROUP_KINDS[self]


_GROUP_KINDS: dict[ActivityGroup, frozenset[ActivityKind]] = {
    ActivityGroup.SESSIONS: frozenset(
        {
            ActivityKind.SIGNED_IN,
            ActivityKind.SIGNED_OUT,
            ActivityKind.SESSION_CLOSED,
            ActivityKind.SESSION_LOCKED_OUT,
            ActivityKind.STAFF_SESSION_CLOSED,
            ActivityKind.RECOVERY_CODE_USED,
        }
    ),
    ActivityGroup.ORDERS: frozenset(
        {
            ActivityKind.ORDER_CREATED,
            ActivityKind.ORDER_ITEMS_ADDED,
            ActivityKind.ORDER_SERVED,
            ActivityKind.ORDER_CANCELLED,
            ActivityKind.ORDER_CHARGED,
            ActivityKind.PAYMENT_RECEIVED,
            ActivityKind.ORDER_DISCOUNTED,
            ActivityKind.ORDER_COURTESY,
            ActivityKind.ORDER_MOVED,
            ActivityKind.ORDER_MERGED,
            ActivityKind.ORDER_ITEM_VOIDED,
        }
    ),
    ActivityGroup.CASH: frozenset(
        {
            ActivityKind.CASH_OPENED,
            ActivityKind.CASH_CLOSED,
            ActivityKind.ORDER_CHARGED,
            ActivityKind.PAYMENT_RECEIVED,
        }
    ),
    ActivityGroup.CUSTOMERS_ACCESS: frozenset(
        {
            ActivityKind.CUSTOMER_CREATED,
            ActivityKind.CUSTOMER_UPDATED,
            ActivityKind.CUSTOMER_EXPORTED,
            ActivityKind.CUSTOMER_ANONYMIZED,
            ActivityKind.CUSTOMER_VIEWED,
            ActivityKind.CUSTOMER_REVEALED,
            ActivityKind.DATA_ACCESS_ALERT,
        }
    ),
    ActivityGroup.INVENTORY: frozenset(
        {
            ActivityKind.INGREDIENT_CREATED,
            ActivityKind.INGREDIENT_UPDATED,
            ActivityKind.STOCK_PURCHASE,
            ActivityKind.STOCK_WASTE,
            ActivityKind.STOCK_ADJUSTMENT,
            ActivityKind.WASTE_APPROVAL_REQUESTED,
            ActivityKind.WASTE_APPROVED,
            ActivityKind.WASTE_REJECTED,
            ActivityKind.STOCK_COUNT_OPENED,
            ActivityKind.STOCK_COUNT_CLOSED,
            ActivityKind.STOCK_COUNT_CANCELLED,
            ActivityKind.RECIPE_UPDATED,
            ActivityKind.SUPPLIER_CREATED,
            ActivityKind.SUPPLIER_UPDATED,
            ActivityKind.PURCHASE_ORDER_CREATED,
            ActivityKind.PURCHASE_ORDER_SENT,
            ActivityKind.PURCHASE_ORDER_RECEIVED,
            ActivityKind.PURCHASE_ORDER_CANCELLED,
        }
    ),
    ActivityGroup.MENU: frozenset(
        {
            ActivityKind.MENU_CATEGORY_CREATED,
            ActivityKind.MENU_CATEGORY_UPDATED,
            ActivityKind.MENU_CATEGORY_DELETED,
            ActivityKind.MENU_ITEM_CREATED,
            ActivityKind.MENU_ITEM_UPDATED,
            ActivityKind.MENU_ITEM_AVAILABILITY_CHANGED,
        }
    ),
    ActivityGroup.STAFF: frozenset(
        {
            ActivityKind.STAFF_REGISTERED,
            ActivityKind.STAFF_UPDATED,
            ActivityKind.STAFF_STATUS_CHANGED,
            ActivityKind.STAFF_PASSWORD_RESET,
            ActivityKind.ROLE_CREATED,
            ActivityKind.ROLE_UPDATED,
            ActivityKind.ROLE_DELETED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    restaurant_id: int
    user_id: int
    kind: ActivityKind
    detail: str = ""
    id: int | None = None
    occurred_at: datetime = field(default_factory=utc_now)

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
