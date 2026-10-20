# Eventos de seguridad: qué se registra, dónde y quién lo ve

Inventario de los registros del backend que sirven para investigar un incidente
(OWASP ASVS 5.0, V16.1.1): los eventos de log, los asientos de las bitácoras y las
tablas de seguridad. Un evento nuevo se agrega aquí en el mismo PR. El panel de
observabilidad y la redacción de logs están descritos en el README («Panel de
observabilidad», «Historial de accesos», «Bitácoras de solo inserción»).

## Destinos

| Destino | Qué llega | Quién lo lee | Cuánto dura |
|---|---|---|---|
| **stdout → Railway** | Todo evento de structlog desde `LOG_LEVEL` (INFO por omisión), una línea JSON por evento con `LOG_JSON=true` (`core/logs.py`). Cada línea lleva hora UTC, nivel, logger, `request_id`, método, ruta, `client_network` (la IP sin su último bloque), y en cuanto la petición se autentica, `account_kind`, `account_id` y `restaurant_id`; con trazas, `trace_id`/`span_id`. Pasa por la misma regla que el panel (`core/redaction.py`): claves sensibles y de datos personales, textos, mensaje y traceback (sin variables locales). | Quien tenga acceso al proyecto de Railway. | Lo que conserve el plan de Railway; no lo controla el código (fuera del código). |
| **`obs_events`** (panel de observabilidad) | Los eventos de nivel `warning` o superior, con `core/redaction.py` aplicado, y las excepciones con su traceback (`core/telemetry.py`). Los `request.completed` de un 4xx no entran: ya están en `obs_requests`. | Cuentas de plataforma con 2FA y `platform.observability.read`: `GET /platform/observability/logs`. | 14 días (`OBSERVABILITY_RETENTION_DAYS`). |
| **`obs_requests`** | Una fila por petición: ruta (plantilla), estado, duración, tipo de cuenta, `account_id`, `restaurant_id`. | Igual: `GET /platform/observability/requests`. | 14 días. |
| **`access_history`** | Cada intento de login: resultado (`succeeded`, `failed`, `throttled`, `blocked`), dispositivo, IP sin su último bloque, ciudad, país, marcas `new_device`/`new_country`/`outside_hours` (`core/access_history.py`). | La propia cuenta (`GET /auth/me/access-history`) y quien tenga `staff.manage` en el local (`GET /staff/{id}/access-history`). | 90 días (`ACCESS_HISTORY_RETENTION_DAYS`). |
| **`activity_log`** (bitácora del local) | Los `ActivityKind` de abajo, con cuenta, hora y un detalle de hasta 200 caracteres (`core/activity.py`, `core/activity_log.py`). | Quien tenga `activity.read` en ese local: `GET /activity`. | Sin purga. Solo inserción: un trigger rechaza `UPDATE` y `DELETE` (migración `0034`) y, en PostgreSQL, `TRUNCATE` (`0044`). |
| **`platform_activity`** | Los `PlatformActivityKind` (`modules/platform/domain/entities.py`). | Plataforma con `platform.activity.read`: `GET /platform/activity`. | Sin purga; solo inserción. |
| **`platform_security_alerts`** | Picos de accesos fallidos, frenados y 403 (`SpikeKind`, `modules/platform/domain/security_alerts.py`). | Plataforma: `GET /platform/observability/security-alerts`. | Sin purga. |
| **`ai_decisions`** | Entrada y salida de cada decisión de la IA o de las reglas. | Quien tenga `insights.read`, por las vistas de `insights`. | Sin purga; solo inserción. |
| **Colector OTLP** (opcional) | Trazas, si existe `OTEL_EXPORTER_OTLP_ENDPOINT` (`core/tracing.py`). | Quien administre el colector. | La del colector. |

## Eventos de log

Los nombres son estables: el panel y las alertas los buscan por nombre. Los `warning`
o superiores llegan además a `obs_events`.

### Acceso y sesión

| Evento | Nivel | Cuándo | Archivo |
|---|---|---|---|
| `auth.login_succeeded` | info | Login correcto del personal (`user_id`, `role_id`, `restaurant_id`). | `modules/accounts/adapters/api/auth_router.py` |
| `auth.login_failed` | warning | Contraseña equivocada o cuenta inexistente (`reason`, correo enmascarado); contraseña temporal vencida (`temporary_password_expired`); local desactivado. | ídem |
| `auth.login_throttled` | warning | El límite de intentos frenó un login (`retry_after`). | ídem |
| `auth.unfamiliar_access` | warning | Login correcto desde un dispositivo o país nuevo. | ídem |
| `auth.previous_session_closed` | info | Un login correcto cerró la sesión que el navegador traía en su cookie. | ídem |
| `auth.logout` | info | Cierre de sesión del personal. | ídem |
| `auth.preview_started` | info | Se canjeó un código de vista previa. | ídem |
| `auth.preview_rejected` | warning | Código de vista previa inválido, usado o vencido. | ídem |
| `auth.preview_ended` | info | Salió de una vista previa: su token deja de valer. | ídem |
| `auth.terms_accepted` | info | Aceptó términos y tratamiento de datos. | ídem |
| `auth.password_changed`, `platform.password_changed` | info | Cambió su contraseña (personal o plataforma). | ídem |
| `auth.password_change_rejected` | warning | La contraseña actual no coincidió al cambiarla. | ídem |
| `auth.reauthenticated` | info | Confirmó su contraseña para una acción crítica. | `modules/accounts/adapters/api/security_router.py` |
| `auth.reauth_failed` | warning | Contraseña o código de dos pasos equivocado al confirmar una acción crítica (`reason`). | ídem |
| `auth.reauth_throttled` | warning | El límite de intentos frenó una reautenticación (`retry_after`). | ídem |
| `auth.session_closed`, `auth.other_sessions_closed` | info | Cerró una sesión propia o todas las demás. | ídem |
| `staff.session_closed`, `staff.sessions_closed` | info | Un encargado cerró sesiones de alguien del personal. | `modules/accounts/adapters/api/staff_security_router.py` |
| `auth.unlock_failed` | warning | Cada PIN o contraseña equivocados al desbloquear. | `modules/accounts/adapters/api/idle_lock_router.py` |
| `auth.session_locked_out` | warning | Tercer PIN equivocado: la sesión se cerró. | ídem |
| `auth.two_factor_enabled`, `auth.two_factor_disabled` | info | Activó o apagó la verificación en dos pasos. | `modules/accounts/adapters/api/two_factor_router.py` |
| `platform.login_succeeded` | info | Login correcto de plataforma (por `/auth/login` o el obsoleto `/platform/auth/login`). | `accounts/.../auth_router.py`, `platform/.../auth_router.py` |
| `platform.login_failed`, `platform.login_throttled` | warning | Fallo o freno en el acceso obsoleto de plataforma; comparte el contador de `/auth/login`. | `modules/platform/adapters/api/auth_router.py` |
| `platform.logout` | info | Cierre de sesión de plataforma. | `accounts/.../auth_router.py` |

### Abuso, errores y disponibilidad

| Evento | Nivel | Cuándo | Archivo |
|---|---|---|---|
| `http.rate_limited` | warning | Una IP pasó `RATE_LIMIT_PER_MINUTE` (429). | `core/rate_limit.py` |
| `auth.csrf_rejected` | warning | Falta la cabecera `X-CSRF-Token` o no coincide con la cookie (403 `csrf_token_invalid`, `reason`). | `core/session_cookie.py` |
| `security.spike` | warning | Un pico superó su umbral (`kind`, `count`, `threshold`, `window`). | `modules/platform/adapters/security_alert_loop.py` |
| `security.spike_check_failed` | error | La revisión de picos falló. | ídem |
| `request.completed` | info, warning (4xx), error (5xx) | Fin de cada petición, con estado y tiempos. Un 401, 403 o 422 sale aquí como aviso. | `core/request_logging.py` |
| `request.failed` | error | Excepción no controlada (500), con traceback. | ídem |
| `request.deadline_exceeded` | error | Una petición pasó `REQUEST_DEADLINE_SECONDS` (503). | `core/request_deadline.py` |
| `database.unavailable` | error | La base no responde (503). | `core/database_errors.py` |
| `telemetry.write_failed`, `telemetry.flush_failed`, `telemetry.purge_failed` | warning | El panel perdió filas o no purgó. | `core/telemetry.py` |
| `app.started`, `app.stopped` | info | Arranque y apagado. | `main.py` |

Los fallos de terceros (`billing.*_unreachable`, `billing.provider_failed`,
`billing.email_rejected`, `lookup.unreachable`, `llm.request_failed`, `jev.timeout`…)
no son de seguridad, pero sirven para ver una integración caída o un token
revocado.

## Asientos de la bitácora del local (`ActivityKind`)

Definidos en `core/activity.py`. Los de seguridad y datos personales:

| Grupo | Tipos |
|---|---|
| Sesiones | `signed_in`, `signed_out`, `session_closed`, `session_locked_out`, `staff_session_closed`, `recovery_code_used` |
| Credenciales | `password_changed`, `pin_changed`, `two_factor_enabled`, `two_factor_disabled`, `staff_password_reset` |
| Consentimiento | `terms_accepted`, `privacy_accepted` |
| Personal y permisos | `staff_registered`, `staff_updated`, `staff_status_changed`, `role_created`, `role_updated`, `role_deleted` |
| Datos de clientes | `customer_created`, `customer_updated`, `customer_viewed`, `customer_revealed`, `customer_exported`, `customer_anonymized`, `data_access_alert` |
| Facturación | `billing_settings_updated`, `invoice_issued`, `invoice_emailed`, `invoice_voided`, `credit_note_issued` |
| Dinero | `order_discounted`, `order_courtesy`, `order_cancelled`, `order_item_voided`, `payment_voided`, `cash_opened`, `cash_closed` |

El resto (pedidos, menú, inventario, compras, IA) son de operación; la lista completa
está en el enum. En la plataforma (`PlatformActivityKind`): `signed_in`,
`restaurant_created`, `restaurant_updated` (también activar o desactivar),
`owner_added`, `owner_password_reset`, `sandbox_reset`, `preview_started`.

## Lo que no queda registrado hoy

- Un código de verificación en dos pasos equivocado en el login no tiene evento propio:
  queda como `request.completed` 401 y como `failed` en `access_history`.
- Los 422 de validación solo aparecen como `request.completed` (V16.3.3).
- No hay un colector externo de logs: las bitácoras viven en la misma base que la
  aplicación (brecha 3 de `docs/asvs-5.0.md`).
- Los logs del navegador quedan en la consola (frontend, `src/services/logger.ts`).
