"""Traducción entre la fila de la tabla y la entidad de dominio.

Este archivo es la razón por la que el dominio puede ignorar SQLAlchemy.
"""

from __future__ import annotations

from resthub.core.permissions import Permission, RoleKind
from resthub.core.timestamps import as_utc
from resthub.modules.accounts.adapters.persistence.models import RoleRow, UserRow
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.roles import Role

_KNOWN_CODES = frozenset(permission.value for permission in Permission)


def role_row_to_entity(row: RoleRow) -> Role:
    return Role(
        id=row.id,
        restaurant_id=row.restaurant_id,
        name=row.name,
        kind=RoleKind(row.kind),
        # Un código que salió del catálogo se descarta al leer.
        stored_permissions=frozenset(
            Permission(code) for code in row.permissions or () if code in _KNOWN_CODES
        ),
        created_at=as_utc(row.created_at),
    )


def sorted_codes(permissions: frozenset[Permission]) -> list[str]:
    return sorted(permission.value for permission in permissions)


def role_entity_to_row(role: Role) -> RoleRow:
    return RoleRow(
        restaurant_id=role.restaurant_id,
        name=role.name,
        name_key=role.name_key,
        kind=role.kind.value,
        permissions=sorted_codes(role.stored_permissions),
        created_at=role.created_at,
    )


def row_to_entity(row: UserRow) -> User:
    return User(
        id=row.id,
        restaurant_id=row.restaurant_id,
        email=row.email,
        full_name=row.full_name,
        role=role_row_to_entity(row.role),
        password_hash=row.password_hash,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def entity_to_row(user: User) -> UserRow:
    return UserRow(
        restaurant_id=user.restaurant_id,
        email=user.email,
        full_name=user.full_name,
        role_id=user.role.id,
        password_hash=user.password_hash,
        is_active=user.is_active,
        created_at=user.created_at,
    )
