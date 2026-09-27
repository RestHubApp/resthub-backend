"""Trabajo de CPU fuera del bucle de eventos, con un tope propio de hilos.

bcrypt tarda unos 250 ms por contraseña a propósito. Correrlo en el bucle de
eventos frenaría a todas las demás peticiones, así que va en un hilo; pero con
`asyncio.to_thread` usa el ejecutor por omisión, que abre hasta
`min(32, núcleos + 4)` hilos. Cuando entra medio turno a la vez (la prueba de
carga lo mide: 25 accesos en el mismo segundo), esos hilos ocupan todos los
núcleos y el bucle, que atiende al resto del local, se queda sin CPU: pedir
las mesas pasaba de 15 ms a más de un segundo. Además el ejecutor por omisión
es el mismo que usa asyncio para resolver nombres al abrir una conexión a la
base, que quedaba en la cola detrás de los accesos.

Aquí los accesos van a un ejecutor aparte con pocos hilos: si llegan muchos,
esperan su turno entre ellos y el resto de la aplicación sigue respondiendo.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

# La mitad de los núcleos, entre 1 y 4: deja al menos otro tanto libre para el
# bucle de eventos y para PostgreSQL cuando corre en la misma máquina.
MAX_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="resthub-cpu")


async def run_cpu_bound[**P, T](func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Como `asyncio.to_thread`, pero en el ejecutor acotado de este módulo.

    Copia el contexto (el `request_id` de los logs, por ejemplo) igual que
    `to_thread`.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(_executor, call)
