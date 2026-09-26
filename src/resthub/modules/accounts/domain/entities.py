"""Entidades de dominio de cuentas.

Python puro. Sin FastAPI, sin SQLAlchemy, sin Pydantic. Este archivo debe
poder ejecutarse sin que exista una base de datos ni un servidor web, y los
contratos de Import Linter lo verifican.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from resthub.core.credentials import MAX_PASSWORD_LENGTH as MAX_PASSWORD_LENGTH
from resthub.core.credentials import MIN_PASSWORD_LENGTH as MIN_PASSWORD_LENGTH
from resthub.core.credentials import normalized_email, password_problem
from resthub.modules.accounts.domain.exceptions import (
    CannotChangeOwnRole,
    CannotDeactivateSelf,
    CannotManageStrongerAccount,
    CannotResetOwnPassword,
    InvalidEmail,
    InvalidFullName,
    WeakPassword,
)
from resthub.modules.accounts.domain.roles import Role, holds_all

MAX_FULL_NAME_LENGTH = 120


@dataclass(slots=True)
class User:
    # Obligatorio: no existe una cuenta sin restaurante, ni siquiera la del
    # primer encargado, que nace junto con el suyo.
    restaurant_id: int
    email: str
    full_name: str
    # El rol entero y no solo su identificador: su nombre se muestra y sus
    # permisos deciden quién puede gestionar esta cuenta.
    role: Role
    password_hash: str
    is_active: bool = True
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.email = normalize_email(self.email)
        self.full_name = validate_full_name(self.full_name)

    def rename(self, full_name: str) -> None:
        self.full_name = validate_full_name(full_name)

    def deactivate(self) -> None:
        self.is_active = False

    def activate(self) -> None:
        self.is_active = True


def normalize_email(raw: str) -> str:
    email = normalized_email(raw)
    if email is None:
        raise InvalidEmail(raw)
    return email


def validate_full_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidFullName("El nombre no puede quedar vacío.")
    if len(name) > MAX_FULL_NAME_LENGTH:
        raise InvalidFullName(f"El nombre no puede pasar de {MAX_FULL_NAME_LENGTH} caracteres.")
    return name


def validate_new_password(plain_password: str) -> str:
    """Longitud de una contraseña nueva.

    Vive en el dominio y no solo en el esquema HTTP porque también la usa el
    script de alta de restaurantes, que no pasa por la API.
    """
    problem = password_problem(plain_password)
    if problem is not None:
        raise WeakPassword(problem)
    return plain_password


# Las tres reglas que siguen protegen lo mismo: que el restaurante no se quede
# sin quien lo administre. Como el encargado no puede degradarse ni desactivarse
# a sí mismo, siempre queda al menos uno activo, el que está operando. La
# cuarta impide que alguien con menos permisos degrade o tome su cuenta.


def ensure_can_deactivate(actor_id: int, target: User) -> None:
    if target.id == actor_id:
        raise CannotDeactivateSelf()


def ensure_can_change_role(actor_id: int, target: User, new_role: Role) -> None:
    if target.id == actor_id and new_role.id != target.role.id:
        raise CannotChangeOwnRole()


def ensure_can_reset_password(actor_id: int, target: User) -> None:
    # Sobre la propia cuenta se exige la contraseña actual, que este camino
    # no pide: si no, una sesión olvidada abierta alcanzaría para cambiarla.
    if target.id == actor_id:
        raise CannotResetOwnPassword()


def ensure_can_manage(actor_permissions: frozenset[str], target: User) -> None:
    if not holds_all(actor_permissions, target.role.permissions):
        raise CannotManageStrongerAccount()
