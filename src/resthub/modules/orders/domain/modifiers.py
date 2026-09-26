"""Las opciones que se eligen al pedir un plato: tamaño, término, extras.

Python puro. La carta (módulo `menu`) define los grupos; acá se valida lo que
eligió el mesero contra ellos y se congela en el ítem, igual que el nombre y
el precio: si mañana el «familiar» sube, el pedido de hoy no cambia.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from resthub.modules.orders.domain.exceptions import InvalidOrder

MAX_CHOSEN = 40


@dataclass(frozen=True, slots=True)
class DishOption:
    name: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class DishOptionGroup:
    name: str
    options: tuple[DishOption, ...]
    min_choices: int = 0
    max_choices: int = 1


@dataclass(frozen=True, slots=True)
class ChosenModifier:
    """Una opción elegida, con lo que sumaba al precio en ese momento."""

    group: str
    option: str
    price: Decimal


def choose_modifiers(
    dish_name: str,
    groups: Sequence[DishOptionGroup],
    selections: Sequence[tuple[str, str]],
) -> tuple[ChosenModifier, ...]:
    """Valida lo elegido contra los grupos del plato y lo devuelve congelado.

    Cada grupo tiene que quedar entre su mínimo y su máximo; una opción que no
    existe (la carta cambió en el celular del mesero) se rechaza.
    """
    if len(selections) > MAX_CHOSEN:
        raise InvalidOrder(f"Demasiadas opciones para {dish_name}.")
    by_name = {group.name.casefold(): group for group in groups}
    chosen: list[ChosenModifier] = []
    counts: dict[str, int] = {}
    for group_name, option_name in selections:
        group = by_name.get(group_name.casefold())
        if group is None:
            raise InvalidOrder(f"{dish_name} no tiene la opción «{group_name}».")
        option = next(
            (o for o in group.options if o.name.casefold() == option_name.casefold()), None
        )
        if option is None:
            raise InvalidOrder(f"«{option_name}» no es una opción de {group.name}.")
        if any(c.group == group.name and c.option == option.name for c in chosen):
            raise InvalidOrder(f"«{option.name}» se eligió dos veces.")
        counts[group.name] = counts.get(group.name, 0) + 1
        chosen.append(ChosenModifier(group=group.name, option=option.name, price=option.price))
    for group in groups:
        count = counts.get(group.name, 0)
        if count < group.min_choices:
            raise InvalidOrder(f"Elige {group.name.lower()} para {dish_name}.")
        if count > group.max_choices:
            raise InvalidOrder(
                f"En {group.name.lower()} se eligen como máximo {group.max_choices}."
            )
    return tuple(chosen)
