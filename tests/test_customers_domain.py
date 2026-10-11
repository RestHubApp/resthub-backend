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
    ConsentRequiredError,
    CustomerNotFoundError,
    InvalidCustomerError,
    PhoneTakenError,
)
from resthub.modules.customers.ports.customer_repository import CustomerOrder, CustomerQuery
from resthub.modules.customers.use_cases.manage_customers import (
    RECENT_ORDERS,
    CustomerData,
    ReadCustomerCard,
    SaveCustomer,
    SearchCustomers,
)
from tests.fakes import FixedClock

RESTAURANT = 1
WAITER = 5


def test_average_ticket_rounds_half_a_cent_up() -> None:
    assert CustomerStats(visits=2, spent=Decimal("20.01")).average_ticket == Decimal("10.01")


def test_without_visits_average_ticket_is_zero() -> None:
    assert CustomerStats().average_ticket == Decimal("0.00")


def test_is_frequent_from_the_third_visit() -> None:
    assert not CustomerStats(visits=2).is_frequent
    assert CustomerStats(visits=3).is_frequent


def test_card_is_normalized_on_creation() -> None:
    customer = Customer(
        restaurant_id=RESTAURANT,
        name="  Ana   Torres ",
        phone=" +51 987  654 321 ",
        email="  Ana.Torres@Correo.PE ",
        address="Jr.  Pizarro 450",
        reference=" frente al  parque ",
        notes="Alérgica  al maní",
    )

    assert customer.name == "Ana Torres"
    assert customer.phone == "+51 987 654 321"
    assert customer.phone_key == "+51987654321"
    assert customer.email == "ana.torres@correo.pe"
    assert customer.address == "Jr. Pizarro 450"
    assert customer.reference == "frente al parque"
    assert customer.notes == "Alérgica al maní"


@pytest.mark.parametrize(
    ("data", "message"),
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
def test_invalid_card_explains_what_to_fix(data: dict[str, str], message: str) -> None:
    fields = {"restaurant_id": RESTAURANT, "name": "Ana Torres", **data}

    with pytest.raises(InvalidCustomerError) as error:
        Customer(**fields)  # type: ignore[arg-type]
    assert error.value.reason == message


def test_exact_card_limits_are_accepted() -> None:
    customer = Customer(
        restaurant_id=RESTAURANT,
        name="N" * MAX_NAME_LENGTH,
        phone="123456",
        notes="N" * MAX_NOTES_LENGTH,
    )

    assert len(customer.name) == MAX_NAME_LENGTH
    assert customer.phone == "123456"
    assert Customer(restaurant_id=RESTAURANT, name="Ana", phone="9" * 20).phone == "9" * 20


def test_phone_and_email_are_optional() -> None:
    customer = Customer(restaurant_id=RESTAURANT, name="Ana")

    assert (customer.phone, customer.email, customer.phone_key) == ("", "", "")


# -- Casos de uso con dobles en memoria -------------------------------------------------


class InMemoryCustomerRepository:
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


class FakeCustomerHistory:
    def __init__(self, stats: dict[int, CustomerStats], orders: list[CustomerOrder]) -> None:
        self._stats = stats
        self._orders = orders
        self.orders_limits: list[int] = []

    async def stats(self, restaurant_id: int, customer_ids: object) -> dict[int, CustomerStats]:
        return {k: v for k, v in self._stats.items() if k in customer_ids}  # type: ignore[operator]

    async def orders(self, restaurant_id: int, customer_id: int, limit: int) -> list[CustomerOrder]:
        self.orders_limits.append(limit)
        return self._orders[:limit]


class RecordingActivityLog:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


async def test_editing_a_customer_keeps_their_registration_and_is_recorded() -> None:
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()
    save_customer = SaveCustomer(repository, activity_log, clock=FixedClock())
    registered = await save_customer(
        RESTAURANT, WAITER, CustomerData(name="Ana", phone="987 654 321", consent=True)
    )

    edited = await save_customer(
        RESTAURANT, WAITER, CustomerData(name="Ana Torres", phone="987654321"), registered.id
    )

    assert edited.id == registered.id
    assert edited.created_at == registered.created_at
    assert repository.rows[registered.id or 0].name == "Ana Torres"
    assert [(kind, detail) for *_, kind, detail in activity_log.entries] == [
        (ActivityKind.CUSTOMER_CREATED, "Ana"),
        (ActivityKind.CUSTOMER_UPDATED, "Ana Torres"),
    ]


async def test_another_customers_phone_cannot_be_taken_when_editing() -> None:
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()
    save_customer = SaveCustomer(repository, activity_log, clock=FixedClock())
    await save_customer(
        RESTAURANT, WAITER, CustomerData(name="Ana", phone="987 654 321", consent=True)
    )
    luis = await save_customer(RESTAURANT, WAITER, CustomerData(name="Luis", consent=True))

    with pytest.raises(PhoneTakenError) as error:
        await save_customer(
            RESTAURANT, WAITER, CustomerData(name="Luis", phone="987654321"), luis.id
        )
    assert str(error.value) == "El teléfono 987654321 ya es de Ana."
    assert repository.rows[luis.id or 0].phone == ""
    assert len(activity_log.entries) == 2


async def test_without_consent_customer_is_not_registered() -> None:
    # Ley N.º 29733: guardar sus datos exige que el cliente lo acepte.
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()

    with pytest.raises(ConsentRequiredError):
        await SaveCustomer(repository, activity_log, clock=FixedClock())(
            RESTAURANT, WAITER, CustomerData(name="Ana")
        )
    assert repository.rows == {}
    assert activity_log.entries == []


async def test_registration_records_when_who_and_which_text_was_accepted() -> None:
    registered = await SaveCustomer(
        InMemoryCustomerRepository(), RecordingActivityLog(), clock=FixedClock()
    )(RESTAURANT, WAITER, CustomerData(name="Ana", consent=True))

    assert registered.consent is not None
    assert registered.consent.version == CONSENT_VERSION
    assert registered.consent.recorded_by == WAITER


async def test_editing_keeps_the_original_consent() -> None:
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()
    save_customer = SaveCustomer(repository, activity_log, clock=FixedClock())
    registered = await save_customer(RESTAURANT, WAITER, CustomerData(name="Ana", consent=True))

    unchecked = await save_customer(RESTAURANT, 9, CustomerData(name="Ana Torres"), registered.id)
    checked_again = await save_customer(
        RESTAURANT, 9, CustomerData(name="Ana", consent=True), registered.id
    )

    assert unchecked.consent == registered.consent
    assert checked_again.consent == registered.consent


async def test_customer_saved_before_consent_was_required_can_accept_when_edited() -> None:
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()
    legacy = await repository.add(Customer(restaurant_id=RESTAURANT, name="Beto"))
    save_customer = SaveCustomer(repository, activity_log, clock=FixedClock())

    still_without = await save_customer(
        RESTAURANT, WAITER, CustomerData(name="Beto Ruiz"), legacy.id
    )
    accepts = await save_customer(
        RESTAURANT, WAITER, CustomerData(name="Beto Ruiz", consent=True), legacy.id
    )

    assert still_without.consent is None
    assert accepts.consent is not None
    assert accepts.consent.recorded_by == WAITER


async def test_editing_a_customer_of_another_restaurant_answers_not_found() -> None:
    repository, activity_log = InMemoryCustomerRepository(), RecordingActivityLog()
    foreign = await repository.add(Customer(restaurant_id=2, name="Otro"))

    with pytest.raises(CustomerNotFoundError):
        await SaveCustomer(repository, activity_log, clock=FixedClock())(
            RESTAURANT, WAITER, CustomerData(name="Yo"), foreign.id
        )
    assert repository.rows[foreign.id or 0].name == "Otro"
    assert activity_log.entries == []


async def test_search_completes_each_card_with_its_visits() -> None:
    repository = InMemoryCustomerRepository()
    ana = await repository.add(Customer(restaurant_id=RESTAURANT, name="Ana"))
    await repository.add(Customer(restaurant_id=RESTAURANT, name="Luis"))
    ana_stats = CustomerStats(visits=4, spent=Decimal("80.00"))

    page = await SearchCustomers(repository, FakeCustomerHistory({ana.id or 0: ana_stats}, []))(
        CustomerQuery(restaurant_id=RESTAURANT)
    )

    assert page.total == 2
    assert [(c.customer.name, c.stats) for c in page.items] == [
        ("Ana", ana_stats),
        ("Luis", CustomerStats()),
    ]


async def test_card_brings_its_latest_orders() -> None:
    repository = InMemoryCustomerRepository()
    ana = await repository.add(Customer(restaurant_id=RESTAURANT, name="Ana"))
    orders = [
        CustomerOrder(i, i, "delivery", "paid", Decimal("20.00"), datetime(2026, 9, i, tzinfo=UTC))
        for i in range(1, 13)
    ]
    history = FakeCustomerHistory({}, orders)

    card = await ReadCustomerCard(repository, history)(RESTAURANT, ana.id or 0)

    assert card.customer.name == "Ana"
    assert card.stats == CustomerStats()
    assert len(card.recent_orders) == RECENT_ORDERS == 10
    assert history.orders_limits == [10]
    with pytest.raises(CustomerNotFoundError):
        await ReadCustomerCard(repository, history)(2, ana.id or 0)
