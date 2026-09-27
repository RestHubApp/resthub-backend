import asyncio
import contextvars
import threading
import time

from resthub.core.cpu_bound import MAX_WORKERS, run_cpu_bound

pedido = contextvars.ContextVar("pedido", default="")


async def test_no_pasa_del_tope_de_hilos() -> None:
    en_curso = 0
    maximo = 0
    candado = threading.Lock()

    def trabajo() -> None:
        nonlocal en_curso, maximo
        with candado:
            en_curso += 1
            maximo = max(maximo, en_curso)
        time.sleep(0.02)
        with candado:
            en_curso -= 1

    await asyncio.gather(*(run_cpu_bound(trabajo) for _ in range(MAX_WORKERS * 3)))

    assert 1 <= maximo <= MAX_WORKERS


async def test_devuelve_el_resultado_y_conserva_el_contexto() -> None:
    pedido.set("abc123")

    resultado = await run_cpu_bound(lambda sufijo: pedido.get() + sufijo, "-x")

    assert resultado == "abc123-x"
