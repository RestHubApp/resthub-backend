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

El seed crea "Restaurante Demo" con una carta peruana de veinte platos en
cinco categorías, ocho mesas, unos treinta insumos con stock inicial (cargado
como compras) y recetas para casi todos los platos. Dos platos quedan sin receta
y dos insumos arrancan bajo el mínimo, para ver esos casos en pantalla. Las dos
cuentas usan la contraseña `resthub123`:

| Correo               | Rol                 |
| -------------------- | ------------------- |
| `admin@resthub.dev`  | Encargado (`admin`) |
| `mesero@resthub.dev` | Mesero (`waiter`)   |

Solo corre con `DEBUG=true` y contra una base local.

Para ver el panel de indicadores con datos, `scripts/seed_history.py` agrega
sesenta días de historia **sintética** (ver [Datos de demostración](#datos-de-demostración)):

```bash
uv run python scripts/seed_history.py
```

### Scripts

| Script                                | Para qué                                                       |
| ------------------------------------- | -------------------------------------------------------------- |
| `scripts/seed_dev.py`                 | Datos de prueba en la base local. Idempotente.                 |
| `scripts/seed_history.py`             | Sesenta días de historia sintética para el BI. Idempotente; después de `seed_dev`. |
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
                      tokens, bitácora, avisos en tiempo real (SSE), logs, IA,
                      tareas en segundo plano
modules/<módulo>/
  domain/             entidades y reglas, Python puro
  ports/              lo que el negocio necesita, como Protocol
  use_cases/          un caso de uso por acción
  adapters/api/       routers y esquemas de FastAPI
  adapters/persistence/  modelos y repositorios de SQLAlchemy
main.py               raíz de composición: el único lugar que conoce a todos los módulos
wiring/               conexiones entre módulos, también parte de la raíz de composición
```

Import Linter hace cumplir cinco contratos, declarados en `pyproject.toml`:

1. **Capas dentro de cada módulo**: `adapters → use_cases → ports → domain`;
   una capa solo importa las de abajo.
2. **El dominio no conoce la tecnología**: `domain`, `ports` y `use_cases` no
   importan FastAPI, SQLAlchemy, Pydantic, JWT, bcrypt ni ninguna otra
   dependencia de terceros.
3. **Los módulos no se importan entre sí.** Si uno necesita datos de otro, los
   lee por SQL desde un adaptador `directories.py` detrás de un puerto propio
   (por ejemplo, `accounts` lee `restaurants` así), o se conectan en `main.py`.
4. **El núcleo no depende de ningún módulo.**
5. **Los módulos no dependen de la raíz de composición** (`main` y `wiring`).

Los contratos usan comodines sobre `resthub.modules.*`: un módulo nuevo queda
cubierto el día que se crea.

## Módulos

| Módulo        | Qué posee                                                                 |
| ------------- | ------------------------------------------------------------------------- |
| `restaurants` | El restaurante propio: nombre y zona horaria.                             |
| `accounts`    | Acceso, sesión, personal y la lectura de la bitácora.                     |
| `menu`        | Categorías y platos, con "disponible hoy" aparte de "en la carta".        |
| `orders`      | Mesas y pedidos: ciclo de cocina, cobro y tablero en vivo.                |
| `inventory`   | Insumos, libro de movimientos de stock, recetas y costo por plato.        |
| `insights`    | Indicadores (BI) y decisiones de IA con Jev o reglas; posee `ai_decisions`. |

Los datos de otro módulo se leen por SQL desde `adapters/persistence/directories.py`
(por ejemplo, `orders` lee los precios de `menu_items` e `inventory` lee los
platos para sus recetas); nunca se escriben.

### Pedidos

```
open ──send──▶ in_kitchen ──ready──▶ ready ──served──▶ served ──charge──▶ paid
  └───────────────────── cancel (con motivo) ───────────────────┘──▶ cancelled
```

- Cada ítem guarda una foto del nombre y el precio del plato al pedirlo.
- Solo entran platos activos y disponibles hoy.
- Una mesa tiene a lo sumo un pedido activo (no pagado ni cancelado).
- Agregar platos a un pedido `ready` o `served` lo devuelve a `in_kitchen`.
  Quitar o cambiar platos solo se puede con el pedido `open`.
- `status_changed_at` marca cuándo entró al estado actual y solo lo mueve un
  cambio de estado (volver a cocina incluido); `updated_at` cambia con
  cualquier edición. El tablero mide con el primero el tiempo en cocina o listo.
- El número es correlativo por restaurante y por día **del restaurante**
  (`core/local_time`). Abrir un pedido bloquea la fila del restaurante
  (`SELECT … FOR UPDATE`) para numerar y comprobar la mesa de a uno; un índice
  único `(restaurant_id, business_date, number)` es la última palabra.
- Se cobra lo servido. En efectivo se anota el monto recibido (sin monto se
  entiende pago justo) y la respuesta trae el vuelto; con otro medio no hay
  monto.
- El mesero ve sus pedidos y los activos de todos; el encargado
  (`orders.read_all`), todos.
- Cada cambio publica un aviso SSE `orders` con el id del pedido a todo el
  personal del restaurante.

### Inventario y consumo automático

- El stock es la suma del libro de movimientos (`purchase`, `consumption`,
  `waste`, `adjustment`); nada se edita ni se borra.
- Una compra actualiza el costo del insumo por **promedio ponderado** con el
  stock que había; si no había stock, toma el costo de la compra.
- Un stock negativo se permite (el plato ya salió) y se reporta en las alertas.
- Al marcar un pedido `served`, `orders` avisa por su puerto
  `ServedOrderHook`. `main.py` reemplaza la implementación por omisión (que no
  hace nada) por `wiring/kitchen_consumption.py`, que llama al caso de uso
  `ConsumeServedOrder` de `inventory` en la misma transacción. El consumo es
  idempotente por ítem de pedido: volver a servir tras agregar platos solo
  descuenta los nuevos. Un plato sin receta no descuenta nada.

### Indicadores (BI)

`insights` no tiene datos propios salvo las decisiones: lee `orders`,
`order_items`, `menu_items`, `recipe_lines`, `ingredients`, `stock_movements`,
`users` y `restaurants` por SQL desde `adapters/persistence/directories.py` y
agrega en código (`domain/sales.py`, `domain/stock.py`), así las cuentas son
las mismas en SQLite y en PostgreSQL.

- Los reportes aceptan `date_from` y `date_to` (días **del restaurante**, ambos
  incluidos); sin ellos, los últimos 30 días hasta hoy. Máximo 366 días.
- Una venta es un pedido `paid`, contado en su `business_date`. La hora del mapa
  de calor es la de apertura del pedido, en la zona del restaurante.
- El margen por plato usa el costo de receta vigente (precio − costo de una
  porción) y lo que dejaron las porciones vendidas en el rango.
- Las mermas se agrupan por insumo y por causa; la causa sale de la última
  clasificación guardada, y lo que falta clasificar cuenta aparte.

### Decisiones con IA: Jev o reglas

Tres decisiones cerradas: qué hacer con cada insumo (`buy_today`,
`buy_this_week`, `wait`, `review_waste`, más una urgencia de 0 a 3), si una nota
de pedido menciona una alergia o restricción (y su tipo: `allergy`,
`preference`, `priority`, `other`) y la causa de una merma (`expiration`,
`mishandling`, `customer_return`, `preparation_error`, `other`).

- El puerto `DecisionEngine` tiene dos adaptadores: `JevDecisionEngine`
  (TypeSafe AI, `POST {TYPESAFE_BASE_URL}/v1/systemone` con `httpx`, sin el SDK)
  y `RuleBasedDecisionEngine` (palabras clave en castellano y umbrales de
  cobertura). `DecisionEngineSelector` elige: sin `TYPESAFE_API_KEY`, reglas;
  con clave, Jev, y si falla, pasa de `TYPESAFE_TIMEOUT_SECONDS`, responde algo
  ilegible o con confianza menor a `AI_MIN_CONFIDENCE`, reglas. Cada caída a
  reglas queda anotada (`fallback_reason`) y en el log.
- A Jev se le manda un `state` JSON con los números ya calculados en código y
  preguntas tipadas (`choice`, `score`, `noul`) con instrucciones y criterios en
  inglés. Una llamada por insumo, nota o merma, con todas sus preguntas; hasta
  seis en paralelo. Ante `429`/`529` reintenta una vez tras una pausa breve.
- La explicación en castellano de cada sugerencia la arma el código con los
  números; Jev no escribe texto.
- Toda decisión se guarda en `ai_decisions` (entrada, salida, motor, modelo y
  confianza) y se audita en `GET /insights/ai-decisions`.
- Al enviar un pedido a cocina (o agregarle platos con el pedido ya en cocina),
  `orders` avisa por su puerto `SentToKitchenHook`. `main.py` lo conecta con
  `wiring/kitchen_notes.py`: cuando la transacción se confirma, una tarea en
  segundo plano (`core/background.py`) abre su propia sesión, clasifica las
  notas pendientes y publica el aviso SSE `insights` con el id del pedido. La
  respuesta al mesero no espera a la IA, y una nota ya clasificada con el mismo
  texto no se vuelve a mandar.

### Datos de demostración

`scripts/seed_history.py` inventa, con semilla fija, sesenta días hasta ayer:
unos 1 900 pedidos pagados (más los fines de semana, en almuerzo y cena, con
feriados y una leve subida), Yape como medio más usado, algunos cancelados,
notas con alergias, consumos según receta, compras periódicas y mermas con
motivos en texto libre. Deja insumos en cada acción de reposición y tres
pedidos en cocina hoy. Crea dos meseros "(sintético)" y marca las compras como
"(histórico sintético)"; también mueve el "Stock inicial" de `seed_dev` al
primer día de la historia. No son datos reales: solo sirven para desarrollo.

## Endpoints

Todo cuelga de `/api/v1`. Los montos y cantidades viajan como texto decimal
(`"28.00"`). Un recurso de otro restaurante responde 404, igual que uno que no
existe.

| Método | Ruta                                          | Permiso                              |
| ------ | --------------------------------------------- | ------------------------------------ |
| GET    | `/menu`                                       | `menu.read`                          |
| POST   | `/menu/categories`                            | `menu.manage`                        |
| PUT    | `/menu/categories/order`                      | `menu.manage`                        |
| PATCH  | `/menu/categories/{id}`                       | `menu.manage`                        |
| DELETE | `/menu/categories/{id}` (solo vacía)          | `menu.manage`                        |
| PUT    | `/menu/categories/{id}/items/order`           | `menu.manage`                        |
| POST   | `/menu/items`                                 | `menu.manage`                        |
| GET    | `/menu/items/{id}`                            | `menu.read`                          |
| PATCH  | `/menu/items/{id}`                            | `menu.manage`                        |
| PATCH  | `/menu/items/{id}/availability`               | `menu.manage`                        |
| GET    | `/tables`                                     | `tables.read`                        |
| POST   | `/tables`                                     | `tables.manage`                      |
| PUT    | `/tables/order`                               | `tables.manage`                      |
| PATCH  | `/tables/{id}`                                | `tables.manage`                      |
| GET    | `/orders`                                     | `orders.take` u `orders.read_all`    |
| GET    | `/orders/active`                              | `orders.take` u `orders.read_all`    |
| GET    | `/orders/{id}`                                | `orders.take` u `orders.read_all`    |
| POST   | `/orders`                                     | `orders.take`                        |
| PATCH  | `/orders/{id}`                                | `orders.take`                        |
| POST   | `/orders/{id}/items`                          | `orders.take`                        |
| PATCH  | `/orders/{id}/items/{item_id}`                | `orders.take`                        |
| DELETE | `/orders/{id}/items/{item_id}`                | `orders.take`                        |
| POST   | `/orders/{id}/send`                           | `orders.take`                        |
| POST   | `/orders/{id}/ready`                          | `orders.manage`                      |
| POST   | `/orders/{id}/served`                         | `orders.take`                        |
| POST   | `/orders/{id}/charge`                         | `orders.charge`                      |
| POST   | `/orders/{id}/cancel`                         | `orders.manage`                      |
| GET    | `/inventory/ingredients`                      | `inventory.read`                     |
| POST   | `/inventory/ingredients`                      | `inventory.manage`                   |
| GET    | `/inventory/ingredients/{id}`                 | `inventory.read`                     |
| PATCH  | `/inventory/ingredients/{id}`                 | `inventory.manage`                   |
| GET    | `/inventory/alerts/low-stock`                 | `inventory.read`                     |
| GET    | `/inventory/movements`                        | `inventory.read`                     |
| POST   | `/inventory/purchases`                        | `inventory.manage`                   |
| POST   | `/inventory/waste`                            | `inventory.manage`                   |
| POST   | `/inventory/adjustments`                      | `inventory.manage`                   |
| GET    | `/inventory/recipes`                          | `inventory.read`                     |
| GET    | `/inventory/recipes/{menu_item_id}`           | `inventory.read`                     |
| PUT    | `/inventory/recipes/{menu_item_id}`           | `inventory.manage`                   |
| GET    | `/insights/summary`                           | `insights.read`                      |
| GET    | `/insights/sales/daily`                       | `insights.read`                      |
| GET    | `/insights/sales/hourly`                      | `insights.read`                      |
| GET    | `/insights/payments`                          | `insights.read`                      |
| GET    | `/insights/waiters`                           | `insights.read`                      |
| GET    | `/insights/dishes/top`                        | `insights.read`                      |
| GET    | `/insights/dishes/margins`                    | `insights.read`                      |
| GET    | `/insights/low-stock`                         | `insights.read`                      |
| GET    | `/insights/waste`                             | `insights.read`                      |
| POST   | `/insights/waste/classify`                    | `insights.read`                      |
| GET    | `/insights/restock`                           | `insights.read`                      |
| POST   | `/insights/restock/refresh`                   | `insights.read`                      |
| GET    | `/insights/order-notes?order_ids=…`           | `insights.read`                      |
| POST   | `/insights/order-notes/classify`              | `insights.read`                      |
| GET    | `/insights/ai-decisions`                      | `insights.read`                      |

Los `GET` de `/insights` nunca llaman a la IA; los `POST` sí (y guardan cada
decisión). El aviso SSE `insights` llega al encargado cuando se clasifican
notas o mermas. El detalle de cada cuerpo y respuesta está en `/api/v1/docs`.
Los endpoints de acceso, personal, restaurante y bitácora no cambian respecto
de la fase 1.

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
| `TYPESAFE_API_KEY`           | vacío                              | Jev (TypeSafe AI). Vacío: deciden las reglas fijas. Nunca se escribe en logs. |
| `TYPESAFE_BASE_URL`          | `https://api.typesafe.ai`          |                                                         |
| `TYPESAFE_MODEL`             | `jev-latest`                       | Alias; la respuesta guarda la versión que respondió.    |
| `TYPESAFE_TIMEOUT_SECONDS`   | `3`                                | Plazo por llamada; pasado, deciden las reglas.          |
| `AI_MIN_CONFIDENCE`          | `0.5`                              | Bajo esta confianza de Jev, deciden las reglas.         |
