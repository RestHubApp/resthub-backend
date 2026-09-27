"""Límites y mensajes del dominio de comprobantes: datos fiscales, IGV, cliente y URL del proveedor.

Complementa `test_billing.py`: acá se fija cada mensaje de SUNAT que ve el
encargado, el redondeo del IGV al medio céntimo y cada forma de URL rechazada
con su motivo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.modules.billing.domain.exceptions import (
    InvalidBillingSettings,
    InvalidInvoice,
    InvoiceNotFound,
    InvoiceNumberTaken,
    OrderAlreadyInvoiced,
    PaidOrderNotFound,
)
from resthub.modules.billing.domain.invoices import (
    ANONYMOUS_LIMIT,
    DEFAULT_IGV_RATE,
    BillingSettings,
    Customer,
    DocumentType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    build_lines,
    provider_url_problem,
    split_igv,
    validate_customer,
    validate_provider_url,
)

D = Decimal


def _factura(**datos: object) -> Invoice:
    campos: dict[str, object] = {
        "restaurant_id": 1,
        "order_id": 9,
        "kind": InvoiceKind.FACTURA,
        "series": "F001",
        "number": 15,
        "customer": Customer(DocumentType.RUC, "20123456789", "Pollería SAC"),
        "lines": (),
        "total": D("118.00"),
        "discount": D("0.00"),
        "igv_rate": D("18.00"),
        "issued_by": 1,
    }
    campos.update(datos)
    return Invoice(**campos)  # type: ignore[arg-type]


# -- Rótulos y códigos ---------------------------------------------------------------


def test_rotulos_y_codigos_de_sunat() -> None:
    assert [k.label for k in InvoiceKind] == ["Boleta de venta", "Factura"]
    assert [d.sunat_code for d in DocumentType] == ["-", "1", "4", "6"]
    assert [s.label for s in InvoiceStatus] == [
        "Aceptado por SUNAT",
        "Pendiente de envío",
        "Rechazado",
        "Sin enviar (sin proveedor)",
    ]


# -- Datos fiscales del local ------------------------------------------------------------


def test_los_datos_fiscales_por_omision_no_estan_listos_para_emitir() -> None:
    local = BillingSettings(restaurant_id=1)

    assert (local.igv_rate, local.boleta_series, local.factura_series) == (
        DEFAULT_IGV_RATE,
        "B001",
        "F001",
    )
    assert not local.is_ready
    assert local.series_for(InvoiceKind.BOLETA) == "B001"
    assert local.series_for(InvoiceKind.FACTURA) == "F001"


def test_los_datos_fiscales_se_normalizan() -> None:
    local = BillingSettings(
        restaurant_id=1,
        ruc=" 20123456789 ",
        legal_name="  Doña   Rosa  SAC ",
        address=" Jr.  Pizarro 450 ",
        igv_rate=D("10.5"),
        boleta_series=" b002 ",
        factura_series="f00a",
        provider_url="  https://api.nubefact.com/x  ",
        provider_token="t",
    )

    assert local.ruc == "20123456789"
    assert local.legal_name == "Doña Rosa SAC"
    assert local.address == "Jr. Pizarro 450"
    assert str(local.igv_rate) == "10.50"
    assert (local.boleta_series, local.factura_series) == ("B002", "F00A")
    assert local.provider_url == "https://api.nubefact.com/x"
    assert local.is_ready


@pytest.mark.parametrize("faltante", ["ruc", "legal_name", "provider_url", "provider_token"])
def test_sin_cualquiera_de_los_cuatro_datos_no_se_emite_de_verdad(faltante: str) -> None:
    datos = {
        "ruc": "20123456789",
        "legal_name": "Rosa SAC",
        "provider_url": "https://api.nubefact.com/x",
        "provider_token": "t",
    }
    datos[faltante] = ""

    assert not BillingSettings(restaurant_id=1, **datos).is_ready  # type: ignore[arg-type]


def test_la_razon_social_y_la_direccion_se_recortan_al_maximo() -> None:
    local = BillingSettings(restaurant_id=1, legal_name="L" * 150, address="A" * 250)

    assert (len(local.legal_name), len(local.address)) == (100, 200)


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        ({"ruc": "30123456789"}, "El RUC del local tiene 11 dígitos y empieza en 10 o 20."),
        ({"ruc": "2012345678"}, "El RUC del local tiene 11 dígitos y empieza en 10 o 20."),
        ({"igv_rate": D("-0.01")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        ({"igv_rate": D("30.01")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        ({"igv_rate": D("18.005")}, "La tasa de IGV va de 0 a 30 % con dos decimales."),
        (
            {"boleta_series": "F001"},
            "La serie de boleta de venta son 4 caracteres y empieza con B.",
        ),
        (
            {"boleta_series": "B0001"},
            "La serie de boleta de venta son 4 caracteres y empieza con B.",
        ),
        ({"factura_series": "B001"}, "La serie de factura son 4 caracteres y empieza con F."),
        ({"factura_series": "F01"}, "La serie de factura son 4 caracteres y empieza con F."),
    ],
)
def test_datos_fiscales_invalidos_explican_el_motivo(
    datos: dict[str, object], mensaje: str
) -> None:
    with pytest.raises(InvalidBillingSettings) as error:
        BillingSettings(restaurant_id=1, **datos)  # type: ignore[arg-type]
    assert error.value.reason == mensaje
    assert str(error.value) == mensaje


@pytest.mark.parametrize("ruc", ["10123456789", "15123456789", "17123456789", "20123456789"])
def test_los_prefijos_validos_del_ruc(ruc: str) -> None:
    assert BillingSettings(restaurant_id=1, ruc=ruc).ruc == ruc


def test_las_tasas_limite_del_igv_se_aceptan() -> None:
    assert BillingSettings(restaurant_id=1, igv_rate=D("0")).igv_rate == D("0.00")
    assert BillingSettings(restaurant_id=1, igv_rate=D("30")).igv_rate == D("30.00")


# -- IGV --------------------------------------------------------------------------------


def test_el_igv_redondea_la_base_al_medio_centimo_hacia_arriba() -> None:
    # 1.77 / 1.18 = 1.5 exacto; 0.59 / 1.18 = 0.5 exacto; 1.00 / 1.18 = 0.847…
    assert split_igv(D("1.77"), D("18")) == (D("1.50"), D("0.27"))
    assert split_igv(D("1.00"), D("18")) == (D("0.85"), D("0.15"))
    # 0.05 / 1.18 = 0.04237… y 0.03 / 1.18 = 0.02542…: se redondea, no se trunca.
    assert split_igv(D("0.03"), D("18")) == (D("0.03"), D("0.00"))
    assert split_igv(D("100.00"), D("0")) == (D("100.00"), D("0.00"))


def test_la_base_y_el_igv_suman_siempre_el_total() -> None:
    for centimos in range(1, 400, 7):
        total = D(centimos) / 100
        base, igv = split_igv(total, D("18"))
        assert base + igv == total


def test_el_comprobante_calcula_su_base_igv_y_codigo() -> None:
    factura = _factura()

    assert (factura.taxable, factura.igv, factura.code) == (D("100.00"), D("18.00"), "F001-15")


# -- Cliente ----------------------------------------------------------------------------


def test_una_factura_con_ruc_y_razon_social_se_normaliza() -> None:
    cliente = validate_customer(
        InvoiceKind.FACTURA,
        Customer(DocumentType.RUC, " 20123456789 ", "  Pollería   SAC ", " Av.  Grau 1 "),
        D("50"),
    )

    assert cliente == Customer(DocumentType.RUC, "20123456789", "Pollería SAC", "Av. Grau 1")


@pytest.mark.parametrize(
    ("kind", "cliente", "total", "mensaje"),
    [
        (
            InvoiceKind.FACTURA,
            Customer(DocumentType.RUC, "2012345678", "SAC"),
            "10",
            "Una factura necesita el RUC del cliente (11 dígitos).",
        ),
        (
            InvoiceKind.FACTURA,
            Customer(DocumentType.DNI, "12345678", "Ana"),
            "10",
            "Una factura necesita el RUC del cliente (11 dígitos).",
        ),
        (
            InvoiceKind.FACTURA,
            Customer(DocumentType.RUC, "20123456789", "   "),
            "10",
            "Una factura necesita la razón social del cliente.",
        ),
        (
            InvoiceKind.BOLETA,
            Customer(),
            "700.01",
            "Una boleta de más de S/ 700.00 necesita el documento del cliente.",
        ),
        (
            InvoiceKind.BOLETA,
            Customer(DocumentType.DNI, "1234567", "Ana"),
            "10",
            "El número de DNI no es válido.",
        ),
        (
            InvoiceKind.BOLETA,
            Customer(DocumentType.CE, "ABC-123", "Ana"),
            "10",
            "El número de CE no es válido.",
        ),
        (
            InvoiceKind.BOLETA,
            Customer(DocumentType.RUC, "99123456789", "Ana"),
            "10",
            "El número de RUC no es válido.",
        ),
        (
            InvoiceKind.BOLETA,
            Customer(DocumentType.DNI, "12345678", " "),
            "10",
            "Escribe el nombre del cliente.",
        ),
    ],
)
def test_cada_regla_de_sunat_sobre_el_cliente_tiene_su_mensaje(
    kind: InvoiceKind, cliente: Customer, total: str, mensaje: str
) -> None:
    with pytest.raises(InvalidInvoice) as error:
        validate_customer(kind, cliente, D(total))
    assert error.value.reason == mensaje
    assert str(error.value) == mensaje


def test_una_boleta_de_justo_setecientos_puede_ir_sin_documento() -> None:
    cliente = validate_customer(
        InvoiceKind.BOLETA, Customer(DocumentType.NONE, "999", "  Ana  "), ANONYMOUS_LIMIT
    )

    # Sin documento no se guarda un número suelto, pero sí el nombre que dieron.
    assert cliente == Customer(DocumentType.NONE, "", "Ana", "")


def test_una_boleta_identificada_acepta_dni_ce_y_ruc() -> None:
    dni = validate_customer(
        InvoiceKind.BOLETA, Customer(DocumentType.DNI, "12345678", "Ana"), D("900")
    )
    ce = validate_customer(
        InvoiceKind.BOLETA, Customer(DocumentType.CE, " ab12345678 ", "Li"), D("1")
    )
    ruc = validate_customer(
        InvoiceKind.BOLETA, Customer(DocumentType.RUC, "10123456789", "Luis"), D("1")
    )

    assert (dni.document_number, ce.document_number, ruc.document_number) == (
        "12345678",
        "AB12345678",
        "10123456789",
    )


def test_el_nombre_y_la_direccion_del_cliente_se_recortan() -> None:
    cliente = validate_customer(
        InvoiceKind.BOLETA, Customer(DocumentType.NONE, "", "N" * 120, "A" * 250), D("1")
    )

    assert (len(cliente.name), len(cliente.address)) == (100, 200)


# -- Comprobante ----------------------------------------------------------------------------


def test_el_resultado_del_proveedor_se_guarda_recortado() -> None:
    factura = _factura()

    factura.record_result(InvoiceStatus.REJECTED, "m" * 400, "u" * 600, {"errors": "x"})

    assert factura.status is InvoiceStatus.REJECTED
    assert (len(factura.provider_message), len(factura.pdf_url)) == (300, 500)
    assert factura.provider_response == {"errors": "x"}


@pytest.mark.parametrize(
    "estado", [InvoiceStatus.PENDING, InvoiceStatus.REJECTED, InvoiceStatus.SIMULATED]
)
def test_solo_lo_aceptado_no_se_reenvia(estado: InvoiceStatus) -> None:
    _factura(status=estado).ensure_resendable()

    with pytest.raises(InvalidInvoice) as error:
        _factura(status=InvoiceStatus.ACCEPTED).ensure_resendable()
    assert str(error.value) == "El comprobante F001-15 ya fue aceptado por SUNAT."


def test_las_lineas_dejan_fuera_las_cortesias_y_redondean() -> None:
    lineas = build_lines(
        [
            ("Lomo saltado", 2, D("28.5"), False),
            ("Chicha", 1, D("5.00"), True),
            ("P" * 300, 3, D("1.335"), False),
        ]
    )

    assert [(ln.description[:12], ln.quantity, ln.unit_price, ln.total) for ln in lineas] == [
        ("Lomo saltado", 2, D("28.50"), D("57.00")),
        ("PPPPPPPPPPPP", 3, D("1.34"), D("4.00")),
    ]
    assert len(lineas[1].description) == 250


def test_la_fecha_de_emision_por_omision_es_ahora_en_utc() -> None:
    antes = datetime.now(UTC)

    assert _factura().issued_at >= antes


# -- URL del proveedor ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "motivo"),
    [
        ("https://api.nubefact.com/a b", "La URL del proveedor no puede llevar espacios."),
        ("https://api.nubefact.com/\x00", "La URL del proveedor no puede llevar espacios."),
        ("https://api.nubefact.com:99999/x", "La URL del proveedor no es válida."),
        ("http://api.nubefact.com/x", "La URL del proveedor tiene que empezar con https://."),
        (
            "https://u:p@api.nubefact.com/x",
            "La URL del proveedor no puede llevar usuario ni contraseña; el token va aparte.",
        ),
        (
            "https://u@api.nubefact.com/x",
            "La URL del proveedor no puede llevar usuario ni contraseña; el token va aparte.",
        ),
        ("https:///x", "La URL del proveedor no tiene servidor."),
        ("https://./x", "La URL del proveedor no tiene servidor."),
        ("https://localhost/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        (
            "https://api.localhost./x",
            "La URL del proveedor no puede apuntar a una dirección interna.",
        ),
        ("https://db.internal/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        ("https://nas.local/x", "La URL del proveedor no puede apuntar a una dirección interna."),
        (
            "https://192.168.1.10/x",
            "La URL del proveedor no puede apuntar a una dirección interna.",
        ),
        ("https://[fd00::1]/x", "La URL del proveedor no puede apuntar a una dirección interna."),
    ],
)
def test_cada_url_rechazada_tiene_su_motivo(url: str, motivo: str) -> None:
    assert provider_url_problem(url) == motivo
    with pytest.raises(InvalidBillingSettings) as error:
        validate_provider_url(url)
    assert error.value.reason == motivo


def test_una_url_valida_se_devuelve_sin_espacios_alrededor() -> None:
    assert validate_provider_url("  https://api.nubefact.com/x  ") == "https://api.nubefact.com/x"
    assert validate_provider_url("   ") == ""
    assert provider_url_problem("HTTPS://API.NUBEFACT.COM/x") is None
    assert provider_url_problem("https://1.1.1.1/x") is None
    assert provider_url_problem("https://api.nubefact.com./x") is None


# -- Errores -------------------------------------------------------------------------------


def test_los_errores_de_comprobantes_explican_lo_que_paso() -> None:
    no_existe = InvoiceNotFound(4)
    no_pagado = PaidOrderNotFound(7)
    ya_emitido = OrderAlreadyInvoiced("B001-3")

    assert (str(no_existe), no_existe.invoice_id) == ("No existe el comprobante 4.", 4)
    assert (str(no_pagado), no_pagado.order_id) == ("El pedido 7 no existe o no está pagado.", 7)
    assert (str(ya_emitido), ya_emitido.code) == (
        "El pedido ya tiene el comprobante B001-3.",
        "B001-3",
    )
    assert str(InvoiceNumberTaken()) == (
        "Otro comprobante tomó el mismo número a la vez. Vuelve a intentarlo."
    )
