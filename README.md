# RestHub — backend

API de RestHub, un sistema de información para restaurantes pequeños:
pedidos por mesa o para llevar, menú, inventario e indicadores. Es
multi-restaurante desde el primer día: cada cuenta pertenece a un restaurante y
toda consulta se acota a él.

La documentación funcional (requisitos, casos de uso, modelo de datos) vive en
Notion. Este README cubre solo lo técnico.

## Stack

- Python 3.12 a 3.14, gestionado con [uv](https://docs.astral.sh/uv/)
- FastAPI, Pydantic 2, SQLAlchemy 2 asíncrono, Alembic
- PostgreSQL (asyncpg) en despliegue; SQLite (aiosqlite) para desarrollo local
- PyJWT y bcrypt para el acceso
- structlog para los logs
- ruff, Import Linter y pytest

## Correr en local

```bash
cp .env.example .env          # SQLite local por defecto, no hace falta nada más
uv sync
uv run alembic upgrade head
uv run python scripts/seed_dev.py
uv run uvicorn resthub.main:app --reload
```

La documentación interactiva queda en <http://localhost:8000/api/v1/docs> y el
esquema en `/api/v1/openapi.json`.

El seed crea "Restaurante Demo" con dos cuentas, ambas con contraseña
`resthub123`:

| Correo               | Rol                 |
| -------------------- | ------------------- |
| `admin@resthub.dev`  | Encargado (`admin`) |
| `mesero@resthub.dev` | Mesero (`waiter`)   |

Solo corre con `DEBUG=true` y contra una base local.

### Scripts

| Script                                | Para qué                                                       |
| ------------------------------------- | -------------------------------------------------------------- |
| `scripts/seed_dev.py`                 | Datos de prueba en la base local. Idempotente.                 |
| `scripts/create_restaurant.py`        | Alta de un restaurante y su primer encargado (no hay registro público). |
| `scripts/export_openapi.py [archivo]` | Vuelca el esquema OpenAPI sin levantar el servidor.            |

```bash
uv run python scripts/create_restaurant.py \
  --name "Cevichería Doña Rosa" --slug dona-rosa \
  --admin-email rosa@example.com --admin-name "Rosa Pérez" --generate
```

Sin `--password` ni `--generate`, la contraseña se pide por consola.

## Verificación

Lo mismo que corre CI (`.github/workflows/ci.yml`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run pytest -q
```

Los hooks de Git son shell puro y se activan una vez por clon:

```bash
git config core.hooksPath .githooks
```

`pre-commit` corre ruff y los contratos; `commit-msg` exige Conventional
Commits en español y rechaza líneas `Co-authored-by`.

## Arquitectura

Hexagonal estricta. El código vive en `src/resthub`:

```
core/                 lo compartido: config, base de datos, identidad, permisos,
                      tokens, bitácora, avisos en tiempo real (SSE), logs, IA
modules/<módulo>/
  domain/             entidades y reglas, Python puro
  ports/              lo que el negocio necesita, como Protocol
  use_cases/          un caso de uso por acción
  adapters/api/       routers y esquemas de FastAPI
  adapters/persistence/  modelos y repositorios de SQLAlchemy
main.py               raíz de composición: el único lugar que conoce a todos los módulos
```

Módulos actuales: `restaurants` (el restaurante propio) y `accounts` (acceso,
sesión y personal).

Import Linter hace cumplir cuatro contratos, declarados en `pyproject.toml`:

1. **Capas dentro de cada módulo**: `adapters → use_cases → ports → domain`;
   una capa solo importa las de abajo.
2. **El dominio no conoce la tecnología**: `domain`, `ports` y `use_cases` no
   importan FastAPI, SQLAlchemy, Pydantic, JWT, bcrypt ni ninguna otra
   dependencia de terceros.
3. **Los módulos no se importan entre sí.** Si uno necesita datos de otro, los
   lee por SQL desde un adaptador `directories.py` detrás de un puerto propio
   (por ejemplo, `accounts` lee `restaurants` así), o se conectan en `main.py`.
4. **El núcleo no depende de ningún módulo.**

Los contratos usan comodines sobre `resthub.modules.*`: un módulo nuevo queda
cubierto el día que se crea.

### Restaurante, roles y permisos

- `restaurant_id` sale siempre del token del principal, nunca del cuerpo ni de
  la URL. Toda tabla de negocio lo lleva.
- El JWT lleva `sub`, `role` y `restaurant_id`, pero rol, estado y restaurante
  se releen de la base en cada petición: desactivar una cuenta o un restaurante
  corta el acceso al instante.
- Dos roles fijos: `admin` (Encargado) y `waiter` (Mesero). Los permisos salen
  del mapa fijo de `core/permissions.py`; cada endpoint exige un permiso con
  `require_permission`, no un rol.
- `GET /api/v1/auth/me` devuelve usuario, restaurante y la lista de permisos
  con la que el frontend arma la navegación.

### Migraciones

```bash
uv run alembic revision --autogenerate -m "descripción en español"
uv run alembic upgrade head
```

Los archivos se nombran `NNNN_descripcion_en_espanol.py`. Un modelo nuevo se
registra en `alembic/env.py` y en `tests/conftest.py`;
`tests/test_migrations.py` falla si las migraciones y los modelos divergen.

## Variables de entorno

Se leen de `.env` (ver `.env.example`).

| Variable                     | Por defecto                        | Notas                                                   |
| ---------------------------- | ---------------------------------- | ------------------------------------------------------- |
| `APP_NAME`                   | `resthub-api`                      |                                                         |
| `APP_VERSION`                | `0.1.0`                            |                                                         |
| `DEBUG`                      | `true`                             | Con `false` exige PostgreSQL y un `JWT_SECRET_KEY` propio. |
| `LOG_LEVEL`                  | `INFO`                             |                                                         |
| `LOG_JSON`                   | `false`                            | `true` en despliegue: una línea JSON por evento.        |
| `DATABASE_URL`               | `sqlite+aiosqlite:///./resthub.db` | En despliegue, `postgresql+asyncpg://...`.              |
| `CORS_ALLOWED_ORIGINS`       | `["http://localhost:5173"]`        | Lista JSON.                                             |
| `FRONTEND_BASE_URL`          | `http://localhost:5173`            | Se envía a OpenRouter como `HTTP-Referer`.              |
| `JWT_SECRET_KEY`             | valor de desarrollo                | Mínimo 32 bytes.                                        |
| `JWT_ALGORITHM`              | `HS256`                            |                                                         |
| `ACCESS_TOKEN_TTL_SECONDS`   | `3600`                             |                                                         |
| `OPENROUTER_API_KEY`         | vacío                              | Vacío apaga la IA; el resto funciona igual.             |
| `OPENROUTER_MODEL`           | `deepseek/deepseek-v4.1-flash`     |                                                         |
| `OPENROUTER_FALLBACK_MODEL`  | `deepseek/deepseek-v4-flash-0731`  |                                                         |
| `OPENROUTER_TIMEOUT_SECONDS` | `45`                               |                                                         |
