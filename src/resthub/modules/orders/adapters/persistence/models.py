from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
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
    customer_phone: Mapped[str] = mapped_column(String(20), default="")
    delivery_address: Mapped[str] = mapped_column(String(200), default="")
    delivery_reference: Mapped[str] = mapped_column(String(150), default="")
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", name="fk_orders_customer", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # El identificador que generó el celular del mesero; ver `Order`.
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str] = mapped_column(String(300), default="")
    # Lo calcula el dominio; se guarda para que los reportes sumen ventas sin
    # recorrer los ítems de cada pedido.
    total: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    cancel_reason: Mapped[str] = mapped_column(String(300), default="")
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2), default=Decimal("0.00")
    )
    discount_reason: Mapped[str] = mapped_column(String(200), default="")
    discounted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_orders_discounted_by", ondelete="RESTRICT"),
        nullable=True,
    )
    # Calculados por el dominio, como `total`: el arqueo y los reportes suman
    # descuentos y cortesías sin recorrer los ítems.
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.00")
    )
    courtesy_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.00")
    )
    # El medio del pago, o `mixed` si la cuenta se pagó con más de uno. El
    # detalle está en `order_payments`.
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
    # El pedido que se quedó con los platos al unir dos mesas.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", name="fk_orders_merged_into", ondelete="SET NULL"),
        nullable=True,
    )

    items: Mapped[list[OrderItemRow]] = relationship(
        cascade="all, delete-orphan",
        # Con sesiones asíncronas no hay carga perezosa: los ítems llegan en
        # la misma ida a la base que el pedido.
        lazy="selectin",
        order_by="OrderItemRow.id",
    )
    payments: Mapped[list[PaymentRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="PaymentRow.id"
    )

    __table_args__ = (
        # El árbitro real del correlativo diario. El caso de uso serializa la
        # numeración, pero la base es la última palabra.
        UniqueConstraint("restaurant_id", "business_date", "number", name="uq_orders_daily_number"),
        # Un reintento del celular (sin señal, doble toque) no crea otro pedido.
        UniqueConstraint("restaurant_id", "client_request_id", name="uq_orders_client_request"),
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
    # Las opciones elegidas, congeladas con su precio: [{group, option, price}].
    modifiers: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    is_courtesy: Mapped[bool] = mapped_column(Boolean, default=False)
    courtesy_reason: Mapped[str] = mapped_column(String(200), default="")
    # El pago que lo cubrió cuando la cuenta se dividió por platos.
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_payments.id", name="fk_order_items_payment", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CashSessionRow(Base):
    __tablename__ = "cash_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_cash_sessions_restaurant", ondelete="RESTRICT")
    )
    opened_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_cash_sessions_opened_by", ondelete="RESTRICT")
    )
    opening_amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    opening_notes: Mapped[str] = mapped_column(String(300), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_cash_sessions_closed_by", ondelete="RESTRICT"),
        nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counted_cash: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), nullable=True
    )
    expected_cash: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), nullable=True
    )
    closing_notes: Mapped[str] = mapped_column(String(300), default="")

    __table_args__ = (
        Index("ix_cash_sessions_restaurant_opened", "restaurant_id", "opened_at"),
        # Una sola caja abierta por local. El caso de uso lo revisa antes; el
        # índice parcial es la última palabra si dos encargados abren a la vez.
        Index(
            "uq_cash_sessions_one_open",
            "restaurant_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


class PaymentRow(Base):
    __tablename__ = "order_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_order_payments_restaurant", ondelete="RESTRICT")
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", name="fk_order_payments_order", ondelete="CASCADE"), index=True
    )
    # Nulo solo en los cobros anteriores a la caja, que la migración trajo
    # desde las columnas del pedido.
    cash_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("cash_sessions.id", name="fk_order_payments_cash_session", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    method: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    tip: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2), default=Decimal("0.00"))
    amount_received: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), nullable=True
    )
    received_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_order_payments_received_by", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # El panel suma los cobros de un rango de días del local.
        Index("ix_order_payments_restaurant_created", "restaurant_id", "created_at"),
    )
