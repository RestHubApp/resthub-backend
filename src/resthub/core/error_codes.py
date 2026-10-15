"""Catálogo único de códigos de error del API.

Toda respuesta de error lleva `{"detail": <texto en español>, "code": <ErrorCode>}`. El
`detail` es para personas y puede cambiar de redacción; el `code` es para programas: es
estable, está en inglés y en `snake_case`, y el frontend decide por él qué hacer (reintentar,
mostrar un campo, pedir iniciar sesión). Un valor no se renombra ni se reutiliza: se agrega uno
nuevo.

Puro a propósito (solo `enum`): lo importan las excepciones de dominio, que declaran su
`code`, y la capa HTTP, que lo escribe en la respuesta. Un mismo concepto en dos módulos
(`CustomerNotFoundError` en pedidos y en reservas) comparte código; lo vigila
`tests/test_architecture.py`, que además exige un código por cada excepción de dominio y que
todo valor se use.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Código estable de un error del API."""

    # -- Genéricos: los de la capa HTTP, sin excepción de dominio detrás -------------------

    # La petición no se puede atender tal como llegó (400 sin un código más específico).
    BAD_REQUEST = "bad_request"
    # Falta la credencial de acceso o ya no vale (401).
    UNAUTHENTICATED = "unauthenticated"
    # La cuenta no puede hacer eso (403 sin un código más específico).
    FORBIDDEN = "forbidden"
    # El recurso o la ruta no existe (404 sin un código más específico).
    NOT_FOUND = "not_found"
    # El método HTTP no está permitido en esa ruta (405).
    METHOD_NOT_ALLOWED = "method_not_allowed"
    # El estado actual no permite la operación (409 sin un código más específico).
    CONFLICT = "conflict"
    # Un campo del cuerpo, la ruta o la consulta no cumple el esquema (422 de FastAPI).
    VALIDATION_ERROR = "validation_error"
    # Una regla de negocio rechazó los datos (422 sin un código más específico).
    UNPROCESSABLE = "unprocessable"
    # Demasiadas peticiones desde esta dirección (429 del límite de tasa).
    RATE_LIMITED = "rate_limited"
    # Demasiados intentos fallidos de inicio de sesión (429 del freno de contraseñas).
    LOGIN_THROTTLED = "login_throttled"
    # La base de datos no responde (503).
    DATABASE_UNAVAILABLE = "database_unavailable"
    # La petición pasó el plazo máximo de atención (503).
    REQUEST_TIMEOUT = "request_timeout"
    # Una fecha y hora sin zona horaria donde la API la exige (422).
    TIMEZONE_REQUIRED = "timezone_required"
    # Cualquier otro error HTTP sin código propio.
    HTTP_ERROR = "http_error"

    # -- De dominio: uno por excepción `XError`, con el mismo nombre ------------------------

    # Ejemplo: «La cuenta ya no está disponible.»
    ADMIN_UNAVAILABLE = "admin_unavailable"

    # El saldo no es el que vio quien cobra: otro pago entró antes, o un doble toque.
    BALANCE_CHANGED = "balance_changed"

    # Ejemplo: «El rol … no cambia de nombre; solo sus permisos.»
    BASE_ROLE_NAME_FIXED = "base_role_name_fixed"

    # Ejemplo: «El rol … es de todo restaurante y no se elimina.»
    BASE_ROLE_NOT_DELETABLE = "base_role_not_deletable"

    # Ejemplo: «No puedes cambiar tu propio rol.»
    CANNOT_CHANGE_OWN_ROLE = "cannot_change_own_role"

    # Ejemplo: «No puedes desactivar tu propia cuenta.»
    CANNOT_DEACTIVATE_SELF = "cannot_deactivate_self"

    # Nadie reparte lo que no tiene: si no, `roles.manage` alcanzaría para todo.
    CANNOT_GRANT_PERMISSIONS = "cannot_grant_permissions"

    # Evita que alguien con `staff.manage` tome o degrade una cuenta con más poder.
    CANNOT_MANAGE_STRONGER_ACCOUNT = "cannot_manage_stronger_account"

    # Ejemplo: «No puedes cambiar un rol que tiene permisos que tú no tienes.»
    CANNOT_MANAGE_STRONGER_ROLE = "cannot_manage_stronger_role"

    # Ejemplo: «Para tu propia cuenta usa el cambio de contraseña, que pide la actual.»
    CANNOT_RESET_OWN_PASSWORD = "cannot_reset_own_password"  # noqa: S105  # nosec B105 - es un código de error, no una contraseña

    # Ejemplo: «Ya hay una caja abierta. Ciérrala antes de abrir otra.»
    CASH_REGISTER_ALREADY_OPEN = "cash_register_already_open"

    # Ejemplo: «La caja está cerrada. El encargado tiene que abrirla para cobrar.»
    CASH_REGISTER_CLOSED = "cash_register_closed"

    # La caja (turno de caja) no existe o es de otro restaurante.
    CASH_SESSION_NOT_FOUND = "cash_session_not_found"

    # Ejemplo: «Ya existe una categoría llamada ….»
    CATEGORY_NAME_TAKEN = "category_name_taken"

    # Ejemplo: «La categoría todavía tiene platos. Muévelos a otra o desactívala en vez de
    # borrarla.»
    CATEGORY_NOT_EMPTY = "category_not_empty"

    # También cuando la categoría existe pero es de otro restaurante.
    CATEGORY_NOT_FOUND = "category_not_found"

    # Ejemplo: «Para guardar al cliente en la libreta, primero tiene que aceptar el tratamiento de
    # sus datos personales.»
    CONSENT_REQUIRED = "consent_required"

    # Ejemplo: «El cliente tiene pedidos en curso. Bórralo cuando estén cobrados o cancelados.»
    CUSTOMER_HAS_ACTIVE_ORDERS = "customer_has_active_orders"

    # También cuando es de otro restaurante.
    CUSTOMER_NOT_FOUND = "customer_not_found"

    # Ejemplo: «Tu descuento máximo es … %. Uno mayor, o una cortesía, lo aplica el encargado.»
    DISCOUNT_NOT_ALLOWED = "discount_not_allowed"

    # El RUC o DNI consultado no figura en SUNAT o RENIEC.
    DOCUMENT_NOT_FOUND = "document_not_found"

    # Ejemplo: «No existe el plato ….»
    DISH_NOT_FOUND = "dish_not_found"

    # Ejemplo: «… no está disponible hoy.»
    DISH_UNAVAILABLE = "dish_unavailable"

    # Ejemplo: «Ya existe una cuenta con el correo ….»
    EMAIL_ALREADY_REGISTERED = "email_already_registered"

    # El proveedor de correo no entregó el mensaje (falló, rechazó la dirección o no respondió).
    EMAIL_DELIVERY = "email_delivery"

    # La cuenta pidió demasiados envíos de comprobantes por correo en poco tiempo.
    EMAIL_RATE_LIMITED = "email_rate_limited"

    # El servidor no tiene configurado el proveedor de correo (sin clave de Brevo).
    EMAIL_SENDING_DISABLED = "email_sending_disabled"

    # El respaldo de comprobantes no está configurado en el servidor (sin credenciales de Drive).
    ARCHIVE_DISABLED = "archive_disabled"

    # No se pudo subir un documento del comprobante al respaldo.
    DOCUMENT_ARCHIVE = "document_archive"

    # El servicio de facturación electrónica (Lycet) no está disponible (no responde o falla).
    BILLING_PROVIDER = "billing_provider"

    # El servicio de facturación rechazó el usuario SOL, la clave o el certificado del local.
    INVALID_BILLING_CREDENTIALS = "invalid_billing_credentials"

    # El certificado digital no abre: archivo dañado, contraseña errada o sin llave privada.
    INVALID_CERTIFICATE = "invalid_certificate"

    # La presentación del insumo (tamaño, pesos del envase) no cumple las reglas.
    INVALID_PACKAGING = "invalid_packaging"

    # Una cantidad en envases o por peso que no se puede pasar a la unidad del insumo.
    INVALID_QUANTITY_ENTRY = "invalid_quantity_entry"

    # Los datos del conteo físico no cumplen las reglas.
    INVALID_STOCK_COUNT = "invalid_stock_count"

    # La decisión sobre una merma por aprobar no cumple las reglas (falta el motivo).
    INVALID_WASTE_APPROVAL = "invalid_waste_approval"

    # El monto desde el que una merma pide aprobación está fuera de rango.
    INVALID_WASTE_APPROVAL_THRESHOLD = "invalid_waste_approval_threshold"

    # El comprobante todavía no tiene ese documento (XML, CDR o PDF) para descargar.
    INVOICE_DOCUMENT_UNAVAILABLE = "invoice_document_unavailable"

    # Ejemplo: «El restaurante de esta cuenta está desactivado.»
    INACTIVE_RESTAURANT = "inactive_restaurant"

    # Ejemplo: «El insumo … está desactivado.»
    INGREDIENT_INACTIVE = "ingredient_inactive"

    # Ejemplo: «Ya existe un insumo llamado ….»
    INGREDIENT_NAME_TAKEN = "ingredient_name_taken"

    # También cuando el insumo es de otro restaurante: no se delata que existe.
    INGREDIENT_NOT_FOUND = "ingredient_not_found"

    # Correo, nombre o contraseña de una cuenta nueva que no cumplen las reglas.
    INVALID_ACCOUNT_DATA = "invalid_account_data"

    # Los datos de facturación del local no cumplen las reglas.
    INVALID_BILLING_SETTINGS = "invalid_billing_settings"

    # Los datos de apertura o cierre de caja no cumplen las reglas.
    INVALID_CASH_SESSION = "invalid_cash_session"

    # No se dice si falló el correo o la contraseña: eso enumera cuentas.
    INVALID_CREDENTIALS = "invalid_credentials"

    # Los datos del cliente no cumplen las reglas (nombre, teléfono, correo).
    INVALID_CUSTOMER = "invalid_customer"

    # El tope de descuento del local está fuera de rango.
    INVALID_DISCOUNT_LIMIT = "invalid_discount_limit"

    # Ejemplo: «El correo electrónico no es válido: …»
    INVALID_EMAIL = "invalid_email"

    # El nombre de la cuenta no cumple las reglas.
    INVALID_FULL_NAME = "invalid_full_name"

    # Los datos del insumo no cumplen las reglas.
    INVALID_INGREDIENT = "invalid_ingredient"

    # Los datos del comprobante no cumplen las reglas.
    INVALID_INVOICE = "invalid_invoice"

    # El RUC (11 dígitos con dígito verificador) o el DNI (8 dígitos) no tiene un formato válido.
    INVALID_DOCUMENT_NUMBER = "invalid_document_number"

    # Los datos de la categoría de la carta no cumplen las reglas.
    INVALID_MENU_CATEGORY = "invalid_menu_category"

    # Los datos del plato no cumplen las reglas.
    INVALID_MENU_ITEM = "invalid_menu_item"

    # El movimiento de stock no cumple las reglas.
    INVALID_MOVEMENT = "invalid_movement"

    # Los datos del pedido no cumplen las reglas.
    INVALID_ORDER = "invalid_order"

    # Ejemplo: «El nuevo orden tiene que nombrar exactamente una vez a cada elemento de la lista.»
    INVALID_ORDERING = "invalid_ordering"

    # El rango de fechas del reporte no es válido o está fuera de la ventana permitida.
    INVALID_PERIOD = "invalid_period"

    # Inexistente, vencido o ya usado: la respuesta es la misma para no dar pistas.
    INVALID_PREVIEW_CODE = "invalid_preview_code"

    # Los datos de la orden de compra no cumplen las reglas.
    INVALID_PURCHASE_ORDER = "invalid_purchase_order"

    # La receta del plato no cumple las reglas.
    INVALID_RECIPE = "invalid_recipe"

    # Los datos de la reserva no cumplen las reglas.
    INVALID_RESERVATION = "invalid_reservation"

    # Nombre, identificador corto o zona horaria que no cumplen las reglas.
    INVALID_RESTAURANT_DATA = "invalid_restaurant_data"

    # El nombre del restaurante no cumple las reglas.
    INVALID_RESTAURANT_NAME = "invalid_restaurant_name"

    # El nombre del rol no cumple las reglas.
    INVALID_ROLE_NAME = "invalid_role_name"

    # Ejemplo: «El identificador … no es válido. Usa minúsculas, números y guiones, sin guiones al
    # principio ni al final.»
    INVALID_SLUG = "invalid_slug"

    # Los datos del proveedor no cumplen las reglas.
    INVALID_SUPPLIER = "invalid_supplier"

    # Los datos de la mesa no cumplen las reglas.
    INVALID_TABLE = "invalid_table"

    # Ejemplo: «El nuevo orden tiene que nombrar exactamente una vez a cada mesa del local.»
    INVALID_TABLE_ORDERING = "invalid_table_ordering"

    # Ejemplo: «La zona horaria … no existe. Ejemplo: America/Lima.»
    INVALID_TIMEZONE = "invalid_timezone"

    # El token de acceso no es válido o ya expiró.
    INVALID_TOKEN = "invalid_token"  # noqa: S105  # nosec B105 - es un código de error, no una contraseña

    # El pedido no está en un estado desde el que se pueda hacer eso.
    INVALID_TRANSITION = "invalid_transition"

    # También cuando es de otro restaurante.
    INVOICE_NOT_FOUND = "invoice_not_found"

    # Ejemplo: «Otro comprobante tomó el mismo número a la vez. Vuelve a intentarlo.»
    INVOICE_NUMBER_TAKEN = "invoice_number_taken"

    # Ejemplo: «Lomo saltado ya está anulado.»
    ITEM_ALREADY_VOIDED = "item_already_voided"

    # Ejemplo: «No existe la entrada de log ….»
    LOG_ENTRY_NOT_FOUND = "log_entry_not_found"

    # La consulta de RUC/DNI está apagada: el servidor no tiene el token del proveedor.
    LOOKUP_DISABLED = "lookup_disabled"

    # Esta cuenta consultó demasiadas veces en el último minuto.
    LOOKUP_RATE_LIMITED = "lookup_rate_limited"

    # El proveedor de la consulta no respondió, respondió tarde o rechazó la credencial.
    LOOKUP_UNAVAILABLE = "lookup_unavailable"

    # Ejemplo: «Ya existe un plato llamado ….»
    MENU_ITEM_NAME_TAKEN = "menu_item_name_taken"

    # También cuando el plato es de otro restaurante, o inactivo para el mesero.
    MENU_ITEM_NOT_FOUND = "menu_item_not_found"

    # Ejemplo: «Tu rol no tiene permiso para esta acción.»
    MISSING_PERMISSION = "missing_permission"

    # La vista previa solo entra a cuentas activas del local de muestra.
    NOT_A_SANDBOX_ACCOUNT = "not_a_sandbox_account"

    # El pedido se ve, pero la acción es de quien lo tomó o del encargado.
    NOT_YOUR_ORDER = "not_your_order"

    # Ejemplo: «El pedido ya tiene el comprobante ….»
    ORDER_ALREADY_INVOICED = "order_already_invoiced"

    # Descuentos, cortesías y cancelaciones van antes del primer pago.
    ORDER_HAS_PAYMENTS = "order_has_payments"

    # Ejemplo: «El pedido no tiene el ítem ….»
    ORDER_ITEM_NOT_FOUND = "order_item_not_found"

    # También cuando el pedido es de otro restaurante, o de otro mesero y ya cerró.
    ORDER_NOT_FOUND = "order_not_found"

    # Ejemplo: «Otro pedido tomó el mismo número a la vez. Vuelve a intentarlo.»
    ORDER_NUMBER_TAKEN = "order_number_taken"

    # Ejemplo: «Los términos cambiaron desde que los abriste. Vuelve a leerlos y acéptalos de
    # nuevo.»
    OUTDATED_TERMS = "outdated_terms"

    # El pedido no existe, es de otro local o todavía no está pagado.
    PAID_ORDER_NOT_FOUND = "paid_order_not_found"

    # Ejemplo: «El teléfono … ya es de ….»
    PHONE_TAKEN = "phone_taken"

    # Ejemplo: «En la vista previa no se cambia la contraseña.»
    PREVIEW_SESSION_RESTRICTED = "preview_session_restricted"

    # También cuando la orden es de otro restaurante.
    PURCHASE_ORDER_NOT_FOUND = "purchase_order_not_found"

    # Ejemplo: «Esa mesa ya está reservada para … en ese horario.»
    RESERVATION_CONFLICT = "reservation_conflict"

    # También cuando es de otro restaurante.
    RESERVATION_NOT_FOUND = "reservation_not_found"

    # Los identificadores que empiezan con `muestra` son del local de muestra.
    RESERVED_SLUG = "reserved_slug"

    # Ejemplo: «El restaurante … ya tiene personal registrado.»
    RESTAURANT_ALREADY_HAS_STAFF = "restaurant_already_has_staff"

    # Ejemplo: «No existe el restaurante ….»
    RESTAURANT_NOT_FOUND = "restaurant_not_found"

    # Ejemplo: «El rol … tiene personal asignado; cámbialo de rol antes.»
    ROLE_IN_USE = "role_in_use"

    # Ejemplo: «Ya existe un rol llamado ….»
    ROLE_NAME_TAKEN = "role_name_taken"

    # Ejemplo: «El rol de encargado no se edita: siempre tiene todos los permisos.»
    ROLE_NOT_EDITABLE = "role_not_editable"

    # También cuando el rol existe pero es de otro restaurante.
    ROLE_NOT_FOUND = "role_not_found"

    # El local de muestra no tiene una cuenta activa de ese tipo: la cambiaron desde adentro.
    SANDBOX_ACCOUNT_UNAVAILABLE = "sandbox_account_unavailable"

    # Otro pedido creó el local de muestra a la vez: ya hay uno vigente.
    SANDBOX_ALREADY_ACTIVE = "sandbox_already_active"

    # Ejemplo: «Ya existe un restaurante con el identificador ….»
    SLUG_ALREADY_TAKEN = "slug_already_taken"

    # Hay otro conteo físico en curso en el local: se cierra o cancela antes de abrir otro.
    STOCK_COUNT_ALREADY_OPEN = "stock_count_already_open"

    # El reporte de diferencias es de un conteo cerrado.
    STOCK_COUNT_NOT_CLOSED = "stock_count_not_closed"

    # También cuando el conteo es de otro restaurante.
    STOCK_COUNT_NOT_FOUND = "stock_count_not_found"

    # El conteo ya se cerró o se canceló: no se editan sus líneas.
    STOCK_COUNT_NOT_OPEN = "stock_count_not_open"

    # Ejemplo: «Ya existe un proveedor llamado ….»
    SUPPLIER_NAME_TAKEN = "supplier_name_taken"

    # También cuando el proveedor es de otro restaurante.
    SUPPLIER_NOT_FOUND = "supplier_not_found"

    # Ejemplo: «La mesa … está desactivada.»
    TABLE_INACTIVE = "table_inactive"

    # Ejemplo: «Ya existe una mesa llamada ….»
    TABLE_LABEL_TAKEN = "table_label_taken"

    # También cuando la mesa es de otro restaurante: no se delata que existe.
    TABLE_NOT_FOUND = "table_not_found"

    # Ejemplo: «La mesa … ya tiene un pedido activo.»
    TABLE_OCCUPIED = "table_occupied"

    # También cuando la cuenta existe pero es de otro restaurante.
    USER_NOT_FOUND = "user_not_found"

    # La merma ya se aprobó o se rechazó.
    WASTE_ALREADY_DECIDED = "waste_already_decided"

    # También cuando la merma es de otro restaurante.
    WASTE_APPROVAL_NOT_FOUND = "waste_approval_not_found"

    # La contraseña no cumple los requisitos mínimos.
    WEAK_PASSWORD = "weak_password"  # noqa: S105  # nosec B105 - es un código de error, no una contraseña

    # Ejemplo: «La contraseña actual no es correcta.»
    WRONG_CURRENT_PASSWORD = "wrong_current_password"  # noqa: S105  # nosec B105 - es un código de error, no una contraseña
