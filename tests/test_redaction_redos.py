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

SIZE = 100 * 1024
LIMIT_SECONDS = 1.0

ATTACKS: dict[str, Callable[[int], str]] = {
    "sentencia_sql": lambda n: "\t" + "[SQL: :" * n,
    "detalle_de_clave": lambda n: "sKey ()=(" * n + "s",
    "columnas_de_clave": lambda n: "Key (n" * n + "\n",
    "jwt": lambda n: "eyJ-" * n + "J.",
    "url_con_usuario": lambda n: "_" + "+A" * n + "\tA://:\x00@",
    "url_repetida": lambda n: "A://:A" * n + "\t",
    "correo_sin_dominio": lambda n: "a." * n + "@a",
    "correo_repetido": lambda n: "a@a" * n,
    "numero_largo": lambda n: "1 " * n + "x",
    "numeros_pegados": lambda n: "1" * n + "a",
}


def _build(make: Callable[[int], str]) -> str:
    return make(SIZE // len(make(1)))


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_crafted_input_does_not_stall_scrubbing(name: str) -> None:
    text = _build(ATTACKS[name])

    start = time.perf_counter()
    scrub_text(text)
    duration = time.perf_counter() - start

    assert duration < LIMIT_SECONDS, f"{name}: {duration:.2f} s con {len(text)} caracteres"


def test_linear_expressions_still_mask_the_same_things() -> None:
    text = (
        "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.firma\n"
        "postgresql+asyncpg://resthub:clave-secreta@db:5432/resthub\n"
        "DETAIL:  Key (restaurant_id, slug)=(1, pollos-juan) already exists.\n"
        "[SQL: SELECT * FROM users WHERE email = 'juan@x.com']\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )

    cleaned = scrub_text(text)

    for secret in ("eyJhbGciOiJIUzI1NiJ9", "clave-secreta", "pollos-juan", "juan@x.com"):
        assert secret not in cleaned
    assert f"Authorization: {REDACTED}" in cleaned
    assert f"postgresql+asyncpg://{REDACTED}@db:5432/resthub" in cleaned
    assert f"Key (restaurant_id, slug)=({REDACTED}) already exists" in cleaned
    assert f"email = '{REDACTED}'" in cleaned
