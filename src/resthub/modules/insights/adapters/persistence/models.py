from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from resthub.core.database import Base


class AiDecisionRow(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Sin índice propio: lo cubren los compuestos de abajo, que empiezan por él.
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", name="fk_ai_decisions_restaurant", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(20))
    # Sin clave foránea a propósito: el asunto puede ser un insumo, un pedido,
    # un plato de un pedido o un movimiento de stock, según `subject_type`. Son
    # tablas de otros módulos y ninguna se borra, así que no queda huérfano.
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[int] = mapped_column(Integer)
    # JSON y no texto: en PostgreSQL se puede consultar por dentro si algún
    # día la auditoría lo pide.
    input_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any]] = mapped_column(JSON)
    engine: Mapped[str] = mapped_column(String(10))
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # "La última decisión de cada insumo" y "las notas de este plato".
        Index("ix_ai_decisions_subject", "restaurant_id", "kind", "subject_type", "subject_id"),
        # La auditoría, de la más nueva a la más vieja.
        Index("ix_ai_decisions_restaurant_moment", "restaurant_id", "created_at"),
    )
