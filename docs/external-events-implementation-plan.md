# 外部事件平面：可实施落地方案

> 状态：Phase 0–3 已实现（2026-10-27）：webhook、connector poll、expert/team/app target、
> 管理 API + 最小 UI、30 天清理。Phase 4 可选增强未做。
> 设计背景与取舍见 `docs/external-events-design.md`；本文只回答“按什么顺序、改哪些文件、怎么验收”。
>
> 核心原则：**共享 ingest/outbox，target 分开 worker/幂等/重试；先 vertical slice，再扩 source。**

---

## 1. 范围冻结

### 1.1 本方案做

- 外部事件统一入口：webhook（MVP）+ connector 只读工具轮询（Phase 3）。
- DB inbox/outbox：`external_events` + `external_deliveries`。
- 订阅：source → expert / team / app，支持过滤、合并、限流。
- 三个 target adapter：
  - expert：一次受限 agent turn；
  - team：`team_events(external.event)` → decision；
  - app：refresh / import 数据。
- HMAC、来源/目标 ACL、去重、retry/dead-letter、审计、最小管理 API。
- 最小外部事件管理页（Phase 3）。

### 1.2 明确不做

- 外部队列 / Temporal / 工作流 DSL。
- 每个事件一条 cron job。
- 让外部 payload 直接调工具或直接写任意目标。
- 任意 URL 抓取、无鉴权的公网 webhook 中继。
- MVP 不做 connector watch（Agent Mail 已有特例保留）、不做 app `append` 去重、不做多进程 HA。

---

## 2. 已拍板的默认值

| 决策 | 默认 |
|---|---|
| 公共 webhook 路径 | `POST /api/hooks/{source_id}`；JWT 豁免前缀 `/api/hooks/`；管理接口走 `/api/external/*` |
| webhook 鉴权 | HMAC-SHA256 + timestamp ±300s + external id 去重；secret 只显示一次 |
| 团队 manual 模式 | 只通知 owner/看板，不写 pending judgment；不允许 judge 订阅 |
| expert stopped | 不自动启动；delivery 进入 retry（`last_error` 标注）并首次通知 owner；agent 启动后下一次重试成功 |
| app 导入 | MVP 只允许 `replace`；权限要求 owner 或 `edit_data` |
| connector poll | MVP 只支持 `mode=incremental` + 稳定 id 的只读工具；snapshot diff 后置 |
| expert/team 成本 | subscription 级 `coalesce_seconds` + `max_per_minute`；team 复用 decision cooldown |
| 事件保留 | 30 天；payload 上限 256 KiB；summary 500 字；大内容放 artifact |
| 单进程 | worker 用条件 UPDATE lease；多 worker 后置 |
| UI | Phase 1/2 先 API + curl/测试；Phase 3 最小页面 |

---

## 3. 数据模型（v27）

新增迁移：

- `src/octop/infra/db/migrations/027_external_events.sql`
- `src/octop/infra/db/migrations/027_external_events.pg.sql`

遵循资源表约定：`id INTEGER` 代理 PK + 公开字符串 id；子表引用公开 id。

### 3.1 `external_sources`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | PK | 代理 |
| `source_id` | TEXT UNIQUE | 公开 id（ULID/short id） |
| `owner_user_id` | INTEGER -> users(id) | 来源归属 |
| `name` | TEXT | 展示名 |
| `kind` | TEXT | `webhook` / `connector_poll` |
| `config_json` | TEXT | poll: `instance_id/tool/args/event_type/items_path/id_path/summary_path/cursor_path/interval_seconds` |
| `secret_blob` | BLOB | webhook HMAC secret（Fernet） |
| `enabled` | INTEGER | |
| `cursor_json` | TEXT | poll 游标 |
| `next_run_at` | INTEGER | poll due |
| `fail_count` | INTEGER | |
| `last_poll_at` | INTEGER | |
| `last_error` | TEXT | |
| `created_at/updated_at` | INTEGER | |

索引：`(enabled, next_run_at)`。

### 3.2 `external_subscriptions`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `subscription_id` | TEXT UNIQUE | |
| `source_id` | TEXT -> external_sources ON DELETE CASCADE | |
| `target_type` | TEXT | `expert` / `team` / `app` |
| `target_id` | TEXT | 多态，不建 FK；投递时校验 |
| `mode` | TEXT | `expert.turn` / `expert.notify` / `team.judge` / `team.notify` / `app.refresh` / `app.import` |
| `filter_json` | TEXT | 简单字段匹配 |
| `template` | TEXT | 安全 `str.format` 字段 |
| `mapping_json` | TEXT | app import 列映射 |
| `coalesce_seconds` | INTEGER | expert/team 合并窗口 |
| `max_per_minute` | INTEGER | 0=不限 |
| `enabled` | INTEGER | |
| `created_by` | INTEGER -> users(id) | |
| `created_at/updated_at` | INTEGER | |

索引：`(source_id, enabled)`。

### 3.3 `external_events`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `event_id` | TEXT UNIQUE | |
| `source_id` | TEXT -> external_sources ON DELETE CASCADE | |
| `external_id` | TEXT | 来源 id |
| `event_type` | TEXT | |
| `occurred_at` | INTEGER | |
| `received_at` | INTEGER | |
| `summary` | TEXT | 有界摘要 |
| `payload_json` | TEXT | 上限 256 KiB |
| `payload_bytes` | INTEGER | |
| `dedupe_key` | TEXT UNIQUE | `source:external_id` 或 hash |
| `status` | TEXT | `received/no_subscribers/dispatched` |
| `created_at` | INTEGER | |

索引：`(source_id, received_at)`。

### 3.4 `external_deliveries`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `delivery_id` | TEXT UNIQUE | |
| `event_id` | TEXT -> external_events ON DELETE CASCADE | |
| `subscription_id` | TEXT -> external_subscriptions ON DELETE CASCADE | |
| `target_type/target_id` | TEXT | 冗余便于查询 |
| `status` | TEXT | `pending/retry/delivering/delivered/blocked/dead` |
| `attempt` | INTEGER | |
| `deliver_after` | INTEGER | 合并窗口后到期 |
| `next_retry_at` | INTEGER | |
| `lease_until` | INTEGER | |
| `started_at` | INTEGER | 用于识别“已开始但崩溃” |
| `last_error` | TEXT | |
| `delivered_at` | INTEGER | |
| `created_at` | INTEGER | |

`UNIQUE(event_id, subscription_id)`；索引 `(status, deliver_after)`, `(status, next_retry_at)`, `(lease_until)`。

**迁移测试**：`tests/unit/db/test_db_pool.py` 全部升到当前版本 v29；新增表加入 `test_run_migrations_creates_tables`。

---

## 4. 代码分层与文件清单

### 4.1 新增 `src/octop/infra/events/`

| 文件 | 职责 |
|---|---|
| `__init__.py` | 导出 service/worker/协议 |
| `service.py` | source/subscription CRUD、ingest 事务、secret、ACL、filter、webhook 验签 |
| `worker.py` | delivery/poll 循环、lease、retry、semaphore、coalesce；app/team target 内联在 `_deliver_app` / `_deliver_team` |
| `connector_poll.py` | connector_poll 配置解析、只读工具调用、item 解析、cursor |
| `expert_target.py` | expert.turn / expert.notify；独立 external session、usage source、stopped 通知 |
| `templating.py` | 安全 `{source}/{type}/{summary}/{event_id}` 字面替换 |
| `security.py` | HMAC、header 常量、replay window、payload cap |
| `rate_limit.py` | source/subscription 滑动窗口限流 |

当前没有 `models.py` / `filters.py` / `targets/` 目录。

**边界**：`infra/events/` 可依赖 `infra/db`、`infra/apps`、`infra/agents/teams`、`infra/cron`、`infra/connectors`；`api/` 只做 HTTP 适配；不要让 repos 依赖 events。

### 4.2 修改文件

| 文件 | 改动 |
|---|---|
| `src/octop/infra/db/repos/external_{sources,subscriptions,events,deliveries}.py`（新） | 每表一个 SQL-only repo；`external_events.ingest` 在单事务内写 event + deliveries |
| `src/octop/infra/db/services.py` | `RepoBundle`/`SharedServices` 加 4 个 repo |
| `src/octop/infra/server.py` | `AppRuntime.external_event_service` / `external_event_worker`；start/stop/replace_services |
| `src/octop/api/routers/hooks.py`（新） | `/api/hooks/{source_id}` 公共入口 |
| `src/octop/api/routers/external_events.py`（新） | `/api/external/*` JWT 管理 API |
| `src/octop/api/app.py` | 挂载两个 router |
| `src/octop/api/deps.py` | `/api/hooks/` JWT 豁免 |
| `src/octop/api/openapi_meta.py` | `external-events` tag |
| `src/octop/infra/errors.py` | `EXTERNAL_*` 错误码 |
| `src/octop/i18n/{en,zh}.json` + dashboard apiErrors | 错误文案 |
| `src/octop/infra/agents/teams/decision.py` | `external.event` judgment；snapshot 取 `summary`；prompt 注入防护 |
| `src/octop/infra/cron/delivery.py` | `CronDeliveryCommand.usage_source`（默认 cron） |
| `dashboard/src/api/modules/externalEvents.ts`（新） | 管理 API 类型与请求 |
| `dashboard/src/pages/Settings/ExternalEvents/`（新） | source/subscription/events/deliveries 最小页 |
| `dashboard/src/routes/index.tsx` / `prefetch.ts` / `layouts/sidebarNav.tsx` | 路由、预取、设置组导航 |
| `src/octop/infra/users/preferences.py` | `SIDEBAR_NAV_KEYS` 加 `external-events` |
| `docs/api.md` | 对外 HTTP 文档（已加） |
| `docs/team-drive-v2.md` | `external.event` 来源说明（已加） |

### 4.3 测试

- `tests/unit/events/test_security.py` — HMAC、replay window、限流
- `tests/unit/events/test_external_event_service.py` — source/secret、filter、dedupe、target 校验
- `tests/unit/events/test_worker.py` — app refresh/import、team judge/notify、retry、lease、disabled/empty 保护
- `tests/unit/events/test_expert_target.py` — turn/notify/stopped/owner
- `tests/unit/events/test_connector_poll.py` — cursor、dedupe、backoff、retention
- `tests/unit/cron/test_cron_delivery.py` — usage_source
- `tests/integration/test_external_events_api.py` — webhook→app、team judge decision、expert notify、connector poll、签名/时间戳/413、ACL、CRUD
- `tests/unit/db/test_db_pool.py` v29 + `external_*` 表/索引断言

---

## 5. API 契约

### 5.1 公共 webhook

```http
POST /api/hooks/{source_id}
Content-Type: application/json
X-Octop-Timestamp: 1790000000
X-Octop-Signature: sha256=<hex hmac(secret, timestamp + "." + raw_body)>
X-Octop-Event-Id: order_123
```

Body（建议但可宽松）：

```json
{
  "type": "order.created",
  "occurred_at": 1790000000,
  "data": { "...": "..." }
}
```

响应：

- `202 {"event_id":"...","accepted":true}`；
- 重复：`202 {"event_id":"...","duplicate":true}`；
- 签名失败：401 `EXTERNAL_SIGNATURE_INVALID`；
- source 不存在/禁用：404 `EXTERNAL_SOURCE_NOT_FOUND`；
- body 过大：413 `EXTERNAL_EVENT_TOO_LARGE`；
- JSON/字段非法：400 `EXTERNAL_EVENT_INVALID`；
- 超频：429 `EXTERNAL_RATE_LIMITED`。

实现要点：

- 路由直接 `await request.body()` 前先检查 `Content-Length`，再用分块读取 cap（建议 1 MiB，独立于 `max_upload_mb`）。
- 不经过 JWT；`/api/hooks/` 加入 `_JWT_EXEMPT_PREFIXES`（`api/deps.py:61`）。
- setup lockdown 无用户时仍返回 503；不额外豁免。
- 验签后才解析 JSON；解析失败 400；payload 只存限长。

### 5.2 管理 API

```text
GET/POST    /api/external/sources
GET/PATCH/DELETE /api/external/sources/{source_id}
POST        /api/external/sources/{source_id}/rotate-secret
POST        /api/external/sources/{source_id}/poll
GET         /api/external/connector-tools?instance_id=
GET/POST    /api/external/subscriptions
GET/PATCH/DELETE /api/external/subscriptions/{subscription_id}
GET         /api/external/events?source_id=&limit=
GET         /api/external/deliveries?status=&limit=
POST        /api/external/deliveries/{delivery_id}/retry
```

- 创建 source 返回 `source_id`、`webhook_path`、`secret` 一次；secret 用 Fernet 加密存 `secret_blob`。
- `connector_poll` source 的 `config` 校验 instance/tool 存在且只读；`next_run_at` 由 worker 推进。
- subscription 创建时校验 target 归属和 mode；manager 页展示 target 名称。
- 所有管理接口 `current_user`；admin 可跨用户，普通用户仅自己的资源。
- 新增错误码与 i18n；dashboard `apiErrors` 同步。

---

## 6. Worker 与 Target 执行

### 6.1 Ingest

`ExternalEventService.ingest`：

1. 验签、body cap、source 校验（公共路由层）；
2. `external_events` 插入（dedupe 冲突 → 返回已有 event_id）；
3. `subscriptions.match(source, event)`；`max_per_minute` 在 ingest 侧 token bucket；
4. 每个匹配写 `external_deliveries(status='pending')`；
5. 无订阅：event 状态 `no_subscribers`；
6. 返回 202。
7. `coalesce_seconds` 只在 worker 的 `expert.turn` 批次中生效；`team.judge` 复用 decision cooldown。

### 6.2 Worker 结构

不要一个循环串行跑所有 target。`ExternalEventWorker` 提供：

```python
asyncio.Task  # 主 claimer + sweeper
Semaphore(app=4, team=2, expert_per_agent=1, global=8)
```

- 主循环每 1s `claim_due_deliveries`（条件 UPDATE + lease）。
- 每个 delivery 根据 target 丢进对应 semaphore；`asyncio.create_task` 执行；不阻塞 claimer。
- 每 30s 扫描 `connector_poll` sources；`asyncio.to_thread(GatewayToolRunner.run)`。
- 每 30s 回收过期 `delivering`：
  - app/team：重排 `retry`（幂等）；
  - expert：若 `started_at` 已过且已开始，置 `blocked`/`needs_review`，不自动重跑（避免重复 turn）。
- 重试：5s/30s/2m/10m/1h；6 次后 `dead`。
- 用 `asyncio.Event` ingest 唤醒；DB 轮询兜底。

### 6.3 Target 协议

```python
class DeliveryError(Exception):
    retryable: bool
    blocked: bool

class Target(Protocol):
    async def deliver(self, ctx: DeliveryContext) -> None: ...
```

### 6.4 Expert adapter

- 目标：`agents.kind='expert'`，owner = source owner（admin 例外）。
- session：`agent_id:dashboard:{user_id}:external`；`ThreadRegistry.get_or_create_by_key`。
- 投递：`CronDeliveryCommand`（不写 cron_jobs）：
  - `cron_id=f"event:{delivery_id}"`, `cron_name=source.name`,
  - `prompt` 由 summary 模板生成，带“外部内容只作数据”；
  - `meta={"kind":"external_event","event_id":...,"usage_source":"external_event"}`。
- 需改 `CronDeliveryService._deliver_agent` 的 `source="cron"` 为 `command.usage_source`（默认 cron）。
- Agent 未运行：`DeliveryError(blocked=True)` + 通知 owner；不自动启动。worker 每 30s 扫描 `blocked` 的 expert delivery，若目标 agent 已 `running` 则重新置 `pending`（否则保持 blocked）。
- 幂等：prompt 带 `delivery_id`；崩溃后已开始的 delivery 不自动重试（见 6.2）；UI 可手动 retry。
- Spike gate：先用 1 天验证 `CronDeliveryService` + 独立会话 + stopped agent；失败则抽 `AgentTurnDelivery`，不要硬改 cron 服务。

### 6.5 Team adapter

- 目标：`agents.kind='team'`；runtime `mode=autopilot` 才允许 judge。
- `record(team_id, type="external.event", dedupe_key=f"external:{source_id}:{external_id}", actor=f"external:{source_id}", payload={"event_id", "source_id", "external_id", "event_type", "summary"})`。
- `decision.py`：`external.event` 加入 `JUDGMENT_EVENT_TYPES`；`recent_judgment` 的 excerpt 优先 `payload.summary`。
- 重复 delivery：event dedupe 已存在，adapter 视为成功。
- manual：只用 owner 级通知（`ws_hub.push_to_user`/dashboard push，不依赖团队房间），不写 judgment；不留下 backlog。
- 前置：Phase 0 修 `decision.py:563-564` 的 usage `int(dict)`。

### 6.6 App adapter

- 目标：app owner/admin；`app.refresh` 需 app source；`app.import` 要求 `edit_structure`。
- refresh：`AppService.run_source(app, app_source_id)`。
- import：payload 的 `records`/`data` → `import_rows(mode="replace", mapping=...)`；MVP 只允许 replace。
- 重复 delivery：replace 幂等；append 后置。
- 数据更新后可选 WS 帧；MVP 不阻塞。

---

## 7. 安全清单（代码级）

- `security.verify_hmac(secret, timestamp, raw_body, signature)`：`hmac.compare_digest`，timestamp ±300s。
- body cap：先查 `Content-Length`，再分块读，超限 413。
- token bucket：`source_id` 级 60/min；`subscription_id` 级 `max_per_minute`；单进程内存，重启重置。
- secret：`external_sources.secret_blob` 用 `encrypt_credentials`；API 不回读；rotate 覆盖。
- ACL：source owner；target owner/权限；admin 例外；投递时二次校验。
- prompt：只放 summary/模板字段；固定 guard 句；不把 payload 直接当指令。
- app：字段校验/转义；不执行 HTML/脚本；replace 全失败保护已有逻辑。
- 审计：source receive、delivery attempt/target/error；不记录 secret/完整敏感 payload。
- 保留：30 天清理 event/delivery；payload max 256 KiB；summary 500 字。

---

## 8. 分阶段实施

### Phase 0：卡点修复（1–1.5 人日）

- [x] 修团队 `decision.py:563-564` usage 嵌套 dict；补回归测试。
- [x] `external.event` 加入 judgment；snapshot 读 `summary`。
- [x] 新增 `EXTERNAL_*` 错误码/i18n。
- [x] 新增 027 迁移骨架 + v27 断言。
- [x] `/api/hooks/` JWT 豁免 + middleware 测试骨架。

**Gate**：团队 decision 测试绿；webhook 路径在 JWT/setup 下行为符合预期。未过不进 Phase 1。

### Phase 1：Webhook + App/Team vertical slice（3–4 人日）

- [x] repos：source/subscription/event/delivery。
- [x] service：ingest 事务、subscription 匹配、secret。
- [x] worker：claim/lease/retry；target semaphores（先 app/team）。
- [x] webhook route + HMAC/body/rate。
- [x] app target：refresh/import（replace）。
- [x] team target：external.event → autopilot decision；manual notify。
- [x] 产物边界：不新增 target；team 由 decision 快照 + `deliverable_update` 回填；app 不做 deliverable 映射。
- [x] 管理 API：source/subscription CRUD、events/deliveries 查询、retry。
- [x] 测试：webhook→app、webhook→team、重复、签名、ACL、retry。
- [x] 不接 expert、不做 poll、不做 UI。

**DoD**：真实 webhook 能更新一张 app 表；autopilot 团队能产生一条 decision；重复投递不重复；日志无 secret。

### Phase 2：Expert target（2–3 人日，含 spike）

- [x] spike 代码路径：`CronDeliveryService` + 独立 external session + stopped agent + usage source（真实 harness 联调待部署）。
- [x] expert adapter：session、prompt、retry/通知。
- [x] 产物边界：expert.turn 只触发 turn，不自动回填 expert deliverable；expert 侧目标/产物工具后续再补。
- [x] stopped expert 通过 retry/backoff 重新排队（未单独引入 blocked 状态）。
- [x] `CronDeliveryCommand.usage_source`（默认 cron）。
- [x] 启用 expert.turn 的 subscription `coalesce_seconds`；expert per-agent 并发 1。
- [x] 测试：fake agent turn、stopped、重复/崩溃、usage source。

**Gate**：spike 通过；否则先抽 delivery，再继续。

### Phase 3：Connector poll + 管理 UI（3–4 人日）

- [x] `connector_poll` source：只读工具、cursor、incremental、lease/backoff。
- [x] source/tool 结果解析（items/id/summary）；缺失 id 用 payload hash 去重。
- [x] 最简 UI：source（webhook URL/secret、poll 配置）、subscription、events/deliveries/retry。
- [ ] 可选：app subscription 的 `deliverable_id` 映射（导入成功后标记产物）；留到 Phase 4。
- [x] metrics/log、30 天清理、dead-letter UI。
- [x] 测试：poll 增量/重复/失败退避；UI 通过 tsc + Vite build（浏览器联调待部署）。

### Phase 4：可选增强（按需）

- connector watch（Agent Mail 泛化）；
- app sync 事件生产者；
- app append `source_key`；
- 多 worker lease/HA；
- 团队 token 预算/外部事件专属闸门；
- 更多 connector 原生订阅。

**总估算**：Phase 0–3 约 9–13 人日（单开发者，含单测，不含 UI 打磨/联调）；对外可用按 2–3 周排期，不要承诺 1 周。

---

## 9. 测试矩阵

| 类型 | 用例 |
|---|---|
| 单元 | HMAC/时间戳/replay；body cap；dedupe；filter；coalesce；token bucket；source claim/backoff；delivery retry/dead；secret 加密 |
| 集成 | webhook→app replace；webhook→team autopilot decision；webhook→expert turn（fake）；manual team notify；重复 delivery；ACL 403 |
| 中间件 | `/api/hooks/` 无 JWT 可入；`/api/external/*` 需 JWT；无用户时 setup lockdown 503；OpenAPI security 不加错 |
| 可靠性 | worker 崩溃/lease 回收；expert 已开始不重复；team dedupe；app replace 幂等 |
| 迁移 | v27 SQLite/PG；v25→v27；表/索引存在 |
| 安全 | 错签/过期/超大/超频/越权 target/跨用户 source |
| 跨平台 | `tmp_path`、不硬编码 POSIX；`posix_only` 仅用于平台相关 |

---

## 10. 卡点与止损线

| 卡点 | 止损/决策 |
|---|---|
| 团队 decision P0 未修 | 停止 Phase 1 team target；先修 + 回归 |
| `/api/hooks` 中间件行为不符合 | 停止 webhook 公开；改用反向代理签名头或先只支持 poll |
| expert spike 1 天不通 | 不硬复用 CronDelivery；抽 `AgentTurnDelivery`，Phase 2 延长 2 天 |
| app 导入映射/权限不清 | MVP 只支持 flat JSON + replace + owner/edit_structure |
| connector 工具无稳定 id | 拒接该工具；不做 snapshot 全量事件 |
| 100 事件压测产生 >N 次 LLM | 强制 coalesce/限流；未达标不上 expert/team 自动触发 |
| 迁移 v27 失败 | 回滚迁移；不进入 Phase 1 |

---

## 11. 上线与回滚

- 迁移 v27 → 重启 → worker 启动（无 source 时为空转）。
- 先创建一个测试 webhook source + 测试 app/team 订阅；用 curl 验证 HMAC、重复、retry。
- 观察 `external_events`/`external_deliveries`/日志；确认无 secret/payload 泄漏。
- 回滚：禁用所有 source（不删数据）→ 停 worker/重启旧 wheel；迁移保留但无代码读取。不要自动删表。
- 单进程限制写入 `docs`；多 worker 前必须验证 lease。

---

## 12. 实施顺序一句话

**Phase 0 修团队 P0 → Phase 1 webhook + app/team vertical slice → Phase 2 expert spike/adapter → Phase 3 poll + UI + 治理**。不要反顺序，也不要先铺 expert/connector watch。
