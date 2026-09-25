"""Lector hacia datos que posee `restaurants`.

La sesión de una cuenta muestra el nombre de su restaurante, y el acceso se
niega si el restaurante está desactivado. `accounts` no puede importar
`restaurants` -son dos módulos de dominio independientes-, así que la pregunta
se declara acá como puerto y un adaptador la responde leyendo la tabla ajena.
Se lee, nunca se escribe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RestaurantSummary:
    id: int
    name: str
    slug: str
    # Nombre IANA ("America/Lima"). La interfaz la necesita para mostrar horas
    # y días del local, que no son los del navegador ni los de UTC.
    timezone: str
    is_active: bool


class RestaurantDirectory(Protocol):
    async def get(self, restaurant_id: int) -> RestaurantSummary | None: ...
