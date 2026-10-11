"""Traducción de los errores del dominio de pedidos a HTTP, en un solo lugar."""

from __future__ import annotations

from fastapi import HTTPException, status

from resthub.core.errors import api_error
from resthub.modules.orders.domain.exceptions import (
    CashSessionNotFoundError,
    CustomerNotFoundError,
    DiscountNotAllowedError,
    DishNotFoundError,
    InvalidCashSessionError,
    InvalidOrderError,
    InvalidTableError,
    InvalidTableOrderingError,
    NotYourOrderError,
    OrderItemNotFoundError,
    OrderNotFoundError,
    OrdersError,
    TableNotFoundError,
)

# 404 también para lo que es de otro restaurante: responder 403 ("existe, pero
# no es tuyo") delataría qué identificadores usa el resto de los locales.
_NOT_FOUND = (
    TableNotFoundError,
    OrderNotFoundError,
    OrderItemNotFoundError,
    DishNotFoundError,
    CashSessionNotFoundError,
    CustomerNotFoundError,
)
_INVALID = (
    InvalidOrderError,
    InvalidTableError,
    InvalidTableOrderingError,
    InvalidCashSessionError,
)
# El pedido se ve (no hay nada que ocultar), pero quien pregunta no puede
# hacer eso con él: un descuento sobre su tope, o cobrar la mesa de otro.
_FORBIDDEN = (DiscountNotAllowedError, NotYourOrderError)


def http_error(error: OrdersError) -> HTTPException:
    if isinstance(error, _NOT_FOUND):
        return api_error(status.HTTP_404_NOT_FOUND, error)
    if isinstance(error, _INVALID):
        return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
    if isinstance(error, _FORBIDDEN):
        return api_error(status.HTTP_403_FORBIDDEN, error)
    # El resto son conflictos con el estado actual: mesa ocupada, plato
    # agotado, transición imposible, caja cerrada, saldo que cambió.
    return api_error(status.HTTP_409_CONFLICT, error)
