-- Migration: rename section_id → crn in sections and monitored_sections
-- Run against your Neon database BEFORE deploying the updated class_finder.py.
-- After running, regenerate schema.sql:
--   pg_dump --schema-only <connection_string> > schema.sql
--
-- PostgreSQL automatically retargets PK and FK constraints to the renamed
-- column — no manual DROP/ADD CONSTRAINT needed.

BEGIN;

ALTER TABLE public.sections
    RENAME COLUMN section_id TO crn;

ALTER TABLE public.monitored_sections
    RENAME COLUMN section_id TO crn;

COMMIT;
