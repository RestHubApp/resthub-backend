"""Errores de dominio de clientes. Los adaptadores los traducen a HTTP."""

from __future__ import annotations

from typing import ClassVar

from resthub.core.error_codes import ErrorCode


class CustomersError(Exception):
    """Raíz de los errores del módulo de clientes."""

    code: ClassVar[ErrorCode]


class InvalidCustomerError(CustomersError):
    code = ErrorCode.INVALID_CUSTOMER

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CustomerNotFoundError(CustomersError):
    """También cuando es de otro restaurante."""

    code = ErrorCode.CUSTOMER_NOT_FOUND

    def __init__(self, customer_id: int) -> None:
        super().__init__(f"No existe el cliente {customer_id}.")
        self.customer_id = customer_id


class CustomerHasActiveOrdersError(CustomersError):
    code = ErrorCode.CUSTOMER_HAS_ACTIVE_ORDERS

    def __init__(self) -> None:
        super().__init__(
            "El cliente tiene pedidos en curso. Bórralo cuando estén cobrados o cancelados."
        )


class ConsentRequiredError(CustomersError):
    code = ErrorCode.CONSENT_REQUIRED

    def __init__(self) -> None:
        super().__init__(
            "Para guardar al cliente en la libreta, primero tiene que aceptar "
            "el tratamiento de sus datos personales."
        )


class PhoneTakenError(CustomersError):
    code = ErrorCode.PHONE_TAKEN

    def __init__(self, phone: str, name: str) -> None:
        super().__init__(f"El teléfono {phone} ya es de {name}.")
        self.phone = phone
        self.name = name
