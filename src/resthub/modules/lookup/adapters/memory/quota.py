"""Límite de consultas, en la memoria del proceso.

Dos ventanas fijas: un minuto por cuenta y un día por restaurante. El token del
tercero es uno solo para todo el sistema y su plan tiene un cupo al mes: sin el
tope por local, una cuenta de cualquier restaurante lo agotaría para todos.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

WINDOW_SECONDS = 60
DAY_SECONDS = 24 * 60 * 60
_SWEEP_AT = 10_000


class _FixedWindows:
    """Cuántas consultas lleva cada clave en su ventana, que empieza con la primera."""

    def __init__(self, limit: int, seconds: int, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._seconds = seconds
        self._clock = clock
        # clave → (inicio de su ventana, consultas en ella).
        self._windows: dict[int, tuple[float, int]] = {}

    def wait(self, key: int) -> int:
        """0 si queda cupo; si no, los segundos hasta que se abra la ventana siguiente."""
        if self._limit <= 0:
            return 0
        now = self._clock()
        if len(self._windows) >= _SWEEP_AT:
            self._windows = {
                other: value
                for other, value in self._windows.items()
                if value[0] + self._seconds > now
            }
        started, count = self._current(key, now)
        if count >= self._limit:
            return max(1, math.ceil(started + self._seconds - now))
        return 0

    def take(self, key: int) -> None:
        if self._limit <= 0:
            return
        started, count = self._current(key, self._clock())
        self._windows[key] = (started, count + 1)

    def _current(self, key: int, now: float) -> tuple[float, int]:
        started, count = self._windows.get(key, (now, 0))
        if started + self._seconds <= now:
            return now, 0
        return started, count


class PerAccountLookupQuota:
    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        per_restaurant_per_day: int = 0,
    ) -> None:
        self._accounts = _FixedWindows(per_minute, WINDOW_SECONDS, clock)
        self._restaurants = _FixedWindows(per_restaurant_per_day, DAY_SECONDS, clock)

    def acquire(self, user_id: int, restaurant_id: int) -> int:
        # Se miran los dos topes antes de anotar: una consulta frenada no gasta el otro.
        wait = self._accounts.wait(user_id) or self._restaurants.wait(restaurant_id)
        if wait:
            return wait
        self._accounts.take(user_id)
        self._restaurants.take(restaurant_id)
        return 0
