# MiniApp 功能与架构现状

> 快照：2026-11-13，分支 `feature/miniapp-capabilities`，HEAD `3ced4a2c`，schema **v30**。
>
> 口径：**以当前代码与测试为准**。仓库里多份 `light-apps-*` / `miniapp-*` 文档是不同阶段的设计稿，只保留动机与历史记录，并在文首指向本文；本文把已落地的事实和明确未落地的部分分开写。
>
> 术语：统一使用 **MiniApp**；代码标识（`light_apps`、`light_app_rows`、`app_*` 工具）保持原样，不作为产品名。

---

## 0. 结论摘要

MiniApp 是 **Agent 交付物的着陆场**：Agent 用一组 `app_*` 工具创建一个带 schema 的数据表，再写一个自包含单文件 HTML 作为界面；数据行存在控制面 DB，HTML 存在 Agent 工作区，页面在无 JWT、无网络的沙箱 iframe 里运行，所有读写通过宿主桥代调。

当前实现已经不是“demo 级单页工具”，而是一套完整的小型应用平台：

| 维度 | 状态 |
|---|---|
| 数据底座 | ✅ 多表、行级 CAS、幂等、懒迁移、导入/导出、备份/恢复 |
| 创建 | ✅ Agent 工具、Excel/CSV 快速模式（确定性渲染，不走 LLM）、系统模板 + 专家模板 |
| 界面 | ✅ 单文件 HTML、`window.octopApp` SDK、能力清单、双层沙箱、CSP、WebRTC 冻结 |
| 协作 | ✅ owner / admin / `edit_data` / `view`，协作者、应用交接、删除守卫 |
| 发布 | ✅ 只读公开链接、发布快照、公开表可见性、PII 脱敏、限流、撤销 |
| AI | ✅ 应用内只读数据助手（含图表）、一次性文本生成；均计入应用 owner 额度 |
| 数据源 | ✅ 工作区文件 + 只读连接器工具；手动/定时、游标增量、upsert、失败退避 |
| 游戏 | ✅ 白名单外部游戏、一次性会话 token、结果回写、侧栏运行 |
| 工作台 | ✅ 个人多 Board、12 列网格、缩放/锁定/撤销/全屏/自动排列 |
| IM | ✅ `app_share_link` 一次调用交付、纯文本格式化、单轮预算 |
| 集成/分发（R2/R3） | ⛔ 外部服务、事件收件箱/发送、统一调度、公开看板、迁移包 v2、认证链接、渠道身份均未实现 |

关键数字：

- 20 个 `app_*` Agent 工具；约 57 个 HTTP 路由 + 1 个公开宿主页。
- 每个用户最多 50 个应用；每个应用最多 12 张表、每表 50 列、2 万行、5 MB。
- 单行读取上限 500；单文件 HTML 上限 512 KB；素材单文件 5 MB。
- 11 个应用能力位：7 个已接入执行链路，3 个为后续预留，1 个（`events.read`）已在新应用默认集但暂无页面 API。
- 后端 `infra/apps/` 约 9,957 行；应用 API 路由约 3,100 行；前端 MiniApp 页面/宿主约 9,300 行。
- 本次验证：`tests/unit/apps` + `test_apps_api.py` + `test_boards_api.py` **368 passed**；浏览器沙箱回归 `test_miniapp_sandbox.py` **12 passed**。

---

## 1. 定位与边界

### 1.1 它是什么

| 维度 | 说明 |
|---|---|
| 数据 | 结构化表（schema + 行），存在控制面 DB，可被 Agent / 人 / 外部数据源写入 |
| 界面 | Agent 生成的单文件 HTML，运行在沙箱 iframe，通过 `window.octopApp` 读写自己的数据 |
| 交付 | 可发布成只读公开链接；可嵌入工作台看板、对话侧栏、IM 卡片 |
| 生命周期 | 归属一个 Agent 工作区；可交接给另一个 Agent；删除有守卫 |

### 1.2 它不是什么

- 不是微信/支付宝式原生小程序运行时：没有每应用进程、没有服务端执行页面代码、没有自定义域名。
- 不是业务数据库的权威源：定位是投影层；任务实体仍落原生表（见 `docs/team-autopilot-design.html`）。
- 不是实时协同编辑：没有 CRDT、行级/字段级权限、多人同时编辑 HTML。
- 不是审批系统：审批在 `hitl_pending`；MiniApp 只承载结果。
- 不是多租户隔离载体：单实例产品，公开链接是 bearer token 语义。

### 1.3 与相邻概念

| 概念 | 存什么 | 谁写 | 谁读 | 生命周期 |
|---|---|---|---|---|
| `threads` / `thread_messages` | 对话 | Agent + 用户 | 人 | 会话级 |
| `memory` | 记忆 | Agent | Agent | 长期 |
| `knowledge_bases` | 文档（RAG） | 用户 | Agent | 长期 |
| **MiniApp** | **结构化数据 + 界面** | **Agent + 人 + 外部同步** | **人（+ Agent 读回）** | **产品级** |

---

## 2. 功能地图

### 2.1 创建

| 入口 | 行为 | 状态 |
|---|---|---|
| Agent 工具 | `app_create` 定义字段并建主表；`app_write_html` 写界面；`app_append_row` 灌数据 | ✅ |
| 快速模式 | 工作台「创建」→ 上传 xlsx/xls/csv/tsv/json → 服务端 inspect → 用户确认字段 → 选视图 → 确定性渲染 HTML | ✅ |
| 本地模板 | 系统模板（包内 5 个）+ Agent 工作区 `app_templates/<id>/` | ✅ |
| 从模板创建 | `POST /api/apps/from-template`、`app_create_from_template`（`source` 为 `system` 或 `agent`） | ✅ |
| 云端市场 | 前端「云端市场」页签只有占位文案 | ⛔ |

快速模式细节：

- 文件上限 50 MB；服务端 `inspect_file()` 给出 sheet、表头行、列类型、非空率、枚举候选、样本、PII 标记。
- 支持单 sheet 和多 sheet：多 sheet 时每张 sheet 建一张表，第一张映射到 `main`。
- 视图三选一：`dashboard`（KPI + 趋势 + 分类 + 明细）、`ledger`（筛选 + 分页表格）、`board`（按状态/阶段分组）。
- **不调用 LLM**：`viewgen.py` 纯函数渲染，秒级完成、结果稳定、可单测。
- 失败回滚：创建或导入失败会删除半成品应用。
- 创建后仍可让专家继续用 `app_write_html` 重写界面。

### 2.2 数据

- **多表**：每个应用从 `main` 开始，最多 12 张表；每表独立 schema、行、CSV 快照。
- **行存储**：`light_app_rows`，`values_json` 存 `{column: string}`；行级 `version` 做 CAS。
- **幂等**：`client_request_id` 唯一索引；外部源 `(source_id, source_key)` 唯一索引。
- **懒迁移**：`storage_version=1` 的旧应用首次读/写时把 CSV 灌入 DB，成功后置 2；失败保留 v1 可重试。
- **Schema 演进**：`app_alter_schema` / `PATCH /schema` 支持改名、删除、改类型；整表先校验，失败整次拒绝。
- **导入**：CSV/TSV/JSON/xlsx/xls；`sheet`、`header_row`、`dry_run`、`upsert_key`、多文件 `paths`、列映射、失败行回传。
- **导出**：`GET /api/apps/{id}/export` 从 DB 渲染 CSV 快照。
- **备份/恢复**：zip（manifest + app.html + preview.svg + tables/*/schema.json + tables/*/data.csv + assets/*）；`GET /backup` 与 `POST /restore`，格式 v1，解压上限 256 MB。
- **PII**：列级 `pii` 标记 + 值级检测；导入默认跳过敏感列（必填敏感列拒绝并要求显式 `include_pii=true`）；viewer / 公开链接 / 助手 / 导出统一脱敏。

限额：

| 项 | 上限 |
|---|---|
| 应用 / 用户 | 50 |
| 表 / 应用 | 12 |
| 列 / 表 | 50 |
| 行 / 表 | 20,000 |
| 单表数据 | 5 MB |
| 单元格 | 10,000 字符 |
| HTML | 512 KB |
| 素材 | 5 MB / 文件 |
| 预览 SVG | 256 KB |
| 协作者 / 应用 | 50 |

### 2.3 界面与宿主

- **单文件 HTML**：CSS/JS 全内联；禁止远程脚本、样式、图片、字体、媒体、`fetch()`、`WebSocket`、`EventSource`、`sendBeacon`、外链 `<a>`、`meta refresh`、`location` 导航、`window.open`、`RTCPeerConnection`、嵌套 frame/object/embed。
- **SDK**：`me()`、`schema(table)`、`tables()`、`listRows(options)`、`appendRow(row, options)`、`generate(prompt, options)`、`launchGame(gameId)`、`composeMessage(text)`、`openExternal(url)`、`asset(name)`。
- **能力清单**：`light_apps.capabilities_json`；旧应用空值 = 全开；新应用默认安全集；显式 `[]` = 页面不能做任何操作。
- **宿主页面**：
  - 应用详情页：iframe + 「界面 / 数据」双 Tab + 助手抽屉 + 分享/发布/能力/数据源/备份入口。
  - 对话：`app_card` 卡片、侧栏 MiniApp 标签、对话内 MiniApp 列表（从 transcript 的 `app_card` 推导）。
  - 工作台：看板单元格直接渲染 MiniApp。
  - 公开链接：`/p/{token}` 服务端渲染宿主页，只读 SDK。

### 2.4 协作与权限

| 角色 | 权限 | 说明 |
|---|---|---|
| owner | `edit_structure` | 创建者；始终全权 |
| admin | `edit_structure` | 系统管理员绕过 |
| collaborator | `view` / `edit_data` | 按应用 ACL；最多 50 人 |
| 公开链接访问者 | 只读 | token 即凭证；不能写、不能 generate、不能 launchGame |

要点：

- **Agent 可访问 ≠ 应用可访问**：创建应用要求可访问该 Agent（owner/shared/admin）；读写按应用 ACL。
- **工具侧与 HTTP 侧同源**：`_get_for_actor` 与 `permission_for` 使用同一套判定。
- **应用交接**：`POST /api/apps/{id}/transfer` 复制已知文件并更新 `agent_id`；源文件 best-effort 保留。
- **删除守卫**：删除仍有应用的 Agent 返回 `AGENT_HAS_APPS`（409），需 `force=true`。
- **`app_delete` 强制 HITL**：`MANDATORY_HITL_TOOLS` 包含 `app_delete`，需要用户批准。
- **团队 host 不挂 `app_*` 工具**：团队 host 使用 board tools / 交付物来引用 MiniApp（`artifact_type="app"`）。

### 2.5 发布与公开链接

- **默认关闭**：只有显式创建/分享才产生公开链接。
- **token**：32 字节随机值，DB 存 SHA-256；明文在 `secrets` 表保留一份，用于 `app_share_link` 复用同一链接；`rotate=true` 才轮换。
- **发布快照**：发布时冻结当前 HTML + 公开表结构（表清单、列定义、标签、`public_visible`）；数据行仍实时读。
- **公开表**：主表默认公开，附加表默认私有；隐藏表立即从公开面消失，即使已发布。
- **PII**：公开读取 `redact_pii=True`；快照列按实时 schema 的 PII 标记脱敏。
- **路由**：`GET /api/public/apps/{token}`、`GET /api/public/apps/{token}/rows`、`GET /p/{token}`；JWT 豁免，`Cache-Control: no-store`，按 token+IP 限流。
- **撤销/过期**：`revoked_at` 或 `expires_at` 命中后 404。
- **审计**：创建/撤销/公开访问写 `audit_log`。
- **不支持**：公开写入、公开事件、公开看板分享。

### 2.6 AI 助手与文本生成

**应用内只读助手**（`POST /api/apps/{id}/assistant`，SSE）：

- 只读工具：`app_tables`、`app_query_rows`、`app_chart`。
- 上限：8 个模型轮次、回放 12 条历史、问题 4,000 字、回答 8,000 字。
- 图表：bar/line/pie/area/scatter/stacked_bar；后端用真实行数据聚合，禁止模型编造序列。
- 历史按 `(app, user)` 分区；回答引用 `row_id`；敏感列脱敏。
- 计费与限额归属**应用 owner**，不是提问者。
- 每应用 LLM 并发 2、每分钟 30 次；超限 `APP_BUSY`（429）。

**一次性生成**（`POST /api/apps/{id}/generate`）：

- 页面通过 `octopApp.generate(prompt, {system})` 调用；无历史、无工具、不读应用数据。
- 需要 `ai.generate` 能力位 + `view` ACL；服务端强制校验能力位。
- 使用 owning agent 的默认模型；计入 owner 额度；90 秒超时、输出上限 16k 字符。
- 目前**不支持按应用选择模型**。

### 2.7 数据源与导入

| 类型 | `kind` | 行为 |
|---|---|---|
| 工作区文件 | `workspace_file` | 按路径重新导入；支持 `upsert_key`、`include_pii` |
| 外部连接器 | `external` | 调一个只读 MCP/连接器工具，取结果 JSON，按 `result_path` 提取记录 |
| 手动运行 | — | `POST /api/apps/{id}/sources/{source_id}/run` |
| 定时运行 | — | `schedule_seconds > 0`；30 秒扫描 + 条件 UPDATE 领取；失败指数退避，上限 24h |

约束：

- 一张表只能绑定一个数据源。
- 外部工具必须是只读（`_require_read_only`）；参数与结果路径可配置；支持 `cursor_arg` / `cursor_path` 增量游标，成功后写回 `params_json`。
- `mode=append|replace`；`replace` 在导入结果为空时拒绝，避免把表清空。
- 外部数据源不是 R2 计划里的 `light_app_services` 通用服务调用/缓存；后者未实现。

### 2.8 外部游戏

- 游戏 URL 只能在 `config.json` 的 `games` 白名单里；MiniApp 只能传 `game_id`。
- `POST /api/apps/{id}/game-sessions`：校验 `edit_data` + `game.launch` 能力位，签发 2 小时一次性 JWT。
- 游戏在侧栏独立 iframe 运行，token 放在 URL fragment；不持有 dashboard JWT。
- 结果通过 `POST /api/game-sessions/{token}/results` 回写；`append` 按 `session_id + op + row` 幂等，`upsert` 按 `upsert_key`。
- 单次最多 10 个 operation、每 operation 1,000 行；写入时重新校验 ACL。
- 公开链接拒绝 `launchGame`。

### 2.9 工作台看板（Board）

- 每个用户多个 Board；独立 URL `/apps/boards/{board_id}` 与全屏 `/apps/boards/{board_id}/view`。
- 12 列网格；每个单元格 `{app_id,x,y,w,h,locked}`；最多 24 个应用。
- 拖拽/缩放、锁定、撤销、自动排列、全屏；布局保存带 CAS 版本，冲突提示并重载。
- 首次加载把旧 `localStorage` 单 Board 无损迁移为默认 Board。
- **仅个人看板**：没有 `share_token_hash`，没有公开看板接口。

### 2.10 IM 通道

- `app_share_link`：按 app_id 或名称查找 → 服务端复用/创建公开链接 → 返回纯文本 + 可选封面；默认不轮换 token。
- `app_open`：dashboard 来源返回 `app_card`；IM 来源降级为公开链接 + 纯文本，避免把 `octop_ui` 卡片发给不支持的通道。
- 微信/QQ：`format_channel_text()` 去掉 Markdown；`withhold_weixin_stream_preamble()` 丢弃最终答案前的中间旁白。
- 单轮预算：`StreamBudget(max_tool_calls=20, max_seconds=180, max_repeats=3)`，超限取消本轮并提示。
- 公开链接需要 `OCTOP_PUBLIC_BASE_URL`（或 `config.json.public_base_url`）才是绝对地址；否则返回相对路径。
- IM 认证链接、渠道身份绑定属于 R3，未实现。

### 2.11 安全与运维

- **写入时校验**：`validate_html()` + `find_external_refs()` 在 `app_write_html` 阶段拒绝外链、frame、网络 API、导航 API、WebRTC。
- **渲染时双层沙箱**：固定 wrapper 文档（`frame-src 'none'`）包住 App iframe（`sandbox="allow-scripts"`）；wrapper 转发消息，第二次 `load` 视为逃逸并熔断。
- **WebRTC / frame 冻结**：App 脚本前注入不可配置的 `undefined`，封堵 `createElement`、`innerHTML`、`DOMParser`、`Range` 等 frame 创建路径；MutationObserver 兜底；SDK 每次请求前做不变量自检。
- **违规上报**：CSP 违规 / 沙箱完整性失败 → `/api/apps/{id}/security-events` → 限流 → metrics + audit；公开页暂不上报。
- **服务端能力校验**：`generate`、`game-sessions` 在服务端调用 `require_capability`，不只依赖前端桥。
- **指标**：`light_app_open_total`、`light_app_row_write_total`、`light_app_export_total`、`light_app_assistant_total`、`light_app_generate_total`、`light_app_public_view_total`、`light_app_csp_violation_total`、`light_app_sandbox_integrity_total`、`light_app_llm_rejected_total`、`light_app_public_rate_limited_total`。
- **限流**：每进程；多 worker 是每 worker 一份预算，不是全局。

### 2.12 桌面模式（AgentOS Shell）

- 工作台顶部「看板 / 桌面」切换；进入桌面自动全屏并播放「智管家」启动动效。
- 每个已启用专家都有独立桌面（即使还没有小程序）；「全部应用」是跨专家的全局小程序库，从它打开应用会跳到所属专家桌面。
- 窗口统一由 `desktopWindows.ts` 的 Window Registry 描述（`app` / `chat` / `external` 的 id、最小尺寸、标题、图标与持久化规则）；窗口位置、大小、层级和最小化/最大化状态恢复到 `localStorage`。
- Dock 2.0 汇总当前桌面的小程序、专家会话和外部网页窗口，显示运行/最小化状态，右键提供显示、最小化、关闭；末尾是「全部应用」启动器。
- Mission Control（菜单栏按钮或 `F3`）一次展示所有专家桌面及打开的窗口，点击窗口会切回所属桌面并聚焦。
- 智能体动态中心汇总运行中的专家、全部打开窗口和未处理的审批/安全提醒；桌宠会按专家状态显示空闲、工作、告警或离线。
- 通知中心支持小程序审批、专家回复、任务、安全与定时任务等类型；当前由桌面会话产生审批/回复通知，其余类型供后续事件源接入。
- 已打开窗口仍只通过 sandboxed MiniAppFrame、桌面会话或外链 webview 渲染，桌面 Shell 不新增任何后端权限。

---

## 3. 架构

### 3.1 全景

```
                    Agent (LLM)                 Dashboard                IM channel
                         │                          │                        │
                  app_* tools                /api/apps, /api/boards     app_share_link
                         │                          │                        │
                         ▼                          ▼                        ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │                    AppService (infra/apps/service.py)            │
              │  lifecycle / ACL / tables / rows / import / publication /        │
              │  sources / backup / transfer                                     │
              └───────┬──────────────────────┬──────────────────────┬────────────┘
                      │                      │                      │
                      ▼                      ▼                      ▼
             LightAppRepo            BackendWorkspace        Assistant / Generator
             (SQLite/PG)             app.html / assets       owner's agent model
                      │                 preview.svg / mirrors   + quota + usage
                      ▼
        light_apps / light_app_tables / light_app_rows
        light_app_collaborators / light_app_assistant_messages
        light_app_sources / light_app_publications / miniapp_boards

  Browser:
  Dashboard host ── postMessage bridge ── wrapper iframe (fixed doc)
                                              └─ App iframe (opaque origin, no JWT)
  Public:
  /p/{token} host page ── same wrapper ── read-only bridge ── /api/public/apps/*
```

### 3.2 模块职责

| 文件 | 职责 |
|---|---|
| `infra/apps/service.py` | 应用生命周期、ACL、表/行、schema 演进、导入导出、发布、数据源、备份恢复、交接 |
| `infra/apps/models.py` | Schema/Column/行校验、CSV 序列化、HTML 写入校验、外链/导航扫描 |
| `infra/apps/capabilities.py` | 能力目录、解析、校验、新应用默认集 |
| `infra/apps/sandbox.py` | 服务端 CSP、wrapper 文档、WebRTC 冻结、静态 frame 检查、违规 URL 脱敏 |
| `infra/apps/tools.py` | 20 个 `app_*` LangChain 工具、看板/分享链接载荷 |
| `infra/apps/assistant.py` | 只读数据助手（SSE、工具循环、图表、历史） |
| `infra/apps/generation.py` | 一次性文本生成 |
| `infra/apps/charts.py` | 图表请求校验与后端聚合 |
| `infra/apps/model_access.py` | 解析应用所属 Agent 的默认模型 |
| `infra/apps/pii.py` | 表头/值级 PII 检测与脱敏 |
| `infra/apps/inspect.py` | Excel/CSV/JSON 结构分析 |
| `infra/apps/importer.py` | 文件解析为行 |
| `infra/apps/viewgen.py` | 快速模式确定性 HTML 渲染 |
| `infra/apps/templates.py` | 系统模板注册表 + Agent 模板信息 |
| `infra/apps/preview.py` | 封面 SVG 校验 |
| `infra/apps/sources.py` | 连接器只读工具调用 |
| `infra/apps/scheduler.py` | 数据源定时同步循环 |
| `infra/apps/boards.py` | 个人看板布局与 CAS |
| `infra/apps/game_sessions.py` | 游戏 token 签发/校验 |
| `infra/apps/session.py` | 从对话 transcript 提取 app_card id |
| `infra/apps/prompt.py` | 向 Agent 系统提示注入 MiniApp 规则 |
| `infra/db/repos/apps.py` | 应用/表/行/协作者/助手历史/数据源/发布 SQL |
| `infra/db/repos/boards.py` | 看板 SQL |
| `api/routers/apps.py` | 应用 HTTP 面（约 47 个路由） |
| `api/routers/public_apps.py` | 公开只读 API + `/p/{token}` 宿主页 |
| `api/routers/boards.py` | 看板 CRUD / 布局 |
| `api/routers/game_sessions.py` | 游戏结果写入 |

### 3.3 数据模型

| 表 | 用途 | 关键字段 |
|---|---|---|
| `light_apps` | 应用元数据 | `app_id`、`agent_id`、`owner_user_id`、`storage_version`、`capabilities_json`、`previous_html*`、计数 |
| `light_app_tables` | 表注册 | `table_key`、`schema_json`、`schema_version`、`data_version`、`public_visible`、行数/字节 |
| `light_app_rows` | 行数据 | `row_id`、`values_json`、`version`、`source`、`source_id/source_key`、`client_request_id` |
| `light_app_collaborators` | 协作者 ACL | `user_id`、`permission` |
| `light_app_assistant_messages` | 助手历史 | `app_id`、`user_id`、`role`、`content` |
| `light_app_sources` | 数据源绑定 | `table_key`、`kind`、`mode`、`schedule_seconds`、`params_json`、`mapping_json`、运行状态 |
| `light_app_publications` | 公开链接 | `token_hash`、`expires_at`、`revoked_at`、`html_snapshot`、`tables_snapshot_json`、`snapshot_digest` |
| `miniapp_boards` | 个人看板 | `board_id`、`owner_user_id`、`layout_json`、`version` |
| `goal_deliverables` | 团队交付物 | `artifact_type` 支持 `app`，`artifact_ref=app_id` |

迁移编号：025（应用/协作者/多表/助手/数据源）→ 026（交付物）→ 027（外部事件）→ 028（行入库）→ 029（公开链接）→ 030（表可见性/能力清单/HTML 回滚/看板）。当前 `_schema_version = 30`，`tests/unit/db/test_db_pool.py` 断言为 30。

### 3.4 存储布局

每个应用在 owning Agent 工作区下：

```
apps/<app_id>/
  app.html            # 唯一必写产物，Agent 生成的完整页面
  preview.svg         # Agent 画的封面（app_set_preview）
  manifest.json       # 只读镜像：app 元数据 + 表清单
  schema.json         # 主表 schema 镜像（历史兼容）
  data.csv            # 主表 CSV 快照（新应用不再随 append 重写）
  tables/<key>/
    schema.json
    data.csv
  assets/<name>       # PNG/JPEG/WebP/GIF，≤5MB
```

行数据在 DB；工作区文件是 `app.html`、`assets/`、`preview.svg` 和 schema/manifest 镜像。远程后端（COS/S3/OSS/OBS/docker）通过 `BackendWorkspace` 读写，Octop 不直接拼路径。

### 3.5 关键链路

**A. Agent 创建应用**

```
LLM → app_inspect_file（可选）
    → app_create（写 light_apps + light_app_tables + 工作区 schema/manifest）
    → app_write_html（validate_html → workspace app.html）
    → app_append_row / app_import_rows（校验 → light_app_rows + 计数）
    → app_set_preview（SVG 校验 → preview.svg）
    → app_open（返回 app_card；对话里出现入口）
```

**B. Dashboard 打开应用**

```
GET /api/apps/{id} + /html
  → buildSrcDoc(HTML) + buildWrapperDoc()
  → host iframe (sandbox=allow-scripts)
      → wrapper iframe (CSP frame-src 'none')
          → app iframe (srcdoc, opaque origin)
  → app 的 octopApp.* → postMessage → bridge.attachBridge
      → 能力位检查 → appsApi.* → /api/apps/...（JWT）
```

**C. 公开链接**

```
app_share_link / POST publication
  → publish(): 冻结 HTML + 公开表结构，token hash 入库
  → /p/{token} 服务端渲染宿主页
      → wrapper → app iframe
      → 只读桥 → /api/public/apps/{token}[/rows]
      → live rows + 实时 PII/表可见性
```

**D. 数据源同步**

```
light_app_sources(schedule_seconds > 0)
  → AppSourceScheduler.tick() 每 30s
  → claim_source() 条件 UPDATE 领取
  → run_source():
       workspace_file → import_from_file()
       external       → GatewayToolRunner.run() → extract → import_rows()
  → 记录 last_status / last_error / next_run_at / fail_count
```

**E. 应用内助手 / 生成**

```
POST /api/apps/{id}/assistant
  → acquire_app_llm()（并发/分钟限流）
  → AppAssistant.prepare()（解析 owner agent 模型、检查额度）
  → SSE: token / tool / chart / done / error
  → app_tables / app_query_rows / app_chart（只读，PII 脱敏）

POST /api/apps/{id}/generate
  → require_capability(ai.generate)
  → app_llm_slot()
  → AppGenerator.generate()（owner 模型、owner 额度）
```

**F. 游戏**

```
octopApp.launchGame(game_id)
  → POST /api/apps/{id}/game-sessions
  → 校验 edit_data + game.launch + 白名单
  → 签发 2h JWT（app/user/game/session）
  → 侧栏 GameFrame 打开 <url>#token=...
  → POST /api/game-sessions/{token}/results
  → append/upsert 写入 light_app_rows
  → postMessage session-finished → MiniApp 重新加载
```

### 3.6 权限模型

MiniApp 有两套正交的权限：

| 维度 | 控制对象 | 规则 |
|---|---|---|
| **用户 ACL** | 登录用户对应用的读写 | owner/admin = `edit_structure`；协作者 = `view` / `edit_data` |
| **页面能力清单** | iframe 页面能向宿主请求什么 | `capabilities_json`；旧应用全开；新应用安全集；`[]` 全关 |

执行点：

- 页面方法：`bridge.ts` 的 `checkMethodAllowed`；未知方法默认拒绝。
- 昂贵/外部副作用端点：服务端 `require_capability`（当前 `generate`、`game-sessions`）。
- 数据端点（`/rows`、`/assets`）：以用户 ACL 为准，能力位只约束页面代码，不约束用户本人。
- 公开面：token 即凭证；只读；不接受 JWT。

能力清单现状：

| 能力 | 页面方法 | 默认新应用 | 执行状态 |
|---|---|---|---|
| `data.read` | `schema` / `tables` / `rows` | 开 | ✅ |
| `data.write` | `append` | 开 | ✅ |
| `asset.read` | `asset` | 开 | ✅ |
| `external.open` | `openExternal` | 开 | ✅ |
| `chat.compose` | `composeMessage` | 开 | ✅ |
| `events.read` | — | 开 | 🟡 仅能力名，SDK/桥无事件方法 |
| `ai.generate` | `generate` | 关 | ✅ |
| `game.launch` | `launchGame` | 关 | ✅ |
| `external.fetch` | — | 关 | ⛔ 预留 |
| `events.emit` | — | 关 | ⛔ 预留 |
| `schedules.manage` | — | 关 | ⛔ 预留；当前只有数据源 `schedule_seconds` |

### 3.7 沙箱模型

App HTML 是 Agent 生成内容，按不可信代码处理：

1. **写入时**：`validate_html` 拒绝远程资源、网络 API、导航 API、frame/object/embed、WebRTC、`javascript:` URL；已有旧应用若静态含 frame，渲染时直接显示安全错误页。
2. **渲染时**：
   - 外层固定 wrapper 文档由 Octop 自己编写，声明 `frame-src 'none'`；App 在 wrapper 的 inner iframe 中，opaque origin。
   - wrapper 负责双向转发；第二次 `load` 视为导航逃逸，移除 inner frame 并向宿主发 `escaped`。
   - App 脚本之前注入 `WEBRTC_FREEZE`：RTC 全局不可配置 `undefined`；封堵 frame 创建 API；MutationObserver 兜底。
   - SDK 每次桥请求前自检 RTC/frame 不变量，失败拒绝请求并上报。
3. **宿主桥**：只接受来自 wrapper/contentWindow 的消息；`event.source` 校验；未知方法拒绝；能力位检查。
4. **公开页**：服务端 `build_app_doc` + `build_wrapper_doc`，使用同一套 CSP/冻结；只读桥仅支持 `tables` / `schema` / `rows`。
5. **残余风险**：srcdoc iframe 与 Dashboard 同 renderer process，死循环仍可卡死页面；浏览器 bug/未覆盖 DOM API 可能绕过；这是纵深防御，不是形式化隔离。需要强隔离时应走独立内容域或服务端浏览器。

### 3.8 并发、幂等与限额

- **行追加**：DB 事务内单行 INSERT + 计数更新；进程内 `asyncio.Lock` 仍包住同一应用的行操作，跨进程依靠 DB 事务、唯一索引和 CAS。
- **行修改/删除**：`row_id + version` CAS；冲突 409 `APP_CONFLICT`。
- **幂等**：`client_request_id`；数据源 `(source_id, source_key)`；游戏 `session_id:op:row`。
- **多实例**：插入/更新/CAS 正确；但限额检查（如 `count >= MAX_ROWS`）仍是 check-then-insert，且限流/锁是进程内，不是全局。
- **懒迁移**：进程内锁 + `storage_version` 条件更新；其他进程完成后重读 DB。

### 3.9 前端结构

| 路径 | 职责 |
|---|---|
| `dashboard/src/pages/Apps/index.tsx` | 工作台：Board 列表、网格、创建入口、备份恢复、归档列表 |
| `dashboard/src/pages/Apps/BoardGrid.tsx` | 12 列网格、拖拽/缩放/锁定/撤销/自动排列 |
| `dashboard/src/pages/Apps/BoardView.tsx` | 全屏看板 |
| `dashboard/src/pages/Apps/Detail.tsx` | 应用详情：界面/数据双 Tab、助手、分享、发布、能力、数据源、备份 |
| `dashboard/src/pages/Apps/CreateMiniAppModal.tsx` | 创建弹窗：快速 / 模板 / 云端市场占位 |
| `dashboard/src/pages/Apps/appBoard.ts` | 旧 localStorage Board 迁移与放置逻辑 |
| `dashboard/src/pages/Apps/DesktopGrid.tsx` | AgentOS 桌面：专家工作空间、窗口管理、启动动效、桌宠 |
| `dashboard/src/pages/Apps/desktopWindows.ts` | Window Registry、窗口工厂、会话恢复与标题解析 |
| `dashboard/src/pages/Apps/DesktopDock.tsx` | Dock 2.0：运行窗口、全部应用、右键窗口操作 |
| `dashboard/src/pages/Apps/DesktopMissionControl.tsx` | Mission Control：跨桌面窗口总览 |
| `dashboard/src/pages/Apps/DesktopActivityCenter.tsx` | 智能体动态、打开窗口与待处理提醒 |
| `dashboard/src/pages/Apps/DesktopChatWindow.tsx` | 桌面内专家会话窗口 |
| `dashboard/src/pages/Apps/DesktopWebView.tsx` | 桌面内外链 web-view 窗口 |
| `dashboard/src/components/MiniAppFrame/index.tsx` | 通用 MiniApp 宿主（聊天/侧栏） |
| `.../wrapper.ts` | 双层 wrapper 文档 |
| `.../bridge.ts` | 共享桥 + 能力检查 + 违规上报 |
| `.../sdk.ts` | `window.octopApp` SDK + WebRTC 冻结 + `buildSrcDoc` |
| `.../capabilities.ts` | 前端能力目录与方法映射 |
| `.../GameFrame.tsx` | 外部游戏侧栏宿主 |
| `dashboard/src/pages/Chat/components/ChatDockSessionApps.tsx` | 对话内 MiniApp 列表/打开 |
| `dashboard/src/plugins/toolRenderers/builtin/AppCard.tsx` | 对话 app_card |
| `dashboard/src/plugins/toolRenderers/builtin/AppInspectCard.tsx` | Excel 检查确认卡 |
| `dashboard/src/api/modules/apps.ts` / `boards.ts` | API 客户端 |

---

## 4. 实现现状矩阵

### 4.1 功能矩阵

| 域 | 能力 | 状态 | 说明 |
|---|---|---|---|
| 创建 | Agent 工具创建 | ✅ | `app_create` + `app_write_html` |
| 创建 | Excel 快速模式 | ✅ | 确定性渲染，不打 LLM |
| 创建 | 系统模板 | ✅ | 5 个：客户跟进、月度开支、库存盘点、项目任务、费用报销 |
| 创建 | 专家模板 | ✅ | `app_templates/<id>/`，随专家发布/安装 |
| 创建 | 云端市场 | ⛔ | 前端占位 |
| 数据 | 多表 | ✅ | 最多 12 张 |
| 数据 | 行入库 + CAS + 幂等 | ✅ | `light_app_rows` |
| 数据 | 旧 CSV 懒迁移 | ✅ | `storage_version` |
| 数据 | Schema 改名/删除/改类型 | ✅ | `app_alter_schema` |
| 数据 | CSV/Excel/JSON 导入 | ✅ | sheet/header/dry-run/upsert/多文件/失败行 |
| 数据 | CSV 导出 | ✅ | 从 DB 渲染 |
| 数据 | 备份/恢复 | ✅ | zip v1，64 MB / 256 MB 上限 |
| 数据 | 工作区文件数据源 | ✅ | 手动/定时 |
| 数据 | 只读连接器数据源 | ✅ | 外部 MCP 工具 + 游标 + 退避 |
| 数据 | 通用外部服务（R2 `light_app_services`） | ⛔ | 未实现 |
| 界面 | 单文件 HTML + SDK | ✅ | 10 个方法 |
| 界面 | 能力清单 | ✅ | 7 执行 + 3 预留 + `events.read` 仅占位 |
| 界面 | 双层沙箱 + CSP + WebRTC 冻结 | ✅ | P0/P1/WebRTC 已完成 |
| 界面 | 违规上报 | ✅ | 登录态页面；公开页暂不上报 |
| 协作 | 应用 ACL | ✅ | view / edit_data / edit_structure |
| 协作 | 协作者 | ✅ | 最多 50 |
| 协作 | 应用交接 | ✅ | 复制文件 + 更新 agent |
| 协作 | 删除守卫 | ✅ | `AGENT_HAS_APPS` |
| 协作 | 团队交付物 = 应用 | ✅ | `artifact_type="app"` |
| 发布 | 只读公开链接 | ✅ | token hash + 撤销/过期 |
| 发布 | 发布快照 | ✅ | HTML + 公开表结构；行实时 |
| 发布 | 公开表可见性 | ✅ | 主表默认公开，附加表默认私有 |
| 发布 | PII 脱敏 | ✅ | 列级 + 值级 |
| 发布 | HTML 上一版回滚 | ✅ | `previous_html` |
| 发布 | 公开看板 | ⛔ | R2/P3 未实现 |
| AI | 只读数据助手 | ✅ | 工具循环 + 图表 + SSE |
| AI | 一次性文本生成 | ✅ | `ai.generate` + owner 额度 |
| AI | 按应用选择模型 | ⛔ | 目前用 owning agent 默认模型 |
| AI | 实时数据刷新 `data_version` 轮询 | 🟡 | 列存在并递增；前端未轮询/未发 `data-changed` |
| 游戏 | 外部游戏接入 | ✅ | 白名单 + 2h token + 结果回写 |
| 看板 | 个人多 Board | ✅ | 12 列、CAS、锁定、撤销、全屏、自动排列 |
| 看板 | 公开看板分享 | ⛔ | 无 `share_token_hash` / `/public/boards/*` |
| IM | `app_share_link` | ✅ | 一次调用、复用链接、封面 |
| IM | `app_open` 通道降级 | ✅ | dashboard → app_card；IM → 文本+链接 |
| IM | 微信纯文本格式化 | ✅ | `format_channel_text` |
| IM | 单轮预算/重复失败熔断 | ✅ | `StreamBudget` |
| IM | 认证链接 / 渠道身份 | ⛔ | R3 未实现 |
| 事件 | 事件平面驱动 `app.refresh` / `app.import` | 🟡 | 已存在；`app.import` 强制 `replace` |
| 事件 | 应用收件箱 / `emitEvent` | ⛔ | 未实现 |
| 分发 | 跨主机 Bundle v2 | ⛔ | 只有同实例 zip 备份/恢复 |
| 运维 | 指标 | ✅ | 应用/行/导出/助手/生成/公开/CSP/沙箱 |
| 运维 | 审计 | ✅ | 快速创建、分享、发布、导出、备份/恢复、游戏、CSP/沙箱、生成；工具侧日常写入不逐条审计 |
| 运维 | 限流 | ✅ | 每进程；非全局 |

### 4.2 Agent 工具（20 个）

| 工具 | 权限 | 作用 |
|---|---|---|
| `app_inspect_file` | Agent 可访问 | 检查工作区表格文件 |
| `app_list` | Agent 可访问 | 列出当前用户在该 Agent 下创建的应用 |
| `app_open` | `view` | 打开应用；dashboard 返回 `app_card`，IM 返回公开链接 |
| `app_share_link` | `edit_structure` | 获取/轮换公开链接 + 封面 |
| `app_create` | Agent 可访问 | 建应用 + 主表 + 可选能力清单 |
| `app_create_from_template` | Agent 可访问 | 从系统/专家模板建空应用 |
| `app_add_table` | `edit_structure` | 新增附加表 |
| `app_update_schema` | `edit_structure` | 追加列（只能加） |
| `app_alter_schema` | `edit_structure` | 改名/删除/改类型/标 PII |
| `app_set_table_visibility` | `edit_structure` | 设置表是否公开 |
| `app_set_capabilities` | `edit_structure` | 替换页面能力清单 |
| `app_write_html` | `edit_structure` | 写完整 HTML（不产生聊天卡片） |
| `app_read_data` | `view` | 分页读行 + `row_id`/`version` |
| `app_append_row` | `edit_data` | 追加一行（可幂等） |
| `app_update_row` | `edit_data` | 按 `row_id` + `version` 更新 |
| `app_delete_row` | `edit_data` | 按 `row_id` + `version` 删除 |
| `app_import_rows` | `edit_data` | 批量导入（dry-run/upsert/多文件） |
| `app_save_asset` | `edit_data` | 保存图片素材 |
| `app_set_preview` | `edit_data` | 保存 SVG 封面 |
| `app_delete` | `edit_structure` | 删除应用（**强制 HITL**） |

说明：团队 host 不挂这组工具；它们只挂普通 Agent（`if not team_host`）。

### 4.3 API 面

应用（`/api/apps`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/apps` | 列表 |
| GET | `/apps/in-thread` | 对话内应用 |
| GET | `/apps/templates` | 模板列表 |
| GET | `/apps/templates/{template_id}/preview` | 模板封面 |
| POST | `/apps/from-template` | 从模板创建 |
| POST | `/apps/quick/inspect` | 快速模式分析文件 |
| POST | `/apps/quick/create` | 快速模式创建 |
| GET | `/apps/{id}` | 详情 |
| GET | `/apps/{id}/tables` | 表列表 |
| PATCH | `/apps/{id}/tables/{table_key}` | 表可见性 |
| GET | `/apps/{id}/html` | HTML |
| POST | `/apps/{id}/security-events` | CSP/沙箱违规上报 |
| GET | `/apps/{id}/files` | 文件列表 |
| GET/POST | `/apps/{id}/assets` | 素材列表/上传 |
| GET | `/apps/{id}/assets/{name}` | 素材读取 |
| GET | `/apps/{id}/preview` | 封面 |
| GET/POST | `/apps/{id}/rows` | 读行/追加 |
| PATCH/DELETE | `/apps/{id}/rows/{row_id}` | 改行/删行（CAS） |
| GET | `/apps/{id}/export` | CSV 导出 |
| GET | `/apps/{id}/backup` | 备份 zip |
| POST | `/apps/restore` | 恢复备份 |
| PATCH | `/apps/{id}/schema` | 结构变更 |
| PATCH | `/apps/{id}` | 改名/描述 |
| PUT | `/apps/{id}/capabilities` | 能力清单 |
| POST | `/apps/{id}/restore-html` | 恢复上一版 HTML |
| DELETE | `/apps/{id}` | 删除 |
| GET/PUT | `/apps/{id}/shares` | 协作者 |
| GET/POST/DELETE | `/apps/{id}/publication` | 公开链接状态/创建/撤销 |
| POST | `/apps/{id}/publication/publish` | 刷新发布快照 |
| POST | `/apps/{id}/transfer` | 交接给另一个 Agent |
| GET | `/apps/{id}/assistant/history` | 助手历史 |
| POST | `/apps/{id}/assistant` | 助手问答（SSE） |
| POST | `/apps/{id}/generate` | 一次性生成 |
| POST | `/apps/{id}/upload` | 上传导入文件 |
| POST | `/apps/{id}/tables/{table_key}/import` | 导入 |
| GET | `/apps/{id}/sources` | 数据源列表 |
| PUT/DELETE | `/apps/{id}/sources/{source_id}` | 保存/删除数据源 |
| POST | `/apps/{id}/sources/{source_id}/run` | 立即运行 |
| GET | `/apps/{id}/source-tools` | 可用的只读连接器工具 |
| POST | `/apps/{id}/game-sessions` | 创建游戏会话 |

公开面（`/api/public/apps`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/public/apps/{token}` | 公开应用元数据 + HTML + 表 |
| GET | `/public/apps/{token}/rows` | 公开行（只读、脱敏、快照列过滤） |
| GET | `/p/{token}` | 服务端渲染宿主页（JWT 豁免） |

看板与游戏：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET/POST | `/api/boards` | 列表/创建 |
| GET/PATCH/DELETE | `/api/boards/{board_id}` | 详情/改名/删除 |
| PUT | `/api/boards/{board_id}/layout` | CAS 保存布局 |
| POST | `/api/game-sessions/{token}/results` | 游戏结果写入（JWT 豁免） |

### 4.4 路线图状态

依据 `docs/miniapp-platform-roadmap.md`：

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **R1** | 能力清单、发布快照、composeMessage、限流、HTML 回滚、多 Board | ✅ 全部落地（个人看板部分） |
| **R2** | 实时外部服务、事件收件箱/发送、统一调度、公开看板 | ⛔ 未开始（仅能力名预留） |
| **R3** | 迁移包 v2、IM owner 认证链接、渠道身份绑定 | ⛔ 未开始 |

R2/R3 的共同地基（能力清单）已经就位；但 `external.fetch` / `events.emit` / `schedules.manage` 目前只是可声明、不可执行的能力名，避免未来端点默认放行。

### 4.5 设计文档落地情况

| 文档 | 文档声明 | 代码现状 |
|---|---|---|
| `light-apps-architecture.md` | 已改为“历史设计稿（主体已实施）” | 行入库、CAS、幂等、懒迁移、发布、MCP 数据源、可编辑数据表、CSV 导入导出已落地；`data_version` 前端轮询未做 |
| `light-apps-enhancement-plan.md` | 已改为“讨论稿（主体已实施）” | P0/P1/P2/P3 主项均落地，遗留模型选择、实时刷新 |
| `light-apps-pii-design.md` | 策略文档 | ✅ 一致 |
| `light-apps-games.md` | 已实现 | ✅ 一致 |
| `miniapp-creation-modes-design.md` | P0/P1 已实现，云端市场 P2 未做 | ✅ 一致；市场仍是占位 |
| `miniapp-im-optimization.md` | P0 已落地 | ✅ 一致；P1/P2 部分未做 |
| `miniapp-sandbox-hardening.md` | P0/P1/WebRTC 已实施 | ✅ 一致；12 个浏览器回归通过 |
| `miniapp-platform-roadmap.md` | R1 已实施，R2/R3 待实施 | ✅ 一致 |
| `miniapp-event-api-design.md` | 讨论稿，未实施 | ✅ 一致；只有既有 `app.refresh`/`app.import` |
| `docs/architecture-overview.md` | 已同步为 20 个工具、23 个文件 / 9,957 行，并链接本文 | ✅ 一致 |

> 已删除两份已实施且被本文覆盖的设计稿：`light-apps-multi-table-design.md`、`light-apps-assistant-and-import-design.md`；多表、助手与导入内容见 §2.2、§2.6、§2.7。

---

## 5. 验证

### 5.1 本次运行

```bash
uv run pytest tests/unit/apps tests/integration/test_apps_api.py \
  tests/integration/test_boards_api.py -q
# 368 passed, 2 warnings in 236.80s

uv run pytest tests/integration/test_miniapp_sandbox.py -q
# 12 passed in 27.73s
```

未运行：`make all`（本次只新增文档，不改 Python/前端代码）；前端 `npx tsc -b` / vitest 未重跑。

### 5.2 测试分布

| 范围 | 覆盖点 | 用例数 |
|---|---|---:|
| 单元：应用核心 | `test_service.py` | 93 |
| 单元：模型/校验 | `test_models.py` | 62 |
| 单元：工具 | `test_tools.py` | 24 |
| 单元：沙箱 | `test_sandbox.py` | 15 |
| 单元：看板 | `test_boards.py` | 15 |
| 单元：图表/分析 | `test_charts.py`、`test_inspect.py` | 25 |
| 单元：封面/助手 | `test_preview.py`、`test_assistant.py` | 22 |
| 单元：导入/快速渲染 | `test_importer.py`、`test_viewgen.py` | 19 |
| 单元：生成/游戏 | `test_generation.py`、`test_game_sessions.py` | 14 |
| 单元：PII/限流/数据源 | `test_pii.py`、`test_limits.py`、`test_sources.py` | 17 |
| 单元：会话/提示/能力/调度 | `test_session_apps.py`、`test_prompt.py`、`test_capabilities.py`、`test_scheduler.py` | 17 |
| 集成：应用 API | `test_apps_api.py` | 40 |
| 集成：看板 API | `test_boards_api.py` | 5 |
| **合计** | `tests/unit/apps` + 两个集成文件 | **368** |
| 集成：浏览器沙箱 | `test_miniapp_sandbox.py`（单独运行） | 12 |

### 5.3 手工验证建议

1. 工作台「创建」→ 上传多 sheet Excel → 确认字段 → 生成看板 → 数据 Tab 改一行 → 分享 viewer。
2. 应用详情「发布」→ 无痕窗口打开 `/p/{token}` → 改 HTML 不重新发布（公开页不变）→ 重新发布（更新）。
3. 对话里让 Agent 创建应用并 `app_open` → 侧栏打开 → 页面 `composeMessage` 预填输入框。
4. `octopApp.launchGame` → 侧栏游戏 → 结果回写 → 应用刷新。
5. 工作台拖拽/缩放/锁定/撤销/全屏/自动排列，两个标签页同时改布局验证 CAS 冲突提示。
6. 上传含身份证列的文件 → 默认不导入 → 标记 `pii` 后公开链接读取显示掩码。

---

## 6. 已知缺口与风险

### 6.1 功能缺口

| 缺口 | 影响 | 备注 |
|---|---|---|
| R2 外部服务 `light_app_services` | 页面不能通过宿主调用任意只读连接器工具 | 当前仅数据源快照同步 |
| R2 事件收件箱 / `events.emit` | 页面收不到外部事件，也不能主动发事件 | 事件平面已有，但未接 MiniApp |
| R2 统一调度 `light_app_schedules` | 只有数据源级 `schedule_seconds`，没有应用级 cron 动作 | `run_agent` / `snapshot_publication` 等未做 |
| R2 公开看板 | Board 不能分享 | 仅个人看板 |
| R3 Bundle v2 | 不能跨主机迁移应用 | 只有同实例 zip 备份/恢复 |
| R3 认证链接 / 渠道身份 | IM 里只能看公开只读版，不能带登录态打开 | 未实现 |
| 云端模板市场 | 只能系统/专家模板 | 前端占位 |
| 按应用选择模型 | 助手/生成固定用 owning agent 默认模型 | `model_access.py` |
| `data_version` 实时刷新 | 两个浏览器不能自动看到对方的新行 | 字段已递增，前端未轮询 |
| `events.read` 能力位 | 新应用默认开启但页面无事件 API | 预留 |

### 6.2 架构风险

| 风险 | 现状 | 缓解/后续 |
|---|---|---|
| 多实例限流不全局 | 限流/锁是进程内 | 需要全局时用 DB 租约或网关限流 |
| 行数限额 check-then-insert | 多实例并发可能略超 2 万 | 当前单实例为主；多实例需条件 UPDATE/计数表 |
| 沙箱资源隔离 | 与 Dashboard 同 renderer process | 独立内容域或服务端浏览器 |
| 浏览器差异 | 已用 Chromium 回归 | Firefox/WebKit 建议补跑 |
| 公开 token 明文副本 | `secrets` 表保存一份用于复用 | 依赖 DB 与 secrets 的访问控制；轮换会作废旧链接 |
| 远程工作区写放大 | `app.html`/assets 仍走对象存储 | 行数据已入库；HTML 写入频率低 |
| 旧应用静态 frame | 渲染时拒绝并显示错误页 | 需要用户让 Agent 重写 |
| 团队 host 无 app 工具 | 团队只能通过交付物引用应用 | 设计如此，避免 host 直接操作应用 |

### 6.3 文档漂移

- 已修正：`docs/light-apps-architecture.md` 的“未实施”状态。
- 已修正：`docs/architecture-overview.md` 的工具数量、行数与 `apps/` 文件数。
- 已删除：`light-apps-multi-table-design.md`、`light-apps-assistant-and-import-design.md`（内容已并入本文 §2.2 / §2.6 / §2.7），以及过时的演示稿 `miniapp-platform-roadmap.html` / `.pptx`。
- 所有保留的 MiniApp 设计稿已加统一的“当前实现状态见 `miniapp-overview.md`”指针；设计稿只保留动机、取舍和历史记录。
- `docs/miniapp-platform-roadmap.md` 的 R1 状态准确；R2/R3 仍未开始。
- 本文作为当前状态的单一入口。

---

## 7. 代码索引

| 关注点 | 位置 |
|---|---|
| 应用服务 | `src/octop/infra/apps/service.py` |
| Schema / 行 / HTML 校验 | `src/octop/infra/apps/models.py` |
| Agent 工具 | `src/octop/infra/apps/tools.py` |
| 能力清单 | `src/octop/infra/apps/capabilities.py` |
| 沙箱 / CSP / wrapper | `src/octop/infra/apps/sandbox.py` |
| 限流 | `src/octop/infra/apps/limits.py` |
| 只读助手 | `src/octop/infra/apps/assistant.py` |
| 一次性生成 | `src/octop/infra/apps/generation.py` |
| 图表聚合 | `src/octop/infra/apps/charts.py` |
| PII | `src/octop/infra/apps/pii.py` |
| 表格分析 | `src/octop/infra/apps/inspect.py` |
| 导入解析 | `src/octop/infra/apps/importer.py` |
| 快速模式渲染 | `src/octop/infra/apps/viewgen.py` |
| 模板 | `src/octop/infra/apps/templates.py` |
| 封面 | `src/octop/infra/apps/preview.py` |
| 数据源 / 调度 | `src/octop/infra/apps/sources.py`、`scheduler.py` |
| 看板 | `src/octop/infra/apps/boards.py`、`infra/db/repos/boards.py` |
| 游戏 | `src/octop/infra/apps/game_sessions.py` |
| 对话内应用 | `src/octop/infra/apps/session.py` |
| 系统提示 | `src/octop/infra/apps/prompt.py` |
| 应用 HTTP | `src/octop/api/routers/apps.py` |
| 公开 HTTP / 宿主页 | `src/octop/api/routers/public_apps.py` |
| 看板 HTTP | `src/octop/api/routers/boards.py` |
| 游戏 HTTP | `src/octop/api/routers/game_sessions.py` |
| 应用仓储 | `src/octop/infra/db/repos/apps.py` |
| 迁移 | `src/octop/infra/db/migrations/025–030_*`、`migrate.py` |
| 专家手册 | `src/octop/infra/agents/experts/library/light-apps/AGENTS.md` |
| 前端工作台 | `dashboard/src/pages/Apps/` |
| 前端宿主 | `dashboard/src/components/MiniAppFrame/` |
| 前端对话 | `dashboard/src/pages/Chat/components/ChatDockSessionApps.tsx`、`utils/sessionApps.ts` |
| 前端 API | `dashboard/src/api/modules/apps.ts`、`boards.ts` |
| 测试 | `tests/unit/apps/`、`tests/integration/test_apps_api.py`、`test_boards_api.py`、`test_miniapp_sandbox.py` |
