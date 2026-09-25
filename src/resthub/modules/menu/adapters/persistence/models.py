from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


class MenuCategoryRow(Base):
    __tablename__ = "menu_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Clave foránea por nombre de tabla y no por el modelo de `restaurants`:
    # importarlo rompería la independencia entre módulos.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_menu_categories_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(60))
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class MenuItemRow(Base):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_menu_items_restaurant", ondelete="RESTRICT"),
        index=True,
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("menu_categories.id", name="fk_menu_items_category", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(300), default="")
    # Numeric y no Float: un precio en coma flotante termina cobrando
    # 12.499999 soles.
    price: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
