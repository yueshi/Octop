# MiniApp 平台路线图：多视图、外部集成与分发

> 状态：R1 已实施（2026-11-12）；R2/R3 待实施
>
> 范围：工作台多 Board、实时外部数据、事件进出、统一定时任务、跨主机迁移、IM 全功能与认证。
>
> 目标：把 MiniApp 从“Agent 生成的数据视图”推进为 **可编排、可连接、可分发的展示面**，同时保持页面无状态、凭据不进页面、外部 I/O 全部宿主代调。
>
> 前置阅读：
> [light-apps-architecture.md](./light-apps-architecture.md)、
> [light-apps-enhancement-plan.md](./light-apps-enhancement-plan.md)、
> [miniapp-event-api-design.md](./miniapp-event-api-design.md)、
> [miniapp-im-optimization.md](./miniapp-im-optimization.md)、
> [external-events-design.md](./external-events-design.md)。
>
> 演示稿：[HTML](./miniapp-platform-roadmap.html) · [PPTX](./miniapp-platform-roadmap.pptx)。
>
> 实施进度（2026-11-12）：F1 页面能力清单、F2 发布快照、F3 composeMessage、F4 限流、F5 上一版回滚、P1/P2 服务端多 Board 均已落地。当前实现为个人看板（每个用户多个 Board、12 列拖拽/缩放、CAS 保存、独立 Tab 展示、旧本地布局无损迁移）；§5.6 中的公开看板分享（share token、`/p/board/*`）留到 P3 与公开实时数据一起做。

---

## 1. 结论摘要

6 个新需求分三层，叠在既有加固计划之上：

| 层 | 需求 | 本质 |
|---|---|---|
| 展示层 | 多工作台视图、全屏 Tab、对外展示 | 把本地单 Board 升级为服务端多 Board |
| 集成层 | 实时外部数据、事件进出、定时任务 | 页面从“读表”升级为“受控地访问外部世界” |
| 分发层 | 跨主机迁移、IM 全功能与认证 | 应用从单实例升级为可搬运、多入口访问 |

贯穿三层的地基是 **能力清单（capability manifest）**。没有它，实时外部调用、事件发送、IM 认证链接都会各自成为新的越权入口。

按三个可独立发布的里程碑推进：

| 里程碑 | 内容 | 可交付状态 |
|---|---|---|
| **R1** | 能力清单、发布快照、composeMessage、限流、回滚、多 Board | 更安全、更好用的 MiniApp 工作台 |
| **R2** | 实时外部数据、事件收件箱/告警、统一定时任务、对外看板 | 读书记录台等外部数据应用可用 |
| **R3** | 迁移包、IM owner 认证链接、渠道身份绑定 | 跨主机迁移；Feishu 内打开完整应用 |

---

## 2. 决策日志

| # | 决策点 | 结论 |
|---|---|---|
| 1 | Board 布局 | **12 列自由网格 + `react-grid-layout`**；服务端 `layout_json` 存 `{x,y,w,h}`，保存做 CAS |
| 2 | 对外看板权限 | **每个应用必须已发布**；看板 token 只授权布局，未发布单元格显示占位；分享时提供“批量发布” |
| 3 | 实时外部数据 MVP | **先通用 `http-extract` + URL 前缀白名单，豆瓣 adapter 后置** |
| 4 | 公开实时 | **结构化 adapter + 缓存优先 + 限流**；通用 `http-extract` 只服务登录态；豆瓣 adapter 是公开实时的前置条件 |
| 5 | 事件范围 | **复用 `infra/events`，分阶段**；MVP 做 `app.inbox` + 出站 webhook + Agent 通知，渠道直投后置 |
| 6 | 定时任务 | **新建 `light_app_schedules` + 扩展 AppSourceScheduler**；cron + 服务器时区；迁移 `schedule_seconds`；不动 `cron_jobs` |
| 7 | 跨主机迁移 | **Bundle v2：内容 + 声明，不带凭据/token**；目标机重新绑定 connector；调度默认停用 |
| 8 | IM 全功能 | **先 owner 认证链接，后渠道身份绑定**；私聊 owner 发一次性链接，群聊/非 owner 只读；默认 `identity_mode=owner` |
| 9 | 发布节奏 | **R1 / R2 / R3 三个独立里程碑**，每个阶段可独立合并、验收、回滚 |

### 2.1 决策 4 带来的顺序调整

决策 3 是“通用先、豆瓣后”，决策 4 要求公开页实时查豆瓣。结合后的准确顺序：

1. **I1a（R2 前段）**：通用 `http-extract` + 服务声明 + 缓存 + 封面 asset，**仅登录态可用**。
2. **I1b（R2 后段）**：豆瓣 adapter + `public_live` 开关 + 缓存优先 + 限流，**公开页才开放实时**。
3. **P3 对外看板** 依赖 I1b；在看板公开前，单元格里的实时外部数据只能读缓存。

如果 R2 时间紧张，I1b + 公开实时可整体滑到 R3；R2 先交付登录态实时 + 缓存版公开页。

---

## 3. 架构不变量

1. **页面是无状态 iframe**：没有每应用进程、没有本地端口、没有 JWT；所有外部 I/O 由宿主代调。
2. **凭据只在服务端**：页面拿到的是数据或短期能力，不是 connector token、IM token、发布 token。
3. **能力先声明后使用**：`external.fetch`、`events.emit`、`chat.compose`、`ai.generate` 都进 manifest；旧应用默认全开，新应用默认安全集。
4. **一个事件面**：扩展现有 `infra/events`，不新建第二套事件总线。
5. **一个 App 调度器**：`light_app_schedules` 统一管理应用级定时任务，逐步替代 `light_app_sources.schedule_seconds`。
6. **一个连接器通道**：`GatewayToolRunner` + catalog adapter；已有 `http-extract`（带 SSRF 防护）可复用，不开放页面任意 URL。
7. **公开面只读且可缓存**：公开链接/看板不写数据；公开实时仅限结构化 adapter、缓存优先、限流。

---

## 4. 与既有设计的关系

| 既有文档 | 关系 |
|---|---|
| `light-apps-architecture.md` | F1/F2 的数据与发布基础；本路线图扩展能力清单、发布快照和多 Board |
| `light-apps-enhancement-plan.md` | 数据源、备份/恢复、应用交接、模板等已落地部分；本路线图接续实时服务与 Bundle v2 |
| `miniapp-event-api-design.md` | 事件平面的长期设计（app-scoped key、CloudEvents、契约、ordering）。R2 先取其中的最小子集：`app.inbox` + `GET /api/apps/{id}/events` + 最小出站；app-scoped ingress key、CloudEvents、事件契约、ordering 后置，避免重复造轮子 |
| `miniapp-im-optimization.md` | IM 快速路径（`app_share_link`、纯文本/预览图、单轮预算）已落地。R3 在其上增加“认证 WebView”；**“不在 IM 聊天里渲染完整 Dashboard”仍是非目标**，完整交互在 WebView/浏览器中打开 |
| `miniapp-creation-modes-design.md` | 创建路径不变；新建应用会带上 capability 默认集 |
| `external-events-design.md` / `external-events-implementation-plan.md` | 事件平面基础；R2 复用 source/subscription/delivery/retry，不重写 outbox |

---

## 5. R1：基础与展示

### 5.1 F1 能力清单

**数据模型**

`light_apps` 新增 `capabilities_json TEXT NOT NULL DEFAULT ''`。空字符串 = 旧应用，按“全部现有能力”解释，避免迁移回填。

**能力目录**

| 能力 | 对应 SDK/动作 | 新应用默认 | 旧应用 | 强制点 |
|---|---|---|---|---|
| `data.read` | `me/schema/tables/listRows` | 开 | 开 | 宿主 bridge |
| `data.write` | `appendRow` | 开 | 开 | **仅 bridge**（详情页数据面板也走 `POST /rows`，服务端不能拦） |
| `asset.read` | `asset` | 开 | 开 | 宿主 bridge |
| `external.open` | `openExternal` | 开 | 开 | 宿主 bridge |
| `chat.compose` | `composeMessage`（F3） | 开 | 开 | 宿主 bridge |
| `events.read` | `events/onEvent`（R2） | 开 | 开 | bridge + 服务端 |
| `ai.generate` | `generate` | **关** | 开 | bridge + 服务端 `/generate` |
| `game.launch` | `launchGame` | **关** | 开 | bridge + 服务端 `/game-sessions` |
| `external.fetch` | service 调用（R2） | **关** | 开（无旧方法，实际不可用） | bridge + 服务端 |
| `events.emit` | `emitEvent`（R2） | **关** | 开（无旧方法） | bridge + 服务端 |
| `schedules.manage` | 调度 CRUD（R2） | **关** | 开（无旧方法） | 服务端 |

**API 与错误**

- `PUT /api/apps/{app_id}/capabilities`，权限 `edit_structure`，审计 `light_app.capabilities.update`。
- `AppSummary` / `AppDetail` 暴露 `capabilities`；`me()` 返回 `{permission, capabilities}`。
- 新错误码 `APP_CAPABILITY_DENIED`（403），同步 `src/octop/i18n/{en,zh}.json` 与 dashboard `apiErrors`。

**Agent 工具**

- `app_create(..., capabilities=[...])`；`app_set_capabilities`；`_summary`/`_app_card` 带上能力。
- 专家手册 `src/octop/infra/agents/experts/library/light-apps/AGENTS.md` 增加能力声明说明。

**关键文件**

`src/octop/infra/apps/capabilities.py`（新增）、`infra/db/repos/apps.py`、`infra/apps/service.py`、`infra/apps/tools.py`、`api/routers/apps.py`、`dashboard/src/components/MiniAppFrame/{sdk.ts,index.tsx}`、`dashboard/src/pages/Apps/Detail.tsx`。

### 5.2 F2 发布快照

**语义**

- 发布 = 冻结 HTML + 公开表结构（表清单、列定义、标签、公开可见性）。
- 数据行继续实时读；PII 脱敏和 `public_visible` 永远用实时值（隐藏表立即失效）。
- 已发布链接在重新发布前不随 `app_write_html`/schema 变化而改变。
- 旧发布（快照为空）公开读回退实时，状态提示“尚未固定内容”。

**数据模型**

`light_app_publications` 新增：

```sql
html_snapshot        TEXT NOT NULL DEFAULT ''
tables_snapshot_json TEXT NOT NULL DEFAULT ''
snapshot_digest      TEXT NOT NULL DEFAULT ''
snapshot_at          INTEGER
```

**API**

- `POST /api/apps/{id}/publication`：创建/轮换 token 时同时发布快照。
- `POST /api/apps/{id}/publication/publish`（新增）：只刷新快照，不轮换 token；`edit_structure`。
- `GET /api/apps/{id}/publication`：返回 `published_at`、`snapshot_stale`、`stale_reasons`。
- 公开端点读快照；`/rows` 只允许“快照内表 ∩ 实时 `public_visible`”，返回行按快照列过滤，脱敏用实时 schema。
- `app_share_link` 发布前先确保快照存在，保证 Agent 发出的链接就是刚交付的版本。

**关键文件**

`src/octop/infra/apps/service.py`、`infra/db/repos/apps.py`、`api/routers/{apps.py,public_apps.py}`、`dashboard/src/pages/Apps/Detail.tsx`。

### 5.3 F3 composeMessage

- SDK 新增 `composeMessage(text)`；宿主把文本填入聊天输入框，**不自动发送**。
- 详情页：`navigate('/chat/<agent_id>', { state: { prefillInput: text } })`，复用 `dashboard/src/pages/Chat/index.tsx:179-211` 的现有机制。
- 聊天侧栏/卡片：留在当前路径，通过 `location.state.prefillInput` 预填。
- 长度 1–4000；公开链接不暴露。
- 能力 `chat.compose`。

### 5.4 F4 限流

| 维度 | 默认值 |
|---|---|
| 每应用并发 LLM 调用 | 2（`generate` + assistant 合计） |
| 每应用每分钟 LLM 调用 | 30 |
| 公开链接/看板每 token+IP | 120 次/分钟，突发 30 |
| 公开链接并发 | 20 |
| 外部服务调用（R2） | 每服务 30 次/分钟 + 全局配额 |
| 事件发送（R2） | 每 app 每分钟 30 条 |

- 超限 `APP_BUSY`（429）+ `Retry-After`。
- 模块内实现每进程限流；多实例是每实例限流，文档注明。

### 5.5 F5 上一版 HTML 回滚

- `light_apps` 新增 `previous_html TEXT NOT NULL DEFAULT ''`、`previous_html_at INTEGER`。
- `write_html` 覆盖前保存旧 HTML；`POST /api/apps/{id}/restore-html`（`edit_structure`）恢复；审计。
- 详情页“恢复上一版”按钮；公开链接不受影响，恢复后需重新发布。

### 5.6 P1/P2 多 Board

> 实施状态：已完成个人看板部分（`miniapp_boards`、`/api/boards*`、12 列网格与独立展示页）。`share_token_hash` 列与下方公开看板接口尚未加入，属于 P3「对外看板」；当前不做公开分享。

**数据模型**

```sql
miniapp_boards (
  id               INTEGER PRIMARY KEY,
  board_id         TEXT NOT NULL UNIQUE,
  owner_user_id    INTEGER NOT NULL REFERENCES users(id),
  name             TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  layout_json      TEXT NOT NULL DEFAULT '{}',
  share_token_hash TEXT UNIQUE,
  share_created_at INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
)
```

`layout_json`：

```json
{
  "version": 1,
  "grid": { "cols": 12, "row_height": 80 },
  "refresh_seconds": 60,
  "items": [{ "app_id": "app_xxx", "x": 0, "y": 0, "w": 4, "h": 4 }]
}
```

**API**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/boards` | 列表/创建 |
| GET/PATCH/DELETE | `/api/boards/{board_id}` | 详情/改名/删除 |
| PUT | `/api/boards/{board_id}/layout` | 保存布局（CAS） |
| POST/DELETE | `/api/boards/{board_id}/share` | 创建/撤销公开看板 token |
| GET | `/api/public/boards/{token}` | 公开看板布局 + app 摘要 |
| GET | `/api/public/boards/{token}/apps/{app_id}/html` | 已发布快照 HTML |
| GET | `/api/public/boards/{token}/apps/{app_id}/rows` | 已发布快照的实时行 |
| GET | `/p/board/{token}` | 服务端渲染的公开看板页 |

**前端**

- 工作台从“单个 localStorage Board”升级为“服务端 Board 列表 + 切换器”。
- 12 列网格拖拽/缩放，使用 `react-grid-layout`（新增依赖）。
- 路由：`/apps/boards/{board_id}`（带工作台外壳）、`/apps/boards/{board_id}/view`（全屏、无侧栏/顶栏，在新标签页打开）。
- 每个 Board 有稳定 URL，多 Tab 可各开一个；服务端状态避免 localStorage 跨 Tab 互相覆盖。
- 首次加载时把旧 `octop:apps-board:v1` 迁移为默认 Board。

### 5.7 R1 数据模型与迁移

- R1 新增列/表：`capabilities_json`、`previous_html*`、`light_app_publications` 快照列、`miniapp_boards`。
- 当前 schema 是 v30。若 030 在 R1 合并时仍未发布，全部折进 `030_light_app_table_visibility.sql`/`.pg.sql`，并在 `migrate.py` 加幂等 `_ensure_column`/建表 helper；若 030 已发布，切 `031`。
- `tests/unit/db/test_db_pool.py` 的版本断言按实际迁移调整。
- 旧应用、旧发布、旧 localStorage Board 都必须无感迁移。

### 5.8 R1 验收

- 旧应用（空 capabilities）所有现有 SDK 方法正常。
- 新应用关掉 `ai.generate`/`data.write` 后，页面被拒；详情页数据面板不受影响。
- 发布后改 HTML/schema，公开页不变；重新发布后更新；隐藏表/PII 立即生效。
- composeMessage 在详情页和聊天侧栏都能预填，不自动发送。
- 多 Board 可创建/切换/拖拽/缩放；每个 Board 可在独立 Tab 全屏。
- `make all` 全绿；`cd dashboard && npx tsc -b` 通过。

---

## 6. R2：集成

### 6.1 I1a 通用外部服务（登录态）

**数据模型**

```sql
light_app_services (
  id                        INTEGER PRIMARY KEY,
  service_id                TEXT NOT NULL UNIQUE,
  app_id                    TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  name                      TEXT NOT NULL,
  connector_instance_id     TEXT NOT NULL,
  allowed_tools_json        TEXT NOT NULL DEFAULT '[]',
  allowed_url_prefixes_json TEXT NOT NULL DEFAULT '[]',
  cache_ttl_seconds         INTEGER NOT NULL DEFAULT 300,
  max_calls_per_minute      INTEGER NOT NULL DEFAULT 30,
  public_live               INTEGER NOT NULL DEFAULT 0,
  enabled                   INTEGER NOT NULL DEFAULT 1,
  created_by                INTEGER NOT NULL,
  created_at                INTEGER NOT NULL,
  updated_at                INTEGER NOT NULL,
  UNIQUE(app_id, name)
)

light_app_service_cache (
  service_id   TEXT NOT NULL,
  tool         TEXT NOT NULL,
  args_hash    TEXT NOT NULL,
  response_json TEXT NOT NULL,
  expires_at   INTEGER NOT NULL,
  created_at   INTEGER NOT NULL,
  PRIMARY KEY(service_id, tool, args_hash)
)
```

**调用链**

```
页面 octopApp.service("douban").call("get_book", {isbn})
  → 宿主 bridge（检查 external.fetch + service 声明）
  → POST /api/apps/{id}/services/{name}/call
  → 校验 app ACL / 工具白名单 / 参数 schema / URL 前缀
  → GatewayToolRunner.run(...)（线程池，避免阻塞事件循环）
  → 缓存 JSON，返回给页面
```

- 只允许只读工具（沿用 `_require_read_only`）。
- `http-extract` 的 `url` 必须命中 `allowed_url_prefixes_json`，并复用 `validate_agent_fetch_url` 的 SSRF 校验。
- 响应 ≤5MB、超时 20s、每服务每分钟限流。
- 封面：新增 `POST /api/apps/{id}/services/{name}/image`，服务端拉取图片 → 存 `assets/` → 返回 asset 名；页面用 `octopApp.asset(name)`。
- 公开链接：只读缓存，不发起 live 调用。
- 公开 asset：新增 `GET /api/public/apps/{token}/assets/{name}`，并在公开 `_SDK` 增加 `asset`。

**Agent 工具**：`app_add_service`、`app_list_services`、`app_remove_service`。

### 6.2 I1b 豆瓣 adapter + 公开实时

- 新增 `src/octop/infra/connectors/gateway/adapters/douban.py`，在 `gateway/registry.py` 注册，`catalog.json` 加条目（`mcp_mode: gateway`, `auth_kind: none`）。
- 工具：`search_books(keyword)`、`get_book(isbn|url)`、`get_cover(url)`。
- 公开实时规则：
  1. 只允许结构化 adapter（不接收任意 URL）；通用 `http-extract` 永不参与公开实时。
  2. 缓存优先，TTL 5–10 分钟；single-flight 防击穿；上游失败可在有限窗口返回 stale。
  3. 按看板/应用 token + IP（30/min）+ 每服务全局配额（120/min）+ 熔断；429 + `Retry-After`。
  4. 只允许 `auth_kind=none` 的公开服务。
  5. 输入/输出约束：ISBN/关键词校验；JSON ≤1MB、图片 ≤2MB、超时 10s。
  6. owner 显式开启 `public_live`，默认关闭。
- 封面统一走 asset；公开页通过公开 asset 端点读取。

### 6.3 I2 事件收件箱

复用 `infra/events`，新增 app 目标模式 `app.inbox`：

```sql
light_app_events (
  id           INTEGER PRIMARY KEY,
  event_id     TEXT NOT NULL UNIQUE,
  app_id       TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  direction    TEXT NOT NULL,          -- 'in' | 'out'
  event_type   TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  source_id    TEXT,
  dedupe_key   TEXT,
  created_at   INTEGER NOT NULL,
  seen_at      INTEGER,
  UNIQUE(app_id, dedupe_key)
)
```

- 入站：`app.inbox` 投递写一行；页面轮询/宿主推送。
- API：`GET /api/apps/{id}/events?since=`、`POST /api/apps/{id}/events/{event_id}/ack`。
- SDK：`octopApp.events({since, limit})`、`octopApp.onEvent(cb)`；宿主在 app 打开时每 5–10 秒轮询并推送。
- 保留：每 app 最多 500 条 / 30 天。
- 公开链接不提供事件接口。

### 6.4 I3 出站事件/告警

- SDK：`octopApp.emitEvent(type, payload)` → `POST /api/apps/{id}/events`；能力 `events.emit`。
- 复用现有事件面：App 作为隐藏的 `app:<app_id>` source，走 `ingest` → subscription → delivery/retry。
- MVP 目标：`webhook`（owner 配置 URL + HMAC 签名，SSRF/私网校验）和 `expert.notify`（Agent 通知，可转发到已有渠道）。
- `channel` 直投后置到 D4 身份模型确定之后。
- 页面不能指定投递目标；payload ≤64KB；每 app 每分钟 30 条；死信可查。

### 6.5 I4 统一定时任务

```sql
light_app_schedules (
  schedule_id    TEXT NOT NULL UNIQUE,
  app_id         TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  action         TEXT NOT NULL,        -- sync_source | snapshot_publication | run_agent | refresh_service_cache
  payload_json   TEXT NOT NULL DEFAULT '{}',
  schedule_spec  TEXT NOT NULL,        -- "0 9 * * *" 或 "@every 1h"
  timezone       TEXT NOT NULL,
  enabled        INTEGER NOT NULL DEFAULT 1,
  next_run_at    INTEGER,
  lease_until    INTEGER,
  last_run_at    INTEGER,
  last_status    TEXT,
  last_error     TEXT,
  created_by     INTEGER NOT NULL,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
)
```

- 复用 APScheduler `CronTrigger` 计算 `next_run_at`；时区默认 `config.default_timezone`。
- 调度循环沿用 `AppSourceScheduler` 的 30 秒扫描 + 条件 UPDATE 租约，多实例只执行一次。
- MVP 动作：`sync_source`、`snapshot_publication`；P2 加 `run_agent`、`refresh_service_cache`。
- 迁移：`light_app_sources.schedule_seconds > 0` 自动生成一条 `sync_source` 调度，然后停止扫描该列；列保留一个版本再删。
- UI：App 详情“自动化”页签（列表、cron、下次运行、最近状态、启用/停用、立即运行）。
- Agent 工具：`app_schedule_create/list/update/delete/run_now`。

### 6.6 P3 对外看板

- Board token 只读；每个单元格检查该 app 是否有有效发布快照，未发布显示占位。
- 公开看板页每格复用 sandbox iframe + `_SDK`，bridge 指向 board 作用域端点。
- 看板 token 可撤销/轮换；`Cache-Control: no-store`；限流。
- 公开实时外部调用仅当服务 `public_live=1` 且走结构化 adapter；否则只读缓存。
- 分享时提供“批量发布看板内应用”，但授权仍落在每个应用上。

### 6.7 R2 验收

- 读书记录台在登录态实时查书、显示封面；重复查询命中缓存。
- 公开页可实时查豆瓣（仅 adapter + `public_live`），缓存/限流生效；通用 `http-extract` 公开调用被拒。
- 外部 webhook → app 收件箱 → 页面通知；页面发告警 → webhook/Agent 通知。
- 定时任务统一在 App 详情管理；旧 `schedule_seconds` 自动迁移；多实例只跑一次。
- 对外看板只显示已发布应用；撤销 token 后 404。

---

## 7. R3：分发

### 7.1 D1/D2 跨主机迁移（Bundle v2）

**迁移边界**

| 迁移 | 不迁移 |
|---|---|
| HTML、schema、行数据、assets、preview | connector 凭据 |
| capabilities、模板来源标识 | 发布 token（目标机重新发布） |
| services/sources/schedules 声明（不含 secret） | IM 身份绑定、审计历史 |
| 可选助手历史 | Board 布局（单独 bundle） |

**Bundle 结构**

```
manifest.json          # format/version, app metadata, capabilities, tables,
                       # services (no creds), sources (no secrets), schedules,
                       # publication metadata (no token), checksums
app.html
preview.svg
assets/...
tables/<key>/schema.json
tables/<key>/data.csv
assistant/history.json  # 可选
```

- 导入：新 app_id、目标 agent；恢复表/资产/HTML/capabilities；services/sources/schedules 默认 `disabled`；connector 实例缺失标记 `needs_binding`。
- 不覆盖已有应用；名称冲突加后缀；可选 `publish_now`。
- API：`GET /api/apps/{id}/export`、`POST /api/apps/import`。
- CLI：`octop app export <app> --out bundle.zip`、`octop app import <bundle.zip> --agent <id>`（embedded server）。
- 安全：导出提示包含 PII；沿用 64MB/256MB 解压上限；可选密码加密放 P2。

### 7.2 D3 owner 一次性认证链接

```sql
app_access_codes (
  code_hash           TEXT NOT NULL UNIQUE,
  app_id              TEXT NOT NULL,
  user_id             INTEGER NOT NULL,
  permission          TEXT NOT NULL,
  channel_type        TEXT,
  channel_subject_id  TEXT,
  expires_at          INTEGER NOT NULL,
  consumed_at         INTEGER,
  created_at          INTEGER NOT NULL
)
```

- `POST /api/apps/{id}/access-link`：已登录用户为自己生成；TTL 60 秒、单次消费。
- `GET /auth/app?code=...`：消费 code → 复用 SSO `exchange_login_code_response` 模式签发 JWT → 302 到 `/apps/{id}`。
- `app_share_link` 增加 `mode="auth"`：仅在**私聊且发送者是 owner** 时返回认证链接；群聊/非 owner 回退只读公开链接。
- 安全：code 只存哈希；单次；短 TTL；审计；不把 JWT 放进 IM 文本。

### 7.3 D4 渠道身份绑定

```sql
channel_identities (
  id                   INTEGER PRIMARY KEY,
  channel_type         TEXT NOT NULL,
  external_subject_id  TEXT NOT NULL,
  user_id              INTEGER NOT NULL REFERENCES users(id),
  verified_at          INTEGER NOT NULL,
  created_at           INTEGER NOT NULL,
  UNIQUE(channel_type, external_subject_id)
)
```

- `/bind <code>`：Dashboard 生成绑定码 → 用户在 IM 发送 → gateway 校验（含 user_id、TTL 5 分钟、单次）→ 写入映射。
- `channel.identity_mode`：默认 `owner`（不改变现有行为）；开启 `bound` 的渠道才按绑定用户处理。
- D4 完成后，才允许给非 owner 发认证链接。
- 群聊策略：不发个人认证链接；只发公开只读链接；写入需私聊或 `/bind`。

### 7.4 D5 渠道矩阵

| 渠道 | 卡片/按钮 | 内嵌 WebView | OAuth | MVP |
|---|---|---|---|---|
| Feishu/Lark | 支持交互卡片 | 支持 | 支持（需配置） | 卡片按钮 + 认证链接，首选 |
| WeCom/WeChat | 有限 | 支持 H5 | 公众号 OAuth | 认证链接 + 只读回退 |
| DingTalk | 支持卡片 | 支持 | 支持 | 同 Feishu |
| Telegram | Inline URL 按钮 | 外部浏览器 | 无 | 认证链接 |

- 前置 1–2 天调研：WebView 域名白名单、卡片按钮回调、OAuth scope。
- 与 `miniapp-im-optimization.md` 的快速路径共存：聊天内仍是“文本 + 预览图 + 链接”，完整交互在 WebView 打开。

### 7.5 R3 验收

- A 主机导出 → B 主机导入可用；数据一致；connector 提示重新绑定；无凭据/token 泄露。
- Feishu 私聊 owner 点卡片按钮 → 认证 WebView 打开完整应用；群聊/非 owner 只读。
- D4 后绑定用户按自己的 ACL 读写；默认 `identity_mode=owner` 的渠道行为不变。

---

## 8. 横切项

| 项 | 内容 |
|---|---|
| i18n | 新错误码：`APP_CAPABILITY_DENIED`、`APP_BUSY`、`APP_SERVICE_DENIED`、`APP_EVENT_INVALID`、`APP_SCHEDULE_INVALID`、`APP_ACCESS_LINK_INVALID`、`BOARD_LAYOUT_INVALID`；`en/zh` 与 dashboard `apiErrors` 同步 |
| OpenAPI | Board、Service、Event、Schedule、Access Link、Import/Export 全部 `summary`/`description`/`response_model`；检查 `/api/docs` |
| 指标 | 服务调用/缓存命中/拒绝、事件入站/出站/重试、调度执行/失败、看板公开访问、认证链接签发/消费 |
| 审计 | 能力变更、服务绑定、事件目标、调度 CRUD、迁移导入、认证链接、Board 分享 |
| 测试 | 单元：capability/service/event/schedule/board/bundle；集成：外部调用白名单、缓存、公开只读、事件重试、调度租约、迁移导入、认证链接单次消费 |
| 文档 | 本文档 + 更新 `docs/api.md`、专家 AGENTS.md、CHANGELOG |
| 安全 | 外部调用白名单/SSRF/响应上限；webhook 签名/私网校验；Board token 撤销；认证链接单次/短 TTL；公开面永不任意 URL |

---

## 9. 迁移编号与兼容

- 当前 schema 为 **v30**。
- R1 的 `capabilities_json`、`previous_html*`、发布快照列、`miniapp_boards`：若 030 未发布，折进 030；若已发布，切 031。
- R2 的 `light_app_services`/`light_app_service_cache`/`light_app_events`/`light_app_schedules`：按当时最新未发布版本折入，或新切一个版本。
- R3 的 `app_access_codes`/`channel_identities`：同上。
- 始终维护 `.sql` / `.pg.sql` 成对文件；SQLite 需要重建的操作放 `migrate.py` 幂等 helper；同步 `tests/unit/db/test_db_pool.py` 版本断言。
- 兼容路径：
  - 空 `capabilities_json` → 旧应用全开。
  - 无快照的发布 → 公开读回退实时。
  - `schedule_seconds > 0` → 自动迁移为 `light_app_schedules`，旧列保留一个版本。
  - 旧备份 v1 → 导入时 capabilities/services 等字段缺省。
  - 旧 localStorage Board → 首次加载创建默认 Board。

---

## 10. 风险与滑档

| 风险 | 影响 | 缓解 |
|---|---|---|
| 新应用默认关闭 `ai.generate`/`game.launch`/`external.fetch`/`events.emit` | Agent 生成的应用报错 | 同步专家 AGENTS.md；`app_create` 显式声明；owner 可在详情页开启；旧应用全开 |
| 发布快照改变“改完即生效”预期 | 用户困惑 | 状态页明确“有未发布修改”；`app_share_link` 自动发布 |
| 快照与实时行漂移 | 页面字段空白 | `stale_reasons` 提示重新发布；公开 rows 按快照列过滤 |
| 公开实时被滥用 | 外部额度/SSRF | 仅结构化 adapter + `public_live` + 缓存优先 + 限流 + 熔断 |
| IM 身份改造 | 会话归属/权限回归 | D3 先 owner-only；D4 按渠道 opt-in；默认 `identity_mode=owner` |
| 迁移包含 PII | 数据泄露 | 导出提示；大小上限；可选密码加密；不入凭据/token |
| Board 新依赖 | 构建/体积 | 使用 MIT 的 `react-grid-layout`；锁定版本；预提交 build 验证 |
| 多 worker 限流不全局 | 绕过限制 | 文档注明每实例限流；需要全局时用 DB 租约 |

**可滑档项**：I1b（豆瓣 adapter + 公开实时）可整体从 R2 滑到 R3；R2 先交付登录态实时 + 缓存版公开页。

---

## 11. 下一步

1. 先提交/整理当前 `feature/light-apps` 未提交改动，避免与 R1 的 diff 混合。
2. R1 按 **F1 能力清单 → F2 发布快照 → F3/F4/F5 → P1/P2 多 Board** 顺序实施。
3. 每个阶段完成后运行：
   ```bash
   uv run pytest tests/unit/apps tests/unit/db -q
   uv run pytest tests/integration/test_apps_api.py -q
   uv run pytest tests/unit/i18n -q
   cd dashboard && npx tsc -b
   make all
   ```
4. R1 合并并收集反馈后，再启动 R2；R2 的 I1b 与 P3 按实际时间决定是否滑档。
