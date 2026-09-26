"""El local de muestra y la vista previa.

Todo exige una credencial de plataforma. La vista previa no recibe un
restaurante ni una cuenta: solo el tipo de cuenta de muestra (`as`), así que no
hay forma de pedirla para un local real.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from resthub.core.auth import SessionDep
from resthub.modules.platform.adapters.api.dependencies import (
    CurrentAdminDep,
    PlatformActivityLogDep,
    SandboxCatalogDep,
    SandboxProvisioningDep,
)
from resthub.modules.platform.adapters.api.errors import to_http
from resthub.modules.platform.adapters.api.schemas import (
    PreviewCodeResponse,
    PreviewRequest,
    SandboxResponse,
)
from resthub.modules.platform.domain.exceptions import PlatformError
from resthub.modules.platform.use_cases.sandbox import (
    ReadSandbox,
    ResetSandbox,
    ResetSandboxCommand,
    StartPreview,
    StartPreviewCommand,
)

router = APIRouter()
preview_router = APIRouter()


@router.get("", response_model=SandboxResponse, summary="El local de muestra vigente")
async def read_sandbox(_: CurrentAdminDep, catalog: SandboxCatalogDep) -> SandboxResponse:
    return SandboxResponse.from_view(await ReadSandbox(catalog)())


@router.post(
    "/reset",
    response_model=SandboxResponse,
    summary="Archivar el local de muestra y crear uno nuevo con los datos de muestra",
)
async def reset_sandbox(
    admin: CurrentAdminDep,
    catalog: SandboxCatalogDep,
    provisioning: SandboxProvisioningDep,
    activity: PlatformActivityLogDep,
    session: SessionDep,
) -> SandboxResponse:
    try:
        view = await ResetSandbox(catalog, provisioning, activity)(
            ResetSandboxCommand(admin_id=admin.id or 0)
        )
    except PlatformError as error:
        raise to_http(error) from error
    # Antes de responder: el botón siguiente («Ver como…») tiene que encontrar
    # el local nuevo, y la sesión de la petición confirma recién después.
    await session.commit()
    return SandboxResponse.from_view(view)


@preview_router.post(
    "",
    response_model=PreviewCodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Código de un solo uso para ver la aplicación como encargado o mesero de muestra",
)
async def start_preview(
    payload: PreviewRequest,
    admin: CurrentAdminDep,
    catalog: SandboxCatalogDep,
    provisioning: SandboxProvisioningDep,
    activity: PlatformActivityLogDep,
    session: SessionDep,
) -> PreviewCodeResponse:
    try:
        code = await StartPreview(catalog, provisioning, activity)(
            StartPreviewCommand(admin_id=admin.id or 0, as_=payload.as_)
        )
    except PlatformError as error:
        raise to_http(error) from error
    # Confirmado antes de responder: la pestaña nueva lo canjea al instante, y
    # la sesión de la petición confirma recién cuando la respuesta ya salió.
    await session.commit()
    return PreviewCodeResponse.from_code(code)
