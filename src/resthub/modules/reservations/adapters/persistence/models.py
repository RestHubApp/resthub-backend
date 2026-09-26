from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


class ReservationRow(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_reservations_restaurant", ondelete="RESTRICT")
    )
    customer_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20), default="")
    party_size: Mapped[int] = mapped_column(Integer)
    reserved_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    table_id: Mapped[int | None] = mapped_column(
        ForeignKey("dining_tables.id", name="fk_reservations_table", ondelete="SET NULL"),
        nullable=True,
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", name="fk_reservations_customer", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_reservations_created_by", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_reservations_restaurant_time", "restaurant_id", "reserved_for"),
        Index("ix_reservations_table_time", "table_id", "reserved_for"),
    )
