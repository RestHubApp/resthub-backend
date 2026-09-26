"""Paginación compartida.

Una página es la misma forma en cualquier módulo, así que el tipo vive en el
núcleo. Si lo poseyera un módulo de dominio, el resto tendría que importarlo y
los módulos dejarían de ser independientes.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
# Más allá, un desplazamiento no apunta a nada real y en PostgreSQL desborda el entero.
MAX_OFFSET = 1_000_000


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0
