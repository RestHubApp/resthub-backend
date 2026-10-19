# Lista de chequeo OWASP ASVS 5.0 (RNFS-21)

Verificación de RestHub contra el [OWASP Application Security Verification Standard 5.0.0](https://github.com/OWASP/ASVS/tree/v5.0.0/5.0),
niveles **L1 y L2** (253 requisitos). El nivel 3 queda fuera de alcance.

- **Fecha:** 2026-10-19.
- **Código revisado:** `resthub-backend` en `d13ba6c` y `resthub-frontend` en `3e336db` (rama `develop`), con las fotos de platos y el régimen tributario.
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
| V2 Validación y lógica de negocio | 8 | 3 | 0 | 0 | 0 | 11 |
| V3 Seguridad del frontend web | 13 | 4 | 1 | 1 | 0 | 19 |
| V4 Seguridad de API y servicios web | 0 | 2 | 0 | 6 | 2 | 10 |
| V5 Manejo de archivos | 5 | 2 | 1 | 1 | 0 | 9 |
| V6 Autenticación | 15 | 12 | 1 | 7 | 0 | 35 |
| V7 Gestión de sesiones | 6 | 8 | 1 | 3 | 0 | 18 |
| V8 Autorización | 6 | 1 | 0 | 0 | 0 | 7 |
| V9 Tokens autocontenidos | 6 | 1 | 0 | 0 | 0 | 7 |
| V10 OAuth y OIDC | 3 | 0 | 0 | 26 | 0 | 29 |
| V11 Criptografía | 6 | 7 | 0 | 1 | 0 | 14 |
| V12 Comunicación segura | 1 | 2 | 0 | 1 | 5 | 9 |
| V13 Configuración | 7 | 5 | 0 | 0 | 1 | 13 |
| V14 Protección de datos | 1 | 7 | 1 | 0 | 0 | 9 |
| V15 Codificación y arquitectura seguras | 8 | 4 | 1 | 0 | 0 | 13 |
| V16 Registro de seguridad y manejo de errores | 9 | 7 | 0 | 0 | 0 | 16 |
| V17 WebRTC | 0 | 0 | 0 | 7 | 0 | 7 |
| **Total** | **110** | **65** | **6** | **64** | **8** | **253** |

## Brechas por prioridad

### Alta

1. **Las respuestas del API no llevan `Cache-Control: no-store`** (V14.3.2, V14.2.2). Pueden quedar en el caché del navegador o de un proxy:
   - la ficha del cliente y su export ARCO;
   - el secreto de la verificación en dos pasos;
   - los códigos de recuperación.

   Agregarlo en `SecurityHeadersMiddleware` para todo `/api/v1`.
2. **`DEBUG` vale `true` por omisión** (V13.4.2, V15.2.3). Si falta la variable en producción, se publican `/docs` y `openapi.json` y no se validan el secreto JWT, la llave de cifrado ni la base SQLite. Invertir el valor por omisión y probar que la documentación queda oculta.
3. **Los códigos de recuperación de la verificación en dos pasos** tienen unos 49 bits y se guardan con SHA-256 sin sal (V6.5.2, V11.5.1). Usar Argon2id o generarlos con 112 bits o más.
4. **La rotación de la llave de datos no cubre el secreto TOTP** (V11.2.2, V14.2.4). Está en `two_factor_credentials.secret`, una columna `String` cifrada a mano, y `scripts/rotate_data_key.py` no la recorre. Al retirar la llave anterior, la verificación en dos pasos deja de funcionar. Pasar la columna a `EncryptedText`.
5. **La sesión no tiene vida máxima absoluta** (V7.3.2, V7.1.1). `POST /auth/refresh` la extiende sin límite; hace falta un tope desde `account_sessions.created_at`.
6. **El token de sesión también viaja en el cuerpo** del login y del refresh, y el frontend lo usa como Bearer (V3.3.4). Un XSS podría robarlo aunque la cookie sea HttpOnly.

### Media

7. **Cookies sin prefijo `__Host-`** (V3.3.3, V3.3.1), y nada obliga a usar `Secure` cuando `DEBUG=false`.
8. **Sin tope global del cuerpo de la petición** (V5.2.1). Una petición enorme, incluso sin sesión, se lee entera en memoria. Hace falta un middleware que responda 413.
9. **Carrera al reutilizar el TOTP o un código de recuperación** (V6.5.1). Hay que leer la fila con `for_update`.
10. **Verificación en dos pasos opcional para el personal**, incluido el encargado (V6.3.3). Exigirla a los roles con `staff.manage` o facturación, o documentar el porqué.
11. **El login no cierra la sesión anterior y la reautenticación no rota el token** (V7.2.4). Para cerrar las propias sesiones no se pide reautenticar, y reautenticar pide solo la contraseña aunque haya verificación en dos pasos (V7.5.1, V7.5.2).
12. **Los JWT no llevan `aud` ni `iss`** (V9.2.3). Además, `jwt_algorithm` no está limitado a HS256 y no hay una prueba que rechace `alg=none`.
13. **Datos del cliente de delivery en `localStorage` después de cerrar sesión** (V14.3.1, V14.3.3). La cola sin conexión guarda nombre, teléfono y dirección, y el logout no envía `Clear-Site-Data`.
14. **Logs de despliegue con poca redacción** (V16.2.5). En stdout solo se ocultan 8 claves exactas; `core/redaction.py` se aplica solo en el panel.
15. **Bitácoras sin bloqueo de `TRUNCATE`, y la app usa el usuario dueño de la base** (V16.4.2, V16.4.3, V13.2.2). Hacen falta:
    - un rol de aplicación sin DDL, separado del de migraciones;
    - un colector de logs externo.
16. **Contraseñas** (V6.2.2, V6.2.4, V6.2.11, V6.2.12, V6.4.1):
    - la lista de contraseñas comunes tiene unas 2 320 entradas y el requisito pide 3 000;
    - no se rechazan las palabras de contexto (el nombre del local, «resthub», el correo);
    - las cuentas de plataforma no pueden cambiar su contraseña;
    - las contraseñas temporales no vencen.

### Baja o de documentación

17. **Plazos de remediación por severidad** sin documentar. Además:
    - `pip-audit` y `pnpm audit` no corren de forma programada;
    - no hay SBOM;
    - la excepción GHSA-vfj7-8cjw-p6xm no tiene fecha de vencimiento.

    Requisitos: V15.1.1, V15.1.2, V15.2.1.
18. **Faltan documentos e inventarios:**
    - un inventario de llaves y algoritmos;
    - la clasificación de datos con su plazo de retención;
    - un inventario de eventos de seguridad registrados;
    - el catálogo de reglas de validación;
    - las vías de autenticación.

    Requisitos: V11.1.1, V11.1.2, V14.1.1, V14.1.2, V16.1.1, V2.1.1, V6.1.3.
19. **Transporte:**
    - TLS verificado hacia PostgreSQL;
    - URL `https` obligatorias para las integraciones;
    - `FORWARDED_ALLOW_IPS="*"` en el `Dockerfile`, que hay que acotar al rango del proxy.

    Requisitos: V12.3.1, V12.3.2, V4.1.3.
20. **Detalles:**
    - Las fotos no se vuelven a codificar en el servidor (V5.4.3).
    - El teléfono de pedidos y reservas no tiene patrón (V2.2.1).
    - El CSP usa `base-uri 'self'`, que debería ser `'none'` (V3.4.3).
    - El DNI/RUC y el teléfono viajan en la URL (V14.2.1).
    - Las búsquedas LIKE no escapan `%` ni `_` (V1.2.4).

## Detalle por capítulo

### V1 Codificación y saneamiento

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V1.1.1 | L2 | Cumple | Starlette/FastAPI decodifican URL y JSON una sola vez antes de Pydantic. El base64 de foto y certificado se decodifica una vez, con `validate=True`, antes de validar: backend:`modules/menu/adapters/api/router.py` (`set_photo`), backend:`modules/billing/adapters/api/router.py` (`upload_certificate`). |
| V1.1.2 | L2 | Cumple | La codificación ocurre al final: React escapa en el render (frontend sin `dangerouslySetInnerHTML`), FastAPI serializa JSON y `compose_email` aplica `html.escape` al armar el cuerpo (backend:`modules/billing/use_cases/delivery.py`). |
| V1.2.1 | L1 | Cumple | El API solo responde JSON o archivos con tipo fijo y `nosniff` (backend:`core/security_headers.py`, `core/file_response.py`). `Content-Disposition` según RFC 6266. Frontend: JSX escapa el texto y no hay `innerHTML`. Test: `test_every_api_response_carries_security_headers`. |
| V1.2.2 | L1 | Cumple | `fill()` aplica `encodeURIComponent` a los parámetros de ruta (frontend:`src/services/paths.ts`) y openapi-fetch arma las llamadas. Los únicos `href` dinámicos son `tel:` (DeliveryCard), `otpauth_uri` que genera el servidor y `appUrl` interno. Backend: RUC/DNI se validan como dígitos antes de ir a la URL de Factiliza/Lycet. |
| V1.2.3 | L1 | Cumple | El JSON sale siempre de serializadores (`JSONResponse`, `httpx json=`, `json.dumps` en backend:`billing/adapters/archive/google_drive.py`; `JSON.stringify` en frontend). No se concatena JS ni JSON a mano. |
| V1.2.4 | L1 | Cumple | ORM SQLAlchemy y `text()` con parámetros enlazados. Los f-strings de backend:`platform/adapters/persistence/directories.py` solo juntan constantes. Detalle menor: `customers/.../sqlalchemy_repositories.py` y `accounts/.../sqlalchemy_user_repository.py` no escapan `%`/`_` en LIKE (comodines, no inyección). Además Bandit y Semgrep corren en CI. |
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
| V2.1.1 | L1 | Parcial | El README documenta formatos sueltos (RUC/DNI, PIN de 4 a 6 dígitos, contraseñas, certificado, `window`). No hay un catálogo de reglas por campo: según el README, la documentación funcional está en Notion, que no se pudo verificar. |
| V2.1.2 | L2 | Parcial | El README describe algunas combinaciones (la factura exige RUC y razón social, el delivery exige nombre, teléfono y dirección, las reservas no se cruzan, el horario cruza la medianoche). No hay una especificación sistemática de la consistencia entre campos. |
| V2.1.3 | L2 | Cumple | El README documenta límites por cuenta (`EMAIL_MAX_PER_HOUR`, `LOOKUP_PER_MINUTE`, tope de descuento del mesero, DLP de 20 fichas en 10 min) y globales (`RATE_LIMIT_PER_MINUTE`, intentos de acceso 5/25, umbrales `SECURITY_ALERT_*`). |
| V2.2.1 | L1 | Parcial | Pydantic (`Field`, `EmailStr`, enums) más validación en el dominio (regex de RUC, serie, teléfono de cliente). Falta para L2: el teléfono de pedidos y reservas solo tiene tope de largo, sin patrón (`orders/domain/orders.py` `_short_text`, `reservations/domain/reservations.py`). |
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
| V3.3.1 | L1 | Parcial | backend: `core/session_cookie.py` `_set` con `secure=settings.session_cookie_secure` (por omisión true; test `test_login_sets_an_httponly_secure_cookie`). Faltan prefijos: `resthub_session`, `resthub_platform_session`, `resthub_csrf` no usan `__Host-`/`__Secure-`; nada impide `SESSION_COOKIE_SECURE=false` con `DEBUG=false`. |
| V3.3.2 | L2 | Cumple | backend: `SameSite=None` deliberado (frontend Vercel y API Railway son sitios distintos, `core/config.py` `session_cookie_samesite`), compensado con doble envío `X-CSRF-Token` (`token_from_cookie`). Test `test_changes_with_the_cookie_require_the_csrf_header`. |
| V3.3.3 | L2 | No cumple | backend: `core/session_cookie.py` nombres sin `__Host-` (`STAFF_COOKIE`, `PLATFORM_COOKIE`, `CSRF_COOKIE`). Ya usan `path="/"`, `Secure` y sin `Domain`: bastaría renombrarlas a `__Host-…` y ajustar tests. |
| V3.3.4 | L2 | Parcial | backend: cookie `HttpOnly` (test `test_login_sets_an_httponly_secure_cookie`), pero el mismo token sale en el cuerpo (`StaffAccessTokenResponse.access_token`, `accounts/adapters/api/schemas.py`; también en `/auth/refresh`). frontend lo guarda en memoria y lo manda como Bearer (`services/apiCredentials.ts`, `store/session.ts`). |
| V3.4.1 | L1 | Cumple | backend: `HSTS = "max-age=63072000; includeSubDomains"` en `core/security_headers.py` (test `test_every_api_response_carries_security_headers`). frontend: misma cabecera en `vercel.json` para `/(.*)`. |
| V3.4.2 | L1 | Cumple | backend: `main.py` `CORSMiddleware` con `allow_origins` explícitos y `allow_credentials`; regex solo en `develop`, validada entera (`core/config.py`). Tests `test_cors_is_read_as_json_or_comma_separated`, `test_cors_accepts_origins_matching_the_pattern`. |
| V3.4.3 | L2 | Parcial | backend: `API_CSP` con `default-src 'none'; base-uri 'none'` (cumple). frontend `vercel.json`: `object-src 'none'`, `script-src 'self'`, pero `base-uri 'self'` (ASVS pide `'none'`) y `style-src 'unsafe-inline'` (widget Sienna). Falta cambiar `base-uri` a `'none'`. |
| V3.4.4 | L2 | Cumple | backend: `nosniff` en `_HEADERS` y en `inline_file_response`; test `test_every_api_response_carries_security_headers`, `test_interactive_docs_do_not_carry_the_api_csp`. frontend: `vercel.json` `X-Content-Type-Options: nosniff`. |
| V3.4.5 | L2 | Cumple | backend: `Referrer-Policy: no-referrer` (`core/security_headers.py`, testeado). frontend: `strict-origin-when-cross-origin` en `vercel.json`; código de vista previa en el fragmento (`features/platform/previewLink.ts`). |
| V3.4.6 | L2 | Cumple | backend: `frame-ancestors 'none'` en `API_CSP` (testeado). frontend: `frame-ancestors 'none'` en la CSP de `vercel.json` (más `X-Frame-Options: DENY`). Única excepción: `/docs` y `/redoc`, solo con `DEBUG=true`. |
| V3.5.1 | L1 | Cumple | backend: con cookie, POST/PUT/PATCH/DELETE exigen `X-CSRF-Token` = cookie `resthub_csrf` con `hmac.compare_digest` (`core/session_cookie.py` `token_from_cookie`). Tests `test_changes_with_the_cookie_require_the_csrf_header`, `test_bearer_header_does_not_need_csrf`. |
| V3.5.2 | L1 | Cumple | backend: no depende solo del preflight: la cabecera `X-CSRF-Token` (no safelisted) es obligatoria aunque la petición sea "simple"; Bearer no lo manda el navegador solo (`core/auth.py`). Mismo test que V3.5.1. |
| V3.5.3 | L1 | Cumple | backend: las acciones sensibles son POST/PUT/PATCH/DELETE (README «Endpoints»; `SAFE_METHODS` no cambian estado y no piden CSRF). No se hallaron GET que modifiquen datos de negocio. |
| V3.5.4 | L2 | Parcial | Frontend (Vercel) y API (Railway) en hostnames distintos. Pero la consola de plataforma (`/plataforma`, `services/paths.ts`) y la app del personal comparten el mismo origen del frontend; solo se separan por cookie/alcance. |
| V3.5.5 | L2 | No aplica | frontend: no hay `postMessage` ni listeners `message` en `src/` (solo el service worker generado por Workbox). |
| V3.7.1 | L2 | Cumple | frontend: React 19, Vite 8, sin plugins NSAPI/Flash/Java; widget Sienna autoalojado (`vite.plugins.ts` `handleSiennaSelfHosted`). `pnpm audit --audit-level high` en `.github/workflows/ci.yml` y Dependabot. |
| V3.7.2 | L2 | Cumple | frontend: las redirecciones automáticas son rutas internas (`services/leaveTab.ts` `appUrl`, `features/auth/previewEntry.ts`); no hay parámetro `next`/`returnTo`. backend: sin `RedirectResponse`. |

### V4 Seguridad de API y servicios web

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V4.1.1 | L1 | Parcial | backend: JSON por `JSONResponse` (`application/json`), SSE `text/event-stream` con charset por Starlette. Pero el XML firmado sale `application/xml` sin `charset` (`billing/use_cases/invoicing.py` → `core/file_response.py`). Falta `; charset=utf-8`. |
| V4.1.2 | L2 | Fuera del código | backend: la app no redirige a HTTPS (sin `HTTPSRedirectMiddleware`), correcto para un API. La redirección HTTP→HTTPS la decide el borde de Railway/Vercel, no configurado en `railway.toml`/`vercel.json`. |
| V4.1.3 | L2 | Parcial | backend: `core/auth.py` `client_address` toma la última IP de `X-Forwarded-For` (test `test_counts_by_the_ip_the_proxy_appends`). Pero `Dockerfile` fija `FORWARDED_ALLOW_IPS="*"` (uvicorn confía en `X-Forwarded-*` de cualquiera) y `CLIENT_IP_HEADER` es falsificable si `*.up.railway.app` es accesible. |
| V4.2.1 | L2 | Fuera del código | Uvicorn (h11) detrás del proxy de Railway; el framing HTTP lo resuelven el servidor y el borde, no el código de la app. |
| V4.3.1 | L2 | No aplica | No hay GraphQL (ni dependencia ni rutas) en backend ni frontend. |
| V4.3.2 | L2 | No aplica | No hay GraphQL. |
| V4.4.1 | L1 | No aplica | No hay WebSocket: el tiempo real es SSE (`core/events_router.py`, «no hace falta un WebSocket»). |
| V4.4.2 | L2 | No aplica | Sin WebSocket (SSE por `GET /events`, sujeto a CORS). |
| V4.4.3 | L2 | No aplica | Sin WebSocket; el SSE usa la sesión normal (`StreamPrincipalDep`). |
| V4.4.4 | L2 | No aplica | Sin WebSocket. |

### V5 Manejo de archivos

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V5.1.1 | L2 | Parcial | El certificado está documentado en el README (.pfx/.p12/.pem, 64 KB). Las fotos de platos (JPEG/PNG/WebP, 400 KB) solo constan en código (`menu/domain/photos.py`), no en el README. No se documenta qué pasa con un archivo malicioso. |
| V5.2.1 | L1 | Parcial | Hay topes por función: foto de 400 KB (`MAX_PHOTO_BYTES` y `max_length` del base64) y certificado de 64 KB. Falta un tope global del cuerpo de la petición: FastAPI lee el JSON entero antes de validar `max_length` y uvicorn arranca sin límite (`Dockerfile` CMD). |
| V5.2.2 | L1 | Cumple | El tipo de la foto sale de los bytes mágicos, no del declarado (`detect_image_type`). El frontend reduce la foto y la vuelve a codificar en un canvas (frontend:`src/features/menu/dishPhotoFile.ts`). El certificado se abre con `cryptography` (`sunat/certificates.py`). Tests: `test_image_type_comes_from_the_bytes`, `test_invalid_photos_are_rejected`. |
| V5.2.3 | L2 | No aplica | No se aceptan archivos comprimidos del usuario. El CDR zip llega de Lycet y nunca se descomprime. |
| V5.3.1 | L1 | Cumple | Las fotos se guardan en la base (`menu_item_photos`), no en una carpeta pública. Se sirven por el API con permiso, tipo fijo, `nosniff` y una CSP que no deja ejecutar nada (`core/security_headers.py`). |
| V5.3.2 | L1 | Cumple | No se arman rutas del sistema de archivos con datos del usuario. Los nombres salen de `document_filename` (serie validada) y las carpetas de Drive se crean por API, sin rutas (`billing/adapters/archive/google_drive.py`). |
| V5.4.1 | L2 | Cumple | El nombre lo genera el servidor (`document_filename`) y siempre va en `Content-Disposition` (`core/file_response.py`). Tests: `tests/test_billing.py:734`, `tests/test_api_billing_voiding.py:251`. La foto se sirve inline, como imagen y sin nombre. |
| V5.4.2 | L2 | Cumple | `_content_disposition` deja un fallback ASCII saneado más `filename*=UTF-8''` codificado (RFC 6266). El adjunto del correo usa el mismo nombre generado. Falta un test con caracteres peligrosos. |
| V5.4.3 | L2 | No cumple | Ningún archivo subido pasa por un antivirus. El riesgo es bajo (solo imágenes validadas por firma, y certificados que no se vuelven a servir). Habría que volver a codificar la foto en el servidor o escanearla, por ejemplo con ClamAV. |

### V6 Autenticación

Revisado sobre `develop` (HEAD = `origin/develop`: backend `d13ba6c`, frontend `3e336db`).

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V6.1.1 | L1 | Cumple | backend:README.md «Acceso: intentos y renovación» y «Cabeceras de seguridad y límite de peticiones» (5 fallos por correo+IP, 25 por correo, 15 min, `RATE_LIMIT_PER_MINUTE`, WAF). El docstring de backend:src/resthub/core/login_throttle.py explica por qué el tope por correo es más alto (contra el bloqueo malicioso). |
| V6.1.2 | L2 | Parcial | No hay ninguna lista documentada de palabras propias del contexto. backend:src/resthub/core/common_passwords.txt solo suma al final unas pocas («restaurante», «cevicheria», «lima123456»), sin «resthub», el nombre del local ni el correo. |
| V6.1.3 | L2 | Parcial | Las vías están documentadas, pero cada una en su sección del README: `/auth/login`, `/platform/auth/login` (obsoleta), `/auth/preview`, desbloqueo con PIN y reautenticación. Falta un inventario único con la fuerza y los controles de cada vía. |
| V6.2.1 | L1 | Cumple | `MIN_PASSWORD_LENGTH = 10` en backend:src/resthub/core/credentials.py (`password_problem`); frontend:src/services/fieldLimits.ts. Test `test_new_password_has_a_minimum` y `test_short_new_password_is_rejected`. No llega a los 15 recomendados. |
| V6.2.2 | L1 | Parcial | El personal la cambia con `POST /auth/me/password` (backend:src/resthub/modules/accounts/adapters/api/auth_router.py, test `test_change_own_password`). Las cuentas de plataforma no tienen ningún endpoint para cambiar la suya: un token de plataforma recibe 401. |
| V6.2.3 | L1 | Cumple | `ChangeOwnPassword` verifica `current_password` antes de cambiarla (backend:src/resthub/modules/accounts/use_cases/change_own_password.py); `ChangeOwnPasswordRequest` pide las dos. Test `test_changing_password_requires_current_one`. |
| V6.2.4 | L1 | Parcial | Sí se comprueba al registrar, cambiar y restablecer (`validate_new_password`), con test `test_new_password_rejects_common_passwords`. Pero la lista (las del top 100k de SecLists con 10 o más caracteres) tiene unas 2 320 entradas, menos de las 3 000 que pide. Falta ampliarla con el top 1M filtrado. |
| V6.2.5 | L1 | Cumple | `password_problem` solo mira el largo y la lista de comunes, y `passwordRule` (frontend:src/services/fieldRules.ts) solo el mínimo y el máximo. No exige ningún tipo de carácter. |
| V6.2.6 | L1 | Cumple | frontend:src/components/PasswordField.tsx usa `type="password"` con un botón Mostrar/Ocultar; el PIN también va en `type="password"` (frontend:src/features/shell/security/PinUnlockForm.tsx). |
| V6.2.7 | L1 | Cumple | No hay `onPaste` ni bloqueo de pegado en `src/`. Los campos llevan `autoComplete="current-password"`/`"new-password"` (LoginFields.tsx, ChangePasswordFields.tsx, StaffPasswordForm.tsx), así que los gestores de contraseñas funcionan. |
| V6.2.8 | L1 | Cumple | Ni el esquema Pydantic ni zod hacen `strip` o cambian mayúsculas en la contraseña, y Argon2id verifica el texto tal cual. Test `test_valid_password_is_returned_as_is` (los espacios se conservan). Las minúsculas se usan solo para comparar con la lista de comunes. |
| V6.2.9 | L2 | Parcial | Se aceptan 64 caracteres ASCII o más (máx. 128; test con `"x" * 72`). Pero `MAX_PASSWORD_BYTES = 72` (por bcrypt) rechaza 64 caracteres con tildes o emojis. Con Argon2id ese tope de bytes ya no hace falta para los hashes nuevos. |
| V6.2.10 | L2 | Cumple | No hay vencimiento ni rotación periódica: no existe ningún campo de expiración en backend:src/resthub/modules/accounts/domain/entities.py ni en credentials.py. Solo se obliga a cambiar la contraseña temporal (`must_change_password`). |
| V6.2.11 | L2 | Parcial | Las palabras de contexto que hay en common_passwords.txt sí se rechazan, pero no existe una lista documentada. «resthub2026», el nombre o slug del local y el correo o nombre del usuario se aceptan. |
| V6.2.12 | L2 | Parcial | La lista local sale de un corpus de filtraciones (xato 100k), pero está muy recortada (unas 2 300). No se consulta un conjunto amplio de contraseñas filtradas (por ejemplo, el rango k-anónimo de HIBP o un corpus local grande). |
| V6.3.1 | L1 | Cumple | Hay un throttle en memoria y otro en BD compartido entre réplicas (backend:src/resthub/core/login_throttle.py, login_attempts.py), más `core/rate_limit.py`; la reautenticación usa el mismo contador. Tests `test_five_wrong_passwords_lock_that_email`, `test_rotating_ip_does_not_evade_the_per_email_cap`, `test_simultaneous_burst_evaluates_at_most_five_passwords`. |
| V6.3.2 | L1 | Cumple | Las migraciones no insertan cuentas. Las cuentas demo (`resthub123`) solo se siembran con `DEBUG` y una BD local, o con `ALLOW_DEMO_SEED` (backend:scripts/seed_dev.py). Las cuentas de plataforma se crean con `create_platform_admin.py --generate`. |
| V6.3.3 | L2 | Parcial | TOTP obligatorio para la plataforma (`platform_two_factor_required=True`, test `test_without_it_the_platform_answers_403_and_me_says_why`). Para el personal, encargados incluidos, es opcional, y no hay una justificación documentada ni una política por local para exigirlo. |
| V6.3.4 | L2 | Parcial | Las vías están documentadas y la 2FA se exige en las dos de login (`test_a_platform_account_uses_it_in_both_logins`). Pero la obsoleta `/platform/auth/login` tiene su propio contador (`get_platform_login_guard`), lo que duplica los intentos contra una cuenta de plataforma. Falta retirarla o unificar el contador. |
| V6.4.1 | L1 | Parcial | Las temporales cumplen la política y `must_change_password` bloquea la API hasta cambiarlas (test `test_reset_by_the_manager_is_temporary`). Pero las dicta una persona (no son aleatorias) y no vencen. La generada para una cuenta de plataforma (`create_platform_admin.py`) se queda como contraseña definitiva. |
| V6.4.2 | L1 | Cumple | No hay pistas ni preguntas secretas en el backend ni en el frontend; la recuperación es asistida por el encargado (frontend:src/features/auth/LoginView.tsx). |
| V6.4.3 | L2 | Cumple | No hay autoservicio. `ResetStaffPassword` (necesita `staff.manage` y no puede restablecer cuentas con más permisos) y `ResetOwnerPassword` (plataforma) fijan una temporal y cortan las sesiones. No tocan `two_factor_credentials`, así que la 2FA se sigue pidiendo. Falta un test que lo pruebe y un camino para las cuentas de plataforma. |
| V6.4.4 | L2 | Parcial | Si se pierde el celular, se entra con los códigos de recuperación (backend:src/resthub/core/two_factor.py). Si también se pierden, no hay un proceso documentado ni un endpoint para que el encargado o la plataforma reinicien la 2FA tras verificar la identidad: solo queda tocar la BD. |
| V6.5.1 | L2 | Parcial | Se guarda `last_step` y se borra el hash del código de recuperación usado (tests `test_a_code_tolerates_one_step_and_is_not_reused`, `test_a_recovery_code_works_once`). Pero `SqlTwoFactorStore.verify` lee la fila sin `for_update`: dos accesos simultáneos con el mismo código pueden pasar los dos. |
| V6.5.2 | L2 | No cumple | Los códigos de recuperación tienen unos 49 bits (10 caracteres de un alfabeto de 31) y se guardan con un SHA-256 sin sal (`hash_recovery`, backend:src/resthub/core/two_factor.py). Hay que pasarlos a Argon2id (`PasswordHasher`) o subir la entropía a 112 bits o más. |
| V6.5.3 | L2 | Cumple | `new_secret` usa `secrets.token_bytes(20)` y `new_recovery_codes` usa `secrets.choice` (backend:src/resthub/core/two_factor.py). |
| V6.5.4 | L2 | Cumple | Los códigos de recuperación tienen unos 49 bits y el TOTP 6 dígitos, por encima de los 20 bits pedidos. No hay códigos fuera de banda (OOB). |
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
| V7.1.1 | L2 | Parcial | Documentados: vida del token (`ACCESS_TOKEN_TTL_SECONDS`, 1 h) y bloqueo por inactividad (RFS-29, 10 min) en backend:README.md. Falta documentar una vida máxima absoluta (no existe) y justificar las desviaciones respecto de NIST SP 800-63B. |
| V7.1.2 | L2 | Parcial | backend:README.md (RFS-17) describe una sesión por dispositivo, listarlas y cerrarlas. No define cuántas sesiones simultáneas se permiten ni qué pasa al llegar al máximo; el código no pone ningún límite. |
| V7.1.3 | L2 | No aplica | No hay SSO ni identidad federada: el acceso es local (`POST /auth/login`, contraseña y TOTP). |
| V7.2.1 | L1 | Cumple | Toda verificación en el backend: backend:src/resthub/core/auth.py (`_resolve_principal`, firma, `sid`, cuenta). El frontend solo lee `exp` para la interfaz (frontend:src/services/tokenExpiry.ts). |
| V7.2.2 | L1 | Cumple | JWT firmado por sesión, con `sid` aleatorio y fila en `account_sessions` (backend:src/resthub/core/tokens.py, backend:src/resthub/core/account_sessions.py). No hay claves de API estáticas para los usuarios. |
| V7.2.3 | L1 | Cumple | `sid` = `secrets.token_urlsafe(32)` (256 bits, CSPRNG) en backend:src/resthub/core/account_sessions.py (`new_session_id`) y en tokens.py; CSRF también de 32 bytes (session_cookie.py). |
| V7.2.4 | L1 | Parcial | El login emite un token y un `sid` nuevos (tokens.py `issue`), pero no cierra la sesión que ya tenía el navegador (`auth_router.login` no revoca el `sid` anterior). `POST /auth/me/reauthenticate` y el desbloqueo con PIN no rotan el token. |
| V7.3.1 | L2 | Cumple | En el servidor: bloqueo 423 tras `idle_lock_minutes` (auth.py `_lock_if_idle`) y sesión muerta tras 1 h sin uso (`SqlSessionRegistry.is_alive`). Tests: backend:tests/test_idle_lock.py, test_account_sessions.py (`test_a_session_unused_longer_than_a_token_is_closed`). Ojo: `idle_lock_minutes=0` lo apaga. |
| V7.3.2 | L2 | No cumple | `POST /auth/refresh` conserva el mismo `sid` sin tope (auth_router.py `refresh_token`); `is_alive` solo mira `last_seen_at`. Falta una vida máxima desde `created_at` que obligue a volver a entrar. |
| V7.4.1 | L1 | Parcial | Logout revoca la fila (`revoked_at`) y borra cookies; cada petición comprueba el `sid` (auth.py `ensure_open_session`). Test: test_account_sessions.py `test_logout_closes_only_this_device`. Falta: el token de vista previa no tiene `sid` y el logout no lo invalida (vive hasta 30 min). |
| V7.4.2 | L1 | Cumple | Desactivar fija `sessions_valid_since` y `revoke_all` (accounts/domain/entities.py `deactivate`, staff_router.py); un local desactivado corta a todo su personal (auth.py). No hay borrado de cuentas. Test: backend:tests/test_session_cutoff.py. |
| V7.4.3 | L2 | Parcial | Cambiar o restablecer la contraseña cierra las demás sesiones (auth_router.py `change_own_password`, staff_router.py, restaurants_router.py). Activar o desactivar 2FA no cierra ni ofrece cerrar las demás (two_factor_router.py). Test: `test_changing_the_password_closes_the_other_devices`. |
| V7.4.4 | L2 | Cumple | «Cerrar sesión» en la barra lateral y en el panel «Más» de todo el shell autenticado (frontend:src/features/shell/SessionActions.tsx, SideNav.tsx, MoreSheet.tsx) y en la pantalla de bloqueo (LockScreen.tsx). |
| V7.4.5 | L2 | Parcial | Quien tiene `staff.manage` cierra una o todas las sesiones de un miembro (backend:src/resthub/modules/accounts/adapters/api/staff_security_router.py; test `test_owner_closes_every_session_of_a_member`). No hay forma de cerrar las sesiones de todos los usuarios salvo desactivar el local o rotar `JWT_SECRET_KEY`. |
| V7.5.1 | L2 | Parcial | 2FA, PIN, roles y datos fiscales exigen `reauthenticate` reciente (auth.py `require_reauthenticated`, two_factor_router.py); el correo no se puede cambiar. Pero la reautenticación solo pide la contraseña, no el segundo factor si está activo. |
| V7.5.2 | L2 | Parcial | `GET/DELETE /auth/me/sessions` (security_router.py) y frontend:src/features/auth/security/OwnSessionsCard.tsx. Cerrar sesiones no pide volver a autenticarse (usa `ActiveSessionSubjectDep`, sin `ensure_recently_authenticated`). |
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
| V9.1.2 | L1 | Cumple | `algorithms=[self._algorithm]`, uno solo y simétrico (HS256 por omisión, core/config.py). PyJWT no acepta `none` con clave. Mejora: `jwt_algorithm` es `str` libre; fijarlo con `Literal` y probar que `alg=none` se rechaza. |
| V9.1.3 | L1 | Cumple | La clave sale solo de `JWT_SECRET_KEY` (mín. 32 bytes, se rechaza el valor de desarrollo fuera de DEBUG: config.py `_validate_secret`, test_config.py). PyJWT no usa `jku`, `x5u` ni `jwk`. |
| V9.2.1 | L1 | Cumple | Se emite `exp` y PyJWT lo valida (tokens.py `_sign`); no se emite `nbf`. Test: test_tokens.py `test_expired_token_is_rejected`. Mejora: `options={"require": ["exp","iat"]}`. |
| V9.2.2 | L2 | Cumple | Solo hay un tipo de token (acceso). El claim firmado `scope` separa restaurante y plataforma y `decode`/`decode_platform` rechazan el ajeno; la vista previa se valida aparte. Tests: test_tokens.py (`test_platform_token_is_not_valid_as_restaurant_token`). |
| V9.2.3 | L2 | Parcial | No hay claims `aud` ni `iss` ni se validan; la separación depende de que `JWT_SECRET_KEY` sea única por entorno. Falta emitir y validar `aud`/`iss` (p. ej. por despliegue). |
| V9.2.4 | L2 | Cumple | Una misma clave firma dos «audiencias» (restaurante y plataforma) y cada token lleva `scope` firmado que la identifica, validado en tokens.py y auth.py. |

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
| V11.1.1 | L2 | Parcial | `backend:README.md` explica cómo generar, guardar y rotar `DATA_ENCRYPTION_KEY` y la llave de age de los respaldos. Falta una política formal (NIST SP 800-57): criptoperiodos, custodios, revocación. `JWT_SECRET_KEY` no tiene procedimiento de rotación. |
| V11.1.2 | L2 | Parcial | Algoritmos y llaves aparecen dispersos en el README (AES-GCM, HMAC, Argon2id, HS256, TOTP, age, certificado SUNAT). Falta un inventario único y mantenido de llaves, algoritmos y certificados, con dónde puede usarse cada uno y qué datos protege. |
| V11.2.1 | L2 | Cumple | `cryptography` (AESGCM, OpenSSL) en `backend:src/resthub/core/crypto.py`; `pwdlib`+argon2 en `core/security.py`; PyJWT en `core/tokens.py`; `hmac`/`secrets` de la biblioteca estándar; age en `.github/workflows/backup.yml`. Nada hecho a mano. |
| V11.2.2 | L2 | Parcial | Hay agilidad: prefijo `enc1:<id de llave>`, llaves previas y `backend:scripts/rotate_data_key.py` (prueba `test_rotation_script_reencrypts...`); los hashes se recalculan solos al subir el costo. Falta: el secreto TOTP (`two_factor_credentials.secret`, `String`) no se re-cifra al rotar; el JWT tiene un solo secreto, sin `kid`. |
| V11.2.3 | L2 | Parcial | AES-256, HMAC-SHA256 con llave de 32 bytes, secreto JWT de 32 bytes o más (`core/config.py`), TOTP de 160 bits. Pero no se valida el tamaño de la llave RSA del certificado SUNAT (`billing/adapters/sunat/certificates.py`): con RSA-2048 son unos 112 bits. |
| V11.3.1 | L1 | Cumple | Único cifrado simétrico: AESGCM (`backend:src/resthub/core/crypto.py`). Ni ECB, ni CBC, ni cifrado RSA con PKCS#1 v1.5 en `src/`, `scripts/` ni `alembic/`. |
| V11.3.2 | L1 | Cumple | AES-256-GCM con nonce aleatorio de 96 bits por valor (`core/crypto.py`, `core/data_keys.py`); respaldos con age (X25519 + ChaCha20-Poly1305). |
| V11.3.3 | L2 | Cumple | Cifrado autenticado GCM; una etiqueta inválida da `DataEncryptionError` (`backend:tests/test_data_encryption.py`). Detalles: no usa AAD (columna o fila), y `decrypt` acepta texto sin prefijo, que se toma como legado. |
| V11.4.1 | L2 | Parcial | SHA-256 y HMAC-SHA256 en todo lo demás; MD5 no aparece. El TOTP usa HMAC-SHA1 (`backend:src/resthub/core/two_factor.py`) para ser compatible con RFC 6238 y las apps de códigos: es un uso heredado, no aprobado para usos nuevos. |
| V11.4.2 | L2 | Cumple | Argon2id con t=3, m=64 MiB, p=1 (RFC 9106), configurable por `Settings`; bcrypt se migra al primer acceso. `backend:src/resthub/core/security.py`; pruebas en `backend:tests/test_password_hashing.py`. El PIN también usa Argon2id. |
| V11.4.3 | L2 | Cumple | Integridad con SHA-256 o más: HMAC-SHA256 (índice ciego), JWT HS256, etiqueta GCM. Los SHA-256 truncados (`key_id`, `public_session_ref`) solo identifican; no autentican. |
| V11.4.4 | L2 | No aplica | Ninguna llave se deriva de una contraseña: la de datos es aleatoria (`core/data_keys.py`), la de age la crea `age-keygen`, y la contraseña PKCS#12 solo abre el certificado. |
| V11.5.1 | L2 | Parcial | `secrets` (CSPRNG) en todo: `sid` y CSRF de 256 bits, código de vista previa de 256 bits, nonces. Pero los códigos de recuperación 2FA son 10 caracteres de un alfabeto de 30 (unos 49 bits, menos de 128) y se guardan con SHA-256 sin sal (`core/two_factor.py`). |
| V11.6.1 | L2 | Parcial | El par RSA lo emite la CA del certificado y la firma XML la hace Lycet, un servicio externo que no está en el repo. La app solo comprueba que la llave sea RSA y la fecha de vencimiento; no revisa tamaño ni exponente. No hay generación propia de llaves. |

### V12 Comunicación segura

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V12.1.1 | L1 | Fuera del código | TLS lo terminan Vercel y el proxy de Railway; ni `railway.toml`, `Dockerfile` (uvicorn HTTP plano en `0.0.0.0:$PORT`) ni `vercel.json` configuran versiones TLS. |
| V12.1.2 | L2 | Fuera del código | Suites de cifrado definidas por el borde de Vercel/Railway; nada en los repos. |
| V12.1.3 | L2 | No aplica | No hay autenticación por certificado de cliente (mTLS). |
| V12.2.1 | L1 | Cumple | HSTS 2 años en API y frontend (`core/security_headers.py`, `vercel.json`); CSP del frontend `connect-src 'self' https:` acotada al origen de `VITE_API_URL` al compilar (`vite.plugins.ts` `boundConnections`). TLS mismo lo provee el host. |
| V12.2.2 | L1 | Fuera del código | Certificados públicos los emiten Vercel/Railway (dominio configurado en el panel, README «Pasos» 3). |
| V12.3.1 | L2 | Parcial | backend: APIs externas por omisión `https://` (`core/config.py`: OpenRouter, TypeSafe, Factiliza), pero no se exige el esquema (`LYCET_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT` aceptan `http`). Conexión a PostgreSQL sin `ssl` requerido (`core/database.py` `connect_args` sin `ssl`). |
| V12.3.2 | L2 | Parcial | backend: clientes `httpx.AsyncClient` con verificación por omisión; no hay `verify=False` en `src/`. asyncpg sin `ssl='verify-full'`: la base no valida certificado. |
| V12.3.3 | L2 | Fuera del código | No hay servicios HTTP internos en producción (Lycet solo en local, `docker/lycet`); el tramo proxy→contenedor de Railway es HTTP en red privada, decisión del host. |
| V12.3.4 | L2 | Fuera del código | Sin certificados internos ni autofirmados en los repos; depende de la red interna de Railway. |

### V13 Configuración

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V13.1.1 | L2 | Cumple | backend: `README.md` «Variables de entorno» y secciones por integración (OpenRouter/TypeSafe, Lycet, Brevo, Google Drive, Factiliza, OTLP, GeoIP local, PostgreSQL). Ningún usuario puede aportar una URL externa a la que se conecte el servidor. frontend: solo `VITE_API_URL` (`.env.example`). |
| V13.2.1 | L2 | Parcial | backend: PostgreSQL con contraseña fija (`DATABASE_URL`); integraciones con claves estáticas (`LYCET_TOKEN`, `BREVO_API_KEY`, `FACTILIZA_TOKEN`, `OPENROUTER_API_KEY`). Solo Google Drive usa tokens cortos OAuth (`billing/adapters/archive/google_drive.py` `_token`). |
| V13.2.2 | L2 | Parcial | backend: `Dockerfile` corre como `resthub` sin privilegios y con código de solo lectura. Pero la app usa el mismo usuario dueño de las tablas y corre `alembic upgrade head` (README «Bitácoras de solo inserción», `Dockerfile` `CMD`). Falta un rol de BD con mínimos permisos separado del de migraciones. |
| V13.2.3 | L2 | Cumple | backend: `core/config.py` `_validate_secret` / `_validate_encryption_keys` rechazan el `JWT_SECRET_KEY` y la llave de desarrollo con `DEBUG=false` (tests `test_production_rejects_development_jwt_secret`, `test_production_rejects_sqlite`). Credencial de BD generada por Railway. |
| V13.2.4 | L2 | Cumple | backend: los destinos salientes son solo las URL base fijadas en `Settings` (`factiliza_base_url`, `typesafe_base_url`, `openrouter_base_url`, `lycet_url`, Drive/Brevo fijos); ningún endpoint descarga de URL del usuario (fotos se suben como bytes). |
| V13.2.5 | L2 | Parcial | Allowlist solo implícita en la capa de aplicación; ni `railway.toml` ni `Dockerfile` restringen el tráfico de salida del contenedor (sin firewall de egress). |
| V13.3.1 | L2 | Parcial | backend: secretos por variables de entorno de Railway (`SecretStr` en `core/config.py`, `.dockerignore` excluye `.env*`), gitleaks en CI (`.github/workflows/ci.yml`, `.gitleaks.toml`) en ambos repos, script de rotación `scripts/rotate_data_key.py`. No hay gestor de secretos/bóveda. |
| V13.3.2 | L2 | Fuera del código | Quién ve las variables del servicio depende de los permisos del proyecto en Railway/Vercel; todos los secretos los tiene un único proceso. |
| V13.4.1 | L1 | Cumple | backend: `.dockerignore` excluye `.git`, `.github`, `.githooks`, `tests`. frontend: Vercel publica solo el `dist` de Vite y `vercel.json` reescribe todo a `index.html`. |
| V13.4.2 | L2 | Parcial | backend: FastAPI sin `debug=True` (sin trazas en 500); `/docs` y `openapi.json` solo con `DEBUG=true` (`main.py` `create_app`). Pero `debug: bool = True` es el valor por omisión (`core/config.py`): falla abierto si no se define `DEBUG=false`. Sin test de docs ocultas. |
| V13.4.3 | L2 | Cumple | backend: sin `StaticFiles` ni `mount`. frontend: `vercel.json` reescribe toda ruta a `/index.html`, no hay listado. |
| V13.4.4 | L2 | Cumple | backend: ninguna ruta declara TRACE; Starlette responde 405 y CORS solo permite GET/POST/PUT/PATCH/DELETE/OPTIONS (`main.py` `_install_middlewares`). Sin test explícito. |
| V13.4.5 | L2 | Cumple | backend: `/api/v1/docs`, `/redoc`, `openapi.json` solo con `DEBUG=true`; observabilidad exige `PlatformPermission.OBSERVABILITY_READ` (test `test_only_the_platform_gets_into_the_panel`). `/health` público expone solo servicio, versión y estado de BD. |

### V14 Protección de datos

| Requisito | Nivel | Estado | Evidencia / qué falta |
|---|---|---|---|
| V14.1.1 | L2 | Parcial | El README identifica los datos personales (Ley 29733), los campos cifrados, las credenciales SOL, el certificado y el secreto 2FA. Falta una clasificación formal por niveles que cubra el nombre del cliente (en claro), el contenido del JWT y los datos solo codificados en base64. |
| V14.1.2 | L2 | Parcial | Hay requisitos sueltos: cifrado, retención (accesos 90 d, observabilidad 14 d, respaldos 30 d en `backend:tests/test_retention_periods.py`), redacción de logs, enmascarado. Remiten a un «Anexo J» externo. Faltan requisitos por nivel y un plazo para clientes y pedidos. |
| V14.2.1 | L1 | Parcial | Tokens en cookie `HttpOnly` o Bearer, SSE por `fetch` con cabecera (`frontend:src/services/eventStream.ts`), código de vista previa en el fragmento `#` (`frontend:src/features/platform/previewLink.ts`). Pero el teléfono va en `GET /customers?q=` y el DNI/RUC en la ruta `/lookup/dni/{dni}`. |
| V14.2.2 | L2 | Parcial | El API no usa caché compartido; la búsqueda de RUC/DNI se recuerda 10 min en memoria (`LOOKUP_CACHE_SECONDS`); los PDF llevan `private, no-store` (`backend:src/resthub/core/file_response.py`). Las respuestas JSON no traen `Cache-Control`, así que un proxy delante (Cloudflare) aplica su comportamiento por omisión. |
| V14.2.3 | L2 | Cumple | El frontend no tiene rastreadores ni analítica (CSP `script-src 'self'` en `frontend:vercel.json`). La IA externa recibe notas sin correo, teléfono, DNI ni nombre (`insights/domain/privacy.py`, `backend:tests/test_ai_privacy.py`) y se puede apagar. Los demás terceros son encargados declarados. |
| V14.2.4 | L2 | Parcial | Lo documentado está implementado y probado: cifrado (`backend:tests/test_data_encryption.py`), retención, redacción (`core/redaction.py`, `hide_parameters`), DLP (`test_api_customers_dlp.py`). Falta: el secreto 2FA queda fuera de la rotación, y sin niveles formales no hay contra qué verificar. |
| V14.3.1 | L1 | Parcial | `cleanSession` (`frontend:src/store/session.ts`) borra el token en memoria, la sesión guardada y el caché de React Query; el logout borra las cookies. Pero no hay `Clear-Site-Data`, y la cola sin conexión (`frontend:src/store/offlineQueue.ts`) queda en `localStorage` tras cerrar sesión, a propósito, con datos de delivery. |
| V14.3.2 | L2 | No cumple | `backend:src/resthub/core/security_headers.py` no pone `Cache-Control: no-store`; solo lo llevan los PDF (`core/file_response.py`). La ficha y el export ARCO del cliente, el secreto 2FA y los códigos de recuperación salen sin anti-caché. Falta poner `no-store` por omisión en las respuestas del API. |
| V14.3.3 | L2 | Parcial | El token ya no se guarda en el navegador (cookie `HttpOnly`) y el CSRF vive solo en memoria. Pero `localStorage` guarda el perfil (nombre, correo, permisos) y los pedidos en cola con nombre, teléfono y dirección del cliente, sin cifrar. El service worker solo guarda los archivos de la app. |

### V15 Codificación y arquitectura seguras

| Requisito | Nivel | Estado | Evidencia / qué falta |
| --- | --- | --- | --- |
| V15.1.1 | L1 | No cumple | Hay automatización (`backend:.github/dependabot.yml`, `frontend:.github/dependabot.yml`, revisión semanal y 7 días de espera), pero ningún documento fija plazos de remediación según la severidad (por ejemplo, crítica en 72 h, alta en 7 días) ni plazos para actualizar en general. |
| V15.1.2 | L2 | Parcial | Inventario de hecho en `backend:uv.lock` (PyPI, con hashes) y `frontend:pnpm-lock.yaml` (con integrity); `deptry`; `minimumReleaseAge` y `overrides` en `frontend:pnpm-workspace.yaml`.<br>Falta: generar y publicar un SBOM (CycloneDX/SPDX) en CI o en la release. |
| V15.1.3 | L2 | Parcial | Repartido en el README del backend (Argon2 en `core/cpu_bound.py`, límite de peticiones, colas y topes de telemetría, tiempos de espera de la IA y de Factiliza) y en docstrings (`core/request_deadline.py`: 12 s frente a los 15 s del cliente).<br>Falta: una sección única que enumere las operaciones costosas y sus defensas; el plazo por petición no está en el README. |
| V15.2.1 | L1 | Parcial | `pip-audit --strict` (`backend:.github/workflows/ci.yml`) y `pnpm audit --audit-level high` (`frontend:.github/workflows/ci.yml`) bloquean el CI.<br>Falta: plazos documentados contra los que medir; no hay escaneo programado (solo en push o PR); se ignora GHSA-vfj7-8cjw-p6xm sin fecha límite. |
| V15.2.2 | L2 | Cumple | `backend:core/rate_limit.py`, `core/login_throttle.py`, `core/cpu_bound.py`, `core/request_deadline.py` (503), `core/pagination.py` (`MAX_PAGE_SIZE`, `MAX_OFFSET`), topes de cola en `core/telemetry.py`, tareas de fondo en `core/background.py`.<br>Pruebas: `tests/test_security_headers_rate_limit.py`, `test_cpu_bound.py`, `test_api_bounds.py`, `test_login_concurrency.py`. |
| V15.2.3 | L2 | Parcial | Docs y OpenAPI solo con `DEBUG` (`backend:main.py`); `tests` fuera de la imagen (`.dockerignore`); el proveedor Pact vive en `tests/contract`.<br>Falta: `debug: bool = True` por omisión (`core/config.py`) abre los docs si alguien olvida `DEBUG=false`; la imagen copia `scripts/` (siembras, fuzzing, carbono). |
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
| V16.1.1 | L2 | Parcial | El README del backend documenta el panel de observabilidad (qué guarda, tablas `obs_*`, 14 días, acceso con token de plataforma, qué nunca se guarda), `access_history` (90 días), las bitácoras y OTel.<br>Falta: un inventario único por capa; dónde quedan los logs de stdout en Railway, quién los ve y cuánto duran; logs del navegador. |
| V16.2.1 | L2 | Parcial | structlog añade hora, nivel, logger, `request_id`, método, ruta y `trace_id` (`backend:core/logs.py`, `core/request_logging.py`); `obs_requests` guarda `account_id` y `restaurant_id`.<br>Falta: quién y la IP de origen no se vinculan a todos los logs de stdout (solo en algunos eventos `auth.*`). |
| V16.2.2 | L2 | Cumple | `TimeStamper(fmt="iso", utc=True)` en `backend:core/logs.py`; puerto `Clock` en UTC (`core/clock.py`, vigilado por `TID251` y `tests/test_architecture.py`). La sincronización de los relojes queda en manos de la infraestructura (Railway). |
| V16.2.3 | L2 | Cumple | Destinos: stdout, `obs_requests`/`obs_events` en la base propia y OTel solo con `OTEL_EXPORTER_OTLP_ENDPOINT`; todos documentados en el README. El frontend solo escribe en la consola (`frontend:src/services/logger.ts`), sin servicios de terceros. |
| V16.2.4 | L2 | Cumple | `LOG_JSON=true` en despliegue: una línea JSON por evento (`backend:core/logs.py`); los logs de uvicorn y de las librerías se reencauzan al mismo formato. Correlación por `request_id`, `X-Request-ID` y `trace_id`. |
| V16.2.5 | L2 | Parcial | El panel aplica `core/redaction.py` (claves y valores con aspecto de credencial) y la base usa `hide_parameters=True`; correo enmascarado y IP truncada.<br>Falta: stdout solo oculta 8 claves exactas (`core/logs.py::SENSITIVE_KEYS`) y no limpia textos ni tracebacks. Pruebas: `tests/test_telemetry_hardening.py`. |
| V16.3.1 | L2 | Parcial | Cada intento de inicio de sesión queda en `access_history` con su resultado y en `auth.login_succeeded`/`auth.login_failed` (`backend:modules/accounts/adapters/api/auth_router.py`); 2FA, PIN y salida van a la bitácora. Pruebas: `tests/test_access_history.py`.<br>Falta: evento explícito para la reautenticación fallida y para los dos primeros PIN equivocados. |
| V16.3.2 | L2 | Cumple | Cada 403 queda en `obs_requests` (ruta, estado, cuenta, restaurante) y como aviso `request.completed`; los picos generan alertas (`backend:modules/platform/adapters/security_alert_loop.py`). Prueba: `tests/test_security_alerts.py`. |
| V16.3.3 | L2 | Parcial | Se registran `http.rate_limited`, `auth.login_throttled`, `security.spike` y `auth.unfamiliar_access`.<br>Falta: una lista documentada de eventos de seguridad; un evento explícito para el CSRF rechazado y los 422; los 4xx no van a `obs_events`. |
| V16.3.4 | L2 | Cumple | Las excepciones no controladas quedan como `request.failed` con su traceback (`backend:core/request_logging.py`); también `database.unavailable` y los fallos de terceros (`billing.*_unreachable`, `llm.request_failed`, `jev.timeout`). Prueba: `tests/test_telemetry_capture.py`. |
| V16.4.1 | L2 | Cumple | El renderizador JSON escapa los saltos de línea; un `X-Request-ID` ajeno se valida con la regex `[A-Za-z0-9._-]{8,128}` (`backend:core/request_logging.py`). Pruebas: `tests/test_logging.py::test_a_malformed_incoming_request_id_is_replaced` y `tests/test_redaction_redos.py`. |
| V16.4.2 | L2 | Parcial | `activity_log`, `platform_activity` y `ai_decisions` rechazan UPDATE y DELETE por trigger (`backend:alembic/versions/0034_append_only_logs.py`, `tests/test_migrations.py::test_logs_are_append_only`); el panel exige el token de plataforma.<br>Falta: proteger contra TRUNCATE al dueño de la tabla; `obs_*` es modificable. |
| V16.4.3 | L2 | Parcial | stdout llega a la plataforma de Railway y OTel puede enviar las trazas a un colector externo (`core/tracing.py`), pero el panel y las bitácoras viven en la misma base de la app ("no hay servicios externos", README).<br>Falta: enviar los logs a un SIEM separado, con sus alertas. |
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
