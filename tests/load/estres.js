// Escenario 2: estrés. Rampa de 25 a 50, 100 y 200 usuarios, con una meseta
// de 3 minutos en cada nivel, para encontrar dónde sube el p95 o aparecen
// errores. Los umbrales de la carga nominal se evalúan igual, pero aquí se
// espera que fallen: el dato es en qué nivel lo hacen.
//
//   k6-sse run tests/load/estres.js

import { escenarioAvisos, escenariosPorEtapas, opciones, preparar, resumen } from './lib/escenarios.js';

export { avisos, cocina, encargado, mesero } from './lib/escenarios.js';

const MESETA = __ENV.MESETA || '3m';
const etapas = [
  { duration: '1m', total: 25 },
  { duration: MESETA, total: 25 },
  { duration: '1m', total: 50 },
  { duration: MESETA, total: 50 },
  { duration: '1m', total: 100 },
  { duration: MESETA, total: 100 },
  { duration: '1m', total: 200 },
  { duration: MESETA, total: 200 },
  { duration: '1m', total: 0 },
];

export const options = opciones(
  Object.assign(escenariosPorEtapas(etapas), escenarioAvisos(10, __ENV.SSE_TOTAL || '17m')),
);

export const setup = preparar;
export const handleSummary = resumen(__ENV.NOMBRE || 'estres');
