"""La aplicación real más la ruta de estados del proveedor, solo para verificar el contrato.

Pact, antes de reproducir cada interacción del contrato con el frontend, pide
por `POST /_pact/provider-states` que el backend quede en el estado que la
interacción supone («hay un pedido servido y la caja abierta»). Esa ruta prepara
los datos llamando al propio API, como lo haría una persona desde la interfaz,
y devuelve los valores que la interacción necesita (el token de la cuenta, el
identificador del pedido, el saldo que se vio al cobrar). Pact los inyecta en
la petición con `fromProviderState`.

Este módulo vive en `tests/` y nunca se importa desde `src/`: la aplicación que
se despliega no tiene la ruta de estados. Se levanta con

    uv run uvicorn tests.contract.provider_app:app --port 8203

contra una base SQLite propia ya migrada y sembrada con `scripts/seed_dev.py`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from resthub.main import create_app

API = "/api/v1"
STATES_PATH = "/_pact/provider-states"
PASSWORD = "resthub123"
OWNER_EMAIL = "admin@resthub.dev"
WAITER_EMAIL = "mesero@resthub.dev"
PLATFORM_EMAIL = "plataforma@resthub.dev"
# El otro local, dueño del recurso que el encargado de la demo no debe ver.
OTHER_SLUG = "contrato-otro-local"
OTHER_OWNER_EMAIL = "encargado@contrato-otro-local.dev"

app: FastAPI = create_app()


class ProviderState(BaseModel):
    """Lo que manda el verificador de Pact antes (y después) de cada interacción."""

    state: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    action: str = "setup"


class _Api:
    """Un cliente del propio API, sin red: llama a la aplicación en el mismo proceso."""

    def __init__(self) -> None:
        self._client = AsyncClient(transport=ASGITransport(app=app), base_url="http://provider")
        self._tokens: dict[str, str] = {}

    async def request(self, method: str, path: str, token: str | None = None, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await self._client.request(method, f"{API}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            msg = f"{method} {path} -> {response.status_code}: {response.text}"
            raise RuntimeError(msg)
        return response.json() if response.content else None

    async def token(self, email: str, *, platform: bool = False) -> str:
        """El token de una cuenta; se entra una vez por cuenta y se reutiliza."""
        if email not in self._tokens:
            path = "/platform/auth/login" if platform else "/auth/login"
            data = await self.request("POST", path, json={"email": email, "password": PASSWORD})
            self._tokens[email] = data["access_token"]
        return self._tokens[email]


_api = _Api()


async def _owner() -> str:
    return await _api.token(OWNER_EMAIL)


async def _first_dish(token: str) -> dict[str, Any]:
    menu = await _api.request("GET", "/menu", token)
    return next(
        item
        for category in menu["categories"]
        for item in category["items"]
        if item["is_available"] and not item["out_of_stock"] and not item["modifier_groups"]
    )


async def _free_table(token: str) -> int:
    tables = await _api.request("GET", "/tables", token)
    free = [table["id"] for table in tables if table["status"] == "free"]
    if free:
        return free[0]
    created = await _api.request("POST", "/tables", token, json={"label": uuid4().hex[:8]})
    return created["id"]


async def _ensure_cash_open(token: str) -> None:
    current = await _api.request("GET", "/cash/current", token)
    if not current["is_open"]:
        await _api.request("POST", "/cash/open", token, json={"opening_amount": "100.00"})


async def _open_table_order(token: str) -> dict[str, Any]:
    dish = await _first_dish(token)
    return await _api.request(
        "POST",
        "/orders",
        token,
        json={
            "type": "dine_in",
            "table_id": await _free_table(token),
            "items": [{"menu_item_id": dish["id"], "quantity": 2}],
        },
    )


async def _served_order(token: str) -> dict[str, Any]:
    order = await _open_table_order(token)
    for step in ("send", "ready", "served"):
        order = await _api.request("POST", f"/orders/{order['id']}/{step}", token)
    return order


async def _paid_order(token: str) -> dict[str, Any]:
    await _ensure_cash_open(token)
    order = await _served_order(token)
    return await _api.request(
        "POST",
        f"/orders/{order['id']}/payments",
        token,
        json={"payment_method": "yape", "expected_balance": order["balance"]},
    )


# -- Estados -----------------------------------------------------------------


async def _state_owner_account() -> dict[str, Any]:
    # La semilla ya creó la cuenta; entrar una vez confirma que existe.
    await _owner()
    return {}


async def _state_owner_session() -> dict[str, Any]:
    return {"token": await _owner()}


async def _state_waiter_session() -> dict[str, Any]:
    return {"token": await _api.token(WAITER_EMAIL)}


async def _state_platform_session() -> dict[str, Any]:
    return {"token": await _api.token(PLATFORM_EMAIL, platform=True)}


async def _state_free_table() -> dict[str, Any]:
    token = await _owner()
    dish = await _first_dish(token)
    return {"token": token, "tableId": await _free_table(token), "menuItemId": dish["id"]}


async def _state_open_order() -> dict[str, Any]:
    token = await _owner()
    order = await _open_table_order(token)
    # Que quede también una mesa libre: el salón se lee con las dos formas.
    await _free_table(token)
    return {
        "token": token,
        "orderId": order["id"],
        "menuItemId": order["items"][0]["menu_item_id"],
    }


async def _state_served_order() -> dict[str, Any]:
    token = await _owner()
    await _ensure_cash_open(token)
    order = await _served_order(token)
    return {"token": token, "orderId": order["id"], "balance": order["balance"]}


async def _state_balance_changed() -> dict[str, Any]:
    """Quien cobra vio un saldo, pero otro pago entró antes que el suyo."""
    token = await _owner()
    await _ensure_cash_open(token)
    order = await _served_order(token)
    seen = order["balance"]
    part = (Decimal(seen) / 2).quantize(Decimal("0.01"))
    await _api.request(
        "POST",
        f"/orders/{order['id']}/payments",
        token,
        json={"payment_method": "plin", "amount": str(part), "expected_balance": seen},
    )
    return {"token": token, "orderId": order["id"], "balance": seen}


async def _state_cash_open() -> dict[str, Any]:
    token = await _owner()
    await _ensure_cash_open(token)
    return {"token": token}


async def _state_paid_order() -> dict[str, Any]:
    token = await _owner()
    order = await _paid_order(token)
    return {"token": token, "orderId": order["id"]}


async def _state_invoiced_order() -> dict[str, Any]:
    token = await _owner()
    order = await _paid_order(token)
    await _api.request(
        "POST", "/billing/invoices", token, json={"order_id": order["id"], "kind": "boleta"}
    )
    return {"token": token, "orderId": order["id"]}


async def _other_restaurant_owner() -> str:
    platform = await _api.token(PLATFORM_EMAIL, platform=True)
    page = await _api.request(
        "GET", "/platform/restaurants", platform, params={"search": OTHER_SLUG}
    )
    if not page["items"]:
        await _api.request(
            "POST",
            "/platform/restaurants",
            platform,
            json={
                "name": "Otro local del contrato",
                "slug": OTHER_SLUG,
                "timezone": "America/Lima",
                "owner": {
                    "full_name": "Encargada del otro local",
                    "email": OTHER_OWNER_EMAIL,
                    "password": PASSWORD,
                },
            },
        )
    return await _api.token(OTHER_OWNER_EMAIL)


async def _state_foreign_order() -> dict[str, Any]:
    """Un pedido que existe, pero en otro restaurante."""
    other = await _other_restaurant_owner()
    category = await _api.request("POST", "/menu/categories", other, json={"name": "Platos"})
    dish = await _api.request(
        "POST",
        "/menu/items",
        other,
        json={"category_id": category["id"], "name": "Ají de gallina", "price": "24.00"},
    )
    order = await _api.request(
        "POST",
        "/orders",
        other,
        json={
            "type": "takeaway",
            "customer_name": "Cliente del otro local",
            "items": [{"menu_item_id": dish["id"], "quantity": 1}],
        },
    )
    return {"token": await _owner(), "orderId": order["id"]}


STATES: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "existe la cuenta del encargado": _state_owner_account,
    "el encargado tiene sesión": _state_owner_session,
    "el mesero tiene sesión": _state_waiter_session,
    "el administrador del sistema tiene sesión": _state_platform_session,
    "hay una mesa libre": _state_free_table,
    "hay un pedido abierto en una mesa": _state_open_order,
    "hay un pedido servido y la caja abierta": _state_served_order,
    "otro pago cambió el saldo del pedido servido": _state_balance_changed,
    "la caja está abierta": _state_cash_open,
    "hay un pedido pagado sin comprobante": _state_paid_order,
    "hay un pedido pagado con su boleta emitida": _state_invoiced_order,
    "hay un pedido de otro restaurante": _state_foreign_order,
}


@app.post(STATES_PATH, include_in_schema=False)
async def provider_state(change: ProviderState) -> dict[str, Any]:
    """Prepara el estado que supone una interacción y devuelve sus valores."""
    if change.action != "setup" or not change.state:
        return {}
    setup = STATES.get(change.state)
    if setup is None:
        msg = f"Estado del proveedor desconocido: {change.state!r}"
        raise ValueError(msg)
    return await setup()
