# MiniApp 数据与协作架构方案

> 状态：设计稿，未实施（2026-10-23）
>
> 范围：`feature/light-apps` 分支上的 MiniApp 能力（Agent tools + 工作区文件 + 沙箱 iframe）
>
> 目标：把行数据从工作区 CSV 迁移到控制面 DB，补齐多人并发、外部数据读取、发布链接与长期可维护性。
>
> 相关：[MiniApp 多 CSV（多数据表）设计](./light-apps-multi-table-design.md) 是一个**先行可交付的增量**
> （一个应用支持多张表），落地后与本方案的 P0 阶段衔接。
>
> 另见：[MiniApp 默认 AI 助手与外部数据导入设计](./light-apps-assistant-and-import-design.md)
> （§10 外部数据的落地版本）。

---

## 1. 背景与问题

当前实现（`src/octop/infra/apps/`）：

- 元数据与协作 ACL 在控制面 DB（`light_apps` / `light_app_collaborators`）；
- 内容文件在 Agent 工作区：`apps/<app_id>/{app.html,data.csv,schema.json,manifest.json}`；
- 读取走 `BackendWorkspace`，支持本地 / COS / S3 / OSS / OBS / docker 等后端；
- `append_row` 是“读全量 CSV → 校验 → 重新渲染整个 CSV → 写回”（`service.py:630-662`）；
- 并发保护是进程内 `asyncio.Lock`（`service.py:66-76`）；
- 工具调用要求调用者是 Agent owner（`tools.py:73-77`）；
- 查看界面只有 iframe，没有可直接维护数据的表格视图；
- 没有发布/分享链接，只有登录态下的应用 ACL。

这套实现有三个结构性限制：

1. **远程工作区写放大**：对象存储只支持整对象 GET/PUT，每次追加都要传输并重写整个 `data.csv`，行数越多越慢。
2. **多人并发不安全**：锁只在单进程内；`launch.py` 支持多 worker，多实例部署时两个追加会互相覆盖。
3. **数据不可直接维护**：数据只能由 Agent 追加或 iframe 内的表单写入；用户改一行、导一批数据都做不到。

WorkBuddy 的资料库/MiniApp 验证了正确方向：CSV 在云端作为“在线表格”承载，HTML 通过表格 ID 读取；发布链接默认关闭、可撤销；Agent 继承当前用户权限。本方案按 Octop 的自托管与多租户模型做适配。

---

## 2. 目标与非目标

### 2.1 目标

- 行数据、schema、数据源、发布记录进入控制面 DB；
- 追加不再重写整文件，远程工作区与多 worker 下正确；
- 多人同时编辑有乐观并发保护，冲突可见；
- 支持 CSV 导入/导出；
- 支持从 MCP 连接器做只读快照同步；
- 支持只读发布链接，默认关闭、可撤销；
- 旧应用无感迁移，不丢数据。

### 2.2 非目标

- 每个应用一个 SQLite 文件；
- 把 `app.html` 存进 DB；
- 公开链接可写；
- iframe/浏览器直连外部服务；
- CRDT / 多人同时编辑 HTML；
- 评论、修订审阅、Office 三件套在线编辑；
- 会员配额与计费。

---

## 3. 架构决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 状态与产物分离：DB 存 schema/rows，工作区只保留 `app.html` 与快照 | 远程后端不适合小步高频写 |
| D2 | 行数据表 `light_app_rows`，值为 JSON 字符串 | 动态列，保持 SQLite/PG 一致 |
| D3 | `data.csv` 降为导出/交换格式 | 保留用户熟悉的文件出口，但不再承担并发写 |
| D4 | 追加 = 单行 INSERT + 计数更新 | 去掉整文件读改写 |
| D5 | 行/结构修改使用版本号 CAS，冲突返回 409 | 多用户/多实例下的正确性 |
| D6 | 追加支持 `client_request_id` 幂等 | LLM 工具重试与网络重试会重复提交 |
| D7 | 旧应用懒迁移（`storage_version`） | 避免启动时扫描所有远程工作区 |
| D8 | 工具按实际操作者 + 应用 ACL 鉴权 | shared agent 的协作者应能按权限使用 |
| D9 | 发布链接只读、默认关闭、token 可撤销 | 传播能力与数据安全兼顾 |
| D10 | 外部数据先 CSV 导入，再 MCP 快照同步 | 先做确定性高、依赖少的能力 |
| D11 | 在线表格视图优先于 HTML 可视化编辑 | “能维护”比“能改样式”更刚需 |
| D12 | 实时刷新先用 `data_version` 轮询 | 正确性靠乐观锁，实时性后补 |

---

## 4. 数据模型

### 4.1 `light_apps` 新增列

| 字段 | 类型 | 说明 |
|------|------|------|
| `schema_json` | TEXT NOT NULL DEFAULT '' | 列定义权威副本 |
| `schema_version` | INTEGER NOT NULL DEFAULT 1 | 结构 CAS 版本 |
| `data_version` | INTEGER NOT NULL DEFAULT 0 | 任意行变更 +1 |
| `storage_version` | INTEGER NOT NULL DEFAULT 1 | 1=文件时代，2=DB |
| `data_bytes` | INTEGER NOT NULL DEFAULT 0 | 已有列，改为增量维护 |

### 4.2 `light_app_rows`

| 字段 | 说明 |
|------|------|
| `id` | 整数主键 |
| `row_id` | 对外字符串 ID（ULID） |
| `app_id` | `REFERENCES light_apps(app_id) ON DELETE CASCADE` |
| `values_json` | `{column: string}`，复用 `validate_row` 的规范化结果 |
| `version` | 行级乐观锁 |
| `source_id` / `source_key` | 外部数据来源与幂等键，可空 |
| `client_request_id` | 追加幂等键，可空 |
| `created_at` / `updated_at` | Unix 秒 |

约束：

- `UNIQUE(row_id)`
- 部分唯一索引 `(app_id, source_id, source_key) WHERE source_id IS NOT NULL`
- 部分唯一索引 `(app_id, client_request_id) WHERE client_request_id IS NOT NULL`
- 普通索引 `(app_id, id)`

### 4.3 `light_app_data_sources`

| 字段 | 说明 |
|------|------|
| `source_id` | 对外 ID |
| `app_id` | 所属应用 |
| `kind` | `csv_upload` / `mcp` |
| `name` | 展示名 |
| `credential_user_id` | 凭据归属用户，可空（用户删除后置空） |
| `server_name` / `tool_name` | MCP 服务器与工具 |
| `args_json` | 固定调用参数 |
| `mapping_json` | `result_path` / `key_path` / `column_map` / `max_rows` |
| `mode` | `snapshot` / `append` |
| `schedule_seconds` | 0 表示仅手动 |
| `next_run_at` / `lease_until` | 调度与多实例租约 |
| `last_sync_at` / `last_status` / `last_error` / `last_result_json` | 同步状态 |
| `created_by` / `created_at` / `updated_at` | 审计字段 |

### 4.4 `light_app_publications`

| 字段 | 说明 |
|------|------|
| `publication_id` | 对外 ID |
| `app_id` | 所属应用 |
| `token_hash` | SHA-256；明文 token 只在创建时返回一次 |
| `created_by` | 创建人，可空 |
| `created_at` / `expires_at` / `revoked_at` / `last_accessed_at` | 生命周期 |

### 4.5 DDL 示例（SQLite）

025 是未发布版本，规范做法是把新列直接写进 `CREATE TABLE light_apps`、新表直接追加到
025；对于已经记录为 v25 的本地库（开发机、已部署实例），由 `migrate.py` 的
`_ensure_column` / `CREATE TABLE IF NOT EXISTS` 幂等补齐。下面的 `ALTER` 片段对应后者。
新建应用直接写 `storage_version = 2` 与 `schema_json`，不经过迁移路径。

```sql
ALTER TABLE light_apps ADD COLUMN schema_json TEXT NOT NULL DEFAULT '';
ALTER TABLE light_apps ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE light_apps ADD COLUMN data_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE light_apps ADD COLUMN storage_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS light_app_rows (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  row_id            TEXT NOT NULL UNIQUE,
  app_id            TEXT NOT NULL REFERENCES light_apps(app_id) ON DELETE CASCADE,
  values_json       TEXT NOT NULL,
  version           INTEGER NOT NULL DEFAULT 1,
  source_id         TEXT,
  source_key        TEXT,
  client_request_id TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_light_app_rows_app
  ON light_app_rows(app_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_light_app_rows_source
  ON light_app_rows(app_id, source_id, source_key)
  WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_light_app_rows_request
  ON light_app_rows(app_id, client_request_id)
  WHERE client_request_id IS NOT NULL;
```

PostgreSQL 版本将 `INTEGER PRIMARY KEY AUTOINCREMENT` 换成
`INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY`，其余约束一致；两个文件都要
按仓库约定成对维护。

### 4.6 迁移编号

上游 `develop` 已占用 022–024（`skill_package_shared`、`user_groups`、
`user_group_profile`），本分支的 MiniApp 迁移因此整体重排为 **025–029**：

- `025_light_apps`：应用、协作者、多表、助手历史、数据源、团队看板；
- `026_goal_deliverables`、`027_external_events`、`028_light_app_rows`、
  `029_light_app_publications`。
- 同时维护 `.sql` / `.pg.sql` 成对文件；`migrate.py` 的
  `_ensure_light_apps_schema` / `_ensure_light_app_rows_schema` /
  `_ensure_light_app_publications_schema` 保持幂等，覆盖已记录旧编号的库。
- `_schema_version` 最终为 29，`tests/unit/db/test_db_pool.py` 的断言同步更新。
- 若库的 watermark 已高于上游 023，`_ensure_user_groups_schema` 会补建
  `user_group` / `user_group_member` / `resource_group_grant`，避免合并后缺表。

---

## 5. 读写与并发

### 5.1 读

```sql
SELECT row_id, values_json, version
FROM light_app_rows
WHERE app_id = ?
ORDER BY id
LIMIT ? OFFSET ?;
```

- 总数用 `light_apps.row_count`；定期或异常时用 `COUNT(*)` 校正。
- 列排序/过滤放 P1：SQLite 用 `json_extract(values_json, '$.col')`，
  Postgres 用 `(values_json::jsonb ->> 'col')`，列名必须来自 schema 校验。
- 单次读取仍限制 500 行（`MAX_ROW_READ`）。
- `_read_schema` / `_read_rows` 改为从 DB 读取，不再通过 `aread_text` 读
  `schema.json` / `data.csv`；`aread_text` 的 fail-soft `None` 分支只保留给 `app.html`。

### 5.2 追加

同一事务内：

1. `UPDATE light_apps`
   `SET row_count = row_count + 1, data_version = data_version + 1,`
   `data_bytes = data_bytes + ?, updated_at = ?`
   `WHERE app_id = ? AND data_bytes + ? <= MAX_CSV_BYTES`
2. 受影响行数为 0 → 行数/容量超限，返回错误；
3. `INSERT INTO light_app_rows(...)`。

`client_request_id` 冲突时返回已有行，并标记 `duplicate: true`。

### 5.3 修改/删除行

```sql
UPDATE light_app_rows
SET values_json = ?, version = version + 1, updated_at = ?
WHERE app_id = ? AND row_id = ? AND version = ?;
```

- 受影响 0 行：先查存在性，存在 → 409 `APP_CONFLICT`，不存在 → 404。
- 删除同理，并在同一事务内减 `row_count` / `data_bytes`；更新时按新旧 JSON 的字节差
  调整 `data_bytes`。

### 5.4 修改结构

```sql
UPDATE light_apps
SET schema_json = ?, schema_version = schema_version + 1, data_version = data_version + 1
WHERE app_id = ? AND schema_version = ?;
```

- 受影响 0 行 → 409。
- 新增列不重写历史行；读取时缺失字段按空字符串处理。

### 5.5 删除文件级事务

行数据入库后，以下机制不再需要，应一并删除：

- `_APP_LOCKS` / `_lock_for` / `_forget_lock`；
- `_write_atomic` / `_recover` / `_pending_files` / `_promote` / `_clear_journal`；
- `.pending.json` 与 `.new` 暂存后缀。

`app.html` 仍是单文件写入，保留 `awrite_text` 即可。

---

## 6. 懒迁移

首次读取或写入 `storage_version = 1` 的应用时：

1. 从工作区读取 `schema.json` 与 `data.csv`（只读一次）；
2. 用 `parse_schema_payload` / `parse_csv` / `validate_row` 校验；
3. 批量插入 `light_app_rows`；
4. 提交标记：

```sql
UPDATE light_apps
SET schema_json = ?, schema_version = ?, data_version = 1, storage_version = 2
WHERE app_id = ? AND storage_version = 1;
```

- 受影响 0 行说明其他进程已完成，重新从 DB 读取即可。
- `aread_text` 返回 `None` 视为后端暂时不可用（远程存储的 fail-soft 语义），保持
  `storage_version = 1`；文件缺失或校验失败同样保持 v1，下次重试，不删原文件。
- 迁移完成后 `data.csv` 视为历史快照，不再自动更新。

**迁移不可回滚。** 上线前须备份控制面 DB 与 Agent 工作区；旧代码在迁移后读到的是
迁移前的 CSV。

可选：提供管理员批量迁移入口（CLI 或 admin API），避免逐个打开应用。默认不做启动时
全量扫描，远程工作区下代价不可控。

---

## 7. 工作区产物

| 文件 | 新定位 |
|------|--------|
| `app.html` | 保留，Agent 唯一必写产物 |
| `schema.json` | 只读镜像，创建/改结构时 best-effort 写一次 |
| `manifest.json` | 只读镜像，标注 `data_storage: "control-plane-db"` |
| `preview.svg` | Agent 画的封面（`app_set_preview`），工作台「全部小程序」列表和模板画廊当缩略图展示 |
| `data.csv` | 不再随 append 重写；由导出接口或 `app_export_csv` 按需生成 |

配套修改：

- `_HTML_HELP` 与 light-apps 专家提示词：读数据用 `app_read_data`，全量分析用
  `app_export_csv`，不要直接读 `data.csv`；
- 卡片的“数据大小”继续展示 `data_bytes`，链接到导出接口；
- `app_write_html` 只更新 `html_bytes` 与 `updated_at`，不再读 schema 文件。

---

## 8. 权限与身份

| 动作 | 校验 |
|------|------|
| `app_list` / `app_read_data` | 对 app 有 `view` 及以上 |
| `app_append_row` / `app_update_row` / `app_delete_row` | `edit_data` 及以上 |
| `app_write_html` / `app_update_schema` | `edit_structure`（owner/admin） |
| `app_create` | 可访问该 Agent（owner / shared / admin），新应用 owner=操作者 |
| `app_delete` | `edit_structure` + HITL |

实现要点：

- `_get_owned` 改为 `_get_for_actor`，使用 `configurable.user`（HITL 恢复走
  `tool_scope`）作为实际操作者；
- 结构调整只允许 owner/admin，协作者只能改数据；
- Agent owner 不会因为拥有 Agent 就自动获得他人创建应用的元数据权限；
- 数据源创建限制为 app owner/admin，`credential_user_id` 记录实际凭据归属。

已知边界：shared agent 的 owner 仍可通过工作区工具看到其他用户创建的应用文件；
应用 ACL 保护的是元数据与 HTTP/工具入口。该边界需在文档中说明，不在本方案内解决。

---

## 9. 发布

### 9.1 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/apps/{id}/publication` | 当前发布状态 |
| POST | `/api/apps/{id}/publication` | 创建，返回一次性 token 与 URL |
| DELETE | `/api/apps/{id}/publication` | 撤销 |
| GET | `/p/{token}` | 公开宿主页（服务端渲染） |
| GET | `/api/public/apps/{token}` | 应用元数据 + HTML |
| GET | `/api/public/apps/{token}/rows` | 只读分页数据 |

### 9.2 规则

- 只读，不支持公开写入；
- 默认关闭，用户显式开启；
- token 32 字节随机值，存 SHA-256，明文只返回一次；
- `revoked_at` 非空或 `expires_at` 过期 → 404；
- `/api/public/apps/` 加入 `api/deps.py` 的 `_JWT_EXEMPT_PREFIXES`，
  沿用 `/api/downloads/link/` 的“不可猜 token 即凭证”模式；
- 公开路由必须注册在 SPA catch-all 之前（`api/app.py` 的 `spa_fallback` 最后注册）；
- 响应 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`，按 token/IP 限流
  （进程内令牌桶；多实例下为每实例限流）；
- 公开宿主页同样使用 sandbox iframe 与 `APP_CSP`，不注入 JWT；
- 发布前提示“页面及其引用的数据会对外可见”，并提供撤销入口。

---

## 10. 外部数据

### 10.1 第一层：CSV 导入（P0）

- `POST /api/apps/{app_id}/import`，`Content-Type: text/csv`；
- 复用 `parse_csv` + `validate_row`；
- 支持 `mode=append|replace`，返回 `{imported, skipped, errors}`；
- 受 20k 行、5MB、50 列、单元格 10k 字符限制。

### 10.2 第二层：MCP 快照同步（P1）

**创建流程**

1. owner 在应用详情页选择连接器实例；
2. `GET /api/apps/{id}/sources/discover?server_name=…` 返回工具列表；
3. 选择工具，填写固定参数与字段映射；
4. 保存为 `light_app_data_sources`。

**工具发现与调用**

```python
configs = await connector_service.mcp_configs_for_user(credential_user_id)
tools = await aload_mcp_tools({server_name: configs[server_name]})
tool = next(t for t in tools if t.name == tool_name)
result = await asyncio.wait_for(tool.ainvoke(args), timeout=60)
```

- 只加载所选服务器，不加载用户全部 MCP；
- 凭据由 `ConnectorService` 负责刷新与解密，不进入日志、不返回前端；
- 结果先转文本，再尝试 JSON 解析；无法解析记为同步失败。

**映射**

```json
{
  "result_path": "data.items",
  "key_path": "id",
  "column_map": {"title": "name", "status": "status", "amount": "amount"},
  "max_rows": 5000
}
```

- 未配置 `column_map` 时按列名同名匹配；
- 每条记录经 `validate_row`，非法行跳过并计数；
- `mode=snapshot`：按 `source_id` 删除旧行后插入，事务内把 `row_count` 重置为精确值；
- `mode=append`：按 `(source_id, source_key)` 去重，要求配置 `key_path`；
- 单次同步受 20k 行与 5MB 限制，超出截断并记录 `truncated`。

**只读保障**

- 只调用用户显式选中的单个工具；
- 工具带 `readOnlyHint` 时必须为 true；无注解时 UI 展示警告；
- 不做自动发现后批量调用；
- 永远不写回外部系统。

**定时同步（P2）**

- `schedule_seconds > 0` 的数据源写入 `next_run_at`；
- `OctopServer` 启动一个 60 秒循环，用租约领取到期任务：

```sql
UPDATE light_app_data_sources
SET lease_until = ?, next_run_at = ?
WHERE source_id = ?
  AND next_run_at <= ?
  AND (lease_until IS NULL OR lease_until < ?);
```

- 受影响 1 行才执行，保证多实例只有一个执行者；
- 失败写 `last_status` / `last_error`，按退避重试，不影响应用读取。

---

## 11. API / SDK / 工具

### 11.1 API

| 方法 | 路径 | 权限 |
|------|------|------|
| GET | `/api/apps/{id}/rows?limit&offset&sort&order` | view |
| POST | `/api/apps/{id}/rows` | edit_data |
| PATCH | `/api/apps/{id}/rows/{row_id}` | edit_data |
| DELETE | `/api/apps/{id}/rows/{row_id}` | edit_data |
| POST | `/api/apps/{id}/import` | edit_data |
| GET | `/api/apps/{id}/export.csv` | view |
| GET/POST | `/api/apps/{id}/sources` | edit_structure |
| POST | `/api/apps/{id}/sources/{sid}/sync` | edit_structure |
| GET/POST/DELETE | `/api/apps/{id}/publication` | edit_structure |
| GET | `/api/public/apps/{token}`、`/rows` | 公开只读 |

路由拆分：`api/routers/apps.py` 保留元数据与行，新增
`api/routers/app_sources.py`、`api/routers/app_public.py`。

### 11.2 SDK

`dashboard/src/pages/Apps/Detail.tsx` 的 `APP_SDK` 增加：

```js
getRow(rowId)
updateRow(rowId, patch, version)
deleteRow(rowId, version)
onDataChange(cb)   // 宿主轮询到 data_version 变化时触发
```

`appendRow` 增加可选 `requestId`。所有调用仍走 postMessage，iframe 不接触 JWT。

### 11.3 Agent 工具

| 工具 | 阶段 | 说明 |
|------|------|------|
| `app_update_row` | P1 | 按 row_id + version 更新 |
| `app_delete_row` | P1 | 按 row_id + version 删除 |
| `app_import_rows` | P1 | 批量导入 |
| `app_export_csv` | P1 | 生成快照到工作区，供 Agent 全量分析 |
| `app_sync_source` | P2 | 触发一次数据源同步 |

`app_read_data` 保持 500 行上限；全量分析走导出。

---

## 12. 前端

### 12.1 应用详情页

新增页签/面板：

- **数据**：只读表格（P0）→ 可编辑表格（P1，行内编辑、删除、新增、排序、筛选）；
- **数据源**：列表、创建、手动同步、状态与错误（P1）；
- **发布**：开关、链接复制、撤销、过期时间（P2）；
- **信息**：现有 Descriptions 保留。

新增组件建议：

```
dashboard/src/pages/Apps/components/
  AppDataTable.tsx
  AppSourcesPanel.tsx
  AppPublishPanel.tsx
```

### 12.2 冲突与刷新

- 写请求带 `version`；409 时提示“数据已被他人修改”，刷新后重试；
- 详情页每 5 秒轮询 `data_version`，变化时刷新表格并向 iframe 发送
  `octop-app-host: data-changed`；
- SDK 新增 `onDataChange`，新生成的 HTML 用它做局部刷新；
- 不做 CRDT、不做实时光标。

### 12.3 HTML 定点修改（P2）

- 详情页提供“让 Agent 修改”入口，把 `app_id`、当前 HTML 选区、用户指令预填进
  Agent 对话；
- Agent 仍通过 `app_write_html` 完成整页写入；
- 真正的可视化编辑器（选中元素、原位修改、多人同时编辑）是独立前端项目，本方案不做。

---

## 13. 分期与工作量

| 阶段 | 内容 | 人日 | 验证 |
|------|------|------|------|
| P0-1 | 025 表结构 + repo + migrate 助手 | 3–4 | 建库/升级/表存在性测试 |
| P0-2 | service 读写切 DB、懒迁移、并发、限额、幂等 | 5–7 | 旧应用迁移后行数与 CSV 一致；并发 append 都落库；stale update 409 |
| P0-3 | 工具/接口按实际操作者鉴权 | 2–3 | shared agent 协作者按 ACL 读写；结构变更仍 owner-only |
| P0-4 | 只读数据表 + CSV 导出 | 3 | 列表不再扫描工作区目录；导出与 DB 一致 |
| P1-1 | 可编辑表格 + CSV 导入 | 4–5 | 增删改/导入回环；冲突提示 |
| P1-2 | MCP 数据源手动同步 | 4–6 | 假 MCP 返回 100 行；映射/跳过/错误统计正确 |
| P2-1 | 发布链接 + 公开宿主页 | 3–4 | 无痕窗口可打开；撤销后 404；公开页无 JWT |
| P2-2 | 定时同步 + 租约 | 2–3 | 到期触发；多实例只执行一次 |
| P2-3 | HTML 定点修改（对话预填） | 2–3 | 选区上下文进入对话 |
| P2-4 | `data_version` 轮询刷新 | 1–2 | 两个浏览器 5 秒内看到新行 |
| | **合计** | **29–40** | |

MVP = P0（13–17 人日）。若优先传播能力，可将 P2-1 提前到 P1 末尾。

---

## 14. 测试与验证

### 14.1 后端

- `tests/unit/db/`：025 迁移、表/索引存在性、repo CRUD、部分唯一索引冲突；
- `tests/unit/apps/`：schema/row 校验、并发追加、幂等追加、CAS 冲突、限额；
- `tests/unit/apps/test_migration.py`：旧文件 → DB，重复调用不重复插入；
- `tests/integration/test_apps_api.py`：行 CRUD、导入/导出、ACL、发布/撤销、
  公开接口无 JWT；
- MCP 同步：monkeypatch `aload_mcp_tools`，覆盖映射、非法行、超时、只读校验。

### 14.2 前端

- `cd dashboard && npx tsc -b`；
- 表格编辑、409 提示、发布开关的组件测试；
- `npm run lint` / Prettier。

### 14.3 总体验证

```bash
uv run pytest tests/unit/apps tests/unit/db -q
uv run pytest tests/integration/test_apps_api.py -q
make all
cd dashboard && npx tsc -b
```

### 14.4 i18n 与文档

- 新增错误码至少包括 `APP_CONFLICT`、`APP_ROW_NOT_FOUND`、`APP_SOURCE_INVALID`、
  `APP_SOURCE_SYNC_FAILED`、`APP_PUBLISH_DISABLED`，同步
  `src/octop/i18n/{en,zh}.json` 的 `errors.*` 与 dashboard `apiErrors.*`；
- 新 UI 文案同步 `dashboard/src/locales/{en,zh}.json` 的 `apps.*`；
- 新路由补 `summary` / `description` / `response_model`，检查 `/api/docs` 可读性；
- 更新 `docs/api.md` 中与 MiniApp 相关的接口说明。

---

## 15. 风险与取舍

| 风险 | 影响 | 缓解 |
|------|------|------|
| SQLite 多 worker 写竞争 | `database is locked` | 事务保持单条 INSERT；SQLite 建议单 worker，多 worker 用 PostgreSQL |
| 懒迁移耗时 | 首次打开慢 | 一次性读取；失败保留 v1 重试；提供管理员批量迁移 |
| 迁移不可回滚 | 旧代码读到旧 CSV | 上线前备份 DB + 工作区；先在测试实例演练 |
| MCP 输出结构漂移 | 同步失败 | 映射配置 + `last_error`；永不阻塞应用读取 |
| 公开链接泄露 | 数据外泄 | 默认关闭、随机 token、可撤销、限流、发布前提示 |
| 共享 Agent 工作区可见 | Agent owner 可读他人应用文件 | 记录为已知边界；敏感场景用独立 Agent |
| `data.csv` 过期 | 用户误读旧数据 | 标注“快照”；导出接口按需生成；提示词改走工具 |

---

## 16. WorkBuddy 借鉴对照

| WorkBuddy 能力 | 本方案落点 | 阶段 |
|----------------|------------|------|
| CSV 是云端在线表格，HTML 按表格 ID 读取 | 行数据入库，`data_version` 驱动刷新 | P0 |
| 发布为链接、默认关闭、可撤销 | `light_app_publications` + `/p/{token}` | P2 |
| Agent 与当前用户同权限 | `_get_for_actor` + 应用 ACL | P0 |
| 在线表格维护数据 | 可编辑数据表 + CSV 导入导出 | P1 |
| 选中区块让 AI 改 | 对话预填（selection → chat） | P2 |
| 多端同步 | `data_version` 轮询 | P2 |
| 评论、修订、协同编辑 | 不做 | — |

WorkBuddy 明确 MiniApp 不适合复杂业务逻辑、高并发写入、严格权限控制；Octop 作为自托管
平台，反而应在权限、审计、数据主权上做强，这是差异化方向。

---

## 17. 关键文件索引

| 关注点 | 位置 |
|--------|------|
| 当前应用生命周期 | `src/octop/infra/apps/service.py` |
| Schema/行校验、CSV 序列化 | `src/octop/infra/apps/models.py` |
| Agent 工具 | `src/octop/infra/apps/tools.py` |
| 元数据/ACL repo | `src/octop/infra/db/repos/apps.py` |
| 迁移 | `src/octop/infra/db/migrations/025_light_apps*.sql`、`src/octop/infra/db/migrate.py` |
| HTTP 路由 | `src/octop/api/routers/apps.py` |
| JWT 豁免 | `src/octop/api/deps.py` |
| 连接器/MCP | `src/octop/infra/connectors/service.py`、`octop_harness/mcp.py` |
| Dashboard 列表/详情 | `dashboard/src/pages/Apps/index.tsx`、`Detail.tsx`、`appCsp.ts` |
| API 客户端 | `dashboard/src/api/modules/apps.ts` |
