"""Traducción entre la fila de la tabla y la entidad de dominio.

Este archivo es la razón por la que el dominio puede ignorar SQLAlchemy.
"""

from __future__ import annotations

from resthub.core.identity import Role
from resthub.core.timestamps import as_utc
from resthub.modules.accounts.adapters.persistence.models import UserRow
from resthub.modules.accounts.domain.entities import User


def row_to_entity(row: UserRow) -> User:
    return User(
        id=row.id,
        restaurant_id=row.restaurant_id,
        email=row.email,
        full_name=row.full_name,
        role=Role(row.role),
        password_hash=row.password_hash,
        is_active=row.is_active,
        created_at=as_utc(row.created_at),
    )


def entity_to_row(user: User) -> UserRow:
    return UserRow(
        restaurant_id=user.restaurant_id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        password_hash=user.password_hash,
        is_active=user.is_active,
        created_at=user.created_at,
    )
