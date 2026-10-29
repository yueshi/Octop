# 团队 L3 自主路线图

Status: design record / draft  
Date: 2026-10-27  
Scope: `docs/team-drive-v2.md` 之后的自主能力演进；不替代现有实现文档。

## 1. 结论

- 当前团队能力处于 **L2 可运行**：目标、任务、依赖、派发、等待、审批、崩溃恢复、决策审计已经可用。
- 但还不是 **L2 可管理**，更不是 L3：缺独立验收、预算硬闸门、异常管理、容量路由和端到端指标。
- 目标 **L3 受约束自治团队**：人给目标、预算、SLA、质量和风险政策；团队在边界内自己计划、执行、验收、返工；人只处理例外和结果。
- 架构方向合理，**不要推倒重来**。保留 host-centric planner + deterministic dispatcher + durable ledger，增加控制面。
- 当前是 **bounded agentic workflow**，不是 fully agentic organization：planner 是无工具的单次 provider 调用，成员是工具型 Agent。对 L3 来说，bounded 是优点，不是缺陷。

## 2. 现状评估

### 2.1 已经合理的部分

- 目标/任务/依赖/优先级/截止时间
- 确定性派发、依赖/并发/忙碌检查
- wait、超时、成员回复结算
- 事件账本、决策审计
- 幂等、CAS/version、崩溃恢复
- 人工审批、暂停/停止、外部事件感知

这些是 L3 的承重墙，不能为了“简化”删掉。

### 2.2 L3 的主要缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| 独立验收 | `review` 只是状态，无验收人/意见/证据 | 无法证明做对了 |
| 预算/策略 | `budget_remaining` 恒为 `None`，派发预算门是死分支 | 无法安全自治 |
| 容量/技能路由 | 成员固定，assignee 由 LLM 选 | 派错人、忙闲不均 |
| 绩效/学习 | 无 KPI/SLA/复盘 | 无法判断是否越跑越好 |
| 人类异常管理 | 有审批/看板，缺收件箱/日报/SLA | 人仍要盯盘 |
| 共享上下文 | 个体 workspace；团队产物/记忆弱 | 交接丢上下文 |
| 规模化 | 单进程编排，无队列/背压/租约 | 多团队互相影响 |

### 2.3 关键代码事实

- `src/octop/infra/agents/teams/decision.py:241`：`budget_remaining` 恒为 `None`。
- `src/octop/infra/agents/teams/dispatcher.py:188`：预算判断永远不生效。
- `src/octop/infra/agents/teams/decision.py:756`：planner 绕过 harness graph，不带工具、不读写 workspace。
- `src/octop/infra/agents/teams/task_state.py:9`：9 种内部任务状态；`review` 没有验收语义。
- `src/octop/infra/agents/teams/coordinator.py:87`：单进程编排假设。
- `src/octop/infra/agents/teams/team_manager.py`：约 2509 行，职责集中。
- `dashboard/src/pages/Teams/TeamBoardPage.tsx`：看板可看/拖状态/审批/暂停，但没有任务创建/编辑。

## 3. L3 目标架构

```
人类：目标 + 预算 + SLA + 质量/风险政策 + 异常收件箱
        │
        ▼
Decision Service（host planner，版本化 prompt，预算感知，无工具）
        │ plan
        ▼
Policy / Budget Gate（硬预算、权限、风险、审批）
        │ approved plan
        ▼
Dispatcher（依赖、并发、容量、优先级）
        │
        ▼
Member Executors（harness agents，隔离 workspace/工具）
        │ result + artifacts + evidence
        ▼
Verifier（确定性检查 + 独立评审人/Agent）
        │ accepted / rework
        ▼
Event Ledger / Settlement → 下一次决策
```

跨切面：全链路 trace、成本归集、共享产物/记忆、审计、人类异常收件箱。

## 4. 实施路径

### Phase A：L3 Gate（必须做）

#### A0. 看板投影 + 任务 CRUD（不动数据库）

前端 `dashboard/src/pages/Teams/TeamBoardPage.tsx`：

- 6 列投影为 4 列：待办 / 进行中 / 待处理 / 已结束。
- 内部状态不变；`review`、`needs_input` 用标签区分。
- 顶部收成一条：目标摘要 + runtime + “待你 N” + 暂停/更多。
- 新增/编辑任务弹窗：标题、说明、负责人、优先级、截止、验收标准。
- 详情抽屉增加验收和决策时间线入口。

API `src/octop/api/routers/teams.py`：

- `POST /teams/{id}/tasks`：创建任务。
- 扩展 `PATCH /teams/{id}/tasks/{task_id}`：title、spec、assignee、acceptance_criteria。
- `GET /teams/{id}/inbox`：异常收件箱。
- `GET /teams/{id}/metrics`：基础指标。
- `GET /teams/{id}/decisions`：返回 rationale/plan 摘要。

状态机 `task_state.py`：

- 保留现有内部状态，不做数据迁移。
- 增加 `review -> proposed` 转移，用于验收打回后重新派发。

验收：看板可见全部任务、可创建/编辑、可处理审批；`npx tsc -b` + vitest 通过。

#### A1. 预算/策略闸门

Schema（按 AGENTS.md 处理版本；024 未发布则并入 024，已发布则新建 025）：

- `team_runtime`
  - `budget_limit_tokens INTEGER NOT NULL DEFAULT 0`（0 = 不限）
  - `policy_json TEXT NOT NULL DEFAULT '{}'`
- `team_goals`
  - `budget_tokens INTEGER`
- `team_budget_entries`
  - `entry_key TEXT NOT NULL UNIQUE`
  - `team_id`, `decision_id`, `task_id`, `goal_id`
  - `kind`：reserve / settle / release
  - `tokens`, `created_at`
- `usage_log`
  - `task_id TEXT`, `goal_id TEXT`
  - 索引 `(team_id, task_id, ts)`

代码：

- 新增 `infra/agents/teams/budget.py`：`remaining`、`reserve`、`settle`、`release`。
- `DecisionRunner.run`：决策前检查日预算并预留；不足则 `paused_budget`。
- `Dispatcher.plan_dispatches`：接收真实 `budget_remaining`；任务预算不足时 `defer_budget`。
- `CronDeliveryCommand` / `record_turn_usage`：带 `task_id`、`goal_id`。
- `TeamSweeper`：孤儿预留对账、日切重置、超时释放。
- `TeamRuntime`：`paused_budget` 真正可写；resume 需要提高预算或新的一天。

API：

- `PATCH /teams/{id}` 支持 `budget_limit_tokens`。
- `GET /teams/{id}/runtime` 返回 `budget_limit/used/reserved/remaining`。
- `POST /teams/{id}/drive/resume` 可带预算提升说明。

验收：并发派发不超卖；预算耗尽后停止决策/派发；重启后预留/结算可恢复。

#### A2. 独立验收（Verifier）

Schema：

- `team_tasks`
  - `acceptance_criteria TEXT NOT NULL DEFAULT ''`
  - `review_required INTEGER NOT NULL DEFAULT 0`
  - `reviewer TEXT`
  - `review_status TEXT NOT NULL DEFAULT 'none'`
  - `rework_count INTEGER NOT NULL DEFAULT 0`
- `team_task_reviews`
  - `review_id`, `team_id`, `task_id`, `reviewer`
  - `status`, `criteria`, `notes`, `evidence_json`
  - `created_at`, `resolved_at`
- `team_waits.kind` 增加 `review`。

流程：

1. 成员回复 `done`。
2. 若 `review_required`，任务进入 `review`，创建 review wait。
3. 选择验收人：显式 reviewer → 团队内非 assignee 成员 → 人类创建者。
4. 先跑确定性检查（文件、格式、测试、引用），再由独立 reviewer 给结论。
5. `approved` → `done`；`rejected` → `review -> proposed`，`rework_count += 1`，带意见重新派发。
6. 超过最大返工次数 → `blocked` + judgment 事件，升级给人。

代码：

- 新增 `infra/agents/teams/verifier.py`。
- `coordinator.handle_member_reply` 接入验收分支。
- `dispatcher` 不派发 `review` 状态。
- `waits` 增加 review 结算/超时。
- API：`POST /teams/{id}/tasks/{task_id}/review`（人类验收）。
- UI：待处理列显示“待验收”，详情显示 criteria/evidence/返工次数。

验收：执行者不能自审；打回可重新派发；超过次数会升级；每个 `done` 有 review 记录或 waiver。

#### A3. 异常收件箱 + 指标

API：

- `GET /teams/{id}/inbox` 聚合：待审批、待验收、超 SLA 的 blocked/needs_input、failed、decision.failed、paused_budget、逾期任务。
- `GET /teams/{id}/metrics?window=7d`：目标完成率/周期、首次验收通过率/返工率、审批等待 p50/p95、派发成功率、决策合法率、预算使用/剩余、单目标 token 成本。

前端：

- 顶部常驻“待你 N”徽标，不默认隐藏。
- 新审批/验收到达时 toast + 浏览器标题计数。
- 抽屉按“审批 > 验收 > 阻塞 > 失败”排序，显示等待时长。
- 决策时间线：为什么建任务、为什么改派、为什么暂停。

验收：审批 p95 满足 SLA；异常项不长期无人处理；管理者不读日志能判断状态。

#### A4. L3 Gate 指标

| 指标 | 目标 |
|---|---|
| 预算遵守率 | 100% |
| 决策合法计划率 | ≥95% |
| 派发成功率 | ≥98% |
| 重启恢复正确率 | 100% |
| 首次验收通过率 | ≥90% |
| 返工率 | ≤10% |
| 目标无需人工改任务的比例 | ≥70% |
| 审批等待 p95 | 按业务 SLA |
| 严重事故 | 0 |

达不到，不进入 Phase B。

### Phase B：L3 Operate

#### B1. 技能/容量路由

- 团队 manifest 增加 `skills`、`capacity`、`cost_rate`。
- 新增 `AssignmentService`：确定性筛选（技能、容量、非验收人、可用）→ LLM 在候选内选择。
- 无候选 → `blocked: no_eligible_member` + judgment。
- Dispatcher busy key 从布尔改为计数，支持 capacity > 1。
- 决策 prompt 增加成员 profile；计划校验拒绝不合格 assignee。

#### B2. 决策协议 v2

- `team_decisions` 增加 `schema_version`。
- 新 prompt 只暴露 5～6 个高层 op：`set_goal/create_task/update_task/wait_approval/notify/noop`。
- 服务端翻译成现有内部 op。
- 幂等键用 `decision_id + 规范化 op 内容 hash`，不用下标。
- 保留 v1 解析器，历史 `plan_json` 可审计/重放。
- 建立 20～30 个真实案例的决策回归集。

#### B3. KPI/SLA + 产物/记忆

- 目标/任务可设 SLA；日报/周报。
- 成员结果结构化：`summary + artifacts + evidence + open_questions`。
- 新增 `team_task_artifacts` 元数据表；文件仍走 `BackendWorkspace`。
- 验收人可引用产物和证据；团队快照只带最近产物元数据。

#### B4. 策略执行

- `policy_json` 定义允许工具、需审批工具、单目标副作用上限。
- 复用 harness 工具权限/安全钩子，在工具执行前拦截。
- 高风险动作默认审批；审批记录进审计。

### Phase C：L3 Scale

- 决策/派发放入独立 worker 或全局信号量 + per-team 队列。
- `team_runtime` / `team_decisions` 增加 DB lease（owner、lease_until），去掉单进程假设。
- 增加背压：单团队并发决策、全局并发派发、慢任务隔离。
- 拆分 `team_manager.py`：`team_service` / `team_runtime` / `team_messaging` / `team_tools` / `team_verifier` / `team_budget`。
- 跨团队依赖、共享资源锁、多团队治理属于 L4 前置；L3 不要求。

## 5. 迁移与发布

1. Schema 版本  
   截至 2026-10-27，023/024 尚未提交/发布。按 AGENTS.md：  
   - 本轮一起发：L3 DDL 并入 024。  
   - 先发 024：再新建 `025_l3_controls.sql/.pg.sql`。  
   两种都要同步 SQLite/PG、迁移助手、schema 版本断言。

2. 向后兼容  
   - 不改现有任务状态值；只新增字段/表。  
   - 现有团队默认 `budget_limit_tokens=0`、`review_required=0`。  
   - 看板 4 列只是投影，不迁移数据。

3. 灰度  
   - 先 shadow：计算预算/验收但不阻断，观察 1～2 周。  
   - 再对 3～5 个试点团队开启预算硬闸门和验收。  
   - 最后全量；每步有 feature flag 和回滚开关。

4. 测试  
   - 预算并发/结算/重启。  
   - 验收自审拒绝、打回、返工上限。  
   - 收件箱 ACL/排序。  
   - 迁移 vN、Windows CI。  
   - `make all` + dashboard `npx tsc -b`。

## 6. 不要现在做

- 不合并内部任务状态（`assigned` / `needs_input` 是承重的）。
- 不重写调度内核为自由 Agent 群。
- 不先做统一专家看板。
- 不先做自动招聘/跨团队。
- 不让 planner 直接拿工具乱跑；保持“无工具规划 + 任务取证”。
- 不自动改 prompt/skill 做自学习；L4 再谈，且要人工审批。

## 7. 待定决策

- 专家是否需要看板，还是只共享 goal/task/deliverable 存储。
- 预算先用 token，还是同时引入货币成本。
- 验收默认独立 Agent，还是高风险任务强制人类。
- 024 先发布再开 025，还是把 L3 DDL 并入 024。
