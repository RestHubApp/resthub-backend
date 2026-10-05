from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


class CustomerRow(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_customers_restaurant", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20), default="")
    # El teléfono sin espacios, para buscar y para que no se repita.
    phone_key: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    reference: Mapped[str] = mapped_column(String(150), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Consentimiento para tratar sus datos (Ley N.º 29733). Vacío en los
    # clientes guardados antes de pedirlo.
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_version: Mapped[str] = mapped_column(String(20), default="")
    consent_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", name="fk_customers_consent_by", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_customers_restaurant_phone", "restaurant_id", "phone_key"),
        Index("ix_customers_restaurant_name", "restaurant_id", "name"),
    )
