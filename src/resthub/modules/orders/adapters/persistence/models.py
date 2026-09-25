from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resthub.core.database import Base


class DiningTableRow(Base):
    __tablename__ = "dining_tables"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Claves foráneas por nombre de tabla y no por modelo: importar los modelos
    # de `restaurants`, `accounts` o `menu` rompería la independencia.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_dining_tables_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    label: Mapped[str] = mapped_column(String(30))
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sin índice propio: lo cubren los compuestos de abajo, que empiezan por él.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_orders_restaurant", ondelete="RESTRICT")
    )
    number: Mapped[int] = mapped_column(Integer)
    business_date: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    table_id: Mapped[int | None] = mapped_column(
        ForeignKey("dining_tables.id", name="fk_orders_table", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    waiter_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_orders_waiter", ondelete="RESTRICT"), index=True
    )
    customer_name: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    # Lo calcula el dominio; se guarda para que los reportes sumen ventas sin
    # recorrer los ítems de cada pedido.
    total: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    cancel_reason: Mapped[str] = mapped_column(String(300), default="")
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    amount_received: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Solo lo mueve un cambio de estado; editar notas toca `updated_at` y no esto.
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[OrderItemRow]] = relationship(
        cascade="all, delete-orphan",
        # Con sesiones asíncronas no hay carga perezosa: los ítems llegan en
        # la misma ida a la base que el pedido.
        lazy="selectin",
        order_by="OrderItemRow.id",
    )

    __table_args__ = (
        # El árbitro real del correlativo diario. El caso de uso serializa la
        # numeración, pero la base es la última palabra.
        UniqueConstraint("restaurant_id", "business_date", "number", name="uq_orders_daily_number"),
        # El tablero pide los activos del local; los reportes, un rango de días.
        Index("ix_orders_restaurant_status", "restaurant_id", "status"),
        Index("ix_orders_restaurant_day", "restaurant_id", "business_date"),
    )


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Redundante con el del pedido, a propósito: toda tabla de negocio lleva
    # su restaurante, y los reportes de platos vendidos filtran sin unir.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_order_items_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", name="fk_order_items_order", ondelete="CASCADE"), index=True
    )
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_items.id", name="fk_order_items_menu_item", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    quantity: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
