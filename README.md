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
y dos insumos arrancan bajo el mínimo, para ver esos casos en pantalla. Las
cuentas usan la contraseña `resthub123`:

| Correo               | Rol                                                     |
| -------------------- | ------------------------------------------------------- |
| `admin@resthub.dev`  | Encargado (`owner`)                                     |
| `mesero@resthub.dev` | Mesero (`waiter`)                                       |
| `cocina@resthub.dev` | Cocinero (`custom`): ve la carta, el tablero y el stock, mueve los pedidos en cocina; no cobra |

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
| `scripts/create_restaurant.py`        | Alta de un restaurante, sus roles Encargado y Mesero y su primer encargado (no hay registro público). |
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
| `restaurants` | El restaurante propio: nombre, zona horaria y tope de descuento del mesero. |
| `accounts`    | Acceso, sesión, personal, los roles de cada restaurante y la lectura de la bitácora. |
| `menu`        | Categorías y platos, con "disponible hoy" aparte de "en la carta".        |
| `orders`      | Mesas y pedidos: ciclo de cocina, tablero en vivo, cobro y caja.          |
| `inventory`   | Insumos, libro de movimientos de stock, recetas y costo por plato.        |
| `insights`    | Indicadores (BI) y decisiones de IA con Jev o reglas; posee `ai_decisions`. |
| `billing`     | Comprobantes electrónicos (boletas y facturas) y los datos fiscales del local. |
| `customers`   | La libreta de clientes frecuentes; visitas y gasto salen de sus pedidos.  |
| `reservations`| Reservas de mesa, sin cruces de horario en la misma mesa.                |

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
- Todo caso de uso que cambia un pedido lo lee con `SELECT … FOR UPDATE`
  (`OrderRepository.get(..., for_update=True)`): un cobro y un plato agregado a
  la vez, o un doble toque en «Cobrar», esperan su turno y leen lo ya guardado
  en vez de pisarse.
- El mesero ve sus pedidos y los activos de todos; el encargado
  (`orders.read_all`), todos.
- Cada cambio publica un aviso SSE `orders` con el id del pedido a todo el
  personal del restaurante.

### Delivery, clientes y reservas

- Un pedido `delivery` no ocupa mesa y exige nombre, teléfono y dirección
  (más una referencia opcional). Con `customer_id` de la libreta, lo que venga
  vacío se completa con los datos del cliente; sin `customer_id`, un teléfono
  que ya está en la libreta vincula el pedido a ese cliente. Sigue el mismo ciclo de cocina;
  «servido» es entregado.
- `customers` guarda nombre, teléfono (único en el local, sin espacios),
  correo, dirección, referencia y una nota (alergias, preferencias). Las
  visitas pagadas, el gasto y la última visita se calculan con `orders`; desde
  la tercera visita el cliente es frecuente.
- `reservations`: quién, cuántos, cuándo (con zona horaria), mesa opcional y
  duración (dos horas por omisión). Dos reservas vigentes de la misma mesa no
  se cruzan: guardar toma la fila de la mesa con `FOR UPDATE`, y editar o
  cerrar toma además la reserva. El cliente, si viene, tiene que ser del local
  (si no, 404). Se listan por día del local.
- `POST /orders` acepta `client_request_id`, que genera el celular: un
  reintento con el mismo valor (volvió la señal, doble toque) devuelve el
  pedido ya creado en vez de abrir otro. El índice único lo garantiza.

### Acceso: intentos y renovación

- Cinco contraseñas equivocadas para el mismo correo desde la misma IP
  bloquean ese par 15 minutos (429 con `Retry-After`); un acceso correcto
  limpia la cuenta. Vive en memoria de cada proceso (`core/login_throttle.py`),
  con tope de claves. La IP es la última de `X-Forwarded-For`, la que agrega el
  proxy; las anteriores las escribe el cliente.
- bcrypt corre en un hilo aparte (`asyncio.to_thread`): no frena al resto de
  las peticiones mientras verifica.
- `POST /auth/refresh` entrega un token nuevo para una sesión válida; el
  frontend lo pide antes de que venza, así el turno no se corta cada hora.

### Respaldos

- `scripts/backup_db.py` respalda con `pg_dump` (PostgreSQL, se restaura con
  `pg_restore`) o con la API de respaldo de sqlite3, y deja los últimos N.
- `.github/workflows/backup.yml` lo corre a diario a las 03:00 de Lima y
  guarda el volcado como artefacto por 30 días. Necesita el secreto
  `BACKUP_DATABASE_URL` (la `DATABASE_PUBLIC_URL` de Railway).

### Mover y unir mesas

- `POST /orders/{id}/move` cambia un pedido en mesa a otra mesa libre y activa
  (toma el turno del restaurante como al abrir, así dos meseros no ocupan la
  misma mesa).
- `POST /orders/{id}/merge` une otra mesa a esta, antes del primer pago: los
  ítems cambian de pedido con su identificador (el consumo de insumos es
  idempotente por ítem, así que un plato ya servido no se descuenta otra vez),
  el pedido que queda toma el estado menos avanzado de los dos y el otro queda
  cancelado con `merged_into_id`. Los reportes no lo cuentan como cancelado.

### Cobro y caja

```
total = subtotal (platos a precio de carta) − cortesías − descuento
saldo = total − pagos          propinas: aparte, no son venta
```

- Los meseros hacen de cajeros (`orders.charge`): cada uno cobra los pedidos
  que tomó; el encargado, cualquiera. Se cobra lo servido.
- Un pedido se paga con uno o varios pagos en `order_payments`: la cuenta
  entera (`POST /orders/{id}/charge`), una parte por monto (cuenta dividida en
  partes iguales, pago mixto) o los platos de alguien
  (`POST /orders/{id}/payments` con `item_ids`, el servidor calcula el monto con
  el descuento). Quien paga lo último cierra la cuenta con los céntimos del
  redondeo. Con lo pagado igual al total, el pedido pasa a `paid`; si se usó
  más de un medio, `orders.payment_method` guarda `mixed`.
- Cada pago puede traer `expected_balance`, el saldo que vio quien cobra: si
  no coincide (otro pago entró antes, o un doble toque), responde 409. Un pago
  por monto (`amount`) lo exige: es el único que se podría repetir.
- En efectivo, `amount_received` incluye la propina y el vuelto es recibido −
  monto − propina.
- Descuento por pedido en porcentaje, con motivo. El mesero, hasta
  `restaurants.max_waiter_discount_percent` (10 % por omisión, lo cambia el
  encargado con `PATCH /restaurant`); por encima, 403. El encargado
  (`orders.discount_any`) no tiene tope y además invita platos (cortesías, que
  se sirven y descuentan insumos pero no se cobran). Descuentos, cortesías y
  cancelaciones van antes del primer pago.
- Caja (`cash_sessions`): una sola abierta por local (índice único parcial).
  Solo el encargado (`cash.manage`) la abre con el efectivo inicial y la cierra
  con el efectivo contado; sin caja abierta no se cobra (409). El cierre fija
  el efectivo esperado (inicial + cobros en efectivo + propinas en efectivo) y
  la diferencia. El arqueo desglosa por medio de pago y por mesero; la propina
  es del mesero que atendió el pedido, aunque haya cobrado otro.
- Un cobro toma la fila de la caja abierta con `FOR UPDATE`: un cierre que
  empieza en ese momento espera, así el arqueo no deja afuera un pago.
- Abrir y cerrar la caja publica el aviso SSE `cash` a todo el personal.

### Opciones de los platos y platos sin insumos

- Un plato tiene grupos de opciones (`menu_items.modifier_groups`, JSON):
  nombre, mínimo y máximo de elecciones y opciones con precio adicional. Un
  grupo con mínimo uno es obligatorio. Al pedir, cada ítem manda lo elegido
  (`modifiers: [{group, option}]`); el servidor lo valida contra la carta y lo
  congela en `order_items.modifiers` con su precio, ya sumado a `unit_price`.
- Con `restaurants.auto_out_of_stock` (activado por omisión), un plato cuya
  receta pide de algún insumo más de lo que hay en stock sale `out_of_stock` en
  la carta y no se puede pedir. `menu` y `orders` lo calculan por SQL sobre
  `recipe_lines` y `stock_movements`. Se apaga desde el inventario si el local
  todavía no registra sus compras; con la regla apagada, el stock negativo
  sigue permitido como antes.

### Compras: proveedores y órdenes de compra

- `suppliers`, `purchase_orders` y `purchase_order_lines` viven en `inventory`.
- Una orden nace como borrador (se edita entera), se marca enviada y se
  recibe con lo que llegó y el costo real: cada línea recibida se registra con
  `RegisterPurchase`, así el stock y el costo ponderado se actualizan igual que
  en una compra a mano (motivo «OC n · proveedor»). Recibir toma la orden con
  `FOR UPDATE` y no se puede recibir dos veces.
- `GET /inventory/purchase-suggestions` dice qué pedir y cuánto: los insumos
  bajo el mínimo o con menos de 3 días de cobertura, hasta cubrir una semana
  de consumo o el doble del mínimo. La IA de reposición dice cuándo; esto,
  cuánto.

### Comprobantes electrónicos (SUNAT)

- `billing` guarda los datos fiscales de cada local (`billing_settings`: RUC,
  razón social, dirección, tasa de IGV, series y la conexión con el proveedor)
  y los comprobantes (`invoices`). Lee los pedidos pagados por SQL.
- Los precios de la carta incluyen IGV: base = total × 100 / (100 + tasa). La
  tasa es configurable (18 % en el régimen general; un restaurante MYPE acogido
  a la Ley 31556 carga la tasa reducida vigente).
- Reglas antes de emitir: la factura exige RUC (11 dígitos) y razón social;
  una boleta de más de S/ 700 exige documento; un pedido tiene un solo
  comprobante; cada serie lleva su correlativo (numerar toma la fila de datos
  fiscales con `FOR UPDATE`, creándola con los valores por omisión si el local
  no la tiene, y un índice único es la última palabra: si choca, 409). No se
  usa la fila del restaurante: el turno dura hasta que responde el proveedor y
  frenaría la toma de pedidos. La fecha de emisión y los filtros por día son
  los del local.
- El envío va por el puerto `ElectronicInvoicer`; el adaptador de Nubefact
  (`adapters/sunat/nubefact.py`, `httpx`) arma el JSON de «generar_comprobante»
  y guarda si SUNAT lo aceptó y el PDF. Sin datos fiscales o credenciales el
  comprobante queda `simulated` («sin enviar») y se reenvía después; un fallo
  de red lo deja `pending`, un rechazo `rejected`. El token nunca vuelve por el
  API ni se escribe en logs.
- Emitir exige `billing.issue` (mesero y encargado); configurar, listar y
  reenviar, `billing.manage`.

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
- Un consumo trae `order_id` y `order_number`, el número del día con que se
  nombra el pedido en el salón. `inventory` lo lee por SQL de la tabla `orders`
  desde su adaptador `directories`, sin importar el módulo.

### Indicadores (BI)

`insights` no tiene datos propios salvo las decisiones: lee `orders`,
`order_items`, `menu_items`, `recipe_lines`, `ingredients`, `stock_movements`,
`users` y `restaurants` por SQL desde `adapters/persistence/directories.py` y
agrega en código (`domain/sales.py`, `domain/stock.py`), así las cuentas son
las mismas en SQLite y en PostgreSQL.

- Los reportes aceptan `date_from` y `date_to` (días **del restaurante**, ambos
  incluidos); sin ellos, los últimos 30 días hasta hoy. Máximo 366 días.
- Una venta es un pedido `paid`, contado en su `business_date`, por su total
  (con descuentos y cortesías, sin propinas). La hora del mapa de calor es la de
  apertura del pedido, en la zona del restaurante.
- Los ingresos por medio de pago suman cada pago de `order_payments`: una cuenta
  mitad en efectivo y mitad con Yape cuenta en los dos. El reporte por mesero
  agrega sus propinas.
- Una cortesía cuenta como porción vendida pero no como ingreso del plato. El
  descuento del pedido no se reparte entre sus platos.
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
  Cada decisión trae su asunto nombrado como en el local: `order_number` (el
  número del día) si es un pedido o un plato de un pedido, y `subject_label`
  con el nombre del insumo o del plato (`null` para la nota del pedido entero).
- La confianza es honesta: `confidence` va de 0 a 1 solo cuando decide Jev, que
  reparte probabilidad entre las opciones. Las reglas guardan `null` a
  propósito: aplican un umbral o una palabra clave y responden igual cada vez,
  así que un 1.0 diría que nunca se equivocan (una merma con motivo ambiguo cae
  en `other` aunque no lo sea). `confidence_kind` dice de dónde sale: `model`
  ("Confianza del modelo") o `rule` ("Regla fija"), para que el panel muestre
  el porcentaje o la etiqueta.
- Las explicaciones escriben los decimales con punto (`1.76 kg`), como el panel
  en es-PE. Las guardadas antes con coma se corrigen al leerlas.
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
feriados y una leve subida), Yape como medio más usado, un turno de caja
cerrado por día con su arqueo y propinas, uno abierto hoy, algunos cancelados,
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
| GET    | `/permissions`                                | `roles.manage`                       |
| GET    | `/roles`                                      | `staff.manage` o `roles.manage`      |
| POST   | `/roles`                                      | `roles.manage`                       |
| PUT    | `/roles/{id}`                                 | `roles.manage`                       |
| DELETE | `/roles/{id}` (solo propio y sin personal)    | `roles.manage`                       |
| GET · POST | `/staff` (`?role_id=`)                    | `staff.manage`                       |
| GET · PATCH | `/staff/{id}`                            | `staff.manage`                       |
| PATCH  | `/staff/{id}/status`                          | `staff.manage`                       |
| POST   | `/staff/{id}/password`                        | `staff.manage`                       |
| GET    | `/activity` (`?role_id=`, `?kind=`)           | `activity.read`                      |
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
| POST   | `/orders/{id}/payments`                       | `orders.charge`                      |
| PUT    | `/orders/{id}/discount`                       | `orders.charge` (sin tope: `orders.discount_any`) |
| PUT    | `/orders/{id}/items/{item_id}/courtesy`       | `orders.discount_any`                |
| DELETE | `/orders/{id}/items/{item_id}/courtesy`       | `orders.discount_any`                |
| GET · POST | `/customers`                              | `customers.read` · `customers.manage` |
| GET · PUT | `/customers/{id}`                          | `customers.read` · `customers.manage` |
| GET · POST | `/reservations` (`?day=`)                 | `reservations.read` · `reservations.manage` |
| PUT    | `/reservations/{id}`                          | `reservations.manage`                |
| POST   | `/reservations/{id}/status?value=`            | `reservations.manage`                |
| POST   | `/auth/refresh`                               | sesión                               |
| POST   | `/orders/{id}/move`                           | `orders.take`                        |
| POST   | `/orders/{id}/merge`                          | `orders.take`                        |
| POST   | `/orders/{id}/cancel`                         | `orders.manage`                      |
| GET    | `/cash/current`                               | `orders.charge` (montos: `cash.manage`) |
| POST   | `/cash/open`                                  | `cash.manage`                        |
| POST   | `/cash/close`                                 | `cash.manage`                        |
| GET    | `/cash/sessions`                              | `cash.manage`                        |
| GET    | `/cash/sessions/{id}`                         | `cash.manage`                        |
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
| GET    | `/inventory/suppliers`                        | `inventory.read`                     |
| POST · PUT | `/inventory/suppliers`, `/inventory/suppliers/{id}` | `inventory.manage`        |
| GET    | `/inventory/purchase-suggestions`             | `inventory.read`                     |
| GET    | `/inventory/purchase-orders`, `/inventory/purchase-orders/{id}` | `inventory.read` |
| POST · PUT | `/inventory/purchase-orders`, `/inventory/purchase-orders/{id}` | `inventory.manage` |
| POST   | `/inventory/purchase-orders/{id}/send`, `/cancel`, `/receive` | `inventory.manage`   |
| GET · PUT | `/billing/settings`                        | `billing.manage`                     |
| POST   | `/billing/invoices`                           | `billing.issue`                      |
| GET    | `/billing/invoices`                           | `billing.manage`                     |
| GET    | `/billing/invoices/{id}`, `/billing/orders/{order_id}/invoice` | `billing.issue`     |
| POST   | `/billing/invoices/{id}/resend`               | `billing.manage`                     |
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
decisión). El aviso SSE `insights` llega a quien tiene `insights.read` cuando
se clasifican notas o mermas. El detalle de cada cuerpo y respuesta está en
`/api/v1/docs`. `/restaurant` suma `max_waiter_discount_percent`, que lee
cualquier cuenta y edita el encargado.

### Restaurante, roles y permisos

- `restaurant_id` sale siempre del token del principal, nunca del cuerpo ni de
  la URL. Toda tabla de negocio lo lleva.
- El JWT lleva solo `sub` y `restaurant_id` (uno anterior que traiga `role`
  sigue sirviendo; el rol se ignora). Rol, permisos, estado y restaurante se
  releen de la base en cada petición: desactivar una cuenta o un restaurante,
  cambiarle el rol a alguien o los permisos a un rol cambia el acceso al
  instante.
- Cada endpoint exige un permiso con `require_permission` (alcanza con uno de
  los pedidos), nunca un rol. El catálogo de permisos es fijo y vive en
  `core/permissions.py`, con etiqueta y grupo; `GET /permissions` lo devuelve
  en el orden de la interfaz.
- Los roles son de cada restaurante (tabla `roles`, del módulo `accounts`), y
  cada cuenta tiene uno (`users.role_id`). Hay tres clases (`kind`):
  - `owner` («Encargado»): uno por local, con **todos** los permisos,
    calculados del catálogo al leer; un permiso nuevo le llega solo. No se
    edita ni se borra.
  - `waiter` («Mesero»): uno por local. Sus permisos se editan (nace con ver
    menú y mesas, tomar y cobrar pedidos, emitir comprobantes, clientes y
    reservas); el nombre no cambia (un `PUT` con otro nombre responde 409) y no
    se borra.
  - `custom`: los que crea el local, como «Cocinero». Nombre (1 a 40
    caracteres, único en el local sin distinguir mayúsculas) y permisos
    editables; se borra solo si nadie lo tiene (409 si no).
- Nadie reparte lo que no tiene: crear o editar un rol con un permiso que uno
  no tiene, editar un rol que tiene alguno que uno no tiene, o darle a alguien
  un rol así, responde 403. Por lo mismo, con `staff.manage` no se edita,
  desactiva ni restablece la contraseña de una cuenta cuyo rol tiene permisos
  que uno no tiene (como la del encargado). Nadie cambia su propio rol (409).
- `GET /roles` devuelve `id`, `name`, `kind`, `permissions` (los que rigen),
  `member_count`, `is_editable` e `is_deletable`, con el encargado primero, el
  mesero después y los propios por nombre. `POST` y `PUT` reciben
  `{name, permissions}`; un código desconocido responde 422. Crear, editar y
  borrar queda en la bitácora; cambiar un rol avisa por SSE (`permissions`) a
  quienes lo tienen, que vuelven a pedir `/auth/me`.
- `GET /api/v1/auth/me` (y `POST /auth/login`) devuelve usuario (con `role_id`
  y `role_label`, el nombre del rol), restaurante (con su zona horaria,
  `timezone`) y la lista de permisos con la que el frontend arma la
  navegación. El personal se da de alta y se edita con `role_id`, un rol del
  mismo restaurante (404 si no); `/staff` y `/activity` filtran con
  `?role_id=`.
- Los avisos SSE llegan a las cuentas nombradas, a todo el local (`orders`,
  `cash`, `menu`) o a quien tenga uno de los permisos del aviso (`insights`),
  nunca por rol.

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
| `DATABASE_URL`               | `sqlite+aiosqlite:///./resthub.db` | En despliegue, PostgreSQL. `postgres://` y `postgresql://` pasan solas a `postgresql+asyncpg://`. |
| `CORS_ALLOWED_ORIGINS`       | `["http://localhost:5173"]`        | Lista JSON o separada por comas, sin barra final.       |
| `FRONTEND_BASE_URL`          | `http://localhost:5173`            | Se envía a OpenRouter como `HTTP-Referer`.              |
| `JWT_SECRET_KEY`             | valor de desarrollo                | Mínimo 32 bytes.                                        |
| `ALLOW_DEMO_SEED`            | `false`                            | Solo en el entorno de demostración: deja correr los seeds contra una base no local. |
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

## Despliegue en Railway

El repositorio trae un `Dockerfile` y un `railway.toml`. Railway construye la
imagen con el Dockerfile (no con Railpack) y la arranca con su `CMD`:
`alembic upgrade head` y después uvicorn en `0.0.0.0:$PORT`. Si la migración
falla, el servidor no llega a escuchar, el sondeo de `/api/v1/health` no
responde y el despliegue anterior sigue atendiendo.

La imagen es de dos etapas: uv instala las dependencias del `uv.lock` con
`uv sync --frozen --no-dev` y la imagen final (`python:3.13-slim`) lleva solo el
entorno, el código, las migraciones y los scripts, y corre con un usuario sin
privilegios. Se prueba igual en local:

```bash
docker build -t resthub-api .
docker run --rm -p 8000:8000 --env-file .env resthub-api
```

### Pasos

1. En un proyecto de Railway, agregar una base **PostgreSQL** y un servicio
   desde este repositorio de GitHub. Railway encuentra `railway.toml` y el
   Dockerfile por sí solo.
2. Cargar las variables del servicio (abajo).
3. En *Settings → Networking*, generar el dominio público.
4. Crear el primer restaurante (no hay registro público).

### Variables del servicio

| Variable               | Valor                                                                 |
| ---------------------- | --------------------------------------------------------------------- |
| `DATABASE_URL`         | `${{Postgres.DATABASE_URL}}`, referencia a la base del proyecto. Llega como `postgresql://` y la configuración la pasa a asyncpg. |
| `JWT_SECRET_KEY`       | Propio, de 32 bytes o más: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `DEBUG`                | `false`. Así la aplicación se niega a arrancar con SQLite o con la clave de ejemplo. |
| `LOG_JSON`             | `true`: una línea JSON por evento en los logs de Railway.             |
| `CORS_ALLOWED_ORIGINS` | El origen del frontend, por ejemplo `https://resthub.example.com` (varios, separados por comas). |
| `FRONTEND_BASE_URL`    | La misma URL del frontend.                                            |
| `TYPESAFE_API_KEY`     | Opcional. Sin ella, las decisiones las toman las reglas fijas.        |

`PORT` lo define Railway; no hay que cargarlo.

### Primer restaurante

`scripts/create_restaurant.py` está dentro de la imagen. Lo más directo es
correrlo en el contenedor desplegado, que ya tiene las variables y alcanza la
base por la red privada:

```bash
railway ssh -- python scripts/create_restaurant.py \
  --name "Cevichería Doña Rosa" --slug dona-rosa \
  --admin-email rosa@example.com --admin-name "Rosa Pérez" --generate
```

Con `--generate` la contraseña se imprime una sola vez; sin él, se pide por
consola. `--service` y `--environment` eligen otro servicio o entorno que el
enlazado.

Desde un clon local también sirve `railway run`, que corre el comando en la
máquina propia con las variables del servicio. La `DATABASE_URL` privada
(`*.railway.internal`) no se resuelve fuera de Railway, así que por esa vía se
le pasa la pública de la base (`DATABASE_PUBLIC_URL` del servicio Postgres):

```bash
railway run -- env DATABASE_URL="postgresql://…pública…" \
  uv run python scripts/create_restaurant.py --name "…" --slug … \
  --admin-email … --admin-name "…" --generate
```

### Datos de demostración (solo en el entorno demo)

`seed_dev.py` y `seed_history.py` se niegan a correr con `DEBUG=false` o contra
una base que no sea local: siembran cuentas con una contraseña que está escrita
en el repositorio. En un entorno de Railway que sea **solo** de demostración se
habilitan con `ALLOW_DEMO_SEED=true` en ese servicio, y después:

```bash
railway ssh -- python scripts/seed_dev.py
railway ssh -- python scripts/seed_history.py
```

Nunca en producción: las cuentas `admin@resthub.dev`, `mesero@resthub.dev` y
`cocina@resthub.dev` quedarían con la contraseña `resthub123`. Al terminar se puede quitar la
variable; los seeds no hacen falta para que la aplicación funcione.
