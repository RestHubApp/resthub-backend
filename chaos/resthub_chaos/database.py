"""Comprobaciones directas en PostgreSQL (sin pasar por Toxiproxy ni por el API).

Atomicidad: después de una caída no puede quedar nada a medias. Un pedido
pagado suma exactamente sus pagos, un pedido con pagos que cubren el total está
pagado, un pedido que salió a cocina tiene platos y cada pago tiene su asiento
en la bitácora (ni uno de más ni uno de menos).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import asyncpg

from resthub_chaos.config import DATABASE_DSN

INVARIANTS: dict[str, str] = {
    "pagado_sin_cuadrar": """
        SELECT o.id FROM orders o
        LEFT JOIN order_payments p ON p.order_id = o.id
        WHERE o.status = 'paid'
        GROUP BY o.id, o.total
        HAVING COALESCE(SUM(p.amount), 0) <> o.total
    """,
    "cubierto_sin_pagar": """
        SELECT o.id FROM orders o
        JOIN order_payments p ON p.order_id = o.id
        WHERE o.status <> 'paid'
        GROUP BY o.id, o.total
        HAVING SUM(p.amount) >= o.total AND o.total > 0
    """,
    "en_cocina_sin_platos": """
        SELECT o.id FROM orders o
        WHERE o.status IN ('in_kitchen', 'ready', 'served', 'paid')
          AND NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id)
    """,
    "pagos_sin_bitacora": """
        SELECT 'pagos' AS tipo,
               (SELECT COUNT(*) FROM order_payments) AS pagos,
               (SELECT COUNT(*) FROM activity_log
                 WHERE kind IN ('order_charged', 'payment_received')) AS asientos
        WHERE (SELECT COUNT(*) FROM order_payments)
           <> (SELECT COUNT(*) FROM activity_log
                WHERE kind IN ('order_charged', 'payment_received'))
    """,
    "pedido_duplicado": """
        SELECT client_request_id FROM orders
        WHERE client_request_id IS NOT NULL
        GROUP BY restaurant_id, client_request_id HAVING COUNT(*) > 1
    """,
}


async def _query(sql: str, *args: Any) -> list[dict[str, Any]]:
    connection = await asyncpg.connect(DATABASE_DSN, timeout=10)
    try:
        return [dict(row) for row in await connection.fetch(sql, *args)]
    finally:
        await connection.close()


def query(sql: str) -> list[dict[str, Any]]:
    return _run_in_thread(_query, sql)


def _run_in_thread(func: Any, *args: Any) -> list[dict[str, Any]]:
    def run() -> list[dict[str, Any]]:
        return asyncio.run(func(*args))

    # Chaos Toolkit ejecuta probes síncronos dentro de un event loop; asyncpg
    # necesita su propio bucle en otro hilo para la verificación directa.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result()


def atomicity_violations() -> dict[str, list[dict[str, Any]]]:
    return {name: query(sql) for name, sql in INVARIANTS.items()}


def no_partial_operations() -> bool:
    return all(not rows for rows in atomicity_violations().values())


def orders_with_client_id(client_request_id: str) -> list[dict[str, Any]]:
    return _run_in_thread(
        _query,
        "SELECT id, number, status, client_request_id FROM orders WHERE client_request_id = $1",
        client_request_id,
    )


def count(table: str) -> int:
    if table not in {"orders", "order_payments", "order_items"}:
        raise ValueError(table)
    return int(query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def query_crids(client_request_ids: list[str]) -> list[dict[str, Any]]:
    return _run_in_thread(
        _query,
        "SELECT id, status, client_request_id FROM orders "
        "WHERE client_request_id = ANY($1::text[])",
        client_request_ids,
    )
