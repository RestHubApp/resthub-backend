"""Errores de dominio de clientes. Los adaptadores los traducen a HTTP."""

from __future__ import annotations


class CustomersError(Exception):
    """Raíz de los errores del módulo de clientes."""


class InvalidCustomer(CustomersError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CustomerNotFound(CustomersError):
    """También cuando es de otro restaurante."""

    def __init__(self, customer_id: int) -> None:
        super().__init__(f"No existe el cliente {customer_id}.")
        self.customer_id = customer_id


class PhoneTaken(CustomersError):
    def __init__(self, phone: str, name: str) -> None:
        super().__init__(f"El teléfono {phone} ya es de {name}.")
        self.phone = phone
        self.name = name
