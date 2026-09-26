"""Quién hizo la petición en curso, para la telemetría.

El middleware de registro no puede depender de cómo se autentica cada lado de
la API: el personal lo resuelve `core/auth.py` y la administración del sistema,
el módulo `platform`. Por eso el middleware abre una anotación vacía por
petición y cada dependencia de acceso, cuando reconoce la credencial, anota el
tipo de cuenta, su identificador y su restaurante. Al terminar la respuesta el
middleware lee lo anotado.

Igual que `core/db_timing.py`, la variable de contexto guarda un objeto
mutable: una dependencia que corre en una copia del contexto (un hilo, un
greenlet) ve el mismo objeto aunque no vea asignaciones nuevas.

Es Python puro a propósito: lo puede importar el adaptador de cualquier módulo.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

STAFF = "staff"
PLATFORM = "platform"
PREVIEW = "preview"
ANONYMOUS = "anonymous"
ACCOUNT_KINDS = (STAFF, PLATFORM, PREVIEW, ANONYMOUS)


@dataclass(slots=True)
class RequestContext:
    # `False` para las rutas que no se guardan (el sondeo de vida, los avisos
    # en tiempo real, el propio panel): tampoco se guardan sus eventos.
    captured: bool = True
    account_kind: str = ANONYMOUS
    restaurant_id: int | None = None
    account_id: int | None = None


_current: ContextVar[RequestContext | None] = ContextVar("resthub_request_context", default=None)


def start_request_context(*, captured: bool = True) -> RequestContext:
    context = RequestContext(captured=captured)
    _current.set(context)
    return context


def current_request_context() -> RequestContext | None:
    return _current.get()


def annotate_account(kind: str, *, account_id: int, restaurant_id: int | None = None) -> None:
    """Anota la cuenta reconocida. Fuera de una petición no hace nada."""
    context = _current.get()
    if context is None:
        return
    context.account_kind = kind
    context.account_id = account_id
    context.restaurant_id = restaurant_id
