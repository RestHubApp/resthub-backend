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
export const PASSWORD = __ENV.LOAD_PASSWORD || 'resthub123';

// Multiplicador de los tiempos de espera. 1 = ritmo realista de un turno; el
// escenario de estrés puede bajarlo para buscar el punto de quiebre con menos
// usuarios virtuales.
export const WAIT_FACTOR = Number(__ENV.THINK_TIME || '1');

// Cuentas que crea `prepare.js`. Todas del local de la semilla y con la
// contraseña de la semilla: el límite de intentos (cinco fallos) nunca se
// activa porque siempre se entra con credenciales correctas.
export const N_WAITERS = 40;
export const N_KITCHEN = 6;
export const N_OWNERS = 6;
export const N_TABLES = 60;

export const waiterEmail = (i) => `carga.mesero${String((i % N_WAITERS) + 1).padStart(2, '0')}@resthub.dev`;
export const cookEmail = (i) => `carga.cocina${String((i % N_KITCHEN) + 1).padStart(2, '0')}@resthub.dev`;
export const ownerEmail = (i) => `carga.encargado${String((i % N_OWNERS) + 1).padStart(2, '0')}@resthub.dev`;
export const tableLabel = (i) => `C${String(i + 1).padStart(2, '0')}`;

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

export function endpointTags(endpoint) {
  const kind = ENDPOINTS[endpoint];
  if (!kind) {
    fail(`endpoint sin registrar: ${endpoint}`);
  }
  return { name: endpoint, endpoint, kind, channel: 'api' };
}

export function pause(min, max) {
  sleep((min + Math.random() * (max - min)) * WAIT_FACTOR);
}

function buildHeaders(token) {
  const h = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (token) {
    h.Authorization = `Bearer ${token}`;
  }
  return h;
}

// Una petición al API con sus etiquetas y su comprobación de estado.
export function apiCall(method, path, endpoint, { token, payload, expectedStatus = 200, params } = {}) {
  const requestOptions = { headers: buildHeaders(token), tags: endpointTags(endpoint), timeout: '30s' };
  const url = `${API}${path}`;
  const body = payload === undefined ? null : JSON.stringify(payload);
  const res = http.request(method, url, body, Object.assign(requestOptions, params || {}));
  const ok = check(
    res,
    { [`${endpoint} → ${expectedStatus}`]: (r) => r.status === expectedStatus },
    { endpoint },
  );
  if (!ok && res.status !== 0) {
    console.warn(`${endpoint} respondió ${res.status}: ${String(res.body).slice(0, 200)}`);
  }
  return { res, ok };
}

// Lote de lecturas en paralelo, como las que hace la pantalla del panel.
export function batchGet(token, requests) {
  const reqs = requests.map(([path, endpoint]) => ({
    method: 'GET',
    url: `${API}${path}`,
    params: { headers: buildHeaders(token), tags: endpointTags(endpoint), timeout: '30s' },
  }));
  const responses = http.batch(reqs);
  responses.forEach((r, i) => {
    const endpoint = requests[i][1];
    check(r, { [`${endpoint} → 200`]: (x) => x.status === 200 }, { endpoint });
  });
  return responses;
}

export function login(email) {
  const { res, ok } = apiCall('POST', '/auth/login', 'POST /auth/login', {
    payload: { email, password: PASSWORD },
  });
  if (!ok) {
    fail(`no se pudo entrar como ${email}: ${res.status}`);
  }
  return res.json('access_token');
}

// Un token por usuario virtual: se entra en la primera iteración y se
// reutiliza en las siguientes, como un celular que mantiene la sesión.
const sessions = {};
export function session(email) {
  if (!sessions[email]) {
    sessions[email] = login(email);
  }
  return sessions[email];
}

export function vu() {
  return exec.vu.idInTest - 1;
}

// Fecha del local (America/Lima, UTC-5 sin horario de verano) menos `dias`.
export function todayMinus(days) {
  const d = new Date(Date.now() - 5 * 3600000 - days * 86400000);
  return d.toISOString().slice(0, 10);
}
