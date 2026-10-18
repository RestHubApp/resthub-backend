from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.clock import utc_now
from resthub.core.database import Base
from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.restaurants.domain.entities import (
    DEFAULT_IDLE_LOCK_MINUTES,
    DEFAULT_WAITER_DISCOUNT_PERCENT,
    DEFAULT_WASTE_APPROVAL_THRESHOLD,
)

SANDBOX_INDEX = "uq_restaurants_one_active_sandbox"


class RestaurantRow(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Nombre IANA, no un desplazamiento fijo: así un local en una zona con
    # horario de verano no necesita que nadie lo corrija dos veces al año.
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)
    # Lo lee `orders` al aplicar un descuento, por SQL y sin importar este módulo.
    max_waiter_discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2), default=DEFAULT_WAITER_DISCOUNT_PERCENT
    )
    # Lo leen `menu` y `orders` para agotar los platos sin insumos.
    auto_out_of_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    # Lo lee `insights` para decidir si las notas van a la IA externa.
    external_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Lo lee `inventory` al registrar una merma. NULL: ninguna pide aprobación.
    waste_approval_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2),
        nullable=True,
        default=DEFAULT_WASTE_APPROVAL_THRESHOLD,
        server_default=str(DEFAULT_WASTE_APPROVAL_THRESHOLD),
    )
    # El horario de atención, `[{day, opens, closes}]`. Lo lee `core/access_history.py`
    # para marcar los accesos fuera de él (RFS-32). NULL o vacío: sin horario.
    opening_hours: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Lo lee `core/auth.py` para bloquear la sesión que pasó ese tiempo sin uso.
    idle_lock_minutes: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_IDLE_LOCK_MINUTES, server_default=str(DEFAULT_IDLE_LOCK_MINUTES)
    )
    # El local de muestra de la vista previa. La lista de la plataforma no lo
    # muestra y `core/auth.py` exige que lo sea para aceptar un token de vista
    # previa.
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        # A lo sumo un local de muestra vigente. La plataforma archiva el
        # anterior antes de crear otro; el índice parcial es la última palabra
        # si dos pedidos lo crean a la vez.
        Index(
            SANDBOX_INDEX,
            "is_sandbox",
            unique=True,
            sqlite_where=text("is_sandbox AND is_active"),
            postgresql_where=text("is_sandbox AND is_active"),
        ),
    )
