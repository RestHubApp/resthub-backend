"""Errores de dominio de mesas y pedidos.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class OrdersError(Exception):
    """Raíz de los errores del módulo de pedidos."""


class InvalidTable(OrdersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidOrder(OrdersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TableNotFound(OrdersError):
    """También cuando la mesa es de otro restaurante: no se delata que existe."""

    def __init__(self, table_id: int) -> None:
        super().__init__(f"No existe la mesa {table_id}.")
        self.table_id = table_id


class TableLabelTaken(OrdersError):
    def __init__(self, label: str) -> None:
        super().__init__(f"Ya existe una mesa llamada {label!r}.")
        self.label = label


class TableInactive(OrdersError):
    def __init__(self, label: str) -> None:
        super().__init__(f"La mesa {label} está desactivada.")
        self.label = label


class TableOccupied(OrdersError):
    def __init__(self, label: str, order_id: int) -> None:
        super().__init__(f"La mesa {label} ya tiene un pedido activo.")
        self.label = label
        self.order_id = order_id


class InvalidTableOrdering(OrdersError):
    def __init__(self) -> None:
        super().__init__(
            "El nuevo orden tiene que nombrar exactamente una vez a cada mesa del local."
        )


class OrderNotFound(OrdersError):
    """También cuando el pedido es de otro restaurante, o de otro mesero y ya cerró."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"No existe el pedido {order_id}.")
        self.order_id = order_id


class OrderItemNotFound(OrdersError):
    def __init__(self, item_id: int) -> None:
        super().__init__(f"El pedido no tiene el ítem {item_id}.")
        self.item_id = item_id


class DishNotFound(OrdersError):
    def __init__(self, menu_item_id: int) -> None:
        super().__init__(f"No existe el plato {menu_item_id}.")
        self.menu_item_id = menu_item_id


class DishUnavailable(OrdersError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name} no está disponible hoy.")
        self.name = name


class InvalidTransition(OrdersError):
    """El pedido no está en un estado desde el que se pueda hacer eso."""

    def __init__(self, status_label: str, action: str) -> None:
        super().__init__(f"Un pedido «{status_label}» no se puede {action}.")
        self.status_label = status_label
        self.action = action


class OrderNumberTaken(OrdersError):
    def __init__(self) -> None:
        super().__init__("Otro pedido tomó el mismo número a la vez. Vuelve a intentarlo.")
