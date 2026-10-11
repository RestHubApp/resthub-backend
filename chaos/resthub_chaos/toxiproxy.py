"""Acciones de Chaos Toolkit sobre Toxiproxy (API HTTP en el puerto 8305)."""

from __future__ import annotations

from typing import Any

import httpx

from resthub_chaos.config import TOXIPROXY_API


def _api() -> httpx.Client:
    return httpx.Client(base_url=TOXIPROXY_API, timeout=5.0)


def cut(proxy: str) -> dict[str, Any]:
    """Deshabilita el proxy: cierra las conexiones abiertas y rechaza las nuevas."""
    with _api() as api:
        response = api.post(f"/proxies/{proxy}", json={"enabled": False})
        response.raise_for_status()
        return response.json()


def restore(proxy: str) -> dict[str, Any]:
    with _api() as api:
        response = api.post(f"/proxies/{proxy}", json={"enabled": True})
        response.raise_for_status()
        return response.json()


def add_latency(
    proxy: str, latency_ms: int, jitter_ms: int = 0, direction: str = "downstream"
) -> dict[str, Any]:
    """Suma `latencia_ms` ± `variacion_ms` a cada trozo de datos del proxy."""
    with _api() as api:
        response = api.post(
            f"/proxies/{proxy}/toxics",
            json={
                "name": f"latencia_{direction}",
                "type": "latency",
                "stream": direction,
                "toxicity": 1.0,
                "attributes": {"latency": latency_ms, "jitter": jitter_ms},
            },
        )
        response.raise_for_status()
        return response.json()


def remove_toxics(proxy: str) -> list[str]:
    removed: list[str] = []
    with _api() as api:
        for toxic in api.get(f"/proxies/{proxy}/toxics").json():
            api.delete(f"/proxies/{proxy}/toxics/{toxic['name']}")
            removed.append(toxic["name"])
    return removed


def reset_all() -> bool:
    """Rollback general: todos los proxies habilitados y sin tóxicos."""
    with _api() as api:
        api.post("/reset").raise_for_status()
    return True


def status(proxy: str) -> dict[str, Any]:
    with _api() as api:
        return api.get(f"/proxies/{proxy}").json()
