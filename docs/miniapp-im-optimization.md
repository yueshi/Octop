# MiniApp 在微信 / 其他 IM 通道的交互优化

> 状态：方案整理（2026-11-08）
> 范围：MiniApp（轻应用）在微信 iLink、飞书、企业微信、Telegram、QQ 等外部 IM 通道中的
> “查看 / 入口 / 创建”链路。
> 目标：让 IM 用户 **少等待、少看中间过程、少让 Agent 调工具**。

## 1. 问题与事故复盘

2026-11-08 微信实测暴露了三类问题：

| 场景 | 现象 | 根因 |
| --- | --- | --- |
| “给我一个可使用的链接入口” | 2 张图 + 2 条文本送达，但先出现一条 212 字符英文中间旁白，正文含 `##` / `**` / Markdown 表格 | IM 输出没有只保留最终答案；微信不渲染 Markdown |
| “把丫丫小窝给我一个入” | 33 分钟无回复，50+ 次工具调用，用户 `/stop` | 没有 `app_share_link` 工具，Agent 用 `execute` + curl 自托管 6.58MB 游戏，GitHub raw 只有 ~21KB/s，反复超时 |
| 同类下载重试 | `pkill -f duckdl.py` 杀掉自身进程（exit -15），继续重试 | tool guard 只告警不拦截；没有重复失败检测和单轮预算 |

结论：**IM 侧缺少一个“服务端确定性、一次调用交付”的 MiniApp 快速路径**，Agent 只能靠
浏览器、curl、截图和下载去“探索和验证”，必然慢且容易循环。

## 2. 目标与非目标

### 2.1 目标

- “给我 X 的入口 / 链接 / 卡片”：**≤ 2 次工具调用，≤ 10 秒**，不出现浏览器、curl、下载、截图。
- 微信回复：**1 张预览图 + 1 条纯文本**，无 Markdown、无 `[Image: ...]`、无中间英文旁白。
- 创建类请求：模板优先，**≤ 5 次工具调用**；超过 60 秒必须有进度提示。
- 任何 IM 轮次：有单轮工具/时间预算；重复失败 3 次自动停并给出替代方案。

### 2.2 非目标

- 不在 IM 里渲染完整 Dashboard 交互（编辑、拖拽、复杂表格）。
- 不支持微信原生小程序卡片（iLink 没有该 item 类型）。
- 不把 Dashboard 的 `octop_ui` 富卡片直接塞给不支持的通道。

## 3. 设计原则

1. **一次调用交付**：服务端把“查找 app → 生成/复用公开链接 → 选预览图 → 组装通道文案”
   收敛到一个工具里，Agent 不再探索。
2. **通道能力感知**：工具根据 `source`（`weixin/...`、`feishu/...`、`dashboard/...`）返回
   对应格式；不支持富卡片的通道只返回文本 + 图片。
3. **服务端确定性**：链接、预览、权限、数据统计都在服务端完成；Agent 不验证、不截图。
4. **失败可降级**：没有预览图就只发文本链接；链接创建失败返回可操作的错误。
5. **预算与循环保护**：IM 轮次有工具数/时间上限；无进展就停，不要“再试一次”。

## 4. 快速路径设计

### 4.1 意图 → 工具映射

| 用户意图 | 工具 | 调用数 |
| --- | --- | --- |
| “我有哪几个小程序？” | `app_list` | 1 |
| “给我 X 的入口 / 链接 / 卡片” | `app_share_link(app_id 或 name)` | 1 |
| “打开 X”（IM） | `app_share_link`（不要用 `app_open`） | 1 |
| “新建一个 …”（模板可覆盖） | `app_create_from_template` → `app_share_link` | 2 |
| “新建一个 …”（需要自定义界面） | `app_create_from_template` → `app_write_html` → `app_share_link` | 3 |
| “看一下 X 的数据” | `app_read_data` | 1 |

### 4.2 新工具 `app_share_link`

建议放在 `infra/apps/tools.py`，加入 `APP_TOOL_NAMES`。

```python
async def app_share_link(
    app_id: str | None = None,
    name: str | None = None,      # 按名称模糊匹配；与 app_id 二选一
    rotate: bool = False,          # 默认复用已有链接，True 才轮换
    ttl_days: int = 0,             # 0 = 永久
) -> str:
    ...
```

返回（示意）：

```json
{
  "app_id": "app_H3X1VZS7MG",
  "name": "读书记录台",
  "url": "http://111.229.217.154:8088/p/4Gh3...",
  "text": "读书记录台\n个人资料库：书架、摘录、知识卡片。\nhttp://111.229.217.154:8088/p/4Gh3...",
  "media": [
    {
      "type": "image",
      "source": {"type": "url", "url": "file:///root/.octop/agents/main/apps/app_.../preview.svg"},
      "filename": "preview.svg"
    }
  ],
  "channel": {"type": "weixin", "rich_card": false}
}
```

要求：

- **幂等**：`rotate=false` 时复用当前有效公开链接；只有用户明确说“换一个链接”才轮换。
  当前 `create_publication()` 每次都换 token，会把用户刚收到的旧链接作废，必须改。
- **预览图选择顺序**：`apps/<id>/preview.png` → `preview.svg` → `app_set_preview` 生成的封面；
  没有预览就返回 `media: []`，不要现场生成/截图。
- **文本格式**：由服务端生成，按通道规则：微信/QQ 纯文本；飞书/企微可 Markdown。
  链接单独一行，文案 ≤ 200 字。
- **可选一次交付**：把 `media` 块放在工具结果里，并让 `MEDIA_PUSH_TOOL_BASES` 接受
  `app_share_link`，则 IM 投影可以直接发图，Agent 不需要再调 `send_file_to_user`。
  如果暂不做，则 Agent 需要第二次调用 `send_file_to_user(media[0].source.url)`。

### 4.3 `app_open` 的通道行为

- `source` 以 `dashboard` 开头：保持现状，返回 `octop_ui: app_card`。
- 其他通道：返回与 `app_share_link` 相同的文本 + 图片 payload，而不是 `octop_ui`。
- 或者更简单：IM 的系统提示里直接禁止 `app_open`，统一走 `app_share_link`。

### 4.4 通道能力矩阵

| 通道 | text | image | 富卡片/Markdown | IM 推荐输出 |
| --- | --- | --- | --- | --- |
| weixin (iLink) | ✅ | ✅ | ❌ | 预览图 + 纯文本 + 链接 |
| wecom | ✅ | ✅ | Markdown | Markdown + 链接 |
| feishu | ✅ | ✅ | interactive card | 卡片 + 链接（可选降级文本） |
| telegram | ✅ | ✅ | Markdown | Markdown + 链接 |
| qq | ✅ | ✅ | Markdown（失败降级纯文本） | 纯文本 + 链接 |
| dashboard | ✅ | ✅ | `app_card` | `app_open` 富卡片 |

`app_share_link` 只返回 `text` 和 `media`，由通道层决定是否使用 Markdown。

## 5. 提示词与专家手册

在 `infra/agents/experts/library/light-apps/AGENTS.md` 增加“IM 快速路径”一节：

```markdown
## 微信 / IM 快速路径

- 用户说“给我 X 的入口 / 链接 / 卡片 / 打开 X”时：
  只调用 `app_share_link`（一次），不要浏览、截图、curl、下载或自托管。
- 微信 iLink 不支持富卡片：返回预览图 + 纯文本链接；不要用 `app_open`。
- 工具返回 `text` 和 `media` 时：直接发送 `text`；需要单独发图时再调用
  `send_file_to_user(media[0].source.url)`。不要改写文本、不要加 Markdown 表格。
- 没有预览图：只发文本链接，并说明“微信里点链接打开”。
- 创建类请求：优先 `app_create_from_template`；最多 5 次工具调用，不要做浏览器验收。
```

同时在通用系统提示里加通道约束：

- IM 回复 ≤ 800 字，纯文本优先；禁止 `##`、`**`、Markdown 表格。
- 链接单独一行；不要输出中间旁白（“Let me verify…”）。
- 工具调用前不要向用户解释每一步。

## 6. 运行时护栏

### 6.1 单轮预算

- IM 通道（非 dashboard）单轮：**最多 8 次工具调用 / 120 秒**（MiniApp 入口类建议 2 次 / 15 秒）。
- 超预算：停止新工具调用，用已有结果给最终回复；附“可以继续”提示。

### 6.2 无进展检测

- 记录 `(tool_name, normalized_args)` 的重复次数；同一调用失败 2 次或相同参数重复 3 次，
  注入系统提示“该方案不可行，换一种方式或直接回复用户”。
- 同类 `execute` 超时 2 次后，禁止继续同类命令。

### 6.3 进度反馈

- 60 秒没有用户可见文本：发一次“仍在处理，请稍等”。
- 超过 5 分钟：询问“继续 / 停止”，或自动停止并给部分结果。
- 微信 `show_tool_hints=false` 保持不变，避免刷屏；只用一条心跳。

### 6.4 工具安全

- `pkill -f` / `kill` 若匹配当前执行命令或工具进程自身：直接拒绝或要求 HITL 审批。
- `rm -rf` 限制在工作区内；工作区外需审批。
- 长下载（>30 秒）不允许前台阻塞：转后台任务 + 完成后通知。

## 7. 渲染与格式化

- 新增 `format_for_channel(text, channel)`（`infra/utils/llm_text.py`）：
  - `weixin` / `qq`：去掉 `#`、`**`、`>`，Markdown 表格转“字段：值”行；保留换行。
  - `feishu` / `wecom` / `telegram`：保留 Markdown。
- `response_mode.withhold_weixin_stream_preamble`：
  - 最终文本消息到达时，**丢弃** held 的中间旁白，而不是先 flush 再发最终答案。
  - 若最终文本是内容本身（没有单独 MESSAGE），保留 held 文本。
- 微信发送顺序：预览图 → 文本（或文本 → 图，按 `app_share_link` 的 `media` 顺序）。
- 禁止把 `octop_ui` envelope 发给 IM；在 `stream_project` 里过滤。

## 8. 数据与幂等

- **公开链接复用（已实现）**：`light_app_publications` 仍只存 `token_hash`；明文 token 写入
  `secrets` 表的 `light_app_publication_token:<app_id>`。`AppService.get_or_create_publication()`
  默认复用，只有 `rotate=True` 才轮换；应用删除/撤销时清理该 secret。
- **公开地址**：`OctopConfig.public_base_url`（env `OCTOP_PUBLIC_BASE_URL`）提供 IM 可点的
  绝对地址；未设置时从 TLS 域名推导，仍为空则返回相对路径。
- **预览缓存**：`app_share_link` 只读取已有预览文件，不触发生成/截图。SVG 预览标记
  `im_safe: false`，不会自动推到微信（iLink 图片消息需要栅格格式）。
- **权限**：创建/轮换链接需要 `edit_structure`；`app_open` 在 IM 上遇到只读用户时降级为
  名称 + 描述文本，不发相对链接。

## 9. 创建流程的 IM 优化

- **模板优先**：`app_create_from_template`（系统/专家模板）直接带 schema + 界面壳，
  避免 Agent 反复写 HTML。
- **快速模式**：复用 `docs/miniapp-creation-modes-design.md` 的 ViewSpec 快速模式，
  用 schema 自动生成默认表格界面；用户满意再进入自定义。
- **不做浏览器验收**：创建后只做数据/HTML 长度校验；渲染验收由 Dashboard 负责。
- **异步创建**：超过 60 秒的创建任务转为后台任务，IM 先回“已开始创建”，完成后推送链接。

## 10. 实施拆分与验收

### P0（先做，直接解决“慢”和“多调用”）

1. `app_share_link` 工具（含文本 + 预览选择 + 幂等复用/轮换策略）。
2. `app_open` 在非 dashboard 通道返回文本 + 图片，不发 `octop_ui`。
3. 微信/QQ 纯文本格式化；丢弃中间旁白。
4. IM 单轮预算 + 无进展检测（至少覆盖 MiniApp 工具）。

验收：

- “给我读书记录台的入口” → 1 次工具调用；微信收到 1 图 + 1 条纯文本；无 `[Image]`、无 Markdown。
- “把鸭鸭小窝给我一个入口” → 1 次工具调用；不出现 `execute` / `browser_use` / `send_file_to_user` 之外的探索。
- 50 次重复下载场景 → 在 3 次失败后停止并回复替代方案。

### P1

5. 意图路由：在 light-apps 专家手册和通用提示里写死快速路径。
6. 工具安全：拦截自杀式 `pkill`；长下载后台化。
7. 进度心跳（60 秒）。
8. 公开链接 token 复用（`token_enc` 或 HMAC）。

### P2

9. 创建流程模板化/快速模式。
10. `channel_payload` 一次交付（工具结果直接触发文本 + 图片 MessageEvent）。
11. 飞书/企微富卡片适配（`octop_ui` → 平台卡片）。

## 11. 开放问题

1. 公开链接是否允许复用？如果允许，用 `token_enc` 还是确定性 HMAC？
2. 微信是否接受“预览图 + 链接”作为“app 卡片”的最终形态，还是需要引导到 Dashboard？
3. IM 创建类请求是否允许超过 120 秒？如果允许，进度心跳和后台任务的产品形态需要定。
4. 是否所有 IM 都禁用 `app_open`，还是按通道能力矩阵动态路由？

## 12. 关键文件

| 内容 | 文件 |
| --- | --- |
| App 工具注册 | `src/octop/infra/apps/tools.py` |
| 公开链接服务 | `src/octop/infra/apps/service.py`、`src/octop/api/routers/apps.py` |
| 公开页 | `src/octop/api/routers/public_apps.py` |
| IM 媒体投影 | `src/octop/infra/gateway/media/tool_media.py` |
| 流投影 / 停止 | `src/octop/infra/gateway/process/stream_project.py` |
| 通道响应模式 | `src/octop/infra/gateway/process/response_mode.py` |
| 文本清理/格式化 | `src/octop/infra/utils/llm_text.py` |
| 专家手册 | `src/octop/infra/agents/experts/library/light-apps/AGENTS.md` |

## 13. 实现状态（2026-11-08）

P0 已落地：

- `app_share_link`：按 id/名称找 app，服务端复用/创建公开链接，返回纯文本 + 可选封面；
  加入 `APP_TOOL_NAMES`、工具目录、系统提示和 `MEDIA_PUSH_TOOL_BASES`。
- `app_open`：`octop_source` 为 dashboard 时保持 `app_card`；其他通道返回公开链接 +
  纯文本；只读用户降级为名称/描述。
- 微信/QQ 纯文本格式化：`format_channel_text()`；`withhold_weixin_stream_preamble()`
  在最终文本消息前丢弃中间旁白。
- IM 单轮预算：`StreamBudget(max_tool_calls=20, max_seconds=180, max_repeats=3)`；
  超时/超调用/重复失败会取消本轮并回复提示。
- 测试：`tests/unit/apps`、`tests/unit/gateway`、`tests/unit/i18n`、
  `tests/unit/agents/test_agent_manager.py`、`tests/integration/test_apps_api.py` 全绿；
  `make lint` / `make typecheck` 通过。

部署时需要设置 `OCTOP_PUBLIC_BASE_URL`（或 `config.json` 的 `public_base_url`），
否则链接仍是相对路径。
