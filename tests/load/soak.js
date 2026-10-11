// Escenario 4: resistencia (soak). La carga nominal, 25 usuarios, durante
// 15 minutos (más 1 minuto de subida). `run.sh` la acompaña con
// `measure_resources.sh` para ver si crecen la memoria de uvicorn o las
// conexiones de PostgreSQL.
//
//   k6-sse run tests/load/soak.js

import { noticesScenario, constantScenarios, buildOptions, prepare, summary } from './lib/scenarios.js';

export { notices, kitchen, owner, waiter } from './lib/scenarios.js';

const DURATION = __ENV.DURACION || '15m';

export const options = buildOptions(
  Object.assign(constantScenarios(25, DURATION), noticesScenario(5, __ENV.SSE_TOTAL || '16m')),
);

export const setup = prepare;
export const handleSummary = summary(__ENV.NOMBRE || 'soak');
