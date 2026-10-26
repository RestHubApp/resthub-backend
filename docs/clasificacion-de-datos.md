# Clasificación de datos

Qué guarda RestHub, en qué nivel cae cada tabla y qué protección y plazo de
conservación le corresponde (OWASP ASVS 5.0, V14.1.1 y V14.1.2; Ley N.º 29733 de
Protección de Datos Personales). Las 37 tablas son las de `Base.metadata` con todos
los modelos cargados:

```bash
uv run python -c "from resthub.core.database import Base; import resthub.main; print(sorted(Base.metadata.tables))"
```

Una tabla o columna nueva entra en este documento en el mismo PR (lo pide la plantilla
de PR, sección «Privacidad (PI-04)»).

## Niveles

| Nivel | Qué es | Daño si se filtra |
|---|---|---|
| **Pública** | Lo que el local ya muestra a sus clientes: la carta. | Ninguno. |
| **Interna** | La operación del local: mesas, inventario, compras, caja, configuración. | Para el negocio (costos, márgenes), no para personas. |
| **Controlada** | Datos del personal y de proveedores, registros de seguridad y auditoría, telemetría. Identifican a personas, pero no a los clientes finales ni dan acceso. | A la privacidad del personal; ayuda a un atacante a planear. |
| **Restringida** | Datos personales de los clientes (Ley N.º 29733), documentos tributarios y toda credencial o secreto (hashes, credenciales SOL, certificado, secreto TOTP). | Directo a personas o permite entrar al sistema o firmar en nombre del local. |

## Tablas por nivel

### Pública (3)

| Tabla | Contenido |
|---|---|
| `menu_categories` | Nombre y orden de las categorías. |
| `menu_items` | Plato, descripción, precio, opciones, disponibilidad. |
| `menu_item_photos` | La foto del plato (bytes y tipo). |

### Interna (13)

| Tabla | Contenido |
|---|---|
| `restaurants` | Nombre, slug, zona horaria, horario, tope de descuento del mesero, umbral de mermas, bloqueo por inactividad, IA externa sí o no. |
| `roles` | Roles del local y sus permisos. |
| `dining_tables` | Mesas. |
| `cash_sessions` | Aperturas y cierres de caja, montos y notas. |
| `order_payments` | Pagos de cada pedido: método, monto, propina, quién cobró. |
| `ingredients` | Insumos, costos, stock mínimo, presentaciones. |
| `recipe_lines` | Recetas. |
| `stock_movements` | Compras, consumos, mermas y ajustes, con su motivo. |
| `stock_counts`, `stock_count_lines` | Conteos físicos. |
| `waste_approvals` | Mermas que esperan aprobación. |
| `purchase_orders`, `purchase_order_lines` | Órdenes de compra. |

### Controlada (11)

| Tabla | Contenido |
|---|---|
| `suppliers` | Proveedor, **nombre del contacto y teléfono en claro**, notas. |
| `access_history` | Cada intento de acceso: resultado, dispositivo, IP sin su último bloque, ciudad y país. |
| `account_sessions` | Sesiones abiertas: `sid`, dispositivo, red, país, último uso, bloqueo. |
| `login_attempts` | SHA-256 del correo y de la IP de los intentos recientes. |
| `preview_codes` | SHA-256 del código de vista previa, quién la abrió. |
| `activity_log` | Bitácora de cada local (`ActivityKind`, quién, cuándo, detalle de hasta 200 caracteres). |
| `platform_activity` | Bitácora de la plataforma. |
| `ai_decisions` | Entrada y salida de cada decisión de la IA o de las reglas. |
| `platform_security_alerts` | Picos de accesos fallidos, bloqueos y 403. |
| `obs_requests` | Una fila por petición: ruta, estado, tiempos, `account_id`, `restaurant_id`. |
| `obs_events` | Avisos y errores de log, con campos redactados y traceback. |

### Restringida (10)

| Tabla | Datos personales o secretos | Cifrado en la columna |
|---|---|---|
| `customers` | Nombre, teléfono, correo, dirección, referencia, nota; consentimiento. | Todo menos el nombre; teléfono con índice ciego (`phone_key`). |
| `orders` | Nombre, teléfono, dirección y referencia del cliente de delivery; nota. | Todo menos `customer_name`. |
| `reservations` | Nombre, teléfono, nota. | Teléfono y nota; el nombre no. |
| `invoices` | Documento (DNI/RUC/CE), nombre o razón social, dirección del cliente; XML firmado, CDR y cuerpo enviado. | Documento, dirección, XML, CDR y cuerpo; el nombre no. |
| `credit_notes` | XML firmado, CDR y cuerpo de la nota (con los datos del cliente). | XML, CDR y cuerpo. |
| `billing_settings` | RUC, razón social y dirección fiscal (datos personales si el dueño es persona natural); usuario y clave SOL, certificado con su llave privada. | RUC, dirección, usuario y clave SOL y certificado; la razón social no (sale en cada comprobante). |
| `users` | Correo, nombre, hash de la contraseña y del PIN, aceptación de términos, motivo de baja. | Correo, con índice ciego (`email_key`) para el acceso, y motivo de baja; el nombre no. Contraseña y PIN son hashes Argon2id. |
| `platform_admins` | Correo, nombre, hash de la contraseña. | No: hash Argon2id. |
| `order_items` | Platos del pedido con su nota de cocina, que puede ser un dato de salud («alérgico al maní»). | La nota. |
| `two_factor_credentials` | Secreto TOTP, hashes de los códigos de recuperación. | El secreto, `EncryptedText` (entra en la rotación: `docs/criptografia.md`). |

Las columnas cifradas y la llave que las protege están en
[`docs/criptografia.md`](criptografia.md).

## Datos fuera de las tablas

| Dato | Nivel | Dónde vive |
|---|---|---|
| Contenido del JWT (`sub`, `scope`, `restaurant_id`, `sid`, `preview`, `platform_admin_id`, `iss`, `aud`, `iat`, `exp`) | Controlada | Firmado, **no cifrado** (`core/tokens.py`): cualquiera que tenga el token lo lee. Viaja en una cookie `HttpOnly` o en `Authorization: Bearer`. |
| Foto en base64 (`PUT /menu/items/{id}/photo`) | Pública | Solo en tránsito; se guarda vuelta a codificar como WebP, sin metadatos. |
| Certificado en base64 y su contraseña (`PUT /billing/settings/certificate`) | Restringida | En tránsito; el certificado se guarda cifrado como PEM y la contraseña se descarta (`billing/adapters/sunat/certificates.py`). |
| Respuesta de Factiliza (nombre por RUC/DNI) | Restringida | En memoria del proceso `LOOKUP_CACHE_SECONDS` (600 s). |
| Logs de stdout | Controlada | Logs del contenedor en el VPS; ver [`docs/eventos-de-seguridad.md`](eventos-de-seguridad.md). |
| Respaldos de la base | El de la tabla más sensible: restringida | Artefacto de GitHub cifrado con age (`.github/workflows/backup.yml`). |
| Sesión y cola sin conexión del navegador | Controlada / restringida | `localStorage` del frontend (repositorio `resthub-frontend`, brecha 6 de `docs/asvs-5.0.md`). |

## Protección por nivel

| Control | Pública | Interna | Controlada | Restringida |
|---|---|---|---|---|
| Aislamiento por local (`restaurant_id` del token en cada consulta) | Sí | Sí | Sí (las de plataforma no tienen local: solo token de plataforma) | Sí |
| Permiso para leer | `menu.read` | El de su módulo (`inventory.read`, `cash.manage`…) | `activity.read`, `staff.manage`; las de plataforma, token de plataforma con 2FA | `customers.read`, `billing.manage`, `billing.issue`; credenciales: nunca se devuelven |
| Cifrado en la columna | No | No | No | Sí en los campos de contacto, documentos y credenciales (ver arriba); hashes para contraseñas, PIN y códigos |
| Enmascarado | — | — | IP sin último bloque, correo enmascarado y tiras de 8 a 19 dígitos ocultas en logs (`core/logs.py::mask_email`, `core/redaction.py`) | Teléfono y correo enmascarados sin `customers.manage` (`customers/domain/masking.py`); verlos completos deja `customer_revealed` |
| Rastro de quién accede | — | Bitácora de cambios | Las bitácoras son de solo inserción (migraciones `0034` y `0044`, que también frena `TRUNCATE` en PostgreSQL) | `customer_viewed`, `customer_exported`, `customer_revealed` y alerta DLP (`data_access_alert`) |
| Reautenticación para cambiar | No | Roles y permisos | — | Datos fiscales, credenciales SOL y certificado |
| En logs y telemetría | Sí | Sí | Solo identificadores | Nunca en claro (`core/redaction.py`, `hide_parameters=True`, `SecretStr`) |
| Hacia la IA externa | Sí | Sí (cantidades) | Notas sin datos de contacto (`insights/domain/privacy.py`) | No |
| Respaldo | Cifrado con age, 30 días | Igual | Igual | Igual; las columnas cifradas viajan además como `enc1:…` |

## Conservación

### Plazos que el código ya aplica

| Dato | Plazo | Dónde |
|---|---|---|
| `obs_requests`, `obs_events` | 14 días (`OBSERVABILITY_RETENTION_DAYS`) | Purga cada hora (`core/telemetry.py`). |
| `access_history` | 90 días (`ACCESS_HISTORY_RETENTION_DAYS`) | Se purga al anotar un intento (`core/access_history.py`). |
| `account_sessions` | 120 días sin uso | `core/account_sessions.py` (`PURGE_AFTER`). |
| `login_attempts` | La ventana del límite, 15 minutos | Se borran al anotar un intento nuevo (`core/login_attempts.py`). |
| `preview_codes` | 24 horas | `modules/accounts/domain/preview.py` (`PREVIEW_CODE_RETENTION_SECONDS`). |
| Respaldos | 30 días | `retention-days: 30` en `.github/workflows/backup.yml`. |
| Caché de Factiliza | 10 minutos, en memoria | `LOOKUP_CACHE_SECONDS`. |

`tests/test_retention_periods.py` fija los de la telemetría, el historial de accesos y
los respaldos.

### Purga de datos personales

`wiring/data_retention.py` corre cada `DATA_RETENTION_INTERVAL_SECONDS` (una hora;
0 la apaga) mientras el API está arriba. Cada paso es idempotente y va por lotes de
500, así que una pasada atrasada no bloquea las tablas y dos réplicas a la vez no
se estorban. Lo que cambia queda en el log como `retention.purged`, solo con
cantidades. Pruebas: `tests/test_data_retention.py`.

| Dato | Plazo | Qué pasa | Motivo |
|---|---|---|---|
| Ficha del cliente (`customers`) | **24 meses** sin pedidos ni reservas (`CUSTOMER_RETENTION_DAYS`) | Se anonimiza con la misma regla que el derecho de cancelación (`AnonymizeCustomer`): la ficha queda con «Cliente eliminado» y sin contacto, y se limpian sus pedidos y reservas. Un pedido en curso la protege. | La finalidad es atender y reconocer al cliente frecuente; sin visitas en dos años, ya no se cumple. |
| Datos de contacto en pedidos (`orders.customer_name`, `customer_phone`, `delivery_address`, `delivery_reference`, `notes`; `order_items.notes`) | **90 días** desde que se pagó o se canceló (`ORDER_CONTACT_RETENTION_DAYS`) | Se vacían. El pedido, sus montos y platos se quedan para las ventas y los indicadores. | Alcanza para un reclamo por la entrega. |
| Datos personales de reservas (`reservations.customer_name`, `phone`, `notes`) | **90 días** desde la fecha reservada (`RESERVATION_CONTACT_RETENTION_DAYS`) | Se vacían; el nombre queda «Cliente eliminado» porque una reserva no se lee sin nombre. | Igual que el pedido. |
| Cuentas del personal desactivadas (`users`) | **12 meses** desde la baja (`STAFF_RETENTION_DAYS`) | Nombre «Cuenta dada de baja», correo `baja-<id>@cuentas.invalid`, sin PIN ni motivo de baja. | La bitácora conserva el id; el dato de contacto ya no hace falta. |
| `activity_log`, `platform_activity`, `ai_decisions` | **2 años** (`AUDIT_LOG_RETENTION_DAYS`, 731 días) | Se borran. La migración `0046` deja pasar en la base solo el `DELETE` de un asiento de más de 730 días: ni la aplicación puede borrar lo reciente, y `UPDATE` y `TRUNCATE` siguen prohibidos. El plazo no puede bajar de 731 (`core/config.py`). | Cubre auditorías y reclamos sin guardar para siempre quién hizo qué. |

El derecho de cancelación (`POST /customers/{id}/anonymize`,
`customers/use_cases/customer_rights.py`) sigue disponible para borrar antes de
plazo a pedido del cliente.

### Sin purga, a propósito

| Dato | Plazo | Motivo |
|---|---|---|
| Comprobantes y notas de crédito | **5 años** desde la emisión; confirmar con el contador. | Obligación tributaria de conservarlos; por eso ni el derecho de cancelación ni la purga los tocan. Borrarlos es irreversible y queda como decisión del contador. |
