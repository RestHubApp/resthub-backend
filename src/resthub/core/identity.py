"""Identidad compartida por todos los módulos.

Quién es el usuario, qué rol tiene y a qué restaurante pertenece es una
pregunta que se hace cada módulo: el menú, los pedidos y el inventario por
igual. Si la respuesta viviera en `accounts`, todos tendrían que importarlo y
dejaría de haber módulos independientes.

Por eso el núcleo posee la *autenticación* (quién sos) y `accounts` posee la
*gestión de usuarios* (tu perfil, el alta del personal). Este archivo es Python
puro a propósito: lo importa hasta la capa de dominio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Role(StrEnum):
    """Tipo de cuenta.

    Son dos y fijos: quien administra el local desde la laptop y quien toma
    pedidos desde el celular. Sin roles editables, lo que cada uno puede hacer
    lo decide el mapa de `permissions.py`.
    """

    ADMIN = "admin"
    WAITER = "waiter"

    @property
    def label(self) -> str:
        return _ROLE_LABELS[self]


_ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "Encargado",
    Role.WAITER: "Mesero",
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Quien hace la petición, reducido a lo que la autorización necesita.

    No es la entidad `User` de `accounts`: no trae nombre ni correo. Un módulo
    que solo tiene que decidir si puede tocar un recurso no necesita conocer el
    perfil de nadie.
    """

    user_id: int
    role: Role
    is_active: bool
    # El restaurante al que pertenece la cuenta. Todo caso de uso filtra por
    # este valor y nunca por uno que mande el cliente: es la frontera entre
    # restaurantes.
    restaurant_id: int
    # Códigos de permiso de su rol. Texto y no el enum del catálogo: este
    # archivo no puede importar el catálogo, que a su vez depende de `Role`.
    permissions: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: int
    role: Role
    restaurant_id: int


class TokenService(Protocol):
    """Puerto de emisión y lectura de credenciales portables.

    Qué formato tengan (JWT, PASETO, una fila en Redis) es decisión del
    adaptador. El negocio solo necesita ir de una identidad a un token y volver.
    """

    def issue(self, user_id: int, role: Role, restaurant_id: int) -> AccessToken: ...

    def decode(self, token: str) -> TokenClaims: ...


class IdentityError(Exception):
    """Raíz de los errores de identidad."""


class InvalidToken(IdentityError):
    def __init__(self, reason: str = "El token no es válido o ya expiró.") -> None:
        super().__init__(reason)
        self.reason = reason


class MissingPermission(IdentityError):
    def __init__(self, required: tuple[str, ...]) -> None:
        super().__init__("Tu rol no tiene permiso para esta acción.")
        self.required = required


def ensure_permission(principal: Principal, *required: str) -> None:
    """Alcanza con tener uno de los permisos pedidos.

    Vive acá y no en el adaptador HTTP para que la misma comprobación sirva a
    un consumidor que no hable HTTP.
    """
    if not any(permission in principal.permissions for permission in required):
        raise MissingPermission(tuple(required))
