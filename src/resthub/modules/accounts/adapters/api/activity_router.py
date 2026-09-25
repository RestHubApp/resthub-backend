"""Adaptador de entrada HTTP para la bitácora del restaurante."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityReaderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal, Role
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
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
    role: Annotated[list[Role] | None, Query(description="Filtra por rol")] = None,
    kind: Annotated[list[ActivityKind] | None, Query(description="Filtra por acción")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActivityPageResponse:
    # El rol es un filtro y no una exclusión: una bitácora que ocultara al
    # encargado, el actor con más poder, no serviría para auditar.
    page = await ReadActivity(activity, users)(
        ReadActivityQuery(
            restaurant_id=principal.restaurant_id,
            roles=frozenset(role) if role else None,
            kinds=frozenset(kind) if kind else None,
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
                user_role=entry.user.role,
                user_role_label=entry.user.role.label,
            )
            for entry in page.items
        ],
        total=page.total,
    )
