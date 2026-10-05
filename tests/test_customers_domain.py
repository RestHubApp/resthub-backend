"""Reglas del cliente sin HTTP: validación de la ficha, estadísticas y alta o edición."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.pagination import Page
from resthub.modules.customers.domain.customers import (
    CONSENT_VERSION,
    MAX_ADDRESS_LENGTH,
    MAX_NAME_LENGTH,
    MAX_NOTES_LENGTH,
    Customer,
    CustomerStats,
)
from resthub.modules.customers.domain.exceptions import (
    ConsentRequired,
    CustomerNotFound,
    InvalidCustomer,
    PhoneTaken,
)
from resthub.modules.customers.ports.customer_repository import CustomerOrder, CustomerQuery
from resthub.modules.customers.use_cases.manage_customers import (
    RECENT_ORDERS,
    CustomerData,
    ReadCustomerCard,
    SaveCustomer,
    SearchCustomers,
)

LOCAL = 1
MESERO = 5


def test_el_ticket_promedio_redondea_el_medio_centavo_hacia_arriba() -> None:
    assert CustomerStats(visits=2, spent=Decimal("20.01")).average_ticket == Decimal("10.01")


def test_sin_visitas_el_ticket_promedio_es_cero() -> None:
    assert CustomerStats().average_ticket == Decimal("0.00")


def test_es_frecuente_desde_la_tercera_visita() -> None:
    assert not CustomerStats(visits=2).is_frequent
    assert CustomerStats(visits=3).is_frequent


def test_la_ficha_se_normaliza_al_crearse() -> None:
    cliente = Customer(
        restaurant_id=LOCAL,
        name="  Ana   Torres ",
        phone=" +51 987  654 321 ",
        email="  Ana.Torres@Correo.PE ",
        address="Jr.  Pizarro 450",
        reference=" frente al  parque ",
        notes="Alérgica  al maní",
    )

    assert cliente.name == "Ana Torres"
    assert cliente.phone == "+51 987 654 321"
    assert cliente.phone_key == "+51987654321"
    assert cliente.email == "ana.torres@correo.pe"
    assert cliente.address == "Jr. Pizarro 450"
    assert cliente.reference == "frente al parque"
    assert cliente.notes == "Alérgica al maní"


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        ({"name": "   "}, "El cliente necesita un nombre."),
        ({"name": "N" * (MAX_NAME_LENGTH + 1)}, "El nombre admite 80 caracteres como máximo."),
        ({"phone": "987-654-321"}, "El teléfono solo lleva dígitos, espacios y un «+» inicial."),
        ({"phone": "12345"}, "El teléfono solo lleva dígitos, espacios y un «+» inicial."),
        ({"phone": "9" * 21}, "El teléfono admite 20 caracteres como máximo."),
        ({"email": "ana.correo.pe"}, "El correo no es válido."),
        (
            {"address": "D" * (MAX_ADDRESS_LENGTH + 1)},
            "La dirección admite 200 caracteres como máximo.",
        ),
        ({"reference": "R" * 151}, "La referencia admite 150 caracteres como máximo."),
        ({"notes": "N" * (MAX_NOTES_LENGTH + 1)}, "La nota admite 300 caracteres como máximo."),
    ],
)
def test_una_ficha_invalida_explica_que_corregir(datos: dict[str, str], mensaje: str) -> None:
    campos = {"restaurant_id": LOCAL, "name": "Ana Torres", **datos}

    with pytest.raises(InvalidCustomer) as error:
        Customer(**campos)  # type: ignore[arg-type]
    assert error.value.reason == mensaje


def test_los_limites_exactos_de_la_ficha_se_aceptan() -> None:
    cliente = Customer(
        restaurant_id=LOCAL,
        name="N" * MAX_NAME_LENGTH,
        phone="123456",
        notes="N" * MAX_NOTES_LENGTH,
    )

    assert len(cliente.name) == MAX_NAME_LENGTH
    assert cliente.phone == "123456"
    assert Customer(restaurant_id=LOCAL, name="Ana", phone="9" * 20).phone == "9" * 20


def test_telefono_y_correo_son_opcionales() -> None:
    cliente = Customer(restaurant_id=LOCAL, name="Ana")

    assert (cliente.phone, cliente.email, cliente.phone_key) == ("", "", "")


# -- Casos de uso con dobles en memoria -------------------------------------------------


class Libreta:
    def __init__(self) -> None:
        self.rows: dict[int, Customer] = {}

    async def add(self, customer: Customer) -> Customer:
        stored = replace(customer, id=len(self.rows) + 1)
        self.rows[stored.id or 0] = stored
        return replace(stored)

    async def get(self, restaurant_id: int, customer_id: int) -> Customer | None:
        found = self.rows.get(customer_id)
        return replace(found) if found and found.restaurant_id == restaurant_id else None

    async def find_by_phone(self, restaurant_id: int, phone_key: str) -> Customer | None:
        return next(
            (
                replace(c)
                for c in self.rows.values()
                if c.restaurant_id == restaurant_id and c.phone_key == phone_key
            ),
            None,
        )

    async def save(self, customer: Customer) -> Customer:
        self.rows[customer.id or 0] = replace(customer)
        return replace(customer)

    async def search(self, query: CustomerQuery) -> Page[Customer]:
        items = [c for c in self.rows.values() if c.restaurant_id == query.restaurant_id]
        return Page(items=items[query.offset : query.offset + query.limit], total=len(items))


class Historial:
    def __init__(self, stats: dict[int, CustomerStats], pedidos: list[CustomerOrder]) -> None:
        self._stats = stats
        self._pedidos = pedidos
        self.pedidos_pedidos: list[int] = []

    async def stats(self, restaurant_id: int, customer_ids: object) -> dict[int, CustomerStats]:
        return {k: v for k, v in self._stats.items() if k in customer_ids}  # type: ignore[operator]

    async def orders(self, restaurant_id: int, customer_id: int, limit: int) -> list[CustomerOrder]:
        self.pedidos_pedidos.append(limit)
        return self._pedidos[:limit]


class Bitacora:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


async def test_editar_un_cliente_conserva_su_alta_y_queda_anotado() -> None:
    libreta, bitacora = Libreta(), Bitacora()
    guardar = SaveCustomer(libreta, bitacora)
    alta = await guardar(LOCAL, MESERO, CustomerData(name="Ana", phone="987 654 321", consent=True))

    editado = await guardar(
        LOCAL, MESERO, CustomerData(name="Ana Torres", phone="987654321"), alta.id
    )

    assert editado.id == alta.id
    assert editado.created_at == alta.created_at
    assert libreta.rows[alta.id or 0].name == "Ana Torres"
    assert [(kind, detalle) for *_, kind, detalle in bitacora.entries] == [
        (ActivityKind.CUSTOMER_CREATED, "Ana"),
        (ActivityKind.CUSTOMER_UPDATED, "Ana Torres"),
    ]


async def test_el_telefono_de_otro_cliente_no_se_puede_tomar_al_editar() -> None:
    libreta, bitacora = Libreta(), Bitacora()
    guardar = SaveCustomer(libreta, bitacora)
    await guardar(LOCAL, MESERO, CustomerData(name="Ana", phone="987 654 321", consent=True))
    luis = await guardar(LOCAL, MESERO, CustomerData(name="Luis", consent=True))

    with pytest.raises(PhoneTaken) as error:
        await guardar(LOCAL, MESERO, CustomerData(name="Luis", phone="987654321"), luis.id)
    assert str(error.value) == "El teléfono 987654321 ya es de Ana."
    assert libreta.rows[luis.id or 0].phone == ""
    assert len(bitacora.entries) == 2


async def test_sin_consentimiento_no_se_da_de_alta() -> None:
    # Ley N.º 29733: guardar sus datos exige que el cliente lo acepte.
    libreta, bitacora = Libreta(), Bitacora()

    with pytest.raises(ConsentRequired):
        await SaveCustomer(libreta, bitacora)(LOCAL, MESERO, CustomerData(name="Ana"))
    assert libreta.rows == {}
    assert bitacora.entries == []


async def test_el_alta_anota_cuando_quien_y_sobre_que_texto_acepto() -> None:
    alta = await SaveCustomer(Libreta(), Bitacora())(
        LOCAL, MESERO, CustomerData(name="Ana", consent=True)
    )

    assert alta.consent is not None
    assert alta.consent.version == CONSENT_VERSION
    assert alta.consent.recorded_by == MESERO


async def test_editar_conserva_el_consentimiento_original() -> None:
    libreta, bitacora = Libreta(), Bitacora()
    guardar = SaveCustomer(libreta, bitacora)
    alta = await guardar(LOCAL, MESERO, CustomerData(name="Ana", consent=True))

    sin_marcar = await guardar(LOCAL, 9, CustomerData(name="Ana Torres"), alta.id)
    marcando_otra_vez = await guardar(LOCAL, 9, CustomerData(name="Ana", consent=True), alta.id)

    assert sin_marcar.consent == alta.consent
    assert marcando_otra_vez.consent == alta.consent


async def test_un_cliente_guardado_antes_de_pedirlo_puede_aceptar_al_editarse() -> None:
    libreta, bitacora = Libreta(), Bitacora()
    antiguo = await libreta.add(Customer(restaurant_id=LOCAL, name="Beto"))
    guardar = SaveCustomer(libreta, bitacora)

    sigue_sin = await guardar(LOCAL, MESERO, CustomerData(name="Beto Ruiz"), antiguo.id)
    acepta = await guardar(LOCAL, MESERO, CustomerData(name="Beto Ruiz", consent=True), antiguo.id)

    assert sigue_sin.consent is None
    assert acepta.consent is not None
    assert acepta.consent.recorded_by == MESERO


async def test_editar_un_cliente_de_otro_local_responde_que_no_existe() -> None:
    libreta, bitacora = Libreta(), Bitacora()
    ajeno = await libreta.add(Customer(restaurant_id=2, name="Otro"))

    with pytest.raises(CustomerNotFound):
        await SaveCustomer(libreta, bitacora)(LOCAL, MESERO, CustomerData(name="Yo"), ajeno.id)
    assert libreta.rows[ajeno.id or 0].name == "Otro"
    assert bitacora.entries == []


async def test_la_busqueda_completa_cada_ficha_con_sus_visitas() -> None:
    libreta = Libreta()
    ana = await libreta.add(Customer(restaurant_id=LOCAL, name="Ana"))
    await libreta.add(Customer(restaurant_id=LOCAL, name="Luis"))
    visitas = CustomerStats(visits=4, spent=Decimal("80.00"))

    pagina = await SearchCustomers(libreta, Historial({ana.id or 0: visitas}, []))(
        CustomerQuery(restaurant_id=LOCAL)
    )

    assert pagina.total == 2
    assert [(c.customer.name, c.stats) for c in pagina.items] == [
        ("Ana", visitas),
        ("Luis", CustomerStats()),
    ]


async def test_la_ficha_trae_sus_ultimos_pedidos() -> None:
    libreta = Libreta()
    ana = await libreta.add(Customer(restaurant_id=LOCAL, name="Ana"))
    pedidos = [
        CustomerOrder(i, i, "delivery", "paid", Decimal("20.00"), datetime(2026, 9, i, tzinfo=UTC))
        for i in range(1, 13)
    ]
    historial = Historial({}, pedidos)

    ficha = await ReadCustomerCard(libreta, historial)(LOCAL, ana.id or 0)

    assert ficha.customer.name == "Ana"
    assert ficha.stats == CustomerStats()
    assert len(ficha.recent_orders) == RECENT_ORDERS == 10
    assert historial.pedidos_pedidos == [10]
    with pytest.raises(CustomerNotFound):
        await ReadCustomerCard(libreta, historial)(2, ana.id or 0)
