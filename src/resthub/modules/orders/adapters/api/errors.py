"""Traducción de los errores del dominio de pedidos a HTTP, en un solo lugar."""

from __future__ import annotations

from fastapi import HTTPException, status

from resthub.modules.orders.domain.exceptions import (
    DishNotFound,
    InvalidOrder,
    InvalidTable,
    InvalidTableOrdering,
    OrderItemNotFound,
    OrderNotFound,
    OrdersError,
    TableNotFound,
)

# 404 también para lo que es de otro restaurante: responder 403 ("existe, pero
# no es tuyo") delataría qué identificadores usa el resto de los locales.
_NOT_FOUND = (TableNotFound, OrderNotFound, OrderItemNotFound, DishNotFound)
_INVALID = (InvalidOrder, InvalidTable, InvalidTableOrdering)


def http_error(error: OrdersError) -> HTTPException:
    if isinstance(error, _NOT_FOUND):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, _INVALID):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))
    # El resto son conflictos con el estado actual: mesa ocupada, plato
    # agotado, transición imposible, nombre repetido.
    return HTTPException(status.HTTP_409_CONFLICT, str(error))
