from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base
from resthub.modules.platform.domain.entities import MAX_DETAIL_LENGTH


class PlatformAdminRow(Base):
    """Cuentas de la administración del sistema.

    Tabla propia y no una fila más de `users`: sin `restaurant_id` ni rol, no
    hay consulta del personal que pueda devolverlas ni permiso de un local que
    pueda alcanzarlas.
    """

    __tablename__ = "platform_admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Único en esta tabla. Puede coincidir con el de una cuenta del personal:
    # son accesos distintos, cada uno con su pantalla.
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PlatformActivityRow(Base):
    __tablename__ = "platform_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(
        ForeignKey("platform_admins.id", name="fk_platform_activity_admin", ondelete="RESTRICT"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(MAX_DETAIL_LENGTH), default="")
    # La pantalla siempre pide lo último.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
