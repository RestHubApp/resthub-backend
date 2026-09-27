// Escenario 4: resistencia (soak). La carga nominal, 25 usuarios, durante
// 15 minutos (más 1 minuto de subida). `correr.sh` la acompaña con
// `medir_recursos.sh` para ver si crecen la memoria de uvicorn o las
// conexiones de PostgreSQL.
//
//   k6-sse run tests/load/resistencia.js

import { escenarioAvisos, escenariosConstantes, opciones, preparar, resumen } from './lib/escenarios.js';

export { avisos, cocina, encargado, mesero } from './lib/escenarios.js';

const DURACION = __ENV.DURACION || '15m';

export const options = opciones(
  Object.assign(escenariosConstantes(25, DURACION), escenarioAvisos(5, __ENV.SSE_TOTAL || '16m')),
);

export const setup = preparar;
export const handleSummary = resumen(__ENV.NOMBRE || 'resistencia');
