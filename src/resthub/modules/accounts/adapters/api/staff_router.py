"""Adaptador de entrada HTTP para la gestión del personal.

Todo exige `staff.manage`, y todo se acota al restaurante del principal: una
cuenta de otro local responde 404, igual que una que no existe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal, Role
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.accounts.adapters.api.dependencies import (
    PasswordHasherDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.schemas import (
    ChangeStaffStatusRequest,
    RegisterStaffRequest,
    ResetStaffPasswordRequest,
    StaffMemberResponse,
    StaffPageResponse,
    UpdateStaffRequest,
)
from resthub.modules.accounts.domain.exceptions import (
    CannotChangeOwnRole,
    CannotDeactivateSelf,
    CannotResetOwnPassword,
    EmailAlreadyRegistered,
    InvalidEmail,
    InvalidFullName,
    UserNotFound,
    WeakPassword,
)
from resthub.modules.accounts.use_cases.manage_staff import (
    ChangeStaffStatus,
    ChangeStaffStatusCommand,
    ListStaff,
    ListStaffQuery,
    ReadStaffMember,
    RegisterStaff,
    RegisterStaffCommand,
    ResetStaffPassword,
    ResetStaffPasswordCommand,
    UpdateStaff,
    UpdateStaffCommand,
)

router = APIRouter()

StaffManagerDep = Annotated[Principal, Depends(require_permission(Permission.STAFF_MANAGE))]


def _not_found(error: UserNotFound) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(error))


@router.get("", response_model=StaffPageResponse, summary="Listar el personal")
async def list_staff(
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    role: Annotated[list[Role] | None, Query(description="Filtra por rol")] = None,
    search: Annotated[str | None, Query(description="Busca en nombre y correo")] = None,
    is_active: Annotated[bool | None, Query(description="Filtra por estado")] = None,
    ordering: Annotated[str | None, Query(description="Columna, '-' invierte")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StaffPageResponse:
    page = await ListStaff(users)(
        ListStaffQuery(
            restaurant_id=principal.restaurant_id,
            roles=frozenset(role) if role else None,
            search=search,
            is_active=is_active,
            ordering=ordering,
            limit=limit,
            offset=offset,
        )
    )
    return StaffPageResponse(
        items=[StaffMemberResponse.from_entity(user) for user in page.items], total=page.total
    )


@router.post(
    "",
    response_model=StaffMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta a un mesero o a otro encargado",
)
async def register_staff(
    payload: RegisterStaffRequest,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
) -> StaffMemberResponse:
    try:
        user = await RegisterStaff(users, hasher, activity)(
            RegisterStaffCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                email=str(payload.email),
                full_name=payload.full_name,
                role=payload.role,
                password=payload.password,
            )
        )
    except EmailAlreadyRegistered as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except (InvalidEmail, InvalidFullName, WeakPassword) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return StaffMemberResponse.from_entity(user)


@router.get("/{user_id}", response_model=StaffMemberResponse, summary="Ver a un miembro")
async def read_staff_member(
    user_id: int, principal: StaffManagerDep, users: UserRepositoryDep
) -> StaffMemberResponse:
    try:
        user = await ReadStaffMember(users)(principal.restaurant_id, user_id)
    except UserNotFound as error:
        raise _not_found(error) from error
    return StaffMemberResponse.from_entity(user)


@router.patch("/{user_id}", response_model=StaffMemberResponse, summary="Editar nombre o rol")
async def update_staff(
    user_id: int,
    payload: UpdateStaffRequest,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> StaffMemberResponse:
    try:
        user = await UpdateStaff(users, activity, events)(
            UpdateStaffCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                user_id=user_id,
                full_name=payload.full_name,
                role=payload.role,
            )
        )
    except UserNotFound as error:
        raise _not_found(error) from error
    except CannotChangeOwnRole as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except InvalidFullName as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return StaffMemberResponse.from_entity(user)


@router.patch(
    "/{user_id}/status",
    response_model=StaffMemberResponse,
    summary="Activar o desactivar una cuenta",
)
async def change_staff_status(
    user_id: int,
    payload: ChangeStaffStatusRequest,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> StaffMemberResponse:
    try:
        user = await ChangeStaffStatus(users, activity, events)(
            ChangeStaffStatusCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                user_id=user_id,
                is_active=payload.is_active,
            )
        )
    except UserNotFound as error:
        raise _not_found(error) from error
    except CannotDeactivateSelf as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return StaffMemberResponse.from_entity(user)


@router.post(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Restablecer la contraseña de una cuenta",
)
async def reset_staff_password(
    user_id: int,
    payload: ResetStaffPasswordRequest,
    principal: StaffManagerDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
) -> Response:
    try:
        await ResetStaffPassword(users, hasher, activity)(
            ResetStaffPasswordCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                user_id=user_id,
                new_password=payload.new_password,
            )
        )
    except UserNotFound as error:
        raise _not_found(error) from error
    except CannotResetOwnPassword as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except WeakPassword as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
