from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Valor de relleno para que el proyecto arranque recién clonado. No es un
# secreto: el validador de abajo impide que sobreviva fuera de depuración.
INSECURE_DEFAULT_SECRET = "cambiame-solo-sirve-en-desarrollo"

# RFC 7518, sección 3.2: una clave HMAC más corta que la salida de la función
# de hash debilita la firma. Para SHA-256 eso son 32 bytes.
MIN_SECRET_LENGTH = 32

# Las plataformas (Railway, Heroku, Render) entregan la URL de PostgreSQL sin
# controlador, y SQLAlchemy asíncrono necesita que diga cuál usar.
_BARE_POSTGRES_SCHEMES = ("postgres://", "postgresql://")
ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg://"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "resthub-api"
    app_version: str = "0.1.0"
    debug: bool = True

    # Nivel mínimo que se escribe: DEBUG, INFO, WARNING o ERROR.
    log_level: str = "INFO"
    # En desarrollo los logs salen en consola con color. En un despliegue van
    # como una línea JSON por evento, que es lo que un recolector sabe indexar.
    log_json: bool = False

    database_url: str = "sqlite+aiosqlite:///./resthub.db"
    # Se acepta como lista JSON o separada por comas. `NoDecode` evita que
    # pydantic-settings exija JSON antes de que el validador vea el texto.
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    # Dónde vive el frontend. No es lo mismo que CORS: ese protege al servidor,
    # este es el origen que se declara ante OpenRouter como `HTTP-Referer`.
    frontend_base_url: str = "http://localhost:5173"

    # Permiso explícito para que `seed_dev` y `seed_history` escriban en una base
    # que no es local o con `DEBUG=false`. Solo en el entorno de demostración:
    # siembran cuentas con una contraseña que está escrita en el repositorio.
    allow_demo_seed: bool = False

    jwt_secret_key: str = INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600

    # IA por OpenRouter. Sin clave queda apagada y el resto de la aplicación
    # funciona igual. Precios por millón de tokens en septiembre de 2026:
    # DeepSeek V4.1 Flash US$ 0.15 de entrada y 0.60 de salida; V4 Flash 0731,
    # US$ 0.06 y 0.12. El respaldo responde si el principal falla.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4.1-flash"
    openrouter_fallback_model: str = "deepseek/deepseek-v4-flash-0731"
    openrouter_timeout_seconds: float = 45.0

    # Decisiones con Jev, de TypeSafe AI: un modelo que no escribe texto sino
    # que elige entre opciones y dice con cuánta confianza. Sin clave, las
    # mismas decisiones las toman reglas fijas y la aplicación funciona igual.
    # Cobra US$ 0.042 por millón de tokens de entrada; la salida es gratis.
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-latest"
    # Corto a propósito: una decisión que tarda más que esto se toma con las
    # reglas. El encargado no espera a la IA para saber qué comprar.
    typesafe_timeout_seconds: float = 3.0
    # Por debajo de esta confianza la respuesta de Jev se descarta y deciden
    # las reglas. 0.5 es el piso que sugiere TypeSafe: más abajo el modelo
    # está diciendo que no sabe.
    ai_min_confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("database_url")
    @classmethod
    def _use_async_driver(cls, value: str) -> str:
        for scheme in _BARE_POSTGRES_SCHEMES:
            if value.startswith(scheme):
                return ASYNC_POSTGRES_SCHEME + value.removeprefix(scheme)
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        # Un origen nunca lleva barra final: el navegador manda
        # `https://app.example.com` y con la barra no coincidiría.
        return [origin.strip().rstrip("/") for origin in text.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _validate_secret(self) -> Settings:
        if len(self.jwt_secret_key.encode()) < MIN_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET_KEY necesita al menos {MIN_SECRET_LENGTH} bytes.")
        if not self.debug and self.jwt_secret_key == INSECURE_DEFAULT_SECRET:
            raise ValueError(
                "JWT_SECRET_KEY conserva el valor de desarrollo. "
                "Define uno propio antes de desplegar con DEBUG=false."
            )
        if not self.debug and self.database_url.startswith("sqlite"):
            raise ValueError(
                "DATABASE_URL apunta a SQLite en producción. "
                "Configura una base de datos PostgreSQL antes de desplegar con DEBUG=false."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
