// Prepara el local de la semilla para las pruebas de carga. Idempotente.
//
//   k6-sse run tests/load/preparar.js
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
  cocinero,
  encargado,
  etiquetaMesa,
  mesero,
  N_COCINA,
  N_ENCARGADOS,
  N_MESAS,
  N_MESEROS,
  PASSWORD,
} from './lib/comun.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };

function llamar(metodo, ruta, token, cuerpo) {
  const res = http.request(metodo, `${API}${ruta}`, cuerpo === undefined ? null : JSON.stringify(cuerpo), {
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    tags: { canal: 'preparacion' },
  });
  if (res.status >= 300) {
    console.error(`${metodo} ${ruta} → ${res.status}: ${res.body}`);
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

  const roles = llamar('GET', '/roles', token).json();
  const rol = (kind, nombre) => roles.find((r) => r.kind === kind && (!nombre || r.name === nombre)).id;
  const rolMesero = rol('waiter');
  const rolCocina = rol('custom', 'Cocinero');
  const rolEncargado = rol('owner');

  // Cuentas.
  const existentes = new Set();
  for (let offset = 0; ; offset += 100) {
    const pagina = llamar('GET', `/staff?search=carga.&limit=100&offset=${offset}`, token).json();
    pagina.items.forEach((c) => existentes.add(c.email));
    if (pagina.items.length < 100) break;
  }
  const cuentas = [];
  for (let i = 0; i < N_MESEROS; i++) cuentas.push([mesero(i), `Mesero de carga ${i + 1}`, rolMesero]);
  for (let i = 0; i < N_COCINA; i++) cuentas.push([cocinero(i), `Cocinero de carga ${i + 1}`, rolCocina]);
  for (let i = 0; i < N_ENCARGADOS; i++) cuentas.push([encargado(i), `Encargado de carga ${i + 1}`, rolEncargado]);
  let creadas = 0;
  for (const [email, nombre, roleId] of cuentas) {
    if (existentes.has(email)) continue;
    const r = llamar('POST', '/staff', token, { email, full_name: nombre, role_id: roleId, password: PASSWORD });
    check(r, { 'cuenta creada': (x) => x.status === 201 });
    creadas++;
  }

  // Mesas.
  let mesas = llamar('GET', '/tables', token).json();
  const etiquetas = new Set(mesas.map((m) => m.label));
  let mesasNuevas = 0;
  for (let i = 0; i < N_MESAS; i++) {
    if (etiquetas.has(etiquetaMesa(i))) continue;
    const r = llamar('POST', '/tables', token, { label: etiquetaMesa(i) });
    check(r, { 'mesa creada': (x) => x.status === 201 });
    mesasNuevas++;
  }

  // Pedidos que quedaron abiertos por una corrida interrumpida.
  const activos = llamar('GET', '/orders/active', token).json();
  let cancelados = 0;
  for (const p of activos) {
    if (p.waiter_name && p.waiter_name.includes('de carga')) {
      const r = llamar('POST', `/orders/${p.id}/cancel`, token, { reason: 'Corrida de carga interrumpida' });
      check(r, { 'pedido cancelado': (x) => x.status === 200 });
      cancelados++;
    }
  }

  // Stock de sobra.
  const insumos = llamar('GET', '/inventory/ingredients', token).json();
  const lista = Array.isArray(insumos) ? insumos : insumos.items;
  for (const insumo of lista) {
    const r = llamar('POST', '/inventory/purchases', token, {
      ingredient_id: insumo.id,
      quantity: '500000',
      unit_cost: insumo.unit_cost || '0.01',
      reason: 'Stock para la prueba de carga',
    });
    check(r, { 'compra registrada': (x) => x.status === 201 || x.status === 200 });
  }

  // Caja.
  const caja = llamar('GET', '/cash/current', token).json();
  if (!caja.is_open) {
    const r = llamar('POST', '/cash/open', token, { opening_amount: '200.00', notes: 'Prueba de carga' });
    check(r, { 'caja abierta': (x) => x.status === 201 || x.status === 200 });
  }

  mesas = llamar('GET', '/tables', token).json();
  console.log(
    `cuentas nuevas ${creadas} (de ${cuentas.length}) · mesas nuevas ${mesasNuevas} · ` +
      `pedidos cancelados ${cancelados} · insumos repuestos ${lista.length} · ` +
      `mesas libres ${mesas.filter((m) => m.status === 'free').length}/${mesas.length}`,
  );
}
