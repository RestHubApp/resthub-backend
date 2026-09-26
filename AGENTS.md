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

Commits en Conventional Commits en español (`feat(pedidos): …`), sin líneas
`Co-authored-by`: el hook `.githooks/commit-msg` lo exige.

## Code Review Rules

Escribe la revisión en español. Formato, lint e imports ya los revisa el CI:
no los comentes. Marca solo lo que cambia el comportamiento o rompe una regla
de esta lista.

### Aislamiento entre restaurantes

- Toda consulta de datos de negocio filtra por `restaurant_id`, y ese
  `restaurant_id` sale del principal (el token), nunca del cuerpo, la URL ni un
  parámetro. Marca cualquier consulta o repositorio nuevo que no lo haga.
- Un recurso de otro restaurante responde 404, igual que uno que no existe:
  un 403 delataría que existe. El 403 solo vale cuando quien pregunta ya puede
  ver el recurso y le falta el derecho para esa acción (por ejemplo, un mesero
  que quiere cobrar un pedido activo de otro).

### Roles y permisos

- Hay dos roles fijos y nada más: `admin` (encargado, que además ayuda en
  cocina) y `waiter` (mesero, que además hace de cajero). No hay rol de cocina
  ni de cajero. Marca cualquier rol nuevo.
- Cada endpoint exige un permiso con `require_permission(Permission.X)`; nunca
  compara el rol. Un permiso nuevo va en `core/permissions.py` con su etiqueta
  y grupo en `CATALOG`.
- El mesero cobra (y descuenta) solo los pedidos que tomó; el encargado,
  cualquiera. Esa regla vive en el caso de uso, no en el permiso: marca un
  cobro o descuento nuevo que no la aplique.

### Arquitectura hexagonal

- `domain`, `ports` y `use_cases` son Python puro: sin FastAPI, SQLAlchemy,
  Pydantic, JWT ni HTTP. Los errores de dominio se traducen a HTTP en
  `adapters/api/errors.py`, no se lanzan `HTTPException` desde el dominio.
- Un módulo no importa a otro. Si necesita sus datos, los lee por un puerto
  propio con un adaptador `directories.py`, o se conectan en `main.py` /
  `wiring/`.

### Dinero y concurrencia

- Montos en soles con `Decimal` y dos decimales exactos. Marca cualquier
  `float` en precios, totales, pagos, costos o vueltos, y cualquier redondeo
  que no sea explícito.
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
- Una regla de negocio nueva trae su prueba, incluida la de permisos (quién
  puede y quién recibe 403/404) y la de aislamiento entre restaurantes.
