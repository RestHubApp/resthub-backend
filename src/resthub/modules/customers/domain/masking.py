"""Enmascarado de los datos de contacto de un cliente (prevención de fugas).

Python puro. Quien puede buscar y ver clientes pero no gestionarlos recibe el
teléfono y el correo con el medio tapado: alcanza para reconocer a la persona,
no para copiar la libreta. El dato completo se pide aparte y queda en la bitácora.
"""

from __future__ import annotations

MASK = "•••"
_VISIBLE_EDGE = 3


def mask_phone(phone: str) -> str:
    """«987654321» queda «987•••321»; uno corto, solo el primer dígito."""
    compact = phone.replace(" ", "")
    if not compact:
        return ""
    if len(compact) <= 2 * _VISIBLE_EDGE:
        return f"{compact[0]}{MASK}"
    return f"{compact[:_VISIBLE_EDGE]}{MASK}{compact[-_VISIBLE_EDGE:]}"


def mask_email(email: str) -> str:
    """«ana@correo.pe» queda «a•••@correo.pe»; el dominio se deja a la vista."""
    if not email:
        return ""
    local, separator, domain = email.partition("@")
    if not separator:
        return MASK
    return f"{local[:1]}{MASK}@{domain}"
