# RestHub — backend

API de FastAPI para restaurantes pequeños: pedidos, cobro, inventario, BI y
decisiones con IA. Multi-restaurante. Arquitectura, módulos y endpoints están
en el `README.md`; esto es lo que un agente necesita además.

## Verificación

```bash
uv sync --locked --group dev --group coverage
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run mypy
uv run deptry .
uv run pytest -q -n auto --cov=resthub
```

La suite corre en paralelo (`-n auto`, pytest-xdist) y en orden aleatorio
(pytest-randomly): una prueba que depende de otra falla, y no se arregla fijando
la semilla sino quitando la dependencia. Para repetir una falla, la semilla sale
en la cabecera del informe: `uv run pytest -p randomly --randomly-seed=<semilla>`.
Cada prueba tiene su propia base (SQLite en memoria) y ningún estado de módulo.

Commits en Conventional Commits en español (`feat(pedidos): …`); el código, en
inglés (ver «Idioma» más abajo). El hook
`.githooks/commit-msg` comprueba la forma, que el resumen no pase de 100
caracteres y que no haya `Co-authored-by` ni atribución a una IA (`Generated
with Claude Code`, `🤖 Generated with`…); el español es convención del equipo,
no lo comprueba el hook. El título y la descripción de un PR tampoco pueden
llevar esa atribución: lo comprueba el CI.

## Reglas que el CI hace cumplir

- **Ruff**: `select = ["ALL"]` con una lista `ignore` que explica cada regla
  descartada en `pyproject.toml` (presencia de docstring `D100-D107`, `COM812` e
  `ISC001` por el formateador, `CPY`, `TC001-003` por Pydantic/FastAPI). Los
  `per-file-ignores` también llevan su motivo (tests: `S101`, `PLR2004`, `SLF001`,
  `ANN401`, `FBT`; los routers de FastAPI: `PLR0913`/`PLR0917` y `FBT001-002`
  porque la inyección y las banderas de la consulta son parámetros del framework).
  Complejidad `C90` máx. 9; `PLR` con máx. 8 argumentos y 6 posicionales. Un
  `noqa` lleva su motivo (`# noqa: BLE001 - …`) y es puntual; en `ARG`, el
  argumento que impone la firma de un puerto se suprime así, no se renombra.
  Los mensajes de una excepción van en una variable (`message = "…"` y luego
  `raise XError(message)`, reglas `EM`); los booleanos de una función son solo por
  nombre (`def f(*, flag: bool = False)`, `FBT`). Ruff está fijado a `<0.17`.
- **mypy estricto** (`uv run mypy`, plugin de Pydantic) sobre `src`. Sin
  `# type: ignore`; si no hay alternativa, con código de error y motivo.
- **Cobertura**: `fail_under = 96.5`. Sube el piso cuando suba la cobertura; no
  lo bajes para que pase un cambio.
- **Propiedades (Hypothesis)**: `tests/test_properties_orders.py` y
  `tests/test_properties_inventory.py` fijan las reglas con aritmética (totales,
  descuentos, cobro por platos o por partes, vuelto, propinas, arqueo, costo
  ponderado, reposición). Una regla de dinero nueva lleva su propiedad. Perfil
  normal corto; para buscar más a fondo:
  `uv run pytest tests/test_properties_*.py --hypothesis-profile=deep`. Si
  Hypothesis halla un contraejemplo, se arregla el código y el caso concreto se
  agrega como prueba normal.
- **Dependencias**: `uv run deptry .` falla con una dependencia declarada y sin
  uso, usada sin declarar (`starlette`) o que solo llega de rebote; las que ningún
  `import` nombra a propósito (`aiosqlite`, `tzdata`, `uvicorn`) están en
  `[tool.deptry.per_rule_ignores]` con su motivo.
- **PostgreSQL en CI**: el job `postgres` corre `alembic upgrade head`,
  `alembic check` y la misma suite contra `postgres:17`. Local:
  `RESTHUB_TEST_DATABASE_URL=postgresql+asyncpg://usuario@host:5432/base uv run pytest -n auto`
  (cada worker crea su esquema y vacía las tablas antes de cada prueba; la
  variable se lee con pydantic-settings en `tests/conftest.py`, no con `os.environ`).
  Sin esa variable la suite usa SQLite en memoria.
- **Fuzzing de la API (Schemathesis)**: el job `api-fuzzing` (bloqueante) levanta la app y corre
  `uv run --group fuzz bash scripts/fuzz_api.sh [ejemplos]` contra
  `/api/v1/openapi.json`, con la sesión del encargado y la de plataforma. Un
  identificador de ruta o de cuerpo usa `PathId`/`EntityId` y todo `offset` lleva
  `le=MAX_OFFSET` (`core/api_params.py`, `core/pagination.py`); así un número fuera
  de rango es un 422 y no un 500. Los errores del API (400, 401, 403, 404, 409,
  429 y el 422 de texto) se completan en el esquema con
  `core/openapi.py`.
- **Códigos de error**: toda respuesta de error es `{"detail": <texto en español>, "code":
  <ErrorCode>}` (`core/error_codes.py` es el catálogo único; `core/errors.py` instala los
  manejadores). Una excepción de dominio `XError` declara `code = ErrorCode.X` (mismo nombre
  en `SNAKE_CASE`; un mismo concepto en dos módulos comparte código) y el router la traduce
  con `api_error(status, error)`; un error sin excepción de dominio levanta
  `ApiError(status, texto, ErrorCode.X)`. El 422 de validación de FastAPI conserva su lista en
  `detail` y la repite en `errors`. Los valores son estables: se agregan, no se renombran.
  `tests/test_error_codes_architecture.py` exige el `code`, prohíbe códigos repetidos entre
  errores distintos y los que nadie usa.
- **Import Linter**: además de las capas, el router no construye adaptadores
  de persistencia (eso lo hace `dependencies.py` del módulo), `persistence` no
  importa web ni API, `api` no importa SQLAlchemy, `domain/ports/use_cases` no
  usan el núcleo con efectos (config, logs, telemetría, bitácora…) y el dominio
  tampoco `os`, `sys`, `asyncio`, `threading`, `random` ni `logging`.
  `SessionDep` se importa de `resthub.core.database`, no de `core.auth`.
- **`tests/test_architecture.py`** (AST puro, sin lista de excepciones):
  - los `__init__.py` están vacíos o solo con docstring (sin reexportar);
  - las clases de `ports/` son `Protocol` o tipos de datos (`dataclass(frozen=True)`,
    `Enum`, excepción), y cada `Protocol` termina en `Repository` (agregado),
    `Directory` (lectura de otro módulo), `Gateway` (servicio externo), `Hook`
    (callback), `Clock`, `Hasher`, `Reader`, `Recorder` o `Policy`;
  - los modelos Pydantic de `adapters/api` terminan en `Request`/`Response`;
  - en `adapters/persistence`, las filas del ORM terminan en `Row` y el resto de
    las clases se llaman `SqlAlchemy*` (`SqlAlchemyOrderRepository`);
  - toda excepción termina en `Error` (`OrderNotFoundError`; ruff `N818` también);
  - las entradas de un caso de uso son `...Command`/`...Query`, no
    `Input`/`Request`/`Dto`/`Params`;
  - ningún valor de `Permission` ni de `ActivityKind` aparece como literal fuera
    de `core/permissions.py` y `core/activity.py`; en las pruebas se usa
    `Permission.X.value` y `ActivityKind.X.value` (`alembic/` queda fuera);
  - en `tests/` no hay `"/api/v1"` literal salvo en `conftest.py`: las rutas se
    arman con `url("orders")` y el prefijo suelto es `API_PREFIX`
    (`from tests.conftest import url`).
- **Contraseñas**: Argon2id con `pwdlib` detrás del puerto `PasswordHasher`
  (`core/security.py`). `verify_and_update` devuelve el hash nuevo cuando el guardado es bcrypt o
  de menor costo, y el caso de uso de acceso lo persiste. Las pruebas bajan el costo con
  `Settings(password_argon2_*)` (`TEST_HASHER`); no uses el costo de producción en ellas.
- **Trazas (OpenTelemetry)**: `core/tracing.py`, apagadas por omisión. Sin
  `OTEL_EXPORTER_OTLP_ENDPOINT` no se importa OpenTelemetry (lo vigila
  `tests/test_tracing.py`). Para activarlas: `OTEL_EXPORTER_OTLP_ENDPOINT=https://<colector>`
  (OTLP/HTTP; `/v1/traces` se agrega solo), `OTEL_EXPORTER_OTLP_HEADERS` si el colector pide
  credencial, `OTEL_SERVICE_NAME` y `OTEL_TRACES_SAMPLER_ARG` (0 a 1). Todo entra por `Settings`,
  nunca por `os.environ` (`TID251`). Las pruebas pasan un exportador en memoria a
  `install_tracing(..., span_processors=[...])`. Cada log lleva `trace_id` y `span_id` dentro de
  una traza. No toca la telemetría propia de plataforma.
- **Entorno**: `os.environ` y `os.getenv` están prohibidos (ruff `TID251`) fuera
  de `core/config.py`, `scripts/` y `chaos/`; la configuración entra por `Settings`.
- **Idioma**: los identificadores van en inglés (variables, funciones, clases,
  argumentos, atributos, constantes, nombres de test, archivos y carpetas de
  `src/`, `tests/`, `scripts/` y `chaos/`, incluidos `tests/load/*.js|sh`); los
  textos para el usuario, los mensajes de error, el `detail` de la API, los
  comentarios, las docstrings, los datos de prueba dentro de strings
  («Lomo saltado») y los commits van en español. Los nombres de test son
  `test_<unidad>_<escenario>` (`test_charge_order_rejects_other_waiters_order`).
  El dominio sigue el glosario del equipo (`boleta` es `sales_receipt`,
  `factura` es `tax_invoice`, `pedido` es `order`, `mesero` es `waiter`…); los
  valores que viajan por la API o la base de datos (`"boleta"`, `"factura"`) no
  cambian. Lo vigila `scripts/check_english_identifiers.py`, que recorre el AST
  y sale con código distinto de 0 si encuentra una palabra española; corre desde
  pytest (`tests/test_english_identifiers.py`). Las excepciones (siglas como
  `ruc`, `igv`, `sunat`, `dni`, `nubefact`, nombres de platos, falsos positivos)
  van en `scripts/english_allowlist.txt`, una palabra por línea. Un campo de un
  modelo Pydantic que sale por la API no se renombra sin un alias
  (`serialization_alias`/`validation_alias`): el contrato HTTP no cambia.
- **Código repetido**: `jscpd` con umbral del 1 % (`.jscpd.json`).

## Huella de carbono

`scripts/measure_carbon.py` mide con CodeCarbon (modo sin conexión) lo que gasta el backend
bajo una carga representativa. Grupo de dependencias aparte (`carbon`), fuera del CI de cada push.

```bash
uv run --group carbon python scripts/measure_carbon.py                    # mixto, 60 s, 5 usuarios
uv run --group carbon python scripts/measure_carbon.py --scenario owner --duration 120
uv run --group carbon python scripts/measure_carbon.py --country PER --workers 8
```

- Levanta uvicorn sobre una SQLite nueva (migrada y sembrada) y la carga con clientes httpx
  que repiten los recorridos de `tests/load`: `waiter` (mesas, carta, pedido, cocina, servido,
  cobro), `owner` (panel de ocho reportes, caja, inventario) y `mixed` (cuatro meseros por
  encargado). `--duration`, `--workers`, `--think-time` y `--scenario` son argumentos.
- `--country` (ISO alfa-3) elige la intensidad de la red. `railway.toml` no fija región: el
  servicio corre en la de Railway por omisión (US West, Oregon), por eso el valor por omisión es
  `USA`. Si el servicio se muda de región, pasa su país.
- Cómo leerlo: `reports/carbon/carbon-<escenario>-<fecha>.json` (en `.gitignore`) trae
  `g_co2e_per_1000_requests` (la cifra para comparar), `kwh_per_1000_requests`, el total de
  energía y emisiones, las peticiones por endpoint y `errors` (respuestas con un estado distinto
  del esperado: algunos 409 al agotarse un plato son normales). `carbon-history.csv` suma una
  fila por corrida. CodeCarbon estima la máquina entera (servidor, clientes de carga y lo demás
  que corra): compara corridas en la misma máquina, no la tomes como cifra absoluta de producción.
- `.github/workflows/carbon.yml` lo corre cada lunes y a mano (`workflow_dispatch`, con
  escenario, duración y país), sube el informe como artefacto y no bloquea PRs.

## Dependencias y versiones

- **Dependabot** (`.github/dependabot.yml`) abre cada lunes PRs agrupados contra
  `develop` (dependencias de producción y de desarrollo por separado, más las
  acciones de GitHub cada mes). Espera 3 días desde que sale una versión antes de
  proponerla. Se revisan y fusionan como cualquier PR: el CI es el mismo.
- **release-please** (`.github/workflows/release.yml`): al fusionar `develop` en
  `main`, abre un PR de versión contra `main` con el `CHANGELOG.md` y el número
  nuevo según los commits convencionales (`feat` sube la menor, `fix` el parche;
  antes de 1.0 un cambio incompatible sube la menor). Al fusionar ese PR crea la
  etiqueta `vX.Y.Z` y la release. No edites a mano la versión ni el changelog.

## Búsqueda de código

Si están instalados, usa CodeGraph y tgrep antes que grep/find o leer archivos
enteros; son más rápidos y cualquier agente o subagente puede usarlos.

- **CodeGraph** (`.codegraph/` en la raíz) para entender código: definiciones,
  quién llama a qué y el impacto de un cambio. MCP `codegraph_explore`, o por
  shell `codegraph explore "<pregunta o símbolos>"`, `codegraph callers <símbolo>`,
  `codegraph impact <símbolo>`. Sin índice: `codegraph init --yes`.
- **tgrep** para texto y regex: MCP `search_code` / `find_files` (con
  `freshness: "current"` para ver ediciones recientes), o por shell
  `tgrep -- "patrón" .`, `tgrep -F -- "literal" .`, `tgrep -l -- "patrón" .`.

Si no están disponibles o no pueden expresar la consulta, usa grep/rg. No
subas `.codegraph/` ni índices de tgrep al repositorio.

## Code Review Rules

Escribe la revisión en español. Formato, lint e imports ya los revisa el CI:
no los comentes. Marca solo lo que cambia el comportamiento o rompe una regla
de esta lista.

### Aislamiento entre restaurantes

- En una petición autenticada, toda consulta de datos de negocio filtra por
  `restaurant_id`, y ese `restaurant_id` sale del principal (el token), nunca
  del cuerpo, la URL ni un parámetro. Marca cualquier consulta o repositorio
  nuevo que no lo haga.
- Excepciones legítimas, donde todavía no hay principal: el acceso
  (`POST /auth/login` busca la cuenta por el correo, que es único en todo el
  sistema) y las tareas en segundo plano, que reciben el `restaurant_id` del
  evento que las disparó. Lo que no vale nunca es tomarlo del cliente.
- La administración del sistema (`/platform/*`, módulo `platform`) es la otra
  excepción: no pertenece a ningún local, así que ahí el restaurante sí viene
  en la URL. La protege el alcance del token (`scope: "platform"`): marca un
  endpoint de plataforma que no exija `CurrentAdminDep`, o uno de restaurante
  que acepte una credencial de plataforma. Sus cuentas viven en
  `platform_admins`, nunca en `users`.
- La vista previa de la plataforma solo entra al local de muestra
  (`restaurants.is_sandbox`). Marca un camino que emita un código o un token de
  vista previa para una cuenta que no sea del local de muestra, que acepte un
  `restaurant_id` o un `user_id` para la vista previa, o que deje de comprobar
  `is_sandbox` en `core/auth.py` para un token con `preview`. Un token de vista
  previa no se renueva.
- Un recurso de otro restaurante responde 404, igual que uno que no existe:
  un 403 delataría que existe. El 403 solo vale cuando quien pregunta ya puede
  ver el recurso y le falta el derecho para esa acción (por ejemplo, un mesero
  que quiere cobrar un pedido activo de otro).

### Roles y permisos

- Los roles son datos de cada restaurante (tabla `roles`, módulo `accounts`):
  un `owner` (Encargado, con todo el catálogo), un `waiter` (Mesero, con
  permisos editables) y los `custom` que cree el local, como un cocinero. El
  código no conoce roles concretos: marca cualquier comparación con un rol,
  su nombre o su `kind` para decidir qué puede hacer alguien, fuera de la
  gestión de roles misma.
- Todo endpoint cuya operación depende de lo que puede hacer la cuenta exige
  un permiso con `require_permission(Permission.X)`; nunca compara el rol. Un
  permiso nuevo va en `core/permissions.py` con su etiqueta y grupo en
  `CATALOG`; el encargado lo recibe solo y el resto de los roles, cuando el
  restaurante se lo da.
- Nadie reparte lo que no tiene: crear o editar un rol, o asignárselo a una
  cuenta, exige tener cada permiso de ese rol, y no se gestiona una cuenta
  cuyo rol tiene permisos que uno no tiene. Marca un camino nuevo que asigne
  permisos o roles sin esa comprobación.
- Los avisos en tiempo real se dirigen a cuentas, a todo el local o a quien
  tenga un permiso (`RealtimeEvent.permissions`), nunca a un rol.
- No necesitan permiso: `POST /auth/login` (público) y lo que toda cuenta
  autenticada puede hacer sobre sí misma o leer de su local, como
  `GET /auth/me`, cambiar la propia contraseña o `GET /restaurant`; esos usan
  `PrincipalDep`. No pidas un permiso ahí.
- Quien no tiene `orders.read_all` (el mesero) cobra y descuenta solo los
  pedidos que tomó; quien lo tiene (el encargado), cualquiera. Esa regla vive
  en el caso de uso, no en el permiso de cobrar: marca un cobro o descuento
  nuevo que no la aplique.

### Arquitectura hexagonal

- `domain`, `ports` y `use_cases` son Python puro: sin FastAPI, SQLAlchemy,
  Pydantic, JWT ni HTTP. Los errores de dominio se traducen a HTTP en
  `adapters/api` (en el router o en un `errors.py` del módulo, como hace
  `orders`); nunca se lanza `HTTPException` desde el dominio ni desde un caso
  de uso.
- Un módulo no importa a otro. Si necesita sus datos, los lee por un puerto
  propio con un adaptador `directories.py`, o se conectan en `main.py` /
  `wiring/`.

### Dinero y concurrencia

- Todo monto va en `Decimal`; marca cualquier `float` en precios, totales,
  pagos, costos o vueltos, y cualquier redondeo que no sea explícito.
- Lo que se cobra o se muestra como importe final (precios, totales, pagos,
  vueltos) tiene dos decimales exactos. Los costos unitarios e intermedios
  conservan su precisión: `unit_cost` de un insumo son soles por gramo,
  mililitro o unidad con seis decimales (`Numeric(14, 6)`). No los redondees
  a dos.
- Lo que lee un registro para después modificarlo (cobrar, cambiar estado,
  mover stock, cerrar caja) lo lee con bloqueo (`for_update`) o comprueba que
  no cambió; si no, dos personas pueden pisarse. Un cobro repetido no debe
  duplicar pagos ni asientos de bitácora.
- El stock nunca se edita a mano: todo cambio es un movimiento.

### Trazabilidad e IA

- Las acciones sensibles (sesiones, personal, menú, pedidos, cobros, stock)
  quedan en la bitácora; los registros de bitácora y de `ai_decisions` no se
  editan ni se borran.
- La IA sugiere y nunca bloquea: si falla, tarda o no hay clave, deciden las
  reglas. Marca cualquier camino donde tomar un pedido, cobrar o mover stock
  espere a la IA o falle por ella.

### Migraciones y pruebas

- Todo cambio de esquema trae su migración de Alembic, reversible, y no
  modifica una migración ya publicada.
- Las migraciones nuevas se llaman `NNNN_snake_case_en_ingles.py` (desde la 0023;
  las publicadas conservan su nombre). Lo vigila `tests/test_english_identifiers.py`.
- Una regla de negocio nueva trae su prueba. Si además expone una operación
  con permiso, prueba quién puede y quién recibe 403/404; si lee o escribe
  datos de un restaurante, prueba que otro restaurante no los ve ni los toca.
  Una regla de dominio pura (una transición, un cálculo) no necesita esas dos.
