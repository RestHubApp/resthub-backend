"""Adaptador de entrada HTTP para los roles del restaurante y el catálogo de permisos.

Ver los roles alcanza con `staff.manage` o `roles.manage`: quien da de alta
personal necesita elegir su rol. Crearlos, editarlos y borrarlos exige
`roles.manage`. Un rol de otro local responde 404, igual que uno que no existe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import require_permission
from resthub.core.identity import Principal
from resthub.core.permissions import Permission
from resthub.core.realtime_broker import EventPublisherDep
from resthub.modules.accounts.adapters.api.dependencies import RoleRepositoryDep
from resthub.modules.accounts.adapters.api.schemas import (
    PermissionResponse,
    RoleRequest,
    RoleResponse,
)
from resthub.modules.accounts.domain.exceptions import (
    AccountsError,
    BaseRoleNameFixed,
    BaseRoleNotDeletable,
    CannotGrantPermissions,
    CannotManageStrongerRole,
    InvalidRoleName,
    RoleInUse,
    RoleNameTaken,
    RoleNotEditable,
    RoleNotFound,
)
from resthub.modules.accounts.use_cases.manage_roles import (
    CreateRole,
    CreateRoleCommand,
    DeleteRole,
    DeleteRoleCommand,
    ListRoles,
    UpdateRole,
    UpdateRoleCommand,
)

router = APIRouter()
permissions_router = APIRouter()

RoleManagerDep = Annotated[Principal, Depends(require_permission(Permission.ROLES_MANAGE))]
RoleViewerDep = Annotated[
    Principal, Depends(require_permission(Permission.STAFF_MANAGE, Permission.ROLES_MANAGE))
]

_STATUS_BY_ERROR: tuple[tuple[type[AccountsError], int], ...] = (
    (RoleNotFound, status.HTTP_404_NOT_FOUND),
    (InvalidRoleName, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (CannotGrantPermissions, status.HTTP_403_FORBIDDEN),
    (CannotManageStrongerRole, status.HTTP_403_FORBIDDEN),
    (RoleNameTaken, status.HTTP_409_CONFLICT),
    (RoleNotEditable, status.HTTP_409_CONFLICT),
    (BaseRoleNameFixed, status.HTTP_409_CONFLICT),
    (BaseRoleNotDeletable, status.HTTP_409_CONFLICT),
    (RoleInUse, status.HTTP_409_CONFLICT),
)
_ROLE_ERRORS = tuple(error for error, _ in _STATUS_BY_ERROR)


def _to_http(error: AccountsError) -> HTTPException:
    code = next(code for kind, code in _STATUS_BY_ERROR if isinstance(error, kind))
    return HTTPException(code, str(error))


@permissions_router.get("", response_model=list[PermissionResponse], summary="Catálogo de permisos")
async def list_permissions(_: RoleManagerDep) -> list[PermissionResponse]:
    return PermissionResponse.catalog()


@router.get("", response_model=list[RoleResponse], summary="Listar los roles del restaurante")
async def list_roles(principal: RoleViewerDep, roles: RoleRepositoryDep) -> list[RoleResponse]:
    views = await ListRoles(roles)(principal.restaurant_id)
    return [RoleResponse.from_view(view) for view in views]


@router.post(
    "",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un rol",
)
async def create_role(
    payload: RoleRequest,
    principal: RoleManagerDep,
    roles: RoleRepositoryDep,
    activity: ActivityRecorderDep,
) -> RoleResponse:
    try:
        view = await CreateRole(roles, activity)(
            CreateRoleCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                actor_permissions=principal.permissions,
                name=payload.name,
                permissions=frozenset(payload.permissions),
            )
        )
    except _ROLE_ERRORS as error:
        raise _to_http(error) from error
    return RoleResponse.from_view(view)


@router.put("/{role_id}", response_model=RoleResponse, summary="Editar nombre y permisos")
async def update_role(
    role_id: int,
    payload: RoleRequest,
    principal: RoleManagerDep,
    roles: RoleRepositoryDep,
    activity: ActivityRecorderDep,
    events: EventPublisherDep,
) -> RoleResponse:
    try:
        view = await UpdateRole(roles, activity, events)(
            UpdateRoleCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                actor_permissions=principal.permissions,
                role_id=role_id,
                name=payload.name,
                permissions=frozenset(payload.permissions),
            )
        )
    except _ROLE_ERRORS as error:
        raise _to_http(error) from error
    return RoleResponse.from_view(view)


@router.delete(
    "/{role_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un rol sin personal"
)
async def delete_role(
    role_id: int,
    principal: RoleManagerDep,
    roles: RoleRepositoryDep,
    activity: ActivityRecorderDep,
) -> Response:
    try:
        await DeleteRole(roles, activity)(
            DeleteRoleCommand(
                restaurant_id=principal.restaurant_id,
                actor_id=principal.user_id,
                role_id=role_id,
            )
        )
    except _ROLE_ERRORS as error:
        raise _to_http(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
