"""Adaptador HTTP de las sesiones del personal (RFS-17).

El encargado ve en qué dispositivos tiene la sesión abierta cada persona del
local y cierra la de un celular perdido o la de alguien que ya no trabaja ahí.
Exige `staff.manage`; una cuenta de otro local responde 404 y una cuenta con
permisos que quien pregunta no tiene, 403, como el resto de la gestión del personal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from resthub.core.access_history import STAFF_ACCOUNT
from resthub.core.account_sessions import SessionRegistryDep
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.api_params import PathId
from resthub.core.auth import require_permission
from resthub.core.database import SessionDep
from resthub.core.errors import api_error
from resthub.core.identity import Principal
from resthub.core.logs import get_logger
from resthub.core.permissions import Permission
from resthub.modules.accounts.adapters.api.dependencies import UserRepositoryDep
from resthub.modules.accounts.adapters.api.schemas import DeviceSessionResponse
from resthub.modules.accounts.adapters.api.security_router import SessionRef, session_not_found
from resthub.modules.accounts.domain.entities import User
from resthub.modules.accounts.domain.exceptions import (
    CannotManageStrongerAccountError,
    UserNotFoundError,
)
from resthub.modules.accounts.use_cases.manage_staff import ReadManageableStaffMember

router = APIRouter()
logger = get_logger("resthub.staff.sessions")

StaffManagerDep = Annotated[Principal, Depends(require_permission(Permission.STAFF_MANAGE))]


async def _member(principal: Principal, users: UserRepositoryDep, user_id: int) -> User:
    try:
        return await ReadManageableStaffMember(users)(
            principal.restaurant_id, principal.permissions, user_id
        )
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    except CannotManageStrongerAccountError as error:
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error


@router.get(
    "/{user_id}/sessions",
    response_model=list[DeviceSessionResponse],
    summary="Las sesiones abiertas de un miembro",
)
async def list_staff_sessions(
    user_id: PathId,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    sessions: SessionRegistryDep,
    db: SessionDep,
) -> list[DeviceSessionResponse]:
    member = await _member(principal, users, user_id)
    opened = await sessions.list_open(db, STAFF_ACCOUNT, member.id or 0)
    return [
        DeviceSessionResponse.from_session(state, current=state.id == principal.session_id)
        for state in opened
    ]


@router.delete(
    "/{user_id}/sessions/{session_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar una sesión de un miembro",
)
async def close_staff_session(
    user_id: PathId,
    session_ref: SessionRef,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    db: SessionDep,
) -> None:
    member = await _member(principal, users, user_id)
    target = await sessions.find_open_by_ref(db, STAFF_ACCOUNT, member.id or 0, session_ref)
    if target is None:
        raise session_not_found()
    await sessions.revoke(db, target.id)
    await activity.record(
        principal.restaurant_id,
        principal.user_id,
        ActivityKind.STAFF_SESSION_CLOSED,
        f"{member.full_name} ({target.device})",
    )
    logger.info("staff.session_closed", user_id=member.id, actor_id=principal.user_id)


@router.delete(
    "/{user_id}/sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar todas las sesiones de un miembro",
)
async def close_all_staff_sessions(
    user_id: PathId,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    sessions: SessionRegistryDep,
    activity: ActivityRecorderDep,
    db: SessionDep,
) -> None:
    """Cierra todas, también la propia si alguien se gestiona a sí mismo por esta ruta."""
    member = await _member(principal, users, user_id)
    await sessions.revoke_all(db, STAFF_ACCOUNT, member.id or 0)
    await activity.record(
        principal.restaurant_id,
        principal.user_id,
        ActivityKind.STAFF_SESSION_CLOSED,
        f"{member.full_name} (todas)",
    )
    logger.info("staff.sessions_closed", user_id=member.id, actor_id=principal.user_id)
