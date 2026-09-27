// Escenario 1: carga nominal. 25 usuarios durante 5 minutos (20 meseros,
// 2 cocina, 3 encargados), después de 1 minuto de subida, y 5 pantallas con el
// canal de avisos abierto.
//
//   k6-sse run tests/load/nominal.js

import { escenarioAvisos, escenariosConstantes, opciones, preparar, resumen } from './lib/escenarios.js';

export { avisos, cocina, encargado, mesero } from './lib/escenarios.js';

const DURACION = __ENV.DURACION || '5m';
const NOMBRE = __ENV.NOMBRE || 'nominal';

export const options = opciones(
  Object.assign(escenariosConstantes(25, DURACION), escenarioAvisos(5, __ENV.SSE_TOTAL || '6m')),
);

export const setup = preparar;
export const handleSummary = resumen(NOMBRE);
