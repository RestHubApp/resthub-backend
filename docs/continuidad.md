# Disponibilidad y continuidad del servicio

Qué promete RestHub cuando algo se cae, cómo se mide y qué se hace para volver. Las
dependencias externas, una por una, están en [`integraciones.md`](integraciones.md).

## Metas

| Indicador | Meta | Cómo se mide |
| --- | --- | --- |
| Disponibilidad del API | 99,5 % al mes en el horario de atención de los locales (unas 3,6 h de caída al mes como máximo) | Monitor externo sobre `GET /api/v1/health` cada 5 minutos (ver «Monitoreo») |
| RTO (tiempo hasta volver a atender) | 4 h o menos, de punta a punta (RNFS-12) | Simulacro de restauración con `scripts/restore_backup.sh`, que informa el tiempo |
| RPO (datos que se pueden perder) | 24 h como máximo con el respaldo diario; menos si el plan de Supabase tiene PITR | Hora del último respaldo correcto en *Actions → Respaldo de la base* |
| Pedidos sin señal | Ningún pedido nuevo del mesero se pierde por un corte de red o del API | Cola del celular con `client_request_id` (frontend, «Sin conexión y sesión») y experimento de caos 03 |

La meta de disponibilidad se cuenta en el horario de atención porque fuera de él nadie usa la
aplicación. Una ventana de mantenimiento anunciada no cuenta como caída.

## Cómo está armado y dónde puede fallar

| Pieza | Dónde corre | Si cae | Punto único de falla |
| --- | --- | --- | --- |
| Frontend (PWA) | Vercel, en su CDN | La aplicación ya instalada sigue abriendo: el service worker guarda el armazón y las pantallas del mesero | No: CDN de Vercel |
| API | Un VPS, con un contenedor y su `HEALTHCHECK` | No se cobra ni se cambian estados; los pedidos nuevos esperan en la cola del celular | **Sí**: un VPS y una réplica |
| Base de datos | Supabase (PostgreSQL administrado) | El API responde 503 en JSON; el sondeo de vida dice `database: unavailable` | Lo cubre Supabase según su plan |
| Dominio del API | Un nombre de `sslip.io` | Nadie llega al API aunque esté sano | **Sí**: un servicio gratuito de terceros |
| Respaldos | Artefactos de GitHub Actions, cifrados con age | No afecta la operación | No |

Para bajar los dos puntos únicos de falla:

1. **Dominio propio** para el API (por ejemplo `api.resthub.pe`), con Cloudflare en modo proxy.
   Además de quitar la dependencia de `sslip.io`, deja poner el WAF (ver «Cabeceras de
   seguridad y límite de peticiones» en el README).
2. **Sitio de contingencia**: un segundo VPS con la misma aplicación configurada pero apagada
   o sin tráfico. La imagen sale del mismo `Dockerfile` y la base vive fuera de los dos, así que
   cambiar de sitio es apuntar el dominio al otro VPS (ver «Si el VPS se pierde»).

## Lo que la aplicación ya hace cuando algo falla

- **Sondeo de vida** (`GET /api/v1/health`): responde aunque la base no conteste (espera a la
  base 2 s como mucho) y dice si la base está bien. Lo usan el `HEALTHCHECK` de la imagen y el
  proxy del VPS para no mandar tráfico a un contenedor que no arrancó.
- **Despliegue sin corte**: la imagen migra antes de escuchar; si la migración falla, el
  contenedor nuevo no pasa el sondeo y el anterior sigue atendiendo. En Dokploy hace falta
  `Order: start-first` y `FailureAction: rollback` (README, «Despliegue»).
- **Plazo por petición**: ninguna petición retiene al servidor más de lo debido; las que esperan
  a un tercero (Lycet, la IA) tienen su propio plazo.
- **Pedidos sin señal**: la cola del celular y el `client_request_id`, que evita duplicar un
  pedido que sí llegó.
- **Terceros que fallan**: cada integración tiene su modo degradado
  ([`integraciones.md`](integraciones.md)).

Esto está probado con experimentos de caos (`chaos/experiments/`): la base cae a mitad de
pedidos y cobros (01, 01b), la base con 1 a 3 s de latencia (02), el backend muerto con
`SIGKILL` mientras el mesero envía (03), respuestas 500, 409, 503 y plazos agotados en todas
las rutas (04 a 04d) y el canal de avisos congelado (05). Las pruebas de carga (`tests/load`:
nominal, pico, sostenida y estrés) fijan el comportamiento bajo uso normal y extremo.

## Respaldos

- **Diario y cifrado**: `.github/workflows/backup.yml` corre a las 03:00 de Lima, vuelca la base
  con el usuario de solo lectura `resthub_backup`, cifra con age y guarda el `.dump.age` 30 días
  como artefacto. Necesita los secretos `BACKUP_DATABASE_URL` y `BACKUP_AGE_RECIPIENT`.
- **Comprobarlo**: que la corrida tenga el paso «Respaldar» en verde y un artefacto. Si falta
  `BACKUP_DATABASE_URL`, el workflow sale en verde **sin respaldar** (lo avisa en el log):
  revisar el artefacto, no solo el color.
- **Supabase**: respaldos propios según el plan. El gratuito no da respaldos descargables ni
  PITR y pausa los proyectos sin uso; para producción conviene el plan Pro.
- **Llaves**: la privada de age y `DATA_ENCRYPTION_KEY` viven en el gestor de secretos del
  equipo, nunca en GitHub, el VPS ni Supabase. Sin `DATA_ENCRYPTION_KEY` los datos personales
  cifrados del respaldo no se pueden leer.
- **Comprobantes**: el XML, el CDR y el PDF de cada comprobante aceptado se copian a Google
  Drive (`integraciones.md`).

## Simulacro de restauración

Una vez por trimestre, y después de cambiar la versión mayor de PostgreSQL:

1. Bajar el último artefacto de *Actions → Respaldo de la base*.
2. Crear una base vacía de prueba (nunca la de producción).
3. `bash scripts/restore_backup.sh <archivo>.dump.age <llave-privada> <url-de-la-base-de-prueba>`.
4. Anotar el tiempo que informa el script y el conteo de filas por tabla.
5. Levantar el API contra esa base con la `DATA_ENCRYPTION_KEY` de producción y entrar con una
   cuenta: así se comprueba que los datos cifrados se leen.

| Fecha | Respaldo usado | Tiempo de restauración | Resultado | Responsable |
| --- | --- | --- | --- | --- |
| (pendiente) | | | | |

## Monitoreo

- **Externo** (pendiente de activar): un monitor gratuito (UptimeRobot o Better Stack) sobre
  `https://<dominio-del-api>/api/v1/health` cada 5 minutos, con palabra clave `"database":"ok"`
  para que una base caída también cuente como caída, y aviso por correo o Telegram. Su
  historial da el porcentaje de disponibilidad de la meta.
- **Interno**: el panel de observabilidad de la plataforma (tráfico, errores, latencias y
  alertas de seguridad) y, si se configura, las trazas de OpenTelemetry (README).

## Qué hacer si…

### El API no responde

1. Mirar el monitor y el sondeo: si `database` dice `unavailable`, ir a «La base no responde».
2. En el panel del VPS (Dokploy o Coolify): estado del contenedor y sus logs.
3. Si el último despliegue lo rompió, volver al anterior desde el panel (*Redeploy* del commit
   anterior) y abrir un `fix` aparte.
4. Mientras tanto, los meseros siguen tomando pedidos: quedan en la cola del celular.

### La base no responde

1. Ver el estado de Supabase (status.supabase.com) y el panel del proyecto: pausado, sin
   conexiones libres en el pooler o caído.
2. Si el proyecto se pausó, reanudarlo desde el panel.
3. Si la base se perdió: restaurar el último respaldo en un proyecto nuevo (ver «Simulacro»),
   volver a crear los usuarios con `scripts/supabase_roles.sql` y cambiar `DATABASE_URL` y
   `MIGRATION_DATABASE_URL` en el VPS.

### Si el VPS se pierde

1. Levantar el sitio de contingencia: la misma aplicación desde el repositorio, con las mismas
   variables (las de «Variables del servicio» del README, desde el gestor de secretos).
2. Si Supabase restringe la red por IP, agregar la IP del VPS nuevo.
3. Apuntar el dominio del API al VPS nuevo (con Cloudflare el cambio es inmediato) y, si el
   dominio cambia, `VITE_API_URL` en Vercel y `CORS_ALLOWED_ORIGINS` en el API.
4. Comprobar con el sondeo de vida y entrando con una cuenta.
