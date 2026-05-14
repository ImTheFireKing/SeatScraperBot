-- Migration v2: per-user tracking + auto-managed course config
-- Run against Neon DB BEFORE deploying the updated class_finder.py.
-- Assumes migration_crn.sql has already been applied.

BEGIN;

-- Replace channel_id + active with per-user UID tracking
ALTER TABLE public.monitored_sections
    ADD COLUMN discord_uids JSONB NOT NULL DEFAULT '[]';

ALTER TABLE public.monitored_sections
    DROP COLUMN channel_id;

ALTER TABLE public.monitored_sections
    DROP COLUMN active;

-- SUBJECTS and COURSES are now auto-derived from monitored_sections JOIN sections
DELETE FROM public.scrape_config WHERE key IN ('SUBJECTS', 'COURSES');

COMMIT;
