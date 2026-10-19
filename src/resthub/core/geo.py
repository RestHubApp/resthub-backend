"""Ciudad y país de una IP, con una base GeoLite2 local (RNFS-15).

La IP no sale a ningún servicio: se busca en el archivo `.mmdb` que indica
`GEOIP_DATABASE_PATH` (GeoLite2 City de MaxMind, que se baja aparte con su
licencia y no va en el repositorio). Sin archivo, o si no se puede abrir, los
accesos se guardan sin ubicación y nada falla: ubicar es un dato de más, no una
condición para entrar.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from resthub.core.config import get_settings
from resthub.core.logs import get_logger

logger = get_logger("resthub.geo")


@dataclass(frozen=True, slots=True)
class Location:
    city: str = ""
    # Código ISO 3166-1 alfa-2 (`PE`); vacío si no se sabe.
    country: str = ""


UNKNOWN = Location()


class GeoLocator(Protocol):
    def locate(self, address: str) -> Location: ...


class _MmdbReader(Protocol):
    def get(self, address: str) -> object: ...


class NoGeoLocator:
    """Sin base de ubicaciones: todo acceso queda sin ciudad ni país."""

    def locate(self, address: str) -> Location:  # noqa: ARG002 - la firma la impone el puerto
        return UNKNOWN


class MaxMindGeoLocator:
    def __init__(self, reader: _MmdbReader) -> None:
        self._reader = reader

    def locate(self, address: str) -> Location:
        try:
            record = self._reader.get(address)
        except ValueError:
            # No es una IP (la cabecera del proxy trajo otra cosa).
            return UNKNOWN
        if not isinstance(record, dict):
            return UNKNOWN
        return Location(city=_spanish_name(record.get("city")), country=_iso_code(record))


def _spanish_name(place: object) -> str:
    if not isinstance(place, dict):
        return ""
    names = place.get("names")
    if not isinstance(names, dict):
        return ""
    name = names.get("es") or names.get("en") or ""
    return str(name)[:80]


def _iso_code(record: dict[object, object]) -> str:
    country = record.get("country")
    if not isinstance(country, dict):
        return ""
    return str(country.get("iso_code") or "")[:2]


def open_geo_locator(path: str) -> GeoLocator:
    if not path:
        return NoGeoLocator()
    try:
        import maxminddb  # noqa: PLC0415 - solo se carga si hay base configurada

        reader = maxminddb.open_database(path)
    except (OSError, ValueError) as error:
        logger.warning("geo.database_unavailable", error=type(error).__name__)
        return NoGeoLocator()
    return MaxMindGeoLocator(reader)


@lru_cache(maxsize=1)
def get_geo_locator() -> GeoLocator:
    return open_geo_locator(get_settings().geoip_database_path)
