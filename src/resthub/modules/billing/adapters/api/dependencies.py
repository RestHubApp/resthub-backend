"""Cableado del adaptador HTTP de facturación."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.auth import PrincipalDep
from resthub.core.database import SessionDep
from resthub.modules.billing.adapters.persistence.repositories import (
    SqlAlchemyBillingSettings,
    SqlAlchemyInvoiceRepository,
    SqlPaidOrderDirectory,
    SqlSandboxDirectory,
)
from resthub.modules.billing.adapters.sunat.nubefact import NubefactInvoicer
from resthub.modules.billing.adapters.sunat.sandbox import SandboxInvoicer
from resthub.modules.billing.ports.billing_ports import (
    BillingSettingsRepository,
    ElectronicInvoicer,
    InvoiceRepository,
    PaidOrderDirectory,
)


def get_billing_settings(session: SessionDep) -> BillingSettingsRepository:
    return SqlAlchemyBillingSettings(session)


def get_invoice_repository(session: SessionDep) -> InvoiceRepository:
    return SqlAlchemyInvoiceRepository(session)


def get_paid_order_directory(session: SessionDep) -> PaidOrderDirectory:
    return SqlPaidOrderDirectory(session)


def get_invoicer() -> ElectronicInvoicer:
    """El proveedor de comprobantes; las pruebas lo reemplazan por uno falso."""
    return NubefactInvoicer()


async def get_restaurant_invoicer(
    principal: PrincipalDep,
    session: SessionDep,
    provider: Annotated[ElectronicInvoicer, Depends(get_invoicer)],
) -> ElectronicInvoicer:
    """El proveedor que le toca al local de quien pide.

    El local de muestra nunca llama al proveedor real, aunque tenga RUC, URL y
    token cargados: su proveedor simula el envío.
    """
    if await SqlSandboxDirectory(session).is_sandbox(principal.restaurant_id):
        return SandboxInvoicer()
    return provider


BillingSettingsDep = Annotated[BillingSettingsRepository, Depends(get_billing_settings)]
InvoiceRepositoryDep = Annotated[InvoiceRepository, Depends(get_invoice_repository)]
PaidOrderDirectoryDep = Annotated[PaidOrderDirectory, Depends(get_paid_order_directory)]
InvoicerDep = Annotated[ElectronicInvoicer, Depends(get_restaurant_invoicer)]
