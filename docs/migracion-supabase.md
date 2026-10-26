# Pasar la base de datos a Supabase

Guía para mover el PostgreSQL de RestHub del VPS a Supabase y dejarlo endurecido. El
backend sigue en el VPS (Dokploy) y el frontend en Vercel; solo cambia dónde vive la
base. El código no cambia: todo es configuración.

Al terminar quedan cerradas dos brechas de `docs/asvs-5.0.md`:

- la del rol de base de datos (V13.2.2): la aplicación entra con un usuario que no
  puede cambiar tablas ni quitar los triggers de las bitácoras;
- la del TLS sin exigir hacia PostgreSQL (V12.3.1, V12.3.2).

## Qué cambia y qué no

| | Antes (todo en el VPS) | Después |
|---|---|---|
| Dónde están los datos | En el disco del VPS, junto a la aplicación | Supabase, en la región de Estados Unidos más cercana al VPS |
| Quién mantiene PostgreSQL | Tú (versiones, parches, disco) | Supabase |
| Usuario de la aplicación | El dueño de las tablas | `resthub_app`: solo lee y escribe datos |
| Usuario de las migraciones | El mismo | `postgres`, por `MIGRATION_DATABASE_URL` |
| Usuario del respaldo diario | El mismo | `resthub_backup`: solo lee |
| TLS hacia la base | `prefer` (degradable) | `verify-full` con el certificado de Supabase |
| Respaldos | El diario cifrado con age (`.github/workflows/backup.yml`) | El mismo, más los de Supabase (y PITR si se contrata) |
| `DATA_ENCRYPTION_KEY` | En el VPS | **Sigue solo en el VPS.** Supabase ve `enc1:…`, nunca el dato. |

Los datos quedan en Estados Unidos, como ya lo estaban en el VPS: es un flujo
transfronterizo (Ley N.º 29733). Hay que declararlo en la política de privacidad
y ante la Autoridad Nacional de Protección de Datos Personales.

## Lo que hace falta

- Acceso al panel de Supabase y al de Dokploy del VPS.
- `psql`, `pg_dump` y `pg_restore` de una versión **igual o mayor** que la del
  PostgreSQL del VPS y que la de Supabase (el panel la muestra en *Database → Settings*).
  `pg_dump` se niega a volcar un servidor más nuevo que él.
- Un gestor de contraseñas para las tres claves nuevas: la de `postgres`, la de
  `resthub_app` y la de `resthub_backup`. Que sean largas y al azar, por ejemplo
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Una ventana sin pedidos (de madrugada): mientras se copian los datos la aplicación
  está detenida. Avisa antes a los locales.

## 1. Crear el proyecto

1. *New project*, en la región de Estados Unidos más cercana al VPS: `us-east-1` (Virginia)
   o `us-east-2` (Ohio) si está en el este, `us-west-1` (California) o `us-west-2`
   (Oregón) si está en el oeste. Cada petición hace varias consultas, así que la distancia
   entre el VPS y la base se nota en cada una: São Paulo, aunque esté más cerca del Perú,
   pondría unos 100 ms o más por consulta. Mídelo antes con `psql -c 'select 1'` y
   `\timing`, desde el VPS.
2. La clave de `postgres` va al gestor de contraseñas. Solo la usan las migraciones y
   esta guía; la aplicación no.

## 2. Cerrar la API automática

Supabase publica cada tabla de `public` en una API REST (PostgREST) con una llave
pública, `anon`. RestHub no la usa: todo pasa por su backend. Sin reglas por fila (RLS),
cualquiera con esa llave podría leer o escribir las tablas. Es el riesgo más grande de
la mudanza, así que se cierra dos veces:

1. En el panel, apaga el **Data API** del proyecto (en su configuración aparece como
   *Enable Data API*). Si tu plan no deja apagarlo, que la lista *Exposed schemas* quede
   vacía.
2. `scripts/supabase_roles.sql` (paso 5) además les quita a `anon` y `authenticated`
   todo permiso sobre `public`, también sobre las tablas que agreguen las migraciones
   futuras. Así, aunque alguien vuelva a encender la API, no ve nada.

No hace falta activar RLS: con esos permisos quitados no hay nada que filtrar.

## 3. TLS y red

1. *Database → Settings → SSL Configuration*: activa **Enforce SSL on incoming
   connections** y descarga el certificado (`prod-ca-2021.crt` o el nombre que muestre).
2. Copia el certificado al VPS, fuera de la imagen, y móntalo en el contenedor del API
   (en Dokploy, *Advanced → Volumes*), por ejemplo en `/certs/supabase-ca.crt`.
3. **Restricciones de red** (*Database → Settings → Network Restrictions*): lo más
   seguro es permitir solo la IP del VPS. Con eso, el respaldo diario de GitHub Actions
   no puede conectarse (sus IP cambian en cada corrida). Elige una:
   - **Restringir y respaldar desde el VPS.** Un cron en el VPS corre los mismos pasos que
     `.github/workflows/backup.yml` (`scripts/backup_db.py`, cifrar con age y copiar el
     `.age` fuera del VPS). Es lo más cerrado.
   - **No restringir por IP.** La base queda alcanzable desde internet, pero solo con
     TLS y con las claves largas del paso anterior, y el usuario que guarda GitHub
     (`resthub_backup`) solo puede leer. Es lo que permite seguir con el respaldo de
     GitHub sin cambios.

## 4. Copiar los datos

Con la aplicación **detenida** en Dokploy, para que nadie escriba durante la copia:

```bash
# Desde el VPS (o una máquina que alcance las dos bases).
# Las URL usan `postgresql://`, sin `+asyncpg`.
OLD_URL='postgresql://usuario:clave@localhost:5432/resthub'
# La conexión directa o el pooler en modo sesión (puerto 5432), como `postgres`.
# El botón «Connect» del panel muestra las dos; en el pooler el usuario es `postgres.<ref>`.
OWNER_URL='postgresql://postgres.<ref>:<clave>@<host-del-pooler>:5432/postgres?sslmode=verify-full&sslrootcert=/ruta/supabase-ca.crt'

# 1. Volcado del esquema `public` (tablas, triggers, la función de las bitácoras y datos).
pg_dump --format=custom --schema=public --no-owner --no-privileges \
  --file=resthub-a-supabase.dump "$OLD_URL"

# 2. Restaurarlo en Supabase como `postgres`.
pg_restore --no-owner --no-privileges --dbname="$OWNER_URL" resthub-a-supabase.dump
```

Si `pg_restore` avisa de que el esquema `public` ya existe, se puede ignorar; cualquier
otro error, no: revisa la salida antes de seguir. Comprueba la versión de las
migraciones, que tiene que ser la misma que en el VPS:

```bash
psql "$OWNER_URL" -c 'select version_num from alembic_version'
```

Borra `resthub-a-supabase.dump` al terminar (`shred --remove`): tiene los datos. Los
personales van cifrados, pero los nombres y los montos no.

## 5. Usuarios y permisos

```bash
psql "$OWNER_URL" -v app_password="$APP_PASSWORD" \
  -v backup_password="$BACKUP_PASSWORD" -f scripts/supabase_roles.sql
```

`scripts/supabase_roles.sql`:

- les quita todo a `anon` y `authenticated` (paso 2);
- crea `resthub_app`: puede `SELECT`, `INSERT`, `UPDATE` y `DELETE`, pero no es dueño de
  nada y no puede crear tablas. No puede quitar los triggers de las bitácoras, ni
  `TRUNCATE`, ni tocar `alembic_version`. Sus consultas se cortan a los 15 s;
- crea `resthub_backup`, de solo lectura, para el respaldo diario;
- deja los mismos permisos para las tablas que creen las migraciones futuras.

Se puede volver a correr sin problema, por ejemplo para cambiar una clave.

Después, la revisión (cada fila debe decir `ok`):

```bash
psql "$OWNER_URL" -f scripts/supabase_check.sql
```

Revisa que haya TLS, que la API automática no tenga permisos, que `resthub_app` no sea
dueño ni pueda crear nada, que lea y escriba todas las tablas, que las bitácoras tengan
sus nueve triggers, que `resthub_backup` solo lea y cuál es la versión de las
migraciones.

## 6. Variables del API en Dokploy

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `postgresql://resthub_app.<ref>:<clave>@<host-del-pooler>:5432/postgres`: el pooler en **modo sesión** (puerto 5432). La aplicación ya tiene su propio pool, así que el modo transacción (6543) no hace falta. |
| `MIGRATION_DATABASE_URL` | La de `postgres` (`OWNER_URL` sin los parámetros `sslmode`). Solo la usa `alembic upgrade head`, que el `CMD` de la imagen corre antes de arrancar. |
| `DATABASE_SSL_MODE` | `verify-full` |
| `DATABASE_SSL_ROOT_CERT` | `/certs/supabase-ca.crt`, la ruta donde montaste el certificado. |

Si la aplicación no conecta con `verify-full` por el pooler (el certificado no calza con
el nombre del host), prueba primero con `psql` y `sslmode=verify-full`. Si así tampoco,
usa `verify-ca` mientras lo resuelves: sigue validando que el certificado lo firmó
Supabase, solo no compara el nombre. No uses `require` ni `prefer`.

El pool de la aplicación abre hasta 15 conexiones (`pool_size=10`, `max_overflow=5` en
`core/database.py`). Revisa que el tamaño del pooler del plan (*Database → Settings →
Connection pooling*) alcance para eso más las migraciones y el respaldo.

Arranca la aplicación. En el log de arranque no debe aparecer
`database.tls_not_enforced`, y `GET /api/v1/health` debe responder `database: ok`.
Entra con una cuenta y abre un pedido para comprobar que escribe.

## 7. Respaldo diario

- Cambia el secreto `BACKUP_DATABASE_URL` del repositorio por la URL de
  `resthub_backup`, con `?sslmode=verify-full` si pasas el certificado al runner, o
  `?sslmode=require` si no.
- La versión de `pg_dump` del runner tiene que alcanzar la de Supabase: si Supabase
  corre PostgreSQL 17, el paso «Instalar el cliente de PostgreSQL» del workflow tiene
  que instalar `postgresql-client-17` del repositorio de PGDG, no el de Ubuntu.
- Lánzalo a mano una vez (*Actions → Respaldo de la base → Run workflow*) y comprueba
  que suba el `.age`.
- Si elegiste restringir por IP (paso 3), apaga este workflow y deja el cron del VPS.

El respaldo con age sigue valiendo la pena aunque Supabase tenga los suyos: es una copia
fuera de su plataforma y cifrada con una llave que Supabase no tiene.

## 8. Cerrar el PostgreSQL viejo

Cuando la aplicación lleve unos días sin problemas en Supabase:

1. Saca un último respaldo del PostgreSQL del VPS, cífralo con age y guárdalo fuera.
2. Detén el servicio de PostgreSQL del VPS y borra su volumen.
3. Cierra en el firewall del VPS el puerto 5432, si estaba abierto.

## Volver atrás

Si algo falla antes del paso 8, la base vieja sigue intacta: detén la aplicación,
vuelve a poner la `DATABASE_URL` anterior y quita `MIGRATION_DATABASE_URL`. Lo que se
haya escrito en Supabase mientras tanto no está en la base vieja: haz la vuelta cuanto
antes, o vuelca Supabase y restáuralo en el VPS con los mismos comandos del paso 4.

## Probado

Los dos scripts SQL, la restauración y los permisos se probaron contra un PostgreSQL 16
local con los roles `anon` y `authenticated` creados como en Supabase:

- la restauración del volcado del respaldo termina sin errores y queda en la última
  migración;
- con `resthub_app`, la aplicación crea un restaurante, encuentra la cuenta por correo
  y corre la purga;
- `resthub_app` no puede quitar un trigger, alterar una tabla, `TRUNCATE`, crear tablas,
  tocar `alembic_version` ni borrar un asiento reciente de la bitácora; sí borra uno de
  más de dos años;
- `resthub_backup` vuelca la base con `scripts/backup_db.py` y no puede borrar nada.

No se probaron contra un proyecto real de Supabase: los nombres exactos de los menús
del panel pueden cambiar.
