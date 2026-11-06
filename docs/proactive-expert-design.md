# Octop 主动工作专家（Proactive Expert）可行性分析

> 日期：2026-10-25 · 状态：分析稿 · 基准：`docs/team-autopilot-design.html` v3（团队自动驾驶设计，P1 已落地）
> 方法：两路专用代码调研（专家工具装配与主动关怀 / harness task 工具与单 agent 状态）+ 团队版四路调研结论复用 + 关键点亲自核验。所有结论可溯源至 文件:行号；未经代码验证处明确标注「推测/未验证」。

---

## 一、结论摘要（问题 A）

**判定：有条件可行，且实施成本低于团队版。**

依据（均有代码实据）：

1. **无人值守执行链路对单专家是现成的。** 普通专家默认拥有全部 6 个 `cronjob_*` 自注册工具（cron 工具仅对 team host 跳过，`infra/agents/manager.py:3782-3786`），cron `task_type=agent` 的完整执行链（session 锁内 stream、usage 记账 `source="cron"`、结果推送）与专家无关 kind 地可用（`infra/cron/delivery.py:134-192`）。团队版 P2 要"解禁"的东西，单专家现状就有。
2. **任务/看板底座已落地且 repo 层零 kind 过滤。** `team_goals` / `team_tasks` 的外键是 `REFERENCES agents(agent_id)`，无 kind 约束（`infra/db/migrations/025_light_apps.sql:98-133`）；`team_tasks.assignee` 同样引用 `agents(agent_id)`——专家自治任务 `assignee=self` 与团队派工 `assignee=成员` 在同一张表里语义天然统一。repo（`team_tasks.py`/`team_goals.py`）、状态机（`task_state.py`）、工具实现（`board_tools._tool_ctx()` 从 `current_tool_scope`/configurable 取 agent_id，agent 无关，`board_tools.py:47-60`）全部可直接复用。kind 过滤只存在于两处薄层：API 路由（`teams.py:70-72` 的 `is_team_agent` 检查）与工具挂载条件（`manager.py:3833` 的 `if team_host:`）。
3. **单专家比团队少一整层分布式复杂度。** 无派工/inbox/房间编排/回叫——"读任务→领取→执行→回写→汇报"在**同一个 ReAct 回合**内闭环；`docs/expert-teams.md:156-162` 明示的「inbox 重启丢在途任务」非目标对单专家自治基本不构成威胁（无跨 agent 在途状态）。
4. **单 agent 主动行为已有先例。** 主动关怀（proactive care）具备完整的 per-agent 调度器、免打扰窗、随机频控、去重与推送链路（`infra/proactive/scheduler.py:44-108,274`、`infra/proactive/service.py:117-263`），其策略参数可直接泛化为自治运行策略。

条件（三个硬前置，全部与团队版 P2/P3 共享）：

- **C1 无人值守遇审批挂起**：cron 运行遇 `hitl_required` 现状直接判失败（`delivery.py:166-169`），必须改为挂起+通知（团队版 P3 同一批改动）。
- **C2 预算与闸门**：`usage_log` 可按 agent/user 聚合但**无任何预算检查代码**（`infra/db/repos/usage.py:250-498`，已验证无 budget 逻辑），失控防护必须新建。
- **C3 底座通用化**：工具挂载条件放宽 + agent 通用 API 路由 + i18n 专家变体（均为小改，见 §五）。

不满足 C1/C2 就上线自治 = 团队版 v2 反思里 A1–A7 风险的单专家复刻；满足后单专家自治是团队自动驾驶的 **n=1 特例**，应共用全部底座（详见 §七）。

> **本次补充核验（2026-10-25）**：U1/U2/U4 已验证（附证据），U3 已验证并修正为「串行排队延迟」风险项，U5 仍未验证。详见 §九。

---

## 二、基准设计参照

`docs/team-autopilot-design.html` v3 的核心机制与实施状态：

| 机制 | 设计章节 | 实施状态（v3 记载） |
|---|---|---|
| `team_goals` / `team_tasks` / `hitl_pending` 三表 | §7.1 | ✅ 已落地（迁移折叠进 025，`025_light_apps.sql:98-157`） |
| 主持人 4 工具 `goal_set/task_create/task_update/task_list` | §7.2-① | ✅ 已落地（`infra/agents/teams/board_tools.py`，仅挂 team host） |
| REST `GET/POST goals`、`GET tasks`、`PATCH task (CAS+状态机校验)` | §八 P1 | ✅ 已落地（`api/routers/teams.py:228-300`） |
| 驱动循环 cron（白名单解禁+隐形 wake+预算闸门+kill switch） | §7.3 | ⬜ P2 未实施 |
| HITL 持久化 + 定向推送 + `ask_creator` | §7.4 | ⬜ P3 未实施（表已建） |
| 原生看板 `/teams/:id/board` | §7.6 | ⬜ P4 未实施 |
| 小程序投影/模板 | §7.6-P2 | ⬜ P5 未实施 |
| 已核销结论 | §7.4 callout | C11（write_scope 仅限邮件写）、C12（HITL 拦截为静态 `interrupt_on` 配置，cron 与对话同路径） |

单专家自治的可行性与这些未实施项的**共享度**是本次分析的主线。

---

## 三、现状盘点（问题 B）—— 单专家机制代码索引

### 3.1 运行/调度链路

| 能力 | 现状 | 引用 |
|---|---|---|
| cron 自注册 | 普通专家默认有全部 6 个 `cronjob_*` 工具（list/get/create/update/delete/run_now）；仅 team_host 不装 | `manager.py:3782-3786`、`infra/cron/tools.py:100-246` |
| cron 默认会话 | `session_key = dashboard_key(agent_id, user_id)`（专家自己的 DM 线程）；`fresh_thread` 默认 False | `infra/cron/manager.py:129-132,57` |
| cron 执行链 | `fresh_thread=true` 先 reset 线程；agent 型在 session 锁内 stream；`write_scope(allowed=False)`；输出推送到 session 渠道 + dashboard 通知 | `infra/cron/delivery.py:72-73,134-192,187-192,323-338` |
| 无人值守 HITL | 遇 `hitl_required` 直接判失败（**团队版 P3 待改，同款问题**） | `delivery.py:166-169` |
| 全量工具装配 | knowledge/mobile/light-app/plugin/MCP/skills 均对非 team_host 装配（专家=全量）；`tools_disabled` 可按 agent 裁剪 | `manager.py:3792-3830,3856-3869,3989-4018`、`:166-170` |
| 主动关怀先例 | per-agent asyncio 调度器（默认开启可 opt-out）：`now+rand(min,max)h`、active_hours 免打扰窗、最短睡眠防自旋；生成是**过程式**（非 ReAct）：episode 拓扑→去重→LLM ≤200 字→`push_text_from_session` | `infra/proactive/scheduler.py:44-108,274,314-323`、`server.py:599-615`、`infra/proactive/service.py:117-263`、表 `001_initial.sql:246-265` |

### 3.2 任务与会话模型

| 能力 | 现状 | 引用 |
|---|---|---|
| harness `task` 工具 | 是 **deepagents 子代理派生**工具（非待办）；专家可经 SubagentCatalog 安装子代理后 spawn | `deepagents/middleware/subagents.py:581,837`、`infra/agents/subagents/catalog.py:160`、`api/routers/subagents.py:169` |
| 待办 `write_todos` | 存于 langgraph state（仅随 checkpoint 持久），**fresh thread 即空**——跨会话任务状态必须落 DB（正好是 `team_tasks`） | `langchain/agents/middleware/todo.py:25,174`、`octop_harness/middleware/decision.py:60-83` |
| 会话生命周期 | session_key=`agent:channel:subject:chat_type`；checkpoint 落 `{workspace}/checkpoints.sqlite` 或复用 Memory 实例；`fresh_thread=false` 时经 `aget_history` 复用历史 | `infra/gateway/threads.py:40,362`、`octop_harness/agent.py:1825,1866,581` |
| 记忆 | 每 agent 一个 memory.sqlite（或 Postgres）；`MEMORY.md/USER.md` 每轮全量注入——专家跨会话有稳定"工作记忆" | `octop_harness/builtin/tools/memory_tools.py:60-100`、`agent.py:1251-1263` |
| 互调能力 | `PeerAgentMiddleware` 在 `team_enabled`（harness 默认 True）时挂载→单专家**有** `agent_list/ask_agent`；`team_peers` 未设=可调同 user 全部 agent；但被限 `peer_invoke_mode="sync"`（无 background/inbox） | `octop_harness/agent.py:1546-1550`、`config/__init__.py:701`、`manager.py:4232-4234,4241-4242` |

### 3.3 权限与 HITL

| 能力 | 现状 | 引用 |
|---|---|---|
| 强制 HITL 工具 | `MANDATORY_HITL_TOOLS = {"app_delete"}`——永不旁路（连同 `ask_user_question`）；policy 校验缺失即报错 | `infra/agents/security/hitl_session.py:22,255`、`policy_store.py:65` |
| 线程级 bypass | `allow_all/allow_tools` 存在，但不含 mandatory 清单 | `hitl_session.py:49-94` |
| agent 归属校验 | `admin \|\| owner \|\| shared`（kind 无关）；团队路由另有显式 `is_team_agent` 检查 | `api/common/agent.py:26-36`、`api/routers/teams.py:66-72` |
| HITL 响应入口 | dashboard SSE resume / IM slash 均为既有闭环 | `api/routers/chat/routes.py:105-306`、`gateway/hitl/coordinator.py:481-609` |

### 3.4 数据库与迁移

- `team_goals/team_tasks/hitl_pending` 三表已在未发布的 025 中，FK 全部 `REFERENCES agents(agent_id)`，**无 kind 约束**；`cron_jobs.meta` 列已加（`025_light_apps.sql:98-160`）。
- `usage_log`：token 明细 + cache/reasoning 列，按 agent/user/thread 聚合查询齐备；**无预算检查**（`001_initial.sql:181-190`、`008_usage_cache_tokens.sql`、`infra/db/repos/usage.py:250-498`）。
- `proactive_care_config/care_push_records`：active_hours、min/max_interval、episode 去重（`001_initial.sql:246-265`）。
- `published_experts`：专家模板市场表；用户自建 expert = 普通 `kind=expert` agent，无特殊运行时（`005_shared_experts_sso_knowledge.sql:5-18`、`infra/agents/experts/`）。

### 3.5 看板相关能力

- 无任何已实施看板页（团队 P4 未做）；小程序仍"读+追加"（团队版结论直接适用）。
- 看板底座（表/状态机/CAS/API）即团队 P1 产物，见 §一依据 2。

### 3.6 system prompt 动态注入钩子（自治指导的注入点）

- cron 路径已有按来源拼接 system prompt 的先例 `append_light_app_system_prompt`（`manager.py:3994-3998`）——「主动工作指导」可仿此按 `task_type`/`source` 动态追加，**不依赖工作区种子**（团队版 v3 的 D15 结论直接继承）。
- `agents.system_prompt` 字段修改 → `on_bootstrap_complete` 重编译（`agent.py:1642-1648`）。

---

## 四、机制映射表（问题 C）

| 团队版机制 | 单专家判定 | 说明 / 改造点 |
|---|---|---|
| `team_goals/team_tasks` 表（§7.1） | **直接复用** | FK 无 kind 约束（`025:100,117`）；`assignee=self` 语义统一；表名不改，文档注明 owner 语义 |
| board 4 工具（`board_tools.py`） | **改造后复用** | 实现已 agent 无关（`_tool_ctx`），仅挂载条件 `if team_host:`（`manager.py:3833`）需放宽 + expert 变体 prompt |
| 状态机 `task_state.py` | **直接复用** | `TASK_STATUSES`/`is_valid_task_transition` 与 agent 无关 |
| REST goals/tasks（`teams.py:228-300`） | **改造后复用** | `_require_owned_team` 有 `is_team_agent` 检查（`teams.py:70-72`）→ 新增 agent 通用路由指向同一 repo（见 §五 E1） |
| 驱动循环 cron 解禁（§7.3） | **直接复用（已解禁）** | 专家现有 6 个 cronjob 工具；补 autopilot 模板约束（`cron_jobs.meta`）+ 限频 |
| 隐形 wake（投影过滤） | **直接复用** | 同一投影层改造；单专家是 DM 线程刷屏，问题同构 |
| stale 回收 / kill switch（§7.3 v2） | **简化后复用** | 无 inbox 在途状态；kill switch=停 autopilot job + open tasks 置 cancelled |
| HITL 持久化（`hitl_pending`，§7.4） | **直接复用** | 表已建、session 机制与 agent 无关 |
| `ask_creator` 工具 | **改造后复用（改名 `ask_owner`）** | 同一 interrupt 机制；必须显式注册 `interrupt_on`（v3 C12 结论）；`task_id` 去重列已有 |
| cron 写限制→批准放行（§7.4-⑤） | **直接复用** | 同一 `delivery.py` 改造 |
| 预算闸门 / usage 配额（§7.3 v3） | **直接复用** | usage 聚合现成，预算检查需新建（对两类 agent 通用） |
| `ask_agent` 派工 / 负载均衡 | **可选复用，派工调度不适用** | 专家有 sync `ask_agent`（可作执行手段：调同 user 其他 agent/团队）；但无 background/inbox，无调度职能 |
| 房间 / 上墙 / 成员 HITL 卡 | **不适用** | 单专家 HITL 卡本来就在自己会话（现状闭环） |
| 主动关怀调度策略（care） | **改造后复用（增强项）** | active_hours/随机频控/去重泛化为自治运行策略 |
| 看板页 / 小程序投影（P4/P5） | **必须新建时即按 agent 通用设计** | 尚未实施——避免做出 team-only UI 后返工 |

---

## 五、关键差距与设计调整（问题 D）

### 5.1 MVP（最小可用）

**E1 底座通用化（改动最小的一档）**

- 数据：**零迁移**（三表复用；若要语义清晰可选做 SQL 视图 `agent_goals/agent_tasks`，不推荐增加维护面）。`agents` 行新增开关 `autonomy_enabled`（默认关）→ 折叠进未发布的 025（团队版 B9 结论：025 未进 main/develop）。
- 工具挂载：`manager.py:3833` 条件 `if team_host:` → `if team_host or (autonomy_enabled and not team_host)`；挂的是同一 `build_team_board_tools()`。`assignee` 对单专家恒为自身 agent_id（工具内校验：expert 上下文不允许 assignee 指向他人，防自治专家把任务甩给别的 agent 而无派工语义）。
- API：新增 `api/routers/agent_work.py`——`GET/POST /api/agents/{agent_id}/goals`、`GET /api/agents/{agent_id}/tasks`、`PATCH /api/agents/{agent_id}/tasks/{task_id}`（鉴权 `assert_agent_owner` + admin bypass，沿用 P1 的 CAS 409 + 状态机 400 校验）。`/teams/*` 路由保留不动。
- i18n：`teams.autopilot_briefing` 增专家变体键（en/zh 对齐，跑 `tests/unit/i18n`）——强调"自产自执行自回写、blocked 时 ask_owner"。
- 注入点：仿 `append_light_app_system_prompt`（`manager.py:3994-3998`）按 `source=autopilot` 追加自治指导，不依赖种子。

**E2 驱动循环（与团队 P2 同一批改动，共享代码）**

- 触发：专家自建 cron（能力已有），服务端约束——`cron_jobs.meta.autopilot=true` 时锁定：`session_key=dashboard_key`、`fresh_thread=false`、prompt 固定 wake 模板「读取 open tasks → 领取一条（置 running）→ 执行（工具循环/必要时 sync `ask_agent`）→ `task_update` 关单 → 汇报；blocked 则 `ask_owner`」；interval 下限（建议 ≥30min，单专家比团队更省但更密）。
- 隐形 wake：投影层过滤标记帧（与团队共享同一实现）。
- 预算闸门：单 wake 领取任务数上限（建议 N≤2）、open tasks 上限（建议 ≤8）、（可选）`usage_log` 按 agent 周期配额，超限自动暂停（置 paused + 通知 owner）。
- kill switch：停 `meta.autopilot` job + open tasks 置 `cancelled` + 关联 pending 失效。
- 触发时机补充：date 一次性（"明天早上 9 点做 X"）天然支持——`cronjob_create` 既有能力，无需新代码。
- **U3 核验后新增的待定项（wake 与用户对话的串行排队）**：wake 与用户同会话入站轮次被 session 锁串行互斥（`delivery.py:69-88` 包 `run_in_session`），wake 是完整 ReAct 工具循环，会给同时在线的用户带来可感知排队延迟。E2 设计须把以下列为待定项：
  ① **wake 单轮时长上限**（超时熔断，避免长时间占锁）；
  ② **抢占策略**（候选：唤醒时段避开用户活跃时段 / wake 轮次超时熔断 / 用户消息优先抢占——`run_in_session` 现状是先到先得，需评估在锁队列中给用户入站轮次插队）；
  ③ 保守默认：active_hours 限窗 + 用户活跃时段不排 wake。

**E3 求助闭环（与团队 P3 合并交付，单专家可作试点）**

- `hitl_pending` 持久化：store 双写 + boot 恢复（表已建）。
- `delivery.py:166-169`：遇 HITL → 建 pending 行 + notifications WS 定向推 owner（`chat/notify_ws.py:26`）+ deep_link 到专家会话/看板卡。
- `ask_owner` 工具：`ask_user_question` 包装 + **显式注册 `interrupt_on`**（团队版 §7.4 v3 callout 的实现要求）+ `hitl_pending.task_id` 去重（同任务单活跃 pending）。

**MVP 验证标准**

- 单测：状态机流转（复用 P1 用例模式）、工具挂载条件、meta.autopilot 约束、预算闸门。
- 集成：给专家设目标 → wake cron 触发 → 产出任务并执行关单 → owner 会话收到汇报；wake 中触发工具审批 → owner 批准 → 续跑（含重启恢复）。
- `make all` 全绿；跨平台约定（OCTOP_HOME monkeypatch、Path 断言）。

### 5.2 完整形态

- **E4 看板**：团队 P4 页面按 agent 通用渲染（kind 徽标区分 team/expert，路由统一 `/agents/{agent_id}/board`，团队路径做别名）；同一 CAS/PATCH 路径；服务器时区渲染（D19 继承）。
- **E5 运行策略**：泛化 `proactive_care_config` 为 `autonomy_policy`（active_hours/min_interval/quiet/预算）——care 与 autonomy 共用一套策略存储与求值（`scheduler.py:44-108` 的触发算法直接改造复用）；事件触发器白名单 + 注入防线（外部内容 data-not-instruction，D21 继承）。
- **E6 长任务与组合**：spawn subagent 执行长任务（`task` 工具 + SubagentCatalog 安装流）+ 子任务 stale 回收；小程序投影（团队 P5 共用：`team_tasks` 数据源）；`infra/metrics.py` 观测（wake 执行/失败/任务流转/HITL 超时）。

### 5.3 单专家特有的状态机补充

团队状态机（proposed→…→done）原样适用，但两条流转语义微调：

- `assigned` 对单专家冗余——工具内部在 expert 上下文把 `task_create` 直接落 `proposed`、wake 领取时直接 `running`（跳过 assigned），或允许 `assigned` 仅表示"计划内未开始"（推荐后者，少改状态机）。
- `review`：单专家无主持人审，默认走"自审 + 可选 owner 验收开关"（团队版 A5 结论的 expert 版）。

---

## 六、风险与人工闸门（问题 E）

| 风险 | 单专家场景的形态 | 缓解 / 闸门 |
|---|---|---|
| 失控循环 | 目标误解析→无限产任务/自催 | 预算闸门（E2）；C2 是硬前置 |
| 误操作面大 | **专家有全量工具**（邮件/MCP/文件写）vs 主持人仅 5 工具——自治风险高于团队版 | ① cron `write_scope` 禁写维持 + 批准放行（§7.4-⑤）；② `MANDATORY_HITL_TOOLS` 机制扩展（现有 `{"app_delete"}`，`hitl_session.py:22`，自治上下文可叠加高风险写工具）；③ 线程 bypass 不覆盖 mandatory（`hitl_session.py:255` 已保证） |
| 提示注入 | 外部内容（agently 邮件、connector 同步）流入 wake 上下文即可指挥专家 | 外部内容 data-not-instruction；外部触发器默认不进自治循环（D21 继承） |
| 成本 | wake = 完整 ReAct 工具循环，比 care push（过程式 ≤200 字）贵 1–2 个量级；但无多成员放大 | active_hours 默认窗 + interval 下限 + usage 配额（E5） |
| 自产自审 | 无独立审核环节，质量风险内化 | `review` 状态 + owner 验收开关（默认开）；`success_criteria` 写入 goal 作为 wake 自检依据 |
| 跨 agent 干扰 | sync `ask_agent` 可调同 user 全部 agent——自治 wake 里调他 agent 可能长阻塞对方会话（同成员串行 inbox） | 自治上下文限制 `ask_agent` 目标白名单（同团队版 team_peers 收窄思路）或默认禁用，MVP 可直接不宣传该路径 |
| wake 排队延迟（U3 已验证） | cron wake 与用户同会话串行（`delivery.py:69-88` 包 `run_in_session`），长 ReAct 轮次会阻塞用户消息（或被用户消息阻塞） | active_hours 限窗避开用户活跃时段；wake 单轮时长上限 + 超时熔断；评估用户消息优先抢占 |
| token/费用失控的累计账单 | usage_log 有明细无检查 | E2/E5 周期配额闸门 |

**应保留的人工闸门（不可 bypass）**：`ask_owner` 求助、工具审批（mandatory 清单）、kill switch、`write_scope` 禁写默认、active hours 默认窗。自治专家的"自由度"必须小于对话态专家——无人值守是更严的上下文，不是更松的。

---

## 七、与团队模式的关系（问题 F）

**结论：必须共用同一套任务/看板底座，单专家自治 = 团队自治的 n=1 特例（"单人团队"）。**

共用面（全部已验证可行）：

| 层 | 共用方式 |
|---|---|
| 表 | `team_goals/team_tasks/hitl_pending` 零改复用（FK 无 kind 约束；表名不改，注释/文档注明 owner 语义） |
| repo / 状态机 | `TeamGoalRepo/TeamTaskRepo/task_state.py` 零改 |
| 工具 | `build_team_board_tools()` 同一实现，挂载条件区分；expert 变体仅 prompt |
| API | agent 通用路由（新）与 `/teams/*`（存量）指向同一 repo；CAS/校验同路径 |
| P2/P3 基建 | 隐形 wake、delivery HITL 挂起、预算闸门、kill switch、`ask_owner`/`ask_creator` 同一批改造，两类 agent 同时受益 |
| P4/P5 | 看板与小程序投影从一开始就按 agent 通用建模，团队/专家只是 kind 渲染差异 |

不共用会造成的分裂（反面对照）：两套任务表 → 两套状态机 → 看板两页 → 小程序投影两套 → 预算/HITL 闸门各一份——每一条都是团队版 v2/v3 反思里同类问题的加倍。

**自然统一的加分项**：`team_tasks.assignee REFERENCES agents(agent_id)`——未来"团队派工给一个自治专家"时，团队主持人写 `assignee=该专家`、专家在自己的 wake 里看到并执行/回写，**同一张表天然支持跨模式协作**，无需任何桥接代码。建议团队版 P2 设计 wake prompt 时就把「检查 assignee=自己的外来任务」纳入模板（一行 prompt 的成本，打通两种模式）。

**分工定位**：团队 host 保留"调度/汇总"职能（goal 拆解、派工、`compose_followup`）；单专家自治不做调度（不引入派工队列），只做"自我管理"（目标→任务→执行→汇报）。两者共存于同一 user 时互不干扰：专家自治任务与团队任务共享一张看板（owner 视角全量可见，assignee 徽标区分来源）。

---

## 八、实施路线图

| 阶段 | 内容 | 与团队版关系 | 验证 |
|---|---|---|---|
| **E1 底座通用化** | `autonomy_enabled` 开关（折叠进 025）；挂载条件放宽；agent 通用 API；i18n 专家变体；prompt 注入钩子 | P1 的薄扩展（同 repo） | `make all`；单测：挂载条件/assignee 自我校验 |
| **E2 驱动循环** | `meta.autopilot` 模板锁定 + 限频；隐形 wake；预算闸门；kill switch | **= 团队 P2 主体**（同一批 PR，两 agent 受益） | date 单次 + interval 周期；wake 帧不上墙；重启回收 |
| **E3 求助闭环** | HITL 持久化 + delivery 挂起 + `ask_owner` + 定向通知 | **= 团队 P3 主体**（单专家可作试点场景） | 无人值守审批→owner 批准→续跑；重启恢复 |
| **E4 看板** | agent 通用看板页（kind 徽标） | **= 团队 P4**（建模即通用，避免返工） | 拖拽 CAS/409；双 kind 同页渲染 |
| **E5 运行策略** | care 策略泛化 autonomy_policy；触发器白名单 | 团队版未规划的增量 | 免打扰窗生效；配额暂停 |
| **E6 长任务/组合** | subagent spawn + stale 回收；小程序投影 | = 团队 P5 + 增量 | spawn 任务回收；投影回写 |

顺序约束：E3 的 delivery 挂起改造不晚于 E2 合入（同团队版 P2/P3 前置关系）；E1 可独立先行（只加开关与路由，零风险）；E2/E3 建议与团队版**同一批实施**，避免分叉。

---

## 九、未验证假设与推测清单（2026-10-25 补充核验：U1/U2/U4 已验证，U3 已验证并修正，U5 仍未验证）

| # | 假设/推测 | 影响 | 核验结论（验证方式 / 实证证据） | 状态 |
|---|---|---|---|---|
| U1 | sync `ask_agent` 在 cron `write_scope(False)` 上下文的行为：ContextVar 是否被 peer 继承 | E2 若允许 wake 内调 peer 需验证 | **已验证（保守方向成立）**。`write_scope` 是 `octop.infra.connectors.gateway.adapters.agently_cli` 的 ContextVar `_WRITES_ALLOWED`（默认 True，`agently_cli.py:107-121`），cron 无人值守路径在 `delivery.py:160-162` 用 `with write_scope(allowed=False)` 包住整个 agent stream，写工具经 `agently_cli.py:334` 的 `writes_allowed()` 拦截。实证：scope 内 `writes_allowed()` 为 False，同一协程链内 await 的 peer 调用读到 False，退出 scope 恢复 True；peer 链 `ask_agent → call_peer() → _invoke_peer() → _call_agent() → entry.agent.call(req)`（`octop_harness/teams/tools.py`）全程在 delivery 同一协程链 / 派生 asyncio task 中，ContextVar 随上下文复制继承。**结论**：cron wake 里调 sync `ask_agent`，被调方 Agent Mail 写工具同处禁写态；无需额外防线即可上线 | ✅ 已验证 |
| U2 | `assert_agent_owner` 对 `kind=expert` 行直接可用（kind 无关） | E1 通用路由鉴权 | **已验证**：`api/common/agent.py:18-23` 只判 `user.is_admin` 与 `row.user_id == user.id`，完全不涉及 kind。专家行可直接复用做通用路由鉴权（仍建议 E1 单测带一条 expert 用例作回归） | ✅ 已验证 |
| U3 | cron wake（`fresh_thread=false`）与用户同时在线聊天的并发行为 | E2 体验 | **已验证，结论修正**：`delivery.py:69-88` 的 `deliver()` 整体包在 `run_in_session(...)` 内（`gateway.py:429-439` 委派 channel manager session 锁，注释明写 "Serialize a proactive operation with inbound turns for its session"）——wake 与用户同会话入站轮次**串行互斥**，非并行。用户消息需等 wake 跑完（或反之），带来**可感知的排队延迟**（wake 是完整 ReAct 工具循环）。非安全漏洞，是**体验/超时风险**；缓解见 §五 E2 待定项与 §六风险表 | ✅ 已验证（修正为排队延迟风险） |
| U4 | `PeerAgentMiddleware` 在 cron stream 上下文挂载 | U1 的前提 | **已验证**：`team_enabled: bool = True` 为 harness 默认（`octop_harness/config/__init__.py:701`）；`manager.py:3989` 注释确认 `agent_list`/`ask_agent` 由 PeerAgentMiddleware 挂载且不依赖 `config.tools`；cron agent 型运行走同一装配路径（`delivery.py:135-168` → `agent_manager.stream`），wake 上下文同样具备 peer 工具。与 U1 合并生效 | ✅ 已验证 |
| U5 | langgraph checkpoint resume 在专家自治长任务上的重启恢复 | E3 | 恢复用例未跑；远程后端场景同团队版 C14 | ⬜ 未验证（待集成用例） |

---

## 附：本报告引用与团队版的关系

本文档的团队版机制描述全部引自 `docs/team-autopilot-design.html` v3（章节号通用）；单专家侧新事实来自两路专项调研（专家装配+主动关怀 / harness 状态+usage），关键共用性论据（DDL 无 kind 约束、repo 零过滤、挂载条件、路由 kind 检查）为本会话逐行核验。
