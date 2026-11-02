-- Checks after db_setup.sql. Run once as each role, using its .env connection string. Changes nothing:
-- the write attempts are rolled back. Other databases are only counted, never connected to.

\set VERBOSITY terse
SELECT current_user AS connected_as, current_database() AS database,
       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl,
       current_user = 'dealadvisor_owner' AS is_owner \gset
\echo role=:connected_as database=:database ssl=:ssl

\echo Objects visible in this database (outside the system catalogs):
SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1, 2;

\echo Other databases on the server this role has CONNECT on (count only, none opened):
SELECT count(*) AS other_databases,
       count(*) FILTER (WHERE has_database_privilege(oid, 'CONNECT')) AS connect_allowed
FROM pg_database WHERE datname <> current_database() AND NOT datistemplate;

\if :is_owner
    BEGIN;
    CREATE TABLE verify_probe (id int);
    \echo A new table created by the owner gives the app (expect t, t):
    SELECT has_table_privilege('dealadvisor_app', 'verify_probe', 'UPDATE') AS app_update,
           has_table_privilege('dealadvisor_app', 'verify_probe', 'DELETE') AS app_delete;
    ROLLBACK;
\else
    \echo Write attempts on audit_log (expect INSERT and SELECT allowed; the rest refused):
    BEGIN;
    DO $$
    DECLARE
        probe text;
    BEGIN
        FOREACH probe IN ARRAY ARRAY[
            $p$INSERT INTO audit_log (actor_name, actor_role, action) VALUES ('verify', 'verify', 'verify')$p$,
            'SELECT count(*) FROM audit_log',
            $p$UPDATE audit_log SET action = 'x'$p$,
            'DELETE FROM audit_log',
            'TRUNCATE audit_log',
            'DROP TABLE audit_log',
            'CREATE TABLE verify_probe (id int)'
        ] LOOP
            BEGIN
                EXECUTE probe;
                RAISE NOTICE 'ALLOWED: %', split_part(probe, ' ', 1) || ' ' || split_part(probe, ' ', 2);
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE NOTICE 'REFUSED: %', split_part(probe, ' ', 1) || ' ' || split_part(probe, ' ', 2);
            END;
        END LOOP;
    END $$;
    ROLLBACK;
\endif
