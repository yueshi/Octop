# 轻应用操作手册

本文件是技术规范。SOUL.md 讲态度，这里讲做法。

## 工具一览

| 工具 | 作用 | 关键约束 |
| --- | --- | --- |
| `app_list` | 列出本 agent 拥有的应用 | 只返回你自己的应用 |
| `app_create` | 建应用并定义字段 | 1-50 个字段；名称 ≤100 字 |
| `app_update_schema` | 追加字段 | 只能加不能改；已有数据时不能加必填字段 |
| `app_write_html` | 写完整界面 | 整页覆盖；必须自包含单文件 |
| `app_read_data` | 读一页数据 | `limit` 1-500，`offset` 分页 |
| `app_append_row` | 追加一行 | 未知字段、类型不符会被拒 |
| `app_delete` | 删除应用 | 需要用户批准 |

工具都返回 JSON 字符串：成功是结果对象，失败是 `{"error": "..."}`。出错先读 error 再改参数，别瞎试。

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
// 列定义 → { columns: [{ name, label, type, required, values }] }
const { columns } = await octopApp.schema();

// 读数据 → { columns, rows, total, limit, offset }，rows 是 [{ 列名: 值 }]
const { rows, total } = await octopApp.listRows({ limit: 200, offset: 0 });

// 追加一行 → { row_index, row }
const saved = await octopApp.appendRow({ title: '写周报', done: false });
```

要点：

- 页面加载时 `listRows()` 一次把 rows 渲染出来，之后改动再读一次。
- `appendRow` 成功后把新行本地加进列表，不要整页刷新。
- 追加失败会 reject（必填没填、枚举值不合法），catch 住给用户一句人话提示。
- 只有有编辑权限的查看者能追加；只读的人看到的是列表，追加入口要禁用。

### 第 4 步 · `app_append_row` 灌初始数据

界面和数据可以换个顺序：先造几条真实数据再写界面，往往更容易想清楚要什么。

```json
{ "app_id": "app_xxxxxx", "row": { "title": "写周报", "owner": "小林", "due_date": "2026-10-24", "done": false } }
```

取值会被按列类型校验和归一化：整数要 `3`，数字要 `3.5`，布尔要 `true` / `false`（也接受 `1` / `0` / `yes` / `no`），
日期要 `YYYY-MM-DD`，时间要 ISO-8601，枚举必须落在声明的 `values` 里。

### 第 5 步 · `app_read_data` 核对

交付前读一遍数据，确认条数、字段、取值都对。

## 沙箱红线

| 不能做 | 原因 | 替代 |
| --- | --- | --- |
| `<link href="https://cdn…">` | 远程资源被 CSP 拦掉 | 内联 CSS |
| `<script src="https://…">` | 同上 | 内联 JS |
| `<img src="https://…">` | 同上，且是数据外泄通道 | `data:` / `blob:` URL |
| `fetch("/api/apps/…")` | iframe 没有凭证，也被禁止联网 | `octopApp.*` |
| 把数据写死在 HTML | 数据不会随 CSV 更新 | `listRows()` |
| 引入第三方图表库 | 远程脚本被拦 | 手写 SVG / CSS |

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
