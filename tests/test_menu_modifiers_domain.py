"""Validaciones del menú que no pasan por HTTP: nombres, precios y opciones de un plato.

Cada caso fija un límite exacto (el último valor que se acepta y el primero que
se rechaza) y el mensaje que ve el encargado, porque ese texto es lo que le
explica qué corregir.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from resthub.modules.menu.domain.entities import (
    MAX_CATEGORY_NAME_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_ITEM_NAME_LENGTH,
    MenuCategory,
    MenuItem,
    reorder,
)
from resthub.modules.menu.domain.exceptions import InvalidMenuCategory, InvalidMenuItem
from resthub.modules.menu.domain.modifiers import (
    MAX_GROUPS,
    MAX_MODIFIER_NAME_LENGTH,
    MAX_OPTIONS,
    ModifierGroup,
    ModifierOption,
    validate_modifier_groups,
)


def _item(**overrides: object) -> MenuItem:
    fields: dict[str, object] = {
        "restaurant_id": 1,
        "category_id": 1,
        "name": "Lomo saltado",
        "price": Decimal("28.00"),
    }
    fields.update(overrides)
    return MenuItem(**fields)  # type: ignore[arg-type]


def _group(name: str = "Tamaño", *options: str, min_choices: int = 0, max_choices: int = 1):
    return ModifierGroup(
        name=name,
        options=tuple(ModifierOption(name=o) for o in (options or ("Personal", "Familiar"))),
        min_choices=min_choices,
        max_choices=max_choices,
    )


# -- Nombres, descripción y precio ------------------------------------------------


def test_el_nombre_de_la_categoria_admite_justo_el_maximo() -> None:
    assert len(MenuCategory(1, "C" * MAX_CATEGORY_NAME_LENGTH).name) == MAX_CATEGORY_NAME_LENGTH

    with pytest.raises(InvalidMenuCategory) as error:
        MenuCategory(1, "C" * (MAX_CATEGORY_NAME_LENGTH + 1))
    assert error.value.reason == "El nombre de la categoría no puede pasar de 60 caracteres."


def test_renombrar_una_categoria_limpia_los_espacios_y_valida() -> None:
    categoria = MenuCategory(1, "Fondos")

    categoria.rename("  Platos   de fondo ")
    assert categoria.name == "Platos de fondo"

    with pytest.raises(InvalidMenuCategory) as error:
        categoria.rename("   ")
    assert error.value.reason == "El nombre de la categoría no puede quedar vacío."
    assert categoria.name == "Platos de fondo"


def test_el_nombre_del_plato_no_queda_vacio_ni_pasa_del_maximo() -> None:
    assert len(_item(name="P" * MAX_ITEM_NAME_LENGTH).name) == MAX_ITEM_NAME_LENGTH

    with pytest.raises(InvalidMenuItem) as vacio:
        _item(name=" \t ")
    assert vacio.value.reason == "El nombre del plato no puede quedar vacío."
    with pytest.raises(InvalidMenuItem) as largo:
        _item().rename("P" * (MAX_ITEM_NAME_LENGTH + 1))
    assert largo.value.reason == "El nombre del plato no puede pasar de 120 caracteres."


def test_la_descripcion_se_recorta_y_tiene_tope() -> None:
    plato = _item(description="  Con papas fritas  ")
    assert plato.description == "Con papas fritas"

    plato.describe("D" * MAX_DESCRIPTION_LENGTH)
    assert len(plato.description) == MAX_DESCRIPTION_LENGTH
    with pytest.raises(InvalidMenuItem) as error:
        plato.describe("D" * (MAX_DESCRIPTION_LENGTH + 1))
    assert error.value.reason == "La descripción no puede pasar de 300 caracteres."


@pytest.mark.parametrize(
    ("precio", "mensaje"),
    [
        ("NaN", "El precio admite como máximo dos decimales."),
        ("Infinity", "El precio admite como máximo dos decimales."),
        ("12.345", "El precio admite como máximo dos decimales."),
        ("0.00", "El precio tiene que ser mayor que cero."),
        ("-0.01", "El precio tiene que ser mayor que cero."),
        ("100000000.00", "El precio es demasiado alto."),
    ],
)
def test_un_precio_fuera_de_rango_explica_el_motivo(precio: str, mensaje: str) -> None:
    with pytest.raises(InvalidMenuItem) as error:
        _item().reprice(Decimal(precio))
    assert error.value.reason == mensaje


def test_los_precios_limite_se_aceptan_con_dos_decimales() -> None:
    plato = _item()

    plato.reprice(Decimal("0.01"))
    assert plato.price == Decimal("0.01")
    plato.reprice(Decimal("99999999.99"))
    assert plato.price == Decimal("99999999.99")
    plato.reprice(Decimal("7.5"))
    assert str(plato.price) == "7.50"


def test_un_plato_agotado_por_stock_no_se_puede_pedir() -> None:
    plato = _item()
    plato.out_of_stock = True

    assert not plato.can_be_ordered


def test_reordenar_platos_devuelve_la_misma_lista_en_otro_orden() -> None:
    platos = [_item(name=f"Plato {i}", id=i) for i in (10, 20)]

    ordenados = reorder(platos, [20, 10])

    assert [p.id for p in ordenados] == [20, 10]
    assert [p.position for p in ordenados] == [0, 1]
    assert ordenados[0] is platos[1]


# -- Grupos de opciones -------------------------------------------------------------


def test_un_grupo_valido_queda_limpio_y_con_centimos() -> None:
    grupos = validate_modifier_groups(
        [
            ModifierGroup(
                name="  Término  de la carne ",
                options=(ModifierOption(" Tres  cuartos "), ModifierOption("Bien", Decimal("2"))),
                min_choices=1,
                max_choices=2,
            )
        ]
    )

    (grupo,) = grupos
    assert grupo.name == "Término de la carne"
    assert [o.name for o in grupo.options] == ["Tres cuartos", "Bien"]
    assert [str(o.price) for o in grupo.options] == ["0.00", "2.00"]
    assert (grupo.min_choices, grupo.max_choices) == (1, 2)
    assert grupo.is_required


def test_un_grupo_sin_minimo_es_opcional() -> None:
    assert not _group(min_choices=0).is_required
    assert _group(min_choices=1).is_required


def test_el_plato_valida_sus_grupos_al_crearse_y_al_cambiarlos() -> None:
    plato = _item(modifier_groups=(_group(" Tamaño "),))
    assert plato.modifier_groups[0].name == "Tamaño"

    plato.set_modifiers([_group("Extras", "Queso", "Huevo", max_choices=2)])
    assert [g.name for g in plato.modifier_groups] == ["Extras"]
    with pytest.raises(InvalidMenuItem):
        plato.set_modifiers([_group("Tamaño"), _group("tamaño")])


@pytest.mark.parametrize(
    ("grupo", "mensaje"),
    [
        (_group("   "), "Un grupo de opciones necesita un nombre."),
        (
            _group("G" * (MAX_MODIFIER_NAME_LENGTH + 1)),
            "El nombre de un grupo de opciones admite 40 caracteres.",
        ),
        (_group("Tamaño", " "), "Una opción necesita un nombre."),
        (
            _group("Tamaño", "O" * (MAX_MODIFIER_NAME_LENGTH + 1)),
            "El nombre de una opción admite 40 caracteres.",
        ),
        (ModifierGroup("Tamaño", ()), "«Tamaño» necesita de 1 a 20 opciones."),
        (
            _group("Tamaño", *(f"Opción {i}" for i in range(MAX_OPTIONS + 1))),
            "«Tamaño» necesita de 1 a 20 opciones.",
        ),
        (_group("Tamaño", "Grande", "grande"), "«Tamaño» repite una opción."),
        (
            _group(min_choices=-1),
            "En «Tamaño», el mínimo y el máximo de opciones no cuadran.",
        ),
        (
            _group(min_choices=0, max_choices=0),
            "En «Tamaño», el mínimo y el máximo de opciones no cuadran.",
        ),
        (
            _group(min_choices=2, max_choices=1),
            "En «Tamaño», el mínimo y el máximo de opciones no cuadran.",
        ),
        (
            _group(min_choices=1, max_choices=3),
            "En «Tamaño», el máximo pasa de la cantidad de opciones.",
        ),
    ],
)
def test_un_grupo_mal_armado_se_rechaza_con_su_motivo(grupo: ModifierGroup, mensaje: str) -> None:
    with pytest.raises(InvalidMenuItem) as error:
        validate_modifier_groups([grupo])
    assert error.value.reason == mensaje


def test_los_limites_exactos_de_un_grupo_se_aceptan() -> None:
    nombre = "G" * MAX_MODIFIER_NAME_LENGTH
    opciones = [f"Opción {i}" for i in range(MAX_OPTIONS)]

    (grupo,) = validate_modifier_groups(
        [_group(nombre, *opciones, min_choices=MAX_OPTIONS, max_choices=MAX_OPTIONS)]
    )

    assert grupo.name == nombre
    assert len(grupo.options) == MAX_OPTIONS
    assert grupo.min_choices == grupo.max_choices == MAX_OPTIONS


@pytest.mark.parametrize(
    ("precio", "mensaje"),
    [
        ("1.005", "El precio de «Familiar» admite como máximo dos decimales."),
        ("NaN", "El precio de «Familiar» admite como máximo dos decimales."),
        ("-0.01", "El precio de «Familiar» va de 0 a 9999.99."),
        ("10000.00", "El precio de «Familiar» va de 0 a 9999.99."),
    ],
)
def test_el_precio_de_una_opcion_va_de_cero_al_tope(precio: str, mensaje: str) -> None:
    grupo = ModifierGroup("Tamaño", (ModifierOption("Familiar", Decimal(precio)),))

    with pytest.raises(InvalidMenuItem) as error:
        validate_modifier_groups([grupo])
    assert error.value.reason == mensaje


def test_una_opcion_puede_costar_cero_o_el_tope() -> None:
    grupo = ModifierGroup(
        "Tamaño",
        (
            ModifierOption("Sin cebolla", Decimal("0")),
            ModifierOption("Familiar", Decimal("9999.99")),
        ),
    )

    (limpio,) = validate_modifier_groups([grupo])

    assert [o.price for o in limpio.options] == [Decimal("0.00"), Decimal("9999.99")]


def test_un_plato_admite_hasta_ocho_grupos_con_nombres_distintos() -> None:
    ocho = [_group(f"Grupo {i}") for i in range(MAX_GROUPS)]
    assert len(validate_modifier_groups(ocho)) == MAX_GROUPS

    with pytest.raises(InvalidMenuItem) as muchos:
        validate_modifier_groups([*ocho, _group("Uno más")])
    assert muchos.value.reason == "Un plato admite hasta 8 grupos de opciones."
    with pytest.raises(InvalidMenuItem) as repetidos:
        validate_modifier_groups([_group("Extras"), _group(" EXTRAS ")])
    assert repetidos.value.reason == "Dos grupos de opciones del plato se llaman igual."
