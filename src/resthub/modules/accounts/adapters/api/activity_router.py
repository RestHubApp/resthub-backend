"""Adaptador de entrada HTTP para la bitácora del restaurante."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from resthub.core.activity import ActivityGroup, ActivityKind
from resthub.core.activity_log import ActivityReaderDep
from resthub.core.api_params import MAX_ID
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.api.dependencies import UserRepositoryDep
from resthub.modules.accounts.adapters.api.schemas import ActivityPageResponse, ActivityResponse
from resthub.modules.accounts.use_cases.read_activity import ReadActivity, ReadActivityQuery

router = APIRouter()

ActivityViewerDep = Annotated[Principal, Depends(require_permission(Permission.ACTIVITY_READ))]


@router.get("", response_model=ActivityPageResponse, summary="Movimientos de las cuentas")
async def read_activity(
    principal: ActivityViewerDep,
    activity: ActivityReaderDep,
    users: UserRepositoryDep,
    role_id: Annotated[
        list[Annotated[int, Field(ge=1, le=MAX_ID)]] | None, Query(description="Filtra por rol")
    ] = None,
    user_id: Annotated[
        int | None,
        Query(
            ge=1, le=MAX_ID, description="Filtra por persona: solo los movimientos de esa cuenta"
        ),
    ] = None,
    kind: Annotated[list[ActivityKind] | None, Query(description="Filtra por acción")] = None,
    group: Annotated[
        list[ActivityGroup] | None,
        Query(
            description=(
                "Filtra por familia de acciones: `sessions` (entradas y salidas), `orders`, "
                "`cash`, `customers_access`, `inventory`, `menu`, `staff`. Se suma a `kind`: "
                "devuelve las acciones de cualquiera de los grupos y tipos pedidos."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> ActivityPageResponse:
    # El rol es un filtro y no una exclusión: una bitácora que ocultara al
    # encargado, el actor con más poder, no serviría para auditar.
    page = await ReadActivity(activity, users)(
        ReadActivityQuery(
            restaurant_id=principal.restaurant_id,
            role_ids=frozenset(role_id) if role_id else None,
            user_id=user_id,
            kinds=_requested_kinds(kind, group),
            limit=limit,
            offset=offset,
        )
    )
    return ActivityPageResponse(
        items=[
            ActivityResponse(
                id=entry.record.id or 0,
                kind=entry.record.kind.value,
                kind_label=entry.record.kind.label,
                detail=entry.record.detail,
                occurred_at=entry.record.occurred_at,
                user_id=entry.user.id or 0,
                user_name=entry.user.full_name,
                user_role_id=entry.user.role.id or 0,
                user_role_label=entry.user.role.name,
            )
            for entry in page.items
        ],
        total=page.total,
    )


def _requested_kinds(
    kinds: list[ActivityKind] | None, groups: list[ActivityGroup] | None
) -> frozenset[ActivityKind] | None:
    """Une los tipos sueltos con los de cada grupo; sin ninguno, no se filtra."""
    if not kinds and not groups:
        return None
    selected = set(kinds or [])
    for group in groups or []:
        selected |= group.kinds
    return frozenset(selected)
