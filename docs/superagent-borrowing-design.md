# Super-agent 控制面借鉴设计（修订版）

Status: design record / draft
Date: 2026-11-06（v2 修订 2026-11-07）
Scope: 把 DeepSeek Super-agent（2026-10-31 公布）的控制面模式借到 Octop 的
「主动工作型专家 / 外部事件 / 数据处理」三个平面。

配套文档：
- `docs/superagent-borrowing-architecture.md` —— 代码怎么摆（模块、依赖、并发、生命周期）
- `docs/superagent-borrowing-e2e.md` —— 断言规格
- `docs/superagent-borrowing-e2e-story.md` —— 业务故事

---

## 修订记录

**v2（2026-11-07）相对 v1 的改动**

| # | 改动 | 原因 |
|---|---|---|
| 1 | 新增 §0 方案 A/B 对比，放在最前 | v1 把半天能做完的 bug 修复和一个月的工作量打包，读者读到 1910 行后才发现 |
| 2 | §1 把四个必修 bug 独立成节，明确标注「不依赖本设计」 | 同上 |
| 3 | §6.3 明确 delivery 状态机在工单体系下的去留 | v1 含糊，两套状态机打架 |
| 4 | §6.1 记录 `proactive_care_config` 一表多用，给出处理策略 | v1 只字未提 |
| 5 | §6.4 新增权限模型 | v1 只写"跨用户隔离"，没有权限设计 |
| 6 | §7 新增迁移与回滚 | v1 只写怎么开，没写怎么关 |
| 7 | §5.3 把 Gate 从「纯函数」改称「无副作用函数」 | 第 4/5/6 步依赖外部状态，原表述不准确 |
| 8 | §4.2 新增 Assessor 成本模型 | v1 只说"批量省成本"，没算 observe 期成本最高 |
| 9 | §13 删除拿 DeepSeek 数字当容量依据的表述 | 场景不可比，且与 §11 的自我警示矛盾 |
| 10 | §5.5 修正：`autonomy_enabled` 与 `accept_mode` **不是**重复开关 | v1 反思稿里把它列为缺陷，该判断有误（见 §5.5 说明） |

---

## 0. 先读这一页：你该做哪个方案

### 0.1 两个方案

| | **方案 A：止血** | **方案 B：控制面** |
|---|---|---|
| **范围** | 4 个 bug + 会话隔离 + 接受模式 | A 的全部 + 工单 / 直通 / 两级过滤 / 数据回流 |
| **工作量** | **3–4 人日** | **20–30 人日** |
| **新增概念** | 1 个（`accept_mode` 字段） | 8 个（Ticket / Mandate / Gate / Assessor / Stream / Policy / Outbox / Sweeper） |
| **新增表** | **0 张** | 3 张 |
| **拿到什么** | 主动工作不污染 DM；主动工作有安全阀；事件不静默丢失；崩溃不重复执行 | 上述全部 + 跨轮次持续的工作单元 + 同事件流直通 + 语义级过滤 + 数据变化驱动专家 |
| **缺什么** | 同一条订单线的后续事件仍会重复研判；light-app 数据变化对 agent 不可见 | — |
| **风险** | 低（改的是已有路径） | 中（新增一层执行语义，与现有 delivery 状态机需要协调） |
| **适合什么时候** | **现在** | 方案 A 上线 2–4 周、确认"跨轮次工作"是真实需求之后 |

### 0.2 建议

**先做方案 A。** 理由：

1. 四个 bug 是**已存在的缺陷**，修它们不需要任何新架构。
2. 会话隔离是单点改动、收益最大——现状主动工作跑在用户的 DM 线程里，
   既抢锁又污染转录。
3. `accept_mode` 是主动工作能不能安全放开的前提。没有它，数字员工一开就
   可能乱发消息。
4. 方案 B 的价值（工单、直通）依赖"同一件事会跨多轮"这个前提。如果
   A 上线后发现专家的工作大多是一轮就结束，B 的一大半价值就不成立。

**方案 B 保留在本文件后续章节**，等 A 的数据出来再逐段评估。

### 0.3 本文的阅读顺序

- 只想修 bug → 读 §1，其余跳过。
- 要做方案 A → 读 §1 + §5.5 + §5.10。
- 要评估方案 B → 读全部。

---

## 1. 四个必修 bug（不依赖本设计）

这四条都是**已存在的缺陷**，与 Super-agent 借鉴无关。列在这里是因为它们
和主动工作直接相关，且修复成本极低。

### B1. 照护调度器异常后静默死亡

**现状**：`src/octop/infra/proactive/scheduler.py:259` 的 `_on_task_done`
只把 task 从字典里摘掉并打日志，**不重新排期**。

**影响**：`_run_loop` 里任何一次未捕获异常，该 agent 的照护任务就永久消失，
直到配置被写入或进程重启。用户只会觉得"它最近不说话了"。

**修法**：在 `_on_task_done` 里，若 task 未被取消且异常非 `CancelledError`，
按退避延迟重新 `_schedule(agent_id)`；连续失败 3 次后停止重排并记错误。

**验收**：注入一个会抛异常的执行体，断言下一轮调度仍然发生。

**工作量**：约 10 行 + 1 个单测。

### B2. 外部投递租约过期后盲目重跑

**现状**：`src/octop/infra/db/repos/external_deliveries.py:146` 的
`requeue_expired` 把所有过期 `delivering` 无条件改回 `failed`，
再由 worker 重新领取。

**影响**：一次 expert turn 可能已经执行了一半（改了外部系统、发了消息），
进程崩溃后会被**完整重跑一遍**，导致重复外部动作。

**修法**：新增 `started_at` 列。过期时按 `started_at` 分流：

| `started_at` | 处置 |
|---|---|
| 为空（还没开始） | 退回 `pending` 重试 |
| 非空（已开始） | 转 `needs_review`，**不自动重试**，收件箱提示人工确认 |

**验收**：两条投递，一条 `started_at` 为空、一条非空，租约过期后分别落到
`pending` 和 `needs_review`；断言非空那条没有再次调用执行体。

**工作量**：1 个迁移列 + 2 个状态 + 约 30 行。

### B3. 限流命中静默丢弃事件

**现状**：`src/octop/infra/events/service.py` 的 ingest 在订阅级限流命中时
直接 `continue`，**不生成 delivery 行、不报错、不记录**。且
`SlidingWindowLimiter` 是内存态，重启清零（`src/octop/infra/events/rate_limit.py:3`）。

**影响**：事件在数据库里存在，但没有任何人知道它被丢掉了。
运维无法回答"这条事件为什么没反应"。

**修法**：命中限流时生成一条 `status='skipped'` 的投递记录，带
`skipped_reason='rate_limited'`；`external_events` 记录照常保留。

**验收**：限流触发后，`GET /api/external/deliveries` 能看到 skipped 记录；
事件仍在 `external_events` 中；可手动重放。

**工作量**：约 20 行 + 1 个集成测试。

### B4. 主动工作跑在用户的 DM 线程里

**现状**：
- cron：`src/octop/infra/cron/manager.py:129` 用
  `ThreadRegistry.dashboard_key(agent_id, user_id)`
- 数字员工：`src/octop/infra/agents/teams/runtime.py:376` 取
  `list_threads(limit=1)`，即最近一条已有线程
- 照护：复用 IM 会话

**影响**：
1. 主动唤醒与用户消息抢同一个 session lock，互相阻塞。
2. 主动产出的内容混进用户的聊天转录。
3. 用户无法区分"我说的话"和"它自己做的事"。

**修法**：三条路径统一改用独立会话命名空间，照抄
`src/octop/infra/events/expert_target.py:32` 的 `external_session_key` 做法：

```python
work_session_key(agent_id, scope) =
    ThreadRegistry.make_key(
        agent_id=agent_id,
        channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
        channel_subject_id=scope,        # 'cron' | 'autonomy' | 'care'
        channel_chat_type="work",
    )
```

**验收**：cron 触发的产出落在 work 会话；用户的 DM 会话里不出现该轮内容；
两者不共享 session lock。

**工作量**：3 处改动 + 面板展示分组（**面板是必需的，见 §7.1**）。

### 1.1 B1–B4 的合计

| 项 | 人日 |
|---|---|
| B1 | 0.3 |
| B2 | 0.7 |
| B3 | 0.5 |
| B4 | 1.0 |
| 面板分组（B4 的必需配套） | 1.0 |
| 回归测试 | 0.5 |
| **合计** | **4.0** |

---

## 2. 方案 B 的结论与范围

> 以下章节描述方案 B。若只做方案 A，可跳过。

### 2.1 结论

Octop 的**团队平面已有 L3 控制面**（`docs/team-l3-roadmap.md`：Decision Service →
Gate → Dispatcher → Executor → Verifier），但**单专家 / 外部事件 / 数据三个平面没有**。

方案 B 补上这三个平面，核心是三个决定：

1. **授权（Mandate）与路由（Subscription）正交**——订阅只管"事件送到哪"，
   授权独立判断"这件事允许做到哪一步"。
2. **两级过滤**——Gate 先做确定性准入，Assessor 再做语义研判；
   `off / observe / auto` 三档作为安全阀。
3. **直通**——已归属的事件直接进工单，不再回全局评估。

### 2.2 范围

**做**：统一的工单模型、两级过滤、接受模式、会话隔离、直通、看门狗、
结果投递、light-app 数据回流。

**不做**：替换 harness、外部 Watchdog Agent、跨设备存算分离、Router 水平切分、
合并 `team_tasks`。

### 2.3 假设

- A1：单专家的自主工作与外部事件触发的工作是**不同触发源、同一类工作单元**。
- A2：现有 `autonomy_enabled` 开关保留（语义见 §5.5）。
- A3：团队平面（`team_tasks`）保持独立，工单不与之互引状态。
- A4：数据库未发布版本为 29（`tests/unit/db/test_db_pool.py:113` 断言 `v == 29`；
  `main` 上只到 021）。本设计的 DDL 折叠进 029。

---

## 3. 现状核实

### 3.1 三个平面

**主动工作型专家**：三条互不相干的路径（见 B4 的现状），没有统一的
"该不该开始工作"的决策点。

**外部事件**：骨架已在——inbox/outbox + 条件 UPDATE lease + 指数退避 +
死信 + 30 天保留（`src/octop/infra/db/migrations/027_external_events.sql`）。
缺的是授权层与上下文层：

| 缺口 | 证据 |
|---|---|
| 只有程序级过滤，无语义级 | `src/octop/infra/events/service.py:79` |
| 上下文有损，只给 500 字符摘要 | `src/octop/infra/events/service.py:51` `MAX_SUMMARY_CHARS` |
| 无"事件流归属" | `src/octop/infra/events/worker.py:422` 的 `_queue_expert` 只是时间窗合并 |
| 无 Watchdog | 见 B2 |
| 256 KiB 硬上限，无 artifact 卸载 | `src/octop/infra/events/security.py:16` |

**数据处理**：

| 缺口 | 证据 |
|---|---|
| 同步运行为非原子：写行与回写 cursor 分离 | `src/octop/infra/apps/service.py:3065`、`:3091` |
| 源端无分页，直接截断 | `src/octop/infra/events/connector_poll.py:116` |
| 无 ETL 层，映射只有列名对应 | — |
| **同步结果不回流成事件** | `src/octop/infra/events/service.py:31` 的 `SOURCE_KINDS` 只有两种 |

### 3.2 已经做对、不要动的东西

- 事件幂等摄取：`external_events.dedupe_key UNIQUE`。
- 事件不可变信封：原始 payload + 摘要 + 字节数。
- 投递条件 UPDATE 租约：`external_deliveries.claim`。
- 每类工作独立并发上限：`MAX_CONCURRENT_APP=4`、`MAX_CONCURRENT_TEAM=2`、
  每 agent 一个专家信号量（`src/octop/infra/events/worker.py:42` 附近）。
- 团队平面的确定性派发 + 事件账本 + 崩溃恢复。

---

## 4. 借鉴映射

### 4.1 概念对照

| DeepSeek 概念 | Octop 现状 | 落点 |
|---|---|---|
| Source | `external_sources`（2 种 kind） | 扩展 kind |
| Trigger | `external_events` | 加 `stream_key` |
| Thread | 无 | `stream_key`（用 stream 命名，避开 `threads` 表） |
| Gate | `matches_filter` | 重写为六步准入链 |
| Assessment | 仅团队路径有 | 新增 `infra/work/assessor.py` |
| accept mode | 无 | `autonomy_policy` 新列 |
| Mandate | 散在订阅与策略里 | 工单的 `scope_json` / `strength` / `policy_version` |
| Ticket | 无 | `work_tickets` |
| Subscription | `external_subscriptions` 三职合一 | 保留路由职责，授权移到工单 |
| Pass-through | 只有时间窗合并 | `work_ticket_streams` |
| Inline & Spill | 只给 500 字符摘要 | 事件卡 + `work_read_event` |
| Watchdog | `TeamSweeper`（仅团队） | 扩到工单表 |
| Outbox | 无 | `work_outbox` |

### 4.2 Assessor 的成本模型

> 这一节是 v2 新增。v1 只说"批量研判省成本"，没算清代价。

| 阶段 | 每次事件是否调用模型 | 成本 | 产出 |
|---|---|---|---|
| `off` | **否** | 0 | 无 |
| `observe` | **是**（每次都要判断"本来会做什么"） | **最高** | 决策记录，无执行 |
| `auto` | 是 | 高 | 工单 |

**反直觉的地方**：`observe` 期成本最高——每条候选都要研判，但不产生任何执行价值。
一个 agent 每天 50 条候选，observe 一周就是 350 次调用。

**缓解**：

1. **采样**：`observe` 期按 `observe_sample_rate`（默认 1.0，可降到 0.2）抽样研判。
2. **批量**：同一 pass 内所有候选合并为一次调用（`auto` 与 `observe` 都适用）。
3. **观察期定长**：建议 3–7 天，而不是无限期。
4. **指标先行**：先看 `work_gate_total` 的通过率，通过率极低的源直接关掉订阅，
   不进入研判。

**不做容量承诺**：DeepSeek 的实验是编码任务、单仓库、300 条消息、worker 用
只耗时工具代替，与 Octop 的订单/IM 场景不可比。任何"省 X%"的说法在这里都
不成立，需要 Octop 自己的实测。

---

## 5. 目标架构

### 5.1 概念与承载

| 概念 | 含义 | 承载 |
|---|---|---|
| Trigger | 一次不可变的环境事件，幂等 ID | `external_events`（已有） |
| Stream | 事件流键 | `external_events.stream_key`（新增） |
| Gate | 确定性准入 | `infra/work/gate.py` |
| Assessor | 语义研判 | `infra/work/assessor.py` |
| Ticket | 被授权的、跨轮次持续的工作单元 | `work_tickets` |
| Mandate | 授权：范围、强度、策略版本 | 工单列 |
| Subscription | 事件流 → 工单，只管路由 | `work_ticket_streams` |
| Worker | 执行工单的隔离会话 | 会话键 `agent:{id}:work:{tid}` |

### 5.2 全局流程

```
环境事件 → Source（webhook / connector_poll / light_app / internal）
   │
   ▼
Gate（无副作用，零 LLM）
   1 源启用  2 订阅过滤  3 职责范围  4 活跃时段  5 预算  6 限流
   拒绝 → filtered / deferred / skipped（三档必须可区分，见 §5.3）
   │
   ▼
Assessor（语义，一次批量调用）
   off → 结束   observe → 记录   auto → 通过则建工单
   │
   ▼
Ticket + Mandate + Subscription（登记 stream_key）
   │
   ▼
Worker（隔离会话）→ Report → Outbox → IM / 面板
```

### 5.3 Gate：无副作用准入链

> v2 修正：v1 称其为"纯函数"不准确——第 4/5/6 步依赖时段、预算计数、限流计数，
> 这些都是外部状态。准确说法是**无副作用**：不写库、不调模型、不改计数器
> （除非调用方显式传入可变计数器）。

| 步 | 判定 | 数据来源 | 拒绝语义 |
|---|---|---|---|
| 1 | 源存在且启用 | `external_sources.enabled` | `filtered` |
| 2 | 订阅过滤命中 | `external_subscriptions.filter_json` | `filtered` |
| 3 | 职责范围命中 | `responsible_scope` / `tool_scope_allow` | `filtered` |
| 4 | 活跃时段命中 | `active_hours_start` / `end` | **`deferred`** |
| 5 | 预算未超 | `budget_limit_tokens` / `sla_tasks_per_day` | **`deferred`** |
| 6 | 限流未命中 | `external_subscriptions.max_per_minute` | **`skipped`** |

**三种"不做"的语义**：

| 标记 | 含义 | 自动恢复 | 数据痕迹 |
|---|---|---|---|
| `filtered` | 不该做 | 否 | 事件记 `filtered` + reason |
| `deferred` | 稍后再做 | **是**（到时段 / 额度恢复） | 事件记 `deferred` + `next_retry_at` |
| `skipped` | 这次不做 | 否 | 投递记 `skipped` + reason，可手动重放 |

### 5.4 Assessor：语义研判

Gate 放行后，候选进入语义研判。它回答的是"这件事值不值得做"，
**不负责建工单**（那是 router 的事，两者的失败处理不同，见 §10）。

| 项 | 规格 |
|---|---|
| **批量** | 一个 pass 内所有候选合并为一次 LLM 调用 |
| **输入：事件卡** | event_id、源、类型、时间、摘要（≤500 字符）、payload 字节数、读取入口 |
| **输入：邻近事件** | 同 `stream_key` 前后各 5 条，每条 ≤400 字符（紧邻 2 条放宽到 1200） |
| **输入：相关工单** | 进行中的工单最多 8 条 + 总数 |
| **输入：规则** | 启用的规则列表（名称 + 指令）、接受模式、策略版本 |
| **输出** | 决定（accept / stop / ignore / unavailable）+ 依据（规则 id + 证据） |
| **规则要求** | 每条启用规则恰好判定一次（命中或不命中都要判）；接受至少需要一条命中 |

**失败处理**：超时、解析失败、模型报错 → 记 `unavailable`，可重试。
下一次同一 stream 的事件触发新一轮研判。

### 5.5 接受模式（accept mode）

| 模式 | 行为 |
|---|---|
| `off` | 推断出的候选一律不成为工作；**明确点名的请求仍然可用** |
| `observe`（默认建议） | 研判但不接单，记录"本来会做什么" |
| `auto` | 研判通过即接单 |

成本特征见 §4.2：**`observe` 期成本最高**——每条候选都要研判，但不产生执行价值。

### 5.6 接受模式与 `autonomy_enabled` 的关系（v2 修正）

> **更正**：v1 反思稿把这两个字段列为"重复开关"，该判断有误。

两者语义确实不同：

| 字段 | 位置 | 语义 |
|---|---|---|
| `autonomy_enabled` | `agents` 表（`src/octop/infra/db/repos/agents.py:78`） | 这个 agent **是否允许**做任何自主工作 |
| `accept_mode` | `autonomy_policy`（新增列） | 允许时，对**推断出的**候选接受到什么程度 |

组合语义：

| `enabled` | `mode` | 行为 |
|---|---|---|
| false | 任意 | 完全不做自主工作 |
| true | `off` | 只做明确点名它的请求，不做任何推断工作 |
| true | `observe` | 记录推断工作，不执行 |
| true | `auto` | 执行推断工作 |

**`enabled=false` ≠ `mode=off`**：前者是全关，后者是"只接明确请求"。

**仍然存在的问题**是用户心智负担（两个字段、四种组合，其中
`false + 任意` 都是同一种行为）。处理策略：

- **保留两个字段**（迁移成本低于收益），但在 API 层提供一个合并视图
  `proactive_level ∈ {disabled, explicit_only, observe, auto}`。
- 面板只展示合并视图，内部映射回两个字段。
- 文档与 API 描述里必须写清 `enabled=false` 覆盖一切。

### 5.7 Mandate 与策略版本

工单的授权字段：

| 字段 | 含义 |
|---|---|
| `scope_json` | 允许做到哪一步（职责范围、工具范围、预算上限快照） |
| `strength` | `explicit` 或 `inferred` |
| `policy_version` | 接受时依据的策略版本 |

**版本计算**（每 agent 一份）：

```
policy_version = sha256(
    accept_mode | rules_json | responsible_scope
    | tool_scope_allow | tool_scope_deny
    | 该 agent 名下所有 external_subscriptions 的
      (subscription_id, strength, scope_json) 排序串
)
```

**策略变更时**：不撤销已开工单；向受影响工单发一条 Input 说明变更；
**恢复工单前必须重校验**：

| 授权类型 | 重校验条件 |
|---|---|
| `explicit` | 策略仍在、源仍在听 |
| `inferred` | `accept_mode` 仍为 `auto`，且命中规则仍启用 |

重校验失败 → 工单保持结束，输入回到 Assessor。

### 5.8 Ticket 状态机

| 状态 | 含义 | 终态 |
|---|---|---|
| `pending` | 已授权，等待派工 | 否 |
| `working` | 已派工，执行中 | 否 |
| `waiting` | 本轮结束，等待后续触发 | 否 |
| `blocked` | 受阻，需要人介入 | 否 |
| `failed` | 失败，可重试 | 否 |
| `done` | 完成 | 是，但可被 `pass` 直通恢复（§5.9） |
| `declined` | 研判后不接单（含 observe 记录） | 是 |
| `stopped` | 人主动停止 | 是，不可恢复 |
| `cancelled` | 来源消失 | 是，不可恢复 |

展示阶段由状态推导，不单独存字段。

### 5.9 直通与恢复

工单建立时登记关注的 `stream_key`：

| 模式 | 行为 |
|---|---|
| `pass` | 后续同 stream 事件直接作为输入，不再走 Assessor；**可恢复 `done` 工单** |
| `context` | 只作上下文送达，不授予权力、**不恢复**工单 |

**恢复约束**：

| 约束 | 规则 |
|---|---|
| Mandate 重校验 | 恢复前按 §5.7 校验；失败则保持 `done`，输入回 Assessor |
| 恢复窗口 | 默认 7 天（可配），从 `ended_at` 起算 |
| 次数上限 | 默认 3 次（可配） |
| 不可恢复 | `stopped` / `cancelled` 永不恢复 |

> **与 DeepSeek 的偏离**：DeepSeek 规定机器事件不唤醒已结束工单，只有人的消息可以。
> Octop 的订单 / CI 场景里，机器事件恰恰是最需要延续同一件事的触发源
> （付款是订单风险评估的下一环）。因此允许机器事件恢复，用**窗口 + 次数 + 重校验**
> 替代"只认人"。这是有意的偏离。

### 5.10 上下文组织：Inline & Spill

| 内容 | 处置 |
|---|---|
| 事件摘要（≤500 字符） | Inline |
| 事件关键字段 | Inline |
| 完整 payload | **Spill**——只保留 `event_id` 引用 |
| 同 stream 邻近事件（前后各 5 条） | Inline，有长度上限 |
| 更早历史 | Spill，只留引用链 |

新增 agent 工具 `work_read_event(event_id)`：返回截断内容 + 继续读取游标。

### 5.11 Worker 会话隔离

```python
work_session_key(agent_id, ticket_id) =
    ThreadRegistry.make_key(
        agent_id=agent_id,
        channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
        channel_subject_id=ticket_id,
        channel_chat_type="work",
    )
```

每个工单一个隔离会话，与人类 DM 线程完全分开。**这条是方案 A 与 B 共用的**。

### 5.12 看门狗

`WorkSweeper`（可挂在 `TeamSweeper` 同频或独立）：

| 扫查项 | 处置 |
|---|---|
| 租约过期且未开始 | 退回 `pending` |
| 租约过期且已开始 | `needs_review`，不自动重跑 |
| `working` 心跳超时 | `blocked` + 附最后 Report |
| `pending` 超派工时限 | 重排一次，再超则 `failed` |
| 活跃时段结束仍有 `working` | 不打断 |
| 目标已删除/停机 | `cancelled` |

### 5.13 结果投递（Outbox）

`work_outbox` 表：幂等键、归属工单、目标会话、用途
（`acknowledgement` / `answer` / `result` / `attention`）、状态、重试字段。

**顺序**：同工单同会话严格有序（前一条失败 3 次后放行）；不同工单不互相等待。

### 5.14 三个平面的投影

- **自主体**：心跳或 cron 产出 `origin_kind='autonomy'` 的 Trigger；Gate 第 4/5 步生效。
- **外部事件**：现有 `external_subscriptions` 保留路由职责，加 `strength` / `scope_json`。
- **数据**：`external_sources` 新增 kind `light_app`；同步结果按规则产出 Trigger。

---

## 6. 数据模型

### 6.1 表角色与拆分决策（v2 新增）

**问题**：`proactive_care_config` 一张表已经服务三个互不相干的功能：

| 功能 | 使用方 |
|---|---|
| 主动照护（定时关心话） | `src/octop/infra/db/repos/proactive_care_config.py` |
| 数字员工策略（duty window / budget / scope） | `src/octop/infra/db/repos/autonomy_policy.py:3` 自述 |
| 工单策略（本设计新增） | 计划再加 `accept_mode` / `rules_json` |

表名从第二个功能起就名不副实。

**决策：本次不拆表。** 理由：

1. 拆表要动 2 个 repo、2 组 API、1 次数据迁移，风险大于收益。
2. 三个功能共用同一行（`agent_id` 主键），拆开还要建关联。
3. 当前没有功能冲突——只是命名问题。

**替代策略（控制债务）**：

- 所有新增列必须带功能前缀：`work_accept_mode`、`work_rules_json`。
- 在表定义处加注释，列出每列的归属功能。
- **触发拆表的条件**：当任一功能需要"同一 agent 多行"时（例如一个 agent 多条工单策略），
  拆表必须提上日程。

### 6.2 新表

```sql
CREATE TABLE IF NOT EXISTS work_tickets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id      TEXT NOT NULL UNIQUE,
  agent_id       TEXT NOT NULL,
  target_type    TEXT NOT NULL,
  target_id      TEXT NOT NULL,
  origin_kind    TEXT NOT NULL,          -- event | autonomy | schedule
  origin_ref     TEXT NOT NULL DEFAULT '',
  stream_key     TEXT NOT NULL DEFAULT '',
  scope_json     TEXT NOT NULL DEFAULT '{}',
  strength       TEXT NOT NULL DEFAULT 'inferred',
  policy_version TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'pending',
  reason         TEXT NOT NULL DEFAULT '',
  session_key    TEXT NOT NULL DEFAULT '',
  attempt        INTEGER NOT NULL DEFAULT 0,
  resume_count   INTEGER NOT NULL DEFAULT 0,   -- 恢复次数上限用
  lease_until    INTEGER,
  last_report    TEXT NOT NULL DEFAULT '',
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  started_at     INTEGER,
  ended_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_work_tickets_open
  ON work_tickets(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_work_tickets_target
  ON work_tickets(target_type, target_id, status);

CREATE TABLE IF NOT EXISTS work_ticket_streams (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  TEXT NOT NULL REFERENCES work_tickets(ticket_id) ON DELETE CASCADE,
  stream_key TEXT NOT NULL,
  mode       TEXT NOT NULL DEFAULT 'pass',
  created_at INTEGER NOT NULL,
  UNIQUE(ticket_id, stream_key, mode)
);

CREATE INDEX IF NOT EXISTS idx_work_ticket_streams_stream
  ON work_ticket_streams(stream_key, mode);

CREATE TABLE IF NOT EXISTS work_outbox (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  outbox_id      TEXT NOT NULL UNIQUE,
  ticket_id      TEXT NOT NULL REFERENCES work_tickets(ticket_id) ON DELETE CASCADE,
  conversation   TEXT NOT NULL,
  purpose        TEXT NOT NULL,          -- acknowledgement|answer|result|attention
  text           TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
  attempt        INTEGER NOT NULL DEFAULT 0,
  next_retry_at  INTEGER,
  last_error     TEXT NOT NULL DEFAULT '',
  dropped_reason TEXT NOT NULL DEFAULT '',
  sent_at        INTEGER,
  created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_outbox_due
  ON work_outbox(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_work_outbox_conversation
  ON work_outbox(ticket_id, conversation, status);
```

### 6.3 delivery 状态机的去留（v2 新增，重要）

**问题**：`external_deliveries` 已有完整状态机
（`pending → delivering → delivered / failed → dead`，
`src/octop/infra/db/repos/external_deliveries.py:102`、`:115`、`:149`）。
若执行语义移到工单，两套状态机会打架。

**决策：明确切分职责，不做大改。**

| 目标类型 | delivery 的职责 | 终态含义 |
|---|---|---|
| `expert.notify` | 推送一条通知 | `delivered` = 已推送 |
| `team.judge` | 交给团队决策账本 | `delivered` = 已记账 |
| `app.refresh` / `app.import` | 触发一次同步 | `delivered` = 同步完成 |
| **`expert.turn`（纳入工单时）** | **只负责"事件 → 工单"这一次转换** | `delivered` = **工单已建立** |

**关键规则**：

1. `expert.turn` 的 delivery 生命周期很短（一次数据库事务），成功建工单即
   `delivered`，之后**不再参与任何重试**。
2. 工单建立后的所有执行失败归 `work_tickets.attempt` 管，与 delivery 无关。
3. 建工单失败（事务回滚）时，delivery 按自己的 `RETRY_DELAYS` 重试。
4. 为避免歧义，新增列 `handed_off_at`，记录"已转交工单"的时间；
   `extern_delivery_id` 继续作为 `work_tickets.origin_ref` 的来源。

**两套退避不会同时生效**：delivery 的退避只覆盖"建工单"这一步（毫秒级，
几乎不会重试）；工单的退避覆盖"执行"（分钟级）。时间尺度不同，不冲突。

**`started_at` 与 B2 的关系**：B2 新增的 `started_at` 对 `expert.turn` 不再需要
（因为执行已归工单）；但对 `team.judge` / `app.*` 仍然需要。保留该列。

### 6.4 权限模型（v2 新增）

工单是**资源**，需要明确的访问控制。规则与现有 `_validate_target`
（`src/octop/infra/events/service.py:398`）保持一致：

| 字段 | 归属 |
|---|---|
| `work_tickets.agent_id` | 工单永远属于某个 agent |
| 可见性 | agent 的 owner（`agents.user_id`）+ admin |

| 操作 | 允许者 |
|---|---|
| 查看工单 | owner、admin |
| 停止工单（`stopped`） | owner、admin |
| 重试工单 | owner、admin |
| 查看工单的输入历史 | owner、admin |
| 跨用户 | **一律拒绝**，即使事件来自他人的源 |

**边界情况**：事件源属于 Alice，订阅目标是 Bob 的 agent——现状
`_validate_target` 已经拒绝这种组合（订阅创建时就校验）。工单层不再重复校验，
但**读取路径**要校验，防止通过工单 id 越权。

### 6.5 既有表的列扩展

| 表 | 新增列 | 说明 |
|---|---|---|
| `external_events` | `stream_key TEXT NOT NULL DEFAULT ''` | 事件流归组 |
| `external_deliveries` | `ticket_id TEXT NOT NULL DEFAULT ''`、`handed_off_at INTEGER`、`started_at INTEGER`（B2） | 投递 → 工单；已转交；已开始 |
| `external_subscriptions` | `strength TEXT NOT NULL DEFAULT 'inferred'`、`scope_json TEXT NOT NULL DEFAULT '{}'` | 授权侧字段 |
| `proactive_care_config` | `work_accept_mode TEXT NOT NULL DEFAULT 'off'`、`work_rules_json TEXT NOT NULL DEFAULT '[]'` | 带 `work_` 前缀，见 §6.1 |

### 6.6 `stream_key` 的派生（v2 修正）

> v1 说"从 payload 猜 `order_id` / `conversation`"，这个做法脆弱且不可预测。

**改为显式声明 + 兜底**：

| 源类型 | 派生方式 |
|---|---|
| webhook / connector_poll | 源配置里的 `stream_field`（点路径，如 `order_id`）；**未配置则用 `external_id`** |
| light_app | `app_id:table` |
| autonomy | `heartbeat:{agent_id}` |
| 兜底 | `event:{event_id}`（每个事件独立成流） |

**不猜测**。某类业务希望按订单聚合，就在建源时写 `stream_field: "order_id"`。
这把"聚合粒度"变成一个显式配置，而不是代码里对业务字段的硬编码假设。

派生函数放在 `infra/work/model.py`（无副作用），由 `infra/events/` 调用。

### 6.7 迁移落点

未发布版本为 29（`tests/unit/db/test_db_pool.py:113`；`main` 上只到 021）。
按仓库规则折叠进 029：

1. `029_light_app_publications.{sql,pg.sql}` 追加本设计的 DDL；
   为反映内容，重命名为 `029_light_app_publications_and_work_control.{sql,pg.sql}`
   （加载器只认编号，`src/octop/infra/db/migrate.py:50` 的正则 `^(\d{3})_.*\.sql$`
   允许重命名）。
2. `migrate.py` 新增幂等助手 `_ensure_work_control_schema(db)`，在
   `version == 29` 分支调用（`src/octop/infra/db/migrate.py:2556`）。
   用 `CREATE TABLE IF NOT EXISTS` + `_ensure_column`（`:112`）保证可重复执行。
3. `test_db_pool.py:113` 的 `v == 29` 断言**保持不变**。

**不切 030**——029 尚未发布，切号会让本地库跳版本。

### 6.8 跨平台与幂等

- DDL 同时提供 SQLite 与 PostgreSQL 两份，类型用 `TEXT` / `INTEGER`
  （对齐 `027_external_events.sql` 的风格）。
- `ALTER TABLE ADD COLUMN` 在 SQLite 上不可重复，由 `_ensure_column` 保证。

---

## 7. 迁移与回滚（v2 新增）

> v1 只写了"怎么开"，没写"怎么关"和"存量怎么办"。这一节补齐。

### 7.1 B4 会话隔离的存量处理

**问题**：换会话键后，用户旧的定时任务 / 主动工作记录仍在旧会话里，
新会话是空的，用户会看到两套历史。

**策略**：

| 项 | 处理 |
|---|---|
| 旧会话 | **保留**，不删除、不迁移数据 |
| 新会话 | 从空开始 |
| 面板 | 会话列表里同时展示旧会话与新的 work 分组；旧会话标注「历史（定时任务）」 |
| 迁移数据 | **不做**。迁移会改变既有转录的归属，风险大于收益 |
| 用户困惑 | 通过面板文案解决（"从这里开始，定时任务记录移到「工作记录」"） |

**回滚**：把 `session_key` 改回原值即可。旧会话一直在，新会话里的内容
通过 `threads` 表可以直接读，不会丢。

### 7.2 `work_control_enabled` 的关闭语义

| 场景 | 行为 |
|---|---|
| 关闭时新建工单 | 拒绝；事件回落到旧路径（直接执行） |
| 关闭时**已在途**的工单 | **允许完成**（不半途而废），但不接受新的直通输入 |
| 关闭时 `pass` 订阅 | 保留，但直通输入不再恢复 `done` 工单 |
| 重新打开 | 在途工单继续；`done` 工单的恢复窗口按 `ended_at` 重算 |
| `work_tickets` 数据 | **保留**，作为审计记录；不清理 |

**回滚到改动前**：设 `work_control_enabled=false` 并重启。旧路径完整保留，
行为与改动前一致（这是架构文档 §14 的验收项之一）。

### 7.3 数据库迁移的回滚

schema 迁移**不做自动回滚**（与仓库既有约定一致，`_schema_version` 只增不减）。
如果需要回退，走 `octop backup` 恢复。

### 7.4 B2 的 `needs_review` 存量影响

新增 `needs_review` 状态后，**历史上已经发生的重复执行无法追溯**。
该状态只对新增投递生效。文档与发布说明里要写清这一点。

---

## 8. 实施阶段

### 方案 A（3–4 人日，建议先做）

| 编号 | 内容 | 依赖 | 人日 |
|---|---|---|---|
| A0 | B1 照护调度器异常重排 | — | 0.3 |
| A1 | B2 投递租约 `started_at` 分流 | 1 迁移列 | 0.7 |
| A2 | B3 限流留痕 | — | 0.5 |
| A3 | B4 会话隔离（cron / autonomy / care） | — | 1.0 |
| A4 | 面板「工作记录」分组 + 旧会话标注 | A3 | 1.0 |
| A5 | `work_accept_mode` 字段 + 三档行为 + API | — | 0.5 |

> A5 只加字段与开关判断，**不建工单**：`off` 时行为不变，`observe` 时写一条
> 决策记录到日志/收件箱，`auto` 时沿用现有的直接执行路径。
> 这样方案 A 不引入任何新表。

### 方案 B（17–26 人日，A 之后）

| 阶段 | 内容 | 人日 |
|---|---|---|
| B-Phase 1 | 工单表 + Gate + Policy + Assessor + observe 落库 | 5–7 |
| B-Phase 2 | 工单派工 + 直通 + `work_read_event` + Sweeper | 8–12 |
| B-Phase 3 | light-app 数据回流 + Outbox | 4–7 |

**门槛**：方案 A 上线 2–4 周后，若以下任一条成立，再启动 B：

- 同一业务对象（订单 / PR / 工单）的后续事件占比 > 20%（说明直通有需求）
- 用户抱怨"同一件事被重复处理"
- 有明确的"跨轮次持续工作"需求

---

## 9. 验收标准

### 方案 A

- 给定照护 task 抛异常，下一次触发仍发生。
- 给定 `started_at` 非空的过期投递，落 `needs_review` 且执行体未被二次调用；
  `started_at` 为空的落 `pending` 并重试。
- 给定订阅限流命中，产生 `skipped` 记录，事件本身仍在。
- 给定 cron 触发，产出落在 work 会话；DM 会话无该轮内容。
- 给定 `work_accept_mode='off'`，推断候选不产生任何工作；明确点名的请求仍可用。
- 给定 `work_accept_mode='observe'`，产生一条决策记录，但不调用执行体。

### 方案 B

见 `docs/superagent-borrowing-e2e.md` 的 E1–E12。

---

## 10. 风险与卡点

| 风险 | 影响 | 缓解 |
|---|---|---|
| 两套状态机并存（`team_tasks` / `work_tickets`） | 认知负担 | 明确边界：团队内部编排 vs 事件触发的对外工作；不互引状态 |
| **delivery 与 ticket 双重记账** | 重试节奏不可预测 | §6.3：delivery 只负责"事件→工单"一次转换，之后不参与 |
| **B4 改变用户可见行为** | 用户以为功能坏了 | §7.1：旧会话保留 + 面板分组 + 文案说明 |
| **observe 期成本最高** | 观察期烧钱无产出 | §4.2：采样 + 定长观察期 + 先看通过率 |
| **`proactive_care_config` 一表多用** | 后续加列归属混乱 | §6.1：`work_` 前缀 + 拆表触发条件 |
| 直通导致授权漂移 | 工单订阅的 stream 上发生的事未必在 scope 内 | 直通输入不授予权力、不恢复工单（除 `pass` 且重校验通过） |
| 恢复窗口 / 次数是拍脑袋值 | 可能过松或过紧 | 做成配置项，默认 7 天 / 3 次，上线后按实际调整 |
| 测试基建要改 | 断言"某 agent 未被调用"需改 fake | 在 `tests/support/fakes.py` 加调用计数器，工作量计入 B-Phase 2 |

---

## 11. 不要现在做

- **不换 harness**。DeepSeek 是 TypeScript + Cordis，Octop 是 Python + LangGraph。
  借模式，不借代码。
- **不做外部 Watchdog Agent**。用确定性扫查器（§5.12）更稳。
- **不做完整 admission table**。DeepSeek 的"接收范围 × 身份 × 是否点名"三格矩阵
  在 IM 场景下粒度过重。先用三档强度 + 职责范围。
- **不做 Router 水平切分 / 多层 Router**。
- **不做存算分离 / 跨设备执行环境**。
- **不合并 `team_tasks` 与 `work_tickets`**。
- **不拆 `proactive_care_config`**（本次）。
- **不为 observe 模式单独建决定表**（落 `work_tickets.status='declined'`）。

---

## 12. 开放决策

| 编号 | 问题 | 选项 | 建议 |
|---|---|---|---|
| D1 | `work_accept_mode` 出厂默认 | `off` / `observe` | `off`（与"Start narrow"一致）；方案 A 阶段尤其如此 |
| D2 | work 会话在面板的呈现 | 独立页面 / 线程列表分组 / 仅 API | 列表内按 `chat_type=work` 分组 + 旧会话标注；**是方案 A 的必需项** |
| D3 | `stream_key` 派生 | 显式配置 / 从 payload 猜 | **显式配置 + `external_id` 兜底**（§6.6） |
| D4 | Assessor 用哪个模型 | 主模型 / 轻量模型 | 轻量模型；需实测 |
| D5 | cron 是否纳入工单 | 纳入 / 保持现状 | **保持现状**。cron 是人工排班，不具备"事件触发 + 授权"语义 |
| D6 | 照护开关与 `work_accept_mode` 的关系 | 独立 / 合并 | 独立。照护是"定时说关心话"，与事件驱动的工作无关 |
| D7 | `escalate_to` 的去向 | 接上 / 删除 | **接上**。现状全代码库无人读取（只有 `src/octop/api/routers/autonomy.py:141` 与 repo 存取） |
| D8 | `work_outbox` 是否独立于 `external_deliveries` | 独立 / 合并 | 独立。语义不同（事件投递 vs 结果投递） |
| D9 | 恢复窗口 / 次数的默认值 | 7 天 / 3 次 | 做成配置；上线后按数据调整 |
| D10 | 方案 A 的 `observe` 记录写到哪里 | 日志 / 收件箱 / 新表 | **收件箱**（复用现有 `GET /api/agents/{id}/inbox`），不建新表 |

---

## 13. 参考

外部资料（2026-11-06 取得）：

- `deepseek-ai/dsh-superagent-prototype` —— 原型仓库与 `docs/000`–`008`
  （`001-domain.md` 领域模型、`002-authorization.md` 授权与接受模式、
  `003-execution.md` 执行与恢复、`007-architecture.md` 架构索引）。
- `deepseek-ai/deepseek-harness` —— Cordis 插件微内核
  （`docs/architecture.md`：Session 事件日志、turn/step 流程、capability seams）。
- 《Super-agent 设计探索：迈向更自主的 Agent 系统》（Harness 团队，2026-10-31）
  —— 五大模块的设计论证。

**引用边界**：原型 README 自述为研究原型、非产品，与文章描述的实现不完全一致；
其消融实验基于**合成负载、单仓库编码任务、worker 用只耗时工具代替**，
数字只说明设计方向，**不作为 Octop 的任何容量、延迟或成本依据**。
本文不引用其具体数字作为论据。
