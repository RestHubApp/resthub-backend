// Escenario 3: pico. De 5 usuarios a 100 en 10 segundos, 2 minutos arriba y
// vuelta a 5, para ver cómo absorbe el salto y si se recupera.
//
//   k6-sse run tests/load/pico.js

import { escenarioAvisos, escenariosPorEtapas, opciones, preparar, resumen } from './lib/escenarios.js';

export { avisos, cocina, encargado, mesero } from './lib/escenarios.js';

const etapas = [
  { duration: '2m', total: 5 },
  { duration: '10s', total: 100 },
  { duration: '2m', total: 100 },
  { duration: '10s', total: 5 },
  { duration: '3m', total: 5 },
];

export const options = opciones(
  Object.assign(escenariosPorEtapas(etapas, { inicio: 5 }), escenarioAvisos(5, '8m')),
);

export const setup = preparar;
export const handleSummary = resumen(__ENV.NOMBRE || 'pico');
