"""El proveedor del local de muestra: no llama a nadie.

Los pedidos del local de muestra son de mentira. Sus comprobantes se emiten y
se numeran como los de cualquier local, pero no salen del servidor aunque
alguien cargue un RUC, un usuario SOL y un certificado en la vista previa: ni
siquiera se registran en el servicio de facturación.
"""

from __future__ import annotations

from resthub.modules.billing.domain.exceptions import BillingProviderError
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import ProviderResult

SANDBOX_MESSAGE = "Local de muestra: simulado, no se envió a SUNAT."


class SandboxInvoicer:
    async def send(
        self,
        settings: BillingSettings,  # noqa: ARG002 - firma del puerto; este proveedor solo simula
        invoice: Invoice,  # noqa: ARG002
        kind: InvoiceKind,  # noqa: ARG002
    ) -> ProviderResult:
        return ProviderResult(
            status=InvoiceStatus.SIMULATED, message=SANDBOX_MESSAGE, raw={"simulated": True}
        )

    async def render_pdf(self, invoice: Invoice) -> bytes:
        message = f"El comprobante {invoice.code} es de muestra: no tiene PDF."
        raise BillingProviderError(message)

    async def register(self, settings: BillingSettings) -> None:
        """Las credenciales del local de muestra no salen del servidor."""

    async def unregister(self, ruc: str) -> None:
        """Nada que olvidar: nunca se registró."""
