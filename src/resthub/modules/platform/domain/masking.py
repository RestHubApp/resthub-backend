"""Cómo aparece un correo en la bitácora de la plataforma.

`platform_activity` no se edita y dura dos años, más que la cuenta: la baja del
personal anonimiza el correo a los 12 meses y en `users` va cifrado. Por eso la
bitácora guarda el id de la cuenta y el correo tapado, nunca el correo entero.
"""

from __future__ import annotations


def mask_email(address: str) -> str:
    """«rosa@donarosa.pe» queda «r•••@donarosa.pe»; el dominio se deja a la vista."""
    local, _, domain = address.strip().partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


def owner_label(owner_id: int, email: str) -> str:
    """«#12 r•••@donarosa.pe»: basta para saber de qué cuenta se habla."""
    return f"#{owner_id} {mask_email(email)}"
