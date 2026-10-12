"""Tope de envíos de correo por cuenta: una ventana deslizante en memoria.

Python puro: la hora entra como argumento. Con una sola réplica alcanza, como el
límite de peticiones por IP (`core/rate_limit.py`); si el proceso se reinicia, la
cuenta arranca de cero.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

WINDOW = timedelta(hours=1)


class SendLimit:
    def __init__(self, max_per_window: int) -> None:
        self.max_per_window = max_per_window
        self._sent: dict[int, deque[datetime]] = defaultdict(deque)

    def try_take(self, user_id: int, now: datetime) -> bool:
        """Cuenta un envío de esa cuenta; falso si ya llegó al tope de la última hora."""
        sent = self._sent[user_id]
        while sent and now - sent[0] >= WINDOW:
            sent.popleft()
        if len(sent) >= self.max_per_window:
            return False
        sent.append(now)
        return True
