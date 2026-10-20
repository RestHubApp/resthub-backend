# RestHub — backend

API de RestHub, un sistema de información para restaurantes pequeños:
pedidos por mesa o para llevar, menú, inventario e indicadores. Es
multi-restaurante desde el primer día: cada cuenta del personal pertenece a un
restaurante y toda consulta se acota a él. Aparte está la administración del
sistema (el equipo de RestHub), que no pertenece a ningún local y los da de alta
(ver [Administración del sistema](#administración-del-sistema)).

La documentación funcional (requisitos, casos de uso, modelo de datos) vive en
Notion. Este README cubre solo lo técnico.

## Stack

- Python 3.12 a 3.14, gestionado con [uv](https://docs.astral.sh/uv/)
- FastAPI, Pydantic 2, SQLAlchemy 2 asíncrono, Alembic
- PostgreSQL (asyncpg) en despliegue; SQLite (aiosqlite) para desarrollo local
- PyJWT y Argon2id (con `pwdlib`) para el acceso
- structlog para los logs
- ruff, Import Linter y pytest

## Correr en local

```bash
cp .env.example .env          # SQLite local y DEBUG=true, no hace falta nada más
uv sync
uv run alembic upgrade head
uv run python scripts/seed_dev.py
uv run uvicorn resthub.main:app --reload
```

La documentación interactiva queda en <http://localhost:8000/api/v1/docs> y el
esquema en `/api/v1/openapi.json`.

`DEBUG` está apagado por omisión: sin `.env` (o sin `DEBUG=true` en el entorno) la
aplicación arranca en modo producción y se niega a hacerlo con SQLite, con el
`JWT_SECRET_KEY` de ejemplo o sin `DATA_ENCRYPTION_KEY`. Las pruebas lo encienden
solas (`[tool.pytest.ini_options] env` en `pyproject.toml`); `alembic`, los seeds,
`scripts/export_openapi.py` y cualquier guion que levante la app sobre SQLite lo
necesitan en el `.env` o en el entorno (`DEBUG=true uv run python scripts/export_openapi.py`).

El seed crea "Restaurante Demo" con una carta peruana de veinte platos en
cinco categorías, ocho mesas, unos treinta insumos con stock inicial (cargado
como compras) y recetas para casi todos los platos. Dos platos quedan sin receta
y dos insumos arrancan bajo el mínimo, para ver esos casos en pantalla. Los
datos salen de `src/resthub/wiring/sample_restaurant.py`, los mismos que usa el
local de muestra de la [vista previa](#vista-previa-local-de-muestra). Las
cuentas usan la contraseña `resthub123`:

| Correo               | Rol                                                     |
| -------------------- | ------------------------------------------------------- |
| `admin@resthub.dev`  | Encargado (`owner`)                                     |
| `mesero@resthub.dev` | Mesero (`waiter`)                                       |
| `cocina@resthub.dev` | Cocinero (`custom`): ve la carta, el tablero y el stock, mueve los pedidos en cocina; no cobra |
| `plataforma@resthub.dev` | Administración del sistema: no es de ningún local; entra por el mismo acceso (`POST /auth/login`) y su sesión trae `kind: "platform"` |

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
| `scripts/create_platform_admin.py`    | Alta de una cuenta de la administración del sistema (no hay registro público). |
| `scripts/set_platform_admin_status.py` | Activa o desactiva una cuenta de plataforma; desactivarla corta sus sesiones. |
| `scripts/revoke_all_sessions.py`      | Cierra todas las sesiones abiertas de un restaurante (`--restaurant <slug>`) o de todo el sistema (`--everyone`) ante un incidente. |
| `scripts/export_openapi.py [archivo]` | Vuelca el esquema OpenAPI sin levantar el servidor.            |
| `scripts/drive_authorize.py`          | Obtiene una vez el refresh token y la carpeta raíz de Google Drive para el respaldo de comprobantes. |

```bash
uv run python scripts/create_restaurant.py \
  --name "Cevichería Doña Rosa" --slug dona-rosa \
  --admin-email rosa@example.com --admin-name "Rosa Pérez" --generate
```

Sin `--password` ni `--generate`, la contraseña se pide por consola.

```bash
uv run python scripts/create_platform_admin.py \
  --email equipo@resthub.pe --name "Equipo RestHub" --generate
```

La contraseña de plataforma nunca va en la línea de comandos: sale de
`--password-stdin` (la primera línea de la entrada estándar), de `--generate`,
de la variable `PLATFORM_ADMIN_PASSWORD` o, si no, se pide por consola.

## Verificación

Lo mismo que corre CI (`.github/workflows/ci.yml`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run pytest -q
```

El contrato con el frontend (Pact) se verifica aparte, porque necesita el
grupo `contract`: levanta la aplicación en el puerto 8203 sobre una SQLite
nueva y reproduce el pact de `tests/contract/pacts/`, que generan las pruebas
de consumidor del frontend (`pnpm test:contract`).

```bash
uv sync --group dev --group contract
uv run pytest tests/contract -v -s
```

Los hooks de Git son shell puro y se activan una vez por clon:

```bash
git config core.hooksPath .githooks
```

`pre-commit` corre ruff y los contratos; `commit-msg` exige Conventional
Commits en español y rechaza líneas `Co-authored-by`.

La verificación contra OWASP ASVS 5.0 (niveles 1 y 2, RNFS-21) está en
[`docs/asvs-5.0.md`](docs/asvs-5.0.md), con el estado de cada requisito y las
brechas por prioridad.

## Arquitectura

Hexagonal estricta. El código vive en `src/resthub`:

```
core/                 lo compartido: config, base de datos, identidad, permisos,
                      tokens, bitácora, avisos en tiempo real (SSE), logs, IA,
                      tareas en segundo plano, captura de telemetría
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
   importan FastAPI, SQLAlchemy, Pydantic, JWT, Argon2 ni ninguna otra
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
| `inventory`   | Insumos, libro de stock, recetas, compras, conteos físicos y mermas.      |
| `insights`    | Indicadores (BI) y decisiones de IA con Jev o reglas; posee `ai_decisions`. |
| `billing`     | Comprobantes electrónicos (boletas y facturas) y los datos fiscales del local. |
| `customers`   | La libreta de clientes frecuentes; visitas y gasto salen de sus pedidos.  |
| `lookup`      | Autocompletar con RUC (SUNAT) o DNI (RENIEC) por un tercero, Factiliza; no guarda nada. |
| `reservations`| Reservas de mesa, sin cruces de horario en la misma mesa.                |
| `platform`    | La administración del sistema: sus cuentas, su acceso, su bitácora, el alta y gestión de restaurantes, la vista previa y el panel de observabilidad (`obs_requests`, `obs_events`). |

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
- Consentimiento (Ley N.º 29733): el alta exige `consent: true`, es decir, que
  el cliente aceptó el tratamiento de sus datos; sin eso responde 422. Se
  guarda cuándo (`consent_at`), sobre qué versión del texto
  (`consent_version`) y quién lo registró. Los clientes guardados antes de
  pedirlo quedan con `consent_at: null` hasta que acepten al editarse; lo ya
  aceptado no se reemplaza.
- Derechos ARCO (permiso `customers.erase`, solo el encargado por omisión):
  `GET /customers/{id}/export` devuelve todo lo que se guarda del cliente (su
  ficha, su consentimiento, sus pedidos y sus reservas) y
  `POST /customers/{id}/anonymize` borra sus datos de la ficha, de sus pedidos
  y de sus reservas; la ficha queda como «Cliente eliminado» para que las
  ventas sigan cuadrando y sale de la libreta. No se deshace; 409 si tiene
  pedidos en curso. Los comprobantes no se tocan (la ley tributaria obliga a
  conservarlos) ni la bitácora, que no se edita. Ambos quedan en la bitácora
  como «Cliente #id», sin el nombre. Los pedidos y reservas los lee y borra
  `wiring/customer_records.py`.
- Prevención de fugas internas: abrir la ficha (`GET /customers/{id}`) y exportarla
  quedan en la bitácora (`customer_viewed`, `customer_exported`) con quién y cuándo.
  Las cuentas sin `customers.manage` reciben el teléfono y el correo enmascarados
  (`987•••222`, `a•••@correo.pe`; `masked: true`) en la lista y la ficha, y
  `POST /customers/{id}/reveal` (`customers.read`) devuelve los completos y deja
  `customer_revealed`. Si una cuenta abre o exporta más de `DLP_ALERT_MAX_CARDS`
  fichas en `DLP_ALERT_WINDOW_MINUTES` minutos (20 en 10 por omisión), se registra
  `data_access_alert` (una por ventana y cuenta) y se avisa por SSE (tema
  `dlp_alert`) a quien tenga `activity.read`; la bitácora se filtra con
  `?kind=data_access_alert`.
- `reservations`: quién, cuántos, cuándo (con zona horaria), mesa opcional y
  duración (dos horas por omisión). Dos reservas vigentes de la misma mesa no
  se cruzan: guardar toma la fila de la mesa con `FOR UPDATE`, y editar o
  cerrar toma además la reserva. El cliente, si viene, tiene que ser del local
  (si no, 404). Se listan por día del local.
- `POST /orders` acepta `client_request_id`, que genera el celular: un
  reintento con el mismo valor (volvió la señal, doble toque) devuelve el
  pedido ya creado en vez de abrir otro. El índice único lo garantiza.

### Cifrado en reposo de datos personales

Teléfono, correo, dirección, referencia y notas del cliente se guardan cifrados
(AES-256-GCM, `cryptography`) en `customers`, y los mismos datos copiados en
`orders` (teléfono, dirección, referencia, notas), `reservations` (teléfono,
notas) y `invoices` (documento y dirección). El nombre no se cifra. Un volcado de
la base, un respaldo o una fuga de SQL muestran `enc1:…`, no el dato.

- **Cómo**: `core/crypto.py` cifra con un nonce aleatorio por valor y guarda
  `enc1:<id de llave>:<nonce+cifrado+etiqueta>`; `core/encrypted_types.py` es el
  tipo de columna (`EncryptedText`) que cifra al escribir y descifra al leer, así
  que casos de uso y API no cambian. Un valor vacío queda vacío.
- **Búsqueda**: lo cifrado no se puede filtrar. El teléfono tiene un índice ciego
  (`customers.phone_key` = HMAC-SHA256 del número sin espacios, con una llave
  derivada aparte): la libreta busca el **número completo** por igualdad, evita
  repetidos y vincula pedidos. La búsqueda por nombre sigue en claro y por partes.
- **Llave**: `DATA_ENCRYPTION_KEY`, 32 bytes en base64. Con `DEBUG=false` la
  aplicación no arranca sin ella; con `DEBUG=true` y vacía usa una llave pública de
  desarrollo. Si se pierde, los datos no se recuperan: guárdala en el gestor de
  secretos, aparte de la base y de sus respaldos.
  ```bash
  uv run python -m resthub.core.data_keys      # imprime una llave nueva
  ```
- **Migración `0026`**: cifra los datos existentes con la llave vigente (falla
  antes de tocar nada si en producción no hay llave) y su `downgrade` los descifra;
  esa vuelta también necesita la misma llave.
- **Rotar la llave**:
  1. Genera una llave nueva (comando de arriba).
  2. Pon la llave actual en `DATA_ENCRYPTION_PREVIOUS_KEYS` y la nueva en
     `DATA_ENCRYPTION_KEY`; reinicia. Lee con ambas y escribe con la nueva.
  3. Corre `uv run python scripts/rotate_data_key.py` (`--dry-run` solo cuenta):
     re-cifra todo con la nueva y recalcula el índice ciego. Es idempotente. Mientras
     corre, la búsqueda por teléfono no encuentra clientes.
  4. Sin filas por rotar, vacía `DATA_ENCRYPTION_PREVIOUS_KEYS`. Conserva la
     llave vieja mientras existan respaldos hechos con ella.
- Ningún log ni mensaje de error imprime un dato en claro ni la llave:
  `hide_parameters` en el motor, `hide_input_in_errors` en la configuración,
  `SecretStr` para las llaves y errores de descifrado sin contenido.

### Acceso: intentos y renovación

- Cinco contraseñas equivocadas para el mismo correo desde la misma IP
  bloquean ese par 15 minutos (429 con `Retry-After`); un acceso correcto
  limpia la cuenta. Cada intento se reserva antes de verificar la contraseña
  (`LoginThrottle.acquire`, sin `await` entre comprobar y anotar), así que una
  ráfaga de peticiones simultáneas tampoco evalúa más de cinco. Una cuenta
  desactivada responde el mismo 401 que una contraseña equivocada: un 403
  confirmaría que la contraseña era la correcta. Se cuenta dos veces: en la memoria
  del proceso (`core/login_throttle.py`, con tope de claves), que frena la ráfaga
  simultánea antes del primer `await`, y en la tabla `login_attempts`
  (`core/login_attempts.py`), compartida entre réplicas, con un candado por correo
  (`pg_advisory_xact_lock`) entre leer y anotar. La tabla guarda el SHA-256 del
  correo y de la IP, no el dato, y borra los intentos vencidos al anotar uno nuevo. La IP es la última de `X-Forwarded-For`, la que agrega el
  proxy; las anteriores las escribe el cliente.
- Las contraseñas se guardan con Argon2id (`core/security.py`, costo en `PASSWORD_ARGON2_*`; por
  omisión 3 pasadas, 64 MiB y un carril, unos 160 ms por hash). Los hashes bcrypt de antes se
  siguen verificando y se reemplazan por Argon2id en el primer acceso correcto, sin que nadie
  haga nada. El hash corre en un hilo aparte, en un ejecutor con tope de hilos
  (`core/cpu_bound.py`, la mitad de los núcleos entre 1 y 4): no frena al resto
  de las peticiones mientras verifica, y muchos accesos a la vez esperan su
  turno entre ellos en vez de ocupar todos los núcleos.
- Contraseña temporal (RFS-20): la que fija otra persona (el alta del personal,
  el restablecimiento del encargado, el alta o el restablecimiento de un encargado
  desde la plataforma, `scripts/create_restaurant.py`) deja la cuenta con
  `users.must_change_password`. La sesión lo trae (`must_change_password: true`)
  y, mientras siga así, el resto de la API responde 403
  `password_change_required`: solo pasan `GET /auth/me`, `POST /auth/refresh`,
  `POST /auth/logout`, el historial de accesos propio, el bloqueo por inactividad y
  `POST /auth/me/password`, que lo apaga. Una vista previa
  nunca lo pide.
- Contraseñas comunes (RFS-30): una contraseña nueva que esté en
  `core/common_passwords.txt` (las de 10 a 72 caracteres de las 100 000 más usadas,
  más algunas en español) responde 422, sin distinguir mayúsculas. La lista es
  local: la contraseña no sale a ningún servicio.
- `POST /auth/refresh` entrega un token nuevo para una sesión válida; el
  frontend lo pide antes de que venza, así el turno no se corta cada hora. Una
  sesión de [vista previa](#vista-previa-local-de-muestra) no se renueva (401).
- Vida máxima de la sesión (ASVS V7.3.2): `POST /auth/refresh` conserva la sesión del
  dispositivo, pero no la alarga para siempre. Pasadas `SESSION_ABSOLUTE_LIFETIME_HOURS`
  (12) desde que se abrió (`account_sessions.created_at`), se use o no, la renovación y
  cualquier otra petición responden 401 `unauthenticated` («La sesión venció») y se vuelve
  a entrar. Doce horas es el tope que NIST SP 800-63B fija para AAL2 (volver a autenticarse
  al menos cada 12 h, sin importar la actividad): alcanza para un turno doble de un
  restaurante y obliga a entrar de nuevo al día siguiente; un turno más largo entra una vez
  más a mitad de camino. Las sesiones vencidas tampoco figuran en `GET /auth/me/sessions`.

### Historial de accesos

- Cada intento de `POST /auth/login` (también el acceso obsoleto de plataforma)
  queda en `access_history` (`core/access_history.py`, RFS-15): cuándo, cómo
  terminó (`succeeded`, `failed`, `throttled`, `blocked` si el local está
  desactivado), el dispositivo (navegador y sistema, sin versiones: «Chrome en
  Android»), la IP sin su último bloque (`200.48.1.x`, `/48` en IPv6), y la ciudad
  y el país. La IP completa, el correo escrito y la contraseña no se guardan; un
  correo que no es de nadie deja una fila sin cuenta.
- Ubicación sin terceros (RNFS-15): con `GEOIP_DATABASE_PATH` apuntando a una base
  GeoLite2 City (`.mmdb`, se baja de MaxMind con su licencia gratuita y no va en el
  repositorio) se busca en local con `maxminddb`. Sin la base, los accesos quedan
  sin ciudad ni país y todo lo demás sigue igual.
- Se purga a los `ACCESS_HISTORY_RETENTION_DAYS` días (90): las filas vencidas se
  borran al anotar un intento nuevo (RNFP-12).
- `GET /auth/me/access-history` (cualquier sesión, del personal o de plataforma)
  y `GET /staff/{id}/access-history` (`staff.manage`, solo personal del local; 404
  si es de otro) devuelven `{items: [{id, occurred_at, outcome, device, network,
  city, country, new_device, new_country, outside_hours}], total}`, lo más nuevo
  primero.
- Fuera de horario (RFS-32): el encargado fija el horario de atención con
  `PATCH /restaurant` (`opening_hours: [{day, opens, closes}]`, día 0 lunes … 6
  domingo, horas del local; un cierre igual o anterior a la apertura cruza la
  medianoche; `[]` lo quita). Un intento de una cuenta del local que cae fuera de
  él se marca `outside_hours` (`core/opening_hours.py`). Sin horario no se marca.
- Aviso de dispositivo o país nuevo (RFS-16): un acceso correcto desde un
  dispositivo o un país que la cuenta no había usado se marca (`new_device`,
  `new_country`) y publica el aviso SSE `access_alert` a esa cuenta y a quien
  tenga `staff.manage` en el local. El primer acceso de una cuenta no avisa.

### Sesiones por dispositivo, contraseña confirmada, PIN y verificación en dos pasos

- Sesiones por dispositivo (RFS-17, `core/account_sessions.py`): cada token lleva
  firmado un `sid` que apunta a una fila de `account_sessions` con la cuenta, el
  dispositivo, la red (IP sin su último bloque), el país y el último uso. Entrar
  abre la sesión, `POST /auth/refresh` la conserva y `POST /auth/logout` cierra
  solo la de ese dispositivo. Una sesión sin usar más que la vida de un token
  deja de contar. Un token sin `sid` (emitido antes de este cambio) ya no vale: se
  vuelve a entrar una vez.
- Sesión nueva al entrar (ASVS V7.2.4): cada acceso correcto emite un `sid` nuevo y, si
  el navegador traía la cookie de una sesión vigente (de la misma cuenta o de otra), la
  cierra antes de abrir la nueva. Solo se mira una cookie firmada por este servidor.
  `POST /auth/me/reauthenticate` y `POST /auth/me/unlock` aceptan además
  `rotate_session: true`: la sesión cambia de `sid` (conserva el dispositivo, la
  apertura, la contraseña confirmada y el bloqueo), deja la cookie nueva y el token de
  la petición deja de valer. La confirmación responde entonces también `access_token`,
  `token_type`, `expires_in` y `csrf_token` (sin la opción van en `null`); el
  desbloqueo responde 200 con la forma de `POST /auth/refresh` en vez de 204. Es
  opcional para que la interfaz que todavía no la usa siga con su token.
- Tokens (ASVS V9.2.3, `core/tokens.py`): cada token lleva `iss` (`JWT_ISSUER`) y `aud`,
  `JWT_AUDIENCE` para el personal y la vista previa y `JWT_PLATFORM_AUDIENCE` para la
  administración del sistema. Al leerlo se exigen el emisor, la audiencia de su alcance,
  `exp`, `iat` y `sub`, y solo se acepta `JWT_ALGORITHM` (un `alg: none` no pasa). Los
  tokens emitidos antes de este cambio no traen `iss` ni `aud`: al desplegarlo todas las
  sesiones abiertas responden 401 y cada cuenta vuelve a entrar una vez. Cambiar
  cualquiera de esas variables tiene el mismo efecto.
- `GET /auth/me/sessions` (las dos clases de cuenta) lista las sesiones abiertas
  `[{id, device, network, city, country, created_at, last_seen_at, current}]`;
  `id` es un nombre público, no el `sid`. `DELETE /auth/me/sessions/{id}` cierra
  una (la propia equivale a salir) y `DELETE /auth/me/sessions`, todas las demás;
  cerrar la de otro dispositivo o todas las demás pide la contraseña confirmada
  (403 `reauthentication_required`, ASVS V7.5.2), cerrar la propia no.
  `GET /staff/{id}/sessions`, `DELETE /staff/{id}/sessions/{session}` y
  `DELETE /staff/{id}/sessions` hacen lo mismo con el personal (`staff.manage`;
  otro local, 404; una cuenta con permisos que uno no tiene, 403). Desactivar a
  alguien o restablecerle la contraseña cierra sus sesiones.
- Sesiones a la vez (ASVS V7.1.2): una cuenta tiene a lo sumo `MAX_SESSIONS_PER_ACCOUNT`
  (10) sesiones abiertas, contando cada navegador o dispositivo; abrir una más cierra la
  usada hace más tiempo. Diez cubren el celular, la tableta y la caja de un encargado que
  entra desde varios equipos del local, y acotan lo que queda abierto si alguien deja
  sesiones olvidadas.
- Cuándo se cierran (ASVS V7.4): salir (`POST /auth/logout`) cierra la de ese dispositivo,
  también la de una vista previa, que abre su propia fila (`account_kind` `preview`) y cuyo
  token deja de valer al salir. Cambiar la contraseña, activar o desactivar la verificación
  en dos pasos cierra las demás sesiones de la cuenta y deja la de esa petición. Desactivar
  la cuenta o el local, o restablecerle la contraseña, las cierra todas. Ante un incidente,
  `scripts/revoke_all_sessions.py --restaurant <slug>` cierra las de todo el personal de un
  local (y `--everyone`, las de todo el sistema); cada cuenta vuelve a entrar.
- Contraseña confirmada (RFS-21): `POST /auth/me/reauthenticate {password}` marca
  la sesión de ese dispositivo por `REAUTHENTICATION_WINDOW_SECONDS` (300) y
  responde `{reauthenticated_at, valid_until}`; los intentos fallidos cuentan como
  los del acceso (429) y cada uno deja `auth.reauth_failed` en el log
  (`auth.reauth_throttled` si ya lo frena el límite). Con la verificación en dos
  pasos activa (ASVS V7.5.1) la contraseña sola no alcanza: igual que el acceso, sin
  `otp_code` responde 401 `two_factor_required` y con uno equivocado, 401
  `invalid_two_factor_code` (cuenta como intento fallido); sirve el código de la
  aplicación o uno de recuperación. Ese 401 no cierra la sesión: su código no es
  `unauthenticated`. Sin esa marca, 403 `reauthentication_required` en: datos
  fiscales y credenciales SOL (`PUT /billing/settings`), certificado
  (`PUT /billing/settings/certificate`), crear, editar o borrar roles, cambiarle el
  rol a alguien (`PATCH /staff/{id}` con otro `role_id`), desactivar una cuenta
  (`PATCH /staff/{id}/status` con `is_active: false`), desactivar un local desde la
  plataforma, elegir o quitar el PIN y la verificación en dos pasos, y cerrar las
  sesiones propias de otros dispositivos.
- Revisión de accesos (RFS-24): `GET /staff/unused?days=60` (`staff.manage`)
  devuelve `[{member, last_activity_at, unused_days}]`: las cuentas activas del
  local sin uso en esos días, la más olvidada primero. Cuenta como uso el último
  de cualquier sesión y el último acceso correcto del historial; si nunca entró,
  desde el alta. Se desactivan con `PATCH /staff/{id}/status`, con su motivo.
- Bloqueo por inactividad (RFS-29): `restaurants.idle_lock_minutes` (10 por
  omisión, 0 lo apaga, `PATCH /restaurant`) fija los minutos sin uso. La interfaz
  bloquea con `POST /auth/me/lock` y `core/auth.py` bloquea sola la sesión que
  vuelve después de ese tiempo sin uso. Bloqueada, la API responde 423
  `session_locked` salvo `GET /auth/me`, `POST /auth/refresh`, `POST /auth/logout`,
  `POST /auth/me/lock` y `POST /auth/me/unlock {pin}` (o `{password}` sin PIN o si
  se olvidó). Un PIN equivocado responde 400 `wrong_pin` con los intentos que
  quedan; el tercero cierra la sesión (401 `unlock_attempts_exhausted`) y queda en
  la bitácora. Cada intento fallido (PIN o contraseña) deja `auth.unlock_failed` en
  el log con los que quedan. `PUT /auth/me/pin {pin}` (4 a 6 dígitos) y `DELETE /auth/me/pin`
  eligen o quitan el PIN. La sesión trae `idle_lock_minutes`, `has_pin` y
  `session_locked`.
- Verificación en dos pasos (RFS-22, `core/two_factor.py`): TOTP de 6 dígitos y
  30 s (RFC 6238), para las dos clases de cuenta. `POST /auth/me/two-factor/setup`
  devuelve `{secret, otpauth_uri}` (el QR) y deja el alta pendiente;
  `POST /auth/me/two-factor/confirm {code}` la activa y responde diez códigos de
  recuperación, que no se vuelven a mostrar. `GET /auth/me/two-factor` dice
  `{enabled, pending, recovery_codes_left}`; `POST /auth/me/two-factor/recovery-codes`
  los renueva y `DELETE /auth/me/two-factor` la apaga. El secreto se guarda cifrado
  (AES-256-GCM, la llave de datos personales) y los códigos de recuperación como
  hash. Con la verificación activa, `POST /auth/login` (y el obsoleto
  `/platform/auth/login`) responde 401 `two_factor_required` hasta recibir
  `otp_code`, el código de la aplicación o uno de recuperación; uno equivocado es
  401 `invalid_two_factor_code` y cuenta como intento fallido. Un código no sirve
  dos veces.

### Bitácoras de solo inserción

`activity_log` (la bitácora de cada local), `platform_activity` (la de la
plataforma) y `ai_decisions` no se editan ni se borran, y no solo por
convención: la migración `0034` les pone un trigger que rechaza todo `UPDATE` y
`DELETE` (en PostgreSQL, `forbid_log_changes()`, con `insufficient_privilege`; en
SQLite, `RAISE(ABORT)`) y les quita esos permisos a `PUBLIC` (RNFS-16). El
trigger vale también para el dueño de las tablas, que en Railway es el mismo
usuario con el que corre la aplicación y al que un `REVOKE` no le alcanzaría.
La migración `0044` agrega en PostgreSQL un trigger de sentencia `BEFORE TRUNCATE`
con la misma función: un `TRUNCATE` no dispara los triggers de fila y vaciaba la
tabla de un golpe (en SQLite no existe). Para corregir un asiento a mano hay que
borrar el trigger: queda a la vista.

### Cabeceras de seguridad y límite de peticiones

- Toda respuesta lleva `Strict-Transport-Security`, `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Permissions-Policy` y una CSP que no deja ejecutar ni enmarcar nada
  (`core/security_headers.py`). `/docs` y `/redoc` van sin esa CSP: cargan
  Swagger UI y ReDoc de un CDN.
- Caché: toda respuesta lleva `Cache-Control: no-store` salvo que la ruta fije
  el suyo. La foto de un plato (`GET /menu/items/{id}/photo?v=<photo_version>`)
  se guarda a propósito con `private, max-age=86400` (cambia de URL al cambiar
  la foto) y los archivos (PDF, XML) van con `private, no-store`.
- Cada IP tiene `RATE_LIMIT_PER_MINUTE` peticiones por minuto (1200 por
  omisión; 0 lo apaga); pasado eso, 429 con `Retry-After`
  (`core/rate_limit.py`). Alto a propósito: los celulares de un local comparten
  la IP de su conexión. El sondeo de vida y el canal de avisos no cuentan.
- Tamaño del cuerpo: una petición cuyo cuerpo pasa de `MAX_REQUEST_BODY_BYTES`
  (1 MiB por omisión) recibe 413 `request_too_large`, con o sin sesión
  (`core/body_limit.py`). Se mira `Content-Length` antes de leer y, si el
  cuerpo llega por partes, se cuentan los bytes al leerlo. Lo más grande
  legítimo es la foto de un plato (400 KB, unos 550 KB en base64).
- WAF: lo que va en el código son estas cabeceras y los dos límites. Filtrar
  ataques conocidos (reglas OWASP), bots y picos de tráfico va en un proxy por
  delante de Railway, por ejemplo Cloudflare con el dominio del API en modo
  proxy, sus reglas administradas y un límite por IP. Detrás de Cloudflare la
  última IP de `X-Forwarded-For` es la de Cloudflare: hay que poner
  `CLIENT_IP_HEADER=cf-connecting-ip` para que los límites vean la IP real.
  Esa cabecera solo es de fiar si el API no se alcanza por fuera del WAF (sin
  el dominio `*.up.railway.app` público).
- IP del cliente y proxy: la IP de los límites, del historial de accesos y de
  las sesiones la calcula la aplicación (`client_address` en `core/auth.py`):
  la **última** IP de `X-Forwarded-For`, la que agrega el proxy, o la de
  `CLIENT_IP_HEADER` detrás de un WAF. Es de fiar solo mientras todo el tráfico
  pase por el proxy: el API no debe alcanzarse por otra vía (otro puerto
  público, un dominio sin proxy). Eso no depende de uvicorn: la imagen lo
  arranca con `--forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"`, así
  que por omisión uvicorn no cree las `X-Forwarded-*` de nadie fuera del propio
  contenedor (en Railway anota la IP del proxy y `http` en sus logs, y nada
  más cambia). Si algún día hace falta que uvicorn las crea, pon en
  `FORWARDED_ALLOW_IPS` el rango del proxy (acepta redes, `100.64.0.0/10`),
  **nunca `*`**: con `*` la IP que uvicorn anota es la primera de
  `X-Forwarded-For`, que escribe el cliente. Con `DEBUG=false` y `*`, la
  aplicación lo avisa al arrancar (`app.forwarded_allow_ips_wildcard`).
- Intentos de acceso (`core/login_throttle.py`): 5 fallos por correo e IP en 15
  minutos, y un tope de 25 por correo sin importar la IP, para que rotar de
  dirección no sirva. Los contadores viven en la base (`login_attempts`), así que
  valen igual con varias réplicas; la cuenta en memoria de cada proceso es solo la
  primera barrera.
- Sesiones: cambiar o restablecer una contraseña, o desactivar la cuenta,
  invalida los tokens emitidos antes (`sessions_valid_since`, en `users` y en
  `platform_admins`, contra el `iat` del token). Reactivar la cuenta no los
  devuelve: hay que iniciar sesión de nuevo. Vale también para las vistas
  previas abiertas por una cuenta de plataforma. Las cuentas de plataforma se
  activan y desactivan con `scripts/set_platform_admin_status.py`. El token de
  acceso dura `ACCESS_TOKEN_TTL_SECONDS`.
- Sesión en cookie (S2 / RNFS-31, `core/session_cookie.py`): el login, la
  renovación y el cambio de contraseña dejan el token en una cookie `HttpOnly;
  Secure; SameSite=None` (`resthub_session` para el personal,
  `resthub_platform_session` para la plataforma; entrar con una borra la otra) y
  devuelven `csrf_token`; `GET /auth/me` también lo devuelve, leído de la cookie,
  para la pestaña que lo perdió (un GET no lo pide). Con la cookie, todo POST, PUT, PATCH y DELETE exige la
  cabecera `X-CSRF-Token` con ese valor, que también va en la cookie
  `resthub_csrf` (doble envío): sin ella o distinta, 403 `csrf_token_invalid`
  y un `auth.csrf_rejected` en el log (`reason`: `missing` o `mismatch`; nunca el valor).
  `Authorization: Bearer` sigue valiendo, tiene prioridad y no pide CSRF (el
  navegador no lo manda solo); la vista previa usa solo Bearer, para no pisar la
  sesión de la plataforma en el mismo navegador. `POST /auth/logout` borra las
  cookies y responde `Clear-Site-Data: "cache"` (también al cerrar la sesión
  del propio dispositivo): el navegador olvida lo que guardó en caché del API.
  Solo la caché, no `"storage"`: la cola de pedidos sin conexión del frontend
  tiene que sobrevivir al cierre de sesión. CORS ya va con `allow_credentials` y orígenes explícitos, y acepta
  `X-CSRF-Token`; el frontend pide con `credentials: "include"` y deja de guardar
  el token en `localStorage`.
- Documentación interactiva: `/api/v1/docs`, `/redoc` y `openapi.json` solo se
  publican con `DEBUG=true` (apagado por omisión). Para regenerar los tipos del frontend
  (`pnpm generate:api`) usa `DEBUG=true uv run python scripts/export_openapi.py` o un
  entorno local.

### Administración del sistema

El equipo de RestHub da de alta restaurantes y a su primer encargado, los
activa o desactiva y agrega encargados. Lo hace el módulo `platform`.

Principio de seguridad: no hay forma de mezclar una sesión de plataforma con
una de restaurante.

- Las cuentas de plataforma viven en su propia tabla (`platform_admins`), no en
  `users`: no tienen `restaurant_id` ni rol, así que ninguna consulta del
  personal ni ningún permiso de un local las alcanza.
- Sus tokens llevan `scope: "platform"` y `sub` = id de la cuenta de
  plataforma; los de restaurante llevan `scope: "restaurant"` (los emitidos
  antes no traen `scope` y siguen sirviendo). Como los dos tipos de cuenta
  pueden tener el mismo id, el alcance firmado es la frontera:
  `JwtTokenService.decode` rechaza uno de plataforma y `decode_platform` uno de
  restaurante. Un token de plataforma contra un endpoint de restaurante
  responde 401, y uno de restaurante contra `/platform/*`, también.
- **Acceso único**: `POST /api/v1/auth/login` atiende a las dos clases de
  cuenta. Prueba primero al personal de los restaurantes y, si no hay una
  credencial válida del personal, a la plataforma. Es el mismo 401 genérico y
  el mismo contador de intentos (el del personal, `core/login_throttle.py`) para
  ambas, así que no revela de qué clase es un correo. Una cuenta de plataforma
  desactivada responde el mismo 401 que una contraseña equivocada. La cuenta se
  relee de la base en cada petición. `accounts` no importa `platform`: pide la
  verificación por el puerto `PlatformSignInGateway`, que implementa
  `wiring/platform_sign_in.py`.
- **Un correo, una sola tabla**: un correo no puede existir a la vez en `users`
  y en `platform_admins` (el acceso entra por el correo). Lo rechazan, con el
  mismo error de correo ya registrado, el alta de personal, el alta de un
  encargado (también con el alta de un restaurante) y
  `scripts/create_platform_admin.py`.
- **Permisos de plataforma** (`PlatformPermission` en `core/permissions.py`, un
  enum aparte de `Permission`: el encargado de un restaurante nunca los recibe
  y el editor de roles no los ofrece). Cada endpoint de `/platform` exige el suyo
  con `require_platform_permission(...)` y responde 403 (`missing_permission`) sin
  él. Hoy toda cuenta de plataforma los tiene todos, calculados al leer
  (`platform_permissions()`, sin columna ni migración): ahí se engancharán los
  roles de plataforma.

  | Permiso                        | Endpoints                                                              |
  | ------------------------------ | ---------------------------------------------------------------------- |
  | `platform.restaurants.read`    | `GET /platform/restaurants`, `GET /platform/restaurants/{id}`          |
  | `platform.restaurants.manage`  | `POST /platform/restaurants`, `PATCH /platform/restaurants/{id}`, `POST /platform/restaurants/{id}/owners`, `POST /platform/restaurants/{id}/owners/{owner_id}/password` |
  | `platform.activity.read`       | `GET /platform/activity`                                               |
  | `platform.preview.open`        | `GET /platform/sandbox`, `POST /platform/sandbox/reset`, `POST /platform/preview` |
  | `platform.observability.read`  | `GET /platform/observability/*`                                        |

- **Sesión discriminada por `kind`**: `POST /auth/login`, `POST /auth/refresh` y
  `GET /auth/me` (estos dos aceptan cualquiera de los dos tokens) responden
  `kind: "staff"` (`StaffSessionResponse`: `user`, `restaurant`, `permissions`,
  `preview`, `terms_version`, `terms_accepted`, `privacy_version`, `privacy_accepted`,
  `must_change_password`) o `kind: "platform"`
  (`PlatformAdminSessionResponse`: `admin: {id, email, full_name}`,
  `permissions` de plataforma, `preview: false`). El login y el refresh le suman
  `access_token`, `token_type` y `expires_in`
  (`StaffAccessTokenResponse` / `PlatformAdminAccessTokenResponse`). Lo propio del
  personal (`/auth/me/terms`, `/auth/me/password`, `/auth/preview`) sigue solo
  para personal: un token de plataforma responde 401.
- Las rutas `/platform/auth/login|refresh|me` quedan **obsoletas**
  (`deprecated` en OpenAPI) pero funcionan, con su propio contador de intentos;
  se borrarán cuando ninguna interfaz las use.
- No hay registro público: las cuentas se crean con
  `scripts/create_platform_admin.py`.
- Es la única parte de la API donde el restaurante viene en la URL
  (`/platform/restaurants/{id}`): la plataforma no pertenece a ninguno y los
  administra a todos.

Cómo cruza módulos: `platform` no importa `restaurants` ni `accounts`.

- Lee restaurantes, personal y encargados por SQL desde
  `adapters/persistence/directories.py` (`RestaurantCatalogDirectory`). Encargado es
  toda cuenta cuyo rol es el `owner` de su local.
- Escribe por el puerto `RestaurantProvisioningGateway`, que implementa
  `wiring/restaurant_provisioning.py` con los casos de uso de esos módulos
  (`CreateRestaurant`, `RegisterFirstAdmin`, `RegisterOwner`) y el repositorio
  de restaurantes; `main.py` lo instala. El alta de un restaurante, sus roles
  Encargado y Mesero y su primer encargado corre en la sesión de la petición:
  una sola transacción, como en `scripts/create_restaurant.py`.
- Desactivar un restaurante corta en la petición siguiente el acceso de todo su
  personal (`core/auth.py` trata como inactiva a la cuenta de un local
  inactivo); reactivarlo lo devuelve.
- La bitácora de plataforma (`platform_activity`) es aparte de la de cada
  local: registra accesos, altas y ediciones de restaurantes (activar y
  desactivar incluidos) y encargados agregados. No se edita ni se borra.

| Método | Ruta (bajo `/api/v1/platform`)                        | Respuesta                                       |
| ------ | ----------------------------------------------------- | ----------------------------------------------- |
| POST   | `/auth/login` `{email, password}` (obsoleto: usa `POST /api/v1/auth/login`) | `{access_token, token_type, expires_in, admin}`; 401 genérico, 429 con `Retry-After` |
| GET    | `/auth/me` (obsoleto: usa `GET /api/v1/auth/me`)      | `{admin: {id, full_name, email}}`               |
| POST   | `/auth/refresh` (obsoleto: usa `POST /api/v1/auth/refresh`) | Igual que el acceso, con un token nuevo   |
| GET    | `/restaurants?search=&limit=25&offset=0`              | `{items: [RestaurantSummary], total}`, lo más nuevo primero; `search` por nombre o identificador, sin mayúsculas |
| POST   | `/restaurants` `{name, slug, timezone, owner: {full_name, email, password}}` | 201 `RestaurantDetail`; 409 identificador o correo usado; 422 zona, identificador (o uno que empiece con `muestra`, reservado) o contraseña inválidos |
| GET    | `/restaurants/{id}`                                   | `RestaurantDetail` (con `owners`); 404          |
| PATCH  | `/restaurants/{id}` `{name?, timezone?, is_active?}`  | `RestaurantDetail`; 404, 422                    |
| POST   | `/restaurants/{id}/owners` `{full_name, email, password}` | 201 `{id, full_name, email, is_active}`; 404, 409 |
| POST   | `/restaurants/{id}/owners/{owner_id}/password` `{new_password}` | `{id, full_name, email, is_active}`: contraseña temporal para un encargado que perdió la suya (RFS-04); corta sus sesiones y queda en la bitácora (`owner_password_reset`). 404 `owner_not_found` si la cuenta no es encargado de ese local (o es del local de muestra), 422 si la contraseña no sirve |
| GET    | `/activity?limit=25&offset=0`                         | `{items: [{id, admin_id, admin_name, kind, kind_label, detail, created_at}], total}` |

`RestaurantSummary` es `{id, name, slug, timezone, is_active, created_at,
staff_count, active_staff_count}` y `RestaurantDetail` le suma
`owners: [{id, full_name, email, is_active, must_change_password}]`. Todo exige un token de
plataforma y el permiso de la tabla de arriba, salvo el acceso.

### Vista previa (local de muestra)

La administración del sistema puede abrir la aplicación **tal como la ve un
encargado o un mesero**, para depurar, sin tocar datos de ningún restaurante
real. Para eso existe un **local de muestra** con los datos de muestra (la
misma carta, mesas, insumos, recetas y roles Encargado, Mesero y Cocinero que
siembra `scripts/seed_dev.py`), que se usa de verdad (tomar pedidos, cobrar) y
se reinicia cuando hace falta.

Principios de seguridad:

- La vista previa **solo** entra al local de muestra. `POST /platform/preview`
  no recibe un restaurante ni una cuenta, solo `as: "owner" | "waiter"`, y el
  caso de uso elige la cuenta de muestra activa de ese tipo. No hay forma, ni
  por API, de previsualizar un restaurante real ni de obtener un token de una
  cuenta real desde la plataforma.
- Se comprueba en tres lugares: al emitir el código (`IssuePreviewCode` exige
  que la cuenta sea de un local con `is_sandbox`), al canjearlo
  (`ExchangePreviewCode` lo relee) y en cada petición: `core/auth.py` rechaza
  con 401 un token con `preview: true` cuya cuenta no es de un local de
  muestra, aunque la firma sea válida.
- El local de muestra es un restaurante normal para el resto del sistema (mismo
  aislamiento por `restaurant_id`), marcado con `restaurants.is_sandbox`. Hay a
  lo sumo uno vigente (activo); lo garantiza un índice único parcial
  (`uq_restaurants_one_active_sandbox`, `WHERE is_sandbox AND is_active`), y si
  dos pedidos lo crean a la vez el segundo responde 409. Su identificador corto
  es `muestra-<8 hex al azar>` y al archivarlo pasa a
  `archivado-<id>-<8 hex al azar>`: nadie puede ocuparlos antes. Además un
  restaurante real no puede tomar un identificador que empiece con `muestra`
  (`POST /platform/restaurants` responde 422). La lista, la búsqueda, el total y la ficha de
  `/platform/restaurants` no ven locales de muestra (ni vigentes ni archivados:
  su ficha y su edición responden 404). Los endpoints del propio restaurante
  (`/restaurant`, `/menu`, `/orders`…) no cambian.
- Sus cuentas («Encargado de muestra», «Mesero de muestra», «Cocinero de
  muestra») tienen correos `<rol>-<id del local>@muestra.resthub.invalid`, un
  dominio reservado que no existe, y el hash de una contraseña aleatoria que se
  descarta al crearlas. Además `POST /auth/login` responde el 401 genérico a
  cualquier cuenta de un local de muestra, aun con la contraseña correcta (por
  ejemplo, una que alguien creó desde la vista previa): ahí solo se entra por la
  vista previa.
- Una cuenta que se da de alta en el local de muestra (`POST /staff` desde la
  vista previa, o un encargado nuevo) no toma el correo que se escribió: como
  el correo es único en todo el sistema y las cuentas no se borran, lo ocuparía
  para siempre. Se conserva lo que va antes de la arroba, se le agrega el id
  del local y pasa al dominio de muestra (`ana@gmail.com` en el local 7 queda
  `ana-7@muestra.resthub.invalid`); la respuesta trae el correo con el que
  quedó. `accounts` sabe si el local es de muestra por su propio lector de
  `restaurants` (`SqlAlchemyRestaurantDirectory`).
- El código es de un solo uso: 32 bytes aleatorios (`secrets.token_urlsafe`),
  vence a los 60 s y se guarda como SHA-256 en `preview_codes`, nunca en claro.
  El canje lo marca usado con una sola sentencia
  (`UPDATE … WHERE used_at IS NULL AND expires_at > ahora`): con dos canjes a la
  vez, uno solo funciona. Inválido, vencido o ya usado responden el mismo 401.
  `POST /platform/preview`, `POST /platform/sandbox/reset` y `POST /auth/preview`
  confirman la transacción antes de responder (la sesión de la petición
  confirma recién cuando la respuesta ya salió): el código se canjea apenas
  llega y queda usado antes de que salga el token. Cada código nuevo borra de
  paso los creados hace más de un día (vencidos, usados o no).
- El token de vista previa es un token de restaurante (`scope: "restaurant"`)
  con dos claims más, `preview: true` y `platform_admin_id`, y **vida corta**:
  30 minutos, sin renovación (`POST /auth/refresh` responde 401). Los permisos
  son los del rol de la cuenta de muestra, releídos de la base como siempre.
  `GET /auth/me` y las respuestas con forma de acceso traen `preview` (`false`
  en las sesiones normales). Cambiar la contraseña con un token de vista previa
  responde **403** (la credencial es válida; la acción no cabe).
- Desactivar una cuenta de plataforma corta sus vistas previas: el canje
  responde el 401 genérico si quien pidió el código ya no está activo, y
  `core/auth.py` relee `platform_admins.is_active` del `platform_admin_id` del
  token en cada petición (401 si no), igual que relee la cuenta y el local.
- Todo queda en la bitácora de plataforma: `sandbox_reset` y `preview_started`
  (con la cuenta y el local). El canje deja además un `signed_in` con detalle
  «Vista previa» en la bitácora del local de muestra.

| Método | Ruta (bajo `/api/v1`)                         | Respuesta                                       |
| ------ | --------------------------------------------- | ----------------------------------------------- |
| GET    | `/platform/sandbox`                           | `{restaurant: RestaurantSummary \| null, accounts: [{kind, role_label, full_name}]}`; las cuentas activas, encargado primero |
| POST   | `/platform/sandbox/reset`                     | Igual que el GET. Si hay uno vigente lo archiva (`is_active=false`, slug `archivado-<id>-<hex>`) y crea otro (slug `muestra-<hex>`) con los datos de muestra, en una transacción |
| POST   | `/platform/preview` `{as: "owner" \| "waiter"}` | 201 `{code, expires_in}` (60 s). Crea el local de muestra si falta; 409 si no tiene una cuenta activa de ese tipo (reinícialo); 422 otro `as` |
| POST   | `/auth/preview` `{code}` (sin autenticación)  | 200 igual que `POST /auth/login` más `preview: true`; 401 genérico |

Los tres de `/platform` exigen un token de plataforma: uno de restaurante, o de
vista previa, responde 401.

Cómo cruza módulos: los códigos son credenciales de cuentas del personal, así
que la tabla `preview_codes` y su canje (`POST /auth/preview`) son de
`accounts`; el canje, que no lleva autenticación previa, no cruza ningún módulo.
`platform` no importa `accounts` ni `restaurants`: lee el local de muestra y sus
cuentas por SQL (`SqlAlchemySandboxCatalogDirectory`) y pide crearlo, archivarlo y emitir
códigos por el puerto `SandboxProvisioningGateway`, que implementa `wiring/sandbox.py`
con `CreateRestaurant`, `Restaurant.archive`, los datos de
`wiring/sample_restaurant.py` e `IssuePreviewCode`; `main.py` lo instala.

### Panel de observabilidad

**Alertas de seguridad (RFS-31).** Cada `SECURITY_ALERT_INTERVAL_SECONDS` (60) el
API cuenta, en los últimos `SECURITY_ALERT_WINDOW_MINUTES` (15) y en todo el
sistema, los accesos fallidos, los frenados por el límite de intentos y las
respuestas 403. Si alguno pasa su umbral (`SECURITY_ALERT_FAILED_LOGINS` 30,
`SECURITY_ALERT_LOCKOUTS` 5, `SECURITY_ALERT_FORBIDDEN` 50) deja una alerta en
`platform_security_alerts` y un `security.spike` en los logs; la misma señal no
vuelve a alertar en `SECURITY_ALERT_COOLDOWN_MINUTES` (60). El panel las lee de
`GET /platform/observability/security-alerts`. Los 403 salen de la telemetría:
con `OBSERVABILITY_ENABLED=false` esa señal queda en cero.

La administración del sistema ve cómo anda la aplicación, al estilo de un
Grafana chico: tráfico, errores, latencias, rutas más lentas y un buscador de
logs con el detalle de cada error. Los datos los guarda el propio backend en su
base, con rotación; no hay servicios externos.

Qué se captura:

- **Cada petición HTTP**, salvo el sondeo de vida (`/health`), la conexión de
  avisos (`/events`) y el propio panel (`/platform/observability/*`): momento,
  método (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`; cualquier
  otro se guarda como `OTHER`), **plantilla de la ruta**
  (`/api/v1/orders/{order_id}`, nunca la ruta con ids; `<sin ruta>` si no llegó
  a ninguna), estado, duración, tiempo y
  cantidad de consultas a la base (`core/db_timing.py`), `request_id`, tipo de
  cuenta (`staff`, `platform`, `preview` o `anonymous`), `restaurant_id` y
  `account_id` si los hay. El tipo de cuenta lo anotan las dependencias de
  acceso (`core/auth.py` y `get_current_admin` de `platform`) en
  `core/request_context.py`; el middleware lo lee al terminar la respuesta.
  De las peticiones `<sin ruta>` (direcciones inventadas, consultas previas de
  CORS: tráfico que cualquiera genera sin límite) se guarda **una de cada
  diez**, así que sus cuentas en el panel son una muestra; las descartadas por
  la muestra no suman a `dropped_events`.
- **Cada evento de log de nivel `warning` o superior** que pase por structlog,
  también los de librerías (`critical` se guarda como `error`): momento, nivel,
  logger, evento, `request_id`, `restaurant_id` (el del evento o el de la
  cuenta de la petición) y los demás campos como JSON. `method`, `path` (la
  ruta cruda, con ids), `request_id` y `restaurant_id` no se repiten en los
  campos: ya están en su columna o en la fila de la petición. Los 5xx dejan su
  `request.completed` como `error`; los 4xx no (en la consola sí salen como
  aviso): su petición ya se guarda con el estado. Los eventos de las rutas que
  no se capturan tampoco se guardan. Los campos serializados no pasan de 16 KB:
  más grandes, se recortan listas, claves y textos (primero se conservan
  `error_type` y `error_message`) y el JSON lleva `"…": "recortado"`.
- **Cada excepción no controlada** (respuesta 500) queda como evento `error`
  (`request.failed`) con `error_type`, `error_message` y el traceback (hasta
  20 000 caracteres; si es más largo se conserva el final, donde están el tipo
  y el mensaje), con el `request_id` de su petición. La copia de la misma
  excepción que loguea uvicorn después ("Exception in ASGI application") no se
  guarda otra vez.

Qué **no** se guarda nunca: cuerpos de petición ni de respuesta, query strings,
cabeceras (tampoco `Authorization`), contraseñas, tokens ni códigos de vista
previa. Antes de encolarse (`core/redaction.py`):

- Todo campo de log cuyo nombre contenga, como palabra, `password`, `passcode`,
  `token`, `jwt`, `bearer`, `auth`, `authorization`, `credential`, `code`,
  `hash`, `secret`, `key`, `signature`, `cookie`, `otp`, `pin` o `dsn` (sin
  importar mayúsculas, `camelCase` o `snake_case`, y a cualquier profundidad de
  un diccionario anidado), o que se llame `database_url`, `session_id` o
  `set-cookie`, se reemplaza por `"[oculto]"`. Las palabras largas (`token`,
  `password`, `secret`…) se buscan también dentro del nombre sin separadores:
  `ACCESSTOKEN` o `PASSWORDHASH` tampoco pasan. `invoice_code` y `status_code`
  se dejan ver; por eso facturación loguea el comprobante como `invoice_code`.
- Todo texto (los campos, el evento, el mensaje y el traceback de una
  excepción) se limpia de lo que parece una credencial: JWT (`eyJ….….…`),
  `Bearer …` y `Basic …`, `token="…"`, usuario y contraseña en una URL
  (`postgresql://usuario:clave@…`), los parámetros que SQLAlchemy copia en sus
  errores (`[parameters: …]`), los valores literales dentro de `[SQL: …]` y los
  de la fila que PostgreSQL pone en su detalle (`Key (email)=(…)`,
  `Failing row contains (…)`).
- Los datos personales: un campo `phone` (o `customer_phone`…), `dni`, `ruc` o
  `document_number` se oculta entero; en todo texto un correo queda `j***@dominio.pe`
  y una tira de 8 a 19 dígitos (con `+` o espacios sueltos: un DNI, un RUC, un
  celular, una tarjeta) queda `"[oculto]"`. Fechas, IP, importes, series
  (`F001-00000123`) e identificadores enteros no se tocan.
- La misma regla corre en cada línea de los logs de la consola (`core/logs.py`):
  claves, evento, valores y el mensaje y traceback de una excepción, que en la
  consola sale sin las variables locales de cada marco.
- Cada línea de la consola lleva, además del `request_id`, la red de origen
  (`client_network`: la IP que usan los límites, sin su último bloque, como en el
  historial de accesos) y, en cuanto la petición se autentica, `account_kind`,
  `account_id` y `restaurant_id`. En el panel, `client_network` no se guarda.
- Los motores de la base (`core/database.py` y `alembic/env.py`) se crean con
  `hide_parameters=True`: un error de la base no copia los valores de la
  consulta en su mensaje. Lo anterior es la segunda barrera.

Aun así, no pongas datos personales en los mensajes de error.

Cómo se escribe sin frenar a nadie (`core/telemetry.py`):

- La captura solo encola en memoria. Un bucle de fondo, que arranca y se
  detiene con la aplicación, escribe en lotes cada ~2 s o en cuanto hay 200
  filas, con un `INSERT` de muchas filas por tabla.
- Cada valor se recorta al largo de su columna antes de encolarse (y los
  enteros fuera de 32 bits quedan vacíos): una fila que no entra no puede
  hacer que PostgreSQL rechace el lote de las demás.
- La cola tiene tope (10 000 filas). Llena, se descartan las más viejas y se
  cuentan. Además, cada proceso encola a lo sumo
  `OBSERVABILITY_MAX_ROWS_PER_MINUTE` filas por minuto (6 000 por omisión,
  peticiones más eventos, con ráfagas de hasta un minuto de filas): lo que pasa
  se descarta y se cuenta, para que una avalancha no llene la base.
- Si escribir un lote falla, se reintenta una vez; si vuelve a fallar, se
  escribe fila por fila y se descartan (y cuentan) solo las que la base
  rechace. Tras cinco filas seguidas rechazadas se da la base por caída y se
  descarta el resto del lote. El total sale en `dropped_events` del resumen y
  es del proceso que responde: con varios procesos, cada uno escribe y cuenta
  lo suyo.
- Lo que loguea el propio bucle no vuelve a la cola.
- Una purga en segundo plano, cada hora, borra lo que pasó la retención, de a
  10 000 filas por sentencia y cada tanda en su transacción. En PostgreSQL
  cada tanda toma un candado consultivo (`pg_try_advisory_xact_lock`): si otro
  proceso está purgando, este lo deja para la próxima vuelta.
- Al apagar, el lote que se estaba escribiendo y lo que quedó en la cola
  tienen 5 s para escribirse; lo que no llega se cuenta como descartado.

Dónde vive: la captura es del núcleo (middleware de registro, un procesador de
structlog y el puerto `TelemetrySink`), porque atraviesa toda la aplicación.
Las tablas (`obs_requests`, `obs_events`), el sumidero que escribe en ellas
(`SqlAlchemyTelemetrySink`) y la API de consulta son de `platform`, que es quien las
lee; `main.py` instala el sumidero al arrancar. Las tablas no tienen claves
foráneas: son telemetría y sobreviven a la baja de un restaurante o una cuenta.

Todo exige un token de plataforma (uno de restaurante o de vista previa
responde 401). Parámetro común `window`: `1h`, `6h`, `24h` (por omisión) o
`7d`, siempre terminando ahora; cualquier otro valor responde 422. Filtro
opcional `restaurant_id` en todos.

| Método | Ruta (bajo `/api/v1/platform/observability`)  | Respuesta                                       |
| ------ | --------------------------------------------- | ----------------------------------------------- |
| GET    | `/summary?window=`                            | `{window, requests, errors_5xx, errors_4xx, error_rate, p50_ms, p95_ms, p99_ms, avg_db_ms, active_restaurants, dropped_events, sampled}` |
| GET    | `/timeseries?window=`                         | `{bucket_seconds, points: [{t, requests, errors_5xx, p95_ms}], sampled}`; cubos de 1 min (1h), 5 min (6h), 15 min (24h) o 2 h (7d), alineados en UTC, los vacíos en cero |
| GET    | `/routes?window=&sort=requests\|p95\|errors&limit=20` | `[{method, route, requests, errors_5xx, p50_ms, p95_ms, avg_db_ms, sampled}]`; `limit` hasta 100 |
| GET    | `/status?window=`                             | `[{status, count}]` ordenado por estado         |
| GET    | `/logs?window=&level=warning\|error&search=&request_id=&limit=50&before_id=` | `{items: [{id, at, level, logger, event, request_id, restaurant_id, has_traceback}], next_before_id}`; lo más nuevo primero |
| GET    | `/logs/{id}`                                  | La entrada con `fields` y `traceback`; 404      |
| GET    | `/requests?window=&status_min=&route=&request_id=&limit=50&before_id=` | `{items: [{id, at, method, route, status, duration_ms, db_ms, db_queries, request_id, account_kind, restaurant_id, account_id}], next_before_id}` |

- `error_rate` es la fracción (de 0 a 1) de peticiones con 5xx; los 4xx se
  cuentan aparte. En una ventana sin datos todo es cero.
- Los percentiles se interpolan de forma lineal. En PostgreSQL los calcula la
  base con `percentile_cont`, sin traer las duraciones; SQLite, la base de las
  pruebas, no lo tiene, y ahí se calculan en Python con la misma fórmula. Las
  cuentas y promedios, siempre en la base. Con más de 200 000 peticiones en la
  ventana, las duraciones se muestrean de forma uniforme (una de cada `k` por
  identificador, igual en las dos bases) y la respuesta trae `sampled: true`;
  las cuentas siguen siendo exactas. En `/routes`, las rutas que quedan fuera
  de la muestra se traen enteras, todas en una sola consulta.
- `/logs` y `/requests` se paginan por identificador: `next_before_id` va como
  `before_id` de la página siguiente (`null` en la última). `limit` hasta 200.
  `search` (hasta 120 caracteres) busca sin mayúsculas en el evento y en el
  JSON de los campos; `%` y `_` se buscan tal cual. No usa índice: recorre los
  eventos de la ventana (que sí lo usa), con campos de a lo sumo 16 KB. `level=error` incluye lo
  que se logueó como `critical`. `route` es la plantilla exacta y `status_min`
  el estado mínimo (`500` para ver solo los errores).
- Para ir de un error a su petición: `/requests?request_id=…`; de una petición
  a sus eventos: `/logs?request_id=…`.

### Trazas (OpenTelemetry)

Apagadas por omisión: sin `OTEL_EXPORTER_OTLP_ENDPOINT` no se importa ni se instala nada de
OpenTelemetry. Con un colector OTLP/HTTP se instrumentan FastAPI (un span por petición, con la
plantilla de la ruta), SQLAlchemy asíncrono (un span por consulta) y httpx (las llamadas a la
IA y a Lycet; la URL de una llamada saliente se anota sin su consulta, que puede llevar un
token). El sondeo de vida y el canal de avisos no se trazan. Cada
log lleva `trace_id` y `span_id` mientras haya una traza activa, para saltar del log a la traza.

En Railway: agrega un colector OTLP (o usa el de tu proveedor: Grafana Cloud, Honeycomb,
Datadog…) y define en el servicio `OTEL_EXPORTER_OTLP_ENDPOINT=https://<colector>` (la ruta
`/v1/traces` se agrega sola), `OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer <clave>` si el
colector la pide y, para bajar el volumen, `OTEL_TRACES_SAMPLER_ARG=0.1`. Es independiente del
panel de observabilidad de la plataforma, que sigue guardando su telemetría en la base.

### Respaldos

- `scripts/backup_db.py` respalda con `pg_dump` (PostgreSQL, se restaura con
  `pg_restore`) o con la API de respaldo de sqlite3, y deja los últimos N.
- `.github/workflows/backup.yml` lo corre a diario a las 03:00 de Lima, cifra
  el volcado con [age](https://age-encryption.org) y guarda solo el `.dump.age`
  como artefacto por 30 días (fuera de Railway; GitHub no ve los datos). Necesita
  dos secretos: `BACKUP_DATABASE_URL` (la `DATABASE_PUBLIC_URL` de Railway) y
  `BACKUP_AGE_RECIPIENT`, la llave **pública** de age. Sin la segunda el workflow
  falla en vez de subir un volcado en claro (RNFS-09).
- La llave privada se genera una vez y se guarda en el gestor de secretos del
  equipo, nunca en GitHub ni en Railway (con ella se lee cualquier respaldo):
  ```bash
  age-keygen -o resthub-backup.key   # imprime la llave pública: va en BACKUP_AGE_RECIPIENT
  ```
- Restaurar y medir (RNFS-12, meta de 4 h o menos de punta a punta): baja el
  artefacto desde Actions y
  ```bash
  bash scripts/restore_backup.sh resthub-AAAAMMDD-HHMMSS.dump.age resthub-backup.key \
    postgresql://usuario:clave@host:5432/base_de_prueba
  ```
  Descifra, restaura con `pg_restore --clean` y dice cuánto tardó y cuántas filas
  quedaron por tabla. Contra una base de prueba, nunca la de producción. Con el
  volcado del local de demostración (60 días de historia, 1 920 pedidos) tarda
  segundos; la prueba con el respaldo real de producción y su tiempo quedan
  anotados en el informe.

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
- Anular un cobro mal hecho (RFS-25): `POST /orders/{id}/payments/{payment_id}/void`
  con `{reason}`, solo con `cash.manage`. El cobro queda en `voided_payments` con
  quién y por qué, deja de sumar a la cuenta, a la caja y a los reportes, y el
  pedido vuelve a `served` si estaba pagado. 409 `payment_not_voidable` si el
  turno de caja del cobro ya se cerró y `order_already_invoiced` si el pedido
  tiene comprobante vigente (primero se anula el comprobante).
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

### Control de mermas: presentaciones, conteos físicos y aprobación

- Un insumo puede tener presentación (`package_size`, `package_label`) y los
  pesos del envase lleno y vacío (`full_weight_g`, `empty_weight_g`). Las compras
  se cargan en su unidad o en envases; los conteos, además, por peso: contenido =
  (peso − vacío) / (lleno − vacío) × tamaño, acotado entre 0 y el tamaño. El
  backend convierte y guarda cómo se ingresó (`entry_mode`, `entry_quantity`).
- Un conteo físico (`stock_counts`, `stock_count_lines`) se abre con la foto del
  stock de cada insumo, se carga y corrige mientras está en curso y al cerrarse
  deja un ajuste por diferencia (motivo «Conteo físico #N», `stock_count_id`). La
  diferencia es lo contado menos el stock del sistema **al cargar la línea**: lo
  que se sirve mientras dura el conteo no se toma como pérdida. Lo no contado no
  se ajusta. Hay un solo conteo en curso por local (índice parcial).
- `GET /inventory/stock-counts/{id}/differences` valoriza cada diferencia al costo
  vigente al cerrar, de la mayor pérdida a la mayor sobra;
  `GET /inventory/ingredients/{id}/count-differences` da las últimas de un insumo.
- `restaurants.waste_approval_threshold` (S/ 50.00 por omisión; `null`, sin
  aprobación): una merma que vale más queda en `waste_approvals` como
  `pending_approval`, responde 202 y no mueve el stock hasta que alguien con
  `inventory.approve_waste` la aprueba; rechazarla pide motivo y no deja
  movimiento.

### Comprobantes electrónicos (SUNAT)

- `billing` guarda los datos fiscales de cada local (`billing_settings`: RUC,
  razón social, dirección, tasa de IGV, series, usuario SOL, clave SOL y
  certificado digital) y los comprobantes (`invoices`). Lee los pedidos pagados
  por SQL.
- Los precios de la carta incluyen IGV: base = total × 100 / (100 + tasa). La
  tasa es configurable (18 % en el régimen general; un restaurante MYPE acogido
  a la Ley 31556 carga la tasa reducida vigente).
- El régimen tributario del local (`billing_settings.tax_regime`: `general`,
  `special`, `mype` o `new_rus`) decide qué comprobantes emite: con el Nuevo RUS
  solo boletas (una factura responde 422 `invalid_invoice`); los locales
  anteriores quedan en `general`. `GET /billing/capabilities` trae
  `tax_invoice_enabled` para que la pantalla de emisión oculte la factura.
- Reglas antes de emitir: la factura exige RUC (11 dígitos) y razón social;
  una boleta de más de S/ 700 exige documento; un pedido tiene un solo
  comprobante; cada serie lleva su correlativo (numerar toma la fila de datos
  fiscales con `FOR UPDATE`, creándola con los valores por omisión si el local
  no la tiene, y un índice único es la última palabra: si choca, 409). No se
  usa la fila del restaurante: el turno dura hasta que responde el proveedor y
  frenaría la toma de pedidos. La fecha de emisión y los filtros por día son
  los del local.
- El envío va por el puerto `ElectronicInvoiceGateway`, con el adaptador de
  **Lycet** (`adapters/sunat/lycet.py`, `httpx`), la API REST de
  [Greenter](https://github.com/giansalex/lycet). Lycet firma el XML (UBL 2.1),
  lo envía a SUNAT y responde con el XML firmado y la constancia (CDR). El
  adaptador arma el JSON de Greenter: boleta `03` o factura `01`, IGV por línea
  con la tasa del local, el descuento del pedido como descuento global que
  baja la base (`codTipo 02`), las cortesías como operaciones gratuitas
  (`tipAfeIgv 13`, leyenda `1002`) y el importe en letras (leyenda `1000`).
- La respuesta de SUNAT se traduce así: código 0, `accepted` («Aceptado por
  SUNAT: …»); 4000 en adelante, `accepted` («Aceptado con observaciones: …»);
  2000 a 3999 o un error del comprobante, `rejected` («Rechazado por SUNAT
  (2800): …»); SUNAT sin responder (0100, 0109, 0130–0138, 0200), Lycet con
  error HTTP o sin conexión, `pending` (se reenvía). Sin RUC, usuario SOL o
  certificado, o con el certificado vencido (`is_ready` es falso: compara
  `certificate_expires_at` con la hora del puerto `Clock`), el comprobante queda
  `simulated` («sin enviar»); sin
  `LYCET_URL`/`LYCET_TOKEN` en el servidor, también.
- Lycet es **uno para todo el servidor** (`LYCET_URL`, `LYCET_TOKEN`) y atiende a
  varias empresas: elige usuario SOL y certificado por el RUC del comprobante.
  Cada local se registra solo: al guardar el usuario SOL y la clave (`PUT
  /billing/settings`) o subir el certificado (`PUT /billing/settings/certificate`),
  RestHub llama a `PUT /api/v1/configuration/company/{ruc}` de Lycet, y vuelve a
  hacerlo cuando cambia el RUC, el usuario, la clave o el certificado (el RUC
  anterior se borra). Si Lycet rechaza el usuario, la clave o el certificado
  (un 4xx), responde 422 `invalid_billing_credentials`; si Lycet no responde o
  falla (5xx, sin conexión), 502 `billing_provider`. En los dos casos no se guarda
  nada. Lycet no consulta a SUNAT al registrar: un usuario SOL o una clave errados
  aparecen al enviar (códigos 0102, 0103, 0104 o 0111), con el mensaje «SUNAT no
  aceptó el usuario SOL o la clave del local…». El token de Lycet va en la consulta (`?token=`): los logs lo
  ocultan y las trazas de llamadas salientes no llevan la consulta de la URL.
- El usuario SOL, la clave y el certificado (PEM con la llave privada) se guardan
  **cifrados** como los datos personales (`core/crypto.py`) y nunca vuelven por el
  API: `GET /billing/settings` solo dice `has_sol_credentials`, `has_certificate` y
  `certificate_expires_at`. El usuario y la clave se cambian juntos; si cambia el
  RUC, hay que volver a escribirlos (422). La bitácora anota que cambiaron, nunca
  su valor.
- Certificado: `PUT /billing/settings/certificate` (`billing.manage`) con
  `{content_base64, password}`: un `.pfx`/`.p12` con su contraseña o un `.pem`, de
  hasta 64 KB. Se comprueba que abre, que trae la llave privada RSA que corresponde
  al certificado y que no está vencido; si no, 422 `invalid_certificate`. La
  contraseña no se guarda.
- Documentos: se guardan, cifrados, el XML firmado, el CDR (zip en base64) y el
  documento enviado. `GET /billing/invoices/{id}/pdf|xml|cdr` (misma regla que ver
  el comprobante) los entrega: el PDF lo arma Lycet al momento con el documento
  enviado; el XML va como `application/xml; charset=utf-8` y el CDR sale como
  `R-<serie>-<número>.zip`. Sin ese documento, 409
  `invoice_document_unavailable`; Lycet caído al pedir el PDF, 502. El comprobante
  dice cuáles hay con `has_pdf`, `has_xml` y `has_cdr`.
- El local de muestra nunca llama a Lycet: aunque tenga datos fiscales y
  credenciales, su `ElectronicInvoiceGateway` (`adapters/sunat/sandbox.py`) deja el
  comprobante `simulated` con el mensaje «Local de muestra: simulado, no se
  envió a SUNAT.» y no registra nada. El router lo elige por local leyendo
  `restaurants.is_sandbox` (`SqlAlchemySandboxDirectory`, del propio `billing`).
- Emitir exige `billing.issue` (mesero y encargado); configurar, listar y
  reenviar, `billing.manage`. Sin `orders.read_all` (el mesero), emitir o ver
  el comprobante de un pedido solo vale para los que tomó él: el de otro
  responde 404, igual que `GET /orders/{id}`.

#### Anular y reemitir (notas de crédito)

Cuando el cliente pide factura en vez de boleta (o al revés, o cambia el RUC o
DNI), el encargado anula el comprobante vigente y emite otro para el mismo pedido.

- `POST /billing/invoices/{id}/void` (`billing.manage`) con `{reason}` (3 a 200
  caracteres; va a SUNAT como motivo). Según el estado del comprobante:
  - `accepted`: emite una nota de crédito de motivo 01 («Anulación de la
    operación», catálogo 09) por el mismo total, por `POST /note/send` de Lycet.
    El comprobante queda anulado solo si SUNAT acepta la nota; pendiente o
    rechazada, sigue vigente y la nota se reenvía con
    `POST /billing/credit-notes/{id}/resend` (responde el comprobante al día).
  - `simulated` o `rejected`: nunca valió ante SUNAT; se anula solo en RestHub.
  - `pending`: 409 `invoice_not_voidable` hasta que se resuelva (reenviarlo).
  - Ya anulado: 409 `invoice_already_voided`; con la nota en curso: 409
    `invoice_not_voidable`.
- Las notas van una por una, como los comprobantes (desde la RS 114-2019 SUNAT
  admite enviar así las de boletas, sin resumen diario), con serie propia por
  local: `BC01` anula boletas y `FC01` facturas (configurables en los datos
  fiscales: `boleta_credit_note_series`, `factura_credit_note_series`). Una nota
  no admite descuentos globales: el del comprobante va repartido en sus líneas.
- Un pedido tiene a lo sumo un comprobante vigente (índice único parcial
  `uq_invoices_order_current` sobre los no anulados). Con el anterior anulado,
  `POST /billing/invoices` emite el nuevo, enlazado con `replaces_invoice_id`;
  `GET /billing/orders/{id}/invoice` da el vigente o, si no hay, el último anulado.
- El comprobante dice `is_voided`, `voided_at`, `void_reason`, `credit_note_id` y
  `credit_note` (la nota resumida, aceptada o en curso). Sus documentos:
  `GET /billing/credit-notes/{id}/pdf|xml|cdr`, con la regla de ver el comprobante.
- Bitácora: `credit_note_issued` y `invoice_voided`. Uno anulado no se reenvía
  ni se envía por correo.

#### Lycet en local (SUNAT beta)

Por ahora Lycet corre solo en local, con Docker, contra el entorno **BETA** de
SUNAT. No hay imagen oficial: `docker/lycet/compose.yml` la construye desde el
código de Lycet fijado a un commit.

```bash
docker compose -f docker/lycet/compose.yml up -d --build   # http://127.0.0.1:8088
```

En el `.env` del backend: `LYCET_URL=http://127.0.0.1:8088` y
`LYCET_TOKEN=resthub-local` (el `CLIENT_TOKEN` de la instancia; se cambia
levantándola con otro `LYCET_TOKEN`). Las credenciales por omisión de Lycet van
vacías: un RUC no registrado no firma con las de otro. Las empresas registradas
viven en el volumen `lycet-data`; si se borra, cada local vuelve a guardar sus
credenciales.

Para probar en beta, cargar en el local el RUC `20000000001`, usuario SOL
`MODDATOS`, clave `moddatos` y el certificado de prueba público de Greenter, que
viene dentro de la imagen:

```bash
docker compose -f docker/lycet/compose.yml exec lycet cat tests/Resources/cert.pem > cert-beta.pem
```

La prueba de integración `tests/test_lycet_beta.py` se omite salvo que se le den
`LYCET_BETA_URL`, `LYCET_BETA_TOKEN` y `LYCET_BETA_CERT` (ver su docstring). En
beta SUNAT acepta casi todo (y avisa con observaciones), así que no reemplaza
probar con un certificado real antes de producción.

#### Enviar por correo (Brevo)

- Puerto `EmailGateway`; adaptador `adapters/email/brevo.py` (`httpx`, sin SDK):
  `POST https://api.brevo.com/v3/smtp/email` con la cabecera `api-key`, remitente
  verificado, `htmlContent` y `textContent`; Brevo responde 201 con el `messageId`
  (https://developers.brevo.com/reference/sendtransacemail). Railway Hobby bloquea el
  SMTP saliente: por eso es HTTPS.
- `POST /billing/invoices/{id}/email` (`billing.issue`, con la misma visibilidad que ver
  el comprobante) envía el PDF adjunto (`attachment` en base64, armado por Lycet) al
  correo indicado; sin PDF, 422. Límite de
  `EMAIL_MAX_PER_HOUR` por cuenta (429 `email_rate_limited`), en memoria del proceso. La
  bitácora anota `a•••@correo.pe`, nunca el correo completo; los logs, ni la dirección
  ni el texto de error de Brevo. Sin `BREVO_API_KEY` o `EMAIL_FROM_ADDRESS` responde 409
  `email_sending_disabled`; `GET /billing/capabilities` dice qué está encendido.
- **Crear la clave de Brevo**: cuenta gratuita en brevo.com, *SMTP & API*, *API Keys*,
  *Generate a new API key* y copiarla a `BREVO_API_KEY` (se muestra una sola vez). **Verificar
  el remitente**: *Senders, Domains & Dedicated IPs*, *Senders*, agregar el correo de
  `EMAIL_FROM_ADDRESS` y confirmar el mensaje que llega a esa bandeja (para entregar bien,
  autenticar el dominio con DKIM/SPF desde *Domains*). El plan gratuito permite 300 correos al
  día.

#### Respaldo en Google Drive

- Si el comprobante queda **aceptado** y el respaldo está encendido, pasa a
  `archive_status = pending` y, al confirmarse la transacción, una tarea en segundo plano
  (`wiring/invoice_archive.py`, `core/background.py`) toma el XML y el CDR guardados, pide
  el PDF a Lycet y los sube con el puerto `DocumentArchiveGateway`. El cobro no espera.
- Adaptador `adapters/archive/google_drive.py`: OAuth con refresh token de la cuenta del dueño
  (las cuentas de servicio no tienen cuota en un Drive personal) y subida multipart de la API v3
  (https://developers.google.com/drive/api/guides/manage-uploads). Carpetas privadas
  `<raíz>/<razón social> [id]/<año>/<año-mes>`; archivos `<serie>-<número>.pdf|xml` y
  `R-<serie>-<número>.zip` (el CDR). Subir es idempotente por nombre. Alcance `drive.file`: la app solo ve lo
  que crea.
- Estados (`archive_status`): `disabled` (apagado o sin documentos aceptados), `pending`,
  `done`, `failed` (con `archive_message` sin datos personales). `POST
  /billing/invoices/{id}/archive` (`billing.manage`) lo pide o reintenta (también sirve si el
  proceso se reinició con el respaldo `pending`); responde 202 con el comprobante.
- **Obtener las credenciales** (una vez): en Google Cloud Console crear un proyecto, habilitar
  *Google Drive API*, configurar la *pantalla de consentimiento OAuth* (externa; en modo
  *Prueba* agregar tu cuenta como usuario de prueba, y mejor *Publicar la app* para que el
  refresh token no caduque a los 7 días) y crear unas *Credenciales*: *ID de cliente OAuth* de
  tipo **App de escritorio**. Luego:

  ```bash
  uv run python scripts/drive_authorize.py --client-id <ID> --client-secret <SECRETO>
  ```

  El script abre el navegador, canjea el código (PKCE), crea la carpeta raíz «RestHub
  comprobantes» e imprime `GOOGLE_DRIVE_REFRESH_TOKEN` y `GOOGLE_DRIVE_FOLDER_ID`. Cargar las
  cuatro variables `GOOGLE_DRIVE_*` en Railway.

### Búsqueda de RUC y DNI (Factiliza)

- `lookup` consulta [Factiliza](https://docs.factiliza.com/api-consulta/endpoint/ruc.md)
  (`GET https://api.factiliza.com/v1/ruc/info/{ruc}` y `/dni/info/{dni}`, con
  `Authorization: Bearer <token>`) para autocompletar el alta de un cliente y los
  datos de una factura o boleta. Es un tercero: se declara en la política de
  privacidad como encargado de tratamiento. No guarda nada; el resultado vuelve a
  quien preguntó y solo se usa si lo confirma.
- `FACTILIZA_TOKEN` vacío apaga la función: `GET /lookup/status` responde
  `{"enabled": false}` (el frontend no ofrece el botón) y las búsquedas, 404 con
  código `lookup_disabled`. El token se carga en las variables del servicio, nunca
  en el repositorio, y no se escribe en logs. Tampoco el número consultado: el
  registro de `httpx` se sube a WARNING porque su URL lo lleva.
- Valida el formato antes de llamar (RUC de 11 dígitos, prefijo 10/15/16/17/20 y
  dígito verificador; DNI de 8 dígitos): 422 `invalid_document_number`. No
  encontrado, 404 `document_not_found`; el tercero caído, lento (5 s) o con la
  credencial rechazada, 503 `lookup_unavailable`. Sin reintentos.
- Una consulta resuelta se recuerda 10 minutos en memoria del proceso
  (`LOOKUP_CACHE_SECONDS`) y cada cuenta puede hacer `LOOKUP_PER_MINUTE` por
  minuto (429 `lookup_rate_limited` con `Retry-After`). El plan gratuito de
  Factiliza da 100 consultas al mes.
- Exige `billing.issue` o `customers.manage`.

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

- El puerto `DecisionEngineGateway` tiene dos adaptadores: `JevDecisionEngine`
  (TypeSafe AI, `POST {TYPESAFE_BASE_URL}/v1/systemone` con `httpx`, sin el SDK)
  y `RuleBasedDecisionEngine` (palabras clave en castellano y umbrales de
  cobertura). `DecisionEngineSelector` elige: sin ninguna clave, reglas;
  con clave, Jev, y si falla, pasa de `TYPESAFE_TIMEOUT_SECONDS`, responde algo
  ilegible o con confianza menor a `AI_MIN_CONFIDENCE`, reglas. Cada caída a
  reglas queda anotada (`fallback_reason`) y en el log.
- Dos transportes para Jev, el mismo motor y el mismo contrato: con
  `TYPESAFE_API_KEY`, TypeSafe directo; si no, y hay `OPENROUTER_API_KEY`, Jev
  por OpenRouter (`POST https://openrouter.ai/api/v1/systemone`, modelo
  `JEV_OPENROUTER_MODEL`, por omisión `~typesafe/jev-latest`). **Basta con
  `OPENROUTER_API_KEY` para usar Jev**; no hace falta cuenta en TypeSafe. No es
  `chat/completions`: OpenRouter expone el mismo endpoint `systemone` (modalidad
  `text->decisions`), como documenta el SDK de TypeSafe
  (<https://docs.typesafe.ai/sdk/python/usage.md#configuring-the-base-url>) y la
  referencia del API (<https://docs.typesafe.ai/api.md>); modelos en
  <https://openrouter.ai/~typesafe/jev-latest>. Plazo, confianza mínima,
  anonimizado y registro del motivo son los mismos; `details.provider`
  (`typesafe` u `openrouter`) y `model` dicen quién decidió. Sin verificar con
  clave real: que la respuesta de OpenRouter sea idéntica a la de TypeSafe.
- A Jev se le manda un `state` JSON con los números ya calculados en código y
  preguntas tipadas (`choice`, `score`, `noul`) con instrucciones y criterios en
  inglés. Una llamada por insumo, nota o merma, con todas sus preguntas; hasta
  seis en paralelo. Ante `429`/`529` reintenta una vez tras una pausa breve.
- Datos personales (Ley N.º 29733): las notas y los motivos de merma salen del
  Perú hacia TypeSafe, así que antes se tapan correos, números de seis dígitos
  o más (teléfonos, DNI, RUC) y el nombre del cliente del pedido
  (`insights/domain/privacy.py`); `ai_decisions` guarda lo mismo que se envió.
  El local puede apagar la IA externa (`PATCH /restaurant` con
  `external_ai_enabled: false`): entonces deciden las reglas, nada sale y la
  decisión queda con `fallback_reason: "disabled"`.
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
| PATCH  | `/staff/{id}/status` `{is_active, reason?}` (desactivar exige `reason`) | `staff.manage` |
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
| GET    | `/menu/items/{id}/photo`                      | `menu.read`                          |
| PUT    | `/menu/items/{id}/photo`                      | `menu.manage`                        |
| DELETE | `/menu/items/{id}/photo`                      | `menu.manage`                        |
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
| POST   | `/customers/{id}/reveal`                      | `customers.read`                     |
| GET · POST | `/reservations` (`?day=`)                 | `reservations.read` · `reservations.manage` |
| PUT    | `/reservations/{id}`                          | `reservations.manage`                |
| POST   | `/reservations/{id}/status?value=`            | `reservations.manage`                |
| POST   | `/auth/refresh`                               | sesión (no una vista previa: 401)    |
| POST   | `/auth/me/password` `{current_password, new_password}` | sesión (una vista previa: 403). 200 con la misma forma que `/auth/login` (`access_token`, `token_type`, `expires_in`, `user`, `restaurant`, `permissions`...): trae un token nuevo y los anteriores, el de la petición incluido, dejan de valer (antes era 204 sin cuerpo) |
| POST   | `/auth/preview`                               | público: canjea un código de vista previa |
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
| GET    | `/inventory/stock-counts`, `/inventory/stock-counts/{id}` | `inventory.read`         |
| GET    | `/inventory/stock-counts/{id}/differences`    | `inventory.read`                     |
| GET    | `/inventory/ingredients/{id}/count-differences` | `inventory.read`                   |
| POST   | `/inventory/stock-counts`, `/{id}/close`, `/{id}/cancel` | `inventory.manage`        |
| PUT    | `/inventory/stock-counts/{id}/lines`          | `inventory.manage`                   |
| GET    | `/inventory/waste-approvals`                  | `inventory.read` o `inventory.approve_waste` |
| POST   | `/inventory/waste-approvals/{id}/approve`, `/reject` | `inventory.approve_waste`     |
| GET · PUT | `/billing/settings`                        | `billing.manage`                     |
| PUT    | `/billing/settings/certificate`               | `billing.manage`                     |
| POST   | `/billing/invoices`                           | `billing.issue`                      |
| GET    | `/billing/invoices`                           | `billing.manage`                     |
| GET    | `/billing/invoices/{id}`, `/billing/orders/{order_id}/invoice` | `billing.issue`     |
| GET    | `/billing/invoices/{id}/pdf`, `/xml`, `/cdr`  | `billing.issue`                      |
| POST   | `/billing/invoices/{id}/resend`               | `billing.manage`                     |
| POST   | `/billing/invoices/{id}/void`                 | `billing.manage`                     |
| POST   | `/billing/credit-notes/{id}/resend`           | `billing.manage`                     |
| GET    | `/billing/credit-notes/{id}/pdf`, `/xml`, `/cdr` | `billing.issue`                   |
| POST   | `/billing/invoices/{id}/email`                | `billing.issue`                      |
| POST   | `/billing/invoices/{id}/archive`              | `billing.manage`                     |
| GET    | `/billing/capabilities`                       | `billing.issue`                      |
| GET    | `/lookup/status`, `/lookup/ruc/{ruc}`, `/lookup/dni/{dni}` | `billing.issue` o `customers.manage` |
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
- El JWT lleva solo `sub`, `restaurant_id` y `scope: "restaurant"` (uno
  anterior sin `scope` o que traiga `role` sigue sirviendo; el rol se ignora);
  el de vista previa suma `preview: true` y `platform_admin_id`. Rol, permisos, estado y restaurante se
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
- `GET /api/v1/auth/me` (y `POST /auth/login`) devuelve, para una cuenta del
  personal, `kind: "staff"`, usuario (con `role_id` y `role_label`, el nombre
  del rol), restaurante (con su zona horaria, `timezone`), la lista de permisos
  con la que el frontend arma la navegación y `preview` (si es una vista previa
  de la plataforma). Para una cuenta de plataforma, `kind: "platform"` (ver
  [Administración del sistema](#administración-del-sistema)).
- Términos y privacidad (Ley N.º 29733), con dos consentimientos separados: la
  sesión trae `terms_version`/`terms_accepted` (términos y condiciones) y
  `privacy_version`/`privacy_accepted` (tratamiento de datos personales). Sin
  aceptar los dos la interfaz no deja trabajar;
  `POST /auth/me/terms {terms_version, privacy_version}` exige ambos (422 si falta
  uno, 409 si alguna versión ya cambió, 403 en una vista previa) y deja un asiento
  por cada uno en la bitácora (`terms_accepted` y `privacy_accepted`). Cambiar
  `TERMS_VERSION` o `PRIVACY_VERSION` (`accounts/domain/entities.py`) vuelve a
  pedir ese consentimiento a todos.
- Motivo de baja (RFS-18): desactivar una cuenta exige `reason` (3 a 200
  caracteres; 422 `invalid_deactivation_reason` si falta) y guarda quién
  (`deactivated_by`), cuándo (`deactivated_at`) y por qué (`deactivation_reason`);
  `/staff` los devuelve, junto con `must_change_password`. Reactivarla los limpia;
  el motivo queda también en el asiento de la bitácora.
- El personal se da de alta y se edita con `role_id`, un rol del
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

Con `DEBUG=false`, las URL de los servicios externos (`LYCET_URL`, `OPENROUTER_BASE_URL`,
`TYPESAFE_BASE_URL`, `FACTILIZA_BASE_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`) tienen que ser
`https://`; `http://` solo se admite hacia la propia máquina (`localhost`, `127.0.0.1`, `::1`).
Si no, la aplicación no arranca (ASVS V12.3.1, V12.3.2).

| Variable                     | Por defecto                        | Notas                                                   |
| ---------------------------- | ---------------------------------- | ------------------------------------------------------- |
| `APP_NAME`                   | `resthub-api`                      |                                                         |
| `APP_VERSION`                | `0.1.0`                            |                                                         |
| `DEBUG`                      | `false`                            | Con `false` exige PostgreSQL, un `JWT_SECRET_KEY` propio y `DATA_ENCRYPTION_KEY`, y no publica `/docs`, `/redoc` ni `openapi.json`. `true` en local, en las pruebas y en E2E. |
| `LOG_LEVEL`                  | `INFO`                             |                                                         |
| `LOG_JSON`                   | `false`                            | `true` en despliegue: una línea JSON por evento.        |
| `DATABASE_URL`               | `sqlite+aiosqlite:///./resthub.db` | En despliegue, PostgreSQL. `postgres://` y `postgresql://` pasan solas a `postgresql+asyncpg://`. |
| `DATABASE_SSL_MODE`          | `prefer`                           | TLS hacia PostgreSQL (modos de libpq): `disable`, `prefer` (cifra si el servidor lo ofrece; degradable), `require` (cifra sin validar el certificado), `verify-ca`, `verify-full` (valida certificado y nombre). Con `DEBUG=false` y `disable`/`prefer`, aviso `database.tls_not_enforced` al arrancar. Ver «Despliegue en Railway». |
| `DATABASE_SSL_ROOT_CERT`     | sin valor                          | PEM con la CA del servidor para `verify-ca`/`verify-full`. Vacío: las CA del sistema. |
| `CORS_ALLOWED_ORIGINS`       | `["http://localhost:5173"]`        | Lista JSON o separada por comas, sin barra final.       |
| `CORS_ALLOWED_ORIGIN_REGEX`  | sin valor                          | Patrón que el origen debe calzar entero, además de la lista. Para los previews de Vercel en el entorno `develop`. |
| `FRONTEND_BASE_URL`          | `http://localhost:5173`            | Se envía a OpenRouter como `HTTP-Referer`.              |
| `RATE_LIMIT_PER_MINUTE`      | `1200`                             | Peticiones por minuto de cada IP; 0 lo apaga.           |
| `MAX_REQUEST_BODY_BYTES`     | `1048576`                          | Tamaño máximo del cuerpo de una petición (mínimo 1024); pasado eso, 413. |
| `CLIENT_IP_HEADER`           | sin valor                          | Con un WAF por delante, la cabecera con la IP real (`cf-connecting-ip` en Cloudflare). |
| `FORWARDED_ALLOW_IPS`        | `127.0.0.1`                        | La lee uvicorn: IP o redes de las que cree `X-Forwarded-*`. Nunca `*` (se avisa al arrancar con `DEBUG=false`). |
| `GEOIP_DATABASE_PATH`        | sin valor                          | Ruta a una base GeoLite2 City (`.mmdb`) para ubicar los accesos sin mandar la IP a terceros. |
| `ACCESS_HISTORY_RETENTION_DAYS` | `90`                            | Días que se guarda el historial de accesos.             |
| `SESSION_COOKIE_SECURE`      | `true`                             | La cookie de sesión solo viaja por https. `false` solo en local por http: con `DEBUG=false` la aplicación no arranca. |
| `SESSION_COOKIE_SAMESITE`    | `none`                             | `none` porque el frontend y el API están en sitios distintos; `lax` en local. |
| `JWT_SECRET_KEY`             | valor de desarrollo                | Mínimo 32 bytes.                                        |
| `ALLOW_DEMO_SEED`            | `false`                            | Solo en el entorno de demostración: deja correr los seeds contra una base no local. |
| `DATA_ENCRYPTION_KEY`        | vacía (llave de desarrollo)        | 32 bytes en base64 para cifrar datos personales. Obligatoria con `DEBUG=false`; ver «Cifrado en reposo». |
| `DATA_ENCRYPTION_PREVIOUS_KEYS` | vacía                           | Llaves anteriores, separadas por comas, solo durante una rotación. |
| `JWT_ALGORITHM`              | `HS256`                            | Único valor admitido; otro (`none`, `RS256`…) impide el arranque. |
| `JWT_ISSUER`                 | `resthub`                          | `iss` de los tokens; se exige al leerlos.               |
| `JWT_AUDIENCE`               | `resthub-api`                      | `aud` de los tokens del personal (y de la vista previa). |
| `JWT_PLATFORM_AUDIENCE`      | `resthub-platform`                 | `aud` de los tokens de la administración del sistema.   |
| `DLP_ALERT_MAX_CARDS`        | `20`                               | Fichas de clientes que una cuenta puede abrir o exportar en la ventana antes de que salte la alerta. |
| `DLP_ALERT_WINDOW_MINUTES`   | `10`                               | Minutos de esa ventana.                                 |
| `ACCESS_TOKEN_TTL_SECONDS`   | `3600`                             |                                                         |
| `SESSION_ABSOLUTE_LIFETIME_HOURS` | `12`                          | Vida máxima de una sesión de dispositivo desde que se abrió, aunque se renueve (1 a 168). |
| `MAX_SESSIONS_PER_ACCOUNT`   | `10`                               | Sesiones abiertas a la vez por cuenta; abrir una más cierra la usada hace más tiempo (1 a 100). |
| `OBSERVABILITY_ENABLED`      | `true`                             | `false` apaga la telemetría del panel de observabilidad. |
| `OBSERVABILITY_RETENTION_DAYS` | `14`                             | Días que se guardan peticiones y eventos (1 a 365).     |
| `OBSERVABILITY_MAX_ROWS_PER_MINUTE` | `6000`                      | Filas por minuto (peticiones más eventos) que guarda cada proceso; lo que pasa se descarta y se cuenta. `0` quita el tope. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | sin valor                         | Base de un colector OTLP/HTTP; con valor se activan las trazas OpenTelemetry. Vacío: no se instala nada. |
| `OTEL_EXPORTER_OTLP_HEADERS` | sin valor                          | `clave=valor,otra=valor` para un colector con credencial (nunca se escribe en logs). |
| `OTEL_SERVICE_NAME`          | `APP_NAME`                         | Nombre del servicio en las trazas.                      |
| `OTEL_TRACES_SAMPLER_ARG`    | `1.0`                              | Fracción de trazas nuevas que se guardan (0 a 1).       |
| `PASSWORD_ARGON2_TIME_COST` / `_MEMORY_KIB` / `_PARALLELISM` | `3` / `65536` / `1` | Costo de Argon2id de las contraseñas nuevas. No hace falta tocarlos. |
| `OPENROUTER_API_KEY`         | vacío                              | Vacío apaga la IA; el resto funciona igual.             |
| `OPENROUTER_MODEL`           | `deepseek/deepseek-v4.1-flash`     |                                                         |
| `OPENROUTER_FALLBACK_MODEL`  | `deepseek/deepseek-v4-flash-0731`  |                                                         |
| `OPENROUTER_TIMEOUT_SECONDS` | `45`                               |                                                         |
| `TYPESAFE_API_KEY`           | vacío                              | Jev (TypeSafe AI). Vacío: deciden las reglas fijas. Nunca se escribe en logs. |
| `TYPESAFE_BASE_URL`          | `https://api.typesafe.ai`          |                                                         |
| `TYPESAFE_MODEL`             | `jev-latest`                       | Alias; la respuesta guarda la versión que respondió.    |
| `TYPESAFE_TIMEOUT_SECONDS`   | `3`                                | Plazo por llamada (TypeSafe u OpenRouter); pasado, deciden las reglas. |
| `JEV_OPENROUTER_MODEL`       | `~typesafe/jev-latest`             | Modelo de Jev por OpenRouter; se usa si no hay `TYPESAFE_API_KEY` pero sí `OPENROUTER_API_KEY`. |
| `LYCET_URL` · `LYCET_TOKEN`  | vacío                              | Lycet (Greenter), el servicio que firma y envía a SUNAT; uno para todo el servidor. Vacío deja los comprobantes «sin enviar». Nunca en logs. |
| `BREVO_API_KEY`              | vacío                              | Correo de comprobantes por Brevo. Vacío apaga el envío. Nunca en logs. |
| `EMAIL_FROM_ADDRESS`         | vacío                              | Remitente verificado en Brevo. |
| `EMAIL_FROM_NAME`            | `RestHub`                          | Nombre del remitente. |
| `EMAIL_MAX_PER_HOUR`         | `20`                               | Correos de comprobantes por cuenta y hora. |
| `GOOGLE_DRIVE_CLIENT_ID` · `_CLIENT_SECRET` · `_REFRESH_TOKEN` · `_FOLDER_ID` | vacío | Respaldo en Drive; con alguna vacía queda apagado. Salen de `scripts/drive_authorize.py`. |
| `AI_MIN_CONFIDENCE`          | `0.5`                              | Bajo esta confianza de Jev, deciden las reglas.         |
| `FACTILIZA_TOKEN`            | vacío                              | Token de Factiliza para buscar RUC/DNI. Vacío: función apagada. Nunca se escribe en logs. |
| `FACTILIZA_BASE_URL`         | `https://api.factiliza.com/v1`     |                                                         |
| `FACTILIZA_TIMEOUT_SECONDS`  | `5`                                | Plazo por llamada, sin reintentos.                      |
| `LOOKUP_CACHE_SECONDS`       | `600`                              | Cuánto se recuerda una búsqueda resuelta; 0 lo apaga.   |
| `LOOKUP_PER_MINUTE`          | `10`                               | Búsquedas por minuto de cada cuenta; 0 lo apaga.        |

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
4. Crear la primera cuenta de plataforma y, con ella o con el script, el primer
   restaurante (no hay registro público).

### Variables del servicio

| Variable               | Valor                                                                 |
| ---------------------- | --------------------------------------------------------------------- |
| `DATABASE_URL`         | `${{Postgres.DATABASE_URL}}`, referencia a la base del proyecto. Llega como `postgresql://` y la configuración la pasa a asyncpg. |
| `JWT_SECRET_KEY`       | Propio, de 32 bytes o más: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `DATA_ENCRYPTION_KEY`  | Obligatoria: 32 bytes en base64 (`python -m resthub.core.data_keys`). Sin ella la aplicación no arranca. Guárdala también fuera de Railway: si se pierde, los datos cifrados no se recuperan. Cámbiala solo rotándola (ver «Cifrado en reposo»). |
| `DEBUG`                | `false` (también es el valor por omisión). Así la aplicación se niega a arrancar con SQLite, con la clave de ejemplo o sin llave de cifrado. |
| `LOG_JSON`             | `true`: una línea JSON por evento en los logs de Railway.             |
| `DATABASE_SSL_MODE`    | `require`. El PostgreSQL de Railway acepta TLS con un certificado propio (sin CA pública), así que `verify-full` no sirve sin su CA. Pruébalo primero en `develop`: si la base no ofrece TLS, la aplicación no conecta y el despliegue nuevo no pasa el sondeo (el anterior sigue en pie). Sin la variable queda `prefer` y el arranque avisa. |
| `CORS_ALLOWED_ORIGINS` | El origen del frontend, por ejemplo `https://resthub.example.com` (varios, separados por comas). |
| `FRONTEND_BASE_URL`    | La misma URL del frontend.                                            |
| `TYPESAFE_API_KEY`     | Opcional. Sin ella, Jev se llama por OpenRouter si hay `OPENROUTER_API_KEY`; sin ninguna, deciden las reglas fijas. |
| `OPENROUTER_API_KEY`   | Opcional. Basta para usar Jev (modelo `JEV_OPENROUTER_MODEL`, por omisión `~typesafe/jev-latest`). |
| `LYCET_URL`, `LYCET_TOKEN` | Opcionales por ahora: Lycet corre solo en local (ver «Lycet en local»). Sin ellas los comprobantes quedan «sin enviar». |
| `BREVO_API_KEY`, `EMAIL_FROM_ADDRESS` | Opcionales. Sin ellas no se ofrece enviar comprobantes por correo. |
| `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, `GOOGLE_DRIVE_REFRESH_TOKEN`, `GOOGLE_DRIVE_FOLDER_ID` | Opcionales. Sin ellas no hay respaldo en Drive. |
| `FACTILIZA_TOKEN`      | Opcional. Token de Factiliza para buscar RUC/DNI; sin él no se ofrece el botón. |
| `CORS_ALLOWED_ORIGIN_REGEX` | Solo en `develop`: el patrón de los previews de Vercel (abajo).  |

`PORT` lo define Railway; no hay que cargarlo.

### Entorno `develop` para los previews de Vercel

`production` despliega `main`. El entorno `develop` del mismo proyecto
despliega la rama `develop` con **su propia base** (otra instancia de
PostgreSQL, sin datos de producción) y su propio `JWT_SECRET_KEY`, así que un
token de un entorno no sirve en el otro. Los previews de Vercel apuntan a él
(`VITE_API_URL` del entorno *Preview*).

Cada preview de Vercel tiene su propia URL (`resthub-frontend-<hash>-<equipo>.vercel.app`,
`resthub-frontend-git-<rama>-<equipo>.vercel.app`), así que en `develop` el
CORS usa, además de la lista, `CORS_ALLOWED_ORIGIN_REGEX`:

```text
https://resthub-frontend-[a-z0-9-]+-<equipo>\.vercel\.app
```

El origen tiene que calzar entero con el patrón. La sesión viaja en la cabecera
`Authorization`, no en cookies, así que el CORS no es lo que protege las
cuentas; aun así el patrón no se usa en `production`.

### Primera cuenta de plataforma y primer restaurante

La administración del sistema no usa la plataforma sin la verificación en dos
pasos (RFS-22): hasta activarla (`/auth/me/two-factor`), los endpoints de
`/platform/*` responden 403 `two_factor_setup_required`, `GET /auth/me` trae
`two_factor_setup_required: true` y no se puede desactivar. Las pruebas E2E del
frontend la apagan con `PLATFORM_TWO_FACTOR_REQUIRED=false`.

`scripts/create_platform_admin.py` y `scripts/create_restaurant.py` están
dentro de la imagen. Con una cuenta de plataforma, los restaurantes se dan de
alta desde `/plataforma`:

```bash
railway ssh -- python scripts/create_platform_admin.py \
  --email equipo@resthub.pe --name "Equipo RestHub" --generate
```

El restaurante también se puede crear por consola. Lo más directo es
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
`cocina@resthub.dev` quedarían con la contraseña `resthub123`. La de plataforma
(`plataforma@resthub.dev`) no se siembra en el demo desplegado, solo en
desarrollo local: no tiene restaurante y con esa contraseña cualquiera
administraría todos los locales. En el demo se crea con
`scripts/create_platform_admin.py`. Al terminar se puede quitar la variable;
los seeds no hacen falta para que la aplicación funcione.
