"""Trabajo que sigue después de responder.

Algunas cosas no tienen por qué hacer esperar a quien las dispara: clasificar
las notas de un pedido con la IA puede tardar unos segundos, y el mesero ya
tiene que ver su pedido en cocina. Este registro lanza esas tareas en el mismo
bucle de eventos, guarda una referencia fuerte a cada una (sin ella, asyncio
puede recolectarlas a mitad de camino) y anota en el log las que fallan, que de
otro modo morirían en silencio.

No es una cola: si el proceso se reinicia, lo pendiente se pierde. Por eso solo
se usa para trabajo que se puede repetir a mano (el encargado vuelve a pedir la
clasificación) y que no deja nada a medias si no corre.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import lru_cache

from resthub.core.logs import get_logger

logger = get_logger("resthub.background")

# Cuánto se espera al apagar a que termine lo que está corriendo antes de
# cancelarlo. Alcanza para una llamada a la IA con su tiempo máximo.
SHUTDOWN_GRACE_SECONDS = 10.0


class BackgroundJobs:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def spawn(self, name: str, job: Callable[[], Awaitable[None]]) -> None:
        """Arranca `job` sin esperarlo. Tiene que llamarse con el bucle corriendo."""
        task = asyncio.get_running_loop().create_task(self._run(name, job), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Espera a que termine todo lo lanzado, incluido lo que se lance mientras tanto."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        if not self._tasks:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self.drain(), timeout=grace_seconds)
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    @staticmethod
    async def _run(name: str, job: Callable[[], Awaitable[None]]) -> None:
        try:
            await job()
        except asyncio.CancelledError:
            logger.warning("background.cancelled", job=name)
            raise
        except Exception as error:
            # Nadie espera esta tarea: si no se anota acá, el error no queda en
            # ningún lado.
            logger.exception("background.failed", job=name, error=str(error))


@lru_cache
def get_background_jobs() -> BackgroundJobs:
    return BackgroundJobs()
