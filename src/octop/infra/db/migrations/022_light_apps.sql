-- Schema v22: agent-generated lightweight apps (metadata + sharing ACL).
-- App files (app.html / data.csv / schema.json / manifest.json) live in the
-- owning agent's workspace under apps/<app_id>/.

CREATE TABLE IF NOT EXISTS light_apps (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id          TEXT NOT NULL UNIQUE,
  agent_id        TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  owner_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  row_count       INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_light_apps_owner ON light_apps(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_light_apps_agent ON light_apps(agent_id);

CREATE TABLE IF NOT EXISTS light_app_collaborators (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id      TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  permission  TEXT NOT NULL DEFAULT 'view',
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  UNIQUE(app_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_light_app_collab_user
  ON light_app_collaborators(user_id);

UPDATE _schema_version SET version = 22;
