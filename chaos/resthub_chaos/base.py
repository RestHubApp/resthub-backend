"""Comprobaciones directas en PostgreSQL (sin pasar por Toxiproxy ni por el API).

Atomicidad: después de una caída no puede quedar nada a medias. Un pedido
pagado suma exactamente sus pagos, un pedido con pagos que cubren el total está
pagado, un pedido que salió a cocina tiene platos y cada pago tiene su asiento
en la bitácora (ni uno de más ni uno de menos).
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from resthub_chaos.config import BASE_DSN

INVARIANTES: dict[str, str] = {
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


async def _consultar(sql: str, *argumentos: Any) -> list[dict[str, Any]]:
    conexion = await asyncpg.connect(BASE_DSN, timeout=10)
    try:
        return [dict(fila) for fila in await conexion.fetch(sql, *argumentos)]
    finally:
        await conexion.close()


def consultar(sql: str) -> list[dict[str, Any]]:
    return asyncio.run(_consultar(sql))


def violaciones_de_atomicidad() -> dict[str, list[dict[str, Any]]]:
    return {nombre: consultar(sql) for nombre, sql in INVARIANTES.items()}


def sin_operaciones_a_medias() -> bool:
    return all(not filas for filas in violaciones_de_atomicidad().values())


def pedidos_con_id_de_cliente(client_request_id: str) -> list[dict[str, Any]]:
    return asyncio.run(
        _consultar(
            "SELECT id, number, status, client_request_id FROM orders WHERE client_request_id = $1",
            client_request_id,
        )
    )


def conteo(tabla: str) -> int:
    if tabla not in {"orders", "order_payments", "order_items"}:
        raise ValueError(tabla)
    return int(consultar(f"SELECT COUNT(*) AS n FROM {tabla}")[0]["n"])


def consultar_crid(client_request_ids: list[str]) -> list[dict[str, Any]]:
    return asyncio.run(
        _consultar(
            "SELECT id, status, client_request_id FROM orders "
            "WHERE client_request_id = ANY($1::text[])",
            client_request_ids,
        )
    )
