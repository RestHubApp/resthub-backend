"""El límite de intentos de acceso, sin HTTP."""

from __future__ import annotations

import pytest

from resthub.core import login_throttle
from resthub.core.login_throttle import LoginThrottle


def test_un_par_sin_intentos_vigentes_no_ocupa_memoria(monkeypatch: pytest.MonkeyPatch) -> None:
    ahora = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: ahora[0])
    throttle = LoginThrottle(window_seconds=60)

    throttle.acquire("ana@local.pe", "200.48.1.7")
    ahora[0] += 61

    assert throttle.retry_after("ana@local.pe", "200.48.1.7") == 0
    assert throttle._failures == {}


def test_pasado_el_tope_se_olvidan_los_que_hace_mas_que_no_fallan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ahora = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: ahora[0])
    throttle = LoginThrottle(max_failures=1, max_keys=3)

    for n in range(5):
        ahora[0] += 1
        throttle.acquire(f"intruso{n}@x.pe", "200.48.1.7")

    assert len(throttle._failures) == 3
    # El más reciente sigue bloqueado; el más viejo ya se olvidó.
    assert throttle.retry_after("intruso4@x.pe", "200.48.1.7") > 0
    assert throttle.retry_after("intruso0@x.pe", "200.48.1.7") == 0


def test_rotar_de_ip_no_esquiva_el_tope_por_correo() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=8)

    for n in range(8):
        # Cada intento sale de una IP distinta: el par nunca llega a 5.
        assert throttle.acquire("ana@local.pe", f"200.48.1.{n}") == 0

    assert throttle.acquire("ana@local.pe", "200.48.1.99") > 0
    assert throttle.retry_after("ana@local.pe", "200.48.1.99") > 0
    assert throttle.retry_after("otra@local.pe", "200.48.1.99") == 0


def test_un_acceso_correcto_no_limpia_el_tope_por_correo() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=3)
    for n in range(2):
        throttle.acquire("ana@local.pe", f"200.48.1.{n}")
    # La dueña entra bien: devuelve su plaza, pero los dos fallos siguen.
    throttle.acquire("ana@local.pe", "200.48.1.9")
    throttle.succeeded("ana@local.pe", "200.48.1.9")
    throttle.acquire("ana@local.pe", "200.48.1.2")

    assert throttle.retry_after("ana@local.pe", "200.48.1.9") > 0


def test_los_accesos_correctos_no_gastan_el_tope_por_correo() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=3)
    for n in range(10):
        assert throttle.acquire("ana@local.pe", f"200.48.1.{n}") == 0
        throttle.succeeded("ana@local.pe", f"200.48.1.{n}")

    assert throttle.retry_after("ana@local.pe", "200.48.1.0") == 0


def test_el_intento_se_reserva_al_comprobar(monkeypatch: pytest.MonkeyPatch) -> None:
    ahora = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: ahora[0])
    throttle = LoginThrottle(max_failures=2, window_seconds=60)

    # Sin anotar ningún fallo: basta con pedir permiso para gastar el intento.
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 61
    # Un acierto limpia el contador.
    throttle.succeeded("ana@local.pe", "200.48.1.7")
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
