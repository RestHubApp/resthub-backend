from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from resthub.core.data_keys import DEV_KEY, DataEncryptionError, parse_key

# Valor de relleno para que el proyecto arranque recién clonado. No es un
# secreto: el validador de abajo impide que sobreviva fuera de depuración.
# Ruff (S105) y Bandit (B105) lo toman por una contraseña en duro; por lo mismo
# es un falso positivo y se suprime solo en esta línea (RNFS-03).
INSECURE_DEFAULT_SECRET = "cambiame-solo-sirve-en-desarrollo"  # noqa: S105  # nosec B105

# RFC 7518, sección 3.2: una clave HMAC más corta que la salida de la función
# de hash debilita la firma. Para SHA-256 eso son 32 bytes.
MIN_SECRET_LENGTH = 32

# Las plataformas (Railway, Heroku, Render) entregan la URL de PostgreSQL sin
# controlador, y SQLAlchemy asíncrono necesita que diga cuál usar.
_BARE_POSTGRES_SCHEMES = ("postgres://", "postgresql://")
ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg://"


class Settings(BaseSettings):
    # `hide_input_in_errors`: un error de validación no copia los valores de entrada, que
    # incluyen llaves y secretos, al mensaje que acaba en el log del despliegue.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "resthub-api"
    app_version: str = "0.1.0"
    debug: bool = True

    # Nivel mínimo que se escribe: DEBUG, INFO, WARNING o ERROR.
    log_level: str = "INFO"
    # En desarrollo los logs salen en consola con color. En un despliegue van
    # como una línea JSON por evento, que es lo que un recolector sabe indexar.
    log_json: bool = False

    # Panel de observabilidad de la plataforma: cada petición y cada aviso se
    # guardan en la propia base (`obs_requests`, `obs_events`) y se borran
    # pasada la retención. Apagarlo deja la aplicación igual, sin telemetría.
    observability_enabled: bool = True
    observability_retention_days: int = Field(default=14, ge=1, le=365)
    # Filas por minuto (peticiones más eventos) que guarda cada proceso; lo que
    # pasa se descarta y se cuenta. 0 quita el tope.
    observability_max_rows_per_minute: int = Field(default=6_000, ge=0)

    database_url: str = "sqlite+aiosqlite:///./resthub.db"
    # Plazo de una petición (`core/request_deadline.py`): menor que los 15 s
    # con que el frontend da una petición por perdida, para que el servidor
    # corte primero y conteste un 503 que la pantalla pueda mostrar.
    request_deadline_seconds: float = Field(default=12.0, gt=0)
    # Peticiones por minuto que acepta cada IP (`core/rate_limit.py`). Alto a
    # propósito: los celulares de un local comparten la IP de su conexión.
    # 0 lo apaga.
    rate_limit_per_minute: int = Field(default=1_200, ge=0)
    # Con un WAF por delante (Cloudflare), la última IP de X-Forwarded-For es
    # la del WAF y todos los clientes parecerían uno. Esta cabecera, si viene,
    # trae la IP real (`cf-connecting-ip` en Cloudflare). Vacía: X-Forwarded-For.
    client_ip_header: str = ""
    # Base GeoLite2 City (`.mmdb`) para ubicar los accesos por ciudad y país sin
    # mandar la IP a terceros. Vacía: el historial guarda los accesos sin ubicación.
    geoip_database_path: str = ""
    # La cookie de sesión (S2): `Secure` y `SameSite=None` porque el frontend y el
    # API están en sitios distintos. En local por http, `SESSION_COOKIE_SECURE=false`
    # y `SESSION_COOKIE_SAMESITE=lax`.
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "none"
    # Días que se guarda el historial de accesos (RFS-15, RNFP-12).
    access_history_retention_days: int = Field(default=90, ge=1, le=365)
    # Cuánto vale la contraseña confirmada para las acciones críticas (RFS-21). El
    # fuzzing de la API lo alarga: su corrida dura más que los cinco minutos.
    reauthentication_window_seconds: int = Field(default=300, ge=60, le=86_400)
    # Se acepta como lista JSON o separada por comas. `NoDecode` evita que
    # pydantic-settings exija JSON antes de que el validador vea el texto.
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    # Además de la lista, un patrón para orígenes que cambian en cada despliegue
    # (los previews de Vercel). El origen tiene que calzar entero con él.
    cors_allowed_origin_regex: str | None = None
    # Dónde vive el frontend. No es lo mismo que CORS: ese protege al servidor,
    # este es el origen que se declara ante OpenRouter como `HTTP-Referer`.
    frontend_base_url: str = "http://localhost:5173"

    # Permiso explícito para que `seed_dev` y `seed_history` escriban en una base
    # que no es local o con `DEBUG=false`. Solo en el entorno de demostración:
    # siembran cuentas con una contraseña que está escrita en el repositorio.
    allow_demo_seed: bool = False

    # Costo de Argon2id para las contraseñas nuevas (`core/security.py`). Los hashes que ya
    # existen con otro costo, o con bcrypt, se rehashean solos al iniciar sesión. Las pruebas
    # los bajan al mínimo; en un despliegue no hay motivo para tocarlos sin medir.
    password_argon2_time_cost: int = Field(default=3, ge=1, le=10)
    password_argon2_memory_kib: int = Field(default=65_536, ge=8, le=1_048_576)
    password_argon2_parallelism: int = Field(default=1, ge=1, le=16)

    # Trazas OpenTelemetry (`core/tracing.py`). Sin endpoint no se instala nada. El endpoint es la
    # base de un colector OTLP/HTTP (la ruta `/v1/traces` se agrega sola); las cabeceras son
    # `clave=valor,otra=valor` para un colector con credencial; el muestreo es la fracción de
    # trazas nuevas que se guardan (1 = todas).
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: str | None = None
    otel_service_name: str | None = None
    otel_traces_sampler_arg: float = Field(default=1.0, ge=0, le=1)

    jwt_secret_key: str = INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    # Prevención de fugas internas: más de `DLP_ALERT_MAX_CARDS` fichas de clientes
    # abiertas o exportadas por una cuenta en `DLP_ALERT_WINDOW_MINUTES` minutos
    # dejan una alerta en la bitácora.
    dlp_alert_max_cards: int = Field(default=20, ge=1)
    dlp_alert_window_minutes: int = Field(default=10, ge=1)

    # Cifrado en reposo de datos personales (Ley N.º 29733): 32 bytes en base64
    # (`python -m resthub.core.data_keys`). Vacía solo se admite con DEBUG=true, donde se
    # usa una llave pública de desarrollo. Las anteriores, separadas por comas,
    # siguen sirviendo para leer mientras se rota la llave.
    data_encryption_key: SecretStr = SecretStr("")
    data_encryption_previous_keys: SecretStr = SecretStr("")

    # Lycet (API REST de Greenter) firma los comprobantes y los envía a SUNAT. Uno solo sirve
    # a todos los locales: cada uno registra en él su RUC, usuario SOL y certificado. Sin
    # URL o sin token, los comprobantes quedan «sin enviar». Nunca se escriben en logs.
    lycet_url: str = ""
    lycet_token: SecretStr = SecretStr("")

    # Correo de los comprobantes por la API HTTPS de Brevo (Railway bloquea SMTP saliente).
    # Sin clave o sin remitente verificado, la función queda apagada y no se ofrece el botón.
    brevo_api_key: str = ""
    email_from_address: str = ""
    email_from_name: str = "RestHub"
    # Envíos de comprobantes por correo que puede hacer una cuenta en una hora.
    email_max_per_hour: int = Field(default=20, ge=1, le=1_000)

    # Respaldo de XML y PDF en Google Drive con OAuth de la cuenta del dueño (refresh token).
    # Con alguna vacía, el respaldo queda apagado. Se obtienen con `scripts/drive_authorize.py`.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""
    google_drive_refresh_token: str = ""
    google_drive_folder_id: str = ""

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
    # Jev también se sirve por OpenRouter (mismo contrato `/v1/systemone`, con la
    # clave de OpenRouter). Se usa solo si no hay `TYPESAFE_API_KEY`. Cobra por
    # el mismo precio que TypeSafe directo, sin cuenta aparte.
    jev_openrouter_model: str = "~typesafe/jev-latest"

    # Autocompletar con RUC/DNI por Factiliza (https://docs.factiliza.com). Sin
    # token la función queda apagada y el frontend no ofrece el botón. El token
    # nunca se escribe en los logs.
    factiliza_token: str = ""
    factiliza_base_url: str = "https://api.factiliza.com/v1"
    # Corto: quien factura espera la respuesta y puede escribir los datos a mano.
    factiliza_timeout_seconds: float = Field(default=5.0, gt=0)
    # Cuánto se recuerda en memoria una consulta resuelta (ahorra cuota del plan).
    lookup_cache_seconds: int = Field(default=600, ge=0)
    # Consultas por minuto de cada cuenta. 0 lo apaga.
    lookup_per_minute: int = Field(default=10, ge=0)

    @field_validator("database_url")
    @classmethod
    def _use_async_driver(cls, value: str) -> str:
        for scheme in _BARE_POSTGRES_SCHEMES:
            if value.startswith(scheme):
                return ASYNC_POSTGRES_SCHEME + value.removeprefix(scheme)
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        # Un origen nunca lleva barra final: el navegador manda
        # `https://app.example.com` y con la barra no coincidiría.
        return [origin.strip().rstrip("/") for origin in text.split(",") if origin.strip()]

    @field_validator("cors_allowed_origin_regex")
    @classmethod
    def _parse_origin_regex(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            re.compile(value)
        except re.error as error:
            message = f"CORS_ALLOWED_ORIGIN_REGEX no es una expresión válida: {error}"
            raise ValueError(message) from error
        return value

    @model_validator(mode="after")
    def _validate_secret(self) -> Settings:
        if len(self.jwt_secret_key.encode()) < MIN_SECRET_LENGTH:
            message = f"JWT_SECRET_KEY necesita al menos {MIN_SECRET_LENGTH} bytes."
            raise ValueError(message)
        if not self.debug and self.jwt_secret_key == INSECURE_DEFAULT_SECRET:
            message = (
                "JWT_SECRET_KEY conserva el valor de desarrollo. "
                "Define uno propio antes de desplegar con DEBUG=false."
            )
            raise ValueError(message)
        self._validate_encryption_keys()
        if not self.debug and self.database_url.startswith("sqlite"):
            message = (
                "DATABASE_URL apunta a SQLite en producción. "
                "Configura una base de datos PostgreSQL antes de desplegar con DEBUG=false."
            )
            raise ValueError(message)
        return self

    def _validate_encryption_keys(self) -> None:
        primary = self.data_encryption_key.get_secret_value().strip()
        try:
            primary_key = parse_key(primary) if primary else None
            for part in self.data_encryption_previous_keys.get_secret_value().split(","):
                if part.strip():
                    parse_key(part)
        except DataEncryptionError as error:
            message = f"DATA_ENCRYPTION_KEY o DATA_ENCRYPTION_PREVIOUS_KEYS inválida: {error}"
            raise ValueError(message) from None
        if not self.debug and (primary_key is None or primary_key == DEV_KEY):
            message = (
                "DATA_ENCRYPTION_KEY falta o es la de desarrollo. Genera una con "
                "`python -m resthub.core.data_keys` y defínela antes de desplegar con DEBUG=false."
            )
            raise ValueError(message)


@lru_cache
def get_settings() -> Settings:
    return Settings()
