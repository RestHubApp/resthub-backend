"""Casos de uso de comprobantes con dobles en memoria: emitir, reenviar y configurar.

Cada caso afirma la serie y el correlativo que toma el comprobante, lo que se
le manda al proveedor (o que no se le manda), el estado con que queda y el
asiento de bitácora.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.identity import Principal
from resthub.core.pagination import Page
from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettings,
    InvalidInvoice,
    InvoiceNotFound,
    OrderAlreadyInvoiced,
    PaidOrderNotFound,
)
from resthub.modules.billing.domain.invoices import (
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
)
from resthub.modules.billing.ports.billing_ports import InvoiceQuery, PaidOrder, ProviderResult
from resthub.modules.billing.use_cases.invoicing import (
    IssueInvoice,
    IssueInvoiceCommand,
    ListInvoices,
    ResendInvoice,
    SettingsChange,
    UpdateBillingSettings,
    find_invoice,
)
from tests.conftest import RecordingActivity

LOCAL = 1
OTRO = 2
D = Decimal
CAJERO = Principal(user_id=7, role_id=2, is_active=True, restaurant_id=LOCAL)
LISTO = BillingSettings(
    restaurant_id=LOCAL,
    ruc="20123456789",
    legal_name="Doña Rosa SAC",
    provider_url="https://api.nubefact.com/x",
    provider_token="t",
    boleta_series="B002",
    factura_series="F003",
    igv_rate=D("10.5"),
)
PEDIDO = PaidOrder(
    id=40,
    number=14,
    total=D("61.50"),
    discount=D("0.00"),
    items=(
        ("Lomo saltado", 2, D("28.00"), False),
        ("Chicha", 1, D("5.50"), False),
        ("Postre", 1, D("9"), True),
    ),
)


class Comprobantes:
    def __init__(self) -> None:
        self.rows: dict[int, Invoice] = {}
        self.bloqueos: list[int] = []
        self.guardados = 0
        self.consultas: list[InvoiceQuery] = []

    async def add(self, invoice: Invoice) -> Invoice:
        stored = replace(invoice, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return stored

    async def get(self, restaurant_id: int, invoice_id: int, *, for_update: bool = False):
        if for_update:
            self.bloqueos.append(invoice_id)
        found = self.rows.get(invoice_id)
        return found if found and found.restaurant_id == restaurant_id else None

    async def for_order(self, restaurant_id: int, order_id: int) -> Invoice | None:
        return next(
            (
                i
                for i in self.rows.values()
                if i.restaurant_id == restaurant_id and i.order_id == order_id
            ),
            None,
        )

    async def save(self, invoice: Invoice) -> Invoice:
        self.guardados += 1
        self.rows[invoice.id or 0] = invoice
        return invoice

    async def search(self, query: InvoiceQuery) -> Page[Invoice]:
        self.consultas.append(query)
        return Page(items=list(self.rows.values()), total=len(self.rows))

    async def next_number(self, restaurant_id: int, series: str) -> int:
        return 1 + sum(1 for i in self.rows.values() if i.series == series)


class Ajustes:
    def __init__(self, config: BillingSettings) -> None:
        self.config = config
        self.guardados: list[BillingSettings] = []
        self.peticiones: list[int] = []

    async def get(self, restaurant_id: int) -> BillingSettings:
        self.peticiones.append(restaurant_id)
        return self.config

    async def save(self, settings: BillingSettings) -> BillingSettings:
        self.guardados.append(settings)
        self.config = settings
        return settings


class Pagados:
    def __init__(self, *pedidos: PaidOrder) -> None:
        self.pedidos = {p.id: p for p in pedidos}

    async def get(self, restaurant_id: int, order_id: int) -> PaidOrder | None:
        return self.pedidos.get(order_id) if restaurant_id == LOCAL else None


class Proveedor:
    def __init__(self, resultado: ProviderResult) -> None:
        self.resultado = resultado
        self.envios: list[tuple[BillingSettings, Invoice, InvoiceKind]] = []

    async def send(self, settings: BillingSettings, invoice: Invoice, kind: InvoiceKind):
        self.envios.append((settings, invoice, kind))
        return self.resultado


ACEPTADO = ProviderResult(InvoiceStatus.ACCEPTED, "Aceptada", "https://pdf/1", {"ok": True})


def _emitir(
    config: BillingSettings = LISTO, resultado: ProviderResult = ACEPTADO
) -> tuple[IssueInvoice, Comprobantes, Proveedor, RecordingActivity]:
    comprobantes, proveedor, bitacora = Comprobantes(), Proveedor(resultado), RecordingActivity()
    emitir = IssueInvoice(comprobantes, Ajustes(config), Pagados(PEDIDO), proveedor, bitacora)
    return emitir, comprobantes, proveedor, bitacora


async def test_una_boleta_toma_la_serie_del_local_y_se_envia_a_sunat() -> None:
    emitir, comprobantes, proveedor, bitacora = _emitir()

    boleta = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    assert (boleta.series, boleta.number, boleta.code) == ("B002", 1, "B002-1")
    assert (boleta.order_id, boleta.total, boleta.discount, boleta.igv_rate) == (
        40,
        D("61.50"),
        D("0.00"),
        D("10.50"),
    )
    assert (boleta.taxable, boleta.igv) == (D("55.66"), D("5.84"))
    assert [ln.description for ln in boleta.lines] == ["Lomo saltado", "Chicha"]
    assert boleta.customer.name == "Clientes varios"
    assert boleta.issued_by == CAJERO.user_id
    assert (boleta.status, boleta.pdf_url, boleta.provider_response) == (
        InvoiceStatus.ACCEPTED,
        "https://pdf/1",
        {"ok": True},
    )
    ((enviado_con, enviado, tipo),) = proveedor.envios
    assert (enviado_con, enviado.id, tipo) == (LISTO, boleta.id, InvoiceKind.BOLETA)
    assert comprobantes.guardados == 1
    assert bitacora.entries == [
        (
            LOCAL,
            7,
            ActivityKind.INVOICE_ISSUED,
            "Boleta de venta B002-1 del pedido #14 (S/ 61.50): Aceptado por SUNAT",
        )
    ]


async def test_una_factura_toma_su_propia_serie_y_correlativo() -> None:
    emitir, comprobantes, _, bitacora = _emitir()
    cliente = Customer(DocumentType.RUC, "20123456789", "Pollería SAC")
    comprobantes.rows[99] = replace(
        await comprobantes.add(
            Invoice(
                LOCAL, 1, InvoiceKind.FACTURA, "F003", 1, cliente, (), D("1"), D("0"), D("18"), 1
            )
        ),
        id=99,
    )

    factura = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.FACTURA, cliente))

    assert factura.code == "F003-3"
    assert bitacora.entries[0][3].startswith("Factura F003-3 del pedido #14")


async def test_sin_proveedor_el_comprobante_queda_sin_enviar_y_no_se_llama() -> None:
    emitir, _, proveedor, bitacora = _emitir(BillingSettings(restaurant_id=LOCAL))

    boleta = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    assert boleta.status is InvoiceStatus.SIMULATED
    assert boleta.provider_message == (
        "Falta configurar el RUC y el proveedor: se reenvía al cargarlos."
    )
    assert (boleta.pdf_url, boleta.provider_response) == ("", {})
    assert boleta.code == "B001-1"
    assert proveedor.envios == []
    assert bitacora.entries[0][3].endswith(": Sin enviar (sin proveedor)")


async def test_una_respuesta_sin_datos_crudos_queda_como_diccionario_vacio() -> None:
    emitir, _, _, _ = _emitir(resultado=ProviderResult(InvoiceStatus.REJECTED, "RUC inválido"))

    boleta = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    assert (boleta.status, boleta.provider_message, boleta.provider_response) == (
        InvoiceStatus.REJECTED,
        "RUC inválido",
        {},
    )


async def test_no_se_emite_lo_no_pagado_ni_dos_veces_ni_con_cliente_invalido() -> None:
    emitir, comprobantes, proveedor, bitacora = _emitir()
    primera = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    with pytest.raises(PaidOrderNotFound) as no_pagado:
        await emitir(IssueInvoiceCommand(CAJERO, 41, InvoiceKind.BOLETA, Customer()))
    with pytest.raises(PaidOrderNotFound):
        await emitir(
            IssueInvoiceCommand(
                replace(CAJERO, restaurant_id=OTRO), 40, InvoiceKind.BOLETA, Customer()
            )
        )
    with pytest.raises(OrderAlreadyInvoiced) as repetida:
        await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    assert no_pagado.value.order_id == 41
    assert repetida.value.code == primera.code
    assert len(comprobantes.rows) == 1
    assert len(proveedor.envios) == 1
    assert len(bitacora.entries) == 1


async def test_una_factura_sin_ruc_no_toma_numero() -> None:
    emitir, comprobantes, _, _ = _emitir()

    with pytest.raises(InvalidInvoice):
        await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.FACTURA, Customer()))
    assert comprobantes.rows == {}


async def test_reenviar_toma_el_comprobante_y_lo_manda_otra_vez() -> None:
    comprobantes = Comprobantes()
    rechazada = await comprobantes.add(
        Invoice(
            LOCAL,
            40,
            InvoiceKind.BOLETA,
            "B002",
            1,
            Customer(),
            (),
            D("10"),
            D("0"),
            D("18"),
            7,
            status=InvoiceStatus.REJECTED,
        )
    )
    proveedor = Proveedor(ACEPTADO)
    reenviar = ResendInvoice(comprobantes, Ajustes(LISTO), proveedor)

    reenviada = await reenviar(LOCAL, rechazada.id or 0)

    assert reenviada.status is InvoiceStatus.ACCEPTED
    assert comprobantes.bloqueos == [rechazada.id]
    assert len(proveedor.envios) == 1
    with pytest.raises(InvalidInvoice) as aceptada:
        await reenviar(LOCAL, rechazada.id or 0)
    with pytest.raises(InvoiceNotFound) as ajena:
        await reenviar(OTRO, rechazada.id or 0)
    assert str(aceptada.value) == "El comprobante B002-1 ya fue aceptado por SUNAT."
    assert ajena.value.invoice_id == rechazada.id
    assert len(proveedor.envios) == 1


async def test_buscar_un_comprobante_sin_bloqueo_por_omision() -> None:
    comprobantes = Comprobantes()
    boleta = await comprobantes.add(
        Invoice(
            LOCAL, 40, InvoiceKind.BOLETA, "B001", 1, Customer(), (), D("1"), D("0"), D("18"), 7
        )
    )

    assert await find_invoice(comprobantes, LOCAL, boleta.id or 0) is boleta
    assert comprobantes.bloqueos == []
    with pytest.raises(InvoiceNotFound):
        await find_invoice(comprobantes, OTRO, boleta.id or 0)


async def test_listar_comprobantes_pasa_la_consulta_tal_cual() -> None:
    comprobantes = Comprobantes()
    consulta = InvoiceQuery(restaurant_id=LOCAL, status=InvoiceStatus.PENDING, limit=5)

    pagina = await ListInvoices(comprobantes)(consulta)

    assert (pagina.items, pagina.total) == ([], 0)
    assert comprobantes.consultas == [consulta]


def _cambio(**datos: object) -> SettingsChange:
    campos: dict[str, object] = {
        "ruc": "20123456789",
        "legal_name": "Rosa SAC",
        "address": "Jr. Pizarro 450",
        "igv_rate": D("18"),
        "boleta_series": "B001",
        "factura_series": "F001",
        "provider_url": " https://api.nubefact.com/x ",
    }
    campos.update(datos)
    return SettingsChange(**campos)  # type: ignore[arg-type]


async def test_configurar_conserva_el_token_y_la_zona_si_no_se_mandan() -> None:
    ajustes = Ajustes(replace(LISTO, provider_token="secreto", timezone="America/Bogota"))
    bitacora = RecordingActivity()

    guardado = await UpdateBillingSettings(ajustes, bitacora)(LOCAL, 9, _cambio())

    assert (guardado.provider_token, guardado.timezone) == ("secreto", "America/Bogota")
    assert guardado.provider_url == "https://api.nubefact.com/x"
    assert (guardado.ruc, guardado.legal_name, guardado.address) == (
        "20123456789",
        "Rosa SAC",
        "Jr. Pizarro 450",
    )
    assert (guardado.igv_rate, guardado.boleta_series, guardado.factura_series) == (
        D("18.00"),
        "B001",
        "F001",
    )
    assert bitacora.entries == [
        (
            LOCAL,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "RUC 20123456789, IGV 18.00 %, series B001 y F001",
        )
    ]


async def test_configurar_reemplaza_o_borra_el_token() -> None:
    ajustes, bitacora = Ajustes(LISTO), RecordingActivity()
    actualizar = UpdateBillingSettings(ajustes, bitacora)

    nuevo = await actualizar(LOCAL, 9, _cambio(provider_token="otro"))
    assert nuevo.provider_token == "otro"
    borrado = await actualizar(LOCAL, 9, _cambio(provider_token="", ruc=""))
    assert borrado.provider_token == ""
    assert bitacora.entries[-1][3] == "RUC —, IGV 18.00 %, series B001 y F001"


async def test_una_url_insegura_no_se_guarda() -> None:
    ajustes, bitacora = Ajustes(LISTO), RecordingActivity()

    with pytest.raises(InvalidBillingSettings):
        await UpdateBillingSettings(ajustes, bitacora)(
            LOCAL, 9, _cambio(provider_url="https://localhost/x")
        )
    assert ajustes.guardados == []
    assert bitacora.entries == []


async def test_configurar_toma_los_datos_fiscales_del_local_y_graba_todo_lo_enviado() -> None:
    ajustes, bitacora = Ajustes(replace(LISTO, igv_rate=D("10.5"))), RecordingActivity()

    guardado = await UpdateBillingSettings(ajustes, bitacora)(
        LOCAL,
        9,
        _cambio(igv_rate=D("20.5"), boleta_series="B007", factura_series="F008"),
    )

    assert ajustes.peticiones == [LOCAL]
    assert (guardado.igv_rate, guardado.boleta_series, guardado.factura_series) == (
        D("20.50"),
        "B007",
        "F008",
    )
    assert bitacora.entries == [
        (
            LOCAL,
            9,
            ActivityKind.BILLING_SETTINGS_UPDATED,
            "RUC 20123456789, IGV 20.50 %, series B007 y F008",
        )
    ]


async def test_el_comprobante_lleva_su_hora_de_emision_en_utc() -> None:
    emitir, _, _, _ = _emitir()
    antes = datetime.now(UTC)

    comprobante = await emitir(IssueInvoiceCommand(CAJERO, 40, InvoiceKind.BOLETA, Customer()))

    assert comprobante.issued_at >= antes
    assert comprobante.issued_at.tzinfo is UTC
