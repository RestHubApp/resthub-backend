"""La limpieza de textos no se deja frenar con una entrada armada (ReDoS).

`scrub_text` corre sobre mensajes y tracebacks sin recortar, dentro del bucle
de eventos: una expresión que retrocede en tiempo cuadrático deja a todo el
servidor esperando. Las entradas son las que encontró el analizador `recheck`
para las versiones anteriores de las expresiones, que con unos 100 KB tardaban
entre 2 y 6 segundos. Con las actuales tardan milisegundos; el tope de un
segundo deja margen de sobra para un runner lento del CI.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from resthub.core.redaction import REDACTED, scrub_text

TAMANO = 100 * 1024
TOPE_SEGUNDOS = 1.0

ATAQUES: dict[str, Callable[[int], str]] = {
    "sentencia_sql": lambda n: "\t" + "[SQL: :" * n,
    "detalle_de_clave": lambda n: "sKey ()=(" * n + "s",
    "columnas_de_clave": lambda n: "Key (n" * n + "\n",
    "jwt": lambda n: "eyJ-" * n + "J.",
    "url_con_usuario": lambda n: "_" + "+A" * n + "\tA://:\x00@",
    "url_repetida": lambda n: "A://:A" * n + "\t",
}


def _armar(hacer: Callable[[int], str]) -> str:
    return hacer(TAMANO // len(hacer(1)))


@pytest.mark.parametrize("nombre", sorted(ATAQUES))
def test_una_entrada_armada_no_frena_la_limpieza(nombre: str) -> None:
    texto = _armar(ATAQUES[nombre])

    inicio = time.perf_counter()
    scrub_text(texto)
    duracion = time.perf_counter() - inicio

    assert duracion < TOPE_SEGUNDOS, f"{nombre}: {duracion:.2f} s con {len(texto)} caracteres"


def test_las_expresiones_lineales_siguen_tapando_lo_mismo() -> None:
    texto = (
        "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.firma\n"
        "postgresql+asyncpg://resthub:clave-secreta@db:5432/resthub\n"
        "DETAIL:  Key (restaurant_id, slug)=(1, pollos-juan) already exists.\n"
        "[SQL: SELECT * FROM users WHERE email = 'juan@x.com']\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )

    limpio = scrub_text(texto)

    for secreto in ("eyJhbGciOiJIUzI1NiJ9", "clave-secreta", "pollos-juan", "juan@x.com"):
        assert secreto not in limpio
    assert f"Authorization: {REDACTED}" in limpio
    assert f"postgresql+asyncpg://{REDACTED}@db:5432/resthub" in limpio
    assert f"Key (restaurant_id, slug)=({REDACTED}) already exists" in limpio
    assert f"email = '{REDACTED}'" in limpio
