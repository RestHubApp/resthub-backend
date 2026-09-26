"""El proveedor del local de muestra: no llama a nadie.

Los pedidos del local de muestra son de mentira. Sus comprobantes se emiten y
se numeran como los de cualquier local, pero no salen del servidor aunque
alguien cargue un RUC, una URL y un token en la vista previa.
"""

from __future__ import annotations

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
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult:
        return ProviderResult(
            status=InvoiceStatus.SIMULATED, message=SANDBOX_MESSAGE, raw={"simulated": True}
        )
