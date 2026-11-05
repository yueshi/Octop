-- Schema v29: read-only public links for light apps.
-- The token is stored as a SHA-256 hash; the plaintext is returned once at
-- creation. A revoked or expired row resolves to 404.

CREATE TABLE IF NOT EXISTS light_app_publications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id      TEXT NOT NULL UNIQUE REFERENCES light_apps(app_id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL UNIQUE,
  created_by  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  INTEGER NOT NULL,
  revoked_at  INTEGER,
  expires_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_light_app_publications_hash
  ON light_app_publications(token_hash);

UPDATE _schema_version SET version = 29;
