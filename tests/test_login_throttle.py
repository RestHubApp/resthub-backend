"""El límite de intentos de acceso, sin HTTP."""

from __future__ import annotations

import pytest

from resthub.core import login_throttle
from resthub.core.login_throttle import LoginThrottle


def test_pair_without_live_attempts_uses_no_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now[0])
    throttle = LoginThrottle(window_seconds=60)

    throttle.acquire("ana@local.pe", "200.48.1.7")
    now[0] += 61

    assert throttle.retry_after("ana@local.pe", "200.48.1.7") == 0
    assert throttle._failures == {}


def test_past_the_cap_long_idle_keys_are_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now[0])
    throttle = LoginThrottle(max_failures=1, max_keys=3)

    for n in range(5):
        now[0] += 1
        throttle.acquire(f"intruso{n}@x.pe", "200.48.1.7")

    assert len(throttle._failures) == 3
    # El más reciente sigue bloqueado; el más viejo ya se olvidó.
    assert throttle.retry_after("intruso4@x.pe", "200.48.1.7") > 0
    assert throttle.retry_after("intruso0@x.pe", "200.48.1.7") == 0


def test_rotating_ip_does_not_evade_the_per_email_cap() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=8)

    for n in range(8):
        # Cada intento sale de una IP distinta: el par nunca llega a 5.
        assert throttle.acquire("ana@local.pe", f"200.48.1.{n}") == 0

    assert throttle.acquire("ana@local.pe", "200.48.1.99") > 0
    assert throttle.retry_after("ana@local.pe", "200.48.1.99") > 0
    assert throttle.retry_after("otra@local.pe", "200.48.1.99") == 0


def test_successful_login_does_not_clear_the_per_email_cap() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=3)
    for n in range(2):
        throttle.acquire("ana@local.pe", f"200.48.1.{n}")
    # La dueña entra bien: devuelve su plaza, pero los dos fallos siguen.
    throttle.acquire("ana@local.pe", "200.48.1.9")
    throttle.succeeded("ana@local.pe", "200.48.1.9")
    throttle.acquire("ana@local.pe", "200.48.1.2")

    assert throttle.retry_after("ana@local.pe", "200.48.1.9") > 0


def test_successful_logins_do_not_spend_the_per_email_cap() -> None:
    throttle = LoginThrottle(max_failures=5, max_failures_per_email=3)
    for n in range(10):
        assert throttle.acquire("ana@local.pe", f"200.48.1.{n}") == 0
        throttle.succeeded("ana@local.pe", f"200.48.1.{n}")

    assert throttle.retry_after("ana@local.pe", "200.48.1.0") == 0


def test_attempt_is_reserved_on_check(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: now[0])
    throttle = LoginThrottle(max_failures=2, window_seconds=60)

    # Sin anotar ningún fallo: basta con pedir permiso para gastar el intento.
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 61
    # Un acierto limpia el contador.
    throttle.succeeded("ana@local.pe", "200.48.1.7")
    assert throttle.acquire("ana@local.pe", "200.48.1.7") == 0
