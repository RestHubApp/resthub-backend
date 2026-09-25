"""Cifrado de contraseñas.

Vive en el núcleo porque varios módulos de dominio lo comparten y porque no
habla ningún tipo de negocio: entra un texto, sale un texto. Satisface el
puerto `PasswordHasher` por estructura, no por herencia, así que el núcleo
nunca importa un módulo de dominio.
"""

from __future__ import annotations

from functools import lru_cache

import bcrypt

DEFAULT_ROUNDS = 12


@lru_cache(maxsize=1)
def _placeholder_hash(rounds: int) -> str:
    """Hash válido que ninguna contraseña real reproduce.

    Se calcula una sola vez y bajo demanda: `bcrypt` con 12 rondas tarda
    cientos de milisegundos y no tiene por qué pagarlos el arranque.
    """
    return bcrypt.hashpw(b"cuenta-inexistente", bcrypt.gensalt(rounds=rounds)).decode()


class BcryptPasswordHasher:
    def __init__(self, rounds: int = DEFAULT_ROUNDS) -> None:
        self._rounds = rounds

    def hash(self, plain_password: str) -> str:
        salt = bcrypt.gensalt(rounds=self._rounds)
        return bcrypt.hashpw(plain_password.encode(), salt).decode()

    def verify(self, plain_password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(plain_password.encode(), password_hash.encode())
        except ValueError:
            return False

    def dummy_hash(self) -> str:
        return _placeholder_hash(self._rounds)
