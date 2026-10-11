import asyncio
import contextvars
import threading
import time

from resthub.core.cpu_bound import MAX_WORKERS, run_cpu_bound

order = contextvars.ContextVar("pedido", default="")


async def test_run_cpu_bound_never_exceeds_thread_cap() -> None:
    running = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    await asyncio.gather(*(run_cpu_bound(work) for _ in range(MAX_WORKERS * 3)))

    assert 1 <= peak <= MAX_WORKERS


async def test_run_cpu_bound_returns_result_and_keeps_context() -> None:
    order.set("abc123")

    result = await run_cpu_bound(lambda suffix: order.get() + suffix, "-x")

    assert result == "abc123-x"
