# 新用户上手引导设计

Status: design record / draft（v2，2026-11-10 评审后修订）
Date: 2026-11-10
Scope: 新用户第一次登录到跑通第一次对话之间的引导。不涉及系统级 Setup 向导
（`/setup` 建管理员、选数据库、配 TLS）。

---

## 0. 结论

**不需要新增「首次登录」标记，判据用现成的：这个用户有没有可用 agent。**

按评审结论，**优先级最高的是管理员分支**——系统装好但没配模型时，管理员自己也是新人，
而且那是真正的死路（走到创建流程第 3 步才发现模型下拉是空的）。

四象限：

| 角色 | 有可用模型 | 没有可用模型 |
|---|---|---|
| **管理员** | 推荐模板 + 一键创建 | **引导去 `/admin/models` 配置**（P0） |
| **普通用户** | 推荐模板 + 一键创建 | 提示「联系管理员」 |

三个补丁：

| # | 补什么 | 优先级 |
|---|---|---|
| 1 | 管理员分支 + 模型前置检查 | **P0** |
| 2 | 推荐模板（按角色分组）+ 一键创建（可见并可切换模型） | P1 |
| 3 | 创建后闭环（跳聊天 / 专家页加按钮 / 专家页推荐区） | P1 |

**工作量 3.0 人日**，不动数据库、不加 API、不改路由表。

---

## 1. 现状

### 1.1 实际流程

```
管理员创建用户 / 用户凭邀请码注册
        │
        ▼
   登录成功 → 跳 /chat
        │
        ├─ 有 agent（含他人共享）→ WelcomeScreen（每个新会话都有）
        │
        └─ 没有 agent → AgentNotReadyScreen
                          「还没有专家」
                          「从专家列表选择模板，快速创建你的第一个 AI 专家。」
                          [去创建专家] → /experts
                              │
                              ▼
                          CreateFromExpertDrawer
                          选模板 → 填名称 / 模型 → 创建
                              │
                              ▼
                          handleCreated(): 停在 /experts，
                          切「我的」tab，新 agent 高亮 1 秒
```

### 1.2 代码事实

| 事实 | 位置 |
|---|---|
| 登录后固定跳 `/chat` | `dashboard/src/pages/Login/index.tsx:176`、`:229` |
| 邀请注册后固定跳 `/chat` | `dashboard/src/pages/Invite/index.tsx:126` |
| 无 agent 时的空状态 | `dashboard/src/pages/Chat/components/AgentNotReadyScreen.tsx:38-58` |
| 创建成功后停在专家页 | `dashboard/src/pages/Experts/index.tsx:347-355` `handleCreated` |
| 模型不可用提示（**只在聊天页**） | `dashboard/src/pages/Chat/index.tsx:1593` `ModelConfigEmpty` |
| 全代码库无 `first_login` / `onboarded` / `welcome_seen` | grep 零命中 |
| `AgentNotReadyScreen` **没有现有测试** | `Chat/components/` 有 31 个测试，该组件空白 |
| 管理员配模型页 | `/admin/models`（`dashboard/src/routes/index.tsx:234`） |
| 创建后状态 | `state: "starting"`、`bootstrap_pending: true` |

### 1.3 关键语义：`noAgents` 包含共享 agent

后端 `/api/agents?scope=mine` 的语义（`src/octop/api/routers/agents.py:228`）：

> Default `scope=mine` returns agents owned by the authenticated user
> **plus agents other users have explicitly shared.**

所以 `noAgents = agents.length === 0` 表示**「连别人共享的都没有」**，
不是「没有自己创建的」。

**这大概率是对的**——有共享 agent 就能用。但实现时要清楚这个边界，
并且当用户只有共享 agent 时，可以在引导里加一句轻提示（"这些专家由其他人共享给你"）。

---

## 2. 判据

| 判据 | 值 |
|---|---|
| 主判据 | `agents.length === 0`（含共享） |
| 次判据 | `/api/providers/resolved` 是否返回非空 |
| 角色 | `user.role === "admin"` |

**不引入 `users.onboarded_at`。** 理由见 §9。

---

## 3. 详细设计

### 3.1 四象限分流（P0）

`AgentNotReadyScreen` 的 `noAgents` 分支改成四象限：

```
noAgents = true
    │
    ├─ 检查模型中… → 骨架屏
    │
    ├─ 模型请求失败（网络 / 5xx）
    │     ┌──────────────────────────────────────┐
    │     │ 无法获取模型列表                      │
    │     │ 请检查网络后重试。                    │
    │     │ [重试]                               │
    │     └──────────────────────────────────────┘
    │
    ├─ 没有可用模型
    │     ├─ 管理员 → 「先配置一个模型」
    │     │            [去配置模型] → /admin/models
    │     └─ 普通用户 → 「还不能创建专家」
    │                  「需要管理员先配置一个模型。」
    │                  [联系管理员]
    │
    └─ 有可用模型
          ┌──────────────────────────────────────┐
          │ 创建专家                             │
          │ 选一个模板，填个名字就能开始。        │
          │                                     │
          │  ┌─────────┐ ┌─────────┐ ┌─────────┐│
          │  │ 通用助手 │ │ 办公自动化│ │ 效率专家 ││
          │  │ 适合起点 │ │ 文档处理 │ │ 任务管理 ││
          │  └─────────┘ └─────────┘ └─────────┘│
          │                                     │
          │ 将使用：MiniMax-M3.1 ▾               │
          │ [查看全部模板]                        │
          └──────────────────────────────────────┘
```

**三种错误语义必须可区分**：

| 情况 | 语义 | 出口 |
|---|---|---|
| 没有模型 | 配置问题 | 管理员去配 / 用户找管理员 |
| 查不到模型 | 连接问题 | **重试按钮**，不降级 |
| 有模型 | 正常 | 推荐卡片 |

> 评审修正：原设计把「查不到模型」降级到「去创建专家」，
> 但用户到了抽屉里模型下拉仍然是空的——问题只是被推迟，且此时用户已填了表单。

### 3.2 推荐模板（P1）

**按角色分组**，不是全局固定三个：

| 角色 | 推荐（id） | 理由 |
|---|---|---|
| 普通用户 | `general-assistant`、`office-automation`、`productivity` | 通用起点 / 文档处理 / 任务管理 |
| 管理员 | `general-assistant`、`business-operations`、`office-automation` | 管理员更可能先验证业务能力 |

**容错**：白名单里的 id 在 catalog 中不存在 → 跳过该卡片；
**三个全部失效** → 降级为「去创建专家」按钮（不是空白页）。

**文件位置**：`dashboard/src/pages/Chat/components/onboardingTemplates.ts`（新文件），
导出 `recommendedTemplates(role: string): string[]`。

### 3.3 模型选择必须可见（P1）

**评审修正**：原设计用「第一个 resolved 模型」，但核实
`src/octop/infra/agents/providers/resolved.py:11` 后发现顺序是
**provider 注册顺序 + manifest 声明顺序**，完全不可控。

风险：第一个可能是最贵的模型，新人一上来就烧钱；管理员调顺序会改变新人的默认值。

**改法**：卡片下方显示一行可点击的模型选择：

```
将使用：MiniMax-M3.1 ▾
```

点开是一个轻量下拉（不是完整抽屉），列出 `/api/providers/resolved` 的全部模型。

默认值规则（写在代码注释里）：
1. 如果存在 `provider.enabled && model` 被标记为默认的 → 用它
2. 否则用 resolved 列表的**第一个**
3. 把这行显示出来，让用户知道是哪个

**把不确定性暴露给用户，而不是藏在代码里。**

### 3.4 一键创建（P1）

点推荐卡片 → 直接创建，**不打开完整抽屉**。

| 字段 | 取值 |
|---|---|
| `name` | 模板 `label_zh`；重名时追加 ` 2`、` 3` |
| `default_model` | §3.3 选择的模型 |
| 其他 | 沿用 `POST /agents/from-expert/{id}` 默认值 |

创建成功后：
- 立即 `navigate(`/chat/${agentId}`)`
- agent 可能处于 `starting`，聊天页会显示「专家正在启动…」——**这是可接受的**，
  因为用户已经到了目的地，而不是停在专家列表里找不到入口

### 3.5 创建后闭环（P1）

| 位置 | 做法 |
|---|---|
| `AgentNotReadyScreen` 一键创建 | 成功后跳 `/chat/{agentId}` |
| `Experts/index.tsx:347` `handleCreated` | **不改**，仍停在专家页 |
| `AgentCard` | **不改**——已有「对话」按钮（`experts.openChat`），`chatReady` 时显示并跳 `/chat/{agentId}` |

> **实现修正**：原设计说要给 `AgentCard` 加 `onStartChat` prop，但核实
> `AgentCard.tsx:520-530` 发现已有对话入口。专家页创建后，agent 进入
> `starting`，按钮先显示「启动」，启动完成后自动变成「对话」。
> 这已经满足"创建后闭环"，无需新增 prop。

**为什么不把专家页也改成自动跳转**：用户在专家页创建时通常想继续挑或配，
强行跳走会打断。

### 3.6 专家页推荐（实现修正：不做）

原设计要加"推荐分区"，理由是"查看全部"跳过去仍是死路。

**核实后取消**：`Experts/index.tsx:420-460` 已有完整的空状态引导——
`StreamSetupGuide` 三段说明（"专家是什么 / 怎么创建 / 用模板更快上手"）
加「新建专家」「去模板库」两个出口。再加推荐分区属于重复。

「查看全部模板」按钮现在跳到 `/experts`，落在这个已有引导上，不是死路。

### 3.7 卡片与网格：完全复用 Experts 页

**不另造卡片，也不另造网格。**

onboarding 的三张推荐卡直接渲染
`dashboard/src/pages/Experts/components/ExpertCard.tsx`，
外层网格直接用 Experts 页的 `.cardGrid`：

```tsx
import expertStyles from "../../Experts/index.module.less";
import { ExpertCard } from "../../Experts/components/ExpertCard";

<div className={expertStyles.cardGrid}>
  {experts.map((expert) => (
    <ExpertCard
      key={expert.id}
      expert={expert}
      lang={lang}
      isInstalled={false}
      disabled={creatingId !== null}
      onCreate={(item) => void handleCreate(item)}
    />
  ))}
</div>
```

| 项 | 值 |
|---|---|
| 卡片 | `ExpertCard`（与专家库视觉完全一致：36px 图标、14px 标题、两行截断描述、"从模板创建"页脚、悬停抬升） |
| 网格 | `expertStyles.cardGrid`（`repeat(auto-fill, minmax(320px, 1fr))`，gutter 12/16px） |
| 容器 | `max-width: min(100%, 1000px)` |
| 列数 | 由容器宽度自动决定：≥984px 三列，≥652px 两列，其余单列 |

**容器为什么要 1000px**：`cardGrid` 的最小列宽是 320px，三列需要
`320 × 3 + 24 = 984px`。原设计把容器定在 620px，会塌成单列大卡片，纵向占满屏。
放宽到 1000px 后，桌面端一行三张，与专家库的观感一致。

#### 为复用而做的一处改动

`ExpertCard` 新增可选 `disabled?: boolean`：

- 点击时短路，加 `aria-disabled`
- 样式 `expertTemplateCardDisabled`：`opacity: .6` + `pointer-events: none`
- 默认 `false`，专家页行为不变

#### 删掉的自造样式

`onboardingCard` / `onboardingCardIcon` / `onboardingCardTitle` /
`onboardingCardDesc` / `onboardingCards` 共约 90 行全部删除。
Chat 侧只剩两条：

```less
.onboardingInner  { max-width: min(100%, 1000px); }
.onboardingModelRow { ... }
```

#### 窄屏

`cardGrid` 自带 `@media (max-width: 640px) { grid-template-columns: 1fr; }`，
无需本地覆盖。

### 3.7.1 "查看全部模板"的去向

跳到 **内置专家 tab**，不是默认的"我的专家"：

```ts
navigate("/experts?tab=library");
```

`Experts/index.tsx:128-138` 从 `searchParams.get("tab")` 初始化，
接受 `my | teams | library | market`，`library` 即内置专家。

模板白名单全部失效时的降级按钮（"去创建专家"）同样跳 `?tab=library`。

### 3.8 文案中性化

**评审修正**：原文案「创建你的**第一个**专家」对"删光 agent 的老用户"不准确。

| 原 | 改 |
|---|---|
| 创建你的第一个专家 | 创建专家 |
| 从专家列表选择模板，快速创建你的第一个 AI 专家。 | 选一个模板，就能开始对话。 |

---

## 4. 不改的地方

| 项 | 原因 |
|---|---|
| 登录后的默认跳转 | 仍 `/chat`；由 `AgentNotReadyScreen` 分流，避免多一处路由判断 |
| `/setup` 向导 | 系统级 bootstrap，与新用户无关 |
| `users` 表 | 不加 `onboarded_at`（见 §9） |
| 邀请注册流程 | 注册完仍跳 `/chat`，落到同一引导 |
| 专家列表排序 | 不在后端加推荐字段；白名单在前端 |

---

## 5. i18n

**沿用现有 `chat.noAgents*` 前缀**（评审修正：原设计用 `chat.onboarding*`，与现有命名不一致）。

| key | zh | en |
|---|---|---|
| `chat.noAgentsCheckModel` | 正在检查模型… | Checking models… |
| `chat.noAgentsModelFailed` | 无法获取模型列表 | Couldn't load models |
| `chat.noAgentsModelFailedHint` | 请检查网络后重试。 | Check your connection and try again. |
| `chat.noAgentsRetry` | 重试 | Retry |
| `chat.noAgentsNoModelAdmin` | 先配置一个模型 | Configure a model first |
| `chat.noAgentsNoModelAdminHint` | 创建专家前，需要至少一个可用的模型。 | You need at least one usable model before creating an expert. |
| `chat.noAgentsGoConfigModel` | 去配置模型 | Configure a model |
| `chat.noAgentsNoModelHint` | 需要管理员先配置一个模型。 | Your admin needs to configure a model first. |
| `chat.noAgentsContactAdmin` | 联系管理员 | Contact your admin |
| `chat.noAgentsTitle` | **创建专家**（改） | **Create an expert** |
| `chat.noAgentsHint` | **选一个模板，就能开始对话。**（改） | **Pick a template and start chatting.** |
| `chat.noAgentsModelLabel` | 将使用：{{model}} | Using: {{model}} |
| `chat.noAgentsViewAll` | 查看全部模板 | View all templates |
| `chat.noAgentsStartChat` | 开始对话 | Start chatting |
| `chat.noAgentsSharedOnly` | 这些专家由其他人共享给你。 | These experts are shared with you. |

按 `AGENTS.md` §7 跑 `uv run pytest tests/unit/i18n -q`。

---

## 6. 指标

**评审新增**：没有指标就无法验证 2–3 人日的投入。

最小方案（后端一行）：

```python
# AgentManager.create() 成功后
if 该 user_id 之前没有 agent:
    METRICS.inc("first_agent_created_total")
```

零新表，复用 `src/octop/infra/metrics.py` 的 `Metrics.inc`。

可选（前端）：记录 `onboarding_shown_total`，但需要新 API，暂不做。

---

## 7. 验收

| # | 场景 | 断言 |
|---|---|---|
| E1 | 普通用户，无 agent，有模型 | 渲染 3 个推荐卡片 + 模型选择行 |
| E2 | 普通用户，无 agent，无模型 | 渲染「联系管理员」，**不显示**模板卡片 |
| E3 | **管理员，无 agent，无模型** | 渲染「去配置模型」，点击跳 `/admin/models` |
| E4 | **管理员，无 agent，有模型** | 渲染管理员版推荐（含 `business-operations`） |
| E5 | 模型请求网络失败 | 渲染「重试」，**不显示**「去创建专家」；点重试重新请求 |
| E6 | 点推荐卡片 | 调用 `POST /agents/from-expert/{id}`，`default_model` 等于模型选择行的值 |
| E7 | 切换模型选择行 | 再次创建时使用新模型 |
| E8 | 一键创建成功 | 路由变为 `/chat/{agentId}` |
| E9 | 白名单中某模板不存在 | 跳过该卡片，其余正常，不抛错 |
| E10 | 白名单 3 个全部不存在 | 降级为「去创建专家」按钮，不是空白 |
| E11 | 专家页创建成功 | **不跳转**，停留 `/experts`，新卡片带「开始对话」 |
| E12 | 点「开始对话」 | 跳 `/chat/{agentId}` |
| E13 | 已有 agent 的用户 | 不渲染任何 onboarding 元素（回归） |
| E14 | 只有共享 agent 的用户 | 不显示引导（回归，`noAgents=false`） |
| E15 | 窄屏（< 640px） | 推荐卡片纵向排列 |

**测试落点**：
- `dashboard/src/pages/Chat/components/AgentNotReadyScreen.test.tsx`（**新建**，需 mock `useNavigate` 与 `providersApi.resolved`）
- `dashboard/src/pages/Experts/index.test.tsx`（若已有则扩展）
- 后端：`tests/unit/agents/` 加 `first_agent_created_total` 计数断言

**测试框架**：Vitest + `@testing-library/react`（`dashboard/package.json:15`、`:61`），
与现有 31 个 Chat 组件测试一致。

---

## 8. 工作量与实现状态

| 项 | 人日 | 状态 |
|---|---:|---|
| 四象限分流 + 模型检查（含重试） | 0.6 | ✅ 已实现 |
| 管理员分支 | 0.3 | ✅ 已实现 |
| 推荐卡片 + 按角色分组 + 白名单容错 | 0.5 | ✅ 已实现 |
| 模型选择行 | 0.3 | ✅ 已实现 |
| 一键创建 + 重名处理 | 0.4 | ✅ 已实现 |
| 创建后跳转 | 0.2 | ✅ 已实现 |
| ~~`AgentCard` 加 `onStartChat`~~ | — | ❌ **已有对话入口，不做** |
| ~~专家页推荐分区~~ | — | ❌ **已有空状态引导，不做** |
| i18n（中英，13 个 key） | 0.2 | ✅ 已实现 |
| 指标（后端） | 0.1 | ✅ 已实现 |
| 测试（9 个场景） | 0.7 | ✅ 已实现 |
| **合计（实际）** | **3.3** | — |

### 8.1 实现清单

| 文件 | 改动 |
|---|---|
| `src/octop/infra/metrics.py` | 加 `first_agent_created_total` 字段与 snapshot |
| `src/octop/infra/agents/manager.py` | `create()` 后判断是否该用户首个 expert，计数 |
| `dashboard/src/api/modules/expertCatalog.ts` | **新增**：`list` / `createFromExpert` |
| `dashboard/src/pages/Chat/components/onboardingTemplates.ts` | **新增**：白名单、`pickRecommended`、`numberedName` |
| `dashboard/src/pages/Chat/components/AgentNotReadyScreen.tsx` | 四象限 + 推荐卡片 + 模型选择 + 重名重试 |
| `dashboard/src/pages/Chat/index.module.less` | 卡片网格与窄屏样式 |
| `dashboard/src/locales/{zh,en}.json` | 13 个 `chat.noAgents*` key |
| `dashboard/src/pages/Chat/components/AgentNotReadyScreen.test.tsx` | **新增**：9 个测试 |

---

## 9. 为什么不用 `onboarded_at`

| | 方案 A（采用） | 方案 B（不采用） |
|---|---|---|
| 判据 | `agents` 为空 | `users.onboarded_at IS NULL` |
| 数据改动 | 零 | 加列（折叠进 030）+ 登录响应 + 标记时机 |
| 能力 | 能覆盖"无 agent"的所有场景 | 能显示"欢迎新用户"专属文案 |
| 局限 | 老用户删光 agent 也会看到引导 | — |

**当前不需要方案 B**：删光 agent 的用户看到"创建专家"是正确的反馈，
把文案中性化（§3.8）后不会造成困惑。

**何时翻回来用**：需要真正的多步教程、按角色区分的首登路径、或"上手完成度"追踪时。

---

## 10. 后续（不在本次范围）

- 引导式任务清单（配模型 ✓ / 创建专家 ✓ / 发第一条消息 ○）
- 首次对话后推荐配置（技能包、知识库、MCP）
- 按用户画像细分推荐（美业 vs 开发者 vs 办公）
- 创建后预填引导消息（当前默认关闭，先看效果）

---

## 11. 评审修订记录

v1 → v2 的改动：

| # | v1 | v2 | 来源 |
|---|---|---|---|
| 1 | 用「第一个 resolved 模型」 | 显示模型并允许切换 | 评审 §1 |
| 2 | 「查看全部」跳到专家页即可 | 专家页补推荐分区 | 评审 §2 |
| 3 | 网络失败降级到"去创建专家" | 改为重试按钮 | 评审 §3 |
| 4 | 未说明 `noAgents` 含共享 agent | 补 §1.3 | 评审 §4 |
| 5 | 推荐模板三个固定 | 按角色分组 | 评审 §5 |
| 6 | i18n 用 `chat.onboarding*` | 沿用 `chat.noAgents*` | 评审 §6 |
| 7 | 文案「第一个专家」 | 中性化 | 评审 §7 |
| 8 | 无指标 | 加 `first_agent_created_total` | 评审 §8 |
| 9 | 无管理员分支 | 提升为 P0，四象限 | 评审 §9 |
| 10 | 未提移动端 | 补 §3.7 | 评审 §10 |
| 11 | 工作量 2.0 | 4.0（说明原因） | 评审 §11 |
| 12 | E5 只说跳过 | 3 个全失效也降级 | 评审 §12 |

**v2 → 实现（2026-11-10）**

| # | v2 设计 | 实际实现 | 原因 |
|---|---|---|---|
| 1 | `AgentCard` 加 `onStartChat` | **不做** | 已有「对话」按钮（`AgentCard.tsx:520`） |
| 2 | 专家页加推荐分区 | **不做** | 已有 3 段空状态引导（`Experts/index.tsx:420`） |
| 3 | 工作量 4.0 | **3.3** | 上面两项省掉 |
| 4 | 15 个验收场景 | **9 个测试** | 合并了同义场景（E6/E7 合并、E9/E10 合并） |
| 5 | 自造 onboarding 卡片样式 | **复用 `ExpertCard`** + 新增可选 `disabled` | 用户指出：不应另造一套卡片 |
| 6 | 自造 3 列 620px 网格 | **复用 `expertStyles.cardGrid`**，容器放宽到 1000px | 用户指出：卡片大小样式应重用；原 198px 窄列压扁了卡片 |
| 7 | "查看全部模板"跳 `/experts` | 跳 `/experts?tab=library` | 应进入内置专家 tab，而非默认的"我的专家" |
