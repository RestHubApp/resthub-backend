"""Límite de consultas por cuenta: ventana fija de un minuto, en la memoria del proceso."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

WINDOW_SECONDS = 60
_SWEEP_AT = 10_000


class PerAccountLookupQuota:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        # cuenta → (inicio de su ventana, consultas en ella).
        self._windows: dict[int, tuple[float, int]] = {}

    def acquire(self, user_id: int) -> int:
        if self._per_minute <= 0:
            return 0
        now = self._clock()
        if len(self._windows) >= _SWEEP_AT:
            self._windows = {
                key: value
                for key, value in self._windows.items()
                if value[0] + WINDOW_SECONDS > now
            }
        started, count = self._windows.get(user_id, (now, 0))
        if started + WINDOW_SECONDS <= now:
            started, count = now, 0
        if count >= self._per_minute:
            return max(1, math.ceil(started + WINDOW_SECONDS - now))
        self._windows[user_id] = (started, count + 1)
        return 0
