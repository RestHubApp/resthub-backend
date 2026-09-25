from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Valor de relleno para que el proyecto arranque recién clonado. No es un
# secreto: el validador de abajo impide que sobreviva fuera de depuración.
INSECURE_DEFAULT_SECRET = "cambiame-solo-sirve-en-desarrollo"

# RFC 7518, sección 3.2: una clave HMAC más corta que la salida de la función
# de hash debilita la firma. Para SHA-256 eso son 32 bytes.
MIN_SECRET_LENGTH = 32


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
    cors_allowed_origins: list[str] = ["http://localhost:5173"]
    # Dónde vive el frontend. No es lo mismo que CORS: ese protege al servidor,
    # este es el origen que se declara ante OpenRouter como `HTTP-Referer`.
    frontend_base_url: str = "http://localhost:5173"

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
