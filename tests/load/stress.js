// Escenario 2: estrés. Rampa de 25 a 50, 100 y 200 usuarios, con una meseta
// de 3 minutos en cada nivel, para encontrar dónde sube el p95 o aparecen
// errores. Los umbrales de la carga nominal se evalúan igual, pero aquí se
// espera que fallen: el dato es en qué nivel lo hacen.
//
//   k6-sse run tests/load/stress.js

import { noticesScenario, scenariosByPhases, buildOptions, prepare, summary } from './lib/scenarios.js';

export { notices, kitchen, owner, waiter } from './lib/scenarios.js';

const PLATEAU = __ENV.PLATEAU || '3m';
const phases = [
  { duration: '1m', total: 25 },
  { duration: PLATEAU, total: 25 },
  { duration: '1m', total: 50 },
  { duration: PLATEAU, total: 50 },
  { duration: '1m', total: 100 },
  { duration: PLATEAU, total: 100 },
  { duration: '1m', total: 200 },
  { duration: PLATEAU, total: 200 },
  { duration: '1m', total: 0 },
];

export const options = buildOptions(
  Object.assign(scenariosByPhases(phases), noticesScenario(10, __ENV.SSE_TOTAL || '17m')),
);

export const setup = prepare;
export const handleSummary = summary(__ENV.RUN_NAME || 'stress');
