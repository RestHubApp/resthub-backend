"""Envío de comprobantes a SUNAT a través de Lycet, la API REST de Greenter.

Lycet es uno para todo el servidor (`LYCET_URL`, `LYCET_TOKEN`) y atiende a varias
empresas: elige el usuario SOL y el certificado por el RUC del comprobante
(`company.ruc`). Cada local se registra en él al guardar sus credenciales
(`PUT /api/v1/configuration/company/{ruc}`). Firma el XML, lo envía a SUNAT y
responde con el XML firmado y la constancia (CDR); el PDF se pide aparte con el
mismo documento. Las notas de crédito van igual por `/note/send` y `/note/pdf`.
Código y documentación: https://github.com/giansalex/lycet

El token va en la consulta (`?token=`), como lo pide Lycet; ni él, ni la URL
completa, ni el usuario SOL, la clave o el certificado se escriben en los logs.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from http import HTTPStatus
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from resthub.core.logs import get_logger
from resthub.modules.billing.domain.amount_words import amount_in_words
from resthub.modules.billing.domain.credit_notes import CreditNote
from resthub.modules.billing.domain.exceptions import (
    BillingProviderError,
    InvalidBillingCredentialsError,
)
from resthub.modules.billing.domain.invoices import (
    CENT,
    HUNDRED,
    ZERO,
    BillingSettings,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import ProviderResult

logger = get_logger("resthub.billing.lycet")

TIMEOUT_SECONDS = 30.0
# Las rutas del API de Lycet cuelgan de acá.
API_PATH = "/api/v1"
# Un PDF de boleta pesa decenas de KB; el tope evita cargar en memoria una respuesta rara.
MAX_PDF_BYTES = 5 * 1024 * 1024
# Catálogo 01 de SUNAT: 01 factura, 03 boleta, 07 nota de crédito.
_KIND_CODE = {InvoiceKind.TAX_INVOICE: "01", InvoiceKind.SALES_RECEIPT: "03"}
_CREDIT_NOTE = "07"
# Catálogo 51: venta interna.
_INTERNAL_SALE = "0101"
# Catálogo 07: gravado (onerosa) y gravado - retiro (lo que se regala).
_TAXED = "10"
_FREE = "13"
# Catálogo 53: descuento global que afecta la base imponible del IGV.
_GLOBAL_DISCOUNT = "02"
# «NIU» es unidad (bienes): un plato se vende por unidad.
_UNIT = "NIU"
# Establecimiento anexo: «0000» es el domicilio fiscal.
_MAIN_PREMISES = "0000"
_SIX = Decimal("0.000001")
_FIVE = Decimal("0.00001")
# Catálogo 52: el importe en letras y la leyenda de una transferencia gratuita.
_AMOUNT_LEGEND = "1000"
_FREE_LEGEND = "1002"
_FREE_LEGEND_TEXT = "TRANSFERENCIA GRATUITA DE UN BIEN Y/O SERVICIO PRESTADO GRATUITAMENTE"
# Códigos de error de SUNAT o de la conexión con ella que no dicen nada del comprobante:
# no llegó a revisarlo (servicio caído, sin respuesta). Se puede reenviar tal cual.
_TRANSIENT_CODES = frozenset({"0100", "0109", "0130", "0131", "0132", "0133", "0134", "0135"})
_TRANSIENT_CODES |= frozenset({"0136", "0137", "0138", "0200", "HTTP"})
# SUNAT no aceptó las credenciales del local: usuario o clave SOL errados, cliente no
# autorizado o sin perfil para emitir. El comprobante no tiene la culpa: se corrige el
# usuario SOL y se reenvía.
_CREDENTIAL_CODES = frozenset({"0102", "0103", "0104", "0111"})
# SUNAT: 0 aceptado; desde 4000, aceptado con observaciones; de 2000 a 3999, rechazado.
_FIRST_OBSERVATION = 4000
_UNREACHABLE = "No se pudo conectar con el servicio de facturación; se puede reenviar."
# Lycet respondió con error sin llegar a SUNAT: lo usual es que el RUC del local no esté
# registrado (sin credenciales no firma) o que el certificado no sirva.
_PROVIDER_FAILED = (
    "El servicio de facturación no pudo firmar ni enviar el comprobante (HTTP {status}): "
    "revisa el usuario SOL y el certificado del local y reenvíalo."
)
_NOT_CONFIGURED = "El servidor no tiene configurado el servicio de facturación: no se envió."
# Lo que SUNAT agrega después del mensaje no le sirve a quien emite («- Detalle: xxx…»).
_DETAIL_MARKER = " - Detalle:"


def _number(value: Decimal) -> float:
    """Lycet lee los importes como números JSON; se redondean antes de convertirlos."""
    return float(value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _issue_datetime(document: Invoice | CreditNote, timezone: str) -> str:
    """El momento de emisión en la hora del local, con su desfase: SUNAT valida ese día."""
    return document.issued_at.astimezone(ZoneInfo(timezone)).replace(microsecond=0).isoformat()


def _company(settings: BillingSettings) -> dict[str, Any]:
    return {
        "ruc": settings.ruc,
        "razonSocial": settings.legal_name,
        "nombreComercial": settings.legal_name,
        "address": {
            "direccion": settings.address or "-",
            "codLocal": _MAIN_PREMISES,
            "codigoPais": "PE",
        },
    }


def _customer_party(invoice: Invoice) -> dict[str, Any]:
    customer = invoice.customer
    client: dict[str, Any] = {
        "tipoDoc": customer.document_type.sunat_code,
        "numDoc": customer.document_number or "-",
        "rznSocial": customer.name or "Clientes varios",
    }
    if customer.address and customer.document_type is not DocumentType.NONE:
        client["address"] = {"direccion": customer.address, "codigoPais": "PE"}
    return client


def _lines(invoice: Invoice, factor: Decimal) -> list[dict[str, Any]]:
    rate = invoice.igv_rate
    details = []
    for index, line in enumerate(invoice.lines, start=1):
        unit_value = (line.unit_price / factor).quantize(_SIX, rounding=ROUND_HALF_UP)
        gross = line.unit_price * line.quantity
        base = _money(gross / factor)
        igv = _money(gross) - base
        detail: dict[str, Any] = {
            "unidad": _UNIT,
            "cantidad": line.quantity,
            "codProducto": f"P{index:03d}",
            "descripcion": line.description,
            "mtoValorVenta": _number(base),
            "mtoBaseIgv": _number(base),
            "porcentajeIgv": _number(rate),
            "igv": _number(igv),
            "totalImpuestos": _number(igv),
        }
        if line.is_courtesy:
            # Una cortesía: no se cobra, pero se informa su valor de referencia.
            detail |= {
                "tipAfeIgv": _FREE,
                "mtoValorUnitario": 0,
                "mtoValorGratuito": _number(unit_value),
                "mtoPrecioUnitario": 0,
            }
        else:
            detail |= {
                "tipAfeIgv": _TAXED,
                "mtoValorUnitario": _number(unit_value),
                "mtoPrecioUnitario": _number(line.unit_price),
            }
        details.append(detail)
    return details


def build_payload(settings: BillingSettings, invoice: Invoice) -> dict[str, Any]:
    """El comprobante en el JSON del modelo `Invoice` de Greenter.

    Los precios de la carta ya traen el IGV: se separa la base con la tasa del local.
    El total cobrado manda; el descuento del pedido va como descuento global que
    baja la base imponible, y las cortesías como operaciones gratuitas.
    """
    factor = (HUNDRED + invoice.igv_rate) / HUNDRED
    details = _lines(invoice, factor)
    paid = [d for d in details if d["tipAfeIgv"] == _TAXED]
    free = [d for d in details if d["tipAfeIgv"] == _FREE]
    taxable, igv = invoice.taxable, invoice.igv
    payload: dict[str, Any] = {
        "ublVersion": "2.1",
        "tipoOperacion": _INTERNAL_SALE,
        "tipoDoc": _KIND_CODE[invoice.kind],
        "serie": invoice.series,
        "correlativo": str(invoice.number),
        "fechaEmision": _issue_datetime(invoice, settings.timezone),
        "formaPago": {"moneda": "PEN", "tipo": "Contado"},
        "tipoMoneda": "PEN",
        "company": _company(settings),
        "client": _customer_party(invoice),
        "mtoOperGravadas": _number(taxable),
        "mtoIGV": _number(igv),
        "totalImpuestos": _number(igv),
        "valorVenta": _number(taxable),
        "subTotal": _number(invoice.total),
        "mtoImpVenta": _number(invoice.total),
        "details": details,
        "legends": [{"code": _AMOUNT_LEGEND, "value": amount_in_words(invoice.total)}],
        "observacion": f"Pedido {invoice.order_id}",
    }
    gross_base = sum((Decimal(str(d["mtoValorVenta"])) for d in paid), ZERO)
    discount_base = gross_base - taxable
    if discount_base > 0 and gross_base > 0:
        payload["descuentos"] = [
            {
                "codTipo": _GLOBAL_DISCOUNT,
                "montoBase": _number(gross_base),
                "factor": _number((discount_base / gross_base).quantize(_FIVE)),
                "monto": _number(discount_base),
            }
        ]
    _add_free_totals(payload, free)
    return payload


def _add_free_totals(payload: dict[str, Any], free: list[dict[str, Any]]) -> None:
    if free:
        payload["mtoOperGratuitas"] = _number(
            sum((Decimal(str(d["mtoValorVenta"])) for d in free), ZERO)
        )
        payload["mtoIGVGratuitas"] = _number(sum((Decimal(str(d["igv"])) for d in free), ZERO))
        payload["legends"].append({"code": _FREE_LEGEND, "value": _FREE_LEGEND_TEXT})


def _spread_discount(paid: list[dict[str, Any]], taxable: Decimal, igv: Decimal) -> None:
    """Reparte el descuento global entre las líneas cobradas, en proporción a su base.

    Una nota de crédito no admite descuentos globales: sus líneas tienen que sumar,
    ya descontadas, la base y el IGV del comprobante que anula. La última línea se
    lleva la diferencia del redondeo.
    """
    gross = sum((Decimal(str(d["mtoValorVenta"])) for d in paid), ZERO)
    if not paid or gross <= 0 or gross == taxable:
        return
    base_left, igv_left = taxable, igv
    for index, detail in enumerate(paid):
        share = Decimal(str(detail["mtoValorVenta"])) / gross
        last = index == len(paid) - 1
        base = base_left if last else _money(taxable * share)
        line_igv = igv_left if last else _money(igv * share)
        base_left -= base
        igv_left -= line_igv
        quantity = Decimal(detail["cantidad"])
        detail |= {
            "mtoValorVenta": _number(base),
            "mtoBaseIgv": _number(base),
            "igv": _number(line_igv),
            "totalImpuestos": _number(line_igv),
            "mtoValorUnitario": _number((base / quantity).quantize(_SIX, ROUND_HALF_UP)),
            "mtoPrecioUnitario": _number(
                ((base + line_igv) / quantity).quantize(_SIX, ROUND_HALF_UP)
            ),
        }


def build_note_payload(
    settings: BillingSettings, note: CreditNote, invoice: Invoice
) -> dict[str, Any]:
    """La nota de crédito en el JSON del modelo `Note` de Greenter.

    Revierte el comprobante entero (motivo 01, anulación de la operación): las mismas
    líneas, el mismo cliente y los mismos totales. El descuento del comprobante va
    repartido en las líneas, porque una nota no tiene descuentos globales.
    """
    factor = (HUNDRED + invoice.igv_rate) / HUNDRED
    details = _lines(invoice, factor)
    paid = [d for d in details if d["tipAfeIgv"] == _TAXED]
    free = [d for d in details if d["tipAfeIgv"] == _FREE]
    taxable, igv = invoice.taxable, invoice.igv
    _spread_discount(paid, taxable, igv)
    payload: dict[str, Any] = {
        "ublVersion": "2.1",
        "tipoDoc": _CREDIT_NOTE,
        "serie": note.series,
        "correlativo": str(note.number),
        "fechaEmision": _issue_datetime(note, settings.timezone),
        # El comprobante que anula y el motivo (catálogo 09). Greenter lo escribe así,
        # «numDocfectado», sin la «A».
        "tipDocAfectado": _KIND_CODE[invoice.kind],
        "numDocfectado": invoice.code,
        "codMotivo": note.reason_code.value,
        "desMotivo": note.reason,
        "tipoMoneda": "PEN",
        "company": _company(settings),
        "client": _customer_party(invoice),
        "mtoOperGravadas": _number(taxable),
        "mtoIGV": _number(igv),
        "totalImpuestos": _number(igv),
        "valorVenta": _number(taxable),
        "subTotal": _number(note.total),
        "mtoImpVenta": _number(note.total),
        "details": details,
        "legends": [{"code": _AMOUNT_LEGEND, "value": amount_in_words(note.total)}],
    }
    _add_free_totals(payload, free)
    return payload


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean(message: str) -> str:
    return message.split(_DETAIL_MARKER, 1)[0].strip()


def _observations(notes: Any) -> str:  # noqa: ANN401 - viene del JSON de Lycet
    if not isinstance(notes, list) or not notes:
        return ""
    # «4096 - La provincia … no cumple con el formato establecido - INFO: 4096 (nodo …)»:
    # el código y la frase bastan.
    return "; ".join(" - ".join(str(note).split(" - ")[:2]).strip() for note in notes[:3])


def interpret(status_code: int, body: Mapping[str, Any]) -> ProviderResult:
    """La respuesta de Lycet como un estado del comprobante, con un mensaje en español.

    - Aceptado (código 0) o aceptado con observaciones (4000 en adelante): aceptado.
    - Rechazado por SUNAT (2000 a 3999, o un error del comprobante): rechazado.
    - SUNAT o Lycet sin responder: pendiente, se puede reenviar.
    """
    raw = _without_documents(body)
    answer = body.get("sunatResponse")
    if status_code != HTTPStatus.OK or not isinstance(answer, Mapping):
        message = (
            _PROVIDER_FAILED.format(status=status_code)
            if status_code != HTTPStatus.OK
            else _UNREACHABLE
        )
        return ProviderResult(status=InvoiceStatus.PENDING, message=message, raw=raw)
    signed_xml = str(body.get("xml") or "")
    if not answer.get("success"):
        error = _mapping(answer.get("error"))
        code = str(error.get("code") or "")
        detail = _clean(str(error.get("message") or "sin detalle"))
        if code in _TRANSIENT_CODES or not code:
            return ProviderResult(
                status=InvoiceStatus.PENDING,
                message=f"SUNAT no respondió ({code or 'sin código'}): {detail}. Reenvíalo.",
                raw=raw,
                signed_xml=signed_xml,
            )
        if code in _CREDENTIAL_CODES:
            return ProviderResult(
                status=InvoiceStatus.REJECTED,
                message=(
                    f"SUNAT no aceptó el usuario SOL o la clave del local ({code}): {detail}. "
                    "Corrígelos en los datos fiscales y reenvía el comprobante."
                ),
                raw=raw,
                signed_xml=signed_xml,
            )
        return ProviderResult(
            status=InvoiceStatus.REJECTED,
            message=f"Rechazado por SUNAT ({code}): {detail}",
            raw=raw,
            signed_xml=signed_xml,
        )
    cdr = _mapping(answer.get("cdrResponse"))
    code = str(cdr.get("code") or "0")
    description = _clean(str(cdr.get("description") or ""))
    cdr_zip = str(answer.get("cdrZip") or "")
    number = int(code) if code.isdigit() else -1
    if number == 0 or number >= _FIRST_OBSERVATION:
        observations = _observations(cdr.get("notes"))
        message = f"Aceptado por SUNAT: {description}" if description else "Aceptado por SUNAT."
        if observations:
            message = f"Aceptado con observaciones: {observations}"
        return ProviderResult(
            status=InvoiceStatus.ACCEPTED,
            message=message,
            raw=raw,
            signed_xml=signed_xml,
            cdr_zip=cdr_zip,
        )
    return ProviderResult(
        status=InvoiceStatus.REJECTED,
        message=f"Rechazado por SUNAT ({code}): {description or 'sin detalle'}",
        raw=raw,
        signed_xml=signed_xml,
        cdr_zip=cdr_zip,
    )


def _without_documents(body: Mapping[str, Any]) -> dict[str, Any]:
    """La respuesta para guardar como referencia, sin el XML ni el CDR (van cifrados aparte)."""
    raw: dict[str, Any] = {key: value for key, value in body.items() if key != "xml"}
    answer = raw.get("sunatResponse")
    if isinstance(answer, Mapping):
        raw["sunatResponse"] = {key: value for key, value in answer.items() if key != "cdrZip"}
    return raw


class LycetGateway:
    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._token = token.strip()
        # El transporte se inyecta en las pruebas: ninguna llama a Lycet de verdad.
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(self._base_url and self._token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self._base_url}{API_PATH}",
            params={"token": self._token},
            transport=self._transport,
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    async def send(
        self,
        settings: BillingSettings,
        invoice: Invoice,
        kind: InvoiceKind,  # noqa: ARG002 - firma del puerto; el tipo ya va en el comprobante
    ) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(status=InvoiceStatus.SIMULATED, message=_NOT_CONFIGURED)
        return await self._send("/invoice/send", build_payload(settings, invoice), invoice.code)

    async def send_note(
        self, settings: BillingSettings, note: CreditNote, invoice: Invoice
    ) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(status=InvoiceStatus.SIMULATED, message=_NOT_CONFIGURED)
        return await self._send(
            "/note/send", build_note_payload(settings, note, invoice), note.code
        )

    async def _send(self, path: str, payload: dict[str, Any], code: str) -> ProviderResult:
        try:
            async with self._client() as client:
                response = await client.post(path, json=payload)
            body = response.json() if response.status_code == HTTPStatus.OK else {}
        except (httpx.HTTPError, ValueError) as error:
            logger.warning(
                "billing.provider_unreachable", invoice_code=code, error=type(error).__name__
            )
            return ProviderResult(
                status=InvoiceStatus.PENDING, message=_UNREACHABLE, document_payload=payload
            )
        if response.status_code != HTTPStatus.OK:
            # Lycet responde 502 cuando el token no corresponde o el documento no se pudo leer.
            logger.warning(
                "billing.provider_failed", invoice_code=code, status=response.status_code
            )
        result = interpret(response.status_code, body if isinstance(body, dict) else {})
        logger.info("billing.invoice_sent", invoice_code=code, status=result.status.value)
        return ProviderResult(
            status=result.status,
            message=result.message,
            raw=result.raw,
            signed_xml=result.signed_xml,
            cdr_zip=result.cdr_zip,
            document_payload=payload,
        )

    async def render_pdf(self, invoice: Invoice) -> bytes:
        return await self._pdf("/invoice/pdf", invoice.document_payload, invoice.code)

    async def render_note_pdf(self, note: CreditNote) -> bytes:
        return await self._pdf("/note/pdf", note.document_payload, note.code)

    async def _pdf(self, path: str, payload: dict[str, Any], code: str) -> bytes:
        if not payload or not self.is_configured():
            message = f"El comprobante {code} no tiene documento para armar el PDF."
            raise BillingProviderError(message)
        try:
            async with self._client() as client:
                response = await client.post(path, json=payload)
        except httpx.HTTPError as error:
            logger.warning("billing.pdf_unreachable", error=type(error).__name__)
            message = "No se pudo conectar con el servicio de facturación para armar el PDF."
            raise BillingProviderError(message) from error
        content_type = response.headers.get("content-type", "")
        if response.status_code != HTTPStatus.OK or not content_type.startswith("application/pdf"):
            logger.warning("billing.pdf_failed", status=response.status_code)
            message = "El servicio de facturación no pudo armar el PDF. Inténtalo de nuevo."
            raise BillingProviderError(message)
        if len(response.content) > MAX_PDF_BYTES:
            message = "El PDF del comprobante pesa demasiado."
            raise BillingProviderError(message)
        return response.content

    async def register(self, settings: BillingSettings) -> None:
        if not self.is_configured():
            # Sin servicio no hay a quién registrar: los comprobantes quedan «sin enviar».
            logger.warning("billing.provider_not_configured")
            return
        payload = {
            "SOL_USER": f"{settings.ruc}{settings.sol_user}",
            "SOL_PASS": settings.sol_password,
            "certificate": base64.b64encode(settings.certificate_pem.encode()).decode("ascii"),
        }
        try:
            async with self._client() as client:
                response = await client.put(f"/configuration/company/{settings.ruc}", json=payload)
        except httpx.HTTPError as error:
            logger.warning("billing.provider_register_unreachable", error=type(error).__name__)
            message = "No se pudo conectar con el servicio de facturación. Inténtalo de nuevo."
            raise BillingProviderError(message) from error
        if response.status_code != HTTPStatus.OK:
            logger.warning("billing.provider_register_failed", status=response.status_code)
            if HTTPStatus.BAD_REQUEST <= response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                # Lycet rechaza lo que le falta o no puede leer (usuario, clave o certificado).
                message = (
                    "El servicio de facturación rechazó el usuario SOL, la clave o el "
                    "certificado del local. Revísalos y vuelve a guardarlos."
                )
                raise InvalidBillingCredentialsError(message)
            message = "El servicio de facturación no está disponible. Inténtalo de nuevo."
            raise BillingProviderError(message)
        logger.info("billing.provider_registered", restaurant_id=settings.restaurant_id)

    async def unregister(self, ruc: str) -> None:
        if not self.is_configured():
            return
        try:
            async with self._client() as client:
                response = await client.delete(f"/configuration/company/{ruc}")
        except httpx.HTTPError as error:
            logger.warning("billing.provider_unregister_unreachable", error=type(error).__name__)
            return
        if response.status_code not in (HTTPStatus.OK, HTTPStatus.NOT_FOUND):
            logger.warning("billing.provider_unregister_failed", status=response.status_code)
