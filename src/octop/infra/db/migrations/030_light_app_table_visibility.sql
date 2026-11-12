-- Schema v30: per-table visibility for public MiniApp links, plus the page
-- capability manifest. Only tables explicitly marked public are exposed by
-- /p/{token}; existing apps keep the main table public and treat add-on
-- tables (members, loans, etc.) as private unless the owner opts in.

ALTER TABLE light_app_tables ADD COLUMN public_visible INTEGER NOT NULL DEFAULT 0;

-- Empty means a legacy app (all capabilities the original SDK exposed).
ALTER TABLE light_apps ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '';

UPDATE light_app_tables
SET public_visible = CASE WHEN table_key = 'main' THEN 1 ELSE 0 END;

UPDATE _schema_version SET version = 30;
