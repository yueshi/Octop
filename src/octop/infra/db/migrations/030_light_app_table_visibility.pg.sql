-- Schema v30: per-table visibility for public MiniApp links, plus the page
-- capability manifest. Mirrors the SQLite file.

ALTER TABLE light_app_tables ADD COLUMN IF NOT EXISTS public_visible INTEGER NOT NULL DEFAULT 0;

-- Empty means a legacy app (all capabilities the original SDK exposed).
ALTER TABLE light_apps ADD COLUMN IF NOT EXISTS capabilities_json TEXT NOT NULL DEFAULT '';

UPDATE light_app_tables
SET public_visible = CASE WHEN table_key = 'main' THEN 1 ELSE 0 END;

UPDATE _schema_version SET version = 30;
