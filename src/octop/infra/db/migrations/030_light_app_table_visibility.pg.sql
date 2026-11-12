-- Schema v30: per-table visibility for public MiniApp links, plus the page
-- capability manifest. Mirrors the SQLite file.

ALTER TABLE light_app_tables ADD COLUMN IF NOT EXISTS public_visible INTEGER NOT NULL DEFAULT 0;

-- Empty means a legacy app (all capabilities the original SDK exposed).
ALTER TABLE light_apps ADD COLUMN IF NOT EXISTS capabilities_json TEXT NOT NULL DEFAULT '';

-- The last HTML replaced by app_write_html, for a one-click restore.
ALTER TABLE light_apps ADD COLUMN IF NOT EXISTS previous_html TEXT NOT NULL DEFAULT '';
ALTER TABLE light_apps ADD COLUMN IF NOT EXISTS previous_html_at INTEGER;

-- Published links freeze the HTML and public table structure; rows stay live.
ALTER TABLE light_app_publications ADD COLUMN IF NOT EXISTS html_snapshot TEXT NOT NULL DEFAULT '';
ALTER TABLE light_app_publications ADD COLUMN IF NOT EXISTS tables_snapshot_json TEXT NOT NULL DEFAULT '';
ALTER TABLE light_app_publications ADD COLUMN IF NOT EXISTS snapshot_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE light_app_publications ADD COLUMN IF NOT EXISTS snapshot_at INTEGER;

UPDATE light_app_tables
SET public_visible = CASE WHEN table_key = 'main' THEN 1 ELSE 0 END;

UPDATE _schema_version SET version = 30;
