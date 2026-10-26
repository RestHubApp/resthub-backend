-- Comprueba que la base de Supabase quedó como pide docs/migracion-supabase.md.
--
--   psql "$OWNER_URL" -f scripts/supabase_check.sql
--
-- Cada fila dice `ok` o `FALLA`. Ninguna consulta modifica la base.

\pset footer off

select 'la conexión usa TLS' as revisión,
       case when ssl then 'ok' else 'FALLA' end as estado
from pg_stat_ssl where pid = pg_backend_pid()

union all

select 'la API automática (anon, authenticated) no tiene permisos en public',
       case when count(*) = 0 then 'ok' else 'FALLA: ' || count(*) || ' permisos' end
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon', 'authenticated')

union all

select 'existe el usuario resthub_app y no es superusuario',
       case when bool_and(not rolsuper and not rolcreaterole and not rolcreatedb) then 'ok'
            else 'FALLA' end
from pg_roles where rolname = 'resthub_app'

union all

select 'resthub_app no es dueño de ninguna tabla',
       case when count(*) = 0 then 'ok' else 'FALLA: ' || count(*) || ' tablas' end
from pg_tables where schemaname = 'public' and tableowner = 'resthub_app'

union all

select 'resthub_app no puede crear objetos en public',
       case when not has_schema_privilege('resthub_app', 'public', 'CREATE') then 'ok'
            else 'FALLA' end

union all

select 'resthub_app puede leer y escribir cada tabla de la aplicación',
       case when count(*) = 0 then 'ok'
            else 'FALLA: ' || string_agg(c.relname, ', ') end
from pg_class c
where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
  and c.relname <> 'alembic_version'
  and not (has_table_privilege('resthub_app', c.oid, 'SELECT')
       and has_table_privilege('resthub_app', c.oid, 'INSERT')
       and has_table_privilege('resthub_app', c.oid, 'UPDATE')
       and has_table_privilege('resthub_app', c.oid, 'DELETE'))

union all

select 'las bitácoras tienen sus 9 triggers de solo inserción',
       case when count(*) = 9 then 'ok' else 'FALLA: ' || count(*) || ' de 9' end
from pg_trigger t join pg_class c on c.oid = t.tgrelid
where not t.tgisinternal
  and c.relname in ('activity_log', 'platform_activity', 'ai_decisions')
  and t.tgname ~ '_(no_update|no_recent_delete|no_truncate)$'

union all

select 'resthub_backup (si existe) solo puede leer',
       case when not exists (select from pg_roles where rolname = 'resthub_backup') then 'ok: no existe'
            when exists (
                select from pg_class c
                where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
                  and (has_table_privilege('resthub_backup', c.oid, 'INSERT')
                    or has_table_privilege('resthub_backup', c.oid, 'UPDATE')
                    or has_table_privilege('resthub_backup', c.oid, 'DELETE')
                    or not has_table_privilege('resthub_backup', c.oid, 'SELECT'))
            ) then 'FALLA'
            else 'ok' end

union all

select 'versión de las migraciones: ' || version_num, 'ok'
from alembic_version;
