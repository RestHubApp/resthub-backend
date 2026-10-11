"""Cifrado de contraseñas.

Vive en el núcleo porque varios módulos de dominio lo comparten y porque no
habla ningún tipo de negocio: entra un texto, sale un texto. Satisface el
puerto `PasswordHasher` por estructura, no por herencia, así que el núcleo
nunca importa un módulo de dominio.

Las contraseñas nuevas se guardan con Argon2id. Los hashes bcrypt que ya
existían se siguen verificando y `verify_and_update` devuelve el hash nuevo
para que quien autentica lo guarde: la migración ocurre sola, al iniciar
sesión, sin pedirle nada a nadie ni dejar cuentas sin entrar.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from resthub.core.config import Settings

# Argon2id con la variante de poca memoria de la RFC 9106 (sección 4): 3 pasadas y 64 MiB. Un
# solo carril (`parallelism = 1`): cada verificación ocupa un hilo del ejecutor con tope
# (`core/cpu_bound`) y no cuatro, que en las instancias pequeñas de un despliegue se disputarían
# los mismos núcleos. La dureza la pone la memoria, no el paralelismo. Todo se puede subir
# desde `Settings`; las pruebas lo bajan al mínimo.
DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST_KIB = 65_536
DEFAULT_PARALLELISM = 1
_PLACEHOLDER_PASSWORD = "cuenta-inexistente"  # noqa: S105  # nosec B105 - relleno, no una credencial


@dataclass(frozen=True, slots=True)
class HashCost:
    """El costo de Argon2id con que se crean los hashes nuevos."""

    time_cost: int = DEFAULT_TIME_COST
    memory_cost_kib: int = DEFAULT_MEMORY_COST_KIB
    parallelism: int = DEFAULT_PARALLELISM


def _build(cost: HashCost) -> PasswordHash:
    # El primero hashea; el resto solo verifica lo antiguo. Si el hash que llega no es del
    # primero (bcrypt), o es de Argon2 con otros parámetros, `verify_and_update` pide el cambio.
    return PasswordHash(
        (
            Argon2Hasher(
                time_cost=cost.time_cost,
                memory_cost=cost.memory_cost_kib,
                parallelism=cost.parallelism,
            ),
            BcryptHasher(),
        )
    )


@lru_cache(maxsize=8)
def _placeholder_hash(cost: HashCost) -> str:
    """Hash válido que ninguna contraseña real reproduce.

    Se calcula una sola vez y bajo demanda: Argon2id con 64 MiB tarda decenas de
    milisegundos y no tiene por qué pagarlos el arranque.
    """
    return _build(cost).hash(_PLACEHOLDER_PASSWORD)


def hasher_from_settings(settings: Settings) -> Argon2PasswordHasher:
    return Argon2PasswordHasher(
        HashCost(
            time_cost=settings.password_argon2_time_cost,
            memory_cost_kib=settings.password_argon2_memory_kib,
            parallelism=settings.password_argon2_parallelism,
        )
    )


class Argon2PasswordHasher:
    def __init__(self, cost: HashCost | None = None) -> None:
        self._cost = cost or HashCost()
        self._hasher = _build(self._cost)

    def hash(self, plain_password: str) -> str:
        return self._hasher.hash(plain_password)

    def verify_and_update(self, plain_password: str, password_hash: str) -> tuple[bool, str | None]:
        """Si la contraseña es la del hash y, si conviene, el hash nuevo que lo reemplaza.

        El segundo valor es `None` cuando el hash ya es de Argon2id con el costo vigente. Un
        hash que no se reconoce o que bcrypt rechaza (más de 72 bytes) es «no coincide».
        """
        try:
            return self._hasher.verify_and_update(plain_password, password_hash)
        except (UnknownHashError, ValueError):
            return False, None

    def dummy_hash(self) -> str:
        return _placeholder_hash(self._cost)
