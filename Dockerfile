# Imagen de despliegue de la API (Railway u otro anfitrión de contenedores).
#
# Dos etapas: la primera instala las dependencias con uv a partir de `uv.lock`;
# la segunda copia solo el entorno ya armado y el código, sin uv ni cachés, y
# corre con un usuario sin privilegios.
#
#   docker build -t resthub-api .
#   docker run --rm -p 8000:8000 --env-file .env resthub-api

ARG PYTHON_VERSION=3.13

# -- Dependencias -------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS builder

# Versión fija de uv: la misma imagen se reconstruye igual mañana.
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

# Bytecode compilado de antemano (el usuario final no puede escribir en /app),
# copias en vez de enlaces (el entorno se muda de etapa) y el Python de la
# imagen, no uno descargado por uv.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Primero solo el lockfile: mientras no cambien las dependencias, esta capa se
# reutiliza aunque cambie el código.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

# -- Ejecución ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie

RUN groupadd --system --gid 10001 resthub \
    && useradd --system --uid 10001 --gid resthub --no-create-home resthub

WORKDIR /app
# Los archivos quedan del superusuario y `resthub` solo puede leerlos: la
# aplicación no escribe en disco, y así tampoco puede reescribir su código.
COPY --from=builder /app /app

# Railway (como casi todo anfitrión) termina TLS en su proxy y reenvía por la
# red interna: sin `FORWARDED_ALLOW_IPS` uvicorn ignora `X-Forwarded-*` y la
# API cree que todo llega por http desde la IP del proxy.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS="*"

USER resthub

EXPOSE 8000

# Las migraciones corren antes de levantar el servidor: si fallan, el
# contenedor no llega a escuchar, el sondeo de vida no responde y Railway deja
# en pie el despliegue anterior. `exec` le cede el PID 1 a uvicorn para que
# reciba la señal de apagado y cierre ordenado. Railway define `PORT`.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn resthub.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
