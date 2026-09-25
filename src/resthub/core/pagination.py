"""Paginación compartida.

Una página es la misma forma en cualquier módulo, así que el tipo vive en el
núcleo. Si lo poseyera un módulo de dominio, el resto tendría que importarlo y
los módulos dejarían de ser independientes.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0
