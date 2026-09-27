"""Lo que hace el API cuando la base falla: hallazgos de los experimentos de caos.

Los experimentos (`chaos/experimentos`) cortan PostgreSQL de verdad; estas
pruebas fijan el comportamiento corregido sin necesitar PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from tests.builders import carta
from tests.conftest import StaffedRestaurant, authorization_for


def _anota_respuestas(app: FastAPI, eventos: list[str]) -> Any:
    async def envoltorio(scope: Scope, receive: Receive, send: Send) -> None:
        async def enviar(message: Message) -> None:
            if message["type"] == "http.response.start":
                eventos.append("respuesta")
            await send(message)

        await app(scope, receive, enviar)

    return envoltorio


async def test_el_pedido_se_confirma_en_la_base_antes_de_responder(
    app: FastAPI, session: AsyncSession, local_a: StaffedRestaurant
) -> None:
    # Hallazgo del experimento 01: el 201 salía antes del COMMIT. Si la base
    # caía en ese instante, el mesero veía el pedido creado y no quedaba nada.
    menu = await carta(session, local_a.id)
    eventos: list[str] = []
    event.listen(session.sync_session, "after_commit", lambda _: eventos.append("commit"))
    transport = ASGITransport(app=_anota_respuestas(app, eventos))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/orders",
            json={"type": "takeaway", "items": [{"menu_item_id": menu.lomo, "quantity": 1}]},
            headers=authorization_for(local_a.waiter),
        )

    assert response.status_code == 201, response.text
    assert "commit" in eventos[: eventos.index("respuesta")], eventos
