-- Usuario de la aplicación y permisos del esquema en Supabase (docs/migracion-supabase.md).
--
-- Se corre una vez, como `postgres`, después de `alembic upgrade head` o de restaurar
-- el volcado (las tablas ya tienen que existir):
--
--   psql "$OWNER_URL" -v app_password="$APP_PASSWORD" \
--        -v backup_password="$BACKUP_PASSWORD" -f scripts/supabase_roles.sql
--
-- `backup_password` es opcional: sin ella no se crea el usuario de los respaldos.
--
-- Volver a correrlo no rompe nada: reaplica los mismos permisos.
--
-- 1. `anon` y `authenticated` (la API automática de Supabase) no ven nada del esquema.
-- 2. `resthub_app` lee y escribe datos, pero no es dueño de nada: no puede crear,
--    cambiar ni borrar tablas, ni quitar los triggers de las bitácoras. Las migraciones
--    siguen con `postgres` (`MIGRATION_DATABASE_URL`).
-- 3. `resthub_backup` solo lee: es el que usa el respaldo diario (`BACKUP_DATABASE_URL`).
--    Si su clave se filtra desde GitHub, no sirve para escribir ni para cambiar nada.

\set ON_ERROR_STOP on

begin;

-- La API automática (PostgREST) entra como `anon` o `authenticated`. RestHub no la usa.
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;
revoke usage on schema public from anon, authenticated;
-- Lo que creen las migraciones de ahora en adelante (como `postgres`) tampoco.
alter default privileges for role postgres in schema public
    revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
    revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
    revoke all on functions from anon, authenticated;

-- El usuario de la aplicación: sin superusuario, sin crear bases ni roles.
-- `\o /dev/null`: que la clave no salga en la pantalla ni en el registro de la terminal.
\o /dev/null
select set_config('resthub.app_password', :'app_password', true);
\o
do $$
begin
    if not exists (select from pg_roles where rolname = 'resthub_app') then
        execute format(
            'create role resthub_app login password %L nosuperuser nocreatedb nocreaterole',
            current_setting('resthub.app_password')
        );
    else
        execute format(
            'alter role resthub_app login password %L nosuperuser nocreatedb nocreaterole',
            current_setting('resthub.app_password')
        );
    end if;
end
$$;

-- Ninguna consulta de la aplicación debería pasar del plazo de una petición (12 s,
-- `REQUEST_DEADLINE_SECONDS`): la base corta antes de que una se quede colgada.
alter role resthub_app set statement_timeout = '15s';
alter role resthub_app set idle_in_transaction_session_timeout = '60s';

grant usage on schema public to resthub_app;
revoke create on schema public from resthub_app;
grant select, insert, update, delete on all tables in schema public to resthub_app;
grant usage, select on all sequences in schema public to resthub_app;
-- La versión de las migraciones la escriben solo ellas.
revoke insert, update, delete on alembic_version from resthub_app;

-- Las tablas que agreguen las migraciones futuras quedan con los mismos permisos.
alter default privileges for role postgres in schema public
    grant select, insert, update, delete on tables to resthub_app;
alter default privileges for role postgres in schema public
    grant usage, select on sequences to resthub_app;

\if :{?backup_password}
\o /dev/null
select set_config('resthub.backup_password', :'backup_password', true);
\o
do $$
begin
    if not exists (select from pg_roles where rolname = 'resthub_backup') then
        execute format(
            'create role resthub_backup login password %L nosuperuser nocreatedb nocreaterole',
            current_setting('resthub.backup_password')
        );
    else
        execute format(
            'alter role resthub_backup login password %L nosuperuser nocreatedb nocreaterole',
            current_setting('resthub.backup_password')
        );
    end if;
end
$$;
alter role resthub_backup set default_transaction_read_only = on;
grant usage on schema public to resthub_backup;
revoke create on schema public from resthub_backup;
grant select on all tables in schema public to resthub_backup;
grant select on all sequences in schema public to resthub_backup;
alter default privileges for role postgres in schema public
    grant select on tables to resthub_backup;
alter default privileges for role postgres in schema public
    grant select on sequences to resthub_backup;
\endif

commit;
