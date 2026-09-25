from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base
from resthub.core.local_time import DEFAULT_TIMEZONE


class RestaurantRow(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Nombre IANA, no un desplazamiento fijo: así un local en una zona con
    # horario de verano no necesita que nadie lo corrija dos veces al año.
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TIMEZONE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
