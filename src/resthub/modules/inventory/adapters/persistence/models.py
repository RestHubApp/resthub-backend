from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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

from resthub.core.clock import utc_now
from resthub.core.database import Base


class IngredientRow(Base):
    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Claves foráneas por nombre de tabla y no por modelo: importar los modelos
    # de otros módulos rompería la independencia.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_ingredients_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(10))
    min_stock: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(precision=14, scale=6))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # La presentación (botella de 750 ml); todo en NULL si no tiene.
    package_size: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    package_label: Mapped[str] = mapped_column(String(30), default="", server_default="")
    full_weight_g: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    empty_weight_g: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StockMovementRow(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sin índice propio: lo cubre el compuesto de abajo, que empieza por él.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_stock_movements_restaurant", ondelete="RESTRICT")
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_stock_movements_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(precision=14, scale=6), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", name="fk_stock_movements_order", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", name="fk_stock_movements_order_item", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_stock_movements_user", ondelete="RESTRICT")
    )
    # Cómo se ingresó la cantidad ("base", "packages") y el número escrito.
    entry_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    entry_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    # El conteo físico que originó un ajuste.
    stock_count_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_counts.id", name="fk_stock_movements_stock_count", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        # Un ítem de pedido descuenta cada insumo una sola vez. El caso de uso
        # ya lo evita; la base es la última palabra si dos avisos se cruzan.
        # Los movimientos que no son consumos llevan el ítem en NULL, y NULL
        # no choca con NULL en un índice único.
        UniqueConstraint("order_item_id", "ingredient_id", name="uq_stock_movements_consumption"),
        Index("ix_stock_movements_restaurant_moment", "restaurant_id", "created_at"),
    )


class RecipeLineRow(Base):
    __tablename__ = "recipe_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_recipe_lines_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_items.id", name="fk_recipe_lines_menu_item", ondelete="RESTRICT"),
        index=True,
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_recipe_lines_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))

    __table_args__ = (
        UniqueConstraint("menu_item_id", "ingredient_id", name="uq_recipe_lines_ingredient"),
    )


class SupplierRow(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_suppliers_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80))
    contact: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_purchase_orders_restaurant", ondelete="RESTRICT")
    )
    # Correlativo por restaurante: "la OC 12" es como se la nombra.
    number: Mapped[int] = mapped_column(Integer)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", name="fk_purchase_orders_supplier", ondelete="RESTRICT"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_purchase_orders_created_by", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[PurchaseOrderLineRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="PurchaseOrderLineRow.id"
    )

    __table_args__ = (
        UniqueConstraint("restaurant_id", "number", name="uq_purchase_orders_number"),
        Index("ix_purchase_orders_restaurant_status", "restaurant_id", "status"),
    )


class PurchaseOrderLineRow(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey(
            "restaurants.id", name="fk_purchase_order_lines_restaurant", ondelete="RESTRICT"
        ),
        index=True,
    )
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", name="fk_purchase_order_lines_order", ondelete="CASCADE"),
        index=True,
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_purchase_order_lines_ingredient", ondelete="RESTRICT")
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(precision=14, scale=6))
    received_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    received_unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=14, scale=6), nullable=True
    )


OPEN_STOCK_COUNT_INDEX = "uq_stock_counts_one_draft"


class StockCountRow(Base):
    __tablename__ = "stock_counts"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_stock_counts_restaurant", ondelete="RESTRICT")
    )
    # Correlativo por restaurante: «Conteo físico #4».
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))
    scope: Mapped[str] = mapped_column(String(10))
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_stock_counts_created_by", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_stock_counts_closed_by", ondelete="RESTRICT"),
        nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[StockCountLineRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="StockCountLineRow.id"
    )

    __table_args__ = (
        UniqueConstraint("restaurant_id", "number", name="uq_stock_counts_number"),
        # A lo sumo un conteo en curso por local. El caso de uso ya lo
        # comprueba; el índice es la última palabra si dos lo abren a la vez.
        Index(
            OPEN_STOCK_COUNT_INDEX,
            "restaurant_id",
            unique=True,
            sqlite_where=text("status = 'draft'"),
            postgresql_where=text("status = 'draft'"),
        ),
    )


class StockCountLineRow(Base):
    __tablename__ = "stock_count_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_stock_count_lines_restaurant", ondelete="RESTRICT")
    )
    stock_count_id: Mapped[int] = mapped_column(
        ForeignKey("stock_counts.id", name="fk_stock_count_lines_count", ondelete="CASCADE"),
        index=True,
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_stock_count_lines_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    opening_stock: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    expected: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    counted: Mapped[Decimal | None] = mapped_column(Numeric(precision=12, scale=3), nullable=True)
    entry_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    entry_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    entry_full_packages: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    counted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_stock_count_lines_counted_by", ondelete="RESTRICT"),
        nullable=True,
    )
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    difference: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=12, scale=3), nullable=True
    )
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(precision=14, scale=6), nullable=True)

    __table_args__ = (
        UniqueConstraint("stock_count_id", "ingredient_id", name="uq_stock_count_lines_ingredient"),
    )


class WasteApprovalRow(Base):
    __tablename__ = "waste_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_waste_approvals_restaurant", ondelete="RESTRICT")
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", name="fk_waste_approvals_ingredient", ondelete="RESTRICT"),
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(precision=14, scale=6))
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_waste_approvals_created_by", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_waste_approvals_decided_by", ondelete="RESTRICT"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str] = mapped_column(String(200), default="")
    movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_movements.id", name="fk_waste_approvals_movement", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (Index("ix_waste_approvals_restaurant_status", "restaurant_id", "status"),)
