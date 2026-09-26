"""Cuánto de cada petición se va en la base de datos.

El middleware de registro abre una medición por petición y los eventos del
motor de SQLAlchemy la van sumando: tiempo en consultas, cuántas consultas y
cuántas conexiones nuevas hubo que abrir. Así un log lento dice si el tiempo se
fue en la base, en abrir conexiones o en la propia aplicación.

La medición vive en una variable de contexto que guarda un objeto mutable: las
consultas de SQLAlchemy asíncrono corren en un greenlet con una copia del
contexto, y una copia ve el mismo objeto aunque no vea asignaciones nuevas.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

_QUERY_START_KEY = "resthub_query_start"


@dataclass(slots=True)
class DbTiming:
    queries: int = 0
    seconds: float = 0.0
    connects: int = 0

    @property
    def milliseconds(self) -> float:
        return round(self.seconds * 1000, 1)


_current: ContextVar[DbTiming | None] = ContextVar("resthub_db_timing", default=None)


def start_request_timing() -> DbTiming:
    timing = DbTiming()
    _current.set(timing)
    return timing


def _before_execute(conn: Any, *_: Any) -> None:
    conn.info.setdefault(_QUERY_START_KEY, []).append(time.perf_counter())


def _after_execute(conn: Any, *_: Any) -> None:
    starts = conn.info.get(_QUERY_START_KEY)
    if not starts:
        return
    elapsed = time.perf_counter() - starts.pop()
    timing = _current.get()
    if timing is not None:
        timing.queries += 1
        timing.seconds += elapsed


def _on_connect(*_: Any) -> None:
    timing = _current.get()
    if timing is not None:
        timing.connects += 1


def instrument(engine: AsyncEngine) -> None:
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before_execute)
    event.listen(sync_engine, "after_cursor_execute", _after_execute)
    event.listen(sync_engine.pool, "connect", _on_connect)
