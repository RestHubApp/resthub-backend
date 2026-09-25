"""La hora del restaurante.

Cada restaurante declara su zona horaria (por omisión, la de Lima). "Las ventas
de hoy" o "el correlativo del día" dependen de en qué día cae un instante para
el local, no para el servidor ni para quien mira la pantalla, así que la
conversión vive en el núcleo y ningún módulo tiene que importar a otro para
saber qué día es.

Python puro: lo importa también la capa de dominio.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/Lima"


def is_valid_timezone(name: str) -> bool:
    """Si el nombre es una zona IANA que el sistema conoce."""
    if not name or name != name.strip():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def local_date(moment: datetime, timezone: str = DEFAULT_TIMEZONE) -> date:
    """El día calendario del restaurante en el que cae un instante."""
    return moment.astimezone(ZoneInfo(timezone)).date()


def local_midnight(day: date, timezone: str = DEFAULT_TIMEZONE) -> datetime:
    """El comienzo de un día calendario del restaurante, con su zona puesta."""
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone))


def local_day_window(
    moment: datetime, timezone: str = DEFAULT_TIMEZONE
) -> tuple[datetime, datetime]:
    """Ventana del día calendario del restaurante que contiene `moment`.

    Comparar fechas de calendario en UTC directamente rompe cerca de la
    medianoche: un pedido de las 20:00 en Lima cae en el día siguiente en UTC,
    y "las ventas de hoy" dejarían de incluirlo.
    """
    day = local_date(moment, timezone)
    return local_midnight(day, timezone), local_midnight(day + timedelta(days=1), timezone)
