"""La foto de un plato.

Python puro. La foto vive en su propia tabla y no en la fila del plato, para que
leer la carta no arrastre los bytes de cada imagen: el plato solo guarda la
versión (`photo_version`), que la interfaz usa para pedir la imagen una vez y
reutilizarla mientras no cambie.

El tipo se deduce de los primeros bytes y no del que declara quien sube el
archivo: un archivo que se hace pasar por imagen no entra.
"""

from __future__ import annotations

from dataclasses import dataclass

from resthub.modules.menu.domain.exceptions import InvalidMenuItemError

# La interfaz reduce la foto antes de enviarla (800 px de lado, WebP o JPEG);
# este tope es el techo del servidor, no el tamaño normal.
MAX_PHOTO_BYTES = 400 * 1024

_JPEG = "image/jpeg"
_PNG = "image/png"
_WEBP = "image/webp"
_RIFF_SIZE = 12


@dataclass(frozen=True, slots=True)
class MenuPhoto:
    restaurant_id: int
    item_id: int
    content_type: str
    data: bytes


def detect_image_type(data: bytes) -> str | None:
    """JPEG, PNG o WebP según la firma del archivo; `None` si es otra cosa."""
    if data.startswith(b"\xff\xd8\xff"):
        return _JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if len(data) >= _RIFF_SIZE and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _WEBP
    return None


def validate_photo(data: bytes) -> str:
    """El tipo de la imagen, o `InvalidMenuItemError` si no sirve como foto."""
    if not data:
        message = "La foto está vacía."
        raise InvalidMenuItemError(message)
    if len(data) > MAX_PHOTO_BYTES:
        message = f"La foto no puede pasar de {MAX_PHOTO_BYTES // 1024} KB."
        raise InvalidMenuItemError(message)
    content_type = detect_image_type(data)
    if content_type is None:
        message = "La foto tiene que ser una imagen JPEG, PNG o WebP."
        raise InvalidMenuItemError(message)
    return content_type
