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
  html_bytes      INTEGER NOT NULL DEFAULT 0,
  data_bytes      INTEGER NOT NULL DEFAULT 0,
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

-- One row per table of an app. The main table keeps its rows in data.csv and
-- its columns in schema.json; additional tables live under tables/<key>/.
CREATE TABLE IF NOT EXISTS light_app_tables (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id     TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  table_key  TEXT NOT NULL,
  label      TEXT NOT NULL DEFAULT '',
  row_count  INTEGER NOT NULL DEFAULT 0,
  data_bytes INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(app_id, table_key)
);

CREATE INDEX IF NOT EXISTS idx_light_app_tables_app
  ON light_app_tables(app_id);

-- Per (app, user) assistant chat history. The assistant answers questions
-- about the app's data with read-only tools; messages are trimmed to the
-- most recent entries by the repo on insert.
CREATE TABLE IF NOT EXISTS light_app_assistant_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id     TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_light_app_assistant
  ON light_app_assistant_messages(app_id, user_id, id);

-- One data-source binding per (app, table): re-import a workspace file or pull
-- from an external connector tool into that table. schedule_seconds = 0 means
-- manual-only; run state (last_run_at/last_status/...) is updated per run.
CREATE TABLE IF NOT EXISTS light_app_sources (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id        TEXT NOT NULL UNIQUE,
  app_id           TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  table_key        TEXT NOT NULL,
  kind             TEXT NOT NULL,
  mode             TEXT NOT NULL DEFAULT 'append',
  schedule_seconds INTEGER NOT NULL DEFAULT 0,
  params_json      TEXT NOT NULL DEFAULT '{}',
  mapping_json     TEXT NOT NULL DEFAULT '{}',
  next_run_at      INTEGER,
  fail_count       INTEGER NOT NULL DEFAULT 0,
  last_run_at      INTEGER,
  last_status      TEXT,
  last_error       TEXT,
  last_row_count   INTEGER,
  created_by       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  UNIQUE(app_id, table_key)
);

CREATE INDEX IF NOT EXISTS idx_light_app_sources_app
  ON light_app_sources(app_id);

-- Autopilot team boards: goals and tasks owned by a team host agent.
-- Resource-table convention: public string id + integer surrogate PK; child
-- rows reference the parent's public string id (AGENTS.md §7).
CREATE TABLE IF NOT EXISTS team_goals (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id          TEXT NOT NULL UNIQUE,
  team_id          TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  title            TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',
  success_criteria TEXT NOT NULL DEFAULT '',
  created_by       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_goals_team ON team_goals(team_id);

CREATE TABLE IF NOT EXISTS team_tasks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL UNIQUE,
  team_id        TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  goal_id        TEXT REFERENCES team_goals(goal_id) ON DELETE SET NULL,
  title          TEXT NOT NULL,
  spec           TEXT NOT NULL DEFAULT '',
  assignee       TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'proposed',
  priority       INTEGER NOT NULL DEFAULT 0,
  result_summary TEXT NOT NULL DEFAULT '',
  blocked_reason TEXT NOT NULL DEFAULT '',
  version        INTEGER NOT NULL DEFAULT 1,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  due_at         INTEGER,
  window_start           INTEGER,
  window_end             INTEGER,
  budget_tokens          INTEGER,
  concurrency_key        TEXT,
  escalation_policy_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key        TEXT,
  active_wait_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_team_tasks_team ON team_tasks(team_id);
CREATE INDEX IF NOT EXISTS idx_team_tasks_team_status
  ON team_tasks(team_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_tasks_idem
  ON team_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Durable HITL approvals/questions (P3 drive-loop prerequisite). Mirrors the
-- in-memory HitlPendingStore; status keeps the store's four states.
CREATE TABLE IF NOT EXISTS hitl_pending (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key      TEXT NOT NULL,
  thread_id        TEXT,
  kind             TEXT NOT NULL,
  payload_json     TEXT NOT NULL DEFAULT '{}',
  created_by_agent TEXT,
  task_id          TEXT,
  status           TEXT NOT NULL DEFAULT 'pending',
  expires_at       INTEGER,
  resolved_by      TEXT,
  resolved_at      INTEGER,
  resume_token     TEXT,
  created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hitl_pending_session
  ON hitl_pending(session_key, status);
CREATE INDEX IF NOT EXISTS idx_hitl_pending_task ON hitl_pending(task_id);

-- Team coordination runtime (docs/team-drive-v2.md §4): the coordinator is a
-- persistent state machine, so its authority state lives here, not in memory.
CREATE TABLE IF NOT EXISTS team_runtime (
  team_id                TEXT NOT NULL PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
  status                 TEXT NOT NULL DEFAULT 'idle',
  mode                   TEXT NOT NULL DEFAULT 'manual',
  goal_id                TEXT,
  active_decision_id     TEXT,
  last_event_seq         INTEGER NOT NULL DEFAULT 0,
  last_decision_at       INTEGER,
  next_heartbeat_at      INTEGER,
  heartbeat_interval_sec INTEGER NOT NULL DEFAULT 1800,
  budget_day             TEXT NOT NULL DEFAULT '',
  budget_used            INTEGER NOT NULL DEFAULT 0,
  budget_reserved        INTEGER NOT NULL DEFAULT 0,
  paused_reason          TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  created_at             INTEGER NOT NULL,
  updated_at             INTEGER NOT NULL
);

-- Append-only event ledger; dedupe_key makes ingestion idempotent, seq gives
-- per-team ordering for watermarks.
CREATE TABLE IF NOT EXISTS team_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     TEXT NOT NULL UNIQUE,
  team_id      TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,
  type         TEXT NOT NULL,
  actor        TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  dedupe_key   TEXT NOT NULL UNIQUE,
  decision_id  TEXT,
  created_at   INTEGER NOT NULL,
  consumed_at  INTEGER,
  UNIQUE(team_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_team_events_team_seq ON team_events(team_id, seq);
CREATE INDEX IF NOT EXISTS idx_team_events_type
  ON team_events(team_id, type, created_at);

-- One budgeted decision-unit run (host LLM plan) with its committed plan for
-- audit/replay. At most one pending/running decision per team (caller-enforced).
CREATE TABLE IF NOT EXISTS team_decisions (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id            TEXT NOT NULL UNIQUE,
  team_id                TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  trigger_event_ids_json TEXT NOT NULL DEFAULT '[]',
  mode                   TEXT NOT NULL DEFAULT 'autonomous',
  status                 TEXT NOT NULL DEFAULT 'pending',
  reason                 TEXT NOT NULL DEFAULT '',
  snapshot_hash          TEXT NOT NULL DEFAULT '',
  plan_json              TEXT,
  budget_reserved        INTEGER NOT NULL DEFAULT 0,
  cost_tokens            INTEGER NOT NULL DEFAULT 0,
  thread_id              TEXT,
  error                  TEXT,
  started_at             INTEGER,
  finished_at            INTEGER,
  created_at             INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_decisions_team
  ON team_decisions(team_id, status);

-- Sparse wait ledger — one row per wait/blocking point, not per action.
-- ``idempotency_key`` dedupes re-delivered settlements; ``external_ref`` links
-- the inbox job id / hitl_pending row this wait is parked on.
CREATE TABLE IF NOT EXISTS team_waits (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  wait_id                TEXT NOT NULL UNIQUE,
  team_id                TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  task_id                TEXT,
  kind                   TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'waiting',
  due_at                 INTEGER,
  timeout_at             INTEGER,
  deadline_at            INTEGER,
  escalation_policy_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key        TEXT NOT NULL UNIQUE,
  external_ref           TEXT,
  payload_json           TEXT NOT NULL DEFAULT '{}',
  attempts               INTEGER NOT NULL DEFAULT 0,
  last_attempt_at        INTEGER,
  created_at             INTEGER NOT NULL,
  settled_at             INTEGER
);

CREATE INDEX IF NOT EXISTS idx_team_waits_due ON team_waits(status, due_at);
CREATE INDEX IF NOT EXISTS idx_team_waits_timeout
  ON team_waits(status, timeout_at);
CREATE INDEX IF NOT EXISTS idx_team_waits_task ON team_waits(task_id, status);

-- Task dependency edges (DAG). Cycle / cross-team checks belong to the writer.
CREATE TABLE IF NOT EXISTS team_task_deps (
  task_id            TEXT NOT NULL REFERENCES team_tasks(task_id) ON DELETE CASCADE,
  depends_on_task_id TEXT NOT NULL REFERENCES team_tasks(task_id) ON DELETE CASCADE,
  created_at         INTEGER NOT NULL,
  PRIMARY KEY (task_id, depends_on_task_id)
);

-- Team-scoped token metering: team member/host turns carry the owning host id
-- so per-team budgets and KPIs can be aggregated (docs/team-drive-v2.md §4.7).
ALTER TABLE usage_log ADD COLUMN team_id TEXT;

CREATE INDEX IF NOT EXISTS idx_usage_log_team ON usage_log(team_id, ts);

UPDATE usage_log SET team_id = agent_id
WHERE team_id IS NULL
  AND agent_id IN (SELECT agent_id FROM agents WHERE kind = 'team');

ALTER TABLE cron_jobs ADD COLUMN meta TEXT NOT NULL DEFAULT '{}';

-- 数字员工：自治开关（默认关；打开即视为数字员工）
-- (docs/digital-employee-plan.md S1.1)。SQLite 不支持 ADD COLUMN IF NOT EXISTS，
-- 由 migrate.py 的 _ensure_autonomy_schema 幂等补齐（见该文件的 version==22 分支）。
ALTER TABLE agents ADD COLUMN autonomy_enabled INTEGER NOT NULL DEFAULT 0;

-- 岗位规格：自治策略。加列到 proactive_care_config，不改表名（001 已发布，
-- 改名需 SQLite/PG 双份重建脚本）。代码层由 AutonomyPolicyRepo 读同一张表。
ALTER TABLE proactive_care_config ADD COLUMN budget_limit_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proactive_care_config ADD COLUMN budget_window TEXT NOT NULL DEFAULT 'day';
ALTER TABLE proactive_care_config ADD COLUMN sla_tasks_per_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proactive_care_config ADD COLUMN escalate_to TEXT NOT NULL DEFAULT '';
ALTER TABLE proactive_care_config ADD COLUMN responsible_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE proactive_care_config ADD COLUMN tool_scope_allow TEXT NOT NULL DEFAULT '';
ALTER TABLE proactive_care_config ADD COLUMN tool_scope_deny TEXT NOT NULL DEFAULT '';

UPDATE _schema_version SET version = 22;
