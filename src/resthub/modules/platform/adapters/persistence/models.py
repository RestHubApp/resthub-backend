from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
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


# Identificadores de 64 bits en PostgreSQL: la telemetría escribe una fila por
# petición y los identificadores no se reutilizan al purgar. En SQLite tiene que
# ser `INTEGER` para que la clave primaria sea el `rowid` autoincremental.
_TelemetryId = BigInteger().with_variant(Integer(), "sqlite")


class ObsRequestRow(Base):
    """Una petición HTTP atendida, para el panel de observabilidad.

    Telemetría y no negocio: sin claves foráneas a restaurantes ni cuentas,
    para que sobreviva a sus bajas y para que escribirla nunca choque con una
    restricción. `restaurant_id` y `account_id` son enteros sueltos, y
    `account_id` es de `users` o de `platform_admins` según `account_kind`.
    """

    __tablename__ = "obs_requests"

    id: Mapped[int] = mapped_column(_TelemetryId, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(String(10))
    # La plantilla de la ruta (`/api/v1/orders/{order_id}`), nunca la ruta con
    # ids ni la query string.
    route: Mapped[str] = mapped_column(String(255))
    status: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float)
    db_ms: Mapped[float] = mapped_column(Float)
    db_queries: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(128))
    account_kind: Mapped[str] = mapped_column(String(16))
    restaurant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_obs_requests_at", "at"),
        Index("ix_obs_requests_route_at", "route", "at"),
        Index("ix_obs_requests_request_id", "request_id"),
    )


class ObsEventRow(Base):
    """Un evento de log de nivel `warning` o `error`, con su traceback si lo tuvo."""

    __tablename__ = "obs_events"

    id: Mapped[int] = mapped_column(_TelemetryId, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(10))
    logger: Mapped[str] = mapped_column(String(120))
    event: Mapped[str] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    restaurant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON guardado como texto y no como columna `JSON`: el buscador del panel
    # lo recorre con `LIKE`, igual en PostgreSQL que en SQLite.
    fields: Mapped[str] = mapped_column(Text, default="{}")
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_obs_events_at", "at"),
        Index("ix_obs_events_level_at", "level", "at"),
        Index("ix_obs_events_request_id", "request_id"),
    )
