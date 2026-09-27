// Arma los escenarios de k6 a partir de cuántos usuarios de cada perfil hay en
// cada momento. El reparto es el de un turno: 80 % meseros, 8 % cocina y 12 %
// encargados (25 usuarios = 20 + 2 + 3).

import { cocinero, entrar } from './comun.js';
import { flujoAvisos, flujoCocina, flujoEncargado, flujoMesero } from './flujos.js';
import { ESTADISTICAS, METAS, porEndpoint, resumen } from './resumen.js';

export function reparto(total) {
  const cocina = Math.round(total * 0.08);
  const encargados = Math.round(total * 0.12);
  return { meseros: total - cocina - encargados, cocina, encargados };
}

// `etapas`: [{ duration, total }]. Devuelve un escenario ramping-vus por perfil.
export function escenariosPorEtapas(etapas, { inicio = 0 } = {}) {
  const perfil = (clave, exec) => ({
    executor: 'ramping-vus',
    exec,
    startVUs: reparto(inicio)[clave],
    stages: etapas.map((e) => ({ duration: e.duration, target: reparto(e.total)[clave] })),
    // Un ciclo de mesero dura cerca de un minuto: al bajar la carga se le deja
    // terminar el pedido en curso en vez de cortarlo a medias.
    gracefulRampDown: '90s',
    gracefulStop: '90s',
  });
  return {
    meseros: perfil('meseros', 'mesero'),
    cocina: perfil('cocina', 'cocina'),
    encargados: perfil('encargados', 'encargado'),
  };
}

// Carga sostenida: `total` usuarios durante `duracion`, después de una subida
// de `subida` en la que van entrando de a poco, como el personal al empezar el
// turno. Sin la subida los 25 inician sesión en el mismo segundo (25 bcrypt a
// la vez), algo que no pasa en un local; ese caso lo cubre el escenario de pico.
export function escenariosConstantes(total, duracion, subida = __ENV.SUBIDA || '1m') {
  const r = reparto(total);
  const perfil = (vus, exec) =>
    subida === '0s'
      ? { executor: 'constant-vus', exec, vus, duration: duracion, gracefulStop: '90s' }
      : {
          executor: 'ramping-vus',
          exec,
          startVUs: 0,
          stages: [
            { duration: subida, target: vus },
            { duration: duracion, target: vus },
          ],
          gracefulRampDown: '90s',
          gracefulStop: '90s',
        };
  return {
    meseros: perfil(r.meseros, 'mesero'),
    cocina: perfil(r.cocina, 'cocina'),
    encargados: perfil(r.encargados, 'encargado'),
  };
}

// Pantallas con el canal de avisos abierto durante toda la prueba.
export function escenarioAvisos(conexiones, duracion) {
  return {
    avisos: {
      executor: 'per-vu-iterations',
      exec: 'avisos',
      vus: conexiones,
      iterations: 1,
      maxDuration: duracion,
      gracefulStop: '30s',
      env: { SSE_DURACION: duracion },
    },
  };
}

export function opciones(scenarios, { metas = true } = {}) {
  return {
    scenarios,
    thresholds: Object.assign({}, metas ? METAS : {}, porEndpoint()),
    summaryTrendStats: ESTADISTICAS,
    setupTimeout: '60s',
  };
}

// La cuenta de cocina que marca «listo» los pedidos: una sola sesión para toda
// la prueba (su acceso es una petición más de `POST /auth/login`).
export function preparar() {
  return { tokenCocina: entrar(cocinero(5)) };
}

export { flujoAvisos as avisos, flujoCocina as cocina, flujoEncargado as encargado, flujoMesero as mesero, resumen };
