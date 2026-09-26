"""El límite de intentos de acceso, sin HTTP."""

from __future__ import annotations

import pytest

from resthub.core import login_throttle
from resthub.core.login_throttle import LoginThrottle


def test_un_par_sin_intentos_vigentes_no_ocupa_memoria(monkeypatch: pytest.MonkeyPatch) -> None:
    ahora = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: ahora[0])
    throttle = LoginThrottle(window_seconds=60)

    throttle.failed("ana@local.pe", "200.48.1.7")
    ahora[0] += 61

    assert throttle.retry_after("ana@local.pe", "200.48.1.7") == 0
    assert throttle._failures == {}  # noqa: SLF001


def test_pasado_el_tope_se_olvidan_los_que_hace_mas_que_no_fallan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ahora = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: ahora[0])
    throttle = LoginThrottle(max_failures=1, max_keys=3)

    for n in range(5):
        ahora[0] += 1
        throttle.failed(f"intruso{n}@x.pe", "200.48.1.7")

    assert len(throttle._failures) == 3  # noqa: SLF001
    # El más reciente sigue bloqueado; el más viejo ya se olvidó.
    assert throttle.retry_after("intruso4@x.pe", "200.48.1.7") > 0
    assert throttle.retry_after("intruso0@x.pe", "200.48.1.7") == 0
