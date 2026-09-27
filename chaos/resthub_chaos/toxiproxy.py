"""Acciones de Chaos Toolkit sobre Toxiproxy (API HTTP en el puerto 8305)."""

from __future__ import annotations

from typing import Any

import httpx

from resthub_chaos.config import TOXIPROXY_API


def _api() -> httpx.Client:
    return httpx.Client(base_url=TOXIPROXY_API, timeout=5.0)


def cortar(proxy: str) -> dict[str, Any]:
    """Deshabilita el proxy: cierra las conexiones abiertas y rechaza las nuevas."""
    with _api() as api:
        respuesta = api.post(f"/proxies/{proxy}", json={"enabled": False})
        respuesta.raise_for_status()
        return respuesta.json()


def restaurar(proxy: str) -> dict[str, Any]:
    with _api() as api:
        respuesta = api.post(f"/proxies/{proxy}", json={"enabled": True})
        respuesta.raise_for_status()
        return respuesta.json()


def agregar_latencia(
    proxy: str, latencia_ms: int, variacion_ms: int = 0, sentido: str = "downstream"
) -> dict[str, Any]:
    """Suma `latencia_ms` ± `variacion_ms` a cada trozo de datos del proxy."""
    with _api() as api:
        respuesta = api.post(
            f"/proxies/{proxy}/toxics",
            json={
                "name": f"latencia_{sentido}",
                "type": "latency",
                "stream": sentido,
                "toxicity": 1.0,
                "attributes": {"latency": latencia_ms, "jitter": variacion_ms},
            },
        )
        respuesta.raise_for_status()
        return respuesta.json()


def quitar_toxicos(proxy: str) -> list[str]:
    quitados: list[str] = []
    with _api() as api:
        for toxico in api.get(f"/proxies/{proxy}/toxics").json():
            api.delete(f"/proxies/{proxy}/toxics/{toxico['name']}")
            quitados.append(toxico["name"])
    return quitados


def reiniciar_todo() -> bool:
    """Rollback general: todos los proxies habilitados y sin tóxicos."""
    with _api() as api:
        api.post("/reset").raise_for_status()
    return True


def estado(proxy: str) -> dict[str, Any]:
    with _api() as api:
        return api.get(f"/proxies/{proxy}").json()
