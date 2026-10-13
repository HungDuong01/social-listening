-- Runs exactly once: automatically, the first time the "db" container
-- starts against an empty data volume (standard behavior of the official
-- postgres image for anything under /docker-entrypoint-initdb.d/).
--
-- docker-compose.yml's POSTGRES_USER / POSTGRES_DB already create the
-- social_crawler role and database and make that role its owner. This
-- script adds what the application's own schema needs on top of that.

CREATE SCHEMA IF NOT EXISTS crawler AUTHORIZATION social_crawler;

-- Every session for this database defaults into the crawler schema without
-- the application ever naming it - SQLAlchemy's table definitions carry no
-- explicit schema= of their own.
ALTER DATABASE social_crawler SET search_path TO crawler, public;

-- pg_trgm: the UI's "content LIKE '%keyword%'" post filter needs a trigram
-- index to avoid a full table scan on every search (see
-- scripts/setup_postgres.py, which creates that index on top of this).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent: lets Vietnamese text match regardless of diacritics.
CREATE EXTENSION IF NOT EXISTS unaccent;
