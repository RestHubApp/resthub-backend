"""Opciones y extras de un plato: tamaño, término, «sin cebolla», adicionales.

Python puro. Un plato tiene grupos de opciones; cada grupo dice cuántas se
pueden elegir (`min_choices` a `max_choices`) y cada opción, cuánto suma al
precio (cero para «sin cebolla», S/ 10 para «familiar»). Un grupo con mínimo
uno es obligatorio: el mesero no puede enviar el plato sin elegir.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from resthub.modules.menu.domain.exceptions import InvalidMenuItem

MAX_GROUPS = 8
MAX_OPTIONS = 20
MAX_MODIFIER_NAME_LENGTH = 40
MAX_OPTION_PRICE = Decimal("9999.99")
_CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ModifierOption:
    name: str
    # Lo que suma al precio del plato; cero si no cambia el precio.
    price: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class ModifierGroup:
    name: str
    options: tuple[ModifierOption, ...]
    min_choices: int = 0
    max_choices: int = 1

    @property
    def is_required(self) -> bool:
        return self.min_choices > 0


def _clean_name(raw: str, what: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidMenuItem(f"{what} necesita un nombre.")
    if len(name) > MAX_MODIFIER_NAME_LENGTH:
        raise InvalidMenuItem(
            f"El nombre de {what.lower()} admite {MAX_MODIFIER_NAME_LENGTH} caracteres."
        )
    return name


def _clean_option(option: ModifierOption) -> ModifierOption:
    name = _clean_name(option.name, "Una opción")
    price = Decimal(option.price)
    if not price.is_finite() or price != price.quantize(_CENT):
        raise InvalidMenuItem(f"El precio de «{name}» admite como máximo dos decimales.")
    if price < 0 or price > MAX_OPTION_PRICE:
        raise InvalidMenuItem(f"El precio de «{name}» va de 0 a {MAX_OPTION_PRICE}.")
    return ModifierOption(name=name, price=price.quantize(_CENT))


def _clean_group(group: ModifierGroup) -> ModifierGroup:
    name = _clean_name(group.name, "Un grupo de opciones")
    options = tuple(_clean_option(option) for option in group.options)
    if not options or len(options) > MAX_OPTIONS:
        raise InvalidMenuItem(f"«{name}» necesita de 1 a {MAX_OPTIONS} opciones.")
    names = [option.name.casefold() for option in options]
    if len(set(names)) != len(names):
        raise InvalidMenuItem(f"«{name}» repite una opción.")
    if group.min_choices < 0 or group.max_choices < 1 or group.min_choices > group.max_choices:
        raise InvalidMenuItem(f"En «{name}», el mínimo y el máximo de opciones no cuadran.")
    if group.max_choices > len(options):
        raise InvalidMenuItem(f"En «{name}», el máximo pasa de la cantidad de opciones.")
    return ModifierGroup(
        name=name, options=options, min_choices=group.min_choices, max_choices=group.max_choices
    )


def validate_modifier_groups(groups: Iterable[ModifierGroup]) -> tuple[ModifierGroup, ...]:
    cleaned = tuple(_clean_group(group) for group in groups)
    if len(cleaned) > MAX_GROUPS:
        raise InvalidMenuItem(f"Un plato admite hasta {MAX_GROUPS} grupos de opciones.")
    names = [group.name.casefold() for group in cleaned]
    if len(set(names)) != len(names):
        raise InvalidMenuItem("Dos grupos de opciones del plato se llaman igual.")
    return cleaned
