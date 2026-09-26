"""Bitácora de la administración del sistema."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE, PageRequest
from resthub.modules.platform.adapters.api.dependencies import (
    CurrentAdminDep,
    PlatformActivityLogDep,
)
from resthub.modules.platform.adapters.api.schemas import (
    PlatformActivityPageResponse,
    PlatformActivityResponse,
)
from resthub.modules.platform.use_cases.read_activity import ReadPlatformActivity

router = APIRouter()


@router.get("", response_model=PlatformActivityPageResponse, summary="Movimientos de la plataforma")
async def read_activity(
    _: CurrentAdminDep,
    activity: PlatformActivityLogDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> PlatformActivityPageResponse:
    page = await ReadPlatformActivity(activity)(PageRequest(limit=limit, offset=offset))
    return PlatformActivityPageResponse(
        items=[PlatformActivityResponse.from_entry(entry) for entry in page.items],
        total=page.total,
    )
