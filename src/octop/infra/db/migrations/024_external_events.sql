-- Schema v24: external event plane (inbox/outbox + subscriptions).
-- Sources receive webhook/poll events; matching subscriptions fan out into
-- deliveries that target adapters consume (expert turn, team judge, app run).

CREATE TABLE IF NOT EXISTS external_sources (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL UNIQUE,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  config_json   TEXT NOT NULL DEFAULT '{}',
  secret_blob   BLOB,
  enabled       INTEGER NOT NULL DEFAULT 1,
  cursor_json   TEXT NOT NULL DEFAULT '{}',
  next_run_at   INTEGER,
  fail_count    INTEGER NOT NULL DEFAULT 0,
  last_poll_at  INTEGER,
  last_error    TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_sources_due
  ON external_sources(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS external_subscriptions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id  TEXT NOT NULL UNIQUE,
  source_id        TEXT NOT NULL REFERENCES external_sources(source_id) ON DELETE CASCADE,
  target_type      TEXT NOT NULL,
  target_id        TEXT NOT NULL,
  mode             TEXT NOT NULL,
  filter_json      TEXT NOT NULL DEFAULT '{}',
  template         TEXT NOT NULL DEFAULT '',
  mapping_json     TEXT NOT NULL DEFAULT '{}',
  coalesce_seconds INTEGER NOT NULL DEFAULT 0,
  max_per_minute   INTEGER NOT NULL DEFAULT 0,
  enabled          INTEGER NOT NULL DEFAULT 1,
  created_by       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_subscriptions_source
  ON external_subscriptions(source_id, enabled);

CREATE TABLE IF NOT EXISTS external_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id      TEXT NOT NULL UNIQUE,
  source_id     TEXT NOT NULL REFERENCES external_sources(source_id) ON DELETE CASCADE,
  external_id   TEXT NOT NULL DEFAULT '',
  event_type    TEXT NOT NULL,
  occurred_at   INTEGER NOT NULL,
  received_at   INTEGER NOT NULL,
  summary       TEXT NOT NULL DEFAULT '',
  payload_json  TEXT NOT NULL DEFAULT '{}',
  payload_bytes INTEGER NOT NULL DEFAULT 0,
  dedupe_key    TEXT NOT NULL UNIQUE,
  status        TEXT NOT NULL DEFAULT 'received',
  created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_events_source
  ON external_events(source_id, received_at);

CREATE TABLE IF NOT EXISTS external_deliveries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id     TEXT NOT NULL UNIQUE,
  event_id        TEXT NOT NULL REFERENCES external_events(event_id) ON DELETE CASCADE,
  subscription_id TEXT NOT NULL REFERENCES external_subscriptions(subscription_id)
                    ON DELETE CASCADE,
  target_type     TEXT NOT NULL,
  target_id       TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempt         INTEGER NOT NULL DEFAULT 0,
  next_retry_at   INTEGER,
  lease_until     INTEGER,
  last_error      TEXT NOT NULL DEFAULT '',
  delivered_at    INTEGER,
  created_at      INTEGER NOT NULL,
  UNIQUE(event_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_external_deliveries_due
  ON external_deliveries(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_external_deliveries_lease
  ON external_deliveries(lease_until);

UPDATE _schema_version SET version = 24;
