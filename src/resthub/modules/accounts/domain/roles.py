"""Roles del personal, que arma cada restaurante.

Todo restaurante tiene un encargado y un mesero; el resto (cocinero, cajero)
los crea el propio local. Un rol es un nombre y una lista de permisos del
catálogo del núcleo: el endpoint sigue exigiendo el permiso, nunca el rol.

Python puro, como el resto del dominio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from resthub.core.permissions import (
    DEFAULT_WAITER_PERMISSIONS,
    Permission,
    RoleKind,
    effective_permissions,
)
from resthub.modules.accounts.domain.exceptions import (
    BaseRoleNameFixed,
    BaseRoleNotDeletable,
    CannotGrantPermissions,
    InvalidRoleName,
    RoleInUse,
    RoleNotEditable,
)

MAX_ROLE_NAME_LENGTH = 40
OWNER_ROLE_NAME = "Encargado"
WAITER_ROLE_NAME = "Mesero"


def validate_role_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidRoleName("El nombre del rol no puede quedar vacío.")
    if len(name) > MAX_ROLE_NAME_LENGTH:
        raise InvalidRoleName(
            f"El nombre del rol no puede pasar de {MAX_ROLE_NAME_LENGTH} caracteres."
        )
    return name


def role_name_key(name: str) -> str:
    """Con qué se compara un nombre: "cocinero" y "Cocinero " son el mismo rol."""
    return " ".join(name.split()).casefold()


@dataclass(slots=True)
class Role:
    restaurant_id: int
    name: str
    kind: RoleKind
    # Lo que se guardó. Del encargado no se lee: ver `permissions`.
    stored_permissions: frozenset[Permission] = frozenset()
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.name = validate_role_name(self.name)

    @classmethod
    def owner(cls, restaurant_id: int) -> Role:
        return cls(
            restaurant_id=restaurant_id,
            name=OWNER_ROLE_NAME,
            kind=RoleKind.OWNER,
            stored_permissions=frozenset(Permission),
        )

    @classmethod
    def waiter(cls, restaurant_id: int) -> Role:
        return cls(
            restaurant_id=restaurant_id,
            name=WAITER_ROLE_NAME,
            kind=RoleKind.WAITER,
            stored_permissions=DEFAULT_WAITER_PERMISSIONS,
        )

    @property
    def name_key(self) -> str:
        return role_name_key(self.name)

    @property
    def permissions(self) -> frozenset[Permission]:
        return effective_permissions(self.kind, (code.value for code in self.stored_permissions))

    @property
    def is_editable(self) -> bool:
        return self.kind is not RoleKind.OWNER

    def update(self, name: str, permissions: Iterable[Permission]) -> None:
        """Nombre y permisos de un rol.

        El mesero conserva su nombre: lo reconoce todo el personal y es el rol
        con el que nace cada cuenta de salón. Mandarlo igual es válido, así la
        interfaz envía el formulario entero.
        """
        if self.kind is RoleKind.OWNER:
            raise RoleNotEditable()
        new_name = validate_role_name(name)
        if self.kind is RoleKind.WAITER and role_name_key(new_name) != self.name_key:
            raise BaseRoleNameFixed(self.name)
        if self.kind is RoleKind.CUSTOM:
            self.name = new_name
        self.stored_permissions = frozenset(permissions)

    def ensure_deletable(self, member_count: int) -> None:
        if self.kind is not RoleKind.CUSTOM:
            raise BaseRoleNotDeletable(self.name)
        # La clave foránea lo impediría igual; así el mensaje dice qué hacer.
        if member_count:
            raise RoleInUse(self.name)


def ensure_can_grant(actor_permissions: frozenset[str], granted: Iterable[Permission]) -> None:
    missing = frozenset(code.value for code in granted) - actor_permissions
    if missing:
        raise CannotGrantPermissions(missing)


def holds_all(actor_permissions: frozenset[str], permissions: Iterable[Permission]) -> bool:
    return all(code.value in actor_permissions for code in permissions)
