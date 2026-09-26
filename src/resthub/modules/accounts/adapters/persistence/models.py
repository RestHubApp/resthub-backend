from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resthub.core.database import Base


class RoleRow(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sin índice propio: lo cubre la restricción única, que empieza por él.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_roles_restaurant", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(40))
    name_key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10))
    # Códigos del catálogo. Una lista y no una tabla intermedia: se lee y se
    # escribe entera, y el catálogo vive en el código, no en la base.
    permissions: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("restaurant_id", "name_key", name="uq_roles_restaurant_name"),
    )


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Clave foránea por nombre de tabla y no por el modelo de `restaurants`:
    # importarlo rompería la independencia entre módulos.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_users_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    # Único en toda la tabla, no por restaurante: el acceso es por correo y
    # contraseña, sin elegir antes el local.
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    # RESTRICT: un rol con personal no se borra.
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", name="fk_users_role", ondelete="RESTRICT"), index=True
    )
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    # Siempre hace falta con la cuenta (su nombre y sus permisos), así que
    # viene en la misma consulta.
    role: Mapped[RoleRow] = relationship(lazy="joined", innerjoin=True)
