// Los recorridos de cada perfil: mesero, cocina, encargado y pantallas con el
// canal de avisos (SSE) abierto. Los tiempos de espera imitan un turno real:
// el mesero mira las mesas, elige platos, vuelve cuando la cocina termina y
// cobra; el encargado revisa el panel, la caja y el inventario.

import { check } from 'k6';
import sse from 'k6/x/sse';
import { Counter, Trend } from 'k6/metrics';
import {
  API,
  cookEmail,
  ownerEmail,
  pause,
  tableLabel,
  todayMinus,
  batchGet,
  waiterEmail,
  N_TABLES,
  apiCall,
  session,
  vu,
} from './common.js';

const chargedOrders = new Counter('orders_charged');
const incompleteCycles = new Counter('incomplete_cycles');
const sseNotices = new Counter('sse_notices');
const kitchenWait = new Trend('kitchen_wait', true);

function pickDishes(menu, count) {
  const available = [];
  for (const category of menu.categories || []) {
    for (const dish of category.items || []) {
      if (dish.is_active && dish.is_available && !dish.out_of_stock && !(dish.modifier_groups || []).some((g) => g.min_selected > 0)) {
        available.push(dish.id);
      }
    }
  }
  const picked = [];
  for (let i = 0; i < count && available.length; i++) {
    const id = available[Math.floor(Math.random() * available.length)];
    picked.push({ menu_item_id: id, quantity: 1 + Math.floor(Math.random() * 2) });
  }
  return picked;
}

function uniqueId() {
  return `k6-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Mesero: mesas → menú → abrir pedido → agregar platos → enviar a cocina →
// (la cocina lo marca listo) → servido → cobro.
export function waiterFlow(setupData) {
  const me = vu();
  const token = session(waiterEmail(me));

  const tables = apiCall('GET', '/tables', 'GET /tables', { token });
  pause(2, 5);

  const menu = apiCall('GET', '/menu', 'GET /menu', { token });
  if (!menu.ok) {
    incompleteCycles.add(1);
    return;
  }
  pause(5, 12);

  // Cada mesero virtual tiene su mesa propia (C01…C60) mientras alcancen; si
  // está ocupada o no hay mesa para él, el pedido es para llevar.
  let payload = { type: 'takeaway', customer_name: `Cliente ${me}` };
  if (tables.ok && me < N_TABLES) {
    const mine = tables.res.json().find((m) => m.label === tableLabel(me));
    if (mine && mine.status === 'free') {
      payload = { type: 'dine_in', table_id: mine.id };
    }
  }
  payload.items = pickDishes(menu.res.json(), 2);
  payload.client_request_id = uniqueId();
  const opened = apiCall('POST', '/orders', 'POST /orders', { token, payload, expectedStatus: 201 });
  if (!opened.ok) {
    incompleteCycles.add(1);
    return;
  }
  const id = opened.res.json('id');
  pause(3, 8);

  apiCall('POST', `/orders/${id}/items`, 'POST /orders/{id}/items', {
    token,
    payload: { items: pickDishes(menu.res.json(), 1) },
  });
  pause(1, 3);

  const sent = apiCall('POST', `/orders/${id}/send`, 'POST /orders/{id}/send', { token });
  if (!sent.ok) {
    incompleteCycles.add(1);
    return;
  }

  // La cocina: una cuenta de cocinero marca «listo» este pedido cuando termina.
  // Se hace desde este mismo usuario virtual para que cada pedido tenga un
  // solo «listo» y dos cocineros no se disputen el mismo.
  const kitchenStart = Date.now();
  pause(8, 15);
  const ready = apiCall('POST', `/orders/${id}/ready`, 'POST /orders/{id}/ready', {
    token: setupData.kitchenToken,
  });
  kitchenWait.add(Date.now() - kitchenStart);
  if (!ready.ok) {
    incompleteCycles.add(1);
    return;
  }
  pause(2, 4);

  // El mesero ve el aviso, abre el pedido y lo lleva a la mesa.
  apiCall('GET', `/orders/${id}`, 'GET /orders/{id}', { token });
  pause(1, 3);
  const served = apiCall('POST', `/orders/${id}/served`, 'POST /orders/{id}/served', { token });
  if (!served.ok) {
    incompleteCycles.add(1);
    return;
  }
  pause(8, 15);

  // Cobro en efectivo o por billetera, con el saldo que se ve en pantalla.
  const balance = served.res.json('balance');
  const method = Math.random() < 0.5 ? 'cash' : 'yape';
  const payment = { payment_method: method, expected_balance: balance };
  if (method === 'cash') {
    payment.amount_received = String(Math.ceil(Number(balance) / 10) * 10 + 10);
  }
  const charge = apiCall('POST', `/orders/${id}/charge`, 'POST /orders/{id}/charge', {
    token,
    payload: payment,
  });
  const paid = charge.ok && check(charge.res, { 'pedido pagado': (r) => r.json('status') === 'paid' });
  if (paid) {
    chargedOrders.add(1);
  } else {
    incompleteCycles.add(1);
  }
  pause(3, 6);

  if (Math.random() < 0.3) {
    apiCall('GET', '/orders/active', 'GET /orders/active', { token });
    pause(2, 4);
  }
}

// Cocina: el tablero se refresca cada pocos segundos (en la aplicación lo hace
// al llegar un aviso; aquí se refresca por tiempo para no depender del SSE).
export function kitchenFlow() {
  const token = session(cookEmail(vu()));
  apiCall('GET', '/orders/active', 'GET /orders/active', { token });
  pause(4, 8);
}

// Encargado: pedidos en curso, panel de indicadores (ocho reportes a la vez,
// como la pantalla), caja e inventario.
export function ownerFlow() {
  const token = session(ownerEmail(vu()));
  const range = `date_from=${todayMinus(29)}&date_to=${todayMinus(0)}`;

  apiCall('GET', '/orders/active', 'GET /orders/active', { token });
  pause(4, 8);

  batchGet(token, [
    [`/insights/summary?${range}`, 'GET /insights/summary'],
    [`/insights/sales/daily?${range}`, 'GET /insights/sales/daily'],
    [`/insights/sales/hourly?${range}`, 'GET /insights/sales/hourly'],
    [`/insights/payments?${range}`, 'GET /insights/payments'],
    [`/insights/waiters?${range}`, 'GET /insights/waiters'],
    [`/insights/dishes/top?${range}&limit=10`, 'GET /insights/dishes/top'],
    [`/insights/dishes/margins?${range}`, 'GET /insights/dishes/margins'],
    [`/insights/waste?${range}`, 'GET /insights/waste'],
  ]);
  pause(15, 25);

  batchGet(token, [
    ['/cash/current', 'GET /cash/current'],
    ['/cash/sessions?limit=10&offset=0', 'GET /cash/sessions'],
  ]);
  pause(10, 20);

  batchGet(token, [
    ['/inventory/ingredients', 'GET /inventory/ingredients'],
    ['/inventory/alerts/low-stock', 'GET /inventory/alerts/low-stock'],
  ]);
  pause(10, 20);
}

// Pantallas con el canal de avisos abierto (tablero de cocina, celular del
// encargado). La conexión dura lo que dura el escenario y cuenta los avisos.
export function noticesFlow() {
  const me = vu();
  const email = me % 2 === 0 ? ownerEmail(me) : cookEmail(me);
  const token = session(email);
  const duration = __ENV.SSE_DURATION || '5m';
  let isOpen = false;
  let ready = false;
  const res = sse.open(
    `${API}/events`,
    {
      headers: { Authorization: `Bearer ${token}` },
      tags: { name: 'GET /events (SSE)', channel: 'sse' },
      timeout: duration,
    },
    (client) => {
      client.on('open', () => {
        isOpen = true;
      });
      client.on('event', (evt) => {
        if (evt.name === 'ready') {
          ready = true;
        } else {
          sseNotices.add(1);
        }
      });
      // El único error esperado es el fin del plazo de la conexión.
      client.on('error', (e) => {
        const text = String(e.error());
        if (!text.includes('deadline exceeded')) {
          console.warn(`SSE: ${text}`);
        }
        client.close();
      });
    },
  );
  check(res, {
    'SSE → 200': (r) => r && r.status === 200,
    'SSE abierta y con «ready»': () => isOpen && ready,
  });
}
