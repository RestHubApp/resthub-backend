"""Parámetros de la API con límites que la base de datos puede representar.

Los identificadores son columnas `Integer` (32 bits en PostgreSQL). Un número mayor
llegaba a la consulta y la base lo rechazaba con un error de driver, que el cliente
veía como un 500 sin explicación. Acotarlo en el borde lo convierte en un 422 que
OpenAPI declara (`maximum`), y la fuzzing de Schemathesis deja de hallar 500.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import Path
from pydantic import AfterValidator, Field

# El mayor valor de una columna `Integer` de 32 bits.
MAX_ID = 2_147_483_647

# Identificador de un recurso en la ruta (`/orders/{order_id}`): positivo y que cabe en la base.
PathId = Annotated[int, Path(ge=1, le=MAX_ID)]

# Identificador de un recurso dentro del cuerpo de una petición (`{"menu_item_id": 3}`).
EntityId = Annotated[int, Field(ge=1, le=MAX_ID)]

# Un teléfono que llega por la API: dígitos y espacios, quizá con un «+» delante, de 6
# a 20 caracteres; vacío si no se dio. Es la misma regla con la que `customers` valida
# el de un cliente (`\+?[\d ]{6,20}`). Se comprueba solo al recibirlo: los teléfonos que
# ya estaban guardados se siguen leyendo aunque no la cumplan. La interfaz la repite.
PHONE_PATTERN = r"^(?:\+?[\d ]{6,20})?$"

# Una fecha pegada a `date.min`/`date.max` desborda al sumarle un día o pasarla a UTC
# (`OverflowError`) y la API respondía 500. El margen cubre cualquier zona horaria y la
# duración de lo que empieza ese día; se mide desde los límites, así que nunca caduca.
_CALENDAR_MARGIN = timedelta(days=7)
MIN_CALENDAR_DAY = date.min + _CALENDAR_MARGIN
MAX_CALENDAR_DAY = date.max - _CALENDAR_MARGIN


def _representable_day(day: date) -> date:
    if not MIN_CALENDAR_DAY <= day <= MAX_CALENDAR_DAY:
        message = "La fecha está fuera del rango que el sistema puede representar."
        raise ValueError(message)
    return day


def _representable_moment(moment: datetime) -> datetime:
    _representable_day(moment.date())
    return moment


# Un día del calendario que llega por la API (`?day=2026-10-02`): fuera del rango, un 422.
CalendarDay = Annotated[date, AfterValidator(_representable_day)]

# Una fecha y hora que llega por la API, con la misma cota que `CalendarDay`.
CalendarDateTime = Annotated[datetime, AfterValidator(_representable_moment)]
