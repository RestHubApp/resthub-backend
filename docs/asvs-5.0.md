# Lista de chequeo OWASP ASVS 5.0 (RNFS-21)

Verificación de RestHub contra el [OWASP Application Security Verification Standard 5.0.0](https://github.com/OWASP/ASVS/tree/v5.0.0/5.0),
niveles **L1 y L2** (253 requisitos). El nivel 3 queda fuera de alcance.

- **Fecha:** 2026-10-26 (primera versión: 2026-10-19). La última revisión cierra las cookies `__Host-`, la rotación del `sid` al reautenticar y desbloquear, los códigos de recuperación de 128 bits o más, la verificación en dos pasos obligatoria para quien administra el personal, la cola sin conexión al cerrar la sesión y el DNI/RUC en los logs.
- **Código revisado:** `resthub-backend` en `f9b3309` (rama `claude/affectionate-planck-q0hbar`, con las brechas cerradas sobre `develop`) y `resthub-frontend` en `a425ad0` (rama `asvs-front`, el PR del frontend que se fusiona justo después del del backend).
- **Método:** cada requisito se comparó con el código y las pruebas de los dos repositorios. Las rutas llevan el prefijo `backend:` o `frontend:` según el repositorio.
- **Estados:**
  - **Cumple:** hay código (y, cuando existe, una prueba) que lo satisface.
  - **Parcial:** se cumple en parte; la celda dice qué falta.
  - **No cumple:** no está implementado.
  - **No aplica:** la función que el requisito cubre no existe en RestHub (por ejemplo, WebRTC, GraphQL o un proveedor OAuth propio).
  - **Fuera del código:** depende de la infraestructura o del proceso (por ejemplo, el TLS del proveedor) y no se puede verificar en los repositorios.

Esta lista se vuelve a revisar con cada versión que toque autenticación, sesiones, datos personales o la configuración del despliegue. Al cerrar una brecha, se actualiza su fila.

## Resumen

| Capítulo | Cumple | Parcial | No cumple | No aplica | Fuera del código | Total |
|---|---:|---:|---:|---:|---:|---:|
| V1 Codificación y saneamiento | 16 | 0 | 0 | 11 | 0 | 27 |
| V2 Validación y lógica de negocio | 11 | 0 | 0 | 0 | 0 | 11 |
| V3 Seguridad del frontend web | 16 | 2 | 0 | 1 | 0 | 19 |
| V4 Seguridad de API y servicios web | 2 | 0 | 0 | 6 | 2 | 10 |
| V5 Manejo de archivos | 8 | 0 | 0 | 1 | 0 | 9 |
| V6 Autenticación | 25 | 3 | 0 | 7 | 0 | 35 |
| V7 Gestión de sesiones | 15 | 0 | 0 | 3 | 0 | 18 |
| V8 Autorización | 6 | 1 | 0 | 0 | 0 | 7 |
| V9 Tokens autocontenidos | 7 | 0 | 0 | 0 | 0 | 7 |
| V10 OAuth y OIDC | 3 | 0 | 0 | 26 | 0 | 29 |
| V11 Criptografía | 9 | 4 | 0 | 1 | 0 | 14 |
| V12 Comunicación segura | 1 | 2 | 0 | 1 | 5 | 9 |
| V13 Configuración | 8 | 4 | 0 | 0 | 1 | 13 |
| V14 Protección de datos | 6 | 3 | 0 | 0 | 0 | 9 |
| V15 Codificación y arquitectura seguras | 11 | 2 | 0 | 0 | 0 | 13 |
| V16 Registro de seguridad y manejo de errores | 13 | 3 | 0 | 0 | 0 | 16 |
| V17 WebRTC | 0 | 0 | 0 | 7 | 0 | 7 |
| **Total** | **157** | **24** | **0** | **64** | **8** | **253** |

## Brechas por prioridad

Solo lo que sigue abierto. Las marcadas «abierta a propósito» son decisiones tomadas: quedan en la lista con su motivo hasta que se revisen.

### Alta

1. **El token de sesión también viaja en el cuerpo** del login y del refresh, y el frontend lo usa como Bearer (V3.3.4). Un XSS podría robarlo aunque la cookie sea HttpOnly. Abierta a propósito: el frontend lo guarda solo en memoria (no en `localStorage`) y lo necesita como Bearer; quitarlo exige pasar el frontend a usar solo la cookie, en los dos repositorios a la vez.
2. **Verificación en dos pasos opcional para el resto del personal** (V6.3.3). Ya es obligatoria para la plataforma y para quien administra el personal (el encargado y todo rol con `staff.manage`). Para meseros, cocina y cajeros sigue siendo opcional, a propósito: `docs/autenticacion.md` («Política») explica por qué (equipos compartidos en locales pequeños, compensados con el bloqueo por PIN, el cierre de sesiones a distancia, el límite de intentos y la reautenticación). Queda abierto exigirla a los roles con facturación sin `staff.manage`.

### Media

3. **Rol de base de datos y SIEM** (V13.2.2, V16.4.2, V16.4.3). Abierta a propósito, porque es infraestructura y no código. Hacen falta:
   - un rol de aplicación sin DDL, separado del de migraciones: hoy la app es dueña de las tablas y podría quitar los triggers de solo inserción (incluido el de `TRUNCATE` de la `0044`), y `obs_*` es modificable. El código ya lo admite (`MIGRATION_DATABASE_URL`), y `docs/migracion-supabase.md` con `scripts/supabase_roles.sql` crea `resthub_app` sin DDL y `resthub_backup` de solo lectura: se cierra al aplicar esa guía;
   - un colector de logs externo, con sus alertas.
4. **TLS hacia PostgreSQL sin exigir** (V12.3.1, V12.3.2). `DATABASE_SSL_MODE` ya existe, pero vale `prefer` por omisión y con `DEBUG=false` solo avisa al arrancar. Fijar `verify-full` en el despliegue: con Supabase, con su certificado y «Enforce SSL» (`docs/migracion-supabase.md`, paso 3).
5. **La cola sin conexión cuando la sesión vence sola** (V14.3.1, V14.3.3). Cerrar la sesión ya borra los pedidos con datos del cliente, pero si la sesión vence (o se cierra el navegador), quedan en `localStorage` sin cifrar hasta que la cuenta vuelve y los envía. Cerrarla del todo pide cifrarlos con una llave que dé el servidor al entrar.
6. **Las cookies de antes del prefijo `__Host-`** (V3.3.3). El API todavía lee los nombres sin prefijo, para no cortar las sesiones abiertas y porque la E2E del frontend los inyecta. Quitar esa lectura cuando ya no queden sesiones de antes, cambiando la E2E para que entre por el navegador.

### Baja o de documentación

7. **El DNI/RUC y el teléfono viajan en la URL** (`/lookup/dni/{dni}`, `/lookup/ruc/{ruc}`, `GET /customers?q=`) (V14.2.1). Abierta a propósito: son consultas GET de solo lectura, el panel guarda la plantilla de la ruta y no el número (`route_template`), las respuestas llevan `no-store` y `Referrer-Policy: no-referrer`. En los logs de la app el número ya se tapa (`core/redaction.py`); queda el del proxy de Dokploy (y de Cloudflare, si se usa), que guarda la ruta entera.
8. **`JWT_SECRET_KEY` sin rotación gradual** (V11.2.2): no hay `kid` ni secreto anterior, así que cambiarlo corta todas las sesiones (`docs/criptografia.md`).
9. **Detalles:**
    - No se consulta un conjunto amplio de contraseñas filtradas, como el rango k-anónimo de HIBP (V6.2.12).
    - No hay un proceso ni un endpoint para reiniciar la verificación en dos pasos si se pierden el celular y los códigos (V6.4.4).
    - Los 422 de validación no tienen evento propio (V16.3.3).
    - La imagen copia `scripts/` (V15.2.3).
    - Otros parciales sin cambios en esta revisión: V3.5.4, V8.1.2, V11.2.3, V11.4.1, V11.6.1, V13.2.1, V13.2.5, V13.3.1 y V15.1.3 (ver su fila).

## Detalle por capítulo

### V1 Codificación y saneamiento

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V1.1.1 | L2 | Cumple | Starlette/FastAPI decodifican URL y JSON una sola vez antes de Pydantic. El base64 de foto y certificado se decodifica una vez, con `validate=True`, antes de validar: backend:`modules/menu/adapters/api/router.py` (`set_photo`), backend:`modules/billing/adapters/api/router.py` (`upload_certificate`). |
| V1.1.2 | L2 | Cumple | La codificación ocurre al final: React escapa en el render (frontend sin `dangerouslySetInnerHTML`), FastAPI serializa JSON y `compose_email` aplica `html.escape` al armar el cuerpo (backend:`modules/billing/use_cases/delivery.py`). |
| V1.2.1 | L1 | Cumple | El API solo responde JSON o archivos con tipo fijo y `nosniff` (backend:`core/security_headers.py`, `core/file_response.py`). `Content-Disposition` según RFC 6266. Frontend: JSX escapa el texto y no hay `innerHTML`. Test: `test_every_api_response_carries_security_headers`. |
| V1.2.2 | L1 | Cumple | `fill()` aplica `encodeURIComponent` a los parámetros de ruta (frontend:`src/services/paths.ts`) y openapi-fetch arma las llamadas. Los únicos `href` dinámicos son `tel:` (DeliveryCard), `otpauth_uri` que genera el servidor y `appUrl` interno. Backend: RUC/DNI se validan como dígitos antes de ir a la URL de Factiliza/Lycet. |
| V1.2.3 | L1 | Cumple | El JSON sale siempre de serializadores (`JSONResponse`, `httpx json=`, `json.dumps` en backend:`billing/adapters/archive/google_drive.py`; `JSON.stringify` en frontend). No se concatena JS ni JSON a mano. |
| V1.2.4 | L1 | Cumple | ORM SQLAlchemy y `text()` con parámetros enlazados. Los f-strings de backend:`platform/adapters/persistence/directories.py` solo juntan constantes. Las búsquedas LIKE de clientes, personal y telemetría escapan `%`, `_` y la barra invertida con backend:`core/sql_like.py` (tests en `tests/test_like_escaping.py`). Además Bandit y Semgrep corren en CI. |
| V1.2.5 | L1 | Cumple | `src/` no usa `subprocess` ni `os.system`. Los scripts (`scripts/backup_db.py`, `scripts/measure_carbon.py`) pasan argumentos fijos en lista y sin shell. Ruff `S` y Bandit (`.github/workflows/ci.yml`). |
| V1.2.6 | L2 | No aplica | No hay LDAP. |
| V1.2.7 | L2 | No aplica | No hay XPath ni parser XML. Lycet, un servicio externo, firma y procesa el XML. |
| V1.2.8 | L2 | No aplica | No hay LaTeX. Lycet genera los PDF. |
| V1.2.9 | L2 | Cumple | La única regex armada con datos del usuario usa `re.escape` (backend:`modules/insights/domain/privacy.py`). La `RegExp` del frontend (`src/components/formRules.ts`) solo interpola una constante numérica. Tests en `tests/test_ai_privacy.py`. |
| V1.3.1 | L1 | No aplica | No hay editor WYSIWYG ni entrada HTML: todo campo es texto plano y React lo escapa. |
| V1.3.2 | L1 | Cumple | No hay `eval`, `exec` ni `new Function` en backend ni en frontend. Ruff `select=ALL` (incluye S307), Bandit y `eslint-plugin-security` (frontend:`eslint.config.js`). |
| V1.3.3 | L2 | Cumple | Lo que va a la IA se enmascara (`insights/domain/privacy.py`) y lo que va a logs se redacta (`core/redaction.py`). A Factiliza solo llegan dígitos validados (`lookup/domain/documents.py`) y el correo sale escapado. Los textos se recortan por largo en el dominio (`_short_text`, `_text`). |
| V1.3.4 | L2 | No aplica | No se acepta SVG: `detect_image_type` (backend:`menu/domain/photos.py`) solo admite JPEG, PNG y WebP por firma. |
| V1.3.5 | L2 | No aplica | No se renderiza Markdown, CSS, XSL ni BBCode del usuario. La apariencia se lee contra listas cerradas (frontend:`src/services/appearanceRules.ts`, `pick`). |
| V1.3.6 | L2 | Cumple | Toda URL de salida es fija y sale de `Settings` (`core/config.py`: Factiliza, Lycet, Brevo, Drive, OpenRouter). Lo que aporta el usuario son dígitos RUC/DNI validados antes de la llamada. Test: `test_use_case_checks_format_and_quota_before_calling_the_provider`. |
| V1.3.7 | L2 | Cumple | No hay motor de plantillas (no está jinja2 en `pyproject.toml`). El correo se arma con un f-string fijo y valores escapados (`compose_email`). |
| V1.3.8 | L2 | No aplica | Stack Python/TS: no hay JNDI. |
| V1.3.9 | L2 | No aplica | No hay memcache. Las cachés viven en la memoria del proceso. |
| V1.3.10 | L2 | Cumple | No hay `str.format` ni `%` con plantilla del usuario: el único `.format` usa una constante (`billing/adapters/sunat/lycet.py:328`). structlog trabaja con pares clave-valor. |
| V1.3.11 | L2 | Cumple | No usa SMTP: Brevo por HTTPS con cuerpo JSON (`billing/adapters/email/brevo.py`). El destinatario es `EmailStr` con tope de 254 (`SendInvoiceEmailRequest`), el asunto se recorta a 200 y el HTML se escapa. |
| V1.4.1 | L2 | No aplica | Lenguajes con memoria gestionada (Python, TypeScript). No hay código no gestionado propio. |
| V1.4.2 | L2 | Cumple | `PathId`/`EntityId` y `MAX_OFFSET` acotan los enteros al rango de la base (backend:`core/api_params.py`, `core/pagination.py`). La telemetría vacía los enteros de más de 32 bits. Tests en `tests/test_api_bounds.py` más Schemathesis en CI. |
| V1.4.3 | L2 | No aplica | Memoria gestionada por el runtime. |
| V1.5.1 | L1 | No aplica | No se parsea XML en backend ni en frontend. El CDR (zip) se guarda y se entrega sin abrirlo. |
| V1.5.2 | L2 | Cumple | Solo JSON (`json.loads`, Pydantic). No hay pickle, yaml ni marshal. JWT con `algorithms=[...]` fijo (`core/tokens.py:138`). El almacenamiento local del frontend se lee con desconfianza (`parseAppearance`, `offlineQueue.ts`). |

### V2 Validación y lógica de negocio

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V2.1.1 | L1 | Cumple | backend:`docs/validacion.md` cataloga las reglas por entidad (límites, formatos y reglas entre campos, sacados del dominio y de los `*Request`) y los archivos aceptados. El README suma formatos sueltos (RUC/DNI, PIN de 4 a 6 dígitos, contraseñas, certificado, `window`). |
| V2.1.2 | L2 | Cumple | backend:`docs/validacion.md` documenta por entidad las combinaciones válidas: `dine_in` exige mesa, el delivery exige nombre, teléfono y dirección, la factura exige RUC y razón social, la boleta exige documento por encima de S/ 700, las reservas no se cruzan. |
| V2.1.3 | L2 | Cumple | El README documenta límites por cuenta (`EMAIL_MAX_PER_HOUR`, `LOOKUP_PER_MINUTE`, tope de descuento del mesero, DLP de 20 fichas en 10 min) y globales (`RATE_LIMIT_PER_MINUTE`, intentos de acceso 5/25, umbrales `SECURITY_ALERT_*`). |
| V2.2.1 | L1 | Cumple | Pydantic (`Field`, `EmailStr`, enums) más validación en el dominio (regex de RUC, serie, teléfono de cliente). El teléfono de pedidos, delivery y reservas sigue la regla de clientes (`PHONE_PATTERN`, backend:`core/api_params.py`). Tests: `test_delivery_phone_follows_the_customer_rule`, `test_reservation_phone_follows_the_customer_rule`. |
| V2.2.2 | L1 | Cumple | Todas las reglas viven en el dominio y los casos de uso del backend (Import Linter). El zod del frontend solo ayuda a la usabilidad. Schemathesis (`scripts/fuzz_api.sh`) ataca el API directamente. |
| V2.2.3 | L2 | Cumple | La reserva exige que el cliente sea del local (404) y no se cruce. El delivery exige sus campos (`_ensure_deliverable`). La factura exige RUC. El rango de fechas de insights es de 366 días como máximo. Tests: `test_cannot_reserve_with_a_customer_of_another_restaurant`, `test_cannot_reserve_in_the_past_or_at_a_foreign_table`. |
| V2.3.1 | L1 | Cumple | Máquina de estados del pedido (open → in_kitchen → ready → served → paid). 2FA en dos pasos, setup y luego confirm. `must_change_password` bloquea el resto del API. Tests: `test_transitions_that_skip_steps_are_rejected` (`tests/test_orders_domain.py`), `test_invalid_transition_responds_409`. |
| V2.3.2 | L2 | Cumple | Tope de descuento (`orders.discount_any`), cuota de correo y de lookup, límite DLP. Tests: `test_quota_limits_each_account_per_minute` (`tests/test_lookup.py`), `tests/test_api_customers_dlp.py`, `test_waiter_cannot_charge_anothers_order_but_owner_can`. |
| V2.3.3 | L2 | Cumple | Una transacción por petición, con commit o rollback (`get_session`, backend:`core/database.py`). El alta de un restaurante es una sola transacción. Test: `test_order_is_committed_to_database_before_responding` (`tests/test_resilience_base.py`). |
| V2.3.4 | L2 | Cumple | `SELECT … FOR UPDATE` en pedidos, caja y reservas (`OrderRepository.get(for_update=True)`, `reservations/use_cases/manage_reservations.py`), índices únicos (número de pedido, `client_request_id`, un sandbox activo) y canje atómico del código de vista previa. Test: `test_same_table_is_not_reserved_twice_at_once`. |
| V2.4.1 | L2 | Cumple | Límite por IP (`core/rate_limit.py`), intentos de acceso en memoria y en base (`core/login_throttle.py`, `core/login_attempts.py`), cuotas de correo y lookup, tope de telemetría y plazo por petición. Tests en `tests/test_security_headers_rate_limit.py`, `tests/test_login_concurrency.py`. Nota: el límite general vive en la memoria de cada proceso. |

### V3 Seguridad del frontend web

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V3.2.1 | L1 | Cumple | backend: `core/security_headers.py` pone `X-Content-Type-Options: nosniff` y CSP `default-src 'none'` en toda respuesta; fotos validadas por bytes (`menu/domain/photos.py` `detect_image_type`). Test `test_every_api_response_carries_security_headers`, `test_invalid_photos_are_rejected`. PDF/XML van `inline` (`core/file_response.py`), sin `sandbox`. |
| V3.2.2 | L1 | Cumple | frontend: React (JSX escapa texto); cero usos de `dangerouslySetInnerHTML`, `innerHTML`, `insertAdjacentHTML`, `eval` en `src/`. `eslint-plugin-security` en error (`eslint.config.js`). |
| V3.3.1 | L1 | Cumple | backend: `core/session_cookie.py` `_set` con `secure=settings.session_cookie_secure` (por omisión true; test `test_login_sets_an_httponly_secure_cookie`), y `core/config.py` rechaza `SESSION_COOKIE_SECURE=false` con `DEBUG=false` (test `test_production_rejects_a_session_cookie_without_secure`). Con `Secure`, las tres cookies llevan el prefijo `__Host-` (`cookie_name`; test `test_the_cookies_carry_the_host_prefix`). |
| V3.3.2 | L2 | Cumple | backend: `SameSite=None` deliberado (frontend en Vercel y API en el VPS son sitios distintos, `core/config.py` `session_cookie_samesite`), compensado con doble envío `X-CSRF-Token` (`token_from_cookie`). Test `test_changes_with_the_cookie_require_the_csrf_header`. |
| V3.3.3 | L2 | Cumple | backend: `core/session_cookie.py` escribe `__Host-resthub_session`, `__Host-resthub_platform_session` y `__Host-resthub_csrf` con `path="/"`, `Secure` y sin `Domain` (test `test_the_cookies_carry_the_host_prefix`). Mientras queden sesiones abiertas antes del cambio también lee el nombre sin prefijo, y al escribir o borrar una cookie borra el viejo (`read_cookie`; test `test_a_session_from_before_the_prefix_still_works`). La E2E del frontend inyecta las de antes porque Chromium no deja poner a mano una `__Host-` (frontend:`e2e/support/fixtures.ts`): quitar la lectura del nombre viejo pide cambiar ese arreglo. |
| V3.3.4 | L2 | Parcial | backend: cookie `HttpOnly` (test `test_login_sets_an_httponly_secure_cookie`), pero el mismo token sale en el cuerpo (`StaffAccessTokenResponse.access_token`, `accounts/adapters/api/schemas.py`; también en `/auth/refresh`). frontend lo guarda en memoria y lo manda como Bearer (`services/apiCredentials.ts`, `store/session.ts`). Abierto a propósito: ver «Brechas por prioridad». |
| V3.4.1 | L1 | Cumple | backend: `HSTS = "max-age=63072000; includeSubDomains"` en `core/security_headers.py` (test `test_every_api_response_carries_security_headers`). frontend: misma cabecera en `vercel.json` para `/(.*)`. |
| V3.4.2 | L1 | Cumple | backend: `main.py` `CORSMiddleware` con `allow_origins` explícitos y `allow_credentials`; regex solo en `develop`, validada entera (`core/config.py`). Tests `test_cors_is_read_as_json_or_comma_separated`, `test_cors_accepts_origins_matching_the_pattern`. |
| V3.4.3 | L2 | Cumple | backend: `API_CSP` con `default-src 'none'; base-uri 'none'`. frontend `vercel.json`: `object-src 'none'`, `script-src 'self'` y `base-uri 'none'` (test frontend:`src/services/securityHeaders.test.ts`). Queda `style-src 'unsafe-inline'` por el widget Sienna, que el requisito no prohíbe. |
| V3.4.4 | L2 | Cumple | backend: `nosniff` en `_HEADERS` y en `inline_file_response`; test `test_every_api_response_carries_security_headers`, `test_interactive_docs_do_not_carry_the_api_csp`. frontend: `vercel.json` `X-Content-Type-Options: nosniff`. |
| V3.4.5 | L2 | Cumple | backend: `Referrer-Policy: no-referrer` (`core/security_headers.py`, testeado). frontend: `strict-origin-when-cross-origin` en `vercel.json`; código de vista previa en el fragmento (`features/platform/previewLink.ts`). |
| V3.4.6 | L2 | Cumple | backend: `frame-ancestors 'none'` en `API_CSP` (testeado). frontend: `frame-ancestors 'none'` en la CSP de `vercel.json` (más `X-Frame-Options: DENY`). Única excepción: `/docs` y `/redoc`, solo con `DEBUG=true`. |
| V3.5.1 | L1 | Cumple | backend: con cookie, POST/PUT/PATCH/DELETE exigen `X-CSRF-Token` = cookie `resthub_csrf` con `hmac.compare_digest` (`core/session_cookie.py` `token_from_cookie`). Tests `test_changes_with_the_cookie_require_the_csrf_header`, `test_bearer_header_does_not_need_csrf`. |
| V3.5.2 | L1 | Cumple | backend: no depende solo del preflight: la cabecera `X-CSRF-Token` (no safelisted) es obligatoria aunque la petición sea "simple"; Bearer no lo manda el navegador solo (`core/auth.py`). Mismo test que V3.5.1. |
| V3.5.3 | L1 | Cumple | backend: las acciones sensibles son POST/PUT/PATCH/DELETE (README «Endpoints»; `SAFE_METHODS` no cambian estado y no piden CSRF). No se hallaron GET que modifiquen datos de negocio. |
| V3.5.4 | L2 | Parcial | Frontend (Vercel) y API (VPS con Dokploy) en hostnames distintos. Pero la consola de plataforma (`/plataforma`, `services/paths.ts`) y la app del personal comparten el mismo origen del frontend; solo se separan por cookie/alcance. |
| V3.5.5 | L2 | No aplica | frontend: no hay `postMessage` ni listeners `message` en `src/` (solo el service worker generado por Workbox). |
| V3.7.1 | L2 | Cumple | frontend: React 19, Vite 8, sin plugins NSAPI/Flash/Java; widget Sienna autoalojado (`vite.plugins.ts` `handleSiennaSelfHosted`). `pnpm audit --audit-level high` en `.github/workflows/ci.yml` y Dependabot. |
| V3.7.2 | L2 | Cumple | frontend: las redirecciones automáticas son rutas internas (`services/leaveTab.ts` `appUrl`, `features/auth/previewEntry.ts`); no hay parámetro `next`/`returnTo`. backend: sin `RedirectResponse`. |

### V4 Seguridad de API y servicios web

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V4.1.1 | L1 | Cumple | backend: JSON por `JSONResponse` (`application/json`), SSE `text/event-stream` con charset por Starlette y el XML firmado como `application/xml; charset=utf-8` (`billing/use_cases/invoicing.py` → `core/file_response.py`). |
| V4.1.2 | L2 | Fuera del código | backend: la app no redirige a HTTPS (sin `HTTPSRedirectMiddleware`), correcto para un API. La redirección HTTP→HTTPS la deciden el proxy de Dokploy en el VPS y el borde de Vercel, no los repositorios. |
| V4.1.3 | L2 | Cumple | backend: `core/auth.py` `client_address` toma la última IP de `X-Forwarded-For` (test `test_counts_by_the_ip_the_proxy_appends`). El `Dockerfile` arranca uvicorn con `${FORWARDED_ALLOW_IPS:-127.0.0.1}` en vez de `*`, y con `DEBUG=false` y `*` se avisa al arrancar (`main.py`; tests `test_uvicorn_trusts_only_localhost_by_default`, `test_trusting_any_proxy_outside_development_is_warned_at_startup`). Supuesto documentado en el README: `CLIENT_IP_HEADER` solo es de fiar si todo el tráfico pasa por el WAF. |
| V4.2.1 | L2 | Fuera del código | Uvicorn (h11) detrás del proxy de Dokploy; el framing HTTP lo resuelven el servidor y el borde, no el código de la app. |
| V4.3.1 | L2 | No aplica | No hay GraphQL (ni dependencia ni rutas) en backend ni frontend. |
| V4.3.2 | L2 | No aplica | No hay GraphQL. |
| V4.4.1 | L1 | No aplica | No hay WebSocket: el tiempo real es SSE (`core/events_router.py`, «no hace falta un WebSocket»). |
| V4.4.2 | L2 | No aplica | Sin WebSocket (SSE por `GET /events`, sujeto a CORS). |
| V4.4.3 | L2 | No aplica | Sin WebSocket; el SSE usa la sesión normal (`StreamPrincipalDep`). |
| V4.4.4 | L2 | No aplica | Sin WebSocket. |

### V5 Manejo de archivos

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V5.1.1 | L2 | Cumple | backend:`docs/validacion.md` («Archivos») documenta la foto (JPEG/PNG/WebP, 400 KB) y el certificado (.pfx/.p12/.pem, 64 KB): cómo se comprueba el tipo, dónde se guarda, cómo vuelve a salir y qué pasa con un archivo malicioso. |
| V5.2.1 | L1 | Cumple | Topes por función: foto de 400 KB (`MAX_PHOTO_BYTES` y `max_length` del base64) y certificado de 64 KB. Además, backend:`core/body_limit.py` responde 413 `request_too_large` cuando `Content-Length` o los bytes leídos pasan de `MAX_REQUEST_BODY_BYTES` (1 MiB por omisión), con o sin sesión. Tests en `tests/test_body_limit.py`. |
| V5.2.2 | L1 | Cumple | El tipo de la foto sale de los bytes mágicos, no del declarado (`detect_image_type`). El frontend reduce la foto y la vuelve a codificar en un canvas (frontend:`src/features/menu/dishPhotoFile.ts`). El certificado se abre con `cryptography` (`sunat/certificates.py`). Tests: `test_image_type_comes_from_the_bytes`, `test_invalid_photos_are_rejected`. |
| V5.2.3 | L2 | No aplica | No se aceptan archivos comprimidos del usuario. El CDR zip llega de Lycet y nunca se descomprime. |
| V5.3.1 | L1 | Cumple | Las fotos se guardan en la base (`menu_item_photos`), no en una carpeta pública. Se sirven por el API con permiso, tipo fijo, `nosniff` y una CSP que no deja ejecutar nada (`core/security_headers.py`). |
| V5.3.2 | L1 | Cumple | No se arman rutas del sistema de archivos con datos del usuario. Los nombres salen de `document_filename` (serie validada) y las carpetas de Drive se crean por API, sin rutas (`billing/adapters/archive/google_drive.py`). |
| V5.4.1 | L2 | Cumple | El nombre lo genera el servidor (`document_filename`) y siempre va en `Content-Disposition` (`core/file_response.py`). Tests: `tests/test_billing.py:734`, `tests/test_api_billing_voiding.py:251`. La foto se sirve inline, como imagen y sin nombre. |
| V5.4.2 | L2 | Cumple | `_content_disposition` deja un fallback ASCII saneado más `filename*=UTF-8''` codificado (RFC 6266). El adjunto del correo usa el mismo nombre generado. Falta un test con caracteres peligrosos. |
| V5.4.3 | L2 | Cumple | No hay antivirus, pero la foto se vuelve a codificar en el servidor: Pillow la decodifica (solo JPEG, PNG o WebP), aplica la orientación, quita los metadatos, la reduce a 800 px y la guarda como WebP (backend:`menu/adapters/images/pillow_photo_encoder.py`); lo que no se decodifica da 422. El certificado nunca se vuelve a servir. Tests: `test_undecodable_and_polyglot_photos_are_rejected`, `test_a_polyglot_photo_is_stored_without_its_payload`. |

### V6 Autenticación

Revisado sobre backend `f9b3309` y frontend `a425ad0` (rama `asvs-front`).

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V6.1.1 | L1 | Cumple | backend:README.md «Acceso: intentos y renovación» y «Cabeceras de seguridad y límite de peticiones» (5 fallos por correo+IP, 25 por correo, 15 min, `RATE_LIMIT_PER_MINUTE`, WAF). El docstring de backend:src/resthub/core/login_throttle.py explica por qué el tope por correo es más alto (contra el bloqueo malicioso). |
| V6.1.2 | L2 | Cumple | backend:README.md («Palabras del contexto») documenta la lista: «resthub», el nombre y el identificador del local, la parte del correo antes de la `@` y el nombre de la persona (`context_words`, backend:src/resthub/core/credentials.py). |
| V6.1.3 | L2 | Cumple | backend:docs/autenticacion.md reúne en un inventario único cada vía (`/auth/login`, `/platform/auth/login` obsoleta, `/auth/preview`, desbloqueo con PIN, reautenticación, `/auth/refresh`, altas y restablecimientos) con su fuerza y sus controles. |
| V6.2.1 | L1 | Cumple | `MIN_PASSWORD_LENGTH = 10` en backend:src/resthub/core/credentials.py (`password_problem`); frontend:src/services/fieldLimits.ts. Test `test_new_password_has_a_minimum` y `test_short_new_password_is_rejected`. No llega a los 15 recomendados. |
| V6.2.2 | L1 | Cumple | El personal y la plataforma la cambian con `POST /auth/me/password` (backend:src/resthub/modules/accounts/adapters/api/auth_router.py; tests `test_change_own_password`, `test_a_platform_account_changes_its_own_password`). |
| V6.2.3 | L1 | Cumple | `ChangeOwnPassword` verifica `current_password` antes de cambiarla (backend:src/resthub/modules/accounts/use_cases/change_own_password.py); `ChangeOwnPasswordRequest` pide las dos. Test `test_changing_password_requires_current_one`. |
| V6.2.4 | L1 | Cumple | Se comprueba al registrar, cambiar y restablecer (`validate_new_password`), con test `test_new_password_rejects_common_passwords`. La lista (backend:src/resthub/core/common_passwords.txt) tiene unas 12 000 entradas: las 10 000 más usadas de SecLists y las de 10 o más caracteres del top 100k. |
| V6.2.5 | L1 | Cumple | `password_problem` solo mira el largo y la lista de comunes, y `passwordRule` (frontend:src/services/fieldRules.ts) solo el mínimo y el máximo. No exige ningún tipo de carácter. |
| V6.2.6 | L1 | Cumple | frontend:src/components/PasswordField.tsx usa `type="password"` con un botón Mostrar/Ocultar; el PIN también va en `type="password"` (frontend:src/features/shell/security/PinUnlockForm.tsx). |
| V6.2.7 | L1 | Cumple | No hay `onPaste` ni bloqueo de pegado en `src/`. Los campos llevan `autoComplete="current-password"`/`"new-password"` (LoginFields.tsx, ChangePasswordFields.tsx, StaffPasswordForm.tsx), así que los gestores de contraseñas funcionan. |
| V6.2.8 | L1 | Cumple | Ni el esquema Pydantic ni zod hacen `strip` o cambian mayúsculas en la contraseña, y Argon2id verifica el texto tal cual. Test `test_valid_password_is_returned_as_is` (los espacios se conservan). Las minúsculas se usan solo para comparar con la lista de comunes. |
| V6.2.9 | L2 | Cumple | Se aceptan de 10 a 128 caracteres, contados como caracteres y no como bytes: ya no hay tope de 72 bytes (`MAX_PASSWORD_LENGTH`, backend:src/resthub/core/credentials.py). Tests `test_password_over_72_bytes_is_accepted`, `test_new_password_accepts_short_shared_words_and_long_unicode`. |
| V6.2.10 | L2 | Cumple | No hay vencimiento ni rotación periódica: no existe ningún campo de expiración en backend:src/resthub/modules/accounts/domain/entities.py ni en credentials.py. Solo se obliga a cambiar la contraseña temporal (`must_change_password`). |
| V6.2.11 | L2 | Cumple | `password_problem` rechaza «resthub», el nombre o slug del local, el correo y el nombre de la cuenta (`context_words`, backend:src/resthub/core/credentials.py) en altas, restablecimientos, cambio propio y plataforma. Tests `test_new_password_rejects_the_context_words`, `test_changing_password_rejects_the_own_email_or_restaurant`, `test_platform_password_cannot_carry_the_account_words`. |
| V6.2.12 | L2 | Parcial | La lista local sale de corpus de filtraciones (SecLists, unas 12 000). No se consulta un conjunto amplio de contraseñas filtradas (por ejemplo, el rango k-anónimo de HIBP o un corpus local grande). |
| V6.3.1 | L1 | Cumple | Hay un throttle en memoria y otro en BD compartido entre réplicas (backend:src/resthub/core/login_throttle.py, login_attempts.py), más `core/rate_limit.py`; la reautenticación usa el mismo contador. Tests `test_five_wrong_passwords_lock_that_email`, `test_rotating_ip_does_not_evade_the_per_email_cap`, `test_simultaneous_burst_evaluates_at_most_five_passwords`. |
| V6.3.2 | L1 | Cumple | Las migraciones no insertan cuentas. Las cuentas demo (`resthub123`) solo se siembran con `DEBUG` y una BD local, o con `ALLOW_DEMO_SEED` (backend:scripts/seed_dev.py). Las cuentas de plataforma se crean con `create_platform_admin.py --generate`. |
| V6.3.3 | L2 | Parcial | TOTP obligatorio para la plataforma (`platform_two_factor_required=True`, test `test_without_it_the_platform_answers_403_and_me_says_why`) y para quien administra el personal de un local, el encargado y todo rol con `staff.manage` (`staff_two_factor_required=True`, `ensure_second_factor` en `core/auth.py`; tests de `tests/test_staff_two_factor.py`). Para el resto del personal es opcional: backend:docs/autenticacion.md («Política») documenta el porqué. Abierto a propósito: ver «Brechas por prioridad». |
| V6.3.4 | L2 | Cumple | Las vías están documentadas (backend:docs/autenticacion.md) y la 2FA se exige en las dos de login (`test_a_platform_account_uses_it_in_both_logins`). La obsoleta `/platform/auth/login` usa el mismo `LoginGuard` que `/auth/login`, así que comparte el contador (test `test_old_platform_login_shares_the_attempts_of_the_unified_login`). |
| V6.4.1 | L1 | Cumple | Las temporales cumplen la política, `must_change_password` bloquea la API hasta cambiarlas (test `test_reset_by_the_manager_is_temporary`) y vencen a las `TEMPORARY_PASSWORD_HOURS` (72): 401 `temporary_password_expired` (tests `test_a_staff_temporary_password_expires`, `test_an_expired_platform_temporary_password_does_not_sign_in`). La que genera `create_platform_admin.py --generate` sale de `secrets.token_urlsafe` y también es temporal (test `test_register_admin_marks_a_temporary_password`). Las que fija el encargado las dicta una persona, no el sistema. |
| V6.4.2 | L1 | Cumple | No hay pistas ni preguntas secretas en el backend ni en el frontend; la recuperación es asistida por el encargado (frontend:src/features/auth/LoginView.tsx). |
| V6.4.3 | L2 | Cumple | No hay autoservicio. `ResetStaffPassword` (necesita `staff.manage` y no puede restablecer cuentas con más permisos) y `ResetOwnerPassword` (plataforma) fijan una temporal y cortan las sesiones. No tocan `two_factor_credentials`, así que la 2FA se sigue pidiendo. Falta un test que lo pruebe y un camino para las cuentas de plataforma. |
| V6.4.4 | L2 | Parcial | Si se pierde el celular, se entra con los códigos de recuperación (backend:src/resthub/core/two_factor.py). Si también se pierden, no hay un proceso documentado ni un endpoint para que el encargado o la plataforma reinicien la 2FA tras verificar la identidad: solo queda tocar la BD. |
| V6.5.1 | L2 | Cumple | Se guarda `last_step` y se borra el hash del código de recuperación usado (tests `test_a_code_tolerates_one_step_and_is_not_reused`, `test_a_recovery_code_works_once`). `SqlTwoFactorStore.verify` lee la credencial con `FOR UPDATE` y gasta el paso o el código con una actualización condicionada a lo leído (tests `test_a_totp_spent_by_a_concurrent_login_does_not_work_twice`, `test_a_recovery_code_spent_by_a_concurrent_login_does_not_work_twice`). |
| V6.5.2 | L2 | Cumple | Los códigos nuevos tienen 28 caracteres de un alfabeto de 31 (unos 139 bits) y se guardan como SHA-256 con prefijo `v3:`, que alcanza con 112 bits o más (`hash_recovery`, backend:src/resthub/core/two_factor.py; test `test_new_recovery_codes_have_at_least_128_bits`). Los de antes (24 caracteres con `v2:` y 10 sin prefijo) siguen valiendo hasta renovarlos y el estado lo avisa (`recovery_codes_outdated`). |
| V6.5.3 | L2 | Cumple | `new_secret` usa `secrets.token_bytes(20)` y `new_recovery_codes` usa `secrets.choice` (backend:src/resthub/core/two_factor.py). |
| V6.5.4 | L2 | Cumple | Los códigos de recuperación tienen unos 139 bits y el TOTP 6 dígitos, por encima de los 20 bits pedidos. No hay códigos fuera de banda (OOB). |
| V6.5.5 | L2 | Cumple | `STEP_SECONDS = 30` con una tolerancia de ±1 paso (`DRIFT_STEPS`, RFC 6238) en backend:src/resthub/core/two_factor.py; test `test_codes_follow_rfc_6238`. No hay OOB. |
| V6.6.1 | L2 | No aplica | No hay OTP por SMS ni por teléfono; la 2FA es solo TOTP. |
| V6.6.2 | L2 | No aplica | No hay autenticación fuera de banda. El código de vista previa (`/auth/preview`) es un traspaso interno de un solo uso que vence en 60 s, no un segundo factor. |
| V6.6.3 | L2 | No aplica | No hay códigos OOB. El TOTP equivocado sí cuenta como un intento fallido del throttle de login. |
| V6.8.1 | L2 | No aplica | No hay proveedores de identidad externos (ni OIDC, ni SAML, ni login social); Google solo se usa para los respaldos en Drive. |
| V6.8.2 | L2 | No aplica | No hay aserciones de un IdP. El JWT propio se verifica con un algoritmo fijo (`jwt.decode(..., algorithms=[...])`, backend:src/resthub/core/tokens.py), pero eso corresponde a V9. |
| V6.8.3 | L2 | No aplica | No se usa SAML. |
| V6.8.4 | L2 | No aplica | No hay un IdP externo, así que no hay `acr`, `amr` ni `auth_time` que validar. |

### V7 Gestión de sesiones

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V7.1.1 | L2 | Cumple | Documentados en backend:README.md: vida del token (`ACCESS_TOKEN_TTL_SECONDS`, 1 h), bloqueo por inactividad (RFS-29, 10 min) y vida máxima absoluta (`SESSION_ABSOLUTE_LIFETIME_HOURS`, 12 h), justificada con el tope de NIST SP 800-63B para AAL2. |
| V7.1.2 | L2 | Cumple | backend:README.md (RFS-17 y «Sesiones a la vez») describe una sesión por dispositivo, listarlas y cerrarlas. `MAX_SESSIONS_PER_ACCOUNT` (10) cierra la usada hace más tiempo al abrir una de más (backend:src/resthub/core/account_sessions.py; test `test_opening_one_session_too_many_closes_the_least_recently_used`). |
| V7.1.3 | L2 | No aplica | No hay SSO ni identidad federada: el acceso es local (`POST /auth/login`, contraseña y TOTP). |
| V7.2.1 | L1 | Cumple | Toda verificación en el backend: backend:src/resthub/core/auth.py (`_resolve_principal`, firma, `sid`, cuenta). El frontend solo lee `exp` para la interfaz (frontend:src/services/tokenExpiry.ts). |
| V7.2.2 | L1 | Cumple | JWT firmado por sesión, con `sid` aleatorio y fila en `account_sessions` (backend:src/resthub/core/tokens.py, backend:src/resthub/core/account_sessions.py). No hay claves de API estáticas para los usuarios. |
| V7.2.3 | L1 | Cumple | `sid` = `secrets.token_urlsafe(32)` (256 bits, CSPRNG) en backend:src/resthub/core/account_sessions.py (`new_session_id`) y en tokens.py; CSRF también de 32 bytes (session_cookie.py). |
| V7.2.4 | L1 | Cumple | El login emite un token y un `sid` nuevos y cierra la sesión que el navegador traía en su cookie (test `test_login_closes_the_session_the_browser_already_had`). `POST /auth/me/reauthenticate` y `POST /auth/me/unlock` rotan el `sid` con `rotate_session: true` (backend:tests/test_session_rotation.py), y el frontend lo manda en los dos y se queda con el token nuevo (frontend:`src/api/accountSecurity.ts`, `adoptRotatedToken` en `src/store/session.ts`). |
| V7.3.1 | L2 | Cumple | En el servidor: bloqueo 423 tras `idle_lock_minutes` (auth.py `_lock_if_idle`) y sesión muerta tras 1 h sin uso (`SqlSessionRegistry.is_alive`). Tests: backend:tests/test_idle_lock.py, test_account_sessions.py (`test_a_session_unused_longer_than_a_token_is_closed`). Ojo: `idle_lock_minutes=0` lo apaga. |
| V7.3.2 | L2 | Cumple | `POST /auth/refresh` ya no alarga la sesión más allá de `SESSION_ABSOLUTE_LIFETIME_HOURS` (12 h) desde `account_sessions.created_at`: después, la renovación y el resto de la API responden 401 (backend:src/resthub/core/account_sessions.py, auth.py). Test: `test_refresh_does_not_extend_the_session_past_its_absolute_lifetime`. |
| V7.4.1 | L1 | Cumple | Logout revoca la fila (`revoked_at`) y borra cookies; cada petición comprueba el `sid` (auth.py `ensure_open_session`). El token de vista previa abre su propia fila y salir de él lo invalida. Tests: test_account_sessions.py `test_logout_closes_only_this_device`, test_session_termination.py `test_logging_out_of_a_preview_invalidates_its_token`. |
| V7.4.2 | L1 | Cumple | Desactivar fija `sessions_valid_since` y `revoke_all` (accounts/domain/entities.py `deactivate`, staff_router.py); un local desactivado corta a todo su personal (auth.py). No hay borrado de cuentas. Test: backend:tests/test_session_cutoff.py. |
| V7.4.3 | L2 | Cumple | Cambiar o restablecer la contraseña cierra las demás sesiones (auth_router.py `change_own_password`, staff_router.py, restaurants_router.py), y activar o desactivar la 2FA también (two_factor_router.py). Tests: `test_changing_the_password_closes_the_other_devices`, `test_enabling_and_disabling_two_factor_closes_the_other_sessions`. |
| V7.4.4 | L2 | Cumple | «Cerrar sesión» en la barra lateral y en el panel «Más» de todo el shell autenticado (frontend:src/features/shell/SessionActions.tsx, SideNav.tsx, MoreSheet.tsx) y en la pantalla de bloqueo (LockScreen.tsx). |
| V7.4.5 | L2 | Cumple | Quien tiene `staff.manage` cierra una o todas las sesiones de un miembro (backend:src/resthub/modules/accounts/adapters/api/staff_security_router.py; test `test_owner_closes_every_session_of_a_member`). Ante un incidente, backend:scripts/revoke_all_sessions.py cierra las de un local (`--restaurant`) o las de todo el sistema (`--everyone`); tests `test_revoke_sessions_closes_only_that_restaurant`, `test_revoke_sessions_of_everyone`. |
| V7.5.1 | L2 | Cumple | 2FA, PIN, roles y datos fiscales exigen `reauthenticate` reciente (auth.py `require_reauthenticated`, two_factor_router.py); el correo no se puede cambiar. Con la 2FA activa, la reautenticación pide también `otp_code` (401 `two_factor_required`; backend:tests/test_reauthenticate_two_factor.py), y el frontend lo pide en la misma ventana (frontend:src/features/shell/security/ReauthenticationDialog.tsx con `TwoFactorCodeField`, src/api/accountSecurity.ts). |
| V7.5.2 | L2 | Cumple | `GET/DELETE /auth/me/sessions` (security_router.py) y frontend:src/features/auth/security/OwnSessionsCard.tsx. Cerrar las sesiones de otros dispositivos pide la contraseña confirmada (403 `reauthentication_required`; test `test_closing_other_sessions_asks_for_the_password_again`); cerrar la propia, no. |
| V7.6.1 | L2 | No aplica | No hay relación RP/IdP federada. |
| V7.6.2 | L2 | No aplica | No hay creación de sesiones federadas; toda sesión nace de un login explícito o del canje de un código de vista previa de un solo uso. |

### V8 Autorización

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V8.1.1 | L1 | Cumple | `backend:AGENTS.md` («Roles y permisos», «Aislamiento entre restaurantes») y `backend:README.md` («Restaurante, roles y permisos») definen permiso por endpoint, `restaurant_id` del token, 404 para otro local y alcance `platform`. Catálogo en `backend:src/resthub/core/permissions.py`. |
| V8.1.2 | L2 | Parcial | Hay reglas sueltas a nivel de campo: teléfono/correo enmascarados sin `customers.manage`, visibilidad con `orders.read_all`, tope de descuento del mesero (`backend:README.md`). Falta una matriz de campos (lectura y escritura) por permiso y estado de cada recurso. |
| V8.2.1 | L1 | Cumple | `require_permission`/`require_reauthenticated` (`backend:src/resthub/core/auth.py`) en todo endpoint de negocio; `require_platform_permission` en `/platform/*`. Pruebas 403 en `backend:tests/test_api_roles.py`, `test_api_customers_rights.py`. No hay prueba que exija permiso en cada ruta nueva. |
| V8.2.2 | L1 | Cumple | `restaurant_id` sale del token y se contrasta con la base (`_load_identity`); otro local da 404. Pruebas: `backend:tests/test_api_menu.py::test_restaurant_cannot_see_or_touch_another_restaurants_menu`, `test_api_customers_edit.py`, `test_billing_voiding.py` y unas 30 más. |
| V8.2.3 | L2 | Cumple | Esquemas `*Request`/`*Response` explícitos (Pydantic ignora campos extra; ningún request acepta `restaurant_id`); enmascarado por permiso en `backend:src/resthub/modules/customers/adapters/api/router.py` y `POST /customers/{id}/reveal` con bitácora. Prueba: `backend:tests/test_api_customers_dlp.py`. |
| V8.3.1 | L1 | Cumple | Toda decisión está en dependencias FastAPI y casos de uso del servidor; permisos releídos de la base en cada petición (no viajan en el JWT, `backend:src/resthub/core/tokens.py`). El frontend solo oculta la interfaz. |
| V8.4.1 | L2 | Cumple | Filtro por `restaurant_id` del principal, token atado al local (`backend:tests/test_api_auth.py::test_token_with_other_restaurant_is_rejected`), SSE por local (`backend:tests/test_realtime.py::test_event_never_crosses_to_another_restaurant`), vista previa limitada a `is_sandbox`. Sin RLS en la base: depende del código y la revisión. |

### V9 Tokens autocontenidos

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V9.1.1 | L1 | Cumple | `jwt.decode` con clave HMAC verifica la firma antes de leer los claims (backend:src/resthub/core/tokens.py `_verify`). Test: backend:tests/test_tokens.py `test_foreign_signature_is_rejected`. |
| V9.1.2 | L1 | Cumple | `algorithms=[self._algorithm]`, uno solo y simétrico: `jwt_algorithm` es `Literal["HS256"]` (core/config.py). Tests `test_jwt_algorithm_outside_the_closed_list_is_rejected` y `test_token_with_alg_none_is_rejected`. |
| V9.1.3 | L1 | Cumple | La clave sale solo de `JWT_SECRET_KEY` (mín. 32 bytes, se rechaza el valor de desarrollo fuera de DEBUG: config.py `_validate_secret`, test_config.py). PyJWT no usa `jku`, `x5u` ni `jwk`. |
| V9.2.1 | L1 | Cumple | Se emite `exp` y la lectura exige `exp`, `iat` y `sub` (tokens.py, `options={"require": ...}`); no se emite `nbf`. Tests: test_tokens.py `test_expired_token_is_rejected`, `test_token_without_a_required_claim_is_rejected`. |
| V9.2.2 | L2 | Cumple | Solo hay un tipo de token (acceso). El claim firmado `scope` separa restaurante y plataforma y `decode`/`decode_platform` rechazan el ajeno; la vista previa se valida aparte. Tests: test_tokens.py (`test_platform_token_is_not_valid_as_restaurant_token`). |
| V9.2.3 | L2 | Cumple | Cada token lleva `iss` (`JWT_ISSUER`) y un `aud` según su alcance (`JWT_AUDIENCE`, `JWT_PLATFORM_AUDIENCE`), y la lectura los exige (backend:src/resthub/core/tokens.py). Tests: `test_token_with_a_foreign_issuer_or_audience_is_rejected`, `test_restaurant_audience_is_not_valid_on_the_platform`. |
| V9.2.4 | L2 | Cumple | Una misma clave firma dos «audiencias» (restaurante y plataforma), y cada token lleva su `aud` y su `scope` firmados, validados en tokens.py y auth.py. |

### V10 OAuth y OIDC

RestHub no es servidor de autorización, ni proveedor OpenID, ni servidor de recursos OAuth (sus APIs usan JWT propios: ver V9). Sí es **cliente OAuth confidencial** de Google para el respaldo en Drive (backend:scripts/drive_authorize.py y backend:src/resthub/modules/billing/adapters/archive/google_drive.py), con un solo servidor de autorización.

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V10.1.1 | L2 | Cumple | El refresh y el access token de Drive viven solo en el backend (variables `GOOGLE_DRIVE_*`, core/config.py; memoria de `GoogleDriveArchive`); nunca llegan al frontend ni a logs. |
| V10.1.2 | L2 | Cumple | backend:scripts/drive_authorize.py genera `state` (`secrets.token_urlsafe(16)`) y `code_verifier` (64 bytes) por ejecución, comprueba el `state` y canjea con el mismo verificador en el mismo proceso (redirect a 127.0.0.1). |
| V10.2.1 | L2 | Cumple | Flujo de código con PKCE `S256` y `state` comprobado (drive_authorize.py `_pkce`, `_authorize`). |
| V10.2.2 | L2 | No aplica | Un solo servidor de autorización, fijo en el código (accounts.google.com / oauth2.googleapis.com). |
| V10.3.1 | L2 | No aplica | No es servidor de recursos OAuth; la audiencia de los JWT propios se evalúa en V9.2.3. |
| V10.3.2 | L2 | No aplica | No recibe tokens de acceso OAuth delegados; los permisos se releen de la base (auth.py). |
| V10.3.3 | L2 | No aplica | No es servidor de recursos OAuth; los JWT propios identifican por `sub` numérico de un único emisor. |
| V10.3.4 | L2 | No aplica | No consume `acr`/`amr`/`auth_time` de un IdP. |
| V10.4.1 | L1 | No aplica | No es servidor de autorización (lo es Google). |
| V10.4.2 | L1 | No aplica | No emite códigos de autorización OAuth. |
| V10.4.3 | L1 | No aplica | No emite códigos de autorización OAuth (el código de vista previa no es OAuth). |
| V10.4.4 | L1 | No aplica | No es servidor de autorización. |
| V10.4.5 | L1 | No aplica | No emite refresh tokens OAuth. |
| V10.4.6 | L2 | No aplica | No es servidor de autorización. Como cliente sí usa PKCE S256. |
| V10.4.7 | L2 | No aplica | No hay registro dinámico de clientes. |
| V10.4.8 | L2 | No aplica | No emite refresh tokens; la vida del de Google la fija Google. |
| V10.4.9 | L2 | No aplica | No es servidor de autorización; el refresh de Drive se revoca en la cuenta de Google. |
| V10.4.10 | L2 | No aplica | No es servidor de autorización. Como cliente se autentica con `client_secret` ante Google. |
| V10.4.11 | L2 | No aplica | No es servidor de autorización. Como cliente pide solo `drive.file`, el alcance mínimo. |
| V10.5.1 | L2 | No aplica | No usa OIDC ni ID Tokens (el alcance es solo Drive). |
| V10.5.2 | L2 | No aplica | No usa OIDC ni ID Tokens. |
| V10.5.3 | L2 | No aplica | No lee metadatos de un servidor de autorización; los endpoints de Google están fijos en el código. |
| V10.5.4 | L2 | No aplica | No usa ID Tokens. |
| V10.5.5 | L2 | No aplica | No hay OIDC back-channel logout. |
| V10.6.1 | L2 | No aplica | No es proveedor OpenID. |
| V10.6.2 | L2 | No aplica | No es proveedor OpenID. |
| V10.7.1 | L2 | No aplica | El consentimiento lo pide Google (`prompt=consent`); RestHub no es servidor de autorización. |
| V10.7.2 | L2 | No aplica | No es servidor de autorización. |
| V10.7.3 | L2 | No aplica | No es servidor de autorización; los consentimientos se gestionan en la cuenta de Google. |

### V11 Criptografía

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V11.1.1 | L2 | Cumple | `backend:docs/criptografia.md` fija la política de ciclo de vida (NIST SP 800-57): períodos de uso, rotación extraordinaria, custodia, revocación, destrucción y registro, con los procedimientos para `DATA_ENCRYPTION_KEY`, `JWT_SECRET_KEY` y la llave de age. |
| V11.1.2 | L2 | Cumple | `backend:docs/criptografia.md` es el inventario único de llaves, algoritmos, columnas cifradas, valores aleatorios y certificados, con dónde se usa cada uno y qué datos protege. |
| V11.2.1 | L2 | Cumple | `cryptography` (AESGCM, OpenSSL) en `backend:src/resthub/core/crypto.py`; `pwdlib`+argon2 en `core/security.py`; PyJWT en `core/tokens.py`; `hmac`/`secrets` de la biblioteca estándar; age en `.github/workflows/backup.yml`. Nada hecho a mano. |
| V11.2.2 | L2 | Parcial | Hay agilidad: prefijo `enc1:<id de llave>`, llaves previas y `backend:scripts/rotate_data_key.py`, que también re-cifra el secreto TOTP (`two_factor_credentials.secret` es `EncryptedText`, migración 0045; prueba `test_rotating_the_key_keeps_two_factor_working`); los hashes se recalculan solos al subir el costo. Falta: el JWT tiene un solo secreto, sin `kid`, y cambiarlo corta todas las sesiones (`docs/criptografia.md`). |
| V11.2.3 | L2 | Parcial | AES-256, HMAC-SHA256 con llave de 32 bytes, secreto JWT de 32 bytes o más (`core/config.py`), TOTP de 160 bits. Pero no se valida el tamaño de la llave RSA del certificado SUNAT (`billing/adapters/sunat/certificates.py`): con RSA-2048 son unos 112 bits. |
| V11.3.1 | L1 | Cumple | Único cifrado simétrico: AESGCM (`backend:src/resthub/core/crypto.py`). Ni ECB, ni CBC, ni cifrado RSA con PKCS#1 v1.5 en `src/`, `scripts/` ni `alembic/`. |
| V11.3.2 | L1 | Cumple | AES-256-GCM con nonce aleatorio de 96 bits por valor (`core/crypto.py`, `core/data_keys.py`); respaldos con age (X25519 + ChaCha20-Poly1305). |
| V11.3.3 | L2 | Cumple | Cifrado autenticado GCM; una etiqueta inválida da `DataEncryptionError` (`backend:tests/test_data_encryption.py`). Detalles: no usa AAD (columna o fila), y `decrypt` acepta texto sin prefijo, que se toma como legado. |
| V11.4.1 | L2 | Parcial | SHA-256 y HMAC-SHA256 en todo lo demás; MD5 no aparece. El TOTP usa HMAC-SHA1 (`backend:src/resthub/core/two_factor.py`) para ser compatible con RFC 6238 y las apps de códigos: es un uso heredado, no aprobado para usos nuevos. |
| V11.4.2 | L2 | Cumple | Argon2id con t=3, m=64 MiB, p=1 (RFC 9106), configurable por `Settings`; bcrypt se migra al primer acceso. `backend:src/resthub/core/security.py`; pruebas en `backend:tests/test_password_hashing.py`. El PIN también usa Argon2id. |
| V11.4.3 | L2 | Cumple | Integridad con SHA-256 o más: HMAC-SHA256 (índice ciego), JWT HS256, etiqueta GCM. Los SHA-256 truncados (`key_id`, `public_session_ref`) solo identifican; no autentican. |
| V11.4.4 | L2 | No aplica | Ninguna llave se deriva de una contraseña: la de datos es aleatoria (`core/data_keys.py`), la de age la crea `age-keygen`, y la contraseña PKCS#12 solo abre el certificado. |
| V11.5.1 | L2 | Cumple | `secrets` (CSPRNG) en todo: `sid` y CSRF de 256 bits, código de vista previa de 256 bits, nonces, y códigos de recuperación 2FA de unos 139 bits (28 caracteres de un alfabeto de 31, `core/two_factor.py`). Los de antes (unos 119 y 49 bits) valen hasta que la cuenta los renueve, y `recovery_codes_outdated` lo avisa. |
| V11.6.1 | L2 | Parcial | El par RSA lo emite la CA del certificado y la firma XML la hace Lycet, un servicio externo que no está en el repo. La app solo comprueba que la llave sea RSA y la fecha de vencimiento; no revisa tamaño ni exponente. No hay generación propia de llaves. |

### V12 Comunicación segura

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V12.1.1 | L1 | Fuera del código | TLS lo terminan Vercel y el proxy de Dokploy en el VPS; ni el `Dockerfile` (uvicorn HTTP plano en `0.0.0.0:$PORT`) ni `vercel.json` configuran versiones TLS. |
| V12.1.2 | L2 | Fuera del código | Suites de cifrado definidas por el borde de Vercel y el proxy de Dokploy; nada en los repos. |
| V12.1.3 | L2 | No aplica | No hay autenticación por certificado de cliente (mTLS). |
| V12.2.1 | L1 | Cumple | HSTS 2 años en API y frontend (`core/security_headers.py`, `vercel.json`); CSP del frontend `connect-src 'self' https:` acotada al origen de `VITE_API_URL` al compilar (`vite.plugins.ts` `boundConnections`). TLS mismo lo provee el host. |
| V12.2.2 | L1 | Fuera del código | Certificados públicos: Vercel y Let's Encrypt por el proxy de Dokploy (README, «Despliegue», paso 3). |
| V12.3.1 | L2 | Parcial | backend: con `DEBUG=false`, `core/config.py` exige `https://` (o loopback) en `LYCET_URL`, OpenRouter, TypeSafe, Factiliza y `OTEL_EXPORTER_OTLP_ENDPOINT` (test `test_production_rejects_integrations_without_https`). El TLS hacia PostgreSQL se configura con `DATABASE_SSL_MODE` (`core/database.py`, también en las migraciones), pero por omisión es `prefer` y fuera de depuración solo se avisa al arrancar (test `test_production_without_enforced_tls_warns_at_startup`). Falta fijar `require` o `verify-full` en el despliegue. |
| V12.3.2 | L2 | Parcial | backend: clientes `httpx.AsyncClient` con verificación por omisión; no hay `verify=False` en `src/`. `DATABASE_SSL_MODE=verify-full` valida certificado y nombre (`DATABASE_SSL_ROOT_CERT`; test `test_verify_full_checks_certificate_and_hostname`), pero no es el valor por omisión. |
| V12.3.3 | L2 | Fuera del código | No hay servicios HTTP internos en producción (Lycet solo en local, `docker/lycet`); el tramo proxy→contenedor es HTTP dentro del VPS (red de Docker); hacia Supabase, TLS con `verify-full` (`docs/migracion-supabase.md`). |
| V12.3.4 | L2 | Fuera del código | Sin certificados internos ni autofirmados en los repos; el tramo interno es la red de Docker del VPS. |

### V13 Configuración

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V13.1.1 | L2 | Cumple | backend: `README.md` «Variables de entorno» y secciones por integración (OpenRouter/TypeSafe, Lycet, Brevo, Google Drive, Factiliza, OTLP, GeoIP local, PostgreSQL). Ningún usuario puede aportar una URL externa a la que se conecte el servidor. frontend: solo `VITE_API_URL` (`.env.example`). |
| V13.2.1 | L2 | Parcial | backend: PostgreSQL con contraseña fija (`DATABASE_URL`); integraciones con claves estáticas (`LYCET_TOKEN`, `BREVO_API_KEY`, `FACTILIZA_TOKEN`, `OPENROUTER_API_KEY`). Solo Google Drive usa tokens cortos OAuth (`billing/adapters/archive/google_drive.py` `_token`). |
| V13.2.2 | L2 | Parcial | backend: `Dockerfile` corre como `resthub` sin privilegios y con código de solo lectura. Pero la app usa el mismo usuario dueño de las tablas y corre `alembic upgrade head` (README «Bitácoras de solo inserción», `Dockerfile` `CMD`). Falta un rol de BD con mínimos permisos separado del de migraciones. Abierto a propósito (infraestructura): ver «Brechas por prioridad». |
| V13.2.3 | L2 | Cumple | backend: `core/config.py` `_validate_secret` / `_validate_encryption_keys` rechazan el `JWT_SECRET_KEY` y la llave de desarrollo con `DEBUG=false` (tests `test_production_rejects_development_jwt_secret`, `test_production_rejects_sqlite`). Credenciales de BD propias por usuario (`resthub_app`, `resthub_backup`, `postgres`), generadas al azar (`docs/migracion-supabase.md`). |
| V13.2.4 | L2 | Cumple | backend: los destinos salientes son solo las URL base fijadas en `Settings` (`factiliza_base_url`, `typesafe_base_url`, `openrouter_base_url`, `lycet_url`, Drive/Brevo fijos); ningún endpoint descarga de URL del usuario (fotos se suben como bytes). |
| V13.2.5 | L2 | Parcial | Allowlist solo implícita en la capa de aplicación; ni el `Dockerfile` ni el VPS restringen el tráfico de salida del contenedor (sin firewall de egress). |
| V13.3.1 | L2 | Parcial | backend: secretos por variables de entorno del servicio en Dokploy (`SecretStr` en `core/config.py`, `.dockerignore` excluye `.env*`), gitleaks en CI (`.github/workflows/ci.yml`, `.gitleaks.toml`) en ambos repos, script de rotación `scripts/rotate_data_key.py`. No hay gestor de secretos/bóveda. |
| V13.3.2 | L2 | Fuera del código | Quién ve las variables del servicio depende de quién entra a Dokploy y al VPS, y de los permisos del proyecto en Vercel; todos los secretos los tiene un único proceso. |
| V13.4.1 | L1 | Cumple | backend: `.dockerignore` excluye `.git`, `.github`, `.githooks`, `tests`. frontend: Vercel publica solo el `dist` de Vite y `vercel.json` reescribe todo a `index.html`. |
| V13.4.2 | L2 | Cumple | backend: FastAPI sin `debug=True` (sin trazas en 500); `/docs` y `openapi.json` solo con `DEBUG=true` (`main.py` `create_app`). `debug: bool = False` por omisión (`core/config.py`): si falta la variable, arranca en modo producción. Tests `test_debug_is_off_by_default`, `test_production_does_not_publish_docs_or_schema`. |
| V13.4.3 | L2 | Cumple | backend: sin `StaticFiles` ni `mount`. frontend: `vercel.json` reescribe toda ruta a `/index.html`, no hay listado. |
| V13.4.4 | L2 | Cumple | backend: ninguna ruta declara TRACE; Starlette responde 405 y CORS solo permite GET/POST/PUT/PATCH/DELETE/OPTIONS (`main.py` `_install_middlewares`). Sin test explícito. |
| V13.4.5 | L2 | Cumple | backend: `/api/v1/docs`, `/redoc`, `openapi.json` solo con `DEBUG=true`; observabilidad exige `PlatformPermission.OBSERVABILITY_READ` (test `test_only_the_platform_gets_into_the_panel`). `/health` público expone solo servicio, versión y estado de BD. |

### V14 Protección de datos

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V14.1.1 | L2 | Cumple | `backend:docs/clasificacion-de-datos.md` clasifica las 37 tablas en cuatro niveles (pública, interna, controlada, restringida) y también los datos fuera de las tablas: el contenido del JWT, los base64 en tránsito, los logs y el almacenamiento del navegador. |
| V14.1.2 | L2 | Cumple | `backend:docs/clasificacion-de-datos.md` fija la protección por nivel y los plazos de conservación: accesos 90 d, observabilidad 14 d, respaldos 30 d (`backend:tests/test_retention_periods.py`), y la purga de `backend:wiring/data_retention.py` (clientes 24 meses, contacto de pedidos y reservas 90 d, personal dado de baja 12 meses, bitácoras 2 años; `backend:tests/test_data_retention.py`). Los comprobantes quedan fuera a propósito, por la obligación tributaria. |
| V14.2.1 | L1 | Parcial | Tokens en cookie `HttpOnly` o Bearer, SSE por `fetch` con cabecera (`frontend:src/services/eventStream.ts`), código de vista previa en el fragmento `#` (`frontend:src/features/platform/previewLink.ts`). Pero el teléfono va en `GET /customers?q=` y el DNI/RUC en la ruta `/lookup/dni/{dni}`. En los logs de la app se tapan los dos (`core/redaction.py`; test `test_scrub_text_hides_documents_and_phones_in_paths`), pero no en los del proxy. Abierto a propósito: ver «Brechas por prioridad». |
| V14.2.2 | L2 | Cumple | El API no usa caché compartido: toda respuesta lleva `Cache-Control: no-store` salvo la ruta que fija el suyo (la foto de un plato, `private`) (`backend:src/resthub/core/security_headers.py`; test `test_a_route_that_sets_its_own_cache_control_keeps_it`). La búsqueda de RUC/DNI se recuerda 10 min en memoria (`LOOKUP_CACHE_SECONDS`); los PDF llevan `private, no-store` (`core/file_response.py`). |
| V14.2.3 | L2 | Cumple | El frontend no tiene rastreadores ni analítica (CSP `script-src 'self'` en `frontend:vercel.json`). La IA externa recibe notas sin correo, teléfono, DNI ni nombre (`insights/domain/privacy.py`, `backend:tests/test_ai_privacy.py`) y se puede apagar. Los demás terceros son encargados declarados. |
| V14.2.4 | L2 | Cumple | Lo documentado está implementado y probado: cifrado (`backend:tests/test_data_encryption.py`), con el secreto 2FA dentro de la rotación (`test_rotating_the_key_keeps_two_factor_working`), retención, redacción (`core/redaction.py`, `hide_parameters`), DLP (`test_api_customers_dlp.py`). Los niveles para verificarlo están en `backend:docs/clasificacion-de-datos.md`. |
| V14.3.1 | L1 | Parcial | `cleanSession` (`frontend:src/store/session.ts`) borra el token en memoria, la sesión guardada y el caché de React Query; el logout borra las cookies y responde `Clear-Site-Data: "cache"` (`backend:src/resthub/core/session_cookie.py`; test `test_platform_logout_also_clears_the_cache`). Cerrar la sesión también borra de la cola sin conexión los pedidos de esa cuenta con datos del cliente, avisando antes (`discardCustomerDataOf`, `frontend:src/store/offlineQueue.ts`). Queda: si la sesión vence sola, la cola se conserva entera para que la cuenta la envíe al volver. |
| V14.3.2 | L2 | Cumple | `backend:src/resthub/core/security_headers.py` pone `Cache-Control: no-store` en toda respuesta del API que no fije el suyo: la ficha y el export ARCO del cliente, el secreto 2FA y los códigos de recuperación salen sin caché. Tests en `backend:tests/test_security_headers_rate_limit.py`. |
| V14.3.3 | L2 | Parcial | El token ya no se guarda en el navegador (cookie `HttpOnly`) y el CSRF vive solo en memoria. Pero `localStorage` guarda el perfil (nombre, correo, permisos) y, mientras haya sesión o si venció sola, los pedidos en cola con nombre, teléfono y dirección del cliente, sin cifrar. El service worker solo guarda los archivos de la app. |

### V15 Codificación y arquitectura seguras

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V15.1.1 | L1 | Cumple | `backend:SECURITY.md` fija la severidad (CVSS) y los plazos de remediación por severidad (crítica en 72 horas), para el código y las dependencias, con el triaje de avisos y las excepciones con vencimiento. La automatización sigue en `backend:.github/dependabot.yml` y `frontend:.github/dependabot.yml`. |
| V15.1.2 | L2 | Cumple | Inventario en `backend:uv.lock` (PyPI, con hashes) y `frontend:pnpm-lock.yaml` (con integrity); `deptry`; `minimumReleaseAge` y `overrides` en `frontend:pnpm-workspace.yaml`. Cada release del backend adjunta un SBOM CycloneDX generado de `uv.lock` (`backend:.github/workflows/release.yml`). |
| V15.1.3 | L2 | Parcial | Repartido en el README del backend (Argon2 en `core/cpu_bound.py`, límite de peticiones, colas y topes de telemetría, tiempos de espera de la IA y de Factiliza) y en docstrings (`core/request_deadline.py`: 12 s frente a los 15 s del cliente).<br>Falta: una sección única que enumere las operaciones costosas y sus defensas; el plazo por petición no está en el README. |
| V15.2.1 | L1 | Cumple | `pip-audit --strict` (`backend:.github/workflows/ci.yml`) y `pnpm audit --audit-level high` (`frontend:.github/workflows/ci.yml`) bloquean el CI, y los dos repositorios lo repiten cada lunes (`backend:.github/workflows/auditoria-dependencias.yml`, `frontend:.github/workflows/auditoria-dependencias.yml`). Los plazos para medir están en `SECURITY.md` de cada repositorio; el del backend hoy no registra excepciones (ya no se ignora GHSA-vfj7-8cjw-p6xm). |
| V15.2.2 | L2 | Cumple | `backend:core/rate_limit.py`, `core/login_throttle.py`, `core/cpu_bound.py`, `core/request_deadline.py` (503), `core/pagination.py` (`MAX_PAGE_SIZE`, `MAX_OFFSET`), topes de cola en `core/telemetry.py`, tareas de fondo en `core/background.py`.<br>Pruebas: `tests/test_security_headers_rate_limit.py`, `test_cpu_bound.py`, `test_api_bounds.py`, `test_login_concurrency.py`. |
| V15.2.3 | L2 | Parcial | Docs y OpenAPI solo con `DEBUG`, apagado por omisión (`backend:core/config.py`; test `test_production_does_not_publish_docs_or_schema`); `tests` fuera de la imagen (`.dockerignore`); el proveedor Pact vive en `tests/contract`.<br>Falta: la imagen copia `scripts/` (siembras, fuzzing, carbono). |
| V15.3.1 | L1 | Cumple | Toda salida pasa por modelos Pydantic `*Response` con campos explícitos; lo vigila `backend:tests/test_architecture.py` (sufijo). Datos personales enmascarados: `tests/test_customers_masking.py`. Las sesiones devuelven un `id` público, no el `sid`. |
| V15.3.2 | L2 | Cumple | Todos los `httpx.AsyncClient` usan el valor por omisión `follow_redirects=False` (httpx 0.28.1) y Lycet lo declara explícito (`backend:modules/billing/adapters/sunat/lycet.py`). No hay `requests` ni `urllib`. |
| V15.3.3 | L2 | Cumple | Un `*Request` por acción con sus campos; los casos de uso reciben `Command` o dataclasses fijas; `restaurant_id` sale del token (AGENTS.md).<br>Prueba: `backend:tests/test_api_staff.py::test_restaurant_id_in_the_body_is_ignored`. Pydantic ignora los campos de más (no hay `extra="forbid"`). |
| V15.3.4 | L2 | Cumple | `backend:core/auth.py::client_address` toma `CLIENT_IP_HEADER` (WAF) o la última IP de `X-Forwarded-For`, y la usan el límite de intentos, el de peticiones y el historial de accesos. Supuestos documentados en el README (`FORWARDED_ALLOW_IPS`, sin acceso que esquive el proxy). |
| V15.3.5 | L2 | Cumple | Validación con Pydantic y `mypy --strict` (`backend:pyproject.toml`); `hmac.compare_digest` en `core/session_cookie.py` y `core/two_factor.py`; frontend: `eqeqeq`, `strict-boolean-expressions` y ts-reset (`JSON.parse` devuelve `unknown`) en `frontend:eslint.config.js`. |
| V15.3.6 | L2 | Cumple | No hay fusión profunda de datos ajenos (sin lodash ni deepmerge); `JSON.parse` devuelve `unknown` y se valida (`frontend:src/services/appearanceRules.ts`); `Object.hasOwn` en `services/logger.ts`; `eslint-plugin-security` como error. `detect-object-injection` está apagada, con su motivo. |
| V15.3.7 | L2 | Cumple | FastAPI declara el origen de cada parámetro (`Query`, `Path`, cuerpo Pydantic, `Header`, `Cookie`); no se mezclan fuentes. Bearer y cookie con CSRF por doble envío, con prioridad definida (`backend:core/session_cookie.py`). El fuzzing de Schemathesis corre en CI. |

### V16 Registro de seguridad y manejo de errores

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V16.1.1 | L2 | Cumple | `backend:docs/eventos-de-seguridad.md` es el inventario por capa: destinos (stdout del contenedor, `obs_*`, `access_history`, bitácoras, OTel), cada evento de log, los tipos de la bitácora, quién los lee y cuánto duran. Lo complementa el README (panel de observabilidad, `access_history` de 90 días). |
| V16.2.1 | L2 | Cumple | structlog añade hora, nivel, logger, `request_id`, método, ruta y `trace_id` (`backend:core/logs.py`, `core/request_logging.py`), y en cuanto la petición se autentica, `account_kind`, `account_id` y `restaurant_id`; el middleware suma `client_network` (la IP sin su último bloque). Tests: `test_request_lines_carry_account_and_client_network`, `test_anonymous_request_lines_carry_only_the_network`. |
| V16.2.2 | L2 | Cumple | `TimeStamper(fmt="iso", utc=True)` en `backend:core/logs.py`; puerto `Clock` en UTC (`core/clock.py`, vigilado por `TID251` y `tests/test_architecture.py`). La sincronización de los relojes queda en manos de la infraestructura (NTP del VPS y Supabase). |
| V16.2.3 | L2 | Cumple | Destinos: stdout, `obs_requests`/`obs_events` en la base propia y OTel solo con `OTEL_EXPORTER_OTLP_ENDPOINT`; todos documentados en el README. El frontend solo escribe en la consola (`frontend:src/services/logger.ts`), sin servicios de terceros. |
| V16.2.4 | L2 | Cumple | `LOG_JSON=true` en despliegue: una línea JSON por evento (`backend:core/logs.py`); los logs de uvicorn y de las librerías se reencauzan al mismo formato. Correlación por `request_id`, `X-Request-ID` y `trace_id`. |
| V16.2.5 | L2 | Cumple | stdout y el panel pasan por la misma regla (`core/redaction.py`): claves sensibles y de datos personales, limpieza de textos, del mensaje y del traceback (sin variables locales), correos enmascarados y tiras de 8 a 19 dígitos ocultas; la base usa `hide_parameters=True`. Pruebas: `tests/test_logging.py` (`test_stdout_line_masks_personal_data_and_tokens`, `test_stdout_exception_is_scrubbed_and_has_no_locals`), `tests/test_telemetry_hardening.py`. |
| V16.3.1 | L2 | Cumple | Cada intento de inicio de sesión queda en `access_history` con su resultado y en `auth.login_succeeded`/`auth.login_failed` (`backend:modules/accounts/adapters/api/auth_router.py`); cada reautenticación fallida en `auth.reauth_failed` y cada PIN o contraseña equivocados al desbloquear en `auth.unlock_failed`; 2FA, PIN y salida van a la bitácora. Pruebas: `tests/test_access_history.py`, `test_every_failed_reauthentication_is_logged`, `test_every_wrong_unlock_is_logged`. |
| V16.3.2 | L2 | Cumple | Cada 403 queda en `obs_requests` (ruta, estado, cuenta, restaurante) y como aviso `request.completed`; los picos generan alertas (`backend:modules/platform/adapters/security_alert_loop.py`). Prueba: `tests/test_security_alerts.py`. |
| V16.3.3 | L2 | Parcial | Se registran `http.rate_limited`, `auth.login_throttled`, `auth.reauth_throttled`, `auth.csrf_rejected` (test `test_a_rejected_csrf_token_is_logged`), `security.spike` y `auth.unfamiliar_access`, y la lista está en `backend:docs/eventos-de-seguridad.md`.<br>Falta: un evento explícito para los 422 de validación; los 4xx no van a `obs_events`. |
| V16.3.4 | L2 | Cumple | Las excepciones no controladas quedan como `request.failed` con su traceback (`backend:core/request_logging.py`); también `database.unavailable` y los fallos de terceros (`billing.*_unreachable`, `llm.request_failed`, `jev.timeout`). Prueba: `tests/test_telemetry_capture.py`. |
| V16.4.1 | L2 | Cumple | El renderizador JSON escapa los saltos de línea; un `X-Request-ID` ajeno se valida con la regex `[A-Za-z0-9._-]{8,128}` (`backend:core/request_logging.py`). Pruebas: `tests/test_logging.py::test_a_malformed_incoming_request_id_is_replaced` y `tests/test_redaction_redos.py`. |
| V16.4.2 | L2 | Parcial | `activity_log`, `platform_activity` y `ai_decisions` rechazan UPDATE y DELETE por trigger (`backend:alembic/versions/0034_append_only_logs.py`) y, en PostgreSQL, también TRUNCATE (`0044_append_only_logs_block_truncate.py`; `tests/test_migrations.py::test_logs_cannot_be_truncated_on_postgres`); el panel exige el token de plataforma.<br>Falta: la app es dueña de las tablas y podría quitar los triggers (sin rol separado); `obs_*` es modificable. |
| V16.4.3 | L2 | Parcial | stdout queda en los logs del contenedor en el VPS y OTel puede enviar las trazas a un colector externo (`core/tracing.py`), pero el panel y las bitácoras viven en la misma base de la app ("no hay servicios externos", README).<br>Falta: enviar los logs a un SIEM separado, con sus alertas. Abierto a propósito (infraestructura): ver «Brechas por prioridad». |
| V16.5.1 | L2 | Cumple | Errores con forma `{detail, code}` (`backend:core/errors.py`, `core/error_codes.py`); un 500 sin manejar devuelve el "Internal Server Error" genérico (debug apagado); la base caída da un 503 genérico (`core/database_errors.py`). Frontend: `frontend:src/router/RouteError.tsx`. Prueba: `tests/test_api_error_codes.py`. |
| V16.5.2 | L2 | Cumple | Si la IA falla, deciden las reglas; Factiliza tiene un tiempo de espera corto y responde 503; el broker SSE cae a modo local (`core/realtime_broker.py`); el sondeo de vida no espera a la base; el frontend tiene una cola sin conexión. Pruebas: `tests/test_resilience_base.py` y experimentos en `chaos/`. |
| V16.5.3 | L2 | Cumple | `core/database.py::get_session` deshace la transacción ante cualquier excepción; el plazo de petición no corta un COMMIT ya empezado; `for_update` en las lecturas que luego modifican; los errores de dominio cortan la acción. Prueba: `tests/test_resilience_base.py`. |

### V17 WebRTC

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V17.1.1 | L2 | No aplica | No hay WebRTC ni servidor TURN: `grep` de `RTCPeerConnection`, `getUserMedia`, `stun:`/`turn:`, `aiortc` y `mediasoup` no da resultados en ninguno de los dos repos. El tiempo real es SSE (`backend:core/events_router.py`). |
| V17.2.1 | L2 | No aplica | No hay servidor de medios ni certificado DTLS. |
| V17.2.2 | L2 | No aplica | No hay DTLS-SRTP. |
| V17.2.3 | L2 | No aplica | No hay SRTP ni RTP. |
| V17.2.4 | L2 | No aplica | No hay tráfico de medios. |
| V17.3.1 | L2 | No aplica | No hay servidor de señalización WebRTC; además, `Permissions-Policy` bloquea la cámara y el micrófono (`frontend:vercel.json`). |
| V17.3.2 | L2 | No aplica | No hay señalización WebRTC; el capítulo entero queda fuera de alcance mientras la app no tenga audio, video ni P2P. |
