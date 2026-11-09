# Super-agent 控制面：建议架构

Status: design record / draft
Date: 2026-11-07
Scope: `docs/superagent-borrowing-design.md` 的模块划分、依赖方向、并发模型与生命周期。
讲**代码怎么摆**，不讲为什么这么设计（那在 design 文档）也不讲人看到什么
（那在 e2e-story 文档）。

前置：`docs/superagent-borrowing-design.md`（下称"设计文档"）。

---

## 1. 架构总则

三条约束来自 `AGENTS.md` §5 与 §7，新代码必须遵守：

| 约束 | 含义 |
|---|---|
| **依赖向内** | `api/` → `infra/` → `infra/db/` / `infra/utils/`；`infra/` 不得 import `api/`、`cli/`、`launch.py` |
| **repo 只做 SQL** | `infra/db/repos/` 不得 import 其他 `infra` 包 |
| **单进程 asyncio** | 无外部队列，无跨进程协调；恢复靠数据库状态 + 扫查 |

新增一条**架构自约束**：

> **`infra/work/` 不 import `infra/agents/`、`infra/gateway/`、`infra/cron/`。**
> 控制面（该不该做、授权到哪、什么时候恢复）与执行面（怎么跑一次 turn、
> 怎么发消息）之间只通过端口通信。端口在 `infra/work/ports.py` 定义，
> 实现由 `infra/server.py` 注入。

这条约束的理由是 `infra/events/` 已经踩过的坑：`ExternalEventWorker` 通过
`TeamTargetResolver` / `ExpertDeliver` 两个 Callable 注入执行能力
（`src/octop/infra/events/worker.py:51`、`:53`），而不是 import 团队与 cron 模块。
新包沿用这个模式，且做得更彻底。

---

## 2. 模块图

```
                              ┌───────────────────────┐
   api/routers/               │  api/routers/          │
   external_events.py  ──────▶│    work_tickets.py     │  (新增，只读 + 重放)
   autonomy.py          ──────▶└──────────┬────────────┘
                                          │
                              ┌───────────▼────────────┐
                              │      infra/work/       │  控制面
                              │  router  ← 编排        │
                              │  gate    ← 准入        │
                              │  assessor← 研判        │
                              │  policy  ← 授权        │
                              │  sweeper ← 看门狗      │
                              │  outbox  ← 结果投递    │
                              │  model   ← 类型        │
                              │  ports   ← 端口        │
                              └───┬───────────┬────────┘
                                  │           │
                    ┌─────────────▼──┐   ┌────▼─────────────────┐
                    │ infra/db/repos │   │ 端口实现（server 注入）│
                    │  work_tickets  │   │  ExecutorPort        │
                    │  work_outbox   │   │   → harness          │
                    └────────────────┘   │  NotifierPort        │
                                          │   → gateway          │
                                          └──────────────────────┘

   ┌──────────────────────────────────────────────────────────┐
   │ 既有模块（不改内部，只改接线）                             │
   │ infra/events/  infra/cron/  infra/proactive/  infra/apps/ │
   └──────────────────────────────────────────────────────────┘
```

---

## 3. `infra/work/` 模块职责

| 模块 | 职责 | 依赖 | 不该做 |
|---|---|---|---|
| `model.py` | 领域类型：`Ticket`、`Mandate`、`Stream`、`GateVerdict`、`Assessment`、`TicketStatus`、展示阶段推导、`stream_key` 派生规则 | stdlib、`infra.utils.ulid` | 任何 I/O |
| `ports.py` | 端口协议：`ExecutorPort`、`NotifierPort`、`ClockPort`、`TicketStore` | `model.py` | 任何实现 |
| `gate.py` | 六步准入链（设计 §5.3），**无副作用**：`(trigger, policy, clock, counters) -> GateVerdict` | `model.py`、`policy.py` | 调 LLM、写数据库 |
| `policy.py` | 策略版本哈希、Mandate 重校验（设计 §5.7） | `model.py` | 持久化 |
| `assessor.py` | 批量语义研判：事件卡组装、LLM 调用、决定解析、规则覆盖检查 | `model.py`、`ports.py` | 建工单（那是 router 的事） |
| `router.py` | 周期 pass：取候选 → Gate → Assessor → 建/续工单 → 派工 → 回收 Report | 全部 | 直接调用 harness/gateway |
| `sweeper.py` | 看门狗：租约、心跳、目标一致性、恢复窗口到期（设计 §5.12） | `model.py`、`ports.py` | 改业务状态机之外的事 |
| `outbox.py` | 结果投递：幂等键、同工单同会话有序、重试退避 | `model.py`、`ports.py` | 决定说什么（内容由 router 给） |

**为什么 `gate.py` 必须无副作用**：它是整套架构里唯一需要"零成本跑全量"的部分。
不写库、不调模型、不改计数器（除非调用方显式传入可变计数器），让它可以
在单测里用表驱动覆盖所有分支，不需要起数据库、不需要 mock 时间、不需要假 LLM。

> 注意措辞：它**不是纯函数**——第 4/5/6 步依赖时段、预算计数、限流计数，
> 这些都是外部状态。准确说法是「无副作用」。判据是
> `src/octop/infra/events/service.py:79` 的 `matches_filter`，它无副作用但读外部配置。

**为什么 `assessor.py` 不管建工单**：研判的语义是"这件事值不值得做"，
建工单的语义是"授权并登记"。两者失败的处理不同——研判失败记 `unavailable`
（可重试），建工单失败是事务回滚（立即暴露 bug）。混在一起会让错误处理含糊。

---

## 4. 持久层

### 4.1 新增 repo

| 文件 | 表 | 关键方法 |
|---|---|---|
| `infra/db/repos/work_tickets.py` | `work_tickets`、`work_ticket_streams` | `create` / `get` / `list_open` / `list_due_for_dispatch` / `claim_lease` / `transition`（CAS）/ `end` / `subscribe_stream` / `streams_for` |
| `infra/db/repos/work_outbox.py` | `work_outbox` | `enqueue` / `list_due` / `claim` / `finish` / `requeue_expired` / `list_behind`（顺序检查） |

repo 只做 SQL，不 import 其他 `infra` 包（`AGENTS.md` §5）。状态机的合法性
（哪些迁移允许）留在 `infra/work/model.py`，repo 的 `transition` 只做
`UPDATE ... WHERE status = ?` 的 CAS。

### 4.2 既有表的改动

| 表 | 改动 | 归属 |
|---|---|---|
| `external_events` | `+ stream_key` | `infra/events/` 写入，`infra/work/` 只读 |
| `external_deliveries` | `+ ticket_id` | `infra/events/` 写入，`infra/work/` 只读 |
| `external_subscriptions` | `+ strength`、`+ scope_json` | `infra/events/` |
| `proactive_care_config` | `+ accept_mode`、`+ rules_json` | `infra/work/policy.py` 读，`autonomy_policy` repo 写 |

**写入权归属**（对齐 DeepSeek 的"每类状态一个写者"）：

| 状态 | 唯一写者 |
|---|---|
| `work_tickets` / `work_ticket_streams` | `infra/work/router.py` |
| `work_outbox` | `infra/work/outbox.py` |
| `external_events` / `external_deliveries` | `infra/events/` |
| `proactive_care_config` | `AutonomyPolicyRepo` |

`infra/work/` 对 `external_*` 只有读权限。这条要写进 code review 清单。

---

## 5. 数据流

### 5.1 入口：事件到工单

```
POST /api/hooks/{source_id}
  │
  ├─ hooks.py          签名 / 大小 / 重放窗口        [既有]
  ├─ ExternalEventService.ingest()
  │    ├─ Gate 1  源启用                             [既有]
  │    ├─ Gate 2  订阅过滤命中                        [既有]
  │    └─ 落 external_events + external_deliveries    [既有]
  │
  └─ worker.wake()                                     [既有]
        │
        ▼
  WorkRouter.pass()   ── 周期见 §16 A3，一轮只处理一轮   [新增]
        │
        ├─ WorkRouter 取本轮候选
        │    repo.list_due_for_evaluation()
        │    → external_deliveries.status='pending' AND target_type='expert'
        │      AND mode='expert.turn' AND ticket_id=''
        │
        ├─ Gate 3-6（无副作用，零 LLM）
        │    gate.evaluate(trigger, policy, now, counters)
        │    ├─ 范围不匹配 → verdict=filtered
        │    ├─ 时段外/额度尽 → verdict=deferred（落 next_retry_at）
        │    └─ 限流命中 → verdict=skipped
        │
        ├─ Assessor.assess(batch)          ── 一次调用覆盖本轮所有候选
        │    accept_mode=off      → 直接结束，不调用
        │    accept_mode=observe  → 记录 declined
        │    accept_mode=auto     → 通过则建工单
        │
        └─ Router.create_ticket(...)        ── 一个事务
             work_tickets + work_ticket_streams + external_deliveries.ticket_id
```

**职责切分的关键点**：Gate 1-2 留在 `infra/events/`（因为那是订阅语义），
Gate 3-6 移到 `infra/work/gate.py`（因为那是授权语义）。限流从 ingest
移到 gate 第 6 步——它属于"这次不做"，不是"收不收这个事件"。

### 5.2 出口：工单到结果

```
WorkRouter.dispatch(ticket)
  │
  ├─ ExecutorPort.run(                 ── 端口，实现指向 harness
  │     session_key="agent:{aid}:work:{tid}",
  │     prompt=事件卡 + 工单背景,
  │     tools=[work_read_event, ...])
  │
  ├─ Report 回调
  │     progress / waiting / ready / blocked / failed / dropped
  │     → work_tickets.status + last_report
  │     → 若 blocked 且有提问 → Outbox.enqueue(purpose='attention')
  │
  └─ Outbox.enqueue(ticket, purpose, text)
        └─ NotifierPort.send(...)      ── 端口，实现指向 gateway
```

`ExecutorPort` 的实现放在 `infra/server.py` 的接线里（不是 `infra/work/` 内部），
形状与现有 `ExpertTarget.deliver` 一致：接收 `CronDeliveryCommand`-like 的参数，
内部走 `CronDeliveryService`。

---

## 6. 并发模型

单进程单事件循环。所有并发都在 asyncio 里，用信号量限流。

| 执行体 | 并发度 | 说明 |
|---|---|---|
| `WorkRouter.pass()` | **1** | 一轮只跑一轮；新 pass 请求合并到下一轮（对齐现有 `Passes` 语义） |
| Gate | 无限（同步、无副作用） | 微秒级，不占 slot |
| Assessor | 批量合并 | 一轮一次 LLM 调用，不并发多轮 |
| 派工 `ExecutorPort.run` | `MAX_CONCURRENT_WORK`（默认 4） | 与现有 `MAX_CONCURRENT_TEAM=2` / `MAX_CONCURRENT_APP=4` 同一模式 |
| 人触发的唤醒 | 额外 +1 slot | 借 DeepSeek 的 `WorkerSlots`：人的消息永远不能被后台任务堵住 |
| Outbox 投递 | `MAX_CONCURRENT_NOTIFY`（默认 2） | 同工单同会话严格有序 |

**为什么 pass 串行**：Gate 与决策需要一致的快照（同一轮里同一 stream 的多条事件
要一起研判）。并发 pass 会让"这条事件该不该和上一条合并"变得不确定。

**为什么派工要限流**：harness 的 agent 实例是重对象（workspace、MCP 连接、
模型客户端）。无限制派工会把内存打爆。

---

## 7. 生命周期

### 7.1 启动

接在 `src/octop/infra/server.py:772` 的 `external_event_worker.start()` 之后：

```python
# server.py 现有顺序保持不变
external_event_worker.start()          # 既有

# 新增，紧随其后
work_router = WorkRouter(
    repos=services.repos,
    policy_reader=AutonomyPolicyReader(services.repos),
    executor=HarnessExecutorPort(agent_registry, cron_delivery),   # 端口实现
    notifier=GatewayNotifierPort(gateway),                          # 端口实现
    clock=SystemClock(),
)
work_sweeper = WorkSweeper(repos=services.repos, clock=SystemClock())
work_router.start()
work_sweeper.start()
```

两者都挂在 `AppRuntime` 上，与 `external_event_worker` 同级。

### 7.2 关闭

顺序：先停接新（router），再等在飞（in-flight），最后停扫查（sweeper）。

```python
await work_router.shutdown()   # 停止接受新 pass，等在飞 pass 完成
await work_sweeper.shutdown()
```

**不等 Worker**：harness 的 turn 可能跑几分钟。关闭时把在飞工单标记为
`interrupted`，重启后由 `WorkSweeper` 按设计 §5.12 处理（`started_at` 非空
→ `needs_review`）。

### 7.3 热切换

沿用 `server.py:330-372` 的 `replace_persistence` 模式：数据库热切换时
重建 repo 引用，但**不重建端口实现**（harness 与 gateway 不随数据库切换）。

---

## 8. 与三个既有平面的接线

### 8.1 外部事件平面

| 改动点 | 位置 | 说明 |
|---|---|---|
| `stream_key` 派生 | `infra/events/service.py` 的 ingest | 按源类型派生（见 §8.4） |
| 限流移出 | `service.py` 限流分支 | 移到 `gate.py` 第 6 步；ingest 不再静默丢事件 |
| `expert.turn` 不再直接执行 | `infra/events/expert_target.py` | 改为：只登记 delivery，交 router 处理 |
| `expert.notify` 保持 | 同上 | 通知型不需要工单 |
| team / app target 保持 | `infra/events/worker.py` | 团队与 light-app 走各自既有路径 |

**`expert.turn` 的改动是这套架构里唯一破坏兼容的地方**：现在它直接调
`CronDeliveryService.deliver()` 跑一次 turn，改后要经过 Gate → Assessor → 工单。
必须提供开关：`work_control_enabled`（默认 false），灰度打开。

### 8.2 自主体平面

| 改动点 | 位置 |
|---|---|
| 心跳触发改为产出 Trigger | `infra/agents/teams/sweeper.py` 在触发心跳前先产出一个 `origin_kind='autonomy'` 的 Trigger |
| 会话隔离 | 工单派工走 `work_session_key`，不再复用 DM 线程 |
| `accept_mode` 读取 | `infra/agents/teams/runtime.py` 从 `AutonomyPolicy` 读，不再只读 `autonomy_enabled` |

### 8.3 数据平面

| 改动点 | 位置 |
|---|---|
| `external_sources` 新增 kind | `infra/events/service.py` 的 `SOURCE_KINDS` |
| 同步完成产出事件 | `infra/apps/service.py` 的 `_record_run` 之后，按同步规则产出 |
| `sync_run_id` 幂等 | 同上 |

### 8.4 `stream_key` 派生规则

| 源类型 | 优先级 |
|---|---|
| webhook / connector_poll | 源配置里的 `stream_field`（点路径，如 `order_id`）；**未配置则用 `external_id`** |
| light_app | `app_id:table` |
| autonomy | `heartbeat:{agent_id}` |
| 兜底 | `event:{event_id}`（即每个事件独立成流） |

**不猜测业务字段**：某类业务希望按订单聚合，就在建源时显式写
`stream_field: "order_id"`。这把聚合粒度变成配置，而不是对业务字段的硬编码假设
（见设计文档 §6.6）。

派生函数放在 `infra/work/model.py`（无副作用），由 `infra/events/` 调用。
这样 `infra/work/` 不需要 import `infra/events/`。

---

## 9. 与 harness 的边界

| 层 | 归谁 | 说明 |
|---|---|---|
| 准入、授权、工单生命周期 | **Octop `infra/work/`** | 与模型无关的确定性逻辑 |
| 一次 turn 的执行、工具调用、上下文压缩 | **`octop-harness`** | Octop 只给 prompt + session_key + 工具集 |
| 会话隔离 | **Octop**（会话键） + **harness**（session 存储） | Octop 决定键，harness 决定存哪 |
| 记忆、workspace | **harness** | `HarnessAgent.workspace`（`AGENTS.md` §7） |

**端口边界**是 `ExecutorPort.run()`。它接收的东西必须是纯数据（prompt、工具名列表、
会话键），不能是 harness 对象。这样 `infra/work/` 的单测可以用假端口，
不需要起 harness。

---

## 10. 观测

### 10.1 指标

用 `octop.infra.metrics.METRICS`（懒 import，避免循环）：

| 指标 | 类型 | 标签 |
|---|---|---|
| `work_gate_total` | counter | `verdict=passed/filtered/deferred/skipped` |
| `work_assessor_total` | counter | `outcome=accept/ignore/unavailable` |
| `work_assessor_latency_ms` | histogram | — |
| `work_ticket_total` | counter | `event=created/resumed/ended` |
| `work_ticket_stage` | gauge | `stage=person/answer, person/review, working, idle, ended` |
| `work_outbox_total` | counter | `status=sent/dropped/failed` |
| `work_pass_duration_ms` | histogram | — |

### 10.2 面板视图

复用 `api/routers/work_tickets.py` 的只读接口，供面板渲染：

| 视图 | 内容 |
|---|---|
| 待人工 | `status='blocked'` 或有 `attention` 待答 |
| 工作中 | `status ∈ {pending, working}` |
| 已结束 | 按 `ended_at` 倒序 |
| 已过滤/已跳过 | 按 `reason` 分组，回答"这条为什么没反应" |

### 10.3 健康

不新建健康看板。用指标 + 面板视图：连续失败计数由 `work_outbox` /
`work_sweeper` 的状态列暴露。

---

## 11. 失败模式

| 组件失败 | 表现 | 恢复 |
|---|---|---|
| Gate 抛异常 | 该轮 pass 中止，候选留在 `pending` | 下一轮重试；连续失败 3 次记错误指标 |
| Assessor 超时/报错 | 候选标 `unavailable`，可重试 | 下一次事件触发新一轮研判 |
| Assessor 返回无法解析 | 同上，附带原始输出便于排查 | — |
| 建工单事务失败 | pass 回滚，候选留在 `pending` | 下一轮重试 |
| 派工失败（provider 抖动） | 工单留 `pending`，按退避重试 3 次 | 成功则继续；超限转 `failed` |
| Worker turn 崩溃 | 工单 `failed`，`last_report` 保留错误 | 人或 sweeper 触发重试 |
| 进程重启 | 在飞工单标 `interrupted` | 按 `started_at` 分流（设计 §5.12） |
| Outbox 发送失败 | 消息留 `pending`，退避重试 | 目标消失则 `dropped`（永久） |
| 数据库不可用 | pass 中止，端口不调用 | 连接恢复后下一轮继续 |

**共同原则**：状态永远先进数据库再执行动作（对齐现状 `docs/external-events-design.md`
的"先存储后执行"）。没有任何一步是"先做了再记"。

---

## 12. 扩展点

| 想加什么 | 改哪里 | 不用改什么 |
|---|---|---|
| 新的 Source 类型 | `SOURCE_KINDS` + `stream_key` 派生规则 + 实现 `scope`/`channel` | `infra/work/` 任何代码 |
| 新的执行目标 | 实现 `ExecutorPort`，在 `server.py` 注入 | `gate.py` / `assessor.py` |
| 新的通知渠道 | 实现 `NotifierPort` | `outbox.py` |
| 新的主动规则 | `rules_json` 加一条 | 代码 |
| 新的准入条件 | `gate.py` 加一步（**必须排在限流之前**） | 其他模块 |
| 新的工单状态 | `model.py` 的状态表 + repo 的 CAS | router 骨架 |

---

## 13. 代码落点总览

```
新增
  src/octop/infra/work/
    __init__.py
    model.py            领域类型、状态、stream_key 派生、展示阶段
    ports.py            ExecutorPort / NotifierPort / ClockPort / TicketStore
    policy.py           策略版本、Mandate 重校验
    gate.py             六步准入链（无副作用）
    assessor.py         批量研判
    router.py           pass 编排 + 派工 + Report 回收
    sweeper.py          看门狗
    outbox.py           结果投递
  src/octop/infra/db/repos/work_tickets.py
  src/octop/infra/db/repos/work_outbox.py
  src/octop/api/routers/work_tickets.py
  src/octop/infra/db/migrations/029_*            折叠进已有 029
  tests/unit/work/                                 gate / policy / model 表驱动
  tests/integration/test_work_control_e2e.py       E1–E12

修改（只改接线，不改内部逻辑）
  src/octop/infra/server.py                        启动/关闭/热切换
  src/octop/infra/events/service.py                stream_key、限流移出
  src/octop/infra/events/expert_target.py          expert.turn 交 router
  src/octop/infra/events/worker.py                 目标分派加开关
  src/octop/infra/agents/teams/sweeper.py          心跳产出 Trigger
  src/octop/infra/agents/teams/runtime.py          会话隔离、accept_mode
  src/octop/infra/cron/manager.py                  会话隔离
  src/octop/infra/proactive/scheduler.py           会话隔离 + 异常重排
  src/octop/infra/apps/service.py                  同步产出事件 + sync_run_id
  src/octop/infra/db/services.py                   RepoBundle 加两个 repo

不动
  src/octop/infra/agents/teams/{coordinator,dispatcher,task_state}.py
  src/octop/infra/agents/manager.py                 （除了 autonomy 相关接线）
  octop-harness（外部包）
```

---

## 14. 架构验收清单

实现完成后逐条核对：

- [ ] `infra/work/` 的 import 里没有 `infra.agents`、`infra.gateway`、`infra.cron`、`api`
- [ ] `infra/db/repos/work_*.py` 的 import 里没有其他 `infra` 子包
- [ ] `gate.py` 无副作用，单测不依赖数据库、网络（时钟与计数器由调用方注入）
- [ ] `work_tickets` 的写入只出现在 `router.py`
- [ ] `external_*` 系列表在 `infra/work/` 里没有任何写入
- [ ] `server.py` 的关闭顺序是 router → sweeper，且有超时
- [ ] 热切换后 `work_router` 持有的是新 repo 引用（有测试）
- [ ] 所有状态迁移都是 CAS，没有"先读再写"
- [ ] `work_control_enabled=false` 时行为与改动前完全一致
- [ ] 新增指标在 `/api/admin/metrics` 可见

---

## 15. 与设计文档的差异记录

| 项 | 设计文档 | 本文 | 原因 |
|---|---|---|---|
| 限流位置 | Gate 第 6 步 | 同，但从 `infra/events/` 移入 `gate.py` | 避免两处限流、避免 ingest 静默丢事件 |
| `expert.turn` 默认行为 | 走工单 | 受 `work_control_enabled` 开关控制，默认关 | 破坏性改动需要灰度 |
| 工单合并规则 | 未明确 | 同 `stream_key` 合并为一个工单，不同 stream 各建 | 对齐"Thread 是研判单位" |
| 健康看板 | 提到 HealthBoard | 不新建，用指标 + 面板视图 | Octop 没有 HealthBoard 基础设施，新建成本高且收益低 |

---

## 16. 待确认

| 编号 | 问题 | 影响 |
|---|---|---|
| A1 | `work_control_enabled` 的开关粒度 | 全局 / 每源 / 每订阅？建议全局先行，B-Phase 2 再细化 |
| A2 | `MAX_CONCURRENT_WORK` 的默认值 | 4 是拍脑袋；需要按 harness 单 agent 内存占用实测 |
| A3 | `WorkRouter.pass()` 的周期 | 秒级（响应快但空转多）vs 30 秒（对齐现有 `AppSourceScheduler`）？**需实测决定，不引用外部数字** |
| A4 | 是否复用 `TeamSweeper` 的 tick 而非独立 `WorkSweeper` | 复用省一个协程，但会让团队与工单的扫查周期耦合 |
