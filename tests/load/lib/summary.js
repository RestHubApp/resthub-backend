// Umbrales comunes y `handleSummary`: el resumen de k6 en la terminal, más un
// JSON con todos los datos y un HTML con la tabla por endpoint.

// Resumen de texto estándar de k6 (jslib oficial de Grafana).
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.1.0/index.js';
import { ENDPOINTS } from './common.js';

export const STATS = ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'];

// Las metas del brief para la carga nominal. Las de latencia se miden solo
// sobre el API (`canal:api`): la conexión SSE dura minutos a propósito.
export const GOALS = {
  'http_req_failed{canal:api}': ['rate<0.001'],
  checks: ['rate==1'],
  'http_req_duration{tipo:lectura}': ['p(95)<=100'],
  'http_req_duration{tipo:escritura}': ['p(95)<=200'],
  'http_req_duration{canal:api}': ['p(99)<=500'],
};

// Submétricas por endpoint. El umbral siempre se cumple: solo sirve para que
// k6 calcule los percentiles de cada endpoint y los entregue a `handleSummary`.
export function perEndpoint() {
  const limits = {};
  for (const endpoint of Object.keys(ENDPOINTS)) {
    limits[`http_req_duration{endpoint:${endpoint}}`] = ['max>=0'];
    limits[`http_req_failed{endpoint:${endpoint}}`] = ['rate<=1'];
    limits[`http_reqs{endpoint:${endpoint}}`] = ['count>=0'];
  }
  return limits;
}

function num(x, dec = 2) {
  return x === undefined || x === null || Number.isNaN(x) ? '—' : Number(x).toFixed(dec);
}

export function endpointTable(data) {
  const durationS = data.state.testRunDurationMs / 1000;
  const rows = [];
  for (const [endpoint, kind] of Object.entries(ENDPOINTS)) {
    const d = data.metrics[`http_req_duration{endpoint:${endpoint}}`];
    const f = data.metrics[`http_req_failed{endpoint:${endpoint}}`];
    const n = data.metrics[`http_reqs{endpoint:${endpoint}}`];
    if (!d || !n || !n.values.count) {
      continue;
    }
    rows.push({
      endpoint,
      tipo: kind,
      n: n.values.count,
      rps: n.values.count / durationS,
      p50: d.values.med,
      p90: d.values['p(90)'],
      p95: d.values['p(95)'],
      p99: d.values['p(99)'],
      max: d.values.max,
      errores: f ? f.values.passes : 0,
      tasaError: f ? f.values.rate : 0,
    });
  }
  return rows;
}

function endpointsText(rows) {
  const header = 'endpoint                           tipo        n     rps    p50    p90    p95    p99    max  err';
  const lines = rows.map(
    (r) =>
      `${r.endpoint.padEnd(34)} ${r.tipo.padEnd(9)} ${String(r.n).padStart(5)} ${num(r.rps).padStart(6)} ` +
      `${num(r.p50, 1).padStart(6)} ${num(r.p90, 1).padStart(6)} ${num(r.p95, 1).padStart(6)} ` +
      `${num(r.p99, 1).padStart(6)} ${num(r.max, 0).padStart(6)} ${String(r.errores).padStart(4)}`,
  );
  return ['', 'Latencia por endpoint (ms)', header, ...lines, ''].join('\n');
}

function thresholdList(data) {
  const output = [];
  for (const [name, m] of Object.entries(data.metrics)) {
    if (!m.thresholds || name.includes('{endpoint:')) {
      continue;
    }
    for (const [rule, r] of Object.entries(m.thresholds)) {
      output.push({ metric: name, rule, ok: r.ok });
    }
  }
  return output;
}

function html(name, data, rows) {
  const u = thresholdList(data);
  const g = (k, s) => (data.metrics[k] ? data.metrics[k].values[s] : undefined);
  const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const rowsHtml = rows
    .map(
      (r) =>
        `<tr><td>${esc(r.endpoint)}</td><td>${r.tipo}</td><td>${r.n}</td><td>${num(r.rps)}</td>` +
        `<td>${num(r.p50, 1)}</td><td>${num(r.p90, 1)}</td><td>${num(r.p95, 1)}</td><td>${num(r.p99, 1)}</td>` +
        `<td>${num(r.max, 0)}</td><td>${r.errores}</td></tr>`,
    )
    .join('');
  const thresholdsHtml = u
    .map((x) => `<tr class="${x.ok ? 'ok' : 'mal'}"><td>${esc(x.metric)}</td><td>${esc(x.rule)}</td><td>${x.ok ? 'cumple' : 'NO cumple'}</td></tr>`)
    .join('');
  return `<!doctype html><html lang="es"><head><meta charset="utf-8"><title>k6 — ${esc(name)}</title>
<style>body{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#111;background:#fff}
table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #bbb;padding:4px 8px;text-align:right}
td:first-child,th:first-child{text-align:left}th{background:#eee}tr.ok td:last-child{color:#060}tr.mal td:last-child{color:#b00;font-weight:600}
.k{display:inline-block;margin:0 18px 8px 0}.k b{display:block;font-size:20px}</style></head><body>
<h1>RestHub — k6: ${esc(name)}</h1>
<p>Fecha: ${new Date().toISOString()} · Duración: ${num(data.state.testRunDurationMs / 1000, 0)} s · Destino: backend local</p>
<div><span class="k">Peticiones<b>${g('http_reqs', 'count')}</b></span>
<span class="k">RPS<b>${num(g('http_reqs', 'rate'))}</b></span>
<span class="k">Errores API<b>${num(100 * (g('http_req_failed{canal:api}', 'rate') || 0), 3)} %</b></span>
<span class="k">Checks<b>${num(100 * (g('checks', 'rate') || 0), 2)} %</b></span>
<span class="k">VUs máx.<b>${g('vus_max', 'max') || g('vus_max', 'value')}</b></span>
<span class="k">p95 API<b>${num(g('http_req_duration{canal:api}', 'p(95)'), 1)} ms</b></span>
<span class="k">p99 API<b>${num(g('http_req_duration{canal:api}', 'p(99)'), 1)} ms</b></span></div>
<h2>Umbrales</h2><table><tr><th>Métrica</th><th>Regla</th><th>Resultado</th></tr>${thresholdsHtml}</table>
<h2>Latencia por endpoint (ms)</h2><table><tr><th>Endpoint</th><th>Tipo</th><th>n</th><th>RPS</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th><th>máx.</th><th>Errores</th></tr>${rowsHtml}</table>
</body></html>`;
}

export function summary(name) {
  return (data) => {
    const dir = __ENV.REPORTES || 'tests/load/reportes';
    const rows = endpointTable(data);
    const text = textSummary(data, { indent: ' ', enableColors: false }) + endpointsText(rows);
    return {
      stdout: text,
      [`${dir}/${name}-resumen.txt`]: text,
      [`${dir}/${name}.json`]: JSON.stringify({ endpoints: rows, data }, null, 2),
      [`${dir}/${name}.html`]: html(name, data, rows),
    };
  };
}
