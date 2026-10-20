"""Búsqueda por texto con `LIKE` sin comodines de quien escribe.

En `LIKE`, `%` y `_` son comodines: buscar «50%» o «a_b» encontraría más de lo escrito, y un
`%` suelto devolvería la lista entera. El texto se escapa con `escape_like` y la consulta
declara el mismo carácter de escape (`column.ilike(pattern, escape=LIKE_ESCAPE)`), igual en
PostgreSQL y en SQLite.

Puro a propósito (solo texto): lo usan los adaptadores de persistencia de cualquier módulo.
"""

from __future__ import annotations

LIKE_ESCAPE = "\\"


def escape_like(text: str) -> str:
    """El texto con la barra invertida, `%` y `_` escapados: en `LIKE` vale lo que dice."""
    return (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )


def contains_pattern(text: str) -> str:
    """El patrón de `LIKE` para «contiene este texto», ya escapado."""
    return f"%{escape_like(text)}%"
