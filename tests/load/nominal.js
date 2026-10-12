// Escenario 1: carga nominal. 25 usuarios durante 5 minutos (20 meseros,
// 2 cocina, 3 encargados), después de 1 minuto de subida, y 5 pantallas con el
// canal de avisos abierto.
//
//   k6-sse run tests/load/nominal.js

import { noticesScenario, constantScenarios, buildOptions, prepare, summary } from './lib/scenarios.js';

export { notices, kitchen, owner, waiter } from './lib/scenarios.js';

const DURATION = __ENV.DURATION || '5m';
const NAME = __ENV.RUN_NAME || 'nominal';

export const options = buildOptions(
  Object.assign(constantScenarios(25, DURATION), noticesScenario(5, __ENV.SSE_TOTAL || '6m')),
);

export const setup = prepare;
export const handleSummary = summary(NAME);
