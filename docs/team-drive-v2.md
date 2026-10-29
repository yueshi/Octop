# 团队驱动 v2：持久化协调者架构

> 状态：阶段 0–3 已实现（阶段 4 治理、阶段 5 清理未实施）。2026-10-25 重写，替代本文件早先的「确定性预检 + 自适应间隔 + 派发账本 + `sleep_until`」草稿。
> 前置阅读：`docs/team-autopilot-design.html`（v1 全量设计，P1–P4 已落地）、`docs/expert-teams.md`（团队基础机制）。
> 一句话：把驱动循环从「host 轮询」改成
> **事件驱动的持久化协调者 + 有预算的判断单元 + 等待账本 + 确定性执行器**。

---

## 0. 变化摘要

v1 的三个症状诊断成立，但旧 v2 草稿仍然把 host 当作循环的拥有者：每个团队一条 drive cron、每次唤醒跑完整 LLM、用提示词约束派工、用 cron `meta` 存水位、用一次性 `date:` cron 表达等待。这等于在轮询架构上打补丁。

本版做三个结构性替换：

| 旧形态 | 新形态 | 理由 |
|---|---|---|
| host 拥有驱动循环，cron 每 15 分钟唤醒完整 LLM | `TeamCoordinator` 持久状态机；cron 只做全局 sweeper | 协调者不应是内存线程，也不应每事件唤醒模型 |
| `dispatch_ledger` 按派发/动作记账 | `team_waits` 按等待/阻塞点记账 | 账本粒度应是「等谁、等到何时、超时怎么办」 |
| `sleep_until` 为每个等待创建一次性 cron | `team_waits(kind=timer)` + sweeper | 等待是一等状态，不是调度器的副作用 |

**host 的角色随之改变**：它不再是循环驱动者，而是协调者按需调用的「决策单元」。它只输出结构化计划，不直接执行成员派发、重试或传输。

---

## 1. 问题与根因

### 1.1 三个症状

| # | 症状 | 根因 |
|---|---|---|
| 1 | token 短时间大量消耗 | 每次 wake 都是完整 agent 回合；房间历史随同一线程增长；唤醒前无「是否需要判断」的路由；noop 只看有没有工具调用，不看是否真的改变了计划 |
| 2 | 协调者不断安排任务，任务并不迫切 | wake 模板把「推进」当默认义务；成员回叫后的 `followup_done` 又推动「再派下一位」；状态机没有「等待」表达；目标完成没有终态 |
| 3 | 没有外部时间/数据获取任务 | host 工具白名单只有 `agent_list / ask_agent / memory_* / current_time`（`src/octop/infra/agents/teams/service.py`），连接器/知识库/MCP 不挂；成员可带连接器，但模板缺委托引导。host 仍可通过 `ask_creator` 向创建人要数据，并非完全隔离，但缺少自主感知 |

### 1.2 旧 v2 为什么不够

旧草稿的预检、预算、账本、睡眠四个方向都对，但落点仍然在「host + cron」里：

- 预检只是决定「这次要不要唤醒」，唤醒后仍是完整 LLM 回合，没有「routine 执行」路径；
- 预算只拦 wake，成员派发和回叫收口不经过 wake，拦不住真正的成本；
- `dispatch_ledger` 按派发记录，无法回答「这个任务在等什么、何时超时、超时找谁」；
- `sleep_until` 创建一次性 cron，和「单驱动」、`stop` 清理、预检豁免互相冲突；
- 用 cron `meta` 存 `last_wake_ts/noop_streak`，但仓库更新路径没有 `meta` 参数，且更新会重排任务；
- KPI 依赖进程内 `METRICS` 和完整 `usage_log`，而团队成员回合目前没有写入 usage。

本版不再修补这些点，而是把驱动权从 host 移到协调者，把等待从 cron 移到账本。

---

## 2. 设计原则

以下八条是本文的硬约束，不再逐条论证。

1. **主动型 ≠ 任务驱动型。** 控制流由事件/目标状态驱动，考核判断力而非执行量。
2. **事件为主，定时兜底。** 定时只做延迟任务、sweeper 补偿、心跳重同步，不做轮询业务表的主驱动。
3. **触发必须分层。** 原始事件 → 聚合/过滤/去重 → 满足条件才升级为 Agent 任务，不能每个事件唤醒完整 LLM 循环。
4. **规划与执行解耦。** 模型输出结构化计划，执行由确定性引擎/工具适配器承担。
5. **等待是一等状态。** 持久化挂起、零算力占用、带超时升级路径，不是进程内 sleep 或轮询。
6. **集中协调 + 稀疏账本。** 协调者是持久化状态机（非内存线程）；账本以「等待/阻塞点」为粒度，不以「动作」为粒度。
7. **有界自主，治理内嵌。** 独立身份、最小权限、控制闸门、审计锚点、预算熔断是架构一等公民。
8. **时间弹性换成本弹性。** 用 deadline 而非 timeout；按余量路由同步/Flex/Batch；预算不足时降级或挂起。

### 2.1 在 Octop 中的翻译

- **「协调者是持久化状态机」** 不等于新增独立守护进程。Octop 可以继续用进程内 asyncio worker，但权威状态必须在 DB：`team_runtime / team_events / team_decisions / team_waits`。任何 worker 重启后都能从 DB 恢复。这样既满足「非内存线程」，又不引入外部队列。
- **「账本以等待/阻塞点为粒度」** 意味着一次成员派发是一行 `team_waits(kind=member_reply)`，一次审批是 `kind=approval`，一次 `sleep_until` 是 `kind=timer`。不记录每次工具调用。
- **「时间弹性」** 在当前 provider 抽象下没有 Flex/Batch 落点。可落地的是：deadline 余量决定「延后/立即/升级」，预算不足时挂起或降级模型。不要伪造不存在的 Batch 通道。

---

## 3. 目标架构

### 3.1 角色

| 角色 | 职责 | 是否调用 LLM |
|---|---|---|
| `TeamCoordinator` | 事件摄取、去重/聚合、routine/judgment 分诊、创建决策、派发等待、超时升级、汇总；维护 `team_runtime` | 否 |
| Decision Unit（host） | 读结构化快照，输出 `DecisionPlan`（目标/任务/指派/等待/升级）；可输出「不动」 | 是，有预算、幂等 |
| `TeamDispatcher` | 依赖、并发、预算、时间窗、deadline、幂等、重试；调用执行适配器；创建/结算 wait | 否 |
| Execution Adapter | harness `submit_peer` / 成员运行 / 房间流式 / HITL | 否（成员运行时另算） |
| Wait Ledger + Sweeper | `team_waits` 持久化；零算力；到期/超时事件；重试、心跳 | 否 |
| Audit / Budget | 追加事件、预算预留/消费、闸门、KPI | 否 |

```
用户消息 / 成员回叫 / 审批结果 / 看板写入 / 定时到期
        │ append(event, dedupe_key)
        ▼
   team_events（持久、追加、可回放）
        │
        ▼
 TeamCoordinator（持久状态机，规则驱动）
   · 聚合、去重、过滤 self-event
   · routine?  → 直接交给 dispatcher
   · judgment? → 创建 team_decisions（每团队同时最多一个）
   · 预算/并发/截止闸门
        │
        ├──────────────► TeamDispatcher（确定性）
        │                  依赖/并发/预算/时间窗/截止/幂等
        │                  创建/结算 team_waits
        │                  调用执行适配器
        │
        ▼
 team_decisions（pending/running/committed/failed）
        │ 结构化快照
        ▼
 Decision Unit（host LLM，plan-only）
        │ DecisionPlan
        ▼
 TeamCoordinator.apply(plan) —— 事务性、幂等、CAS
        │
        ▼
 TeamDispatcher.dispatch(task) —— 无 LLM
        │
        ▼
 成员执行 / 审批 / 外部动作
        │ reply / approval event
        └──────────────► team_events

team_waits：member_reply / approval / timer / budget
全局 team_sweeper（cron）：due wait、retry、heartbeat
```

### 3.2 四类任务单元

| 单元 | Octop 实现 | 调用 LLM | 幂等/预算 |
|---|---|---|---|
| 触发单元 | `team_events` + `TeamCoordinator.ingest` | 否 | `dedupe_key`；无预算 |
| 决策单元 | `team_decisions` + `TeamDecisionService` | 是 | `decision_id`；预留预算；空计划合法 |
| 执行单元 | `TeamDispatcher` + harness `submit_peer`/`stream` | 否 | `idempotency_key`；重试上限；副作用走审批 |
| 等待单元 | `team_waits` + sweeper | 否 | 零算力；timeout → escalation |

拆分边界是**等待点和审批点**，不是工具调用。一个任务经历「派发 → 成员回复 → 审批 → 再派发」会有两到三行 wait，但不会有几十行工具日志。

### 3.3 状态机

**Runtime**（`team_runtime.status`）：

```
idle → running → deciding → dispatching → waiting → running
                  │
                  ├→ paused_user
                  ├→ paused_budget
                  ├→ finishing → idle
                  └→ stopped
```

`paused_*` 不删除任务和账本；`finishing` 表示目标完成、不再接新工作；`stopped` 是 kill switch，取消 open 任务并清空 inbox。

**Decision**（`team_decisions.status`）：

```
pending → running → committed
                 ├→ failed（可重试，复用 decision_id）
                 └→ abandoned（预算/超时/取消）
```

**Wait**（`team_waits.status`）：

```
waiting → settled
       ├→ timed_out → escalated
       ├→ cancelled
       └→ deferred（runtime 暂停时保留）
```

**Task**：沿用现有 `proposed/assigned/running/blocked/needs_input/review/done/failed/cancelled`。`ready` 由「proposed + 依赖终态 + 时间窗开放」派生，不新增状态；阻塞中的任务通过 `active_wait_id` 指向 wait 行。允许的迁移仍由 `task_state.py` 统一校验，新增等待相关边（如 `proposed → blocked`）需在此集中声明。

**并发约束**：一个团队同一时刻最多一个 `running` decision；一个 `concurrency_key` 同一时刻最多一个活跃执行。两者都用 DB CAS，不依赖内存锁。

---

## 4. 数据模型

> 迁移规则：若 022 仍未发布，折叠进 `022_light_apps`；若已发布，新开下一个编号。无论哪种，都必须同时更新 `.sql`/`.pg.sql`、扩展 `src/octop/infra/db/migrate.py` 的 `_ensure_team_board_schema`（供已记录 v22 的库补表/补列），并更新 `tests/unit/db/test_db_pool.py` 的版本断言。

### 4.1 `team_runtime`

每个团队一行，协调者的权威状态。

| 列 | 说明 |
|---|---|
| `team_id` PK | `REFERENCES agents(agent_id) ON DELETE CASCADE` |
| `status` | `idle/running/deciding/dispatching/waiting/paused_user/paused_budget/finishing/stopped` |
| `mode` | `autopilot/manual`（manual 只响应显式用户指令） |
| `goal_id` | 当前 active goal |
| `active_decision_id` | 当前 decision；CAS 互斥 |
| `last_event_seq` | 已消费事件水位 |
| `last_decision_at` | 上次决策时间 |
| `next_heartbeat_at` | 下次心跳重同步 |
| `heartbeat_interval_sec` | 默认 1800；沿用原 drive interval 的下限作为防滥用默认 |
| `budget_day` | 服务器时区的日期 `YYYY-MM-DD` |
| `budget_used` / `budget_reserved` | token（或成本） |
| `paused_reason` | 用户暂停/预算暂停/迁移 |
| `version` | 乐观锁 |
| `updated_at` | |

### 4.2 `team_events`

追加事件账本，也是审计锚点。

| 列 | 说明 |
|---|---|
| `event_id` PK | ULID |
| `team_id` | 索引 |
| `seq` | 每团队递增（DB 事务内分配） |
| `type` | `user.message / member.replied / approval.resolved / task.changed / goal.changed / wait.due / wait.timeout / external.event / budget.threshold / runtime.boot / sweeper.heartbeat` |
| `actor` | `user / coordinator / host:<decision_id> / member:<agent_id> / external:<source_id> / sweeper` |
| `payload_json` | 事件体；不放全量房间文本 |
| `dedupe_key` UNIQUE | 见 §5.2 |
| `created_at` | |
| `consumed_at` / `decision_id` | 被哪个 decision 消费 |

保留期：默认 30 天；`DONE/CANCELLED` 事件可归档，审计所需字段保留在 `team_decisions`。

### 4.3 `team_decisions`

| 列 | 说明 |
|---|---|
| `decision_id` PK | ULID |
| `team_id` | |
| `trigger_event_ids_json` | 触发事件 |
| `mode` | `autonomous/interactive` |
| `status` | `pending/running/committed/failed/abandoned` |
| `reason` | 分诊原因（`member_failed / deadline_risk / goal_complete / user_instruction / budget ...`） |
| `snapshot_hash` | 快照摘要 |
| `plan_json` | 已提交计划（审计/重放） |
| `budget_reserved` / `cost_tokens` | |
| `thread_id` | 专用决策线程 |
| `started_at` / `finished_at` / `error` | |

### 4.4 `team_waits`

等待账本，替代 `dispatch_ledger` + `sleep_until` cron + 部分 HITL 关联。

| 列 | 说明 |
|---|---|
| `wait_id` PK | ULID |
| `team_id` / `task_id` | 索引；`task_id` 可空（团队级等待） |
| `kind` | `member_reply / approval / timer / budget` |
| `status` | `waiting/settled/timed_out/escalated/cancelled/deferred` |
| `due_at` | timer 到期；member/approval 可为空 |
| `timeout_at` | 操作超时（重试/升级），区别于业务 deadline |
| `deadline_at` | 业务截止快照，便于调度 |
| `escalation_policy_json` | 超时后：通知创建人 / 升级 decision / 失败 |
| `idempotency_key` UNIQUE | 例如 `dispatch:{task_id}:{attempt}`、`approval:{hitl_id}`、`timer:{task_id}:{until}` |
| `external_ref` | inbox `job_id`、`hitl_pending.id`、task id |
| `payload_json` | 重投所需：message、source_thread_id、session_key、mode |
| `attempts` / `last_attempt_at` | 重试 |
| `created_at` / `settled_at` | |

索引：`(team_id, status, due_at)`、`(team_id, status, timeout_at)`、`(task_id, status)`。

### 4.5 `team_task_deps`

```
task_id TEXT NOT NULL REFERENCES team_tasks(task_id) ON DELETE CASCADE
depends_on_task_id TEXT NOT NULL REFERENCES team_tasks(task_id) ON DELETE CASCADE
PRIMARY KEY (task_id, depends_on_task_id)
```

写入时校验同团队、非自依赖、无环。查询「无未完成依赖」走 join，不依赖 SQLite/PG 的 JSON 函数。

### 4.6 `team_tasks` 新增列

| 列 | 说明 |
|---|---|
| `window_start` / `window_end` | 允许执行窗口；空表示全天 |
| `budget_tokens` | 任务级预算；空继承团队 |
| `concurrency_key` | 默认 assignee；可设业务对象 |
| `escalation_policy_json` | 超时/失败升级策略 |
| `idempotency_key` | 任务创建去重（decision 重试）；部分唯一索引 |
| `active_wait_id` | 当前阻塞 wait；可空 |

业务截止沿用现有 `due_at` 列（不另设 `deadline_at`，避免同义两列）——文档与提示词层面把 `due_at` 的语义收敛为「业务截止」即可。

`task_update` 需要支持显式清除 `due_at/blocked_reason`（已实现：`clear_fields` 参数，见 `board_tools.py` 的 `CLEARABLE_TASK_FIELDS`），避免「不能清空截止时间」导致调度反复命中。

### 4.7 `usage_log.team_id`

`usage_log` 目前只有 `agent_id/user_id/thread_id/model/tokens/source`。新增可空 `team_id` + 索引：

- 团队成员回合：`agent_id=member`、`team_id=host`；
- 主持人收口/决策回合：`agent_id=host`、`team_id=host`；
- 普通聊天：`team_id=NULL`。

这是预算和 KPI 的分母。**在成员回合写入 usage 之前，预算功能不得上线**：当前已实现（`stream_peer_to_room` 内挂 `UsageTracker`，成员回合 `source="team_member"`、host 房间流回合 `source="team_host"`、cron 唤醒沿用 `source="cron"`），但 inbox wrap-up 直接走 harness `stream_followup` 的路径尚未计量——该路径将在阶段 3 被决策服务替换，届时补齐。

---

## 5. 事件与触发

### 5.1 事件来源

| 来源 | 事件 | 当前落点 |
|---|---|---|
| 成员回叫 | `member.replied` | `TeamManager.on_reply` |
| 用户消息 | `user.message` | 团队房间消息处理 |
| 看板工具/REST | `task.changed` / `goal.changed` | `board_tools.py` / `api/routers/teams.py` |
| HITL 解决 | `approval.resolved` | `hitl` coordinator |
| 外部事件 | `external.event` | `infra/events/worker.py`（webhook → `TeamCoordinator.record`） |
| 定时/超时 | `wait.due` / `wait.timeout` | `team_sweeper` |
| 预算 | `budget.threshold` | usage 聚合 |
| 启动/心跳 | `runtime.boot` / `sweeper.heartbeat` | 协调者/sweeper |

所有事件先写 `team_events`，再触发内存 notify；进程崩溃时 sweeper 从表中恢复。

### 5.2 去重与聚合

- `dedupe_key`：`inbox:{inbox_id}`、`task:{task_id}:v{version}`、`goal:{goal_id}:{updated_at}`、`hitl:{pending_id}:{status}`、`wait:{wait_id}:{due|timeout}`、`hb:{team_id}:{bucket}`、`boot:{team_id}:{ts}`。用户消息在入口处没有稳定 id，阶段 1 用 `usermsg:{session_key}:{time_ns}` 占位（不承诺幂等）；阶段 3 接入消息 id 后换成 `msg:{message_id}`。
- 普通事件在 3–5 秒窗口内按团队聚合；同一团队最多一个 `pending/running` decision，后续事件合并进下一次。
- 紧急事件（审批被拒、deadline 临近、预算硬阈值）可跳过聚合窗口，但仍受「每团队一个 decision」约束。
- **self-event 抑制**：决策单元在本轮写看板产生的事件，`actor=host:<decision_id>`，只记录/消费，不触发下一次 decision。否则会回到「每写一次看板醒一次」的自激循环。
- 背压：decision 运行期间事件只入队；超过队列上限时只保留 judgment 类事件，routine 事件合并或丢弃并记录。

### 5.3 routine / judgment 分诊

| 事件 | routine（dispatcher 直接处理） | judgment（创建 decision） |
|---|---|---|
| 成员完成，任务无需复核 | 标 done，派下一个 ready 任务 | 结果矛盾、需要取舍、任务要求复核 |
| 成员失败，瞬时错误 | 按策略重试 | 权限/策略错误、连续失败 |
| 依赖完成 | 依赖满足的任务进入 ready | 依赖失败/取消 |
| 审批通过 | 恢复等待中的执行 | 审批被拒、超时 |
| 定时到期 | 已有计划的下一步继续派发 | 没计划、截止风险 |
| 预算软阈值 | 延后低优先级任务 | 需要重新排优先级 |
| 用户消息 | 正常聊天回合 | 目标/约束改变，看板失效 |
| 外部事件 | `team.notify` 只通知 | `team.judge` 写入 `external.event`，总是创建 decision |
| 决策单元自己写看板 | 记录、消费 | 外部写入导致计划失效 |

这张表就是「协调者只做分诊」的具体含义：**分诊是规则，判断才叫模型**。

### 5.4 全局 sweeper

`CronManager.schedule_system_job("team_sweeper", trigger="interval:60", ...)` 注册一个进程级系统任务（`src/octop/infra/cron/manager.py`）。它只做：

- 扫描 `team_waits`：`due_at <= now` 的 timer、`timeout_at <= now` 的等待，发出 `wait.due/timeout`；
- 扫描 `team_decisions`：`pending` 超时、`running` 心跳丢失，重试或标 failed；
- 扫描 `team_runtime`：`next_heartbeat_at <= now` 的 running 团队，发心跳事件；
- 扫描重试：`next_attempt_at <= now` 的任务/等待；
- 预算日切换：重置 `budget_day/used/reserved`，恢复 `paused_budget`；
- 事件/等待清理。

它**不扫描 `team_tasks` 来决定业务工作**；ready 集由 dispatcher 在计划提交/等待结算后按需计算。心跳事件也不必然产生 LLM：coordinator 只有在「有 open 任务、无活跃 wait、无活跃 decision、且超过阈值/临近 deadline」时才创建 decision。

---

## 6. 决策单元

### 6.1 结构化快照

决策单元不读全量房间历史。快照由 coordinator 构建并限长：

```
{
  "runtime": {"status", "goal_id", "budget_remaining", "heartbeat_at"},
  "goal": {"goal_id", "title", "description", "success_criteria", "status"},
  "tasks": [top 30 open, 按 priority/deadline 排序：
            {"task_id", "title", "spec_excerpt", "status", "assignee",
             "priority", "deadline_at", "deps", "active_wait", "last_result_excerpt", "version"}],
  "waits": [active/due/overdue：{"wait_id", "kind", "task_id", "due_at", "timeout_at"}],
  "events": [最近 20 条 judgment 事件摘要，不含全量正文],
  "metrics": {"open_count", "done_today", "failed_today", "cost_today"},
  "policy": {"max_new_tasks", "max_dispatches", "review_rules"}
}
```

上限建议：任务 30 条、事件 20 条、每条 excerpt 500–800 字；快照总量控制在决策预算的 30% 以内。需要更多上下文时，host 只能通过受限的 `room_digest`/`task_get` 读取，不允许把整段房间历史塞回上下文。

### 6.2 DecisionPlan

```json
{
  "goal_ops": [
    {"op": "set|update", "goal_id": "...", "status": "active|paused|done|archived",
     "title": "...", "success_criteria": "...", "idempotency_key": "..."}
  ],
  "task_ops": [
    {"op": "create", "task_id": "local-key", "title": "...", "spec": "...",
     "assignee": "...", "priority": 0, "deadline_at": 0,
     "depends_on": ["task-id"], "budget_tokens": 0, "idempotency_key": "..."},
    {"op": "update", "task_id": "...", "expected_version": 3, "fields": {...}},
    {"op": "assign", "task_id": "...", "assignee": "...", "concurrency_key": "..."},
    {"op": "close", "task_id": "...", "result_summary": "..."},
    {"op": "cancel", "task_id": "...", "reason": "..."}
  ],
  "wait_ops": [
    {"op": "sleep", "task_id": "...", "until": 0},
    {"op": "wait_approval", "task_id": "...", "reason": "..."},
    {"op": "escalate", "task_id": "...", "to": "creator|host", "reason": "..."}
  ],
  "notify": {"user": true, "text": "..."},
  "rationale": "...",
  "noop": false
}
```

约束：

- **没有 `dispatch` op**。`assign` 只写任务状态，真正的派发由 dispatcher 执行。这是原则 4 的硬边界。
- 每个 op 带 `idempotency_key`；`create` 用 `(decision_id, local-key)` 去重，`update` 用 `expected_version` CAS。
- 计划应用是事务性的；非法迁移/依赖环/跨团队 assignee 直接拒绝，decision 标 failed，事件保留给下一次决策。
- `noop=true` 是合法结果，记录 `rationale` 与成本，然后进入冷却；不要为了「显得有进展」造任务。

### 6.3 工具 profile

| 模式 | 工具 |
|---|---|
| 决策模式（自动） | 只读：`task_list/goal_get/memory_*/current_time/room_digest(受限)`；计划：`goal_set/task_create/task_update/task_assign/task_close/wait_set/escalate` |
| 聊天模式（用户） | 保留现有 host 工具，但 `ask_agent` 逐步替换为 `task_assign`；任何产生承诺的动作走 task/wait |

`HOST_TOOLS_ALLOWED` 现在是静态白名单；实现时改为按模式下发。决策模式**不挂 `ask_agent`**，避免模型绕过 dispatcher 直接执行。

### 6.4 幂等与重试

- `decision_id` 标识一次判断；LLM 重试复用同一 `decision_id`，但已提交的 plan 不重复应用。
- 崩溃在 `running`：sweeper 标 `failed`，由下一次 decision 重新判断；不要自动重放旧 plan（事件可能已变化）。
- 计划应用失败：保留 `plan_json` 和错误，下一次 decision 读快照时能看到失败原因。
- 决策线程：建议使用独立 `team-decision-{team_id}` 线程，只保存决策摘要与计划，不保存房间全文；房间仍是人类时间线。这样既控制上下文成本，又保留决策连续性。

### 6.5 决策预算

- 决策开始前预留 `budget_reserved`；结束后按 `usage_log` 实际扣减，释放余额。
- 决策预算是团队日预算的子集；决策内派发数量有硬上限（如 3–5），避免一轮决策派空整个看板。
- 决策超预算/超时：标 `abandoned`，不执行未提交计划。

### 6.6 提示词原则

提示词只负责「怎么判断」，不负责「必须推进」：

- 先判断「现在是否真的需要行动」；不需要就输出 `noop` 并说明理由；
- 任务是对目标的承诺，不是产出指标；不要为了填满看板而创建任务；
- 目标完成/无可行推进/等待外部时，选择 `goal done`、`sleep`、`wait_approval` 或 `escalate`，不要调用 kill switch；
- 需要外部数据时，派给有相应能力的成员，不要凭记忆编造；
- 只输出计划，不直接执行；不要声称「已派给某人」——派发由 dispatcher 完成并在房间确认。

`teams.followup_done` 必须同步改写：成员完成后不再默认「再派下一位」，而是「判断是否需要继续；不需要则收口」。

---

## 7. 执行器与等待

### 7.1 Dispatcher 算法

```
for task in ready_tasks(team_id) order by priority desc, deadline asc:
    if deps not terminal: continue
    if concurrency_key busy: continue
    if now < window_start: create_wait(timer, window_start); continue
    if now > deadline_at: escalate(task, "deadline_missed"); continue
    if budget.remaining < task.estimate: defer_or_escalate(task); continue
    wait = create_wait(member_reply, task,
                       timeout_at=policy, deadline_at=task.deadline_at,
                       idempotency_key=f"dispatch:{task_id}:{attempt}")
    task.status = assigned; task.active_wait_id = wait.wait_id
    result = adapter.submit_peer(task, message=task.spec, wait=wait)
    if result.ok: task.status = running; wait.external_ref = result.job_id
    else: settle(wait, failed) / schedule_retry(task)
```

- 不持有跨成员运行的 asyncio 锁；派发后立即返回。
- `submit_peer` 是现有 `wire_host_dispatch` 的执行缝；dispatcher 复用它，不新造传输层。
- ready 集按需计算，不做全表轮询。

### 7.2 Wait 生命周期

| kind | 创建 | 结算 | 超时 |
|---|---|---|---|
| `member_reply` | dispatcher 派发前 | `member.replied`（按 `inbox_id` 幂等） | `timeout_at` → 重试或升级 |
| `approval` | 成员运行命中 HITL | `approval.resolved` | 过期 → 取消/升级 |
| `timer` | `sleep_until`、窗口外、重试退避 | `due_at` 到 → `wait.due` | 无（本身就是定时） |
| `budget` | 预算耗尽暂停 | 日切换/预算调整 | 可配置提醒 |

- 等待期间零 LLM、零成员运行；只有 sweeper 的 SQL/事件。
- `settle` 必须幂等：同一 `dedupe_key` 的重复事件只结算一次；重复 `inbox_id` 不重复更新任务。
- `timeout` 与 `deadline` 分开：`timeout` 触发重试/升级；`deadline` 是业务失败/上报，超过 deadline 不再重试。
- `team_waits` 是账本，`hitl_pending` 仍是审批的运行时/UI 存储；用 `external_ref` 关联，不复制状态。

### 7.3 成员回叫

`on_reply` 的顺序改为：

1. 写 `team_events(member.replied)`（按 `inbox_id` 去重）；
2. 结算 `member_reply` wait；
3. 更新 task（done/failed/review）；
4. 按 routine/judgment 路由：routine 直接让 dispatcher 继续；judgment 创建 decision；
5. 房间展示作为副作用，不再默认触发 host 收口回合。

这替代了当前「每个回叫都走 `compose_followup` 唤醒 host」的路径，是症状 2 和成本问题的关键改动。

### 7.4 审批

- 成员运行命中 HITL：创建 `approval` wait，`external_ref=hitl_id`；成员图挂起，零算力。
- 审批通过：结算 wait，恢复执行；不产生 decision（routine）。
- 审批拒绝/过期：结算 failed，按策略升级为 decision 或直接失败。
- 写外部系统的任务必须走审批或标记 `retry_safe`；重投不能假装幂等。

### 7.5 重试与副作用

- 仅瞬时错误自动重试；权限/策略/参数错误不重试，升级。
- `attempts` 上限 + `next_attempt_at` 退避；sweeper 触发。
- `idempotency_key` 防账本重复，不防外部副作用；副作用任务需要审批或连接器幂等键。
- 重投复用 `payload_json`，保证「同一条派发」，不是「同任务的新派发」。

### 7.6 并发、依赖、时间窗、deadline

- **并发**：`concurrency_key` 默认 assignee；同一 key 同时一个 running。成员忙闲以 DB wait 为准，`TeamJobTracker` 只做缓存。
- **依赖**：join 表 + 环检测；依赖终态后 dispatcher 重新计算 ready；依赖失败/取消 → dependent 转 `needs_input` 并升级。
- **时间窗**：窗口外创建 timer wait，到窗口开始再派发；不占用成员。
- **deadline**：派发前检查；临近按策略升级；超过 deadline 不再派发/重试。

### 7.7 暂停、完成、停止

| 动作 | 行为 |
|---|---|
| `pause` | `status=paused_user`；不创建新 decision/派发；保留 wait/task；timer 到期标 `deferred` |
| `resume` | 恢复 `running`；sweeper 重新激活 due wait；可选触发一次决策 |
| `finish` | 目标标 done；不再接新工作；等待自然结算或按策略取消；不取消已完成/在审任务 |
| `stop` | kill switch：取消 open 任务、取消 waits、清空 inbox、`status=stopped`；必须用户显式触发 |
| `budget_pause` | 硬阈值自动进入；只停新决策/派发，不取消在途；日切换自动恢复或通知 |

`drive_disable` 不再作为「目标完成」的终态；它只对应 `stop`。

---

## 8. 预算、治理与审计

### 8.1 预算

- 维度：团队日预算为主，任务/决策预算可选继承。
- 单位：MVP 用 token；有模型定价后再加成本预算。文档需注明 token 预算对多模型团队不公平。
- 预留：decision 开始、dispatch 前预留；实际按 `usage_log` 扣减；结束释放。
- 软阈值（如 80%）：延后低优先级任务、跳过非紧急决策、通知一次。
- 硬阈值（100%）：`paused_budget`；拒绝新决策/派发；在途允许完成；通知创建人（每日一次）。
- 硬阈值 ×2 不自动 `stop`；只暂停并升级，避免丢工作。
- 日界：`OctopConfig.default_timezone`；与 cron/展示一致。
- 通知：新 frame `team.budget`，不要复用 `approval_required`（语义是待审批）；文案进 `en.json/zh.json`。

### 8.2 治理

- **身份**：actor 至少区分 `user / coordinator / host:<decision_id> / member:<agent_id> / sweeper`；成员使用自己的 agent 配置/连接器凭证，host 不借用成员权限。
- **最小权限**：决策模式 plan-only；dispatcher 只执行已提交任务；成员工具按任务范围；host 不直接挂连接器/MCP（沿用白名单）。
- **控制闸门**：pause/stop、预算、审批、最大 open 任务、每决策最大派发、每小时最大决策、最大重试。
- **审计锚点**：每次状态迁移写 `team_events`（actor、decision_id、wait_id、task_id、task_version、reason）；用户动作继续写 `audit_log`。`team_events` 只追加，不更新业务状态。

### 8.3 KPI

| 指标 | 来源 | 用途 |
|---|---|---|
| 无操作决策率 | `team_decisions.noop` / 总数 | 判断力/克制 |
| 计划变更率 | `plan_json` 与上一版 diff | 决策是否真的改变计划 |
| 单位决策成本 | `usage_log.team_id` 日聚合 / decision 数 | 成本效益 |
| 单位完成任务成本 | 日 token / `done` 转移数 | 趋势 |
| 等待时延 | `team_waits.created_at → settled_at` | 成员/审批效率 |
| 超时/升级率 | `timed_out/escalated` / waits | 可靠性 |
| 按期率 | `done` 且 `settled_at <= deadline_at` | 时间弹性 |
| 返工率 | 重开/回退任务 / done | 判断质量 |

`METRICS` 只保留实时运维计数；KPI 一律从表查询，不依赖进程内计数。

---

## 9. 时间语义

- `deadline_at`：业务截止，硬约束；错过即失败/升级。
- `timeout_at`：操作超时，触发重试/升级；可以早于 deadline。
- `window_start/window_end`：允许执行的时间窗；窗口外等待，不算失败。
- `due_at`：`team_tasks` 的业务截止列（实现保留原名，不设 `deadline_at` 同义列）；`team_waits.due_at` 则是 timer 的到期时间。
- 所有时间存 UTC epoch；用户输入按服务器 `default_timezone` 解释；展示走 `useServerTimezone`/`formatServerDateTime`。
- **deadline 余量策略**：`slack = deadline - now - estimate`；余量大 → 延后/后台；余量小 → 立即/高优先级；余量 ≤0 → 升级/失败。预算不足 → 挂起非紧急任务。
- **Flex/Batch**：当前 provider 抽象不支持，不在本版实现。可落地的是 defer/suspend/模型降级；未来 provider 层支持后再接路由。

---

## 10. API、Dashboard、CLI

### 10.1 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/teams/{id}/runtime` | 状态、目标、预算、活跃 decision/wait |
| POST | `/api/teams/{id}/drive/start` | 启动/恢复 autopilot |
| POST | `/api/teams/{id}/drive/pause` | 用户暂停 |
| POST | `/api/teams/{id}/drive/resume` | 恢复 |
| POST | `/api/teams/{id}/drive/finish` | 目标完成，停止接新工作 |
| POST | `/api/teams/{id}/drive/stop` | 保留兼容；kill switch |
| GET | `/api/teams/{id}/decisions` | 决策审计（分页） |
| GET | `/api/teams/{id}/waits` | 等待账本（分页） |

### 10.2 Dashboard

- 团队卡片显示 runtime 状态：running/paused/waiting/budget paused/finishing/stopped。
- 暂停/恢复/完成/停止按钮；预算横幅；到期/等待徽标。
- 看板任务显示 `deadline_at`、依赖、`active_wait`；`blocked/needs_input` 显示原因。
- 不再把 drive 任务展示为普通 cron；迁移后 cron 页面不出现团队驱动。
- WS frame：`team.runtime.updated`、`team.decision.updated`、`team.wait.updated`、`team.task.updated`、`team.budget`；前端补对应 hook/i18n。

### 10.3 CLI

如现有团队命令需要暴露 runtime，优先只读；启停走 API。无新增外部依赖。

---

## 11. 迁移与落地

### 11.1 从 v1 迁移

1. **drive cron → runtime**：扫描 `cron_jobs.meta.kind='team_drive'`。
   - enabled → `team_runtime.status=running`；
   - interval → `heartbeat_interval_sec`（保留 floor 作为防滥用默认，不再建 cron）；
   - 未来 `date:` → `team_waits(kind=timer)`；
   - 迁移后删除 drive cron 行，写审计。
2. **在途任务**：`running/assigned` 且无可用 inbox 的，标 `needs_input/blocked`，原因写「migrated: in-flight dispatch state unknown」，由首次 decision 处理；不要静默判 failed。
3. **HITL**：现有 `hitl_pending` 保留；为 pending 行创建/关联 `approval` wait，`external_ref=hitl_id`。
4. **usage**：加 `team_id`；对 `agents.kind='team'` 的历史行回填 `team_id=agent_id`；成员历史无法回填的置空，从迁移后开始计量。
5. **旧代码**：保留一个 release 的 feature flag/双写；迁移完成后删除 `meta.kind=team_drive` 分支、`_prepare_team_wake`、`dispatch_ledger`/`sleep_until` 计划。

### 11.2 阶段

| 阶段 | 内容 | 验证 |
|---|---|---|
| 0 | 计量 + schema：`usage_log.team_id`、成员/收口 usage、5 张新表、`team_tasks` 列、迁移 helper | 单测；v22 库补表；PG/SQLite 一致 |
| 1 | 事件影子：`on_reply`/board/HITL/用户消息写 `team_events`；coordinator 只观察/分类，不替换旧循环 | 事件不丢、dedupe、无自激、routine 占比 |
| 2 | 决策/派发影子：快照 + plan dry-run；dispatcher 只计算不执行 | 计划合法、幂等、预算预留、顺序正确 |
| 3 | 切换：runtime 权威；全局 sweeper；迁移 drive cron；wait 结算 | 端到端：事件→决策→派发→回叫→结算；崩溃恢复 |
| 4 | 治理：预算硬/软、pause/finish/stop、deadline/deps、审计、KPI | 预算/暂停/超时/依赖测试；KPI 可查询 |
| 5 | 清理：删除旧 drive 路径与双写 | 回归 + 旧数据迁移验证 |

阶段落地状态（随实现更新）：

- **阶段 0 已完成**：schema（5 表 + `team_tasks` 7 列 + `usage_log.team_id` 回填）、repos、成员/host/cron 三路计量、`clear_fields`。
- **阶段 1 已完成**：`infra/agents/teams/coordinator.py` 的 `TeamCoordinator`（record → ensure runtime → append → 规则分诊计数，只观察不接管）。埋点：`TeamManager.on_reply`（`member.replied`，非团队 peer 房间被 `is_team_agent` 守卫排除）、`board_tools.py` 写路径 + `api/routers/teams.py`（`task.changed`/`goal.changed`）、`HitlPendingStore.mark_resolved`（`approval.resolved`，仅带 `task_id` 的团队暂停）、`GlobalProcessor.iter_turn_chunks`/`__call__`（`user.message`，dashboard/CLI/MCP 与 IM 两个入口）、`OctopServer._boot_runtime`（`runtime.boot`）。分诊结果只进 `METRICS`（`team_event_routine_total`/`team_event_judgment_total`）。
- **阶段 2 已完成**：`decision.py`（有界快照、`DecisionPlan` schema、双次重试解析、校验、provider-only 计划调用、预算预留/用量归因、cooldown）与 `dispatcher.py` 的只读投影；`TeamCoordinator` 在 `team_shadow_decisions` 开启时后台记录 shadow decision，不应用、不派发。
- **阶段 3 已完成**：`plan_apply.py` 事务性应用计划（goal/task/dep/wait，host self-event 抑制）；`TeamDispatcher` 真实执行（`member_reply` wait、CAS assigned→running、并发/依赖/窗口/deadline 闸门、失败升级）；`TeamCoordinator` 权威路由（autopilot 下 routine 直接派发、judgment 决策后派发、自事件只消费）；`sweeper.py` 全局 `team_sweeper`（due/timeout/heartbeat/stale decision/事件回放/预算日切换）；`runtime.py` 的 start/pause/resume/finish/stop、房间解析、inbox 提交与 legacy drive cron 迁移；成员回叫结算 wait 并关闭任务，autopilot 房间跳过 host 收口回合；失败决策写 `decision.failed` 并在 cooldown 后重试，快照带最近决策错误；启动时回收随进程丢失的 `member_reply` wait（任务标 blocked，交给下一次决策）；决策级 `wait_approval` 可通过 `POST /api/teams/{id}/waits/{wait_id}/resolve` 批准/拒绝（看板审批条），成员 HITL 暂停仍在会话卡片处理；dashboard runtime/decision/wait/task/goal 帧与看板状态条。**预算硬/软阈值、KPI、旧 drive 代码删除留待阶段 4/5。**

### 11.3 测试

- 单元：事件去重/聚合/self-event 抑制；routine/judgment 路由；plan 校验；dispatch 依赖/并发/预算/窗口/deadline；wait settle/timeout；预算日切换；pause/stop。
- 集成：成员完成不触发 host 回合；失败触发 decision；审批恢复；重复 `inbox_id` 不重复结算；进程重启后 waits 恢复；stop 不复活。
- 成本：routine 完成零 LLM；一次事件风暴只产生一个 decision；快照不含全量房间；决策 token 上限。
- 迁移：v1 库带 drive cron/任务/HITL；v22 已记录版本的补表；SQLite/PG 一致；Windows 路径/时区/DST。
- 跨平台：遵循 `AGENTS.md` §7，`tmp_path`、`posix_only`、不硬编码 `/`。

---

## 12. 明确不做

| 项 | 理由 |
|---|---|
| 外部队列 / Temporal 级引擎 | 单进程个人产品；DB 事件表 + 进程内 worker + sweeper 足够 |
| 每个事件唤醒 LLM | 原则 3；事件必须聚合/分诊 |
| 按工具调用记流水账 | 原则 6；账本只记等待/阻塞点 |
| 每个等待创建 cron | 原则 5；wait 表 + sweeper |
| 任务类型学落码（触发/决策/执行/等待类层级） | 四类是运行时角色，不是类型系统 |
| YAML 规则引擎 / 学习型分诊 | 先用规则跑出误判数据 |
| 伪造 Flex/Batch 路由 | 当前 provider 抽象不支持；只做 defer/suspend/模型降级 |
| 决策单元读全量房间历史 | 成本不可控；快照 + 受限读工具 |
| LLM 承担重试/传输/等待 | 执行器确定性；模型只判断 |
| 预算超限自动 `stop` | 会丢工作；只 pause + 升级 |
| host 直接挂连接器/MCP | 保持最小权限；委托成员取数 |

---

## 13. 风险与开放问题

### 13.1 风险

- **事件风暴/自激**：self-event 抑制、每团队单 decision、聚合窗口、每小时上限。
- **规则误判**：routine auto-close 可能丢判断；不确定时保守升级 decision；任务可标 `requires_review`。
- **计划质量**：LLM 计划需 schema 校验、非法迁移拒绝、CAS 冲突处理；失败的 decision 不重放旧计划。
- **at-least-once**：`idempotency_key` 防账本重复，不防外部副作用；写操作审批或连接器幂等。
- **计量前提**：成员/收口 usage 未记录前，预算不可信；这是阶段 0 的硬门槛。
- **身份/凭证**：成员用各自 agent 配置；审计 actor 必须可区分；不要复用创建人权限。
- **迁移复杂度**：drive cron/在途任务/HITL/usage 四类数据；先影子后切换，保留回滚。
- **UX 变化**：团队不再表现为 cron；需要 runtime 状态和暂停/恢复入口。
- **单进程假设**：多 worker 需重新设计 `active_decision_id` CAS 和 inbox 归属。
- **预算单位**：token 对多模型不公平；成本预算需要定价数据。

### 13.2 开放问题

1. 交互聊天中的派工是否也一律走 dispatcher？建议「产生承诺就走 task/wait；纯只读查询可例外」。
2. 目标完成由 coordinator 根据任务终态推断，还是决策单元显式 `goal done`？建议后者。
3. 哪些结果需要 host 复核：任务元数据 `requires_review`，还是每次判断？建议前者默认、后者例外。
4. 决策单元是否使用更便宜的模型？需要模型定价/能力配置后再做。
5. `hitl_pending` 是否最终并入 `team_waits`？短期 `external_ref` 关联。
6. 事件/决策/等待的保留期与归档策略。
7. 多进程/多租户下的协调者归属。
8. 外部副作用的重试边界：哪些连接器支持幂等键，哪些必须人工确认。

---

## 附录 A：与旧 v2 草稿的差异

| 旧 v2 | 本版 | 原因 |
|---|---|---|
| 每团队 drive cron + interval floor | `team_runtime` + 全局 `team_sweeper` | 事件驱动；消除一次性 cron/固定 id/stop 清理/meta 更新 |
| `_prepare_team_wake` 预检 | coordinator 事件规则 | 分层；通用 cron 不依赖团队表 |
| cron `meta` 存水位/noop | `team_runtime/team_events` | 无 meta 更新路径；update 会重排任务 |
| 自适应 interval 改写 trigger | 事件驱动 + heartbeat | 预检已让不唤醒近似零成本；改 cron 无收益 |
| `dispatch_ledger` 每派发一行 | `team_waits(kind=member_reply)` | 等待粒度；统一重投/超时 |
| `sleep_until` 创建 `date:` cron | `team_waits(kind=timer)` | 等待一等；无 per-wait cron |
| 只改 wake 提示词 | 决策简报 + plan-only 工具 | 规划执行解耦 |
| `drive_disable` 终态 | `finish/pause/stop` 三态 | stop 是破坏性动作 |
| 预算只拦 wake | decision 预留 + dispatch 闸门 + 收口检查 | 成员/回叫也消耗 |
| KPI 从 METRICS 推导 | 决策/事件/等待表 + usage | 进程内计数不可历史查询 |

## 附录 B：现状证据（实现时需回看）

- 团队 host 工具白名单：`src/octop/infra/agents/teams/service.py`（`HOST_TOOLS_ALLOWED`）。
- 派发缝：`src/octop/infra/agents/teams/team_manager.py`（`wire_host_dispatch`、`on_reply`、`stream_peer_to_room`）。
- 驱动 cron/停止：`src/octop/infra/agents/teams/drive.py`（`enable_team_drive`、`stop_team_drive`、`reap_stale_tasks`）。
- 看板状态机：`src/octop/infra/agents/teams/task_state.py`、`board_tools.py`。
- 持久 HITL：`src/octop/infra/db/repos/hitl.py`、`022_light_apps.sql`。
- 用量：`src/octop/infra/db/repos/usage.py`、`001_initial.sql` 的 `usage_log`。
- 系统任务注册：`src/octop/infra/cron/manager.py`（`schedule_system_job`）。
- 迁移 helper：`src/octop/infra/db/migrate.py`（`_ensure_team_board_schema`）。
- 前端团队入口：`dashboard/src/pages/Teams/`、`dashboard/src/api/modules/teams.ts`。
