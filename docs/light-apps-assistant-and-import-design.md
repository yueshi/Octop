# 轻应用：默认 AI 助手 + 外部数据导入设计

> 状态：已实施 —— M1 多表、M2 助手、M3 手动导入、M4 外部数据源、M5 自动同步全部落地（2026-10-24；遗留：`append` 模式无去重键、助手无应用内 SDK 入口）
>
> 范围：`feature/light-apps` 分支上的轻应用能力
>
> 相关：[轻应用多 CSV（多数据表）设计](./light-apps-multi-table-design.md)、
> [轻应用数据与协作架构方案](./light-apps-architecture.md)

---

## 1. 两个需求

| # | 需求 | 一句话 |
|---|------|--------|
| 1 | 轻应用默认支持 AI 助手 | 应用创建后自带一个能读本应用数据、能回答问题的对话入口；用户不必自己接模型 |
| 2 | 支持从外部文件或其他数据源导入数据 | 表的数据可以来自上传的 CSV/Excel、Agent 工作区里的文件，或外部数据源；既可手动导入，也可自动同步 |

需求 1 让「应用自带智能」；需求 2 让「应用有真数据」。两者都建立在多表模型之上：
助手读表，导入写表。

---

## 2. 需求 1：默认 AI 助手

### 2.1 形态选择

「默认支持 AI 助手」有三种可能的落地形态：

| 形态 | 描述 | 判断 |
|------|------|------|
| A. 应用内嵌对话 | 应用 HTML 自己画一个聊天框，调 `octopApp.ask()` | 需要应用作者写代码，不算「默认」 |
| B. Dashboard 内置助手面板 | 打开应用详情页时，右侧自带一个助手面板，用户直接提问 | **默认支持**的正解：与应用作者无关，装上就有 |
| C. 每次数据变更自动摘要 | 写入数据后自动生成说明 | 是附加能力，不是助手 |

**决定：做 B，并把 A 作为可选的编程入口。**

- B 保证「默认」：任何轻应用（包括 Agent 随手生成的最简页面）打开就有助手；
- A 让应用作者能把助手嵌进自己的界面（例如把「问助手」做成页面内的一个按钮）。

### 2.2 助手的身份与能力边界

助手**不是**一个新的 Agent 实例，而是对**已有 Agent 的一次受限调用**：

| 维度 | 决定 | 理由 |
|------|------|------|
| 用哪个 Agent | 应用所属的 Agent（`light_apps.agent_id`） | 用户已经在跟这个 Agent 打交道，不引入新身份 |
| 用哪个模型 | 该 Agent 的默认模型（`agent.config.pick_default_model_ref()`） | 与 Agent 在其它场景表现一致 |
| 能不能调工具 | **只能调本应用的只读数据工具** | 助手是「这个应用的数据问答」，不是「让 Agent 随便干活」 |
| 能不能写数据 | **不能** | 写操作交给显式的 `app_append_row` 工具，由用户或 Agent 主动发起 |
| 历史 | 按「应用 + 用户」维度保存在服务端 | 同一应用内多轮对话连续；不同应用互不可见 |
| 越权 | 用户对应用没有 `view` 权限 → 不显示助手 | 与应用本身的 ACL 完全一致 |

**只读工具集**（新增 `app_ask` 工具的配套工具，随助手会话注入）：

| 工具 | 作用 |
|------|------|
| `app_tables` | 列出本应用的表与列定义 |
| `app_query_rows(table, filters?, limit?, offset?)` | 按表读行，可带简单过滤 |

助手只能看到**该用户本来就有权限读的表**——即 `light_apps` + `light_app_collaborators` 授权范围内的应用。

### 2.3 交互链路

```
Dashboard 应用详情页
   │  助手面板输入「这个月哪类支出最高？」
   ▼
POST /api/apps/{app_id}/assistant   （SSE 流式返回）
   │  校验 JWT + 应用 view 权限
   │  prepare()：解析所属 Agent + 默认模型（未运行/无模型 → 普通 HTTP 错误）
   │  组装：系统提示（应用名 + 表结构摘要）+ 会话历史 + 用户问题
   │  注入 app_tables / app_query_rows 两个只读工具（bind_tools）
   ▼
bind_tools 后的 LLM 循环（infra/apps/assistant.py，最多 4 轮）
   │  模型可能先调 app_query_rows 拉数据再回答
   ▼
SSE token 流 → Dashboard 助手面板显示回答

> 实施记录（2026-10-24）：未走完整 harness graph，而是解析所属 Agent 的
> 默认模型后直接跑一个受限 bind_tools 循环（`AppAssistant`）。工具只读、
> 只绑本应用，安全边界一致；代价是不记录轨迹，用量改为循环内自行按
> `usage_metadata` 累计并写入 `usage_repo`（source=app_assistant）。

### 2.4 会话与历史

| 项 | 决定 |
|----|------|
| 存储 | 专用表 `light_app_assistant_messages`（v25 迁移，随多表 DDL 一起落地），按 `(app_id, user_id)` 隔离；**未**复用线程表 |
| 清理 | repo 插入时裁剪到最近 200 条；应用删除时按 `app_id` 清空 |
| 上下文裁剪 | 每次提问回放最近 12 条历史消息作为上下文 |

### 2.5 SDK 入口（可选，供应用作者内嵌）— 未实施

```js
// 应用 HTML 内自行渲染聊天框时可用
const answer = await octopApp.ask("帮我看看这周的趋势");
```

- 与 Dashboard 助手面板**共用**同一个后端端点与会话；
- iframe 仍不持有 JWT（沿用现有 postMessage 桥：iframe → 父页面 → HTTP → 后端）；
- CSP 不变（`connect-src 'none'`），iframe 只能通过 postMessage 走父页面。

### 2.6 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/apps/{app_id}/assistant` | SSE 流式问答；body `{question, thread_id?}` |
| GET | `/api/apps/{app_id}/assistant/history` | 取该用户在此应用的会话历史（用于面板重开时回填） |

SSE 沿用 `api/routers/chat/sse.py` 的 `format_sse`，事件名统一为 `chunk`，
事件类型放在 data 载荷的 `type` 字段：`token` / `tool` / `done` / `error`
（与 chat 流的 dashboard 解析约定一致）。

### 2.7 失败与降级

| 情况 | 行为 |
|------|------|
| 应用所属 Agent 未运行 | `prepare()` 抛出 Agent 可用性错误（如 `AGENT_NOT_RUNNING`），POST 返回普通 HTTP 错误，面板显示后端本地化消息 |
| 没有可用模型 | 同上（`INTERNAL_ERROR`），后续可再隐藏面板入口 |
| 模型调用失败 | SSE `error` 事件（本地化文案），面板显示可重试提示 |
| 工具轮数超限 | 最多 4 轮，超出发 `error` 事件提示换更具体的问法 |
| 表数据为空 | 助手正常回答「这张表还没有数据」，不报错 |

### 2.8 用量与成本

助手调用消耗 Agent 的模型额度，因此：

- 助手调用计入 Agent 的用量统计：循环内按 `usage_metadata` 累计，
  回答完成后写 `usage_repo.record(..., source="app_assistant")`；
- 面板上标注「消耗 Agent 额度」；
- 单次会话最大轮数与超时沿用 Agent 既有上限，不另设。

---

## 3. 需求 2：外部数据导入

### 3.1 数据源类型

| 类型 | 标识 | 来源 | 落地方式 |
|------|------|------|------|
| 上传文件 | `upload` | Dashboard 上传 CSV / Excel | ✅ 已实施：`POST /api/apps/{app_id}/upload` 经 `BackendWorkspace` 落到 `inbound/imports/`（不需要 Agent 在运行，沿用 `max_upload_bytes` 上限） |
| 工作区文件 | `workspace_file` | Agent 工作区里已有的 CSV / Excel | ✅ 已实施：按工作区相对路径读取；拒绝任何含隐藏段（`.` 开头，含 `.octop`）的路径，且要求 edit_structure |
| 外部数据源 | `external` | MCP 连接器暴露的只读查询工具 | ✅ 已实施（2026-10-24）：仅支持进程内 gateway 连接器（`mcp_mode=gateway`）；`GatewayToolRunner` 绑定时与运行时双重校验 `readOnlyHint is not False`，凭据经 `ConnectorService.decrypt` 解密后调 `call_gateway_tool`，输出按 `result_path` 点路径提取记录 |

三者统一落到**多表模型的一张表**：导入 = 往某张表灌数据。

### 3.2 手动 vs 自动

| 模式 | 触发 | 语义 |
|------|------|------|
| 手动（manual） | 用户点「导入」/ Agent 调工具 | 一次性导入，追加或替换 |
| 自动（sync） | 绑定周期，到点自动跑 | 周期性重新拉取，替换该表内容 |

自动模式需要一张**数据源绑定表**，记录「哪张表 ← 哪个源 ← 什么参数」。

### 3.3 表单结构（控制面）

新表 `light_app_sources`（✅ 已折入 v25 迁移；每张表至多一个绑定，DB 层 `UNIQUE(app_id, table_key)` 兜底）：

```
id, source_id(公开字符串), app_id → light_apps(app_id),
table_key,                            -- 目标表（UNIQUE(app_id, table_key)，每表一个绑定）
kind(workspace_file|external),        -- upload 是一次性动作，不建绑定
mode(append|replace),                 -- 导入语义
schedule_seconds(0=仅手动),
params_json,                          -- workspace_file: {path}; external: {instance_id, tool, args?, result_path?}
mapping_json,                         -- 源列 → 表列 的映射
last_run_at, last_status, last_error, last_row_count,
created_by, created_at, updated_at
```

### 3.4 导入流程

```
上传/指定文件 或 调用外部工具
   ▼
读出原始行（list[dict]）
   ▼
按 mapping_json 映射到目标表的列（未映射的列忽略，缺列填空）
   ▼
用目标表的列定义做类型校验（复用现有 validate_row）
   ▼
mode=append → 追加到该表；mode=replace → 覆盖该表
   ▼
写该表的 CSV + 更新行数缓存
```

校验失败的行**跳过并计数**，不阻断整批导入；结果回给调用方：
`{imported, skipped, errors}`（最多报 5 条错误明细）。

实施记录（2026-10-24）：
- 列映射默认按同名列自动映射；`mapping` 显式指定「源列 → 表列」，未知目标列报 `APP_SCHEMA_INVALID`；
- 权限：`append` 要 edit_data；`replace` 与 `workspace_file` 源要 edit_structure（所有者/管理员）；
- `replace` 时若**所有**行都校验失败则报错不动表，避免坏映射把表清空；
- 行数（20k）与 CSV 体积（5MB）上限沿用现有 `MAX_ROWS` / `MAX_CSV_BYTES`；
- 解析支持 CSV/TSV/JSON/.xlsx/.xls，文本解码 utf-8-sig → gb18030 回退（`infra/apps/importer.py`）。

### 3.5 文件格式支持

| 格式 | 解析 | 备注 |
|------|------|------|
| CSV / TSV | `csv` 模块 | 表头=列名；编码沿用知识库的 `utf-8-sig` → `gb18030` 回退 |
| Excel (.xlsx) | `openpyxl` | 取第一个工作表；`.xls` 用 `xlrd` |
| JSON 数组 | `json` | 对象数组，键=列名 |

依赖 `openpyxl` / `xlrd` **已在 `pyproject.toml` 里**（知识库解析在用），不新增依赖。

### 3.6 外部数据源（MCP）

| 方面 | 决定 |
|------|------|
| 连接器复用 | 复用 `infra/connectors` 的现有凭据与 MCP 装配（`build_mcp_server_configs_for_user` / `registry.call_gateway_tool`） |
| 只读保证 | 只调用用户显式选定的那一个工具；工具带 `readOnlyHint` 时必须为 true；无注解时 UI 提示风险 |
| 凭据归属 | 用**应用所有者**的连接器凭据执行（绑定时记录 `credential_user_id`） |
| 结果形状 | 工具返回的 JSON → 按 `params_json.result_path` 取数组（如 `data.items`），每项按 `mapping_json` 映射 |
| 失败 | 记录 `last_status=error` + `last_error`，不影响应用读取 |

### 3.7 自动同步

| 项 | 决定 |
|----|------|
| 触发 | 服务端一个周期性任务扫描 `schedule_seconds > 0` 且到期的绑定 |
| 多实例 | 领取用 UPDATE 条件更新（`next_run_at` + 租约），同一绑定同一时刻只有一个实例执行 |
| 幂等 | `replace` 模式天然幂等；`append` 模式按 `source_key` 去重键避免重复灌入 |
| 失败退避 | 失败后 `next_run_at` 指数退避，最大间隔封顶 |
| 影响面 | 同步只改目标表，不动其他表 |

实施记录（2026-10-24，M5 已落地）：
- 调度器 `infra/apps/scheduler.py`：`AppSourceScheduler` 单协程循环（30s 扫描），`start()` / `shutdown()` / `tick(now)`（tick 公开供测试）；由 `infra/server.py` 在启动时构建并拉起，`stop()` 关停，向导热切换 DB 经 `replace_persistence` 重绑。
- 列：`next_run_at`（NULL=到期即跑）+ `fail_count` 折入未发布的 v25 迁移；`update_source` 重置两者。
- 领取即租约：`claim_source` 是条件 UPDATE（`COALESCE(next_run_at,0) <= now` 才置为 `now + schedule_seconds`），无独立租约列；进程崩溃最多丢一个周期。
- 退避：`run_source` 统一记账（手动/自动同路）——成功 `next_run_at = run_at + schedule_seconds`、`fail_count = 0`；失败 `next_run_at = run_at + min(schedule · 2^(fail_count-1), 24h)`。
- `append` 去重（`source_key`）未做——与手动导入行为一致（append 就是重复灌入），需要幂等请用 replace 模式。
- Dashboard：info 抽屉每张表显示绑定状态（kind/路径或工具、周期、上次同步、行数、成功/失败 Tag），绑定对话框（工作区文件路径 或 连接器实例+只读工具+参数 JSON+result_path+列映射 JSON），立即同步 / 删除绑定；周期预设 5min/30min/1h/6h/1d。

### 3.8 Agent 工具

| 工具 | 作用 |
|------|------|
| `app_import_rows(app_id, path, table?, mode?, mapping?)` | ✅ 已实施：读工作区文件（CSV/TSV/JSON/Excel）灌进指定表 |
| `app_bind_source(app_id, table, kind, params, mode, schedule_seconds?)` | 未实施（绑定走 HTTP API；M5 加自动同步时再评估） |

「删除源」暂不提供（与删表一样，涉及破坏性，先留 HTTP 侧或后续再加）。

### 3.9 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/apps/{app_id}/tables/{key}/import` | ✅ 已实施：手动导入（body：`{kind, path, mode, mapping?}`） |
| POST | `/api/apps/{app_id}/upload` | ✅ 已实施：上传待导入文件（multipart，落到 `inbound/imports/`，返回 path） |
| GET | `/api/apps/{app_id}/sources` | ✅ 已实施（view 权限） |
| PUT | `/api/apps/{app_id}/sources/{source_id}` | ✅ 已实施（edit_structure；每表一个绑定；external 绑定时校验连接器与只读工具） |
| DELETE | `/api/apps/{app_id}/sources/{source_id}` | ✅ 已实施（edit_structure，204） |
| POST | `/api/apps/{app_id}/sources/{source_id}/run` | ✅ 已实施（edit_data；失败记入 last_status/last_error 后抛错） |
| GET | `/api/apps/{app_id}/source-tools?instance_id=` | ✅ 已实施（edit_structure；binding 对话框的工具清单，含 read_only 标记） |

### 3.10 Dashboard

| 功能 | 说明 |
|------|------|
| 应用详情页「数据」区 | ✅ 每张表显示行数；有 edit_data 权限时显示「导入」按钮 |
| 导入对话框 | ✅ 已实施（上传文件 → 选目标表 → 追加/覆盖 → 结果提示）；映射预览未做（同名列自动映射，`mapping` 参数留给 API 调用方） |
| 工作区文件 / 外部数据源导入 | 工作区文件 ✅（HTTP 导入 + `app_import_rows` 工具）；外部源经 API 绑定/运行 ✅ |
| 外部源绑定对话框 | ✅ 已实施（M5）：info 抽屉每表数据源状态行 + 绑定对话框 + 立即同步/删除 |

---

## 4. 三个需求的合并视图

```
                     ┌──────────────── 轻应用 ────────────────┐
                     │  app.html（沙箱 iframe）                │
   需求 1            │    ↕ window.octopApp（postMessage 桥）    │
   助手        ←──→  │  Dashboard 详情页：助手面板 + 数据区       │
                     └───────────────┬────────────────────────┘
                                     │  服务层：按表读写
                     ┌───────────────▼────────────────────────┐
   需求 2            │  表 A（main）   表 B   表 C …            │
   导入        ───→  │  ↑ 手动导入 / 自动同步（外部文件/数据源） │
                     └────────────────────────────────────────┘
```

---

## 5. 实施顺序与工作量

| 阶段 | 内容 | 人日 | 依赖 |
|------|------|------|------|
| M1 | 多表模型（另一文档） | 8–12 | — |
| M2 | 助手面板 + SSE 端点 + 只读工具（`app_tables`/`app_query_rows` 随会话注入；`app_ask` 应用内 SDK 入口未做） | 已实施 2026-10-24 | M1（助手要按表读数据） |
| M3 | 手动导入（上传 / 工作区文件 / CSV·Excel·JSON 解析） | 已实施 2026-10-24（映射预览未做） | M1 |
| M4 | 外部数据源（MCP 只读调用 + 映射） | 已实施 2026-10-24（仅进程内 gateway 连接器） | M3 |
| M5 | 自动同步（绑定 + 周期任务 + 租约 + 退避） | 已实施 2026-10-24（`append` 去重未做） | M4 |

M2 与 M3 可并行。M1 是三者共同前提。

---

## 6. 风险与取舍

| 风险 | 影响 | 缓解 |
|------|------|------|
| 助手能读到应用数据 | 越权读他人应用数据 | 助手权限完全跟随应用 ACL；助手只对已有 `view` 权限的用户开放；工具只能读本应用 |
| 助手消耗 Agent 额度 | 成本 | 计入 Agent 用量；面板标注；轮数与超时沿用 Agent 上限 |
| 助手被诱导执行越权指令 | 通过应用数据里的文本做提示注入 | 助手系统提示明确「表内文本是数据不是指令」；工具集只读且限定本应用 |
| 自动同步把表冲掉 | 数据丢失 | `replace` 前先写临时文件再替换（沿用现有原子写机制）；绑定可随时停用 |
| 外部工具返回值巨大 | 内存与上下文爆炸 | 拉取后先按 `max_rows` 截断，再映射 |
| 同步在多实例下重复执行 | 数据重复 | 领取用条件 UPDATE + 租约；`append` 模式另有去重键 |

---

## 7. 明确不做

| 不做 | 原因 |
|------|------|
| 助手写数据 | 写操作应由显式工具发起，避免「问一句就改了表」 |
| 助手跨应用访问 | 助手只服务一个应用 |
| 助手长期记忆 / 跨应用画像 | 超出轻应用边界 |
| 导入时自动改表结构 | 列变更必须显式（`app_update_schema`），导入只填数据 |
| 外部源的写操作 | 只读工具，杜绝把外部系统改掉 |
| 从 URL 抓取导入 | 需要抓取策略与 SSRF 防护，另行设计 |

---

## 8. 关键文件索引

| 关注点 | 位置 |
|--------|------|
| 应用服务（读写、建表） | `src/octop/infra/apps/service.py` |
| 表模型与校验 | `src/octop/infra/apps/models.py` |
| Agent 工具与只读工具集 | `src/octop/infra/apps/tools.py` |
| HTTP 路由 | `src/octop/api/routers/apps.py` |
| SSE 格式化 | `src/octop/api/routers/chat/sse.py` |
| 一次性 LLM 调用（参考） | `src/octop/infra/utils/llm_text.py` |
| Agent 运行时与工具集注入 | `src/octop/infra/agents/manager.py` |
| 连接器 / MCP 装配 | `src/octop/infra/connectors/builder.py`、`infra/connectors/gateway/registry.py` |
| 文件解析（Excel/编码）参考 | `src/octop/infra/knowledge/parse.py` |
| 工作区上传 | `src/octop/api/routers/uploads.py` |
| Dashboard 应用详情页与 SDK | `dashboard/src/pages/Apps/Detail.tsx` |
| Dashboard API 客户端 | `dashboard/src/api/modules/apps.ts` |
