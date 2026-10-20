# Reglas de validación

Catálogo de lo que el backend acepta en cada entidad: límites, formatos y reglas entre
campos (OWASP ASVS 5.0, V2.1.1, V2.1.2 y V5.1.1). La fuente es el código: las
constantes del dominio (`modules/*/domain/`) y los modelos `*Request`
(`modules/*/adapters/api/schemas.py`). Si este documento y el código no coinciden,
manda el código y se corrige el documento.

## Cómo se valida

- **Dos capas.** El modelo Pydantic de la petición rechaza el tipo y el largo con un
  422 de FastAPI (`detail` con la lista de errores y `errors`). El dominio repite la
  regla y agrega las de negocio; su error es un 4xx (400, 403, 404, 409 o 422) con
  `{detail, code}` (`core/errors.py`, `core/error_codes.py`).
- **Texto.** Los textos libres se normalizan (espacios repetidos a uno, sin espacios
  en los bordes) antes de medir el largo. Todo campo de texto tiene un largo máximo.
- **Dinero.** `Decimal`, nunca `float`; dos decimales como máximo, se rechaza un
  tercero en vez de redondearlo. Cantidades de inventario, tres decimales; costos
  unitarios, seis.
- **Identificadores y paginación.** Todo id de ruta o de cuerpo es entero de 1 a
  2 147 483 647 (`PathId`, `EntityId`, `core/api_params.py`); `limit` de 1 a 100 y
  `offset` hasta 1 000 000 (`core/pagination.py`).
- **Campos de más.** Pydantic los ignora; el `restaurant_id` siempre sale del token,
  nunca del cuerpo.
- **Fuzzing.** Schemathesis recorre el OpenAPI en el CI (`scripts/fuzz_api.sh`): un
  valor fuera de rango tiene que dar 4xx, no 500.

## Por entidad

### Cuentas, roles y acceso

`modules/accounts/domain/entities.py`, `roles.py`; `core/credentials.py`.

| Campo | Regla |
|---|---|
| Correo (login, alta) | `EmailStr`; se guarda en minúsculas y sin espacios. |
| Contraseña nueva | 10 a 128 caracteres y hasta 72 bytes en UTF-8; no puede estar en `core/common_passwords.txt` (sin distinguir mayúsculas). |
| Nombre completo | 1 a 120 caracteres. |
| PIN de desbloqueo | 4 a 6 dígitos ASCII. Se manda el PIN **o** la contraseña, no los dos (`UnlockRequest`). |
| Motivo de baja de una cuenta | 3 a 200 caracteres. |
| Nombre de rol | 1 a 40 caracteres, único en el local sin distinguir mayúsculas. |
| Código TOTP | 6 dígitos ASCII; un código ya usado se rechaza. |

### Restaurante y horario

`modules/restaurants/domain/entities.py`, `core/opening_hours.py`.

| Campo | Regla |
|---|---|
| Nombre | 1 a 120 caracteres. |
| Slug | `[a-z0-9]+(-[a-z0-9]+)*`, hasta 60; el prefijo del local de muestra está reservado. |
| Zona horaria | Una zona IANA válida (`core/local_time.py`). |
| Descuento máximo del mesero | 0 a 100 %, dos decimales. |
| Monto para aprobar mermas | 0 o más, dos decimales, hasta 99 999 999,99. |
| Bloqueo por inactividad | 0 (apagado) a 120 minutos. |
| Horario | Día 0 (lunes) a 6; horas `HH:MM`; hasta 21 tramos; apertura distinta del cierre. Un cierre anterior a la apertura cruza la medianoche. |

### Carta

`modules/menu/domain/entities.py`, `modifiers.py`, `photos.py`.

| Campo | Regla |
|---|---|
| Categoría | 1 a 60 caracteres. |
| Plato | Nombre 1 a 120; descripción hasta 300. |
| Precio | Mayor que 0, hasta 99 999 999,99, dos decimales. |
| Grupos de opciones | Hasta 8 por plato, con nombres distintos; cada uno de 1 a 20 opciones sin repetir; nombres de hasta 40; precio de opción de 0 a 9 999,99; mínimo y máximo de elecciones coherentes entre sí y con la cantidad de opciones. |
| Foto | Ver «Archivos». |

### Pedidos, cobro y caja

`modules/orders/domain/orders.py`, `cash.py`, `modifiers.py`, `tables.py`;
`modules/orders/use_cases/take_orders.py`.

| Campo | Regla |
|---|---|
| Mesa | Nombre 1 a 30 caracteres. |
| Tipo y mesa | `dine_in` exige mesa; `takeaway` y `delivery` no la llevan. Solo se unen o mueven pedidos en mesa. |
| Delivery | Exige nombre, teléfono y dirección (o un `customer_id` de la libreta que los tenga); referencia opcional. |
| Cliente del pedido | Nombre hasta 80; teléfono hasta 20 (**sin patrón**, brecha 20 de `docs/asvs-5.0.md`); dirección hasta 200; referencia hasta 150. |
| Plato pedido | `menu_item_id` de la carta del local y disponible; cantidad 1 a 99; nota hasta 200; hasta 40 opciones elegidas, cada una del grupo del plato, sin repetir y dentro del mínimo y máximo del grupo. |
| Precio | Lo pone el servidor: precio de la carta más las opciones, congelado al pedir. El cliente nunca manda precios. |
| Nota del pedido | Hasta 300. |
| `client_request_id` | 8 a 64 caracteres; único por local: un reintento devuelve el mismo pedido. |
| Total | `subtotal − cortesías − descuento`, con los platos anulados fuera; se recalcula siempre en el servidor. |
| Descuento | 0 a 100 %, dos decimales; con motivo (hasta 200) si no es cero; sin `orders.discount_any` no pasa del tope del local (403). |
| Cortesía, anulación de plato | Motivo obligatorio, 1 a 200, sin espacios en los bordes. |
| Cancelación | Motivo obligatorio, hasta 300. |
| Pago | Solo de un pedido servido. Por monto **o** por platos, no las dos cosas. Por monto: mayor que 0, no más que el saldo y con `expected_balance` igual al saldo actual (si cambió, 409). Por platos: platos del pedido aún sin pagar. |
| Propina | 0 o más, dos decimales. |
| Monto recibido | Solo en efectivo, y cubre lo cobrado más la propina. |
| Caja | Montos de 0 a 99 999 999,99 con dos decimales; notas hasta 300. |

Las reglas de dinero tienen además pruebas de propiedades
(`tests/test_properties_orders.py`).

### Clientes y reservas

`modules/customers/domain/customers.py`, `modules/reservations/domain/reservations.py`.

| Campo | Regla |
|---|---|
| Cliente: nombre | Obligatorio, hasta 80. |
| Cliente: teléfono | `\+?[\d ]{6,20}`; único en el local, comparado sin espacios. |
| Cliente: correo | Hasta 120, con `@`; se guarda en minúsculas. |
| Cliente: dirección, referencia, nota | Hasta 200, 150 y 300. |
| Cliente: consentimiento | El alta exige `consent: true` (Ley N.º 29733). |
| Reserva: nombre | Obligatorio, hasta 80. |
| Reserva: teléfono | Hasta 20 (sin patrón). |
| Reserva: personas | 1 a 50. |
| Reserva: duración | 30 a 360 minutos (120 por omisión). |
| Reserva: hora | Con zona horaria; no más de 15 minutos en el pasado. |
| Reserva: mesa | Dos reservas vigentes (`booked` o `seated`) de la misma mesa no se cruzan: `[inicio, inicio + duración)` no se solapa (`ensure_table_free`, con `FOR UPDATE` sobre la mesa). |
| Reserva: estado | Solo una reserva `booked` se edita o cambia de estado. |
| Cliente vinculado | El `customer_id` tiene que ser del local (si no, 404). |

### Inventario y compras

`modules/inventory/domain/entities.py`, `packaging.py`, `purchasing.py`,
`stock_counts.py`, `waste_approvals.py`.

| Campo | Regla |
|---|---|
| Insumo | Nombre 1 a 80; stock mínimo 0 o más; costo unitario positivo, hasta 99 999 999,999999 con seis decimales. |
| Cantidades | Hasta 999 999 999,999 con tres decimales. |
| Movimientos | Compra: suma y lleva costo. Consumo: resta y sale de un plato servido. Merma: resta y lleva motivo. Ajuste: distinto de cero y con motivo. Motivo hasta 200. |
| Receta | Cantidades positivas; un insumo no se repite. |
| Presentación | Tamaño mayor que 0; nombre hasta 30; para contar por peso, envase vacío mayor que 0 y lleno más pesado que vacío. |
| Proveedor | Nombre obligatorio hasta 80; contacto hasta 80; teléfono hasta 20; notas hasta 300. |
| Orden de compra | 1 a 60 insumos sin repetir; cantidades y costos positivos; lo recibido, 0 o más; solo se recibe o cancela en el estado que corresponde. |
| Conteo físico | 1 a 500 insumos sin repetir; lo contado, 0 o más; nota hasta 300. |
| Merma que pide aprobación | Por encima del umbral del local; el rechazo lleva motivo hasta 200. |

### Facturación electrónica

`modules/billing/domain/invoices.py`; `modules/lookup/domain/documents.py`.

| Campo | Regla |
|---|---|
| RUC del local | `(10|15|17|20)\d{9}`. |
| Tasa de IGV | 0 a 30 %, dos decimales. |
| Series | Boleta `B` + 3 letras o dígitos; factura `F` + 3. Las de notas de crédito no pueden ser iguales a las de sus comprobantes. |
| Usuario SOL | 3 a 20 letras o dígitos, sin el RUC delante; se manda junto con la clave o ninguno. |
| Clave SOL | Hasta 100. |
| Régimen | En Nuevo RUS solo se emiten boletas. |
| Factura | Exige RUC del cliente con el mismo patrón y razón social. |
| Boleta | Sin documento hasta S/ 700,00; por encima exige documento. DNI de 8 dígitos, CE de 8 a 12 letras o dígitos, RUC con el patrón de arriba, y el nombre. |
| Cliente del comprobante | Nombre hasta 100; dirección hasta 200; documento hasta 15. |
| Anulación | Motivo de 3 a 200; no se anula un comprobante pendiente en SUNAT ni uno con nota de crédito en curso. |
| Envío por correo | `EmailStr` hasta 254; 20 envíos por hora por cuenta (`EMAIL_MAX_PER_HOUR`). |
| Búsqueda RUC/DNI (Factiliza) | RUC de 11 dígitos con prefijo 10, 15, 16, 17 o 20 y dígito verificador; DNI de 8 dígitos. Se valida antes de llamar al proveedor. |

El prefijo `16` lo acepta la búsqueda y no el RUC de un comprobante: los dos patrones
no coinciden.

### Indicadores y observabilidad

| Campo | Regla |
|---|---|
| Rango de fechas de `insights` | Hasta 366 días (`modules/insights/domain/period.py`). |
| `window` del panel | `1h`, `6h`, `24h` o `7d`; `search` hasta 120 caracteres; `limit` hasta 200 (`modules/platform/domain/observability.py`). |

## Archivos

Solo se suben dos tipos de archivo, ambos en base64 dentro de un JSON. No se aceptan
archivos comprimidos ni se arman rutas del sistema de archivos con datos del usuario.

| | Foto de un plato | Certificado digital SUNAT |
|---|---|---|
| Ruta | `PUT /menu/items/{id}/photo` (`menu.manage`) | `PUT /billing/settings/certificate` (encargado con contraseña confirmada) |
| Tipos | JPEG, PNG o WebP | `.pfx`/`.p12` (con su contraseña) o `.pem` con la llave privada |
| Tamaño | 400 KB decodificada (`MAX_PHOTO_BYTES`); el base64 tiene su propio `max_length` | 64 KB decodificado (`MAX_CERTIFICATE_BYTES`); contraseña hasta 200 |
| Cómo se comprueba el tipo | Por los primeros bytes, no por el tipo declarado (`detect_image_type`, `menu/domain/photos.py`) | Se abre con `cryptography`: tiene que ser RSA, la llave tiene que corresponder al certificado y no puede estar vencido (`billing/adapters/sunat/certificates.py`, `billing/use_cases/invoicing.py`) |
| Base64 | Decodificado una vez con `validate=True`; inválido, 422 | Igual |
| Dónde se guarda | Tabla `menu_item_photos`, no en disco | `billing_settings.certificate_pem`, cifrado (`EncryptedText`); la contraseña se descarta |
| Cómo vuelve a salir | `GET /menu/items/{id}/photo` con permiso, `Content-Type` fijo del tipo detectado, `nosniff` y una CSP que no deja ejecutar nada (`core/security_headers.py`) | Nunca: solo se manda a Lycet para firmar |

Qué pasa con un archivo malicioso:

- **Disfrazado** (un ejecutable, HTML o SVG con extensión de imagen): no tiene la firma
  de JPEG, PNG ni WebP y se rechaza con 422 antes de guardarse.
- **Una imagen válida con contenido extra** (un políglota, metadatos EXIF): pasa,
  porque el servidor no vuelve a codificar la imagen ni la escanea (V5.4.3, brecha 20
  de `docs/asvs-5.0.md`). El riesgo queda acotado: se sirve siempre como imagen, con
  `nosniff` y sin poder ejecutarse, y el frontend reduce y vuelve a codificar la foto
  en un canvas antes de subirla. Los metadatos que traiga quedan guardados.
- **Un certificado manipulado**: si `cryptography` no lo abre, no es RSA o la llave no
  corresponde, 422 `invalid_certificate` sin guardar nada; los mensajes no llevan la
  contraseña ni el contenido.
- **Demasiado grande**: el `max_length` del base64 corta antes de decodificar. No hay
  todavía un tope global del cuerpo de la petición (brecha 8 de `docs/asvs-5.0.md`).
