# 小程序创建途径增强设计

> 状态：设计稿（2026-11-06）
>
> 实施状态（2026-11-06）：已落地「创建」入口、快速模式和系统级本地模板；云端市场保留为 P2，先以 `source` 字段为后续扩展点。
>
> 范围：工作台「创建」入口、快速模式、系统级本地模板、云端模板市场
>
> 目标：把当前单一的「从模板创建」升级为三种创建方式，降低第一次创建的门槛，并让模板可以跨用户/专家复用。

---

## 1. 背景与现状

当前工作台只有一条创建路径：

- 入口：`dashboard/src/pages/Apps/index.tsx` 的「从模板创建」按钮；
- 模板来源：所选 agent 工作区的 `app_templates/<name>/{schema.json,app.html}`；
- 创建逻辑：`POST /api/apps/from-template` → `AppService.create_from_template()`；
- 默认 agent 是 `agents[0]`，通常是 `main`；`main` 没有模板时下拉为空。

问题：

1. **入口单一**：没有数据的用户只能先找专家对话，或者面对空模板。
2. **模板是 agent 私有的**：系统无法提供一套所有用户、所有专家都能用的内置模板。
3. **没有数据优先的路径**：Excel/台账用户最自然的诉求是「导入后直接看板」，当前必须走对话确认。
4. **没有分发能力**：模板不能从云端市场获取、版本化、更新。
5. **默认 agent 选择不合理**：模板列表跟随 `agents[0]`，而不是当前上下文中的 agent。

---

## 2. 目标与非目标

### 2.1 目标

- 工作台只保留一个主入口「创建」，点开后选择三种模式：
  - **快速模式**：上传 Excel/CSV 台账 → 确认字段 → 自动生成看板/控制台；
  - **本地模板**：系统级模板 + 当前 agent 模板，统一画廊；
  - **云端市场**：浏览、搜索、安装云端模板。
- 系统级模板对所有用户和所有专家可用，不依赖某个 agent 的工作区。
- 创建出来的小程序仍然归属一个 agent 工作区（沿用现有 ACL、应用助手、数据源能力）。
- 不引入第二套小程序运行时；快速模式仍然落成标准 schema + rows + 单文件 HTML。

### 2.2 非目标

- 不做模板可视化编辑器（后续可做模板上传/发布）；
- 不做小程序实例的自动升级（模板升级不自动改已有应用）；
- 不做实时协同、行级权限（沿用现有非目标）；
- 移动端暂不支持（沿用当前范围）。

---

## 3. 总体交互

```
工作台
  └─ [创建]  ──► 创建小程序弹窗
                    ├─ 快速创建   上传台账 → 预览字段 → 选视图 → 创建
                    ├─ 本地模板   系统模板 / 专家模板 → 预览 → 创建
                    └─ 云端市场   搜索/分类 → 安装 → 用模板创建
```

弹窗内用三张模式卡或顶部 Segmented 切换；切换时保留已选 agent，避免重复选择。

公共字段：

| 字段 | 说明 |
|---|---|
| 所属 Agent | 必选；决定应用工作区、应用助手、文件位置 |
| 名称 | 默认取文件名/模板名，可改 |
| 描述 | 可选 |

默认值从当前 agent 上下文取，不再固定 `agents[0]`。

---

## 4. 快速模式

### 4.1 用户流程

1. **上传**：拖入 `.xlsx/.xls/.csv/.tsv/.json`（上限沿用 `MAX_INSPECT_BYTES` 50MB）。
2. **解析预览**：服务端调用现有 `inspect_file()`：
   - sheet 列表、表头行猜测、行数；
   - 列类型、非空率、枚举候选、PII 标记；
   - 前 5 行样本。
3. **确认字段**：表格形式展示 `label / name / type / required / 是否导入 / PII`，用户可改名、改类型、勾选列。
4. **选择视图**：三选一（可后续扩展）：
   - **概览看板**：KPI + 趋势图 + 分类图 + 明细表；
   - **明细台账**：筛选 + 搜索 + 分页表格；
   - **进度看板**：按状态/阶段分组计数，类似轻量 kanban。
5. **创建**：服务端创建应用 → 导入数据 → 生成 HTML → 打开详情页。

### 4.2 生成策略

推荐 **确定性生成（不走 LLM）**：

- 快：无模型延迟，秒级完成；
- 稳：同样的字段和视图配置生成同样的页面；
- 省：不消耗 token，不依赖专家在线；
- 可测：渲染器是纯函数，容易单测。

创建完成后提供「让专家继续完善」按钮，跳转对话并预填应用上下文；专家可以用 `app_write_html` 重写界面。

> 备选：直接让 light-apps 专家走现有 `app_inspect_file` 卡片流程。缺点是慢、依赖模型、结果不稳定，适合作为「高级模式」而不是默认快速模式。

### 4.3 视图配置（ViewSpec）

快速模式不把布局写死，而是把推断结果转成一份 `ViewSpec`，由渲染器消费：

```json
{
  "title": "2026Q3 销售台账",
  "view": "dashboard",
  "kpis": [
    { "column": "amount", "agg": "sum", "label": "销售额" },
    { "column": "order_id", "agg": "count", "label": "订单数" }
  ],
  "charts": [
    { "type": "line", "x": "date", "y": "amount", "agg": "sum" },
    { "type": "bar", "x": "region", "y": "amount", "agg": "sum" }
  ],
  "filters": ["region", "date"],
  "table": { "columns": ["date", "region", "order_id", "amount"], "page_size": 50 }
}
```

类型映射规则（首版）：

| 字段类型 | 默认用途 |
|---|---|
| `number` / `integer` | KPI（sum/avg）、柱状图/折线图的数值轴 |
| `date` / `datetime` | 折线图 X 轴、日期筛选 |
| `enum` / `boolean` / 低基数 `string` | 分类柱状图、饼图、筛选器 |
| `string` / `text` | 表格列、搜索 |
| `pii` 列 | 默认不导入、不参与图表、表格脱敏 |

### 4.4 API

**检查文件**

```
POST /api/apps/quick/inspect        multipart/form-data
  agent_id: string
  file: UploadFile
  sheet?: string

200 {
  "upload_path": "inbound/imports/ab12cd34-sales.xlsx",
  "report": { ...inspect_file()... },
  "suggested_view": { ...ViewSpec... }
}
```

**创建应用**

```
POST /api/apps/quick/create
{
  "agent_id": "51ZY4A",
  "path": "inbound/imports/ab12cd34-sales.xlsx",
  "sheet": "明细",
  "header_row": 1,
  "columns": [
    {"name": "date", "label": "日期", "type": "date", "required": true},
    {"name": "amount", "label": "金额", "type": "number"}
  ],
  "view_spec": { ... },
  "name": "2026Q3 销售台账",
  "description": "",
  "include_pii": false,
  "upsert_key": ["order_id"]
}

201 AppDetail
```

服务端步骤：

1. 校验 agent 访问权限；
2. `service.create()` 创建 schema；
3. `service.import_from_file()` 导入（先用 `dry_run` 做一次一致性校验，再正式导入）；
4. `viewgen.render(schema, view_spec)` 生成自包含 HTML；
5. `service.write_html()`；
6. 任一步失败删除半成品应用；
7. 写审计 `miniapp.quick_create`。

### 4.5 边界与错误

- 文件 > 50MB：拒绝并提示；
- 空 sheet / 无表头：提示换 sheet 或指定表头行；
- 全部列都是 PII：默认拒绝导入，要求显式 `include_pii=true`；
- 导入失败行：返回 `failed_rows`，前端提供下载与续跑；
- 同一文件重复提交：前端禁用按钮，后续可加 `client_request_id`；
- 大文件解析：首版同步处理；超过 10MB 可改为后台任务。

### 4.6 快速模式不做的

- 多表关联（首版只导一张 sheet；多表用专家/模板路径）；
- 复杂图表配置（首版固定 KPI/折线/柱状/表格）；
- 自动写公式、透视表。

---

## 5. 本地模板（系统级 + 专家级）

### 5.1 模板来源

保留现有专家模板，同时新增系统级模板：

| 来源 | 存储位置 | 可见范围 | 写入方式 |
|---|---|---|---|
| `system` | 包内 `src/octop/infra/apps/templates/<id>/` | 所有用户、所有 agent | 随版本发布，只读 |
| `agent` | agent 工作区 `app_templates/<id>/` | 该 agent 的可见用户 | 专家发布/用户上传 |
| `cloud` | DB `app_template_packages`（见 §6） | 安装时选择 system/agent 范围 | 市场安装 |

系统模板目录结构：

```
src/octop/infra/apps/templates/sales-ledger/
  manifest.json      # 标题、描述、分类、版本、预览图
  schema.json        # 字段定义（复用现有格式）
  app.html           # 自包含界面
  preview.png        # 可选，仅本地静态资源
```

`manifest.json`：

```json
{
  "id": "system.sales-ledger",
  "version": 1,
  "name": { "zh": "销售台账", "en": "Sales Ledger" },
  "description": { "zh": "按区域和月份汇总订单", "en": "Orders by region and month" },
  "category": "sales",
  "tags": ["销售", "台账"],
  "schema": "schema.json",
  "html": "app.html",
  "preview": "preview.png",
  "min_octop_version": "1.0.0"
}
```

### 5.2 解析与优先级

新增 `TemplateRegistry`，按顺序聚合：

1. `cloud` 已安装模板（scope=system 优先，scope=agent 次之）；
2. `system` 包内模板；
3. `agent` 工作区模板。

模板 ID 用 `source:template_id` 复合键，避免同名冲突。同一来源内 ID 唯一。若业务上希望 agent 覆盖系统模板，可在展示层按「agent > system > cloud」去重，但 API 始终返回 `source`，创建时显式传 `source`。

### 5.3 API 扩展

```
GET /api/apps/templates?agent_id=...&source=all|system|agent|cloud&q=&category=

200 {
  "templates": [
    {
      "template_id": "system.sales-ledger",
      "source": "system",
      "name": "销售台账",
      "description": "按区域和月份汇总订单",
      "category": "sales",
      "version": 1,
      "has_html": true,
      "preview_url": "/api/apps/templates/system.sales-ledger/preview",
      "scope": "global"
    },
    {
      "template_id": "reading-tracker",
      "source": "agent",
      "agent_id": "51ZY4A",
      "name": "读书清单",
      "version": 1,
      "has_html": true,
      "scope": "agent"
    }
  ]
}
```

创建请求向后兼容：

```json
{
  "agent_id": "51ZY4A",
  "template_id": "system.sales-ledger",
  "source": "system",
  "name": "我的销售台账"
}
```

旧字段 `template` 保留，默认 `source="agent"`。

### 5.4 系统模板运营

- 随 wheel 发布，版本号跟产品版本走；
- 首批建议 3–5 个：销售台账、客户跟进、项目任务、费用报销、库存盘点；
- 新增/修改走代码评审和 HTML 沙箱校验；
- 后续若需要管理员在线编辑，再加 DB overlay，不改包内文件。

---

## 6. 云端市场

### 6.1 架构

```
Dashboard ──► Octop API ──► Market Catalog（HTTPS，签名 JSON）
                               └─► Template ZIP（签名/哈希）
                                     └─► 校验 → 安装到 DB / agent 范围
```

不让浏览器直连市场，避免 CORS、凭证和内容校验绕过；所有下载与校验在服务端完成。

### 6.2 模板包格式

```
<id>-<version>.zip
  manifest.json
  schema.json
  app.html
  preview.png        # 可选
  README.md          # 可选
```

`manifest.json` 增加：

```json
{
  "id": "market.sales-dashboard",
  "version": 3,
  "name": { "zh": "销售看板", "en": "Sales Dashboard" },
  "author": "Octop Labs",
  "min_octop_version": "1.0.0",
  "content_hash": "sha256:...",
  "signature": "ed25519:..."
}
```

### 6.3 安装范围

| 范围 | 谁能装 | 谁能用 | 存储 |
|---|---|---|---|
| `system` | 管理员 | 所有用户/agent | DB，全局 |
| `agent` | agent 所有者 | 该 agent 的可见用户 | DB，绑定 agent |
| `user`（后续） | 用户本人 | 本人 | DB |

首版建议只开放 **admin 安装 system 模板**，避免市场内容未经审核进入所有用户视野；agent 范围作为第二阶段。

### 6.4 安装流程

1. `GET /api/apps/market/templates?q=&category=` 获取目录（服务端缓存 5–10 分钟）；
2. `POST /api/apps/market/templates/{id}/install`：
   - 下载 zip；
   - 校验 zip-slip、解压大小（≤ 5MB）、文件数量；
   - 校验 `content_hash` / `signature`；
   - 校验 manifest 必填字段与版本；
   - 对 `app.html` 跑 `validate_html()`（禁止外链、`fetch()`）；
   - 写入 `app_template_packages`；
   - 审计 `miniapp.template_install`。
3. 卸载/停用：`DELETE /api/apps/market/installs/{id}`，只影响新建，不影响已有应用。

### 6.5 安全与治理

- 目录与包都签名；公钥随产品内置，支持轮换；
- 只允许 HTTPS 且域名白名单；
- 模板不携带任何用户数据；
- 安装时不执行模板 JS；
- 版本只增不减；同版本哈希不一致直接拒绝；
- 提供「仅内置市场」开关，离线环境可关闭；
- 市场内容审核（人工/自动扫描）作为运营流程。

### 6.6 离线/内网

- 支持管理员上传模板 zip（`POST /api/apps/market/upload`），走同一套校验与安装流程；
- 目录不可达时，本地模板和快速模式不受影响。

---

## 7. 数据模型

### 7.1 新增表 `app_template_packages`（迁移 030）

| 列 | 说明 |
|---|---|
| `id` | 整数主键 |
| `template_id` | 模板业务 ID（`market.sales-dashboard`） |
| `version` | 整数版本 |
| `scope` | `system` / `agent` / `user` |
| `agent_id` | scope=agent 时绑定 |
| `source` | `market` / `upload` |
| `manifest_json` | manifest 原文 |
| `schema_json` | schema |
| `html` | 单文件 HTML |
| `preview` | 可选 BLOB |
| `content_hash` | sha256 |
| `signature` | 可选 |
| `enabled` | 0/1 |
| `created_by` | user id |
| `created_at` / `updated_at` | 时间戳 |

唯一约束：`(template_id, version, scope, agent_id)`。

### 7.2 `light_apps` 增加创建来源（可选但推荐）

| 列 | 说明 |
|---|---|
| `created_via` | `quick` / `template` / `market` / `agent` |
| `template_ref` | `source:template_id@version`，快速模式为空 |

用于统计「哪种创建方式最常用」和问题追踪；不参与 ACL。

---

## 8. 前端设计

新增组件（建议目录 `dashboard/src/pages/Apps/Create/`）：

| 组件 | 职责 |
|---|---|
| `CreateMiniAppModal.tsx` | 模式切换、公共字段、提交 |
| `QuickCreateWizard.tsx` | 上传 → 字段确认 → 视图选择 |
| `TemplateGallery.tsx` | 系统/专家模板卡片、搜索、预览 |
| `MarketPanel.tsx` | 云模板列表、安装状态 |
| `ViewSpecPreview.tsx` | 快速模式的看板缩略预览 |

页面改动：

- `dashboard/src/pages/Apps/index.tsx`：按钮文案 `apps.fromTemplate` → `apps.create`；空态增加「创建」按钮；
- `dashboard/src/api/modules/apps.ts`：新增 `quickInspect`、`quickCreate`、`listTemplates({source})`、`installMarketTemplate` 等类型；
- i18n：`dashboard/src/locales/{zh,en}.json` 增加 `apps.create.*`、`apps.quick.*`、`apps.templates.*`、`apps.market.*`；
- 默认 agent 使用当前上下文（`useAgent().activeAgentId`），没有则回退第一个可访问 agent。

交互细节：

- 切换模式不丢已上传文件和已填字段；
- 快速模式字段表可直接改 `label/type/required`，对应 `app_alter_schema` 的能力；
- 创建按钮防重复点击；
- 创建成功跳转 `/apps/{id}`；
- 失败展示后端错误码对应的中文文案。

---

## 9. 权限与安全

| 操作 | 权限 |
|---|---|
| 查看系统模板 | 登录用户 |
| 查看 agent 模板 | 该 agent 可访问 |
| 快速创建 | 该 agent 可访问 + 可创建应用 |
| 安装 system 市场模板 | 管理员（`users` 权限） |
| 安装 agent 市场模板 | agent 所有者 |
| 卸载/停用 | 安装者或管理员 |

安全要点：

- 所有模板 HTML 走现有 `validate_html()`；
- 模板包不得包含数据文件、脚本外链、`fetch()`；
- 快速模式沿用 PII 默认拦截与脱敏；
- 市场下载设置大小、超时、域名白名单；
- 审计：创建、安装、卸载、导出。

---

## 10. 兼容与迁移

- 旧 `GET /apps/templates?agent_id=` 保持返回 agent 模板；新增 `source` 参数默认 `agent`，不破坏现有调用；
- 旧 `POST /apps/from-template` 保持 `{agent_id, template}` 可用；
- 现有专家模板目录格式不变；
- 新表迁移 030，版本断言 29 → 30；
- 已部署实例无需迁移数据；系统模板随 wheel 上线；
- 旧应用不受模板升级影响。

---

## 11. 测试与验收

### 11.1 单元

- `TemplateRegistry`：来源聚合、排序、同名冲突、权限过滤；
- manifest 校验：缺字段、路径穿越、超限；
- `viewgen`：类型 → KPI/图表/表格映射，PII 排除；
- 快速模式：字段确认、`include_pii`、失败行、空表；
- 市场安装：哈希/签名失败、zip-slip、HTML 外链拒绝。

### 11.2 集成

- 上传 xlsx → inspect → quick create → rows 正确 → HTML 可渲染；
- 系统模板对两个不同 agent 都可见、都能创建；
- 市场模板安装后出现在模板列表；
- 非管理员安装 system 模板 403；
- 卸载后新创建列表不再出现，已有应用不受影响。

### 11.3 验收场景

| # | 场景 | 预期 |
|---|---|---|
| 1 | 新用户上传销售 Excel | 30 秒内得到一个可筛选的销售看板 |
| 2 | 用户不想上传数据 | 从系统模板「客户跟进」一键建空应用 |
| 3 | 管理员从市场安装模板 | 所有用户/专家立即看到 |
| 4 | 普通用户尝试安装系统模板 | 403 |
| 5 | 模板 HTML 含外链 | 安装/创建被拒绝 |
| 6 | 台账含身份证列 | 默认不导入，看板不展示 |

---

## 12. 分期与工作量

| 阶段 | 内容 | 预估 |
|---|---|---|
| **P0 快速模式 + 创建入口** | 「创建」弹窗、快速上传/字段确认/看板渲染、API、测试 | 4–6 人日 |
| **P1 系统级本地模板** | 包内模板目录、TemplateRegistry、模板画廊、系统模板 3–5 个 | 3–4 人日 |
| **P2 云端市场（管理员）** | 目录拉取、签名校验、安装/卸载、审计 | 4–6 人日 |
| **P3 增强** | agent 范围安装、模板上传/发布、版本更新提示、使用统计 | 4–6 人日 |

建议顺序：P0 → P1 → P2。P0/P1 不依赖网络和新服务，能先解决大多数创建诉求。

---

## 13. 风险与开放决策

### 13.1 风险

| 风险 | 应对 |
|---|---|
| 市场模板携带恶意 HTML | 沙箱 + `validate_html` + 签名 + 人工审核 |
| 快速模式生成页面千篇一律 | 提供 3 种视图 + 创建后「让专家完善」 |
| 大文件解析阻塞请求 | 首版限制 50MB/20k 行；后续异步任务 |
| 模板 ID 冲突 | `source:template_id` 复合键 |
| 系统模板随版本升级，用户已建应用不一致 | 模板只影响新建，不自动升级 |
| PII 进入图表 | 默认不导入；渲染器跳过 `pii` 列 |

### 13.2 需要拍板

1. **快速模式的生成方式**：确定性渲染（推荐）还是直接交给专家模型？
2. **系统模板的维护方式**：随代码发布（推荐首版）还是管理员在线编辑？
3. **云端市场第一阶段范围**：只做管理员安装 system 模板（推荐），还是一开始就支持 agent/用户范围？
4. **市场后端**：静态签名目录（推荐 MVP）还是接入完整市场服务？
5. **是否现在加 `created_via` / `template_ref` 两列**：推荐加，便于后续统计与排障。

---

## 14. 本次落地（2026-11-06）

已实现 P0 + P1：

- 后端：`infra/apps/templates.py`（系统模板注册表）、`infra/apps/system_templates/`（内置模板）、`infra/apps/viewgen.py`（确定性看板渲染）、`AppService.quick_inspect()/quick_create()`；
- API：`GET /apps/templates?source=all|system|agent`、`POST /apps/from-template`（新增 `source`）、`POST /apps/quick/inspect`、`POST /apps/quick/create`；
- 前端：工作台「创建」入口、`CreateMiniAppModal`（快速创建 / 模板 / 云端市场）、字段确认、PII 提示、三种视图；快速模式带三步向导，云端市场当前展示「正在开发中，敬请期待」；
- 兼容：旧 `GET /apps/templates`（默认 `source=agent`）和旧 `POST /apps/from-template` 保持可用。

云端市场（P2）尚未实现真实目录与安装链路，`source` 字段与 API 已为市场来源预留扩展位。
