// Utilidades compartidas por los escenarios de carga de k6.
//
// Solo apuntan a un backend local: `BASE_URL` por omisión es el puerto 8204 de
// esta máquina. No se lanzan contra Railway ni contra ningún entorno
// desplegado.

import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import exec from 'k6/execution';

export const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8204').replace(/\/$/, '');
export const API = `${BASE_URL}/api/v1`;
export const PASSWORD = __ENV.CARGA_PASSWORD || 'resthub123';

// Multiplicador de los tiempos de espera. 1 = ritmo realista de un turno; el
// escenario de estrés puede bajarlo para buscar el punto de quiebre con menos
// usuarios virtuales.
export const ESPERA = Number(__ENV.ESPERA || '1');

// Cuentas que crea `preparar.js`. Todas del local de la semilla y con la
// contraseña de la semilla: el límite de intentos (cinco fallos) nunca se
// activa porque siempre se entra con credenciales correctas.
export const N_MESEROS = 40;
export const N_COCINA = 6;
export const N_ENCARGADOS = 6;
export const N_MESAS = 60;

export const mesero = (i) => `carga.mesero${String((i % N_MESEROS) + 1).padStart(2, '0')}@resthub.dev`;
export const cocinero = (i) => `carga.cocina${String((i % N_COCINA) + 1).padStart(2, '0')}@resthub.dev`;
export const encargado = (i) => `carga.encargado${String((i % N_ENCARGADOS) + 1).padStart(2, '0')}@resthub.dev`;
export const etiquetaMesa = (i) => `C${String(i + 1).padStart(2, '0')}`;

// Cada endpoint que tocan los escenarios, con su tipo. El tipo define la meta:
// lecturas p95 <= 100 ms, escrituras p95 <= 200 ms. El acceso va aparte porque
// su costo es bcrypt a propósito.
export const ENDPOINTS = {
  'POST /auth/login': 'acceso',
  'GET /tables': 'lectura',
  'GET /menu': 'lectura',
  'GET /orders/active': 'lectura',
  'GET /orders/{id}': 'lectura',
  'POST /orders': 'escritura',
  'POST /orders/{id}/items': 'escritura',
  'POST /orders/{id}/send': 'escritura',
  'POST /orders/{id}/ready': 'escritura',
  'POST /orders/{id}/served': 'escritura',
  'POST /orders/{id}/charge': 'escritura',
  'GET /insights/summary': 'lectura',
  'GET /insights/sales/daily': 'lectura',
  'GET /insights/sales/hourly': 'lectura',
  'GET /insights/payments': 'lectura',
  'GET /insights/waiters': 'lectura',
  'GET /insights/dishes/top': 'lectura',
  'GET /insights/dishes/margins': 'lectura',
  'GET /insights/waste': 'lectura',
  'GET /cash/current': 'lectura',
  'GET /cash/sessions': 'lectura',
  'GET /inventory/ingredients': 'lectura',
  'GET /inventory/alerts/low-stock': 'lectura',
};

export function etiquetas(endpoint) {
  const tipo = ENDPOINTS[endpoint];
  if (!tipo) {
    fail(`endpoint sin registrar: ${endpoint}`);
  }
  return { name: endpoint, endpoint, tipo, canal: 'api' };
}

export function espera(min, max) {
  sleep((min + Math.random() * (max - min)) * ESPERA);
}

function cabeceras(token) {
  const h = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (token) {
    h.Authorization = `Bearer ${token}`;
  }
  return h;
}

// Una petición al API con sus etiquetas y su comprobación de estado.
export function pedir(metodo, ruta, endpoint, { token, cuerpo, estado = 200, params } = {}) {
  const opciones = { headers: cabeceras(token), tags: etiquetas(endpoint), timeout: '30s' };
  const url = `${API}${ruta}`;
  const body = cuerpo === undefined ? null : JSON.stringify(cuerpo);
  const res = http.request(metodo, url, body, Object.assign(opciones, params || {}));
  const ok = check(
    res,
    { [`${endpoint} → ${estado}`]: (r) => r.status === estado },
    { endpoint },
  );
  if (!ok && res.status !== 0) {
    console.warn(`${endpoint} respondió ${res.status}: ${String(res.body).slice(0, 200)}`);
  }
  return { res, ok };
}

// Lote de lecturas en paralelo, como las que hace la pantalla del panel.
export function lote(token, pedidos) {
  const reqs = pedidos.map(([ruta, endpoint]) => ({
    method: 'GET',
    url: `${API}${ruta}`,
    params: { headers: cabeceras(token), tags: etiquetas(endpoint), timeout: '30s' },
  }));
  const respuestas = http.batch(reqs);
  respuestas.forEach((r, i) => {
    const endpoint = pedidos[i][1];
    check(r, { [`${endpoint} → 200`]: (x) => x.status === 200 }, { endpoint });
  });
  return respuestas;
}

export function entrar(email) {
  const { res, ok } = pedir('POST', '/auth/login', 'POST /auth/login', {
    cuerpo: { email, password: PASSWORD },
  });
  if (!ok) {
    fail(`no se pudo entrar como ${email}: ${res.status}`);
  }
  return res.json('access_token');
}

// Un token por usuario virtual: se entra en la primera iteración y se
// reutiliza en las siguientes, como un celular que mantiene la sesión.
const sesiones = {};
export function sesion(email) {
  if (!sesiones[email]) {
    sesiones[email] = entrar(email);
  }
  return sesiones[email];
}

export function vu() {
  return exec.vu.idInTest - 1;
}

// Fecha del local (America/Lima, UTC-5 sin horario de verano) menos `dias`.
export function hoyMenos(dias) {
  const d = new Date(Date.now() - 5 * 3600000 - dias * 86400000);
  return d.toISOString().slice(0, 10);
}
