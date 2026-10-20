from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resthub.core.clock import utc_now
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

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
    # La última versión de los términos y condiciones aceptada.
    terms_version: Mapped[str] = mapped_column(String(20), default="")
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # La última versión del tratamiento de datos personales aceptada (aparte).
    privacy_version: Mapped[str] = mapped_column(String(20), default="")
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Desde cuándo valen las sesiones: los tokens emitidos antes ya no sirven.
    # Lo sellan el cambio de contraseña y la desactivación de la cuenta.
    sessions_valid_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # La contraseña la puso otra persona: hasta cambiarla, la cuenta solo puede
    # hacer eso (`core/auth.py`).
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Cuándo se fijó la contraseña temporal (un restablecimiento); nulo en el alta, que
    # cuenta desde `created_at`. Vence a las `TEMPORARY_PASSWORD_HOURS` (ASVS 6.4.1).
    temporary_password_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Quién desactivó la cuenta, cuándo y por qué; vacíos mientras esté activa.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_users_deactivated_by", ondelete="SET NULL"),
        nullable=True,
    )
    deactivation_reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # El hash del PIN que desbloquea la sesión tras la inactividad (RFS-29); nulo sin PIN.
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Siempre hace falta con la cuenta (su nombre y sus permisos), así que
    # viene en la misma consulta.
    role: Mapped[RoleRow] = relationship(lazy="joined", innerjoin=True)


class PreviewCodeRow(Base):
    """Códigos de un solo uso para entrar a la vista previa como una cuenta del local de muestra.

    Los emite la administración del sistema y los canjea `POST /auth/preview`.
    Es una credencial de una cuenta del personal, por eso la posee este módulo;
    `platform_admin_id` es solo quién la pidió, para firmarlo en el token.
    """

    __tablename__ = "preview_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # SHA-256 en hexadecimal: el código en claro nunca se guarda.
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_preview_codes_user", ondelete="RESTRICT")
    )
    # Por nombre de tabla: importar el modelo de `platform` rompería la
    # independencia entre módulos.
    platform_admin_id: Mapped[int] = mapped_column(
        ForeignKey("platform_admins.id", name="fk_preview_codes_admin", ondelete="RESTRICT")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
