// Prepara el local de la semilla para las pruebas de carga. Idempotente.
//
//   k6-sse run tests/load/prepare.js
//
// Requiere la base migrada y sembrada (`seed_dev.py` y `seed_history.py`).
// Crea, con la cuenta del encargado de la semilla:
// - 40 meseros, 6 cocineros y 6 encargados `carga.*@resthub.dev` con la
//   contraseña de la semilla, para que cada usuario virtual use su cuenta;
// - 60 mesas `C01`…`C60`, una por mesero virtual;
// - una compra grande de cada insumo, para que ningún plato se agote en medio
//   de la prueba;
// - la caja abierta, si no lo está.
// Además cancela los pedidos activos que haya dejado una corrida interrumpida
// en esas mesas, para empezar con todas libres.

import { check, fail } from 'k6';
import http from 'k6/http';
import {
  API,
  cookEmail,
  ownerEmail,
  tableLabel,
  waiterEmail,
  N_KITCHEN,
  N_OWNERS,
  N_TABLES,
  N_WAITERS,
  PASSWORD,
} from './lib/common.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };

function call(method, path, token, payload) {
  const res = http.request(method, `${API}${path}`, payload === undefined ? null : JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    tags: { channel: 'preparacion' },
  });
  if (res.status >= 300) {
    console.error(`${method} ${path} → ${res.status}: ${res.body}`);
  }
  return res;
}

export default function () {
  const login = http.post(`${API}/auth/login`, JSON.stringify({ email: 'admin@resthub.dev', password: PASSWORD }), {
    headers: { 'Content-Type': 'application/json' },
  });
  if (login.status !== 200) {
    fail(`no entra el encargado de la semilla: ${login.status}`);
  }
  const token = login.json('access_token');

  const roles = call('GET', '/roles', token).json();
  const findRole = (kind, name) => roles.find((r) => r.kind === kind && (!name || r.name === name)).id;
  const waiterRole = findRole('waiter');
  const kitchenRole = findRole('custom', 'Cocinero');
  const ownerRole = findRole('owner');

  // Cuentas.
  const existing = new Set();
  for (let offset = 0; ; offset += 100) {
    const pageData = call('GET', `/staff?search=carga.&limit=100&offset=${offset}`, token).json();
    pageData.items.forEach((c) => existing.add(c.email));
    if (pageData.items.length < 100) break;
  }
  const accounts = [];
  for (let i = 0; i < N_WAITERS; i++) accounts.push([waiterEmail(i), `Mesero de carga ${i + 1}`, waiterRole]);
  for (let i = 0; i < N_KITCHEN; i++) accounts.push([cookEmail(i), `Cocinero de carga ${i + 1}`, kitchenRole]);
  for (let i = 0; i < N_OWNERS; i++) accounts.push([ownerEmail(i), `Encargado de carga ${i + 1}`, ownerRole]);
  let created = 0;
  for (const [email, name, roleId] of accounts) {
    if (existing.has(email)) continue;
    const r = call('POST', '/staff', token, { email, full_name: name, role_id: roleId, password: PASSWORD });
    check(r, { 'cuenta creada': (x) => x.status === 201 });
    created++;
  }

  // Mesas.
  let tables = call('GET', '/tables', token).json();
  const labels = new Set(tables.map((m) => m.label));
  let newTables = 0;
  for (let i = 0; i < N_TABLES; i++) {
    if (labels.has(tableLabel(i))) continue;
    const r = call('POST', '/tables', token, { label: tableLabel(i) });
    check(r, { 'mesa creada': (x) => x.status === 201 });
    newTables++;
  }

  // Pedidos que quedaron abiertos por una corrida interrumpida.
  const active = call('GET', '/orders/active', token).json();
  let cancelled = 0;
  for (const p of active) {
    if (p.waiter_name && p.waiter_name.includes('de carga')) {
      const r = call('POST', `/orders/${p.id}/cancel`, token, { reason: 'Corrida de carga interrumpida' });
      check(r, { 'pedido cancelado': (x) => x.status === 200 });
      cancelled++;
    }
  }

  // Stock de sobra.
  const ingredients = call('GET', '/inventory/ingredients', token).json();
  const ingredientList = Array.isArray(ingredients) ? ingredients : ingredients.items;
  for (const ingredient of ingredientList) {
    const r = call('POST', '/inventory/purchases', token, {
      ingredient_id: ingredient.id,
      quantity: '500000',
      unit_cost: ingredient.unit_cost || '0.01',
      reason: 'Stock para la prueba de carga',
    });
    check(r, { 'compra registrada': (x) => x.status === 201 || x.status === 200 });
  }

  // Caja.
  const cashSession = call('GET', '/cash/current', token).json();
  if (!cashSession.is_open) {
    const r = call('POST', '/cash/open', token, { opening_amount: '200.00', notes: 'Prueba de carga' });
    check(r, { 'caja abierta': (x) => x.status === 201 || x.status === 200 });
  }

  tables = call('GET', '/tables', token).json();
  console.log(
    `cuentas nuevas ${created} (de ${accounts.length}) · mesas nuevas ${newTables} · ` +
      `pedidos cancelados ${cancelled} · insumos repuestos ${ingredientList.length} · ` +
      `mesas libres ${tables.filter((m) => m.status === 'free').length}/${tables.length}`,
  );
}
