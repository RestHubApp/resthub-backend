# Integraciones: qué pasa si una falla

Cada servicio externo de RestHub, qué tan crítico es, cómo se comporta la aplicación sin él y
qué respaldo tiene. El detalle técnico de cada adaptador está en el README; la continuidad del
servicio en general, en [`continuidad.md`](continuidad.md).

`GET /billing/capabilities` y `GET /lookup/status` dicen en vivo qué está encendido.

## Resumen

| Integración | Para qué | Criticidad | Si falla | Respaldo o salida | Pendiente |
| --- | --- | --- | --- | --- | --- |
| **Supabase** (PostgreSQL) | Todos los datos | Crítica | El API responde 503; los pedidos nuevos esperan en el celular | Respaldo diario cifrado fuera de Supabase y respaldos del plan | Comprobar que el respaldo diario corre (ver `continuidad.md`) |
| **Vercel** | Sirve el frontend | Alta | La app instalada sigue abriendo (service worker); un celular nuevo no puede abrirla | Cualquier hosting estático sirve el mismo `dist/` | Ninguno |
| **Lycet → SUNAT** | Firmar y enviar boletas, facturas y notas de crédito | Alta (obligación tributaria) | El comprobante queda `pending` y se reenvía; el cobro no se detiene | Reenvío manual (`/resend`); SUNAT ya registrado (1033) se confirma sin duplicar | Hoy solo corre en local: en producción los comprobantes quedan «sin enviar». Al desplegarlo, respaldar su volumen `lycet-data` (certificados y credenciales) |
| **Google Drive** | Copia del XML, CDR y PDF de cada comprobante aceptado | Media | `archive_status` queda `pending` o `failed`; el cobro no espera | Reintento con `POST /billing/invoices/{id}/archive`; el XML y el CDR también quedan en la base | Confirmar que está configurado en producción |
| **Brevo** | Enviar el comprobante por correo | Baja | 409 `email_sending_disabled` o error al enviar | El PDF se descarga y se entrega por otro medio | Sin reintento automático |
| **Factiliza** | Autocompletar RUC y DNI | Baja | 503 `lookup_unavailable`; el botón deja de ofrecerse si está apagado | Escribir los datos a mano | Plan gratuito: 100 consultas al mes para todos los locales |
| **TypeSafe / OpenRouter (Jev)** | Decisiones de compra, alergias en notas y causas de merma | Baja | Pasa solo a las reglas (`RuleBasedDecisionEngine`) y lo anota (`fallback_reason`) | Las reglas siempre están | Ninguno |
| **`sslip.io`** | Nombre del dominio del API | Crítica | Nadie llega al API aunque esté sano | Ninguno hoy | Dominio propio (`continuidad.md`) |
| **GitHub Actions** | CI, respaldo diario, huella de carbono | Media (solo el respaldo) | No hay respaldo ese día | El respaldo se puede lanzar a mano o correr `scripts/backup_db.py` desde otra máquina | Ninguno |

## Principios que siguen todas

- **Puertos y adaptadores**: el negocio conoce un puerto (`ElectronicInvoiceGateway`,
  `DocumentArchiveGateway`, `EmailGateway`, `DecisionEngineGateway`…); cambiar de proveedor es
  escribir otro adaptador, sin tocar los casos de uso.
- **Plazo propio**: cada llamada a un tercero tiene su plazo (`httpx` con `timeout`) y no
  retiene la base ni el resto de la petición más de lo necesario.
- **Lo que no es esencial no bloquea**: la copia en Drive y la clasificación con IA corren en
  segundo plano después de confirmar la transacción.
- **Apagado limpio**: sin su clave, cada integración se apaga sola y la aplicación lo dice
  (`capabilities`, `status`) en vez de fallar.
- **Sin secretos ni datos personales en los logs**: tokens, RUC o DNI consultados y correos se
  anotan enmascarados o no se anotan.
- **Trazas**: con OpenTelemetry encendido, cada llamada a la IA, a Lycet y a Factiliza es un
  span (sin la consulta de la URL, que puede llevar un token).

## Pruebas

- Dobles de prueba de cada tercero (`tests/fakes*.py`) y respuestas reales de Lycet grabadas
  (`tests/fixtures/lycet/`): aceptado, observado, rechazado y nota aceptada.
- `tests/test_lycet_beta.py` contra el entorno beta de SUNAT, si se le dan las variables.
- Contrato con el frontend (Pact, `tests/contract/`).
- Experimentos de caos sobre la base y el API (`continuidad.md`).
