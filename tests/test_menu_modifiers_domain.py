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
from resthub.modules.menu.domain.exceptions import InvalidMenuCategoryError, InvalidMenuItemError
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


def _group(
    name: str = "Tamaño", *options: str, min_choices: int = 0, max_choices: int = 1
) -> ModifierGroup:
    return ModifierGroup(
        name=name,
        options=tuple(ModifierOption(name=o) for o in (options or ("Personal", "Familiar"))),
        min_choices=min_choices,
        max_choices=max_choices,
    )


# -- Nombres, descripción y precio ------------------------------------------------


def test_category_name_accepts_exactly_the_maximum() -> None:
    assert len(MenuCategory(1, "C" * MAX_CATEGORY_NAME_LENGTH).name) == MAX_CATEGORY_NAME_LENGTH

    with pytest.raises(InvalidMenuCategoryError) as error:
        MenuCategory(1, "C" * (MAX_CATEGORY_NAME_LENGTH + 1))
    assert error.value.reason == "El nombre de la categoría no puede pasar de 60 caracteres."


def test_renaming_a_category_trims_spaces_and_validates() -> None:
    category = MenuCategory(1, "Fondos")

    category.rename("  Platos   de fondo ")
    assert category.name == "Platos de fondo"

    with pytest.raises(InvalidMenuCategoryError) as error:
        category.rename("   ")
    assert error.value.reason == "El nombre de la categoría no puede quedar vacío."
    assert category.name == "Platos de fondo"


def test_dish_name_is_not_empty_nor_over_the_maximum() -> None:
    assert len(_item(name="P" * MAX_ITEM_NAME_LENGTH).name) == MAX_ITEM_NAME_LENGTH

    with pytest.raises(InvalidMenuItemError) as empty:
        _item(name=" \t ")
    assert empty.value.reason == "El nombre del plato no puede quedar vacío."
    with pytest.raises(InvalidMenuItemError) as too_long:
        _item().rename("P" * (MAX_ITEM_NAME_LENGTH + 1))
    assert too_long.value.reason == "El nombre del plato no puede pasar de 120 caracteres."


def test_description_is_trimmed_and_capped() -> None:
    dish = _item(description="  Con papas fritas  ")
    assert dish.description == "Con papas fritas"

    dish.describe("D" * MAX_DESCRIPTION_LENGTH)
    assert len(dish.description) == MAX_DESCRIPTION_LENGTH
    with pytest.raises(InvalidMenuItemError) as error:
        dish.describe("D" * (MAX_DESCRIPTION_LENGTH + 1))
    assert error.value.reason == "La descripción no puede pasar de 300 caracteres."


@pytest.mark.parametrize(
    ("price", "message"),
    [
        ("NaN", "El precio admite como máximo dos decimales."),
        ("Infinity", "El precio admite como máximo dos decimales."),
        ("12.345", "El precio admite como máximo dos decimales."),
        ("0.00", "El precio tiene que ser mayor que cero."),
        ("-0.01", "El precio tiene que ser mayor que cero."),
        ("100000000.00", "El precio es demasiado alto."),
    ],
)
def test_out_of_range_price_explains_the_reason(price: str, message: str) -> None:
    with pytest.raises(InvalidMenuItemError) as error:
        _item().reprice(Decimal(price))
    assert error.value.reason == message


def test_boundary_prices_are_accepted_with_two_decimals() -> None:
    dish = _item()

    dish.reprice(Decimal("0.01"))
    assert dish.price == Decimal("0.01")
    dish.reprice(Decimal("99999999.99"))
    assert dish.price == Decimal("99999999.99")
    dish.reprice(Decimal("7.5"))
    assert str(dish.price) == "7.50"


def test_dish_out_of_stock_cannot_be_ordered() -> None:
    dish = _item()
    dish.out_of_stock = True

    assert not dish.can_be_ordered


def test_reordering_dishes_returns_the_same_list_in_another_order() -> None:
    dishes = [_item(name=f"Plato {i}", id=i) for i in (10, 20)]

    reordered = reorder(dishes, [20, 10])

    assert [p.id for p in reordered] == [20, 10]
    assert [p.position for p in reordered] == [0, 1]
    assert reordered[0] is dishes[1]


# -- Grupos de opciones -------------------------------------------------------------


def test_valid_group_is_cleaned_and_has_cents() -> None:
    groups = validate_modifier_groups(
        [
            ModifierGroup(
                name="  Término  de la carne ",
                options=(ModifierOption(" Tres  cuartos "), ModifierOption("Bien", Decimal(2))),
                min_choices=1,
                max_choices=2,
            )
        ]
    )

    (group,) = groups
    assert group.name == "Término de la carne"
    assert [o.name for o in group.options] == ["Tres cuartos", "Bien"]
    assert [str(o.price) for o in group.options] == ["0.00", "2.00"]
    assert (group.min_choices, group.max_choices) == (1, 2)
    assert group.is_required


def test_group_without_minimum_is_optional() -> None:
    assert not _group(min_choices=0).is_required
    assert _group(min_choices=1).is_required


def test_dish_validates_its_groups_on_creation_and_on_change() -> None:
    dish = _item(modifier_groups=(_group(" Tamaño "),))
    assert dish.modifier_groups[0].name == "Tamaño"

    dish.set_modifiers([_group("Extras", "Queso", "Huevo", max_choices=2)])
    assert [g.name for g in dish.modifier_groups] == ["Extras"]
    with pytest.raises(InvalidMenuItemError):
        dish.set_modifiers([_group("Tamaño"), _group("tamaño")])


@pytest.mark.parametrize(
    ("group", "message"),
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
def test_malformed_group_is_rejected_with_its_reason(group: ModifierGroup, message: str) -> None:
    with pytest.raises(InvalidMenuItemError) as error:
        validate_modifier_groups([group])
    assert error.value.reason == message


def test_exact_group_limits_are_accepted() -> None:
    name = "G" * MAX_MODIFIER_NAME_LENGTH
    options = [f"Opción {i}" for i in range(MAX_OPTIONS)]

    (group,) = validate_modifier_groups(
        [_group(name, *options, min_choices=MAX_OPTIONS, max_choices=MAX_OPTIONS)]
    )

    assert group.name == name
    assert len(group.options) == MAX_OPTIONS
    assert group.min_choices == group.max_choices == MAX_OPTIONS


@pytest.mark.parametrize(
    ("price", "message"),
    [
        ("1.005", "El precio de «Familiar» admite como máximo dos decimales."),
        ("NaN", "El precio de «Familiar» admite como máximo dos decimales."),
        ("-0.01", "El precio de «Familiar» va de 0 a 9999.99."),
        ("10000.00", "El precio de «Familiar» va de 0 a 9999.99."),
    ],
)
def test_option_price_ranges_from_zero_to_the_cap(price: str, message: str) -> None:
    group = ModifierGroup("Tamaño", (ModifierOption("Familiar", Decimal(price)),))

    with pytest.raises(InvalidMenuItemError) as error:
        validate_modifier_groups([group])
    assert error.value.reason == message


def test_option_can_cost_zero_or_the_cap() -> None:
    group = ModifierGroup(
        "Tamaño",
        (
            ModifierOption("Sin cebolla", Decimal(0)),
            ModifierOption("Familiar", Decimal("9999.99")),
        ),
    )

    (cleaned,) = validate_modifier_groups([group])

    assert [o.price for o in cleaned.options] == [Decimal("0.00"), Decimal("9999.99")]


def test_dish_accepts_up_to_eight_groups_with_distinct_names() -> None:
    eight = [_group(f"Grupo {i}") for i in range(MAX_GROUPS)]
    assert len(validate_modifier_groups(eight)) == MAX_GROUPS

    with pytest.raises(InvalidMenuItemError) as too_many:
        validate_modifier_groups([*eight, _group("Uno más")])
    assert too_many.value.reason == "Un plato admite hasta 8 grupos de opciones."
    with pytest.raises(InvalidMenuItemError) as duplicates:
        validate_modifier_groups([_group("Extras"), _group(" EXTRAS ")])
    assert duplicates.value.reason == "Dos grupos de opciones del plato se llaman igual."
