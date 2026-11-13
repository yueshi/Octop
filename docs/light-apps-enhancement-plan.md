# MiniApp 增强方案

> 状态：讨论稿（2026-11-04；主体已实施，逐项状态见 §5）
>
> **当前实现状态以 [MiniApp 功能与架构现状](./miniapp-overview.md) 为准；本文保留方案、分期与实施记录。**
>
> 术语说明（2026-11-08）：本文档已全文统一为 MiniApp；此前的旧称（「小程序」「轻应用」）不再使用，仅保留于 git 历史。
>
> 范围：`feature/light-apps` 分支上的 MiniApp 能力（Agent tools + 工作区文件 + 沙箱 iframe）
>
> 定位：把 MiniApp 从“agent 生成的单页工具”升级为**可维护、可接入、可协作、可运营的数据产品**。
> HTML 仍是最后一层皮，增强重点在底座。
>
> 相关：[MiniApp 数据与协作架构方案](./light-apps-architecture.md)（数据层 D1–D12 的既有设计，本方案沿用）；
> 多表、助手与导入的现状见 [MiniApp 功能与架构现状](./miniapp-overview.md)。

---

## 1. 背景与现状问题

当前实现（`src/octop/infra/apps/`）：

- 元数据与协作 ACL 在控制面 DB（`light_apps` / `light_app_collaborators` / `light_app_tables`）；
- 内容文件在 Agent 工作区：`apps/<app_id>/{app.html,data.csv,schema.json,manifest.json}`，
  附加表在 `tables/<key>.csv`；
- 行数据仍是工作区 CSV，追加 = 读全量 → 校验 → 重写整个文件；并发保护只在进程内；
- 导入支持 CSV/TSV/JSON/xlsx/xls，但只读第一个 sheet；
- 字段只能追加，不能改名、删除或改类型；
- 查看界面只有 iframe，没有可直接维护数据的表格视图；
- 工具侧只允许 agent owner，HTTP 侧按应用 ACL，两个口径不一致；
- 团队 host 不挂 app 工具；任务交付物只有 file/link/text；专家发布快照不含应用；
- 删除 agent 会级联删除应用元数据，应用不能迁移；
- 没有应用级资产通道（封面/图片），也没有公开分享链接。

| # | 问题 | 证据 |
|---|---|---|
| 1 | 行数据在 CSV，写放大；锁只在进程内 | `apps/service.py:66-76`、`docs/light-apps-architecture.md` §1 |
| 2 | Schema 只能追加，改错要重建 | `light-apps/AGENTS.md`、`extend_schema` |
| 3 | 无通用表格视图，用户无法直接维护数据 | `dashboard/src/pages/Apps/Detail.tsx` |
| 4 | Excel 只读首 sheet，无 dry-run、无导出 | `apps/importer.py:73` |
| 5 | 工具 owner-only vs HTTP ACL | `apps/tools.py:82` vs `api/routers/apps.py:215` |
| 6 | 团队/交付物/专家模板与应用断开 | `agents/manager.py:3857`、`teams/board_tools.py:359`、`experts/publish.py:277-293` |
| 7 | 删 agent 即级联删应用，不能迁移 | `025_light_apps.sql:8`、`agents/manager.py:1092` |
| 8 | 无资产通道、无公开链接 | `dashboard/src/pages/Apps/appCsp.ts`、`light-apps-architecture.md` §9（未实施） |

---

## 2. 目标与非目标

### 2.1 目标

- **数据可靠**：多人/多实例下不丢行、可查询、可导出。
- **可维护**：用户能直接改数据、改结构，不必让 agent 重建。
- **可接入**：文件、连接器、网页取数统一成“数据源”。
- **可协作**：一套 ACL；应用可作为团队/数字员工的交付物。
- **可沉淀**：专家能带应用壳，应用能迁移、归档、备份。

### 2.2 非目标

- 实时协同编辑、CRDT、行级/字段级权限。
- 复杂公式、审批流、流程引擎、多页应用。
- 外链 CDN 白名单、任意后端代码。
- 应用市场与计费。

---

## 3. 设计原则

1. **应用离线，网络在工具层**：iframe 永远只读自己的表，不直连外部。
2. **数据是资产，HTML 是视图**：优先投资数据可靠性，而不是页面表现力。
3. **一套 ACL，两种入口**：HTTP 与 agent 工具共用同一个权限解析器。
4. **默认安全**：CSP、HITL、写入时外链校验，三道闸门都不放开。
5. **专家生产，应用沉淀**：不新增 agent kind；专家是生产者，应用是产物。

---

## 4. 能力设计

### 4.1 数据层（P1）

**方案**

- 新增 `light_app_rows`：`row_id`、`app_id`、`table_key`、`values_json`、`version`、`source`、
  `source_id`、`source_key`、`client_request_id`、`created_by`、时间戳。
- `light_app_tables` 增加 `schema_json`、`schema_version`、`data_version`；
  `light_apps` 增加 `storage_version`（1=文件，2=DB）。
- 追加 = 单行 INSERT + 计数更新；修改/删除用行级 CAS；追加支持 `client_request_id` 幂等。
- 旧应用懒迁移：首次读取时把 CSV 灌入 DB，`storage_version` 置 2；失败保留 v1 可重试。
- `data.csv` 降级为导出格式，不再承担并发写。
- **Schema 演进**：支持改名、删除、类型变更；改名重写 `values_json` 的键，类型变更先全量校验，
  删除列需二次确认。
- 导出：`GET /api/apps/{id}/export?table=&format=csv` 流式返回。

**验收**

- 10 个并发追加全部落库，无覆盖。
- 旧应用迁移后行数与 CSV 一致；重复迁移不重复插入。
- stale version → 409；重复 `client_request_id` → 只落一行。
- 改名/类型变更后旧数据可读，非法值有明确报告。

### 4.2 交互层（P0/P1）

- **通用表格视图**：按 table 分 tab，支持分页、筛选、排序、行内编辑、新增/删除行、列显示；
  `edit_data` 可改行，`edit_structure` 可改结构。
- **SDK 扩展**（`dashboard/src/pages/Apps/Detail.tsx` 的 `APP_SDK`）：
  - `octopApp.asset(name)` → 父页面鉴权取回资产，返回 `blob:` URL；
  - `octopApp.openExternal(url)` → 父页面校验 `http/https` 并确认后新开标签；
  - `octopApp.export(table)`、`octopApp.me()`（返回当前权限）、`octopApp.notify(text)`。
- **CSP 安全组件**：宿主内联注入一个固定版本的小型图表 helper（`octopChart`），模型不引 CDN
  也能画图；图标走内联 SVG。
- **写入时校验**：`validate_html` 增加外链扫描（`src/href/action`、`@import`、`fetch(`、
  `WebSocket`），拒绝并给出修复提示；把白屏问题从用户侧提前到工具侧。
- 预览时监听 `securitypolicyviolation`，显示被拦资源，便于诊断。

### 4.3 接入层（P1）

- **`app_inspect_file`**（服务端只读工具）：输入工作区路径，返回 sheet 列表、表头候选行、
  前 N 行样本、列类型推断、空值/重复统计、疑似 PII 列；不依赖沙箱环境。
- **导入增强**：`sheet`、`header_row`、多文件合并、`dry_run`、`upsert_key`、错误报告；
  保留 20k 行 / 5MB 上限。
- **资产管道**：
  - `POST /api/apps/{id}/assets` 上传（png/jpeg/webp/gif，5MB 上限，暂不支持 SVG）；
  - `GET /api/apps/{id}/assets/{name}` 用**应用 ACL** 鉴权；
  - agent 工具 `app_save_asset(app_id, url|path)` 下载封面到 `apps/<id>/assets/`。
- **数据源增强**：允许无凭证的只读连接器；外部源补去重键与增量同步；新增通用 HTTP 取数
  （URL + 选择器/JSON-LD）作为 `http_extract` 连接器。

#### 4.3.1 Excel 分析 → 生成 MiniApp （重点）

这条能力不是一个“导入按钮”，而是贯穿**接入层 + 交互层 + Agent 协作层**的完整链路。
关键原则：**解析用代码，建议用模型，决定权在用户**。模型不直接读整张 Excel，只看服务端
返回的紧凑统计和样本。

**目标流程**

```
上传 1~N 个 Excel
   ↓  ① 上传（inbound/）
服务端确定性解析 + 统计
   ↓  ② app_inspect_file（sheet、表头、类型、空值、重复、PII）
Agent 给出建应用方案
   ↓  ③ 表拆分 + 字段建议 + 去重键 + 清洗说明 + 2~3 个问题
用户确认 / 调整
   ↓  ④ 结构化确认卡（不是纯文本）
创建应用与表
   ↓  ⑤ app_create / app_add_table
dry-run 导入
   ↓  ⑥ 只校验不写入，返回 would_import / skipped / errors
正式导入
   ↓  ⑦ 幂等 upsert + 来源列
写界面
   ↓  ⑧ app_write_html
核对
   ↓  ⑨ 表格视图 / app_read_data / 助手问答
```

**`app_inspect_file` 输出**

```json
{
  "file": "inbound/2026_q3.xlsx",
  "size_bytes": 123456,
  "sheets": [{
    "name": "9月",
    "rows": 1200,
    "columns": 8,
    "header_row": 2,
    "header": ["工号", "姓名", "部门", "..."],
    "sample_rows": [["E001", "张悦", "烘焙房", "..."]],
    "columns_meta": [{
      "source": "工号",
      "name_suggestion": "employee_no",
      "type": "text",
      "non_empty_ratio": 1.0,
      "distinct": 1200,
      "samples": ["E001", "E002"],
      "pii": false
    }],
    "warnings": ["merged_header", "totals_row_detected", "formula_cells"]
  }],
  "pii": [{"sheet": "9月", "column": "身份证号", "kind": "id_card"}]
}
```

必须处理：

- **表头检测**：跳过前导说明行、合并单元格、多级表头；
- **合计行**：识别“合计/小计/总计”并默认排除；
- **公式列**：`data_only=True` 读缓存值，空值要提示；
- **多 sheet**：全部列出，默认建议每个 sheet 一张表或合并加“月份”列；
- **大文件**：只扫前 20k 行做统计，超限给出“聚合/分批”建议；
- **PII**：身份证、银行卡、手机号等列标记，默认不导入、不导出、不分享。

**建应用方案（结构化）**

```json
{
  "app": {"name": "薪资台账", "description": "按月记录工资与社保"},
  "tables": [{
    "key": "main",
    "label": "工资明细",
    "source": {"file": "inbound/2026_q3.xlsx", "sheet": "9月", "header_row": 2},
    "columns": [
      {"name": "employee_no", "label": "工号", "type": "text", "required": true},
      {"name": "name", "label": "姓名", "type": "text", "required": true},
      {"name": "base_salary", "label": "基本工资", "type": "number"}
    ],
    "dedupe_key": ["employee_no", "month"]
  }],
  "warnings": ["身份证号列建议不导入", "9 月表含 1 行合计已排除"],
  "questions": ["以后每月还会更新吗？", "谁会一起看？", "哪些列敏感？"]
}
```

确认卡需要三个动作：**确认创建 / 调整字段 / 只分析不建应用**。第三项很重要——用户可能只想要
一次分析，不该被硬塞一个应用。

**导入增强**

在现有 `POST /api/apps/{app_id}/tables/{table_key}/import` 上增加：

| 字段 | 作用 |
|---|---|
| `sheet` | 指定 worksheet，解决“只读第一个 sheet” |
| `header_row` | 表头所在行，解决前导说明/多级表头 |
| `dry_run` | 只校验不写入，返回 `would_import / skipped / errors` |
| `upsert_key` | 按业务键去重（工号+月份、订单号、ISBN），重复导入更新已有行 |
| `paths` | 一次导入多个工作区文件到同一张表，按顺序合并 |
| `source_columns` | 记录来源文件/sheet，写进行元数据便于追溯（行已带 `source`） |

导入结果固定返回：`imported / updated / skipped / errors / warnings`。批量补全或按月追加时，
失败清单要能续跑。

**多文件 / 多 sheet 的归并规则**

- **同一实体按月/按文件拆分**：合并到一张表，加 `source_file`、`source_sheet`、`period` 列，
  用 `upsert_key` 去重；
- **不同实体**（明细 vs 汇总、人员 vs 订单）：拆成多张表，`app_add_table`；
- **不要**按“每个文件一张表”机械拆分，否则用户要在多个 tab 之间对账。

**UI 生成**

台账类应用默认生成三块：**列表 + 新增表单 + 汇总**。提示词里固化：

- 加载时先 `listRows()`，不把数据写死；
- 日期用服务器时区展示；
- 金额右对齐、合计置顶或置底；
- 空态、错误提示、移动端自适应；
- 敏感列默认不展示，或按权限隐藏。

**提示词 / 流程固化**

在 `light-apps/AGENTS.md` 增加“台账导入”章节：

1. 先 `app_inspect_file`，不要用 `read_file` 读 xlsx；
2. 用 3–5 句话总结：几个 sheet、多少行、表头问题、PII；
3. 给字段建议，并解释为什么这样拆表/选类型；
4. 问三个问题：会不会持续更新、谁一起用、哪些列敏感；
5. 用户确认后再 `app_create`；
6. 先 `dry_run` 导入，把 `would_import/skipped/errors` 给用户看；
7. 正式导入后核对条数，再写界面。

同时保留 `SOUL.md` 里的判断：**不值得建就别硬造**；一次性分析直接出报告。

### 4.4 权限层（P0/P1）

- **统一 ACL**：抽出 `app_acl` 解析器，HTTP 与工具共用；角色保持
  `view / edit_data / edit_structure`，owner/admin 绕过。
- **共享 agent 语义**：agent 可访问 ≠ app 可访问；创建应用要求 agent 访问权，读写按 app ACL。
- **PII 策略**：列级标记（身份证、银行卡、手机号等），导入/分享/导出前提示，默认不导出。
- **配额**：应用数、行数、存储、助手 token；沿用现有硬上限并补按应用统计。
- **公开链接（P3）**：只读 token、可撤销、`/p/{token}` 宿主页，按
  `light-apps-architecture.md` §9 执行，不开放写入。

### 4.5 Agent 协作层（P2）

- **应用即交付物**：`goal_deliverables.artifact_type` 增加 `app`，`artifact_ref=app_id`；
  数字员工自报，团队 host 汇总 roster 成员的应用；看板卡片可跳转 `/apps/{id}`。
- **专家自带应用壳**：
  - 专家目录新增 `app_templates/<name>/{schema.json, app.html}`（**不含 data.csv**）；
  - 发布快照允许打包 `app_templates/`，安装后 seed 到工作区；
  - 用户可从模板一键建应用，数据为空。
- **自动写入**：cron/IM/团队写入带 `source`、`source_key`，幂等 upsert，表格视图显示来源列。
- **助手产品化**：回答引用 `row_id`、可关闭、可选模型、用量计入 per-app 统计；保持只读。

### 4.6 运维层（P3）

- **指标**：应用打开、活跃用户、每周新增行、助手提问、导出次数、CSP 违规；走 `METRICS`。
- **备份/恢复**：按应用导出 zip（html/schema/csv/assets/manifest），支持导入恢复。
- **审计**：分享、导出、公开链接访问记录。

---

## 5. 分期与工作量

| 阶段 | 内容 | 人日 | 依赖 |
|---|---|---|---|
| **P0 质量与边界 + Excel MVP** | 写入校验、统一 ACL、生命周期保护（删除提示/迁移）、资产通道、SDK 扩展、`app_inspect_file`、导入 `sheet/header_row/dry_run`、提示词流程、文本确认 | 11–15 | 无 |
| **P1 数据与维护** | `light_app_rows` + CAS + 幂等 + 懒迁移、表格视图、Schema 演进、导出、结构化确认卡、`upsert_key` + 来源列 + PII 标记 + 多文件归并 | 20–26 | P0 |
| **P2 接入与协作** | 数据源增强、交付物 app、专家应用壳、自动写入来源、助手引用 | 12–16 | P1 |
| **P3 运营与传播** | 公开链接、指标、审计、备份恢复、按月自动追加与失败续跑 | 8–10 | P2 |
| | **合计** | **51–67** | |

建议顺序：P0 先止血并打通 Excel 冷启动 → P1 做数据底座 → P2 打通专家/团队 → P3 做传播与运营。

各阶段的验收 case 见 §7.4：P0 覆盖 Case 2 与 Case 1 的权限/并发部分；P1 覆盖 Case 1/3/4/5；
P2 覆盖 Case 6/7/8；P3 覆盖后置项。

### 5.1 P0 进度

| 工作包 | 状态 | 说明 |
|---|---|---|
| WS1 `app_inspect_file` | ✅ 已实现 | 新增 `src/octop/infra/apps/inspect.py`；工具 + 目录登记 + i18n + 提示词 |
| WS2 导入 `sheet/header_row/dry_run` | ✅ 已实现 | 解析统一到 `inspect.read_sheets`；`ImportResult` 增 `would_import/dry_run`；API 与工具同步 |
| WS3 HTML 写入时外链校验 | ✅ 已实现 | `find_external_refs()` + `validate_html`；附 `octopApp.openExternal(url)` SDK |
| WS4 统一 ACL | ✅ 已实现 | 工具侧 `_get_for_actor` 与应用 ACL 同源；共享 agent 可创建、协作者可追加 |
| WS5 生命周期保护与交接 | ✅ 已实现 | 删除守卫 `AGENT_HAS_APPS`（409 + `force`）；`POST /api/apps/{id}/transfer` |
| WS6 资产通道 + SDK | ✅ 已实现 | 资产上传/读取（5MB、PNG/JPEG/WebP/GIF）、`app_save_asset`、`octopApp.asset` |
| WS7 提示词、i18n、文案 | ✅ 已实现 | 工具表、台账导入流程、导入参数速查表、素材章节、系统提示词、manifest 快速提示 |
| WS8 集成与验收 | 🟡 进行中 | 单测/集成已补；全量 unit 4843 passed；待 `make all` 与手工冒烟 |

**P1 进度（2026-11-05）**

| 项目 | 状态 | 说明 |
|---|---|---|
| 行数据入库 + CAS + 幂等 + 懒迁移 | ✅ | `light_app_rows`（028 迁移）、行级版本、`client_request_id`、首读迁移 |
| 通用表格视图 | ✅ | 应用详情「数据」tab：分页、搜索、行内编辑、新增、删除、导出 |
| Schema 演进 | ✅ | `app_alter_schema` / `PATCH /apps/{id}/schema`：改名、删除、改类型，整表校验后迁移 |
| 导出 | ✅ | `GET /api/apps/{id}/export`，从 DB 渲染 CSV 快照 |
| 幂等追加 / 来源去重 | ✅ | `client_request_id`；导入 `upsert_key`（按列组合更新已有行）；`source_key` upsert |
| PII 强制策略 | ✅ | schema 列级 `pii` 标记；导入默认 `include_pii=false` 跳过敏感列（必填列需显式确认）；viewer/公开链接/助手读与导出脱敏；备份要求 `edit_structure`；分享/公开链接前端告警 |
| 结构化确认卡 | ✅ | `app_inspect_file` 返回 `octop_ui: app_inspect_card`；对话内可编辑字段名/类型/必填/导入列并一键生成确认消息 |
| 多文件归并 | ✅ | `import paths=[...]`（API/工具）按顺序合并到同一张表 |

**P1/P2/P3 补强记录（2026-11-05）**

- PII：`Column.pii` 持久化到 schema；导入自动检测并标记，`include_pii=false` 时跳过敏感列（必填敏感列直接拒绝并提示）；viewer、公开链接、助手读与导出统一脱敏；备份改为 `edit_structure`。
- 确认卡：`app_inspect_file` 输出 `octop_ui` 卡；`AppInspectCard` 支持选 sheet、改字段名/类型/必填、勾选导入列，确认后生成结构化消息交给 agent。
- 增量：外部源支持 `cursor_arg` / `cursor_path`，游标存回 `params_json`；工作台数据源弹窗可配置。
- 续跑：导入结果新增 `pii_columns` / `blocked_pii` / `failed_rows`；失败行可从工作台下载为可直接重导的 JSON 数组。

**P1 实现记录（2026-11-05）**

- 新表 `light_app_rows`：`app_id + table_key + id` 分页索引、`client_request_id` 与 `source_key` 部分唯一索引。
- 新 API：`PATCH/DELETE /api/apps/{id}/rows/{row_id}`（CAS）、`GET /api/apps/{id}/export`、`PATCH /api/apps/{id}/schema`；导入支持 `upsert_key`。
- 新工具：`app_update_row`、`app_delete_row`、`app_alter_schema`；`app_read_data` 返回 `row_meta`，`app_append_row` 支持 `client_request_id`，`app_import_rows` 支持 `upsert_key`。
- 旧 CSV 应用在首次读/写时自动迁移；新应用直接写 DB（`storage_version=2`），`data.csv` 只在创建时留空快照。
- 表格视图不依赖 iframe：同一页面即可维护数据，viewer 只读；支持搜索、行内编辑、删除、导出。
- 行元数据带 `source`（manual/import/migration/connector/agent）；导入可多文件合并、按 `upsert_key` 更新已有行。
- 导入结果与 inspect 报告都会标记疑似 PII 列；硬性拒绝/脱敏留到后续。

**P3 实现记录（2026-11-05）**

- 备份格式：`manifest.json`（format/version/app/tables）+ `app.html` + `tables/<key>/schema.json` + `tables/<key>/data.csv` + `assets/<name>`；导出要求 `view`，写审计 `light_app.backup`。
- 恢复：`POST /apps/restore?agent_id=…`，上传 zip 后先校验 format/version、表数量与累计解压预算（256 MB），再创建新应用并逐表导入；失败时删除半成品。恢复要求目标 agent 可访问，写审计 `light_app.restore`。
- 恢复不覆盖已有应用、不复用 `app_id`；源应用保持不变。前端在应用详情提供「备份」，在列表页提供「恢复备份」。
- 定时数据源支持 `params.upsert_key`：保存时校验并持久化，`workspace_file` 与 `external` 两类运行都把它传给 `import_rows`，按月追加不会重复。

**P3 进度（2026-11-05）**

| 项目 | 状态 | 说明 |
|---|---|---|
| 只读公开链接 | ✅ | `029_light_app_publications`；`POST/GET/DELETE /apps/{id}/publication`；`/api/public/apps/{token}` + `/p/{token}` 宿主页；撤销/过期 404；不注入 JWT |
| 指标 | ✅ | `light_app_open/row_write/export/assistant/public_view` 计数器，`METRICS.snapshot` 暴露 |
| 审计 | ✅ | 分享、导出、公开链接创建/撤销、公开访问写 `audit_log` |
| 备份/恢复 | ✅ | `GET /apps/{id}/backup` 导出 zip（HTML/schema/CSV/assets/manifest），`POST /apps/restore` 校验后恢复为新应用；工作台提供「备份」「恢复备份」入口 |
| 按月自动追加/失败续跑 | ✅ | 调度器、失败退避、`upsert_key` 幂等；导入返回 `failed_rows`（含映射值和错误，最多 200 行），工作台可下载为 JSON，修正后按 `upsert_key` 重导续跑 |

**P2 进度（2026-11-05）**

| 项目 | 状态 | 说明 |
|---|---|---|
| 应用即交付物 | ✅ | `goal_deliverables.artifact_type="app"`；团队校验 roster 成员/数字员工自有应用；看板交付物可跳转 `/apps/{id}` |
| 专家自带应用壳 | ✅ | `app_templates/<name>/{schema.json,app.html}` 随专家发布/安装；`GET /apps/templates`、`POST /apps/from-template`、`app_create_from_template`；工作台「从模板创建」 |
| 自动写入来源 | ✅ | agent 写入标记 `source=agent`；`row_meta.source` 暴露，表格视图显示来源列 |
| 助手引用 | ✅ 引用 / 🟡 模型选择 | 助手查询返回 `row_meta`，提示词要求引用 `row_id`；前端显示引用并切换到数据 tab；敏感列对助手脱敏；模型选择未做 |
| 数据源增强 | ✅ 无凭证 + HTTP JSON + 增量 | `auth_kind="none"` + `http-extract`；数据源支持 `cursor_arg`/`cursor_path`，成功后持久化游标，下次运行自动带入 |

**P2 实现记录（2026-11-05）**

- `board_tools.deliverable_update` 支持 `artifact_type="app"`：团队 host 只能挂 host 或 roster 成员的应用，数字员工只能挂自己的应用。
- 专家快照新增 `app_templates/**`（只打包 `schema.json`/`app.html`，不含数据/素材）；安装时随工作区 seed。
- 新增 `app_create_from_template` 工具与模板创建 API；`light-apps` 专家附带 `reading-tracker` 示例壳；工作台新增「从模板创建」入口。
- 表格视图新增来源列；应用助手查询结果带 `row_meta`。
- 数据源允许 `auth_kind="none"` 的只读连接器；通用 HTTP 取数与增量同步仍未做。

**P0 实现记录（2026-11-05）**

- 新工具：`app_inspect_file`（表格检查）、`app_save_asset`（素材保存）。
- 导入：`sheet` / `header_row` / `dry_run` 贯通 API 与工具；`ImportResult` 增 `would_import` / `dry_run`。
- ACL：工具侧 `_get_for_actor` 与应用 ACL 同源；共享 agent 可创建，协作者可追加，结构变更仍 owner-only。
- 生命周期：删除有应用的 agent 返回 `AGENT_HAS_APPS`（409），需 `force`；`POST /api/apps/{id}/transfer` 复制文件与素材后迁移。
- 资产：`POST/GET /api/apps/{id}/assets`（5MB，PNG/JPEG/WebP/GIF，不含 SVG）；SDK 增 `octopApp.asset` / `openExternal`。
- 写入校验：`find_external_refs` 在 `app_write_html` 阶段拒绝 CDN、外链图片、`fetch()`、外链 `<a>`。
- 提示词：`light-apps/AGENTS.md` 增台账导入 7 步流程、导入参数速查、素材章节；manifest 增「台账导入」快速提示。

---

## 6. 数据模型变更

- 新增迁移 `028_light_app_rows.sql` / `.pg.sql`，并同步 `migrate.py` 幂等 helper；
  测试版本断言从 27 → 28。
- P3 再新增 `029_light_app_publications.sql` / `.pg.sql`，版本断言 28 → 29。
- 合并上游前置：上游 `develop` 已占用 022–024（`skill_package_shared`、`user_groups`、
  `user_group_profile`），本分支原 022–026 已整体重编号为 **025–029**。旧编号库
  （v22–v26）靠 `migrate.py` 的幂等 helper 升级；同时新增
  `_ensure_user_groups_schema` / `_ensure_upstream_column_bridge`，为跳过上游
  022–024 的库补建 `user_group*`、`resource_group_grant`、`skill_packages.shared`
  及 `knowledge_bases` / `bridge_connections` 的 `icon_url`、`shared` 列。
- 升级验证：旧分支 v24 实库副本（13 个应用）与「上游 022–024 已应用」的模拟
  v24 库均升级到 v29，应用数据保留，`user_group` / `skill_packages.shared` /
  `knowledge_bases.icon_url` 均补齐。
- 主要 DDL：`light_app_rows`（含两个部分唯一索引）、`light_app_tables` 加
  `schema_json/schema_version/data_version`、`light_apps` 加 `storage_version`；
  `goal_deliverables.artifact_type` 无需改表。
- `app_templates/` 走文件目录，不进 DB。

---

## 7. 验收与测试

### 7.1 通用

- `make all` 绿；新增 `tests/unit/apps`、`tests/integration/test_apps_api.py` 用例。
- 迁移：v27 → v28，旧 CSV 应用迁移后行数一致、幂等。
- 并发：并行 append 不丢行；CAS 冲突 409；幂等键只落一行。
- 安全：含外链的 HTML 被拒；CSP 违规能上报；公开页无 JWT。

### 7.2 Excel 场景 fixture

1. 干净单 sheet 台账；
2. 多 sheet 按月台账；
3. 合并单元格 + 两行表头；
4. 含“合计”行；
5. 含公式列；
6. GBK 编码 CSV；
7. 25,000 行（应给出聚合/分批建议，而不是静默截断）；
8. 含身份证/银行卡列（应标记且默认不导入）。

断言：

- inspect 的 sheet、表头行、类型推断正确；
- dry-run 的 `would_import/skipped` 与正式导入一致；
- 同一文件重复导入不产生重复行；
- 合计行、空行、表头说明行被排除；
- 确认卡能“只分析不建应用”；
- 建出的应用能通过表格视图改行、助手能问答；
- `make all` 绿。

### 7.3 端到端（读书工作台）

上传 Excel → inspect → 建应用 → dry-run 导入 → 正式导入 → 表格改一行 → 分享 viewer →
封面通过 asset 加载 → 助手回答并引用行。

### 7.4 典型场景验收矩阵

| # | 典型场景 | 阶段 | 验证能力 | 关键断言 |
|---|---|---|---|---|
| 1 | 多人协作排班表：owner 建应用，两个协作者分别为 `edit_data` 与 `view`，同时追加/改行 | P0–P1 | 统一 ACL、行级 CAS、表格视图、冲突提示 | 10 次并发追加不丢行；stale 版本 409；viewer 的 PATCH/DELETE 403；刷新能看到对方新增 |
| 2 | 应用交接与删除保护：专家 A 的应用迁移给专家 B；删除 A 时保留应用 | P0 | 生命周期解耦、transfer、删除守卫 | 迁移后 `agent_id=B` 且行/文件完整；删除 A 后应用仍可打开；未迁移时必须显式确认/归档，不能静默级联 |
| 3 | 大台账（>20k 行）与失败续跑：5 万行 Excel，超限提示；1000 行导入中 50 行失败，修正后只重导失败行 | P1 | inspect 限额、dry-run、upsert、错误报告、分批导入 | 不静默截断；dry-run 与正式结果一致；重复导入不重复；失败行可定位、可续跑 |
| 4 | 敏感台账（薪资/客户）：导入含身份证/银行卡的工资表，分享给 viewer，尝试导出 | P1 | PII 策略、字段级展示控制、导出/分享审计 | 默认不导入敏感列（可显式勾选）；viewer 通过 API 和 HTML 都拿不到；导出被阻止或脱敏；审计有记录 |
| 5 | 多表关联（订单 + 明细）：一个应用两张表，导入两个 sheet，按订单号筛选并汇总 | P1 | `app_add_table`、多表导入、SDK `tables()`、表格视图 tab | 两表行数独立正确；跨表筛选/汇总正确；单表 `replace` 不影响另一张表 |
| 6 | Agent 自动写入的日报/巡检：数字员工每天 09:00 从连接器取数写一行；重复运行不重复写；用户看趋势并问助手 | P2 | cron + 连接器 + 幂等 upsert + `source` 来源列 + 助手引用 | 同一天重复运行只有一行；`source=agent/connector` 可见；助手回答引用行；失败/预算可见 |
| 7 | 专家自带应用壳：安装“读书工作台”专家，自带 schema+HTML 模板；一键建空应用并添加书籍 | P2 | `app_templates` 发布/安装、模板建应用、资产通道 | 安装后模板存在；建出的应用字段正确、无数据；封面显示；模板不含任何用户数据 |
| 8 | 团队交付物 = 应用：团队目标“Q3 运营报告”，成员做看板应用，host 挂到交付物 | P2 | `artifact_type=app`、roster 校验、看板链接 | 非成员的应用被拒；交付物显示 app 名称/链接；任务完成后可追溯 |

**后置（P3）**

- 公开只读分享：`/p/{token}` 可打开，撤销后 404，页面无 JWT。
- 移动端填写：打卡/巡检在手机上追加行，布局自适应。
- 助手引用行：回答带 `row_id` 引用，权限不足时不返回。

**不建议加入**

复杂审批流、实时协同编辑、公开可写表单、大二进制编辑——会显著扩大范围，且不属于 MiniApp 主线。

---

## 8. 风险与取舍

| 风险 | 缓解 |
|---|---|
| 数据迁移丢行 | 先备份；懒迁移 + 失败保留 v1；提供批量迁移与回滚脚本 |
| SQLite 多 worker 写竞争 | 短事务；建议单 worker；多 worker 用 PostgreSQL |
| 范围膨胀 | 严格按 P0–P3；实时协同/行级权限明确不做 |
| 公开链接泄露 | 默认关闭、随机 token、可撤销、限流、发布前提示 |
| 专家模板带出用户数据 | 只打包 `app_templates/`，永不打包 `apps/` |
| 模型直接读 Excel 撑爆上下文 | 强制走 `app_inspect_file`，只返回统计和样本 |
| 表头识别错误导致 schema 错 | 确认卡展示前 5 行样本和推断类型，用户可改 |
| 大文件/超行数 | 上限前置校验，给聚合/分批/只导近 N 行选项 |
| PII 泄露 | 列级标记 + 默认不导入/不导出 + 分享前提示 |
| 重复导入 | `upsert_key` + `client_request_id` 幂等 |
| 第三方取数合规 | 限速、缓存、遵守 robots/ToS；豆瓣仅作用户自配来源 |

---

## 9. 不做清单

- 实时协同编辑 / CRDT。
- 行级/字段级权限（等真实场景出现再做）。
- 复杂公式、审批流、流程引擎。
- 任意后端代码、多页应用。
- 外链 CDN 白名单。
- 应用市场与计费。

---

## 10. 关键文件索引

| 关注点 | 位置 |
|---|---|
| 数据/服务 | `src/octop/infra/apps/service.py`、`models.py`、`tools.py`、`importer.py` |
| API | `src/octop/api/routers/apps.py` |
| 仓储 | `src/octop/infra/db/repos/apps.py` |
| 迁移 | `src/octop/infra/db/migrations/025–029_*`、`migrate.py` |
| 前端 | `dashboard/src/pages/Apps/{index,Detail}.tsx`、`appCsp.ts`、`dashboard/src/api/modules/apps.ts` |
| Agent 接线 | `src/octop/infra/agents/manager.py:3857-3870` |
| 团队交付 | `src/octop/infra/agents/teams/board_tools.py`、`api/routers/goals.py` |
| 专家发布 | `src/octop/infra/agents/experts/publish.py` |
| 提示词/模板 | `src/octop/infra/agents/experts/library/light-apps/{SOUL.md,AGENTS.md}` |

---

## 11. 附录：P0 详细实现方案与步骤

### 11.0 范围与边界

P0 的目标是**止血 + 打通 Excel 冷启动**，并且**不引入数据库迁移**（仍停留在 v24），
因此可以独立发布。

| 纳入 P0 | 不纳入 P0（P1+） |
|---|---|
| HTML 写入时外链校验 | 行数据入库 + CAS + 幂等 |
| 统一 ACL（HTTP 与工具同一套） | 通用表格视图 |
| 应用交接 / 删除保护 | Schema 演进（改名/删除/类型变更） |
| 资产通道（图片/封面）+ SDK | 结构化确认卡 |
| `app_inspect_file` | `upsert_key`、来源列、PII 强制策略 |
| 导入 `sheet/header_row/dry_run` | 多文件归并、失败续跑 |
| 提示词流程 + 文本确认 | 交付物 app、专家应用壳、公开链接 |

> Case 1 的“并发追加不丢行”属于 P1（行数据入库）；P0 只验证 ACL 与权限边界。

---

### 11.1 WS1：`app_inspect_file`（3–4 人日）

**目标**

用户上传 Excel 后，agent 能拿到一份紧凑、确定性的结构统计，而不是把整张表塞进上下文。

**改动文件**

- 新增 `src/octop/infra/apps/inspect.py`
- 重构 `src/octop/infra/apps/importer.py`
- `src/octop/infra/apps/service.py`：新增 `inspect_file(agent_id, path)`
- `src/octop/infra/apps/tools.py`：新增工具
- `src/octop/infra/agents/settings/tool_catalog.py`：登记工具
- `src/octop/i18n/domains/tools.py` + `en.json` / `zh.json`
- 测试：`tests/unit/apps/test_inspect.py`、`test_importer.py`

**实施步骤**

1. **抽出 workbook 解析层**

   ```python
   @dataclass(frozen=True)
   class SheetRows:
       name: str
       header_row: int          # 0-based
       headers: list[str]
       rows: list[list[str]]
       warnings: list[str]
   ```

   `parse_workbook(filename, data) -> list[SheetRows]`；
   `parse_import_file(...)` 保留，默认取第一个 sheet。

2. **表头检测**
   - 扫描前 10 行，按 `非空比例 × 0.5 + 去重比例 × 0.3 + 文本比例 × 0.2` 打分；
   - 最高分低于阈值时退回第 0 行，并记 `blank_header` 警告；
   - 合并单元格：文件 ≤10MB 时用普通模式读 `merged_cells`；大文件用 `read_only=True` 并降级告警。

3. **列统计**
   - 最多扫 20k 行；`non_empty_ratio`、`distinct`（集合上限 10k）、`samples`（前 5 个）、`max_length`；
   - 类型推断：`boolean → integer → number → date/datetime → enum → text/string`；
   - `enum`：distinct ≤ 12 且非空比例 > 0.9；`text`：平均长度 > 50。

4. **清洗识别**
   - 合计行：首列包含 `合计/小计/总计/汇总`；
   - 公式列：`data_only=False` 抽查前 20 行 `cell.data_type == "f"`；
   - 空列/重复表头/多级表头。

5. **PII 标记**
   - 表头关键词：身份证、银行卡、手机号、工资、薪资、住址；
   - 值正则：18 位身份证、16–19 位银行卡、11 位手机号；
   - 只标记，不阻断（强制策略在 P1）。

6. **工具封装**

   ```python
   async def app_inspect_file(path: str) -> str
   ```

   - `_tool_ctx()` 取 `agent_id/user_id`；
   - 校验 `is_agent_accessible(agent_id, user_id)`；
   - `_safe_import_path(path)`；
   - 返回 JSON 字符串（失败返回 `{"error": ...}`）。

7. **测试**
   - 单 sheet、多 sheet、合并表头、两行表头、合计行、公式列、GBK CSV、25k 行、PII 列；
   - 断言 sheet 名、header_row、类型、PII 标记、warnings。

**验收**

- 不依赖应用存在即可调用；
- 25k 行返回 `too_large` 警告，不静默截断；
- 输出 < 20KB，模型可直接消费。

---

### 11.2 WS2：导入增强（2–3 人日）

**目标**

导入能指定 sheet 和表头行，并支持 dry-run；结果可核对后再落库。

**改动文件**

- `src/octop/infra/apps/importer.py`
- `src/octop/infra/apps/service.py`
- `src/octop/infra/apps/tools.py`
- `src/octop/api/routers/apps.py`
- `dashboard/src/api/modules/apps.ts`（可选，先把字段透出）
- 测试：`tests/unit/apps/test_service.py`、`tests/integration/test_apps_api.py`

**实施步骤**

1. `parse_import_file(filename, data, *, sheet=None, header_row=None)`：
   - `sheet` 支持名称或索引；
   - `header_row` 之后才开始读数据；
   - 保留默认行为（第一个 sheet、第 0 行）。

2. `ImportBody` 增加：

   ```python
   sheet: str | None = None
   header_row: int | None = Field(default=None, ge=0, le=100)
   dry_run: bool = False
   ```

3. `service.import_from_file(...)` 透传；`import_rows(..., dry_run=True)`：
   - 完成全部校验与计数，但**不写文件**；
   - `replace` 的 dry-run 绝不能清表；
   - 返回 `imported / would_import / skipped / errors / warnings / dry_run`。

4. 工具 `app_import_rows` 增加 `sheet`、`header_row`、`dry_run` 参数。

5. 测试：
   - dry-run 与正式导入计数一致；
   - dry-run 后文件字节不变；
   - 多 sheet 指定第二张表；
   - 表头行偏移正确。

**验收**

- 1000 行中 50 行失败时，dry-run 返回 950/50，正式导入一致；
- 重复 dry-run 无副作用。

---

### 11.3 WS3：HTML 写入时校验（1 人日）

**改动文件**

- `src/octop/infra/apps/models.py`：`validate_html`
- 测试：`tests/unit/apps/test_models.py`

**实施步骤**

1. 新增 `find_external_refs(html) -> list[tuple[str, str]]`，扫描：
   - `<(script|link|img|iframe|video|audio|source|embed|object)>` 的 `src/href/data/poster`；
   - `@import`、`url(http…)`；
   - `fetch(`、`XMLHttpRequest`、`WebSocket`、`EventSource`、`sendBeacon`；
   - 外链 `<a href="http…">`（提示改用 `octopApp.openExternal`）。

2. `validate_html` 命中即抛 `APP_HTML_INVALID`，错误信息列出前 3 个位置和修复建议。

3. 测试：外链 script/img/link、内联 data URL、正常内联页面。

**验收**

- 含 CDN 引用的 HTML 在 `app_write_html` 阶段被拒；
- 已有应用不受影响（只在写入时校验）。

---

### 11.4 WS4：统一 ACL（2–3 人日）

**目标**

HTTP 与 agent 工具共用一套权限判定；共享 agent 的协作者能按应用 ACL 使用工具。

**改动文件**

- 新增 `src/octop/infra/apps/access.py`
- `src/octop/infra/apps/service.py`
- `src/octop/infra/apps/tools.py`
- `src/octop/api/routers/apps.py`
- 测试：`tests/unit/apps/test_tools.py`、`tests/integration/test_apps_api.py`

**规则**

| 操作 | 权限 |
|---|---|
| 查看 / 读行 / 助手 | `view` |
| 追加 / 修改行 | `edit_data` |
| 改名 / 改结构 / 删除 / 分享 | `edit_structure` |
| 创建应用 | 可访问该 agent（owner / shared / admin），新应用 owner=操作者 |
| agent owner | **不自动**获得他人应用权限 |

**实施步骤**

1. `AppService.permission_for(row, user_id, is_admin)`：
   - admin → `edit_structure`；
   - `owner_user_id == user_id` → `edit_structure`；
   - 否则取 `light_app_collaborators.permission`（view / edit_data）。

2. `is_agent_accessible(agent_id, user_id, is_admin)`：
   - `agent.user_id == user_id` 或 `agent.is_shared` 或 admin。

3. 工具侧 `_get_owned` → `_get_for_actor`：
   - `app_create` 用 `is_agent_accessible`；
   - `app_list` / `app_read_data` 用 `view`；
   - `app_append_row` 用 `edit_data`；
   - `app_write_html` / `app_update_schema` / `app_delete` 用 `edit_structure`。

4. HTTP `_row_access` 改为调用同一 `permission_for`。

5. 测试：
   - 共享 agent 的普通用户可创建；
   - 应用协作者可通过对话追加；
   - 非协作者不能读元数据；
   - viewer 不能改结构；
   - admin 可绕过。

---

### 11.5 WS5：生命周期保护与交接（2–3 人日）

**改动文件**

- `src/octop/infra/db/repos/apps.py`：`list_by_agent`、`set_agent_id`
- `src/octop/infra/apps/service.py`：`transfer`、`has_apps`
- `src/octop/infra/agents/manager.py`：`delete(..., force=False)` 守卫
- `src/octop/api/routers/agents.py`：`force` 查询参数
- `src/octop/api/routers/apps.py`：`POST /api/apps/{id}/transfer`
- `src/octop/infra/errors.py` + i18n：`AGENT_HAS_APPS`
- 测试

**实施步骤**

1. **删除守卫**
   - `delete(agent_id, *, force=False)` 前查 `list_by_agent`；
   - 有应用且 `force=False` → `OctopError(AGENT_HAS_APPS, details={"count": n})`（409）；
   - API `DELETE /api/agents/{id}?force=true`；CLI 加 `--force`。

2. **交接**

   ```python
   async def transfer(app_id: str, target_agent_id: str, *, actor: User) -> LightAppRow
   ```

   - 校验目标 agent 可访问、非同一 agent；
   - 复制已知文件：`app.html`、`data.csv`、`schema.json`、`manifest.json`、
     `tables/*.csv|schema.json`、`assets/*`；
   - 更新 `light_apps.agent_id`；
   - 源文件清理 best-effort（后端无删除 API 时保留，随源 agent 删除而消失）。

3. **测试**
   - 有应用时删除被拒，`force=true` 成功；
   - 交接后行数/文件完整，应用仍可打开；
   - 交接后删除源 agent 不影响应用。

**风险**

- 远程后端复制是逐文件读写，应用大时较慢；P0 可只复制已知路径，不做递归。
- 源文件可能残留；在文档和提示里说明。

---

### 11.6 WS6：资产通道 + SDK（3–4 人日）

**改动文件**

- `src/octop/infra/apps/service.py`：`save_asset` / `read_asset` / `list_assets`
- `src/octop/api/routers/apps.py`：`POST/GET /api/apps/{id}/assets`
- `src/octop/infra/apps/tools.py`：`app_save_asset`
- `dashboard/src/api/modules/apps.ts`：资产读取
- `dashboard/src/pages/Apps/Detail.tsx`：APP_SDK + postMessage handler
- 测试

**实施步骤**

1. **存储**
   - `apps/<app_id>/assets/<name>`；
   - 文件名 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，拒绝 `..`；
   - 类型：png/jpeg/webp/gif；**P0 不支持 SVG**；
   - 单文件 5MB；魔数校验（PNG/JPEG/GIF/WEBP），不能只信 content-type。

2. **API**
   - `POST` 上传，`edit_data`；
   - `GET /assets/{name}`，`view`，返回正确 Content-Type + 缓存头。

3. **工具**
   - `app_save_asset(app_id, url|path)`：
     - URL 走 SSRF 校验（逐跳校验，参考 harness `web_fetch`），限制 5MB；
     - path 从工作区读字节；
     - 保存到 assets，返回 `{name, size, mime}`。

4. **SDK**
   - `octopApp.asset(name)`：父页面带 JWT 取字节，转 base64/data URL 回传；iframe 用 `data:` URL
     （CSP 已允许）。
     > 不走 `blob:`：sandbox 无 `allow-same-origin`，父页面创建的 blob URL 在 opaque origin 里可能取不到。
   - `octopApp.openExternal(url)`：父页面校验 `http/https` + 域名确认框，再
     `window.open(..., noopener,noreferrer)`。
   - `octopApp.me()`：返回 `{permission}`。
   - `octopApp.notify(text)`：父页面 toast，限长。

5. **测试**
   - 非法文件名/类型/超大文件被拒；
   - viewer 不能上传；
   - 封面能显示；
   - 外链点击需确认，且新开标签。

---

### 11.7 WS7：提示词、i18n、文案（1–2 人日）

**改动文件**

- `src/octop/infra/agents/experts/library/light-apps/AGENTS.md`
- `.../SOUL.md`、`.../manifest.json`
- `src/octop/i18n/en.json`、`zh.json`、`domains/tools.py`
- `dashboard/src/locales/en.json`、`zh.json`（新增 UI 文案）
- `src/octop/api/openapi_meta.py`（如需新 tag，通常不用）

**内容**

- AGENTS.md 增加：
  - 工具表新增 `app_inspect_file`、`app_save_asset`；
  - “Excel 台账”流程：inspect → 总结 → 字段建议 → 问三个问题 → 确认 → 建应用 →
    dry-run → 正式导入 → 写界面；
  - 外链规则：图片走 asset，跳转走 `openExternal`，禁止 CDN。
- manifest 增加快速提示：“把这张 Excel 做成台账”。
- i18n 增加新工具显示名与 `AGENT_HAS_APPS`、`APP_ASSET_INVALID`。

**验收**

- `uv run pytest tests/unit/i18n -q` 绿。

---

### 11.8 WS8：集成、验收、收尾（2–3 人日）

**测试**

```bash
uv run pytest tests/unit/apps -x -q
uv run pytest tests/integration/test_apps_api.py -x -q
uv run pytest tests/unit/i18n -q
make all
cd dashboard && npx tsc -b
```

**手工验收**

1. 上传单 sheet Excel → inspect → 建应用 → dry-run → 导入 → 写界面；
2. 多 sheet + 表头偏移；
3. 含合计行/公式/PII 的文件；
4. 共享 agent 协作者通过对话追加行；
5. 应用交接后删除源专家；
6. 封面通过 asset 显示；
7. 含 CDN 的 HTML 被拒。

**文档**

- 更新 `docs/api.md` 中导入/资产/交接接口；
- 在本文档 §5 标注 P0 完成项。

---

### 11.9 排期建议

| 天 | 工作 |
|---|---|
| D1 | WS3 写入校验；WS1 解析层 |
| D2–D3 | WS1 inspect 工具与测试 |
| D3–D4 | WS2 导入参数与 dry-run |
| D5–D6 | WS4 ACL 统一 |
| D7–D8 | WS5 删除守卫与交接 |
| D9–D10 | WS6 资产与 SDK |
| D11 | WS7 提示词与 i18n |
| D12–D13 | WS8 集成、手工验收、缓冲 |

合计 11–15 人日，与 §5 一致。

---

### 11.10 已决事项（2026-11-04）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 共享 agent 的普通用户能否创建应用 | **允许，owner=操作者**；文件仍落在共享工作区，属已知边界（`light-apps-architecture.md` §8） |
| 2 | 资产类型与大小限制 | **5MB + png/jpeg/webp/gif，不支持 SVG**（SVG 可内联脚本，需沙箱化转码才能支持） |
| 3 | 删除有应用的 agent | **409 阻止，必须 `force=true` 显式确认**（`AGENT_HAS_APPS`） |
| 4 | 应用交接时源文件 | **复制到目标 + 更新 DB，源文件保留**（`BackendProtocol` 无删除接口，强行删反而不一致） |
| 5 | P0 是否引入行数据入库 | **不引入**，P0 停留在 v24；入库放 P1（否则工期从 11–15 涨到 25+ 人日） |

