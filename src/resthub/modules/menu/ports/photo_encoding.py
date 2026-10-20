"""Puerto hacia quien vuelve a codificar la foto de un plato.

El dominio solo mira la firma del archivo; decodificar la imagen y escribirla de
nuevo (sin metadatos, con un tamaño acotado) es trabajo de una librería de
imágenes, que vive en un adaptador.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EncodedPhoto:
    content_type: str
    data: bytes


class PhotoEncodingGateway(Protocol):
    async def reencode(self, data: bytes) -> EncodedPhoto | None:
        """La foto decodificada y escrita de nuevo; `None` si no se pudo leer como imagen."""
        ...
