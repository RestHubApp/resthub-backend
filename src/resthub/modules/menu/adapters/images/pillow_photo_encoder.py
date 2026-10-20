"""La foto de un plato, decodificada y escrita de nuevo con Pillow (ASVS V5.4.3).

Mirar la firma del archivo no alcanza: un archivo políglota (una imagen que
además es un HTML, un ZIP o un script) pasa esa prueba y se serviría tal cual.
Aquí la imagen se decodifica de verdad y se escribe una nueva desde los píxeles:

- solo con los decodificadores de JPEG, PNG y WebP (ningún otro formato que
  Pillow conozca se intenta leer);
- se gira según su orientación EXIF y después se tiran todos los metadatos
  (EXIF con la ubicación del celular, perfiles, comentarios);
- se reduce a 800 px de lado, como hace la interfaz antes de subirla;
- se guarda como WebP, solo el primer cuadro si estaba animada.

Una imagen que declara más píxeles de los razonables se rechaza antes de
decodificarla (una «bomba» de pocos KB que ocuparía gigas en memoria). Es
trabajo de CPU: corre en el ejecutor acotado de `core/cpu_bound.py`, fuera del
bucle de eventos.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from resthub.core.cpu_bound import run_cpu_bound
from resthub.core.logs import get_logger
from resthub.modules.menu.ports.photo_encoding import EncodedPhoto

# El lado más largo de la foto guardada, igual que el que usa la interfaz.
MAX_PHOTO_SIDE = 800
# Más que cualquier cámara de celular (unos 50 megapíxeles): lo que declare más
# no se decodifica.
MAX_SOURCE_PIXELS = 60_000_000
WEBP_QUALITY = 82
_FORMATS = ("JPEG", "PNG", "WEBP")
_WEBP = "image/webp"

logger = get_logger("resthub.menu.photos")


def reencode_photo(data: bytes) -> bytes | None:
    """Los bytes de una WebP nueva hecha con los píxeles de `data`; `None` si no es imagen."""
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as source:
            width, height = source.size
            if width * height > MAX_SOURCE_PIXELS:
                return None
            # Un JPEG grande se decodifica ya reducido: mucho menos trabajo.
            source.draft("RGB", (MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
            image = ImageOps.exif_transpose(source)
            image.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
            has_alpha = image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
            clean = image.convert("RGBA" if has_alpha else "RGB")
            clean.info = {}
            output = io.BytesIO()
            clean.save(output, format="WEBP", quality=WEBP_QUALITY, method=4)
    except Exception as error:  # noqa: BLE001 - Pillow falla con muchos tipos según el archivo
        logger.info("menu.photo_undecodable", error_type=type(error).__name__)
        return None
    return output.getvalue()


class PillowPhotoEncoder:
    async def reencode(self, data: bytes) -> EncodedPhoto | None:
        encoded = await run_cpu_bound(reencode_photo, data)
        return EncodedPhoto(content_type=_WEBP, data=encoded) if encoded is not None else None
