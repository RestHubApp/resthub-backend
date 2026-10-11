"""Errores de dominio de mesas y pedidos.

Los adaptadores los traducen a códigos HTTP. El dominio no conoce HTTP.
"""

from __future__ import annotations


class OrdersError(Exception):
    """Raíz de los errores del módulo de pedidos."""


class InvalidTableError(OrdersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidOrderError(OrdersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TableNotFoundError(OrdersError):
    """También cuando la mesa es de otro restaurante: no se delata que existe."""

    def __init__(self, table_id: int) -> None:
        super().__init__(f"No existe la mesa {table_id}.")
        self.table_id = table_id


class TableLabelTakenError(OrdersError):
    def __init__(self, label: str) -> None:
        super().__init__(f"Ya existe una mesa llamada {label!r}.")
        self.label = label


class TableInactiveError(OrdersError):
    def __init__(self, label: str) -> None:
        super().__init__(f"La mesa {label} está desactivada.")
        self.label = label


class TableOccupiedError(OrdersError):
    def __init__(self, label: str, order_id: int) -> None:
        super().__init__(f"La mesa {label} ya tiene un pedido activo.")
        self.label = label
        self.order_id = order_id


class InvalidTableOrderingError(OrdersError):
    def __init__(self) -> None:
        super().__init__(
            "El nuevo orden tiene que nombrar exactamente una vez a cada mesa del local."
        )


class OrderNotFoundError(OrdersError):
    """También cuando el pedido es de otro restaurante, o de otro mesero y ya cerró."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"No existe el pedido {order_id}.")
        self.order_id = order_id


class OrderItemNotFoundError(OrdersError):
    def __init__(self, item_id: int) -> None:
        super().__init__(f"El pedido no tiene el ítem {item_id}.")
        self.item_id = item_id


class DishNotFoundError(OrdersError):
    def __init__(self, menu_item_id: int) -> None:
        super().__init__(f"No existe el plato {menu_item_id}.")
        self.menu_item_id = menu_item_id


class DishUnavailableError(OrdersError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name} no está disponible hoy.")
        self.name = name


class InvalidTransitionError(OrdersError):
    """El pedido no está en un estado desde el que se pueda hacer eso."""

    def __init__(self, status_label: str, action: str) -> None:
        super().__init__(f"Un pedido «{status_label}» no se puede {action}.")
        self.status_label = status_label
        self.action = action


class OrderNumberTakenError(OrdersError):
    def __init__(self) -> None:
        super().__init__("Otro pedido tomó el mismo número a la vez. Vuelve a intentarlo.")


class CustomerNotFoundError(OrdersError):
    """También cuando el cliente es de otro restaurante."""

    def __init__(self, customer_id: int) -> None:
        super().__init__(f"No existe el cliente {customer_id}.")
        self.customer_id = customer_id


class OrderHasPaymentsError(OrdersError):
    """Descuentos, cortesías y cancelaciones van antes del primer pago."""

    def __init__(self, action: str) -> None:
        super().__init__(f"Un pedido con pagos registrados no se puede {action}.")
        self.action = action


class BalanceChangedError(OrdersError):
    """El saldo no es el que vio quien cobra: otro pago entró antes, o un doble toque."""

    def __init__(self, balance: object) -> None:
        super().__init__(
            f"La cuenta cambió mientras se cobraba: ahora faltan S/ {balance}. Revísala."
        )
        self.balance = balance


class DiscountNotAllowedError(OrdersError):
    def __init__(self, limit: object) -> None:
        super().__init__(
            f"Tu descuento máximo es {limit} %. Uno mayor, o una cortesía, lo aplica el encargado."
        )
        self.limit = limit


class NotYourOrderError(OrdersError):
    """El pedido se ve, pero la acción es de quien lo tomó o del encargado."""

    def __init__(self, action: str) -> None:
        super().__init__(f"Solo el mesero que tomó el pedido o el encargado pueden {action}.")
        self.action = action


class CashRegisterClosedError(OrdersError):
    def __init__(self) -> None:
        super().__init__("La caja está cerrada. El encargado tiene que abrirla para cobrar.")


class CashRegisterAlreadyOpenError(OrdersError):
    def __init__(self) -> None:
        super().__init__("Ya hay una caja abierta. Ciérrala antes de abrir otra.")


class CashSessionNotFoundError(OrdersError):
    def __init__(self, session_id: int | None = None) -> None:
        super().__init__(
            "No hay una caja abierta."
            if session_id is None
            else f"No existe el turno de caja {session_id}."
        )
        self.session_id = session_id


class InvalidCashSessionError(OrdersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
