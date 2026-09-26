# RestHub — backend

API de FastAPI para restaurantes pequeños: pedidos, cobro, inventario, BI y
decisiones con IA. Multi-restaurante. Arquitectura, módulos y endpoints están
en el `README.md`; esto es lo que un agente necesita además.

## Verificación

```bash
uv sync --locked --group dev
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest -q
```

Commits en Conventional Commits en español (`feat(pedidos): …`). El hook
`.githooks/commit-msg` comprueba la forma, que el resumen no pase de 100
caracteres y que no haya líneas `Co-authored-by`; el español es convención del
equipo, no lo comprueba el hook.

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
- Una regla de negocio nueva trae su prueba. Si además expone una operación
  con permiso, prueba quién puede y quién recibe 403/404; si lee o escribe
  datos de un restaurante, prueba que otro restaurante no los ve ni los toca.
  Una regla de dominio pura (una transición, un cálculo) no necesita esas dos.
