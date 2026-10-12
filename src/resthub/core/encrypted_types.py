"""Tipos de columna que cifran al guardar y descifran al leer.

`EncryptedText` va en las columnas con datos personales. La aplicación ve
siempre texto en claro; la base solo ve `enc1:…`. Como cada cifrado lleva un
nonce distinto, no se puede filtrar ni ordenar por estas columnas: para
buscar por igualdad se guarda aparte un índice ciego (`blind_index`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from resthub.core.crypto import get_cipher


class EncryptedText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:  # noqa: ARG002 - firma de SQLAlchemy
        return None if value is None else get_cipher().encrypt(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:  # noqa: ARG002, ANN401 - firma de SQLAlchemy
        return None if value is None else get_cipher().decrypt(str(value))
