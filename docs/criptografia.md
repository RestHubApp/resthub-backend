# Llaves, algoritmos y su ciclo de vida

Inventario de lo que el backend cifra, firma o resume, con qué llave y cómo se cambia
(OWASP ASVS 5.0, V11.1.1 y V11.1.2). Se actualiza en el mismo PR que agregue o cambie
una llave, un algoritmo o un dato protegido. El TLS lo terminan el proxy de Dokploy en el VPS
(API) y Vercel (frontend): queda fuera de este inventario. Hacia la base, en Supabase, va
TLS con `verify-full` (`docs/migracion-supabase.md`).

## Llaves y secretos

| Llave | Variable | Algoritmo y uso | Qué protege | Rotación hoy |
|---|---|---|---|---|
| Llave de datos | `DATA_ENCRYPTION_KEY` (32 bytes en base64; `uv run python -m resthub.core.data_keys` genera una) | AES-256-GCM, nonce aleatorio de 96 bits por valor, sin AAD. Formato `enc1:<id de llave>:<base64url(nonce+cifrado+etiqueta)>`; el id son 8 hex de `SHA-256("resthub:key-id:"+llave)` (`core/crypto.py`, `core/data_keys.py`). | Las columnas `EncryptedText` (`core/encrypted_types.py`), ver la lista de abajo. | Sí: `DATA_ENCRYPTION_PREVIOUS_KEYS` y `scripts/rotate_data_key.py`. |
| Llaves anteriores | `DATA_ENCRYPTION_PREVIOUS_KEYS` (separadas por comas) | Solo descifran, nunca cifran (`DataCipher` en `core/crypto.py`). | Lo cifrado antes de la rotación. | Se vacía al terminar la rotación. |
| Llave del índice ciego | Derivada: `HMAC-SHA256(DATA_ENCRYPTION_KEY, "resthub:blind-index:v1")` | HMAC-SHA256 del teléfono sin espacios (`DataCipher.blind_index`). | `customers.phone_key`: buscar por igualdad sin descifrar. | Cambia con la llave de datos; el script la recalcula. |
| Secreto TOTP | Uno por cuenta, aleatorio de 160 bits (`core/two_factor.py`, `new_secret`) | HMAC-SHA1, 6 dígitos, 30 s, ±1 paso (RFC 6238). Se guarda cifrado con la llave de datos en una columna `EncryptedText` (migración `0045`). | `two_factor_credentials.secret`. | Sí: entra en `scripts/rotate_data_key.py` como las demás columnas `EncryptedText` (prueba `test_rotating_the_key_keeps_two_factor_working`). |
| Secreto JWT | `JWT_SECRET_KEY` (32 bytes o más; `core/config.py` lo exige y rechaza el valor de desarrollo con `DEBUG=false`) | HMAC con `JWT_ALGORITHM`, que solo admite `HS256`, PyJWT (`core/tokens.py`). Un solo secreto para los tres alcances: personal, plataforma y vista previa; la frontera es el `scope` firmado y el `aud` de cada alcance (`JWT_AUDIENCE`, `JWT_PLATFORM_AUDIENCE`). | La firma del token de acceso (`sub`, `scope`, `restaurant_id`, `sid`, `iss`, `aud`, `iat`, `exp`). | **Sin rotación gradual:** no hay `kid` ni secreto anterior. Cambiarlo invalida todos los tokens (ver abajo). |
| Llave de respaldos | `BACKUP_AGE_RECIPIENT` (pública, secreto de GitHub); la privada, fuera de GitHub, del VPS y de Supabase | age: X25519 + ChaCha20-Poly1305 (`.github/workflows/backup.yml`). | Los volcados diarios de la base (artefacto de 30 días). | Sí, a mano (ver abajo). |
| Certificado digital SUNAT | Lo sube cada local (`PUT /billing/settings/certificate`, `.pfx`/`.p12`/`.pem`) | Par RSA que emite la CA del local; la app solo exige RSA, que la llave corresponda al certificado y que no esté vencido (`billing/adapters/sunat/certificates.py`). Firma Lycet, fuera del repositorio. | La firma de boletas, facturas y notas de crédito. | Sí: el encargado sube otro (pide contraseña confirmada). La contraseña del `.pfx` no se guarda. |
| Credenciales SOL | Las escribe el local (`PUT /billing/settings`) | Se guardan en `EncryptedText`. | El acceso del local a SUNAT. | Las cambia el local; rotan con la llave de datos. |
| Secretos de integración | `LYCET_TOKEN`, `BREVO_API_KEY`, `FACTILIZA_TOKEN`, `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`, `GOOGLE_DRIVE_CLIENT_SECRET` y `GOOGLE_DRIVE_REFRESH_TOKEN`, `OTEL_EXPORTER_OTLP_HEADERS` | Credenciales de terceros enviadas por HTTPS. Solo `LYCET_TOKEN` es `SecretStr`. | Las llamadas a cada proveedor. | En el panel del proveedor; se reemplaza la variable y se reinicia. |

`DEV_KEY` (`core/data_keys.py`) es una llave de datos **pública** para desarrollo y
pruebas. Con `DEBUG=false` la configuración no arranca sin una llave propia ni con
esa (`Settings._validate_encryption_keys`).

### Columnas cifradas con la llave de datos

Las descubre `scripts/rotate_data_key.py` (`encrypted_columns`), así que una columna
`EncryptedText` nueva entra sola en la rotación.

| Tabla | Columnas |
|---|---|
| `customers` | `phone`, `email`, `address`, `reference`, `notes` |
| `orders` | `customer_phone`, `delivery_address`, `delivery_reference`, `notes` |
| `reservations` | `phone`, `notes` |
| `invoices` | `customer_document_number`, `customer_address`, `signed_xml`, `cdr_zip`, `document_payload` |
| `credit_notes` | `signed_xml`, `cdr_zip`, `document_payload` |
| `billing_settings` | `sol_user`, `sol_password`, `certificate_pem` (llave privada en PKCS#8 sin contraseña más el certificado) |
| `two_factor_credentials` | `secret` |

## Valores aleatorios y resúmenes sin llave

No son llaves, pero dependen de la misma biblioteca (`secrets`, `hashlib`, `hmac`):

| Qué | Cómo | Dónde |
|---|---|---|
| Contraseñas y PIN | Argon2id, t=3, m=64 MiB, p=1 (`PASSWORD_ARGON2_*`); bcrypt solo se verifica y se reemplaza en el primer acceso | `core/security.py` |
| `sid` de la sesión del dispositivo | `secrets.token_urlsafe(32)` (256 bits), firmado dentro del JWT; la fila se busca por él | `core/tokens.py`, `core/account_sessions.py` |
| Nombre público de la sesión | 24 hex de `SHA-256(sid)`; solo identifica | `core/account_sessions.py` (`public_session_ref`) |
| Valor anti-CSRF | `secrets.token_urlsafe(32)`, doble envío, comparado con `hmac.compare_digest` | `core/session_cookie.py` |
| Código de vista previa | `secrets.token_urlsafe(32)`, guardado como SHA-256, 60 s, un uso | `modules/accounts/domain/preview.py` |
| Códigos de recuperación 2FA | 28 caracteres de un alfabeto de 31 (unos 139 bits), guardados como SHA-256 con prefijo `v3:`; los de antes (24 caracteres con `v2:`, unos 119 bits, y 10 sin prefijo, unos 49) valen hasta renovarlos (`recovery_codes_outdated`) | `core/two_factor.py` |
| Contador de intentos de acceso | SHA-256 del correo y de la IP, sin sal | `core/login_attempts.py` |

## Procedimientos

### Rotar la llave de datos

Paso a paso en `scripts/rotate_data_key.py` y en el README («Cifrado en reposo de
datos personales»):

1. Generar la nueva: `uv run python -m resthub.core.data_keys`.
2. Pasar la vigente a `DATA_ENCRYPTION_PREVIOUS_KEYS` y la nueva a
   `DATA_ENCRYPTION_KEY`; reiniciar.
3. `uv run python scripts/rotate_data_key.py --dry-run`, luego sin `--dry-run`. Es
   idempotente y recorre también los secretos TOTP. Mientras corre, la búsqueda por
   teléfono no encuentra clientes.
4. Vaciar `DATA_ENCRYPTION_PREVIOUS_KEYS` cuando no quede nada por rotar. Guardar la
   llave vieja mientras existan respaldos cifrados con ella (30 días).

### Cambiar `JWT_SECRET_KEY`

No hay convivencia de dos secretos: al reiniciar con el nuevo, todo token emitido
antes deja de valer y cada cuenta vuelve a iniciar sesión (personal, plataforma y
vistas previas). Las filas de `account_sessions` quedan huérfanas y se purgan solas a
los 120 días sin uso (`core/account_sessions.py`, `PURGE_AFTER`).

1. Generar 32 bytes o más (por ejemplo, `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Reemplazar la variable en el servicio de Dokploy fuera del horario de atención de los locales y
   reiniciar.

Rotarlo sin cortar sesiones exige un `kid` en la cabecera y una lista de secretos
anteriores, como la llave de datos. Queda pendiente (V11.2.2, brecha 8 de
`docs/asvs-5.0.md`).

### Cambiar la llave de respaldos

1. `age-keygen -o resthub-backup-AAAAMM.key` y guardar la privada en el gestor de
   secretos.
2. Poner la pública nueva en el secreto `BACKUP_AGE_RECIPIENT`.
3. Conservar la privada anterior 30 días más (lo que vive el último artefacto cifrado
   con ella) y destruirla después.

## Política de ciclo de vida

Basada en NIST SP 800-57 parte 1, ajustada al tamaño del equipo:

| Llave | Período de uso | Rotación extraordinaria |
|---|---|---|
| `DATA_ENCRYPTION_KEY` | 12 meses | Fuga o sospecha de fuga de la llave o de un respaldo junto con ella; salida de alguien que la tuvo. |
| `JWT_SECRET_KEY` | 12 meses, con aviso a los locales | Fuga o sospecha; salida de alguien con acceso a Dokploy o al VPS. |
| Llave de age | 24 meses | Fuga de la privada (con ella se lee cualquier respaldo de su período). |
| Secretos de integración | Los que fije cada proveedor; como máximo 12 meses | Fuga, o salida de alguien con acceso al panel del proveedor. |
| Certificado SUNAT | Lo que dure su vigencia (la app no deja subir uno vencido y, si vence, deja los comprobantes sin enviar a SUNAT: `billing/use_cases/invoicing.py`) | El local lo revoca ante su CA si se filtra y sube el nuevo. |

- **Custodia.** Toda llave vive en el gestor de secretos del equipo (según la
  recomendación del Capstone), y desde ahí se copia a las variables del servicio en Dokploy o a los
  secretos de GitHub. Nunca en el repositorio (gitleaks corre en el CI), en un chat ni
  en un ticket. La privada de age y las llaves de datos retiradas solo existen en el
  gestor. Pueden leerlas las personas del equipo que despliegan; nadie más.
- **Revocación.** Ante una fuga: rotar de inmediato (sin esperar el período), revisar
  `access_history`, `account_sessions` y la bitácora desde la fecha probable de la
  fuga, y tratarla como incidente de severidad crítica (`SECURITY.md`).
- **Destrucción.** Una llave retirada se borra del gestor cuando ya no hay datos ni
  respaldos cifrados con ella.
- **Registro.** Cada rotación se anota (fecha, llave, quién la hizo) en el gestor de
  secretos o en el canal del equipo; los valores nunca.
