from __future__ import annotations

from datetime import UTC, datetime


def as_utc(moment: datetime) -> datetime:
    """Devuelve la fecha con su zona horaria puesta.

    SQLite no almacena la zona, asi que una fecha guardada con UTC vuelve
    ingenua. Sin esta correccion el API serializa la marca sin desplazamiento y
    el navegador la interpreta como hora local: la fecha se corre tantas horas
    como diga el reloj de quien mira. Y solo pasa en local, porque PostgreSQL
    si conserva la zona, que es la forma mas cara de encontrar el error.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
