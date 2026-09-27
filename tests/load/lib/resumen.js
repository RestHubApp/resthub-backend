// Umbrales comunes y `handleSummary`: el resumen de k6 en la terminal, más un
// JSON con todos los datos y un HTML con la tabla por endpoint.

// Resumen de texto estándar de k6 (jslib oficial de Grafana).
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.1.0/index.js';
import { ENDPOINTS } from './comun.js';

export const ESTADISTICAS = ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'];

// Las metas del brief para la carga nominal. Las de latencia se miden solo
// sobre el API (`canal:api`): la conexión SSE dura minutos a propósito.
export const METAS = {
  'http_req_failed{canal:api}': ['rate<0.001'],
  checks: ['rate==1'],
  'http_req_duration{tipo:lectura}': ['p(95)<=100'],
  'http_req_duration{tipo:escritura}': ['p(95)<=200'],
  'http_req_duration{canal:api}': ['p(99)<=500'],
};

// Submétricas por endpoint. El umbral siempre se cumple: solo sirve para que
// k6 calcule los percentiles de cada endpoint y los entregue a `handleSummary`.
export function porEndpoint() {
  const umbrales = {};
  for (const endpoint of Object.keys(ENDPOINTS)) {
    umbrales[`http_req_duration{endpoint:${endpoint}}`] = ['max>=0'];
    umbrales[`http_req_failed{endpoint:${endpoint}}`] = ['rate<=1'];
    umbrales[`http_reqs{endpoint:${endpoint}}`] = ['count>=0'];
  }
  return umbrales;
}

function num(x, dec = 2) {
  return x === undefined || x === null || Number.isNaN(x) ? '—' : Number(x).toFixed(dec);
}

export function tablaEndpoints(data) {
  const duracionS = data.state.testRunDurationMs / 1000;
  const filas = [];
  for (const [endpoint, tipo] of Object.entries(ENDPOINTS)) {
    const d = data.metrics[`http_req_duration{endpoint:${endpoint}}`];
    const f = data.metrics[`http_req_failed{endpoint:${endpoint}}`];
    const n = data.metrics[`http_reqs{endpoint:${endpoint}}`];
    if (!d || !n || !n.values.count) {
      continue;
    }
    filas.push({
      endpoint,
      tipo,
      n: n.values.count,
      rps: n.values.count / duracionS,
      p50: d.values.med,
      p90: d.values['p(90)'],
      p95: d.values['p(95)'],
      p99: d.values['p(99)'],
      max: d.values.max,
      errores: f ? f.values.passes : 0,
      tasaError: f ? f.values.rate : 0,
    });
  }
  return filas;
}

function textoEndpoints(filas) {
  const cab = 'endpoint                           tipo        n     rps    p50    p90    p95    p99    max  err';
  const lineas = filas.map(
    (r) =>
      `${r.endpoint.padEnd(34)} ${r.tipo.padEnd(9)} ${String(r.n).padStart(5)} ${num(r.rps).padStart(6)} ` +
      `${num(r.p50, 1).padStart(6)} ${num(r.p90, 1).padStart(6)} ${num(r.p95, 1).padStart(6)} ` +
      `${num(r.p99, 1).padStart(6)} ${num(r.max, 0).padStart(6)} ${String(r.errores).padStart(4)}`,
  );
  return ['', 'Latencia por endpoint (ms)', cab, ...lineas, ''].join('\n');
}

function umbrales(data) {
  const salida = [];
  for (const [nombre, m] of Object.entries(data.metrics)) {
    if (!m.thresholds || nombre.includes('{endpoint:')) {
      continue;
    }
    for (const [regla, r] of Object.entries(m.thresholds)) {
      salida.push({ metrica: nombre, regla, ok: r.ok });
    }
  }
  return salida;
}

function html(nombre, data, filas) {
  const u = umbrales(data);
  const g = (k, s) => (data.metrics[k] ? data.metrics[k].values[s] : undefined);
  const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const filasHtml = filas
    .map(
      (r) =>
        `<tr><td>${esc(r.endpoint)}</td><td>${r.tipo}</td><td>${r.n}</td><td>${num(r.rps)}</td>` +
        `<td>${num(r.p50, 1)}</td><td>${num(r.p90, 1)}</td><td>${num(r.p95, 1)}</td><td>${num(r.p99, 1)}</td>` +
        `<td>${num(r.max, 0)}</td><td>${r.errores}</td></tr>`,
    )
    .join('');
  const umbralesHtml = u
    .map((x) => `<tr class="${x.ok ? 'ok' : 'mal'}"><td>${esc(x.metrica)}</td><td>${esc(x.regla)}</td><td>${x.ok ? 'cumple' : 'NO cumple'}</td></tr>`)
    .join('');
  return `<!doctype html><html lang="es"><head><meta charset="utf-8"><title>k6 — ${esc(nombre)}</title>
<style>body{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#111;background:#fff}
table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #bbb;padding:4px 8px;text-align:right}
td:first-child,th:first-child{text-align:left}th{background:#eee}tr.ok td:last-child{color:#060}tr.mal td:last-child{color:#b00;font-weight:600}
.k{display:inline-block;margin:0 18px 8px 0}.k b{display:block;font-size:20px}</style></head><body>
<h1>RestHub — k6: ${esc(nombre)}</h1>
<p>Fecha: ${new Date().toISOString()} · Duración: ${num(data.state.testRunDurationMs / 1000, 0)} s · Destino: backend local</p>
<div><span class="k">Peticiones<b>${g('http_reqs', 'count')}</b></span>
<span class="k">RPS<b>${num(g('http_reqs', 'rate'))}</b></span>
<span class="k">Errores API<b>${num(100 * (g('http_req_failed{canal:api}', 'rate') || 0), 3)} %</b></span>
<span class="k">Checks<b>${num(100 * (g('checks', 'rate') || 0), 2)} %</b></span>
<span class="k">VUs máx.<b>${g('vus_max', 'max') || g('vus_max', 'value')}</b></span>
<span class="k">p95 API<b>${num(g('http_req_duration{canal:api}', 'p(95)'), 1)} ms</b></span>
<span class="k">p99 API<b>${num(g('http_req_duration{canal:api}', 'p(99)'), 1)} ms</b></span></div>
<h2>Umbrales</h2><table><tr><th>Métrica</th><th>Regla</th><th>Resultado</th></tr>${umbralesHtml}</table>
<h2>Latencia por endpoint (ms)</h2><table><tr><th>Endpoint</th><th>Tipo</th><th>n</th><th>RPS</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th><th>máx.</th><th>Errores</th></tr>${filasHtml}</table>
</body></html>`;
}

export function resumen(nombre) {
  return (data) => {
    const dir = __ENV.REPORTES || 'tests/load/reportes';
    const filas = tablaEndpoints(data);
    const texto = textSummary(data, { indent: ' ', enableColors: false }) + textoEndpoints(filas);
    return {
      stdout: texto,
      [`${dir}/${nombre}-resumen.txt`]: texto,
      [`${dir}/${nombre}.json`]: JSON.stringify({ endpoints: filas, data }, null, 2),
      [`${dir}/${nombre}.html`]: html(nombre, data, filas),
    };
  };
}
