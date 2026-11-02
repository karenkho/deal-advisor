-- Deal Advisor database setup on the shared server <postgres-server> (POC v3, W0b).
-- Creates only: roles dealadvisor_owner and dealadvisor_app, database dealadvisor, and audit_log inside it.
-- Touches no other database, no firewall rule and no server setting. Passwords come from the environment.
--
-- Run once with the server admin login, from tor-poc (psql asks for the admin password):
--
--   export DEALADVISOR_OWNER_PASSWORD="$(openssl rand -hex 24)" DEALADVISOR_APP_PASSWORD="$(openssl rand -hex 24)"
--   H=<postgres-server>.postgres.database.azure.com
--   psql "host=$H port=5432 dbname=postgres user=<admin-login> sslmode=require" -f infra/db_setup.sql && {
--     echo "DATABASE_URL_OWNER=postgresql://dealadvisor_owner:$DEALADVISOR_OWNER_PASSWORD@$H:5432/dealadvisor?sslmode=require"
--     echo "DATABASE_URL=postgresql://dealadvisor_app:$DEALADVISOR_APP_PASSWORD@$H:5432/dealadvisor?sslmode=require"
--   } >> .env
--   unset DEALADVISOR_OWNER_PASSWORD DEALADVISOR_APP_PASSWORD
--
-- Undo (only these objects), with the admin login connected to dbname=postgres:
--   GRANT dealadvisor_owner TO CURRENT_USER;
--   DROP DATABASE IF EXISTS dealadvisor;
--   DROP ROLE IF EXISTS dealadvisor_app;
--   DROP ROLE IF EXISTS dealadvisor_owner;

\set ON_ERROR_STOP on
\getenv owner_password DEALADVISOR_OWNER_PASSWORD
\getenv app_password DEALADVISOR_APP_PASSWORD

-- The server accepts only md5 logins (azure.accepted_password_auth_method), so the roles use its default.
CREATE ROLE dealadvisor_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 5 PASSWORD :'owner_password';
CREATE ROLE dealadvisor_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20 PASSWORD :'app_password';

-- PG16: the admin must be able to SET ROLE to the new owner. Removed again at the end.
GRANT dealadvisor_owner TO CURRENT_USER;

CREATE DATABASE dealadvisor OWNER dealadvisor_owner;
REVOKE ALL ON DATABASE dealadvisor FROM PUBLIC;
GRANT CONNECT ON DATABASE dealadvisor TO dealadvisor_app;

\connect dealadvisor
-- On Azure, public belongs to azure_pg_admin rather than the database owner. This changes this database only.
ALTER SCHEMA public OWNER TO dealadvisor_owner;
SET ROLE dealadvisor_owner;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dealadvisor_app;

-- Every table the owner creates later (Alembic) is usable by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE dealadvisor_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dealadvisor_app;
ALTER DEFAULT PRIVILEGES FOR ROLE dealadvisor_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO dealadvisor_app;

-- Append-only for the app. The first Alembic migration adopts this table and must not recreate it:
-- a recreated table would pick up UPDATE and DELETE from the default privileges above.
CREATE TABLE audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_name  text NOT NULL,
    actor_role  text NOT NULL,
    action      text NOT NULL,
    entity_type text,
    entity_id   text,
    version     text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb
);
REVOKE ALL ON audit_log FROM dealadvisor_app;
GRANT SELECT, INSERT ON audit_log TO dealadvisor_app;

RESET ROLE;
REVOKE dealadvisor_owner FROM CURRENT_USER;
