"""Límites del restaurante y de las cuentas de la administración del sistema, sin HTTP."""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.core.activity import ActivityKind
from resthub.modules.platform.domain.entities import (
    MAX_FULL_NAME_LENGTH,
    PlatformActivityKind,
    PlatformAdmin,
    validate_new_password,
)
from resthub.modules.platform.domain.exceptions import InvalidAccountData
from resthub.modules.restaurants.domain.entities import (
    MAX_NAME_LENGTH,
    Restaurant,
    is_reserved_slug,
)
from resthub.modules.restaurants.domain.exceptions import (
    InvalidDiscountLimit,
    InvalidRestaurantName,
    InvalidSlug,
    RestaurantNotFound,
)
from resthub.modules.restaurants.use_cases.read_restaurant import ReadOwnRestaurant
from resthub.modules.restaurants.use_cases.update_restaurant import (
    UpdateRestaurant,
    UpdateRestaurantCommand,
)

# -- Restaurante -----------------------------------------------------------------------


def test_el_nombre_del_restaurante_tiene_tope() -> None:
    local = Restaurant(name="R" * MAX_NAME_LENGTH, slug="rosa")
    assert len(local.name) == MAX_NAME_LENGTH

    with pytest.raises(InvalidRestaurantName) as error:
        local.rename("R" * (MAX_NAME_LENGTH + 1))
    assert str(error.value) == "El nombre del restaurante no puede pasar de 120 caracteres."
    with pytest.raises(InvalidRestaurantName) as vacio:
        local.rename("  ")
    assert str(vacio.value) == "El nombre del restaurante no puede quedar vacío."


def test_el_identificador_admite_sesenta_caracteres() -> None:
    assert Restaurant(name="Rosa", slug="x" * 60).slug == "x" * 60


def test_el_error_del_identificador_explica_la_forma() -> None:
    with pytest.raises(InvalidSlug) as error:
        Restaurant(name="Rosa", slug="Doña Rosa")
    assert error.value.value == "Doña Rosa"
    assert str(error.value) == (
        "El identificador 'Doña Rosa' no es válido. Usa minúsculas, números y guiones, "
        "sin guiones al principio ni al final."
    )


def test_archivar_desactiva_y_libera_el_identificador() -> None:
    local = Restaurant(name="Rosa", slug="rosa")

    local.archive("  Rosa-Archivado-1 ")

    assert (local.slug, local.is_active) == ("rosa-archivado-1", False)


def test_solo_el_prefijo_de_muestra_esta_reservado() -> None:
    assert is_reserved_slug("muestra-1")
    assert not is_reserved_slug("la-muestra")


@pytest.mark.parametrize(
    ("tope", "mensaje"),
    [
        ("-0.01", "El descuento máximo del mesero va de 0 a 100 %."),
        ("100.01", "El descuento máximo del mesero va de 0 a 100 %."),
        ("12.345", "El descuento máximo admite como máximo dos decimales."),
    ],
)
def test_el_tope_de_descuento_del_mesero_se_valida(tope: str, mensaje: str) -> None:
    local = Restaurant(name="Rosa", slug="rosa")

    with pytest.raises(InvalidDiscountLimit) as error:
        local.limit_waiter_discount(Decimal(tope))
    assert str(error.value) == mensaje
    assert local.max_waiter_discount_percent == Decimal("10.00")


def test_el_tope_de_descuento_acepta_cero_y_cien() -> None:
    local = Restaurant(name="Rosa", slug="rosa")

    local.limit_waiter_discount(Decimal("0"))
    assert str(local.max_waiter_discount_percent) == "0.00"
    local.limit_waiter_discount(Decimal("100"))
    assert local.max_waiter_discount_percent == Decimal("100.00")


class Locales:
    def __init__(self, *locales: Restaurant) -> None:
        self.rows = {local.id: local for local in locales}
        self.guardados: list[Restaurant] = []

    async def get(self, restaurant_id: int) -> Restaurant | None:
        return self.rows.get(restaurant_id)

    async def save(self, restaurant: Restaurant) -> Restaurant:
        self.guardados.append(restaurant)
        return restaurant


class Bitacora:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, ActivityKind, str]] = []

    async def record(
        self, restaurant_id: int, user_id: int, kind: ActivityKind, detail: str = ""
    ) -> None:
        self.entries.append((restaurant_id, user_id, kind, detail))


async def test_leer_el_propio_restaurante() -> None:
    rosa = Restaurant(name="Rosa", slug="rosa", id=1)
    leer = ReadOwnRestaurant(Locales(rosa))  # type: ignore[arg-type]

    assert await leer(1) is rosa
    with pytest.raises(RestaurantNotFound) as error:
        await leer(2)
    assert str(error.value) == "No existe el restaurante 2."


async def test_editar_el_restaurante_cambia_solo_lo_enviado_y_lo_anota() -> None:
    rosa = Restaurant(name="Rosa", slug="rosa", id=1)
    locales, bitacora = Locales(rosa), Bitacora()
    editar = UpdateRestaurant(locales, bitacora)  # type: ignore[arg-type]

    guardado = await editar(
        UpdateRestaurantCommand(1, 9, name="  Doña  Rosa ", auto_out_of_stock=False)
    )

    assert (guardado.name, guardado.timezone, guardado.auto_out_of_stock) == (
        "Doña Rosa",
        "America/Lima",
        False,
    )
    assert bitacora.entries == [
        (
            1,
            9,
            ActivityKind.RESTAURANT_UPDATED,
            "Doña Rosa (America/Lima), descuento del mesero hasta 10.00 %",
        )
    ]
    await editar(
        UpdateRestaurantCommand(
            1, 9, timezone="America/Bogota", max_waiter_discount_percent=Decimal("15")
        )
    )
    assert (rosa.name, rosa.timezone, rosa.max_waiter_discount_percent) == (
        "Doña Rosa",
        "America/Bogota",
        Decimal("15.00"),
    )
    assert rosa.auto_out_of_stock is False
    assert len(locales.guardados) == 2


async def test_editar_un_restaurante_que_no_existe_no_anota_nada() -> None:
    bitacora = Bitacora()

    with pytest.raises(RestaurantNotFound):
        await UpdateRestaurant(Locales(), bitacora)(UpdateRestaurantCommand(5, 9, name="X"))  # type: ignore[arg-type]
    assert bitacora.entries == []


# -- Administración del sistema ---------------------------------------------------------


def test_la_cuenta_de_plataforma_normaliza_correo_y_nombre() -> None:
    cuenta = PlatformAdmin(
        email="  Soporte@RestHub.Dev ", full_name="  Mesa  de ayuda ", password_hash="h"
    )

    assert (cuenta.email, cuenta.full_name, cuenta.is_active) == (
        "soporte@resthub.dev",
        "Mesa de ayuda",
        True,
    )


@pytest.mark.parametrize(
    ("datos", "mensaje"),
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
def test_una_cuenta_de_plataforma_invalida_explica_el_motivo(
    datos: dict[str, str], mensaje: str
) -> None:
    campos = {"email": "a@b.dev", "full_name": "Ana", "password_hash": "h", **datos}

    with pytest.raises(InvalidAccountData) as error:
        PlatformAdmin(**campos)  # type: ignore[arg-type]
    assert str(error.value) == mensaje


def test_el_nombre_de_la_cuenta_admite_el_maximo() -> None:
    cuenta = PlatformAdmin("a@b.dev", "N" * MAX_FULL_NAME_LENGTH, "h")

    assert len(cuenta.full_name) == MAX_FULL_NAME_LENGTH


@pytest.mark.parametrize(
    ("clave", "mensaje"),
    [
        ("corta", "La contraseña necesita al menos 10 caracteres."),
        ("x" * 129, "La contraseña no puede pasar de 128 caracteres."),
        (
            "ñ" * 40,
            "La contraseña es demasiado larga: hasta 72 caracteres, "
            "menos si lleva tildes o emojis.",
        ),
    ],
)
def test_una_contrasena_nueva_debil_o_larga_se_rechaza(clave: str, mensaje: str) -> None:
    with pytest.raises(InvalidAccountData) as error:
        validate_new_password(clave)
    assert str(error.value) == mensaje


def test_una_contrasena_valida_se_devuelve_tal_cual() -> None:
    assert validate_new_password("  diez-caracteres ") == "  diez-caracteres "
    assert validate_new_password("x" * 72) == "x" * 72


def test_cada_accion_de_la_plataforma_tiene_su_frase() -> None:
    assert [k.label for k in PlatformActivityKind] == [
        "Inició sesión",
        "Dio de alta un restaurante",
        "Editó un restaurante",
        "Agregó un encargado",
        "Reinició el local de muestra",
        "Abrió la vista previa",
    ]
