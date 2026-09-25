"""Respuesta HTTP que entrega un archivo guardado por la aplicación.

La usan los reportes en PDF. El nombre del archivo puede venir de un dato que
escribió alguien (el nombre del restaurante, por ejemplo), así que no se confía
en él para armar la cabecera: se deja una versión ASCII segura y la original va
codificada según RFC 6266.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Response

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _content_disposition(filename: str) -> str:
    fallback = _UNSAFE.sub("_", filename).strip("._") or "archivo"
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def inline_file_response(content: bytes, content_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": _content_disposition(filename),
            # El tipo lo fija quien genera el archivo; sin esto el navegador
            # podría adivinar otro y ejecutar el contenido.
            "X-Content-Type-Options": "nosniff",
            # Son datos del negocio: ningún caché compartido los guarda.
            "Cache-Control": "private, no-store",
        },
    )
