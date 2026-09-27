// Los recorridos de cada perfil: mesero, cocina, encargado y pantallas con el
// canal de avisos (SSE) abierto. Los tiempos de espera imitan un turno real:
// el mesero mira las mesas, elige platos, vuelve cuando la cocina termina y
// cobra; el encargado revisa el panel, la caja y el inventario.

import { check } from 'k6';
import sse from 'k6/x/sse';
import { Counter, Trend } from 'k6/metrics';
import {
  API,
  cocinero,
  encargado,
  espera,
  etiquetaMesa,
  hoyMenos,
  lote,
  mesero,
  N_MESAS,
  pedir,
  sesion,
  vu,
} from './comun.js';

const pedidosCobrados = new Counter('pedidos_cobrados');
const ciclosIncompletos = new Counter('ciclos_incompletos');
const avisosSse = new Counter('avisos_sse');
const esperaCocina = new Trend('espera_cocina', true);

function elegirPlatos(menu, cuantos) {
  const disponibles = [];
  for (const categoria of menu.categories || []) {
    for (const plato of categoria.items || []) {
      if (plato.is_active && plato.is_available && !plato.out_of_stock && !(plato.modifier_groups || []).some((g) => g.min_selected > 0)) {
        disponibles.push(plato.id);
      }
    }
  }
  const elegidos = [];
  for (let i = 0; i < cuantos && disponibles.length; i++) {
    const id = disponibles[Math.floor(Math.random() * disponibles.length)];
    elegidos.push({ menu_item_id: id, quantity: 1 + Math.floor(Math.random() * 2) });
  }
  return elegidos;
}

function idUnico() {
  return `k6-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Mesero: mesas → menú → abrir pedido → agregar platos → enviar a cocina →
// (la cocina lo marca listo) → servido → cobro.
export function flujoMesero(datos) {
  const yo = vu();
  const token = sesion(mesero(yo));

  const mesas = pedir('GET', '/tables', 'GET /tables', { token });
  espera(2, 5);

  const menu = pedir('GET', '/menu', 'GET /menu', { token });
  if (!menu.ok) {
    ciclosIncompletos.add(1);
    return;
  }
  espera(5, 12);

  // Cada mesero virtual tiene su mesa propia (C01…C60) mientras alcancen; si
  // está ocupada o no hay mesa para él, el pedido es para llevar.
  let cuerpo = { type: 'takeaway', customer_name: `Cliente ${yo}` };
  if (mesas.ok && yo < N_MESAS) {
    const mia = mesas.res.json().find((m) => m.label === etiquetaMesa(yo));
    if (mia && mia.status === 'free') {
      cuerpo = { type: 'dine_in', table_id: mia.id };
    }
  }
  cuerpo.items = elegirPlatos(menu.res.json(), 2);
  cuerpo.client_request_id = idUnico();
  const abierto = pedir('POST', '/orders', 'POST /orders', { token, cuerpo, estado: 201 });
  if (!abierto.ok) {
    ciclosIncompletos.add(1);
    return;
  }
  const id = abierto.res.json('id');
  espera(3, 8);

  pedir('POST', `/orders/${id}/items`, 'POST /orders/{id}/items', {
    token,
    cuerpo: { items: elegirPlatos(menu.res.json(), 1) },
  });
  espera(1, 3);

  const enviado = pedir('POST', `/orders/${id}/send`, 'POST /orders/{id}/send', { token });
  if (!enviado.ok) {
    ciclosIncompletos.add(1);
    return;
  }

  // La cocina: una cuenta de cocinero marca «listo» este pedido cuando termina.
  // Se hace desde este mismo usuario virtual para que cada pedido tenga un
  // solo «listo» y dos cocineros no se disputen el mismo.
  const inicioCocina = Date.now();
  espera(8, 15);
  const listo = pedir('POST', `/orders/${id}/ready`, 'POST /orders/{id}/ready', {
    token: datos.tokenCocina,
  });
  esperaCocina.add(Date.now() - inicioCocina);
  if (!listo.ok) {
    ciclosIncompletos.add(1);
    return;
  }
  espera(2, 4);

  // El mesero ve el aviso, abre el pedido y lo lleva a la mesa.
  pedir('GET', `/orders/${id}`, 'GET /orders/{id}', { token });
  espera(1, 3);
  const servido = pedir('POST', `/orders/${id}/served`, 'POST /orders/{id}/served', { token });
  if (!servido.ok) {
    ciclosIncompletos.add(1);
    return;
  }
  espera(8, 15);

  // Cobro en efectivo o por billetera, con el saldo que se ve en pantalla.
  const saldo = servido.res.json('balance');
  const medio = Math.random() < 0.5 ? 'cash' : 'yape';
  const pago = { payment_method: medio, expected_balance: saldo };
  if (medio === 'cash') {
    pago.amount_received = String(Math.ceil(Number(saldo) / 10) * 10 + 10);
  }
  const cobro = pedir('POST', `/orders/${id}/charge`, 'POST /orders/{id}/charge', {
    token,
    cuerpo: pago,
  });
  const pagado = cobro.ok && check(cobro.res, { 'pedido pagado': (r) => r.json('status') === 'paid' });
  if (pagado) {
    pedidosCobrados.add(1);
  } else {
    ciclosIncompletos.add(1);
  }
  espera(3, 6);

  if (Math.random() < 0.3) {
    pedir('GET', '/orders/active', 'GET /orders/active', { token });
    espera(2, 4);
  }
}

// Cocina: el tablero se refresca cada pocos segundos (en la aplicación lo hace
// al llegar un aviso; aquí se refresca por tiempo para no depender del SSE).
export function flujoCocina() {
  const token = sesion(cocinero(vu()));
  pedir('GET', '/orders/active', 'GET /orders/active', { token });
  espera(4, 8);
}

// Encargado: pedidos en curso, panel de indicadores (ocho reportes a la vez,
// como la pantalla), caja e inventario.
export function flujoEncargado() {
  const token = sesion(encargado(vu()));
  const rango = `date_from=${hoyMenos(29)}&date_to=${hoyMenos(0)}`;

  pedir('GET', '/orders/active', 'GET /orders/active', { token });
  espera(4, 8);

  lote(token, [
    [`/insights/summary?${rango}`, 'GET /insights/summary'],
    [`/insights/sales/daily?${rango}`, 'GET /insights/sales/daily'],
    [`/insights/sales/hourly?${rango}`, 'GET /insights/sales/hourly'],
    [`/insights/payments?${rango}`, 'GET /insights/payments'],
    [`/insights/waiters?${rango}`, 'GET /insights/waiters'],
    [`/insights/dishes/top?${rango}&limit=10`, 'GET /insights/dishes/top'],
    [`/insights/dishes/margins?${rango}`, 'GET /insights/dishes/margins'],
    [`/insights/waste?${rango}`, 'GET /insights/waste'],
  ]);
  espera(15, 25);

  lote(token, [
    ['/cash/current', 'GET /cash/current'],
    ['/cash/sessions?limit=10&offset=0', 'GET /cash/sessions'],
  ]);
  espera(10, 20);

  lote(token, [
    ['/inventory/ingredients', 'GET /inventory/ingredients'],
    ['/inventory/alerts/low-stock', 'GET /inventory/alerts/low-stock'],
  ]);
  espera(10, 20);
}

// Pantallas con el canal de avisos abierto (tablero de cocina, celular del
// encargado). La conexión dura lo que dura el escenario y cuenta los avisos.
export function flujoAvisos() {
  const yo = vu();
  const email = yo % 2 === 0 ? encargado(yo) : cocinero(yo);
  const token = sesion(email);
  const duracion = __ENV.SSE_DURACION || '5m';
  let abierta = false;
  let listo = false;
  const res = sse.open(
    `${API}/events`,
    {
      headers: { Authorization: `Bearer ${token}` },
      tags: { name: 'GET /events (SSE)', canal: 'sse' },
      timeout: duracion,
    },
    (cliente) => {
      cliente.on('open', () => {
        abierta = true;
      });
      cliente.on('event', (evento) => {
        if (evento.name === 'ready') {
          listo = true;
        } else {
          avisosSse.add(1);
        }
      });
      // El único error esperado es el fin del plazo de la conexión.
      cliente.on('error', (e) => {
        const texto = String(e.error());
        if (!texto.includes('deadline exceeded')) {
          console.warn(`SSE: ${texto}`);
        }
        cliente.close();
      });
    },
  );
  check(res, {
    'SSE → 200': (r) => r && r.status === 200,
    'SSE abierta y con «ready»': () => abierta && listo,
  });
}
