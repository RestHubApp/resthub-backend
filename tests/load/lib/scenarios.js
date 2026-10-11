// Arma los escenarios de k6 a partir de cuántos usuarios de cada perfil hay en
// cada momento. El reparto es el de un turno: 80 % meseros, 8 % cocina y 12 %
// encargados (25 usuarios = 20 + 2 + 3).

import { cookEmail, login } from './common.js';
import { noticesFlow, kitchenFlow, ownerFlow, waiterFlow } from './flows.js';
import { STATS, GOALS, perEndpoint, summary } from './summary.js';

export function split(total) {
  const kitchen = Math.round(total * 0.08);
  const owners = Math.round(total * 0.12);
  return { waiters: total - kitchen - owners, kitchen, owners };
}

// `etapas`: [{ duration, total }]. Devuelve un escenario ramping-vus por perfil.
export function scenariosByPhases(phases, { start = 0 } = {}) {
  const profile = (key, exec) => ({
    executor: 'ramping-vus',
    exec,
    startVUs: split(start)[key],
    stages: phases.map((e) => ({ duration: e.duration, target: split(e.total)[key] })),
    // Un ciclo de mesero dura cerca de un minuto: al bajar la carga se le deja
    // terminar el pedido en curso en vez de cortarlo a medias.
    gracefulRampDown: '90s',
    gracefulStop: '90s',
  });
  return {
    waiters: profile('waiters', 'waiter'),
    kitchen: profile('kitchen', 'kitchen'),
    owners: profile('owners', 'owner'),
  };
}

// Carga sostenida: `total` usuarios durante `duracion`, después de una subida
// de `subida` en la que van entrando de a poco, como el personal al empezar el
// turno. Sin la subida los 25 inician sesión en el mismo segundo (25 bcrypt a
// la vez), algo que no pasa en un local; ese caso lo cubre el escenario de pico.
export function constantScenarios(total, duration, rampUp = __ENV.RAMP_UP || '1m') {
  const r = split(total);
  const profile = (vus, exec) =>
    rampUp === '0s'
      ? { executor: 'constant-vus', exec, vus, duration: duration, gracefulStop: '90s' }
      : {
          executor: 'ramping-vus',
          exec,
          startVUs: 0,
          stages: [
            { duration: rampUp, target: vus },
            { duration: duration, target: vus },
          ],
          gracefulRampDown: '90s',
          gracefulStop: '90s',
        };
  return {
    waiters: profile(r.waiters, 'waiter'),
    kitchen: profile(r.kitchen, 'kitchen'),
    owners: profile(r.owners, 'owner'),
  };
}

// Pantallas con el canal de avisos abierto durante toda la prueba.
export function noticesScenario(connections, duration) {
  return {
    notices: {
      executor: 'per-vu-iterations',
      exec: 'notices',
      vus: connections,
      iterations: 1,
      maxDuration: duration,
      gracefulStop: '30s',
      env: { SSE_DURATION: duration },
    },
  };
}

export function buildOptions(scenarios, { useGoals = true } = {}) {
  return {
    scenarios,
    thresholds: Object.assign({}, useGoals ? GOALS : {}, perEndpoint()),
    summaryTrendStats: STATS,
    setupTimeout: '60s',
  };
}

// La cuenta de cocina que marca «listo» los pedidos: una sola sesión para toda
// la prueba (su acceso es una petición más de `POST /auth/login`).
export function prepare() {
  return { kitchenToken: login(cookEmail(5)) };
}

export { noticesFlow as notices, kitchenFlow as kitchen, ownerFlow as owner, waiterFlow as waiter, summary };
