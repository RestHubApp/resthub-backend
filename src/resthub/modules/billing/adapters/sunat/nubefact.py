"""Envío de comprobantes a SUNAT a través de Nubefact (proveedor autorizado).

Nubefact recibe un JSON por comprobante en la ruta y con el token que le da a
cada empresa (`provider_url`, `provider_token`), lo firma, lo envía a SUNAT y
responde si fue aceptado y dónde está el PDF. Documentación:
https://www.nubefact.com/integracion

Cualquier otro OSE o PSE entra implementando el mismo puerto `ElectronicInvoicer`.
El token nunca se escribe en los logs.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from resthub.core.logs import get_logger
from resthub.modules.billing.domain.invoices import (
    CENT,
    HUNDRED,
    BillingSettings,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import ProviderResult

logger = get_logger("resthub.billing.nubefact")

TIMEOUT_SECONDS = 15.0
# Catálogo de Nubefact: 1 factura, 2 boleta. Moneda 1 = soles.
_TIPO = {InvoiceKind.FACTURA: 1, InvoiceKind.BOLETA: 2}
_SOLES = 1
# IGV «gravado - operación onerosa»; «NIU» es unidad (bienes) y «ZZ» servicios.
_GRAVADO = 1
_UNIDAD = "ZZ"
_SIX = Decimal("0.000001")


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def build_payload(settings: BillingSettings, invoice: Invoice) -> dict[str, Any]:
    """El JSON de «generar_comprobante». Precios con IGV incluido, como en la carta."""
    factor = (HUNDRED + invoice.igv_rate) / HUNDRED
    items = []
    for line in invoice.lines:
        valor_unitario = (line.unit_price / factor).quantize(_SIX)
        subtotal = (line.total / factor).quantize(CENT, rounding=ROUND_HALF_UP)
        items.append(
            {
                "unidad_de_medida": _UNIDAD,
                "codigo": "",
                "descripcion": line.description,
                "cantidad": line.quantity,
                "valor_unitario": str(valor_unitario),
                "precio_unitario": _money(line.unit_price),
                "descuento": "",
                "subtotal": _money(subtotal),
                "tipo_de_igv": _GRAVADO,
                "igv": _money(line.total - subtotal),
                "total": _money(line.total),
                "anticipo_regularizacion": False,
            }
        )
    customer = invoice.customer
    return {
        "operacion": "generar_comprobante",
        "tipo_de_comprobante": _TIPO[invoice.kind],
        "serie": invoice.series,
        "numero": invoice.number,
        "sunat_transaction": 1,
        "cliente_tipo_de_documento": customer.document_type.sunat_code,
        "cliente_numero_de_documento": customer.document_number or "-",
        "cliente_denominacion": customer.name or "Clientes varios",
        "cliente_direccion": customer.address,
        "fecha_de_emision": invoice.issued_at.strftime("%d-%m-%Y"),
        "moneda": _SOLES,
        "porcentaje_de_igv": str(invoice.igv_rate),
        "descuento_global": _money(invoice.discount / factor) if invoice.discount else "",
        "total_descuento": _money(invoice.discount) if invoice.discount else "",
        "total_gravada": _money(invoice.taxable),
        "total_igv": _money(invoice.igv),
        "total": _money(invoice.total),
        "enviar_automaticamente_a_la_sunat": True,
        "enviar_automaticamente_al_cliente": False,
        "formato_de_pdf": "TICKET",
        "observaciones": f"Pedido {invoice.order_id} · {settings.legal_name}",
    }


def interpret(status_code: int, body: dict[str, Any]) -> ProviderResult:
    """La respuesta del proveedor como un estado del comprobante."""
    if status_code >= 400 or "errors" in body:
        return ProviderResult(
            status=InvoiceStatus.REJECTED,
            message=str(body.get("errors") or f"El proveedor respondió {status_code}."),
            raw=body,
        )
    aceptado = bool(body.get("aceptada_por_sunat"))
    return ProviderResult(
        status=InvoiceStatus.ACCEPTED if aceptado else InvoiceStatus.PENDING,
        message=str(body.get("sunat_description") or ("Aceptado" if aceptado else "Enviado")),
        pdf_url=str(body.get("enlace_del_pdf") or ""),
        raw=body,
    )


class NubefactInvoicer:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # El transporte se inyecta en las pruebas: ninguna llama a Nubefact de verdad.
        self._transport = transport

    async def send(
        self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind
    ) -> ProviderResult:
        payload = build_payload(settings, invoice)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    settings.provider_url,
                    json=payload,
                    headers={"Authorization": f'Token token="{settings.provider_token}"'},
                )
            body = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "billing.provider_unreachable", code=invoice.code, error=type(error).__name__
            )
            return ProviderResult(
                status=InvoiceStatus.PENDING,
                message="No se pudo conectar con el proveedor; se puede reenviar.",
            )
        result = interpret(response.status_code, body if isinstance(body, dict) else {})
        logger.info("billing.invoice_sent", code=invoice.code, status=result.status.value)
        return result
