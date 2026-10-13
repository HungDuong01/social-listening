-- ============================================================
-- BUOC 1 - chay trong pgAdmin, Query Tool cua database "postgres"
-- (server PostgreSQL 18, cong 5433)
--
-- Tao role ung dung + database. Chay 1 lan.
-- Neu bao "already exists" thi bo qua, nghia la da co san.
-- ============================================================

-- Role ung dung: KHONG phai superuser, chi dung cho app nay.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'social_crawler') THEN
        CREATE ROLE social_crawler LOGIN PASSWORD 'fwi8GwF1ZyKstkC6UvDCo38ONial';
    ELSE
        ALTER ROLE social_crawler PASSWORD 'fwi8GwF1ZyKstkC6UvDCo38ONial';
    END IF;
END
$$;

-- CREATE DATABASE khong chay duoc trong DO block / transaction,
-- nen de rieng o day. Bao loi "already exists" la binh thuong.
CREATE DATABASE social_crawler
    OWNER social_crawler
    ENCODING 'UTF8'
    TEMPLATE template0;
