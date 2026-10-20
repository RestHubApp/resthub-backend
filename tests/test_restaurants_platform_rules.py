"""Límites del restaurante y de las cuentas de la administración del sistema, sin HTTP."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.core.credentials import context_words
from resthub.modules.platform.domain.entities import (
    MAX_FULL_NAME_LENGTH,
    PlatformActivityKind,
    PlatformAdmin,
    validate_new_password,
)
from resthub.modules.platform.domain.exceptions import InvalidAccountDataError
from resthub.modules.restaurants.domain.entities import (
    MAX_NAME_LENGTH,
    Restaurant,
    is_reserved_slug,
)
from resthub.modules.restaurants.domain.exceptions import (
    InvalidDiscountLimitError,
    InvalidRestaurantNameError,
    InvalidSlugError,
    RestaurantNotFoundError,
)
from resthub.modules.restaurants.use_cases.read_restaurant import ReadOwnRestaurant
from resthub.modules.restaurants.use_cases.update_restaurant import (
    UpdateRestaurant,
    UpdateRestaurantCommand,
)

# -- Restaurante -----------------------------------------------------------------------


def test_restaurant_name_has_a_limit() -> None:
    restaurant = Restaurant(name="R" * MAX_NAME_LENGTH, slug="rosa")
    assert len(restaurant.name) == MAX_NAME_LENGTH

    with pytest.raises(InvalidRestaurantNameError) as error:
        restaurant.rename("R" * (MAX_NAME_LENGTH + 1))
    assert str(error.value) == "El nombre del restaurante no puede pasar de 120 caracteres."
    with pytest.raises(InvalidRestaurantNameError) as vacio:
        restaurant.rename("  ")
    assert str(vacio.value) == "El nombre del restaurante no puede quedar vacío."


def test_slug_accepts_sixty_characters() -> None:
    assert Restaurant(name="Rosa", slug="x" * 60).slug == "x" * 60


def test_slug_error_explains_the_format() -> None:
    with pytest.raises(InvalidSlugError) as error:
        Restaurant(name="Rosa", slug="Doña Rosa")
    assert error.value.value == "Doña Rosa"
    assert str(error.value) == (
        "El identificador 'Doña Rosa' no es válido. Usa minúsculas, números y guiones, "
        "sin guiones al principio ni al final."
    )


def test_archiving_deactivates_and_releases_the_slug() -> None:
    restaurant = Restaurant(name="Rosa", slug="rosa")

    restaurant.archive("  Rosa-Archivado-1 ")

    assert (restaurant.slug, restaurant.is_active) == ("rosa-archivado-1", False)


def test_only_the_sandbox_prefix_is_reserved() -> None:
    assert is_reserved_slug("muestra-1")
    assert not is_reserved_slug("la-muestra")


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        ("-0.01", "El descuento máximo del mesero va de 0 a 100 %."),
        ("100.01", "El descuento máximo del mesero va de 0 a 100 %."),
        ("12.345", "El descuento máximo admite como máximo dos decimales."),
    ],
)
def test_waiter_discount_limit_is_validated(limit: str, message: str) -> None:
    restaurant = Restaurant(name="Rosa", slug="rosa")

    with pytest.raises(InvalidDiscountLimitError) as error:
        restaurant.limit_waiter_discount(Decimal(limit))
    assert str(error.value) == message
    assert restaurant.max_waiter_discount_percent == Decimal("10.00")


def test_discount_limit_accepts_zero_and_one_hundred() -> None:
    restaurant = Restaurant(name="Rosa", slug="rosa")

    restaurant.limit_waiter_discount(Decimal(0))
    assert str(restaurant.max_waiter_discount_percent) == "0.00"
    restaurant.limit_waiter_discount(Decimal(100))
    assert restaurant.max_waiter_discount_percent == Decimal("100.00")


class InMemoryRestaurants:
    def __init__(self, *restaurants: Restaurant) -> None:
        self.rows = {restaurant.id: restaurant for restaurant in restaurants}
        self.saved: list[Restaurant] = []

    async def get(self, restaurant_id: int) -> Restaurant | None:
        return self.rows.get(restaurant_id)

    async def save(self, restaurant: Restaurant) -> Restaurant:
        self.saved.append(restaurant)
        return restaurant


class ActivityLog:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


async def test_reading_own_restaurant() -> None:
    own_restaurant = Restaurant(name="Rosa", slug="rosa", id=1)
    read = ReadOwnRestaurant(InMemoryRestaurants(own_restaurant))  # type: ignore[arg-type]

    assert await read(1) is own_restaurant
    with pytest.raises(RestaurantNotFoundError) as error:
        await read(2)
    assert str(error.value) == "No existe el restaurante 2."


async def test_updating_restaurant_changes_only_what_was_sent_and_logs_it() -> None:
    own_restaurant = Restaurant(name="Rosa", slug="rosa", id=1)
    restaurants, activity_log = InMemoryRestaurants(own_restaurant), ActivityLog()
    update = UpdateRestaurant(restaurants, activity_log)  # type: ignore[arg-type]

    saved_restaurant = await update(
        UpdateRestaurantCommand(1, 9, name="  Doña  Rosa ", auto_out_of_stock=False)
    )

    assert (
        saved_restaurant.name,
        saved_restaurant.timezone,
        saved_restaurant.auto_out_of_stock,
    ) == (
        "Doña Rosa",
        "America/Lima",
        False,
    )
    assert activity_log.entries == [
        (
            1,
            9,
            ActivityKind.RESTAURANT_UPDATED,
            "Doña Rosa (America/Lima), descuento del mesero hasta 10.00 %",
        )
    ]
    await update(
        UpdateRestaurantCommand(
            1, 9, timezone="America/Bogota", max_waiter_discount_percent=Decimal(15)
        )
    )
    assert (
        own_restaurant.name,
        own_restaurant.timezone,
        own_restaurant.max_waiter_discount_percent,
    ) == (
        "Doña Rosa",
        "America/Bogota",
        Decimal("15.00"),
    )
    assert own_restaurant.auto_out_of_stock is False
    assert len(restaurants.saved) == 2


async def test_updating_a_missing_restaurant_logs_nothing() -> None:
    activity_log = ActivityLog()

    with pytest.raises(RestaurantNotFoundError):
        await UpdateRestaurant(InMemoryRestaurants(), activity_log)(
            UpdateRestaurantCommand(5, 9, name="X")
        )  # type: ignore[arg-type]
    assert activity_log.entries == []


# -- Administración del sistema ---------------------------------------------------------


def test_platform_account_normalizes_email_and_name() -> None:
    account = PlatformAdmin(
        email="  Soporte@RestHub.Dev ", full_name="  Mesa  de ayuda ", password_hash="h"
    )

    assert (account.email, account.full_name, account.is_active) == (
        "soporte@resthub.dev",
        "Mesa de ayuda",
        True,
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"email": "sin-arroba"}, "El correo electrónico no es válido: 'sin-arroba'"),
        ({"email": "a@dominio"}, "El correo electrónico no es válido: 'a@dominio'"),
        ({"full_name": "   "}, "El nombre no puede quedar vacío."),
        (
            {"full_name": "N" * (MAX_FULL_NAME_LENGTH + 1)},
            "El nombre no puede pasar de 120 caracteres.",
        ),
    ],
)
def test_invalid_platform_account_explains_the_reason(fields: dict[str, str], message: str) -> None:
    all_fields = {"email": "a@b.dev", "full_name": "Ana", "password_hash": "h", **fields}

    with pytest.raises(InvalidAccountDataError) as error:
        PlatformAdmin(**all_fields)  # type: ignore[arg-type]
    assert str(error.value) == message


def test_account_name_accepts_the_maximum() -> None:
    account = PlatformAdmin("a@b.dev", "N" * MAX_FULL_NAME_LENGTH, "h")

    assert len(account.full_name) == MAX_FULL_NAME_LENGTH


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("corta", "La contraseña necesita al menos 10 caracteres."),
        ("x" * 129, "La contraseña no puede pasar de 128 caracteres."),
        (
            "mi-clave-de-resthub",
            (
                "La contraseña no puede llevar tu nombre, tu correo, el nombre del local "
                "ni «RestHub»: se adivina fácil. Elige otra."
            ),
        ),
    ],
)
def test_weak_or_too_long_new_password_is_rejected(password: str, message: str) -> None:
    with pytest.raises(InvalidAccountDataError) as error:
        validate_new_password(password)
    assert str(error.value) == message


def test_valid_password_is_returned_as_is() -> None:
    assert validate_new_password("  diez-caracteres ") == "  diez-caracteres "
    assert validate_new_password("x" * 72) == "x" * 72
    # Sin el tope de 72 bytes de bcrypt: Argon2id acepta tildes y emojis hasta 128 caracteres.
    assert validate_new_password("ñ" * 64) == "ñ" * 64
    assert validate_new_password("🍋" * 128) == "🍋" * 128


def test_platform_password_cannot_carry_the_account_words() -> None:
    context = context_words("lucia.rojas@resthub.dev", "Lucía Rojas")

    with pytest.raises(InvalidAccountDataError, match="tu nombre"):
        validate_new_password("Rojas-del-equipo-2026", context)
    assert validate_new_password("cielo-gris-sobre-lima", context)


def test_each_platform_action_has_its_phrase() -> None:
    assert [k.label for k in PlatformActivityKind] == [
        "Inició sesión",
        "Dio de alta un restaurante",
        "Editó un restaurante",
        "Agregó un encargado",
        "Restableció la contraseña de un encargado",
        "Reinició el local de muestra",
        "Abrió la vista previa",
        "Cambió su contraseña",
    ]
