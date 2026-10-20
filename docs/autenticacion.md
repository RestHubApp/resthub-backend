# Vías de autenticación

Todas las formas de obtener, renovar o confirmar una sesión en el backend, con su
fuerza y sus controles (OWASP ASVS 5.0, V6.1.3). Una vía nueva, o un cambio en los
controles de una existente, se anota aquí en el mismo PR. Rutas bajo `/api/v1`.

## Resumen

| Vía | Ruta | Quién | Factores | Límite de intentos | Resultado |
|---|---|---|---|---|---|
| Login | `POST /auth/login` | Personal y plataforma | Contraseña + TOTP (obligatorio en plataforma, opcional en el personal) | 5 por correo e IP y 25 por correo en 15 min | Token de 1 h en cookie `HttpOnly` y en el cuerpo, más `csrf_token` |
| Login de plataforma (obsoleto) | `POST /platform/auth/login` | Plataforma | Contraseña + TOTP | El mismo contador que `POST /auth/login` | Token en el cuerpo, sin cookie |
| Vista previa | `POST /auth/preview` | Plataforma, como una cuenta del local de muestra | Código de un uso de 256 bits | No hace falta (entropía); límite global por IP | Token de 30 min con su propia sesión, solo Bearer, no se renueva |
| Desbloqueo | `POST /auth/me/unlock` | Personal con la sesión bloqueada | PIN (4 a 6 dígitos) o contraseña, sobre un token válido | 3 fallos cierran la sesión | La misma sesión, desbloqueada; con `rotate_session: true`, un token con otro `sid` |
| Reautenticación | `POST /auth/me/reauthenticate` | Personal y plataforma | Contraseña, más el código de dos pasos si está activo, sobre un token válido | El mismo límite del login, con clave propia | Marca de 5 min en la sesión del dispositivo; con `rotate_session: true`, un token con otro `sid` |
| Renovación | `POST /auth/refresh` (y `POST /platform/auth/refresh`, obsoleto) | Personal y plataforma | El token vigente | Límite global por IP | Token nuevo con el mismo `sid`, hasta 12 h desde que se abrió la sesión |
| Recuperar el anti-CSRF | `GET /auth/me` | Personal y plataforma | La cookie de sesión | Límite global por IP | Devuelve el `csrf_token` de la cookie |

Comunes a todas:

- **Contraseñas**: 10 a 128 caracteres (contados como caracteres, sin tope de bytes),
  fuera de la lista de comunes y sin las palabras del contexto (`core/credentials.py`,
  `core/common_passwords.txt`), guardadas con Argon2id (`core/security.py`). El PIN
  también va con Argon2id.
- **Token**: JWT HS256 firmado con `JWT_SECRET_KEY`, con `scope` (`restaurant` o
  `platform`), `sid`, `iss`, `aud` (uno por alcance) e `iat`; al leerlo se exigen
  `exp`, `iat`, `sub`, `iss` y `aud` (`core/tokens.py`). Cada petición relee la cuenta y la
  sesión de la base (`core/auth.py`): una cuenta desactivada, una contraseña cambiada
  (`sessions_valid_since`) o una sesión cerrada cortan el token aunque no haya vencido.
- **Sesión del dispositivo**: fila en `account_sessions` por `sid`, con una vida
  máxima de `SESSION_ABSOLUTE_LIFETIME_HOURS` (12) desde que se abrió y a lo sumo
  `MAX_SESSIONS_PER_ACCOUNT` (10) por cuenta: la de más cierra la usada hace más
  tiempo. Se listan y se cierran desde `GET/DELETE /auth/me/sessions`
  (`core/account_sessions.py`); cerrar las de otros dispositivos pide la contraseña
  confirmada.
- **Cookie**: `resthub_session` o `resthub_platform_session`, `HttpOnly`, `Secure` y
  `SameSite=None` por omisión; con la cookie, todo POST/PUT/PATCH/DELETE exige
  `X-CSRF-Token` (doble envío, `core/session_cookie.py`). `Authorization: Bearer`
  tiene prioridad y no pide CSRF.
- **Límite global**: `RATE_LIMIT_PER_MINUTE` por IP (`core/rate_limit.py`).
- **Rastro**: cada intento de login va a `access_history`; los eventos están en
  [`docs/eventos-de-seguridad.md`](eventos-de-seguridad.md).

## Detalle por vía

### `POST /auth/login`

`modules/accounts/adapters/api/auth_router.py`. Un solo acceso para las dos clases de
cuenta: prueba primero al personal y, si la credencial no sirve, a la plataforma.

1. Reserva el intento antes de verificar nada (`LoginGuard.acquire`,
   `core/login_attempts.py`): 5 fallos por correo e IP o 25 por correo en 15 minutos
   responden 429 con `Retry-After`. Se cuenta en memoria (`core/login_throttle.py`) y
   en la tabla `login_attempts`, compartida entre réplicas.
2. Verifica la contraseña. Un correo inexistente, una contraseña equivocada y una
   cuenta desactivada responden el mismo 401 y gastan el mismo contador; contra un
   correo inexistente se verifica igual un hash de relleno (`dummy_hash`).
3. Si la cuenta tiene la verificación en dos pasos, pide `otp_code`: sin él, 401
   `two_factor_required`; equivocado, 401 `invalid_two_factor_code`, que cuenta como
   fallo (`check_second_factor`, `core/two_factor.py`).
4. Un local desactivado responde 403 (`InactiveRestaurantError`) y queda como
   `blocked`.
5. Cierra la sesión que el navegador traía en su cookie, abre la del dispositivo,
   anota el acceso, avisa por SSE si es un dispositivo o país nuevo y deja el token en
   la cookie.

Una contraseña temporal (alta o restablecimiento por otra persona, o la que genera
`create_platform_admin.py`) deja la cuenta con `must_change_password`: hasta cambiarla,
casi toda la API responde 403 `password_change_required`. Vence a las
`TEMPORARY_PASSWORD_HOURS` (72): después, el login responde 401
`temporary_password_expired` y hay que pedir otra.

### `POST /platform/auth/login` (obsoleto)

`modules/platform/adapters/api/auth_router.py`, marcado `deprecated` en el OpenAPI.
Mismos pasos y misma verificación en dos pasos, pero:

- usa el mismo `LoginGuard` que `POST /auth/login`, así que los intentos de las dos
  rutas suman en un solo contador (V6.3.4);
- no deja cookie: el token va solo en el cuerpo.

Se borra cuando el frontend deje de usarla. Hasta entonces, cualquier cambio en el
login se replica aquí.

### `POST /auth/preview`

La plataforma pide el código con `POST /platform/preview` (exige token de plataforma,
verificación en dos pasos activa y `platform.preview.open`, y lo deja en
`platform_activity`). La pestaña nueva lo canjea:

- código de `secrets.token_urlsafe(32)`, guardado como SHA-256, válido 60 segundos y
  una sola vez (`modules/accounts/domain/preview.py`); se marca usado antes de
  responder;
- el token dura 30 minutos (`PREVIEW_TOKEN_TTL_SECONDS`), no se renueva, viaja solo
  como Bearer (sin cookie) y lleva firmado quién la abrió (`platform_admin_id`); abre
  su propia fila en `account_sessions`, así que salir de la vista previa lo invalida;
- la sesión de vista previa no puede cambiar contraseña, PIN, verificación en dos
  pasos ni términos (`PreviewSessionRestrictedError`);
- las cuentas del local de muestra no tienen una contraseña que alguien conozca
  (`unusable_password_secret`).

### `POST /auth/me/unlock`

`modules/accounts/adapters/api/idle_lock_router.py`. La sesión se bloquea por
inactividad (`restaurants.idle_lock_minutes`, 10 por omisión) o a pedido; bloqueada,
el resto de la API responde 423. Se desbloquea con el PIN o, sin PIN, con la
contraseña. El tercer fallo cierra la sesión (401 `unlock_attempts_exhausted`) y queda
en la bitácora (`session_locked_out`); cada fallo queda en el log
(`auth.unlock_failed`). Con `rotate_session: true` responde un token con otro `sid` y
el anterior deja de valer. No crea una sesión nueva: requiere el token de una
sesión ya abierta en ese dispositivo. Solo para el personal: la plataforma no se usa
en un celular compartido.

### `POST /auth/me/reauthenticate`

`modules/accounts/adapters/api/security_router.py`. Pide la contraseña otra vez y
marca la sesión por `REAUTHENTICATION_WINDOW_SECONDS` (300). Sin esa marca, 403
`reauthentication_required` en: datos fiscales y credenciales SOL, certificado
digital, crear/editar/borrar roles, cambiar el rol de alguien, desactivar una cuenta o
un local, y elegir o quitar el PIN o la verificación en dos pasos. Los fallos usan el
mismo límite que el login, con la clave `reauthenticate:<tipo>:<id>`.

Si la cuenta tiene la verificación en dos pasos, pide también `otp_code` (de la
aplicación o de recuperación), como el login: sin él, 401 `two_factor_required`;
equivocado, 401 `invalid_two_factor_code`. Con `rotate_session: true` responde un
token con otro `sid`; el frontend manda `false` y el token no cambia (V7.2.4, brecha 6
de `docs/asvs-5.0.md`).

### `POST /auth/refresh`

Entrega un token nuevo, con el mismo `sid`, a una sesión que sigue válida (cuenta
activa, sesión abierta, contraseña sin cambiar). El frontend lo pide antes de que
venza el de una hora. La vista previa no se renueva. La renovación no alarga la sesión más
allá de `SESSION_ABSOLUTE_LIFETIME_HOURS` (12) desde `account_sessions.created_at`:
pasado eso, responde 401 y se vuelve a entrar.

### `GET /auth/me` y la cookie

Devuelve la sesión y, si la petición trae la cookie, el `csrf_token` que está en la
cookie `resthub_csrf`. Sirve a la pestaña que recargó y perdió el valor anti-CSRF,
sin pedir la contraseña. No es un login: solo funciona con una cookie de sesión
válida, y CORS limita qué orígenes pueden leer la respuesta.

### Altas y restablecimientos fuera del login

No hay «olvidé mi contraseña» por correo. Las contraseñas las fija otra persona y
quedan temporales:

- el encargado (`staff.manage`) da de alta al personal o le restablece la contraseña
  (`POST /staff`, `POST /staff/{id}/password`), lo que también cierra sus sesiones;
- la plataforma crea o restablece al encargado de un local
  (`/platform/restaurants/{id}/owners`);
- las cuentas de plataforma se crean y se activan por consola
  (`scripts/create_platform_admin.py --generate`, `scripts/set_platform_admin_status.py`);
  la contraseña generada es temporal y la cuenta la cambia con `POST /auth/me/password`.

## Verificación en dos pasos

TOTP de 6 dígitos y 30 s (RFC 6238) más diez códigos de recuperación de un uso, de 24
caracteres (unos 119 bits)
(`core/two_factor.py`, `modules/accounts/adapters/api/two_factor_router.py`). Empezar
el alta, renovar los códigos y apagarla piden la contraseña confirmada; confirmar el
alta pide un código válido. Un código ya usado no vuelve a servir, tampoco en dos accesos simultáneos (la
credencial se lee con `FOR UPDATE`). Activarla o apagarla cierra las demás sesiones.

### Política

| Cuenta | Verificación en dos pasos |
|---|---|
| Plataforma | **Obligatoria.** Sin ella, todo lo de plataforma responde 403 `two_factor_setup_required` salvo activarla (`get_second_factor_admin`, `modules/platform/adapters/api/dependencies.py`; `PLATFORM_TWO_FACTOR_REQUIRED`, que solo se apaga en las pruebas E2E). |
| Personal del local, encargado incluido | **Opcional.** Cada cuenta la activa desde su perfil. |

Por qué es opcional para el personal:

- Los locales son pequeños: el mesero y la cocina suelen usar un celular o una tablet
  del local, compartidos. Un TOTP atado al celular personal de cada uno no encaja con
  ese uso, y uno atado al equipo compartido no agrega un segundo factor real.
- Lo que protege a esos equipos es otra cosa: el bloqueo por inactividad con PIN, el
  cierre de sesiones a distancia, el límite de intentos, el aviso de dispositivo o
  país nuevo y la reautenticación para lo crítico.
- El encargado, que es quien tiene `staff.manage`, facturación y los datos de los
  clientes, puede activarla en su propia cuenta cuando quiera.

Es una decisión de riesgo deliberada, **pendiente de revisión por el equipo**
(V6.3.3, brecha 2 de `docs/asvs-5.0.md`). La alternativa en estudio es exigirla a
los roles con `staff.manage` o permisos de facturación, o dejar que cada local la
exija para su personal.
