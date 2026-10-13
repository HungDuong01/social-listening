-- ============================================================
-- BUOC 2 - DOI KET NOI sang database "social_crawler" roi moi chay
--
-- Trong pgAdmin: cay ben trai -> Databases -> social_crawler
-- -> chuot phai -> Query Tool.  (Rat quan trong: khong chay o
--    database "postgres", neu khong extension se vao nham cho.)
-- ============================================================

-- pg_trgm: giup loc "content LIKE '%tu khoa%'" dung duoc index,
-- neu khong moi lan tim kiem la mot lan quet toan bang.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent: cho phep khop tieng Viet khong dau.
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Schema rieng, khong dung public (moi role deu thay public).
CREATE SCHEMA IF NOT EXISTS crawler AUTHORIZATION social_crawler;

ALTER DATABASE social_crawler SET search_path TO crawler, public;

GRANT CONNECT ON DATABASE social_crawler TO social_crawler;
GRANT USAGE, CREATE ON SCHEMA crawler TO social_crawler;

ALTER DEFAULT PRIVILEGES IN SCHEMA crawler
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO social_crawler;
ALTER DEFAULT PRIVILEGES IN SCHEMA crawler
    GRANT USAGE, SELECT ON SEQUENCES TO social_crawler;

-- Kiem tra nhanh
SELECT current_database() AS db,
       current_schema()   AS schema,
       (SELECT count(*) FROM pg_extension
         WHERE extname IN ('pg_trgm','unaccent')) AS extensions_ok;
