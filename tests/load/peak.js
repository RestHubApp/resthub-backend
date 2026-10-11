// Escenario 3: pico. De 5 usuarios a 100 en 10 segundos, 2 minutos arriba y
// vuelta a 5, para ver cómo absorbe el salto y si se recupera.
//
//   k6-sse run tests/load/peak.js

import { noticesScenario, scenariosByPhases, buildOptions, prepare, summary } from './lib/scenarios.js';

export { notices, kitchen, owner, waiter } from './lib/scenarios.js';

const phases = [
  { duration: '2m', total: 5 },
  { duration: '10s', total: 100 },
  { duration: '2m', total: 100 },
  { duration: '10s', total: 5 },
  { duration: '3m', total: 5 },
];

export const options = buildOptions(
  Object.assign(scenariosByPhases(phases, { start: 5 }), noticesScenario(5, '8m')),
);

export const setup = prepare;
export const handleSummary = summary(__ENV.RUN_NAME || 'peak');
