"""Adaptadores de lectores hacia tablas ajenas (`restaurants`).

Consulta cruda, acotada a la pregunta del puerto y cubierta por pruebas. Se
lee, nunca se escribe: el dueño de la tabla es el módulo `restaurants`.
"""

from __future__ import annotations

from sqlalchemy import Boolean, text
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.modules.accounts.ports.restaurant_directory import RestaurantSummary

_RESTAURANT_QUERY = text(
    "SELECT id, name, slug, timezone, is_active, is_sandbox FROM restaurants "
    "WHERE id = :restaurant_id"
).columns(is_active=Boolean(), is_sandbox=Boolean())


class SqlRestaurantDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, restaurant_id: int) -> RestaurantSummary | None:
        row = (
            await self._session.execute(_RESTAURANT_QUERY, {"restaurant_id": restaurant_id})
        ).one_or_none()
        if row is None:
            return None
        return RestaurantSummary(
            id=int(row.id),
            name=str(row.name),
            slug=str(row.slug),
            timezone=str(row.timezone),
            is_active=bool(row.is_active),
            is_sandbox=bool(row.is_sandbox),
        )
