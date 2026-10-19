"""Comprobante aceptado → respaldo de sus documentos fuera de la petición.

`billing` declara el puerto `InvoiceArchiveHook` y el caso de uso
`ArchiveInvoiceDocuments`; este adaptador los une con el sistema de tareas en
segundo plano (`core/background.py`) y `main.py` lo instala en lugar del que no
hace nada.

1. Al avisar, solo anota qué comprobante hay que respaldar.
2. Cuando la transacción que lo guardó se confirma, lanza la tarea. Si se
   deshace, no lanza nada.
3. La tarea abre su propia sesión, toma el XML y el CDR guardados, pide el PDF al
   servicio de facturación, los sube y deja el
   estado (hecho o fallido) en el comprobante.

No es una cola: si el proceso se reinicia con el respaldo a medias, el
comprobante queda «pendiente» y el encargado lo reintenta
(`POST /billing/invoices/{id}/archive`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from resthub.core.background import BackgroundJobs, get_background_jobs
from resthub.core.clock import Clock
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep, get_session_factory
from resthub.core.logs import get_logger
from resthub.modules.billing.adapters.api.dependencies import (
    get_document_archive,
    get_invoicer,
)
from resthub.modules.billing.adapters.persistence.sqlalchemy_repositories import (
    SqlAlchemyBillingSettings,
    SqlAlchemyInvoiceRepository,
)
from resthub.modules.billing.ports.billing_ports import ElectronicInvoiceGateway
from resthub.modules.billing.ports.delivery_ports import (
    DocumentArchiveGateway,
    InvoiceArchiveHook,
)
from resthub.modules.billing.use_cases.archiving import ArchiveInvoiceDocuments

logger = get_logger("resthub.billing.archive")

SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
BackgroundJobsDep = Annotated[BackgroundJobs, Depends(get_background_jobs)]
ArchiveDep = Annotated[DocumentArchiveGateway, Depends(get_document_archive)]
InvoicerDep = Annotated[ElectronicInvoiceGateway, Depends(get_invoicer)]


async def archive_in_background(
    *,
    factory: async_sessionmaker[AsyncSession],
    invoicer: ElectronicInvoiceGateway,
    archive: DocumentArchiveGateway,
    clock: Clock,
    restaurant_id: int,
    invoice_id: int,
) -> None:
    async with factory() as session:
        invoice = await ArchiveInvoiceDocuments(
            invoices=SqlAlchemyInvoiceRepository(session),
            settings=SqlAlchemyBillingSettings(session),
            invoicer=invoicer,
            archive=archive,
            clock=clock,
        )(restaurant_id, invoice_id)
        await session.commit()
    if invoice is not None:
        # Sin el RUC, el cliente ni el monto: solo identificadores y el estado.
        logger.info(
            "billing.archive_finished",
            restaurant_id=restaurant_id,
            invoice_id=invoice_id,
            archive_status=invoice.archive_status.value,
        )


class InvoiceArchiveLauncher:
    """Implementa el puerto de `billing` con una tarea en segundo plano."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        jobs: BackgroundJobs,
        invoicer: ElectronicInvoiceGateway,
        archive: DocumentArchiveGateway,
        clock: Clock,
    ) -> None:
        self._session = session
        self._factory = factory
        self._jobs = jobs
        self._invoicer = invoicer
        self._archive = archive
        self._clock = clock

    def is_enabled(self) -> bool:
        return self._archive.is_enabled()

    def schedule(self, restaurant_id: int, invoice_id: int) -> None:
        def launch(_: Session) -> None:
            self._jobs.spawn(
                f"respaldo-comprobante-{invoice_id}",
                lambda: archive_in_background(
                    factory=self._factory,
                    invoicer=self._invoicer,
                    archive=self._archive,
                    clock=self._clock,
                    restaurant_id=restaurant_id,
                    invoice_id=invoice_id,
                ),
            )

        # `once`: la sesión es de esta petición y se confirma una sola vez.
        # Si se deshace, el oyente nunca se dispara y se va con la sesión.
        sqlalchemy_event.listen(self._session.sync_session, "after_commit", launch, once=True)


def get_invoice_archive_launcher(
    session: SessionDep,
    factory: SessionFactoryDep,
    jobs: BackgroundJobsDep,
    invoicer: InvoicerDep,
    archive: ArchiveDep,
    clock: ClockDep,
) -> InvoiceArchiveHook:
    return InvoiceArchiveLauncher(
        session=session,
        factory=factory,
        jobs=jobs,
        invoicer=invoicer,
        archive=archive,
        clock=clock,
    )
