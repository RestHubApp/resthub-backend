from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base
from resthub.core.local_time import DEFAULT_TIMEZONE
from resthub.modules.restaurants.domain.entities import DEFAULT_WAITER_DISCOUNT_PERCENT


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
