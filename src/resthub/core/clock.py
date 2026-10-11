"""La hora actual, por un único puerto.

Leer el reloj directo (`datetime.now`, `date.today`, `time.time`) reparte la
hora por todo el código y obliga a las pruebas a esquivarla con fechas lejanas.
La hora entra por `Clock`: los casos de uso y los servicios lo reciben por la
raíz de composición y las pruebas lo reemplazan por uno fijo.

Este es el único archivo que lee el reloj del sistema (lo vigila la regla
`TID251` de ruff, ver `pyproject.toml`). Para medir duraciones se usa un reloj
monotónico (`time.perf_counter`, `time.monotonic`): no es «la hora actual».

Python puro: lo importan también el dominio y los casos de uso.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """El instante actual en UTC, siempre con `tzinfo`."""
        ...


class SystemClock:
    """El reloj del sistema operativo."""

    def now(self) -> datetime:
        return datetime.now(UTC)


_SYSTEM_CLOCK = SystemClock()


def utc_now() -> datetime:
    """La hora del sistema, para el valor por omisión de una marca de creación.

    Una entidad o una fila que no recibe la hora la toma de aquí. Quien decide
    cuándo ocurrió algo (un caso de uso) la pide a su `Clock` y la pasa.
    """
    return _SYSTEM_CLOCK.now()
