# 小程序操作手册

本文件是技术规范。SOUL.md 讲态度，这里讲做法。

## 工具一览

| 工具 | 作用 | 关键约束 |
| --- | --- | --- |
| `app_inspect_file` | 检查工作区里的表格文件 | 只读；返回 sheet、表头、列统计、样本行；**不要自己读 xlsx** |
| `app_list` | 列出本 agent 拥有的应用 | 只返回你自己的应用；每项带表清单 |
| `app_open` | 在对话中打开应用 | 返回 app_card；**首个完整可用版本完成后调用一次**；后续编辑/优化不要再调用 |
| `app_create` | 建应用并定义字段 | 1-50 个字段；名称 ≤100 字；同时创建主表 `main`；可传 `capabilities` 声明页面能力 |
| `app_create_from_template` | 从模板建空应用 | 模板来自 agent 的 `app_templates/` 或系统模板（`source="system"`）；只带 schema+界面，不带数据 |
| `app_add_table` | 给应用新增一张附加表 | key 应用内唯一；每个应用最多 12 张表；建了不能删；默认不公开 |
| `app_update_schema` | 追加字段 | 只能加不能改；已有数据时不能加必填字段；可传 `table` |
| `app_alter_schema` | 改名/删除/改类型/标 PII | 会迁移所有已有行；有行不符合新结构时整次拒绝；`pii` 可给已有列补标或清除；可传 `table` |
| `app_set_table_visibility` | 设置表是否出现在公开链接 | 主表默认公开，附加表默认私有；可传 `table` |
| `app_set_capabilities` | 替换页面能力清单 | 只约束 iframe 页面；`[]` 表示页面不能做任何操作；不影响你自己的工具 |
| `app_write_html` | 写完整界面 | 整页覆盖；必须自包含单文件；内部写入，不产生聊天卡片 |
| `app_read_data` | 读一页数据 | `limit` 1-500，`offset` 分页；每行带 `row_id` / `version`；可传 `table` |
| `app_append_row` | 追加一行 | 未知字段、类型不符会被拒；可传 `client_request_id` 幂等；可传 `table` |
| `app_update_row` | 修改一行 | 用 `app_read_data` 返回的 `row_id`；传 `expected_version` 防覆盖；可传 `table` |
| `app_delete_row` | 删除一行 | 用 `row_id`；传 `expected_version` 防覆盖 |
| `app_import_rows` | 从工作区文件批量导入 | 支持 CSV/TSV/JSON/xlsx；可传 `table` 和列映射；先 `dry_run` |
| `app_save_asset` | 把图片存到应用目录 | PNG/JPEG/WebP/GIF，≤5MB；来源可以是 URL 或工作区路径 |
| `app_set_preview` | 画小程序的预览图（封面） | 自己写一段 SVG；工作台和模板画廊用它当缩略图 |
| `app_delete` | 删除应用 | 需要用户批准 |

工具都返回 JSON 字符串：成功是结果对象，失败是 `{"error": "..."}`。出错先读 error 再改参数，别瞎试。

## 页面能力

`app_create(..., capabilities=[...])` 声明页面能向宿主请求什么。省略时使用安全默认集：
`data.read` / `data.write` / `asset.read` / `external.open` / `chat.compose` / `events.read`。
高风险能力（`ai.generate`、`game.launch`、`external.fetch`、`events.emit`、
`schedules.manage`）必须在 `capabilities` 里显式声明，否则页面调用会被拒绝。

- 页面确实需要 `octopApp.generate()` 时才加 `ai.generate`；需要外部游戏时才加 `game.launch`。
- `app_set_capabilities` 可以随时替换清单；传 `[]` 表示页面只能展示、不能读写或调用任何宿主能力。
- 能力只约束 iframe 页面；你自己的 `app_*` 工具和 Dashboard 数据面板不受影响。

## 标准流程

### 第 1 步 · `app_create` 定字段

```json
{
  "name": "本周待办",
  "description": "团队每周任务跟踪",
  "columns": [
    { "name": "title", "label": "事项", "type": "text", "required": true },
    { "name": "owner", "label": "负责人", "type": "string" },
    { "name": "due_date", "label": "截止日期", "type": "date" },
    { "name": "done", "label": "是否完成", "type": "boolean" }
  ]
}
```

字段规则：

- `name`：snake_case，字母或下划线开头，列内唯一。
- `label`：给人看的名字，可以是中文。
- `type`：`string` | `text` | `integer` | `number` | `boolean` | `date` | `datetime` | `enum`。
- `required`：必填列在追加数据时不能为空。
- `values`：只有 `enum` 用，列出全部可选项。

字段定错了只能追加、不能改也不能删，所以先想清楚——宁可少而准。

### 第 2 步 · `app_write_html` 写界面

整页 HTML，CSS 与 JS 全部内联：

```html
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>本周待办</title>
<style>
/* 样式写这里 */
</style>
</head>
<body>
<h1>本周待办</h1>
<ul id="list"></ul>
<form id="add"><!-- 输入项 --></form>
<script>
// 脚本写这里
</script>
</body>
</html>
```

写的时候把注释换成真代码，别把骨架原样交出去。

### 第 3 步 · 用 SDK 取数

宿主会在页面里注入 `window.octopApp`：

```js
// 表清单 → { tables: [{ table_key, label, row_count, is_main }] }
const { tables } = await octopApp.tables();

// 列定义 → { table, columns: [{ name, label, type, required, values }] }
const { columns } = await octopApp.schema();            // 主表
const orderColumns = await octopApp.schema('orders');   // 指定表

// 读数据 → { table, columns, rows, total, limit, offset }，rows 是 [{ 列名: 值 }]
const { rows, total } = await octopApp.listRows({ limit: 200, offset: 0 });
const orders = await octopApp.listRows({ table: 'orders', limit: 200 });

// 追加一行 → { row_index, row, table }
const saved = await octopApp.appendRow({ title: '写周报', done: false });
const order = await octopApp.appendRow({ item: '纸杯' }, { table: 'orders' });
```

要点：

- 页面加载时 `listRows()` 一次把 rows 渲染出来，之后改动再读一次。
- `appendRow` 成功后把新行本地加进列表，不要整页刷新。
- 追加失败会 reject（必填没填、枚举值不合法），catch 住给用户一句人话提示。
- 只有有编辑权限的查看者能追加；只读的人看到的是列表，追加入口要禁用。
- `schema()` / `listRows()` / `appendRow()` 的 `table` 都缺省为 `main`，只有多表应用才需要传。

页面里要跳转到站外链接时，不要写 `<a href="https://…">`（会被当成外链拒绝），改用：

```js
// 宿主会弹出确认框，用户确认后才新开标签页
await octopApp.openExternal("https://example.com/page");
```

### 图片与封面：先存素材，再用 `octopApp.asset`

页面不能加载外链图片，所以封面、图标要先存进应用目录：

```json
// 工具：从 URL 或工作区路径保存一张图
{ "app_id": "app_xxxxxx", "source": "https://img.example.com/cover.jpg", "name": "cover.jpg" }
```

保存后用 SDK 读取，返回 `{ name, mime, data_url }`，直接塞给 `<img>`：

```js
const asset = await octopApp.asset("cover.jpg");
document.querySelector("#cover").src = asset.data_url;  // data: URL，CSP 允许
```

- 只支持 PNG / JPEG / WebP / GIF，单张 ≤5MB；**SVG 不支持**（可以内联写进 HTML）。
- 存 URL 时工具会做 SSRF 校验；失败就换工作区路径（先把图下载到 `inbound/`）。
- 不要在 HTML 里写死 base64 大图——素材单独存，页面只记文件名。

### 第 4 步 · `app_append_row` 灌初始数据

界面和数据可以换个顺序：先造几条真实数据再写界面，往往更容易想清楚要什么。

```json
{ "app_id": "app_xxxxxx", "row": { "title": "写周报", "owner": "小林", "due_date": "2026-10-24", "done": false } }
```

多表应用往附加表灌数据时再加 `"table": "orders"`，缺省写主表。

取值会被按列类型校验和归一化：整数要 `3`，数字要 `3.5`，布尔要 `true` / `false`（也接受 `1` / `0` / `yes` / `no`），
日期要 `YYYY-MM-DD`，时间要 ISO-8601，枚举必须落在声明的 `values` 里。

### 第 5 步 · `app_read_data` 核对

交付前读一遍数据，确认条数、字段、取值都对。

### 第 6 步 · `app_set_preview` 画封面

工作台（应用列表）和模板画廊用预览图当缩略图，所以每个小程序都要有一张自己画的封面：

```json
{
  "app_id": "app_xxxxxx",
  "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 1200 630\">…</svg>"
}
```

写法要求：

- 一份完整的 `<svg>…</svg>` 文档，`viewBox="0 0 1200 630"`（宽幅卡片），≤256KB。
- 自包含：颜色、渐变、图形都用 SVG 自己画，**不引外部图片、字体、样式**，不写 `<script>`、事件属性、`foreignObject`、动画元素。
- 内容跟这个应用对上：标题用应用名，主视觉用这个应用真正关心的东西（账本就是分类占比，跟进就是阶段漏斗），别画通用装饰。
- 文字用 `font-family="system-ui, -apple-system, 'PingFang SC', sans-serif"`，颜色对比度够高。

写坏了工具会告诉你哪一条不合法（比如 "SVG must not contain <script> elements"），照着改完再调一次。

自己提供应用模板时（`app_templates/<模板名>/`），同样放一份 `preview.svg`（同名同规则），
模板画廊就会用它当缩略图；用模板建出来的应用会自动继承这张封面，直到 agent 给自己画新的。

### 交付 · 小程序卡片入口只给一次

首个**完整可用版本**（界面、数据读写、封面都就绪）完成后，调用一次 `app_open`，让 Dashboard
显示小程序卡片入口。这是交付动作，不是每次改界面都做。

- 后续优化、改字段、改 HTML、补数据：直接改，**不要再调用 `app_open`**；回复里说明“已更新同一个应用”即可，已有卡片仍然可点。
- `app_write_html` 只是内部写界面，**不产生聊天卡片**；不要用它来交付入口。
- 用 `app_create_from_template` 建出来且已经可用的应用，它的卡片就是入口，不要再补 `app_open`。
- 只有用户明确说“打开/展示某个已有应用”时，才再次调用 `app_open`。

### 修改已有数据

`app_read_data` 返回的 `row_meta` 与 `rows` 一一对应，每项带 `row_id` 和 `version`：

- 改一行：`app_update_row(app_id, row_id, row, expected_version=version)`。
- 删一行：`app_delete_row(app_id, row_id, expected_version=version)`。
- 版本对不上会返回冲突，不要盲目重试；重新读一次再改。

结构变更用 `app_alter_schema`（改名 / 删除 / 改类型），它会迁移所有已有行；
任何一行不符合新结构（比如把文本列改成整数但里面是「暂无」）会整次拒绝，不会写一半。
追加字段仍用 `app_update_schema`，两者都只影响一张表。

**删列会永久丢掉那一列的数据**：动手前先把要删的列名和影响告诉用户，得到明确同意再调用。

## 台账导入：先检查，再建应用

用户丢来一个 Excel / CSV 台账要你做成应用时，**不要自己读文件**（一次读几千行会撑爆上下文，
也容易看错值）。按这个顺序走：

1. `app_inspect_file(path)` 拿报告：每个 sheet 的表头行、列类型、空值比例、去重数、样本值、
   以及 PII 警告（`warnings` 里会有 `header_not_first_row`、`totals_row_detected`、
   `merged_cells`、`formula_cells`、`too_many_rows`）。
2. 用 3–5 句话向用户复述：几个 sheet、多少行、表头在第几行、有没有敏感列。
3. 给字段建议（用报告里的 `name_suggestion` 和 `type`），并说明为什么这么拆表：
   同一实体按月分文件就合并成一张表加 `period` 列；字段集明显不同才 `app_add_table` 拆表。
4. 问三个问题再动手：**以后还会按月更新吗？谁会一起用？哪些列敏感？**
5. 用户确认后 `app_create` / `app_add_table` 定字段。
6. `app_import_rows(path, table=..., mapping=..., sheet=..., header_row=..., dry_run=True)`
   先试跑一次：报告里的 `header_row` 直接传进去，多 sheet 用 `sheet="9月"` 指定。
   把 `would_import` / `skipped` / `errors` / `blocked_pii` / `failed_rows` 报给用户看，
   确认无误再去掉 `dry_run` 正式导入。列名对不上就传 `mapping`（源列名 → 目标列名）。
   敏感列默认不写（`blocked_pii`）；只有用户明确同意后才传 `include_pii=true`。
   `app_create` 的列定义里把报告中的 `pii` 一并带上（如 `{"name": "phone", "pii": "phone"}`），
   这样查看者和公开链接会自动脱敏。失败行会带在 `failed_rows` 里，修正源文件后按
   `upsert_key` 重导即可续跑，成功行不会重复。
7. `app_read_data` 核对条数，再写界面。

三条红线：

- 报告里 `pii` 为真的列（身份证、银行卡、手机号、工资、住址）**默认不要导入**，除非用户明确说
  要；用户要了就只导入他点名的那几列，并传 `include_pii=true`。必填的敏感列必须先征得同意，
  否则导入会被拒绝。
- `too_many_rows` 说明文件超过 2 万行：告诉用户可以只导一段时间、或者先做聚合，不要硬导全量。
- 表格里如果有「合计」行，`app_inspect_file` 会标出来并排除；不要自己再算一遍合计。

导入参数速查：

| 参数 | 什么时候用 |
| --- | --- |
| `dry_run=True` | 先看会导入多少行、哪些行会被跳过，确认后再导 |
| `sheet="9月"` | 一个文件里有多个 sheet 时，选要导的那张 |
| `header_row=2` | 报告说表头不在第一行时（前面有标题或说明） |
| `mapping={"工号": "employee_no"}` | 源列名和应用字段对不上时 |
| `upsert_key=["employee_no", "month"]` | 按月重复导入同一批人，按业务键更新而不是追加重复行 |
| `include_pii=true` | 用户明确同意导入敏感列后；默认 false 会跳过这些列 |
| `paths=["inbound/9月.xlsx", "inbound/10月.xlsx"]` | 一次导入多个文件到同一张表（按顺序合并） |
| `mode="replace"` | 想整表覆盖（会清掉原有数据，只在 owner 确认后用） |

## 多表：什么时候拆表

一个应用可以有多张表。`app_create` 建的是主表 `main`；需要另一组完全不同的字段时，用
`app_add_table` 加一张附加表：

```json
{
  "app_id": "app_xxxxxx",
  "key": "orders",
  "label": "订单",
  "columns": [
    { "name": "item", "label": "商品", "type": "string", "required": true },
    { "name": "amount", "label": "金额", "type": "number" }
  ]
}
```

判断标准：

- **该拆**：不同实体、字段集明显不同——待办和项目、账单和成员、订单和商品。
- **不该拆**：同一实体换个分类。用 `enum` 列 + 界面筛选，别按分类建表。

约束：

- `key` 用 snake_case（`orders`、`members`），`label` 给人看；保留字 `app` / `data` / `schema` /
  `manifest` / `tables` 不能用。
- 主表 `main` 永远存在，不能重复创建；表建了不能删，字段只能加不能改——拆之前想清楚。
- 每个应用最多 12 张表（含主表）。
- 往附加表读写时带 `table`：`app_read_data(app_id, table="orders")`、
  `app_append_row(app_id, row, table="orders")`、`app_update_schema(app_id, columns, table="orders")`。

界面上有多张表时，用 `octopApp.tables()` 渲染 tab / 下拉切换器；切表后重新
`schema(key)` + `listRows({ table: key })`，不要缓存别的表的数据当当前表用。

## 沙箱红线

| 不能做 | 原因 | 替代 |
| --- | --- | --- |
| `<link href="https://cdn…">` | 远程资源被 CSP 拦掉 | 内联 CSS |
| `<script src="https://…">` | 同上 | 内联 JS |
| `<img src="https://…">` | 同上，且是数据外泄通道 | `data:` / `blob:` URL |
| `<a href="https://…">` | 外链会替换整个应用页面 | `octopApp.openExternal(url)` |
| `fetch("/api/apps/…")` | iframe 没有凭证，也被禁止联网 | `octopApp.*` |
| 把数据写死在 HTML | 数据不会随表更新 | `listRows()` |
| 引入第三方图表库 | 远程脚本被拦 | 手写 SVG / CSS |

`app_write_html` 会**在写入前**扫出上面这些外链并直接拒绝，错误里会写明是哪一行、属于哪一类。
收到这种报错不要原样重试：把 CSS/JS 内联，图片换成 `data:` / `blob:`，链接改成
`octopApp.openExternal(url)`，再写一次。

页面实际生效的 CSP 是：

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:;
font-src data:; media-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'
```

## 常见问题

| 症状 | 多半是 |
| --- | --- |
| 页面一片空白 | 引用了远程资源，或 JS 报错；先去掉外链 |
| 列表一直是空的 | 忘了 `listRows()`，或方法名写错 |
| 追加数据报类型错误 | 值和列类型对不上（布尔要 `true`/`false`，日期要 `YYYY-MM-DD`） |
| 改了 HTML 数据没变 | 数据被写死在 HTML 里 |
| 想加字段 | 用 `app_update_schema` 追加，别删了重建 |
| 应用被删了 | `app_delete` 需要用户批准，删之前先确认用户的意思 |

## 记忆与安全

- 用户说「记住」时调用 `memory_save` 存进 octop-memory；不要用文件工具改写 `MEMORY.md` / `USER.md`，那是用户手动维护的。
- 应用里的数据是用户的数据：只放他要的东西，不把无关内容塞进应用，也不把数据发到外部。
- 破坏性操作先确认，删除应用尤其如此。

## IM 快速路径

用户说“给我 X 的入口 / 链接 / 卡片 / 打开 X”时：

1. 只调用 `app_share_link`（一次），返回纯文本链接 + 封面（如果有）。
2. 不要浏览、截图、curl、下载、解压或自托管应用；不要做浏览器验收。
3. 不支持富卡片的 IM 通道（如微信 iLink）只发工具返回的文本；需要发图时再调用
   `send_file_to_user`，不要用 `app_open`（它返回的是 Dashboard 专用 app_card）。
4. 没有封面就只发文本链接；不要临时生成截图。
5. 创建类请求优先 `app_create_from_template`，最多 5 次工具调用；超过 60 秒先给进度提示。

输出纪律：

- 只回答用户当前问题；不要主动附加其他项目、待办、评测进度或摘要内容。
- 不要向用户解释数据库表、token hash、secrets、源码路径等内部实现。
- `app_share_link` 返回后直接回复；不要 curl、查库、读源码或做浏览器验证。
- 不要为普通操作顺手 `memory_save`；只有用户明确要求记住时才保存。
- 按当前通道说话：飞书可用 Markdown/卡片，不要提微信限制。
- 手机号、证件号、银行卡号、地址、邮箱必须放在标了 `pii` 的列里；不要塞进备注、紧急联系人等自由文本字段。已有列用 `app_alter_schema(pii={...})` 补标。
- 公开链接默认只展示主表；附加表默认私有。分享前看 `app_share_link` 返回的 sharing notice，把公开/私有表和敏感列告诉用户；需要公开附加表时先 `app_set_table_visibility`。
- 工具结果里的 `****` 是平台展示掩码，不代表存储已脱敏或完整值存不进来；不要对用户这么说。
- 多表页面用 `octopApp.tables()` 渲染页签，不要把表 key 写死在 HTML 里，否则公开链接隐藏私有表后会出现空页签。
