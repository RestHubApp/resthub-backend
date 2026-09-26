"""Lectores hacia tablas ajenas: `restaurants`, `users` y `roles`.

Consultas crudas, acotadas a lo que pide el puerto y cubiertas por pruebas. Se
lee, nunca se escribe: esas tablas las poseen `restaurants` y `accounts`, y lo
que la plataforma cambia en ellas pasa por sus casos de uso
(`wiring/restaurant_provisioning.py`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, DateTime, Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from resthub.core.pagination import Page, PageRequest
from resthub.core.permissions import RoleKind
from resthub.core.timestamps import as_utc
from resthub.modules.platform.ports.restaurants import OwnerAccount, RestaurantSummary

_SUMMARY_SELECT = (
    "SELECT r.id, r.name, r.slug, r.timezone, r.is_active, r.created_at, "
    "(SELECT COUNT(*) FROM users u WHERE u.restaurant_id = r.id) AS staff_count, "
    "(SELECT COUNT(*) FROM users u WHERE u.restaurant_id = r.id AND u.is_active = :active) "
    "AS active_staff_count FROM restaurants r"
)
# `ESCAPE` para que un `%` o un `_` en lo que se busca se busquen tal cual.
_SEARCH_FILTER = (
    " WHERE lower(r.name) LIKE :pattern ESCAPE '\\' OR lower(r.slug) LIKE :pattern ESCAPE '\\'"
)
_SUMMARY_TYPES = {"is_active": Boolean(), "created_at": DateTime(timezone=True)}

# Quién es encargado sale del `kind` de su rol: es la definición del contrato
# ("cuentas cuyo rol es el Encargado del local"), no una decisión de permisos.
_OWNERS_QUERY = text(
    "SELECT u.id, u.full_name, u.email, u.is_active FROM users u "
    "JOIN roles ro ON ro.id = u.role_id "
    "WHERE u.restaurant_id = :restaurant_id AND ro.kind = :owner_kind "
    "ORDER BY u.full_name, u.id"
).columns(is_active=Boolean())


def _like_pattern(raw: str) -> str:
    escaped = raw.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _to_summary(row: Row[Any]) -> RestaurantSummary:
    return RestaurantSummary(
        id=int(row.id),
        name=str(row.name),
        slug=str(row.slug),
        timezone=str(row.timezone),
        is_active=bool(row.is_active),
        created_at=as_utc(row.created_at),
        staff_count=int(row.staff_count),
        active_staff_count=int(row.active_staff_count),
    )


class SqlRestaurantCatalog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(self, search_text: str | None, page: PageRequest) -> Page[RestaurantSummary]:
        where = _SEARCH_FILTER if search_text else ""
        params: dict[str, object] = {"active": True}
        if search_text:
            params["pattern"] = _like_pattern(search_text)

        total = (
            await self._session.execute(text(f"SELECT COUNT(*) FROM restaurants r{where}"), params)
        ).scalar_one()
        rows = await self._session.execute(
            text(
                f"{_SUMMARY_SELECT}{where} ORDER BY r.created_at DESC, r.id DESC "
                "LIMIT :limit OFFSET :offset"
            ).columns(**_SUMMARY_TYPES),
            {**params, "limit": page.limit, "offset": page.offset},
        )
        return Page(items=[_to_summary(row) for row in rows.all()], total=int(total))

    async def get(self, restaurant_id: int) -> RestaurantSummary | None:
        row = (
            await self._session.execute(
                text(f"{_SUMMARY_SELECT} WHERE r.id = :restaurant_id").columns(**_SUMMARY_TYPES),
                {"active": True, "restaurant_id": restaurant_id},
            )
        ).one_or_none()
        return _to_summary(row) if row is not None else None

    async def owners(self, restaurant_id: int) -> list[OwnerAccount]:
        rows = await self._session.execute(
            _OWNERS_QUERY, {"restaurant_id": restaurant_id, "owner_kind": RoleKind.OWNER.value}
        )
        return [
            OwnerAccount(
                id=int(row.id),
                full_name=str(row.full_name),
                email=str(row.email),
                is_active=bool(row.is_active),
            )
            for row in rows.all()
        ]
