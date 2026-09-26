"""El local de muestra y la vista previa, vistos desde la administración del sistema.

La vista previa abre la aplicación como un encargado o un mesero para depurar,
sin tocar ningún restaurante real: solo entra al local de muestra, que se usa de
verdad (pedidos, cobros) y se reinicia cuando hace falta. No hay forma de pedir
un código para una cuenta cualquiera: el caso de uso elige la cuenta de muestra
del tipo pedido, y quien emite el código vuelve a comprobar que lo sea.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.core.permissions import RoleKind
from resthub.modules.platform.domain.entities import PlatformActivityKind
from resthub.modules.platform.domain.exceptions import SandboxAccountUnavailable
from resthub.modules.platform.ports.activity_log import PlatformActivityLog
from resthub.modules.platform.ports.restaurants import RestaurantSummary
from resthub.modules.platform.ports.sandbox import (
    IssuedPreviewCode,
    PreviewAs,
    SandboxAccount,
    SandboxCatalog,
    SandboxProvisioning,
)

_KIND_OF = {PreviewAs.OWNER: RoleKind.OWNER, PreviewAs.WAITER: RoleKind.WAITER}


@dataclass(frozen=True, slots=True)
class SandboxView:
    restaurant: RestaurantSummary | None
    accounts: list[SandboxAccount]


async def _view(catalog: SandboxCatalog) -> SandboxView:
    restaurant = await catalog.current()
    if restaurant is None:
        return SandboxView(restaurant=None, accounts=[])
    return SandboxView(restaurant=restaurant, accounts=await catalog.accounts(restaurant.id))


class ReadSandbox:
    def __init__(self, catalog: SandboxCatalog) -> None:
        self._catalog = catalog

    async def __call__(self) -> SandboxView:
        return await _view(self._catalog)


@dataclass(frozen=True, slots=True)
class ResetSandboxCommand:
    admin_id: int


class ResetSandbox:
    """Deja un local de muestra nuevo, con los datos de muestra intactos.

    El vigente no se borra: queda desactivado como histórico, con otro
    identificador corto, y con él todo lo que se hizo en la vista previa. Corre
    en una sola transacción: si crear el nuevo falla, el anterior sigue vigente.
    """

    def __init__(
        self,
        catalog: SandboxCatalog,
        provisioning: SandboxProvisioning,
        activity: PlatformActivityLog,
    ) -> None:
        self._catalog = catalog
        self._provisioning = provisioning
        self._activity = activity

    async def __call__(self, command: ResetSandboxCommand) -> SandboxView:
        previous = await self._catalog.current()
        if previous is not None:
            await self._provisioning.archive(previous.id)
        restaurant_id = await self._provisioning.create()
        view = await _view(self._catalog)

        detail = f"local de muestra #{restaurant_id}"
        if previous is not None:
            detail += f"; el #{previous.id} quedó archivado"
        await self._activity.record(command.admin_id, PlatformActivityKind.SANDBOX_RESET, detail)
        return view


@dataclass(frozen=True, slots=True)
class StartPreviewCommand:
    admin_id: int
    as_: PreviewAs


class StartPreview:
    """Un código de un solo uso para abrir la vista previa como encargado o mesero.

    Si todavía no hay local de muestra, lo crea.
    """

    def __init__(
        self,
        catalog: SandboxCatalog,
        provisioning: SandboxProvisioning,
        activity: PlatformActivityLog,
    ) -> None:
        self._catalog = catalog
        self._provisioning = provisioning
        self._activity = activity

    async def __call__(self, command: StartPreviewCommand) -> IssuedPreviewCode:
        restaurant = await self._catalog.current()
        created = restaurant is None
        if restaurant is None:
            await self._provisioning.create()
            restaurant = await self._catalog.current()
        if restaurant is None:
            # `create` acaba de dejar uno vigente en esta misma transacción.
            raise SandboxAccountUnavailable(command.as_.value)

        kind = _KIND_OF[command.as_]
        account = next(
            (item for item in await self._catalog.accounts(restaurant.id) if item.kind is kind),
            None,
        )
        if account is None:
            raise SandboxAccountUnavailable(command.as_.value)

        code = await self._provisioning.issue_preview_code(account.user_id, command.admin_id)
        detail = (
            f"como {account.role_label} ({account.full_name}), local de muestra #{restaurant.id}"
        )
        if created:
            detail += ", recién creado"
        await self._activity.record(command.admin_id, PlatformActivityKind.PREVIEW_STARTED, detail)
        return code
