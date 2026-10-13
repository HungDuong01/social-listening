-- ============================================================================
-- social-crawler : PostgreSQL bootstrap (run once, as a superuser)
--
--   psql -U postgres -h 127.0.0.1 -p 5433 \
--        -v app_password=the-app-password \
--        -f scripts/bootstrap_postgres.sql
--
-- Pass the password RAW, with no surrounding quotes: ":'app_password'" below
-- already quotes it. Wrapping it here too stores a password that literally
-- contains apostrophes, and every later login fails for no visible reason.
--
-- Creates a least-privilege application role, the database, a dedicated
-- schema and the extensions the query layer depends on. Idempotent: running
-- it twice changes nothing.
--
-- The application NEVER connects as a superuser. It owns its own schema and
-- nothing else, so a bug in the crawler cannot reach the rest of the cluster.
-- ============================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- role -----
-- psql does NOT interpolate :'variables' inside a dollar-quoted $$...$$ body,
-- so the statement is generated outside one and executed with \gexec.
SELECT format('CREATE ROLE social_crawler LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'social_crawler')
\gexec

SELECT format('ALTER ROLE social_crawler PASSWORD %L', :'app_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'social_crawler')
\gexec

-- ------------------------------------------------------------ database -----
-- CREATE DATABASE cannot run inside a transaction block or a DO body, so it is
-- generated and executed conditionally instead.
SELECT format(
           'CREATE DATABASE social_crawler OWNER social_crawler '
           'ENCODING ''UTF8'' TEMPLATE template0'
       )
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'social_crawler')
\gexec

-- Connect to the new database for everything below.
\connect social_crawler

-- ---------------------------------------------------------- extensions -----
-- pg_trgm powers substring search on post text. Without it, the UI's
-- "content LIKE '%keyword%'" filter can only ever be a sequential scan.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- unaccent lets Vietnamese text match regardless of diacritics.
CREATE EXTENSION IF NOT EXISTS unaccent;

-- -------------------------------------------------------------- schema -----
CREATE SCHEMA IF NOT EXISTS crawler AUTHORIZATION social_crawler;

-- Keep application objects out of public: anything created there is visible
-- to every role in the cluster by default.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER DATABASE social_crawler SET search_path TO crawler, public;

GRANT CONNECT ON DATABASE social_crawler TO social_crawler;
GRANT USAGE, CREATE ON SCHEMA crawler TO social_crawler;

-- Objects the app creates later are owned by it already; this covers anything
-- created by a different role on its behalf.
ALTER DEFAULT PRIVILEGES IN SCHEMA crawler
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO social_crawler;
ALTER DEFAULT PRIVILEGES IN SCHEMA crawler
    GRANT USAGE, SELECT ON SEQUENCES TO social_crawler;

\echo ''
\echo '  database "social_crawler" and role "social_crawler" are ready.'
\echo '  Next: python scripts/setup_postgres.py   (creates tables + indexes)'
\echo ''
