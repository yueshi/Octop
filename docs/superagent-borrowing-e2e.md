# Super-agent 控制面：E2E Use Case 描述

Status: design record / draft
Date: 2026-11-06
Scope: `docs/superagent-borrowing-design.md` 的端到端使用场景。用途有二——
(1) 说明目标架构跑起来之后**人看到的完整旅程**；(2) 作为 E2E 测试的规格来源。

前置阅读：`docs/superagent-borrowing-design.md`（下称"设计文档"）。

叙事版见 `docs/superagent-borrowing-e2e-story.md`：用一家小电商的一夜加一天，
把本文 UC-1 到 UC-4 的机制串成一个可读的故事。要理解"人看到什么"读那一篇，
要写测试读本文。

---

## 0. 角色与素材

| 角色 | 说明 |
|---|---|
| **Alice** | 普通用户。拥有一个专家 agent「巡检员」和一个 light app「订单看板」 |
| **订单系统** | 外部系统，通过 webhook 推送订单事件（`order.created` / `order.paid` / `order.refunded`） |
| **Bob** | 另一位用户。用于验证跨用户隔离 |

所有场景都在 `tests/integration/` 下的 fake harness 上跑（`env_fake_harness` fixture，
`tests/support/harness.py` 的 `patch_harness`），不调真实 LLM、不调真实外部系统。

**接口面**（已存在的标"现有"，设计新增的标"新增"）：

| 用途 | 端点 |
|---|---|
| 建/列事件源 | `POST /api/external/sources`、`GET /api/external/sources`（现有） |
| 建/列订阅 | `POST /api/external/subscriptions`、`GET /api/external/subscriptions`（现有） |
| 收事件（公开） | `POST /api/hooks/{source_id}`（现有） |
| 看事件 / 投递 | `GET /api/external/events`、`GET /api/external/deliveries`（现有） |
| 自主策略 | `GET/PUT /api/agents/{agent_id}/autonomy-policy`（现有接口，字段新增） |
| 自主开关 | `PUT /api/agents/{agent_id}/autonomy`（现有） |
| 收件箱 / 指标 | `GET /api/agents/{agent_id}/inbox`、`GET /api/agents/{agent_id}/metrics`（现有） |
| 工单 | `GET /api/external/work-tickets`（**新增**） |
| 工单详情 | `GET /api/external/work-tickets/{ticket_id}`（**新增**） |

---

## UC-1（主线）：外部事件 → 专家自主工作 → 结果回推

**这是整套设计要证明的那条路**。其余 use case 都是它的分支或降级。

### UC-1.1 前置配置

| 步 | Alice 做什么 | 期望状态 |
|---|---|---|
| 1 | 登录，建 provider，建专家 agent「巡检员」并启动 | agent 处于 running |
| 2 | `POST /api/external/sources {"name":"Orders","kind":"webhook"}` | 拿到 `source_id` 与 `secret` |
| 3 | `POST /api/external/subscriptions`，`target_type=expert`、`target_id=巡检员`、`mode=expert.turn`、`filter={"event_type":"order.created"}` | 订阅存在，`strength=inferred`（默认） |
| 4 | `PUT /api/agents/{agent_id}/autonomy-policy`，`accept_mode="observe"`、`responsible_scope="order"`、`rules_json=[{"id":"over_threshold","instruction":"订单金额超过 5000 时跟进"}]` | 策略生效，`policy_version` 非空 |

> 第 4 步是**观察期**。Alice 先想看看这个专家"本来会做什么"，而不是让它直接开工。

### UC-1.2 第一次事件：observe 模式下只记录，不执行

订单系统推送：

```http
POST /api/hooks/{source_id}
X-Octop-Event-Id: ord-1001
X-Octop-Event-Type: order.created
X-Octop-Timestamp: <now>
X-Octop-Signature: <hmac>
{"order_id":"ord-1001","amount":8200,"currency":"CNY"}
```

**期望**（按时间顺序断言）：

| 断言点 | 期望 |
|---|---|
| `GET /api/external/events` | 1 条事件，`summary` 非空，`payload_json` 完整保存 |
| Gate 第 2 步（事件类型） | 放行 |
| Gate 第 3 步（职责范围 `order`） | 放行 |
| Assessor | 被调用一次，判定命中 `over_threshold` 规则 |
| `GET /api/external/work-tickets` | 1 条工单，`status='declined'`（observe 的记录），`reason` 含规则 id 与依据 |
| `GET /api/agents/{agent_id}/inbox` | 1 条"本来会做什么"的记录，含订单摘要与命中规则 |
| **harness** | 巡检员 agent **没有**被调用（fake agent 的 `last_request` 未变） |
| `GET /api/external/deliveries` | 无 `delivered` 记录 |

**关键性质**：这一整轮**没有产生任何对人的打扰**，但留下了可审计的"它本来会做什么"。

### UC-1.3 切到 auto：同一类事件真的开工

Alice 看过 observe 记录，确认规则合理，`PUT /api/agents/{agent_id}/autonomy-policy`
把 `accept_mode` 改成 `auto`。

订单系统推送**新的**事件（`external_id` 必须不同，否则被 `dedupe_key` 判为重复）：

```http
X-Octop-Event-Id: ord-1002
{"order_id":"ord-1002","amount":9200,"currency":"CNY"}
```

**期望**：

| 断言点 | 期望 |
|---|---|
| 事件 | 1 条新事件入库 |
| Assessor | 再次被调用，判定通过 |
| 工单 | 1 条 `status='pending'`、`strength='inferred'`、`policy_version` 等于当前策略版本 |
| 派工 | 工单转为 `working`，`session_key` = `agent:{agent_id}:work:{ticket_id}` |
| **harness** | 巡检员被调用；**断言 `last_request` 的会话键是 work 会话，不是 `dashboard_key` 的 DM 会话** |
| 上下文 | 传给 Worker 的是**事件卡**（含 `event_id`、源、类型、时间、摘要），不是裸摘要串 |
| Worker 产出 | 工单 `last_report` 记录进展，`status` 保持 `working` |
| 完成 | 工单转 `done` |
| 结果回推 | 收件箱 / IM 出现一条结果消息 |
| 幂等 | 重放 `ord-1002`（同 `external_id`）→ `POST /api/hooks` 返回 duplicate，**不新增工单** |

### UC-1.4 直通：同一事件流的后续事件不再打扰评估

订单系统对同一笔订单推送后续事件，`stream_key` 相同（`order_id=ord-1002`）：

```http
X-Octop-Event-Id: ord-1002-paid
X-Octop-Event-Type: order.paid
{"order_id":"ord-1002","amount":9200}
```

**期望**：

| 断言点 | 期望 |
|---|---|
| Assessor | **不再被调用**（直通，绕过评估） |
| 工单数 | **不新增**工单 |
| 原工单 | 该事件成为它的输入；工单从 `done`/`waiting` 回到 `working` 并被再次派工 |
| 输入历史 | 工单详情里能看到这条输入（可审计） |
| 授权 | 这条输入**不授予新权力**：工单的 `scope_json` 与 `strength` 不变 |
| 恢复约束 | 仅在恢复窗口（默认 7 天）内、且累计恢复次数未超上限（默认 3）时恢复；超限则该事件走完整 Gate+Assessor |

### UC-1.5 需要人：提问与恢复

Worker 执行中遇到超出职责范围的情况，向人提问。

**期望**：

| 断言点 | 期望 |
|---|---|
| 工单 | `status='blocked'`，`last_report` 含具体问题 |
| 阶段 | 展示阶段为"需人工：等待回答" |
| 收件箱 | 出现 1 条待答项 |
| Alice 回答 | 回答作为输入回到工单 |
| 工单 | 恢复 `working` → `done` |
| 通知策略 | 提问**立刻**通知（`attention` 用途），进展类消息不通知 |

### UC-1.6 旅程总览

```
Alice 建源 + 建订阅 + 配策略(observe)
        │
订单事件 ─▶ Gate(程序级 6 步) ─▶ Assessor(语义) ─▶ 工单(declined) ─▶ 收件箱记录
                                                                  （不执行、不打扰）
Alice 切 auto
        │
订单事件 ─▶ Gate ─▶ Assessor(通过) ─▶ 工单(pending) ─▶ 派工 ─▶ 隔离会话 Worker
                                                                  │
                                                        事件卡 + 工具
                                                                  │
                                                            Report / 提问
                                                                  │
                                              ┌───────────────────┴──────────────┐
                                          提问                                结果
                                              │                                │
                                        工单(blocked)                      工单(done)
                                        收件箱待答                          Outbox 回推
                                              │
                                        Alice 回答 ─▶ 工单恢复 ─▶ done
```

---

## UC-2：准入链的逐级拒绝（Fast Path）

同一个源、同一类事件，逐条触发 Gate 的每一级拒绝。**每种拒绝都必须留下可观测的痕迹，
不允许事件凭空消失。**

| 变体 | 前置 | 期望 |
|---|---|---|
| 2a 事件类型不匹配 | 订阅 filter 只认 `order.created`，推 `order.paid` | 事件入库但标记为 filtered；**无工单**；`GET /api/external/events` 能看到它被过滤的原因 |
| 2b 职责范围不匹配 | `responsible_scope="invoice"`，事件是 order | 同 2a，`filtered` |
| 2c 时段外 | `active_hours_start=09:00`，当前 23:00 | 标记为 **deferred（延后，不是拒绝）**；到 09:00 后被自动取回并继续流程 |
| 2d 预算耗尽 | `sla_tasks_per_day` 已用满 | **deferred**；次日额度恢复后被取回 |
| 2e 限流命中 | `max_per_minute=1`，一分钟内推第 2 条 | 标记为 **skipped**；`GET /api/external/deliveries` 能看到这条 skipped 记录；事件本身仍在 `external_events` 里 |

> 2c / 2d 是"稍后再做"，2a / 2b 是"不该做"，2e 是"这次不做"。**三者的区别必须在数据上可见**，
> 否则运维无法回答"这条事件为什么没反应"。

---

## UC-3：崩溃与恢复（Watchdog）

### UC-3.1 Worker 执行中进程被杀

| 步 | 动作 | 期望 |
|---|---|---|
| 1 | 事件触发，工单 `working`，Worker 已开始执行 | `external_deliveries.status='delivering'` 且 `started_at` 非空 |
| 2 | 模拟进程崩溃（lease 到期） | — |
| 3 | 重启后扫查 | 工单**不自动重跑**；投递转为 `needs_review`；收件箱出现一条"需要人工确认这次工作是否已经做了一半" |
| 4 | Alice 确认 | 可选择"重跑"或"标记完成" |

**为什么不能自动重跑**：外部动作（发消息、提交工单、改数据）可能被重复执行。
DeepSeek 的对应做法是 `assignmentInterrupted`——重启时带一条恢复提示
（"检查你的目录与状态，别重复外部动作"），而不是盲目重来。

### UC-3.2 未开始执行就被中断

`started_at` 为空的过期投递 → 退回 `pending` 重试。**与 3.1 的区别必须体现在状态上。**

### UC-3.3 照护调度器意外退出

主动照护的 asyncio task 抛异常 → **被自动重排**，下一次照护照常发生。

（现状：`src/octop/infra/proactive/scheduler.py:259` 的 `_on_task_done` 只打日志不重排，
照护会静默死掉。这是设计文档 B1 要修的。）

---

## UC-4：数据回流（light-app → 事件 → 专家）

订单看板（light app）的数据同步结果驱动专家工作。

| 步 | 动作 | 期望 |
|---|---|---|
| 1 | light app 配置外部数据源，每 15 分钟同步 | 同步运行记录 `last_status='ok'`、`row_count` 正确 |
| 2 | 同步完成，命中"某表新增行数 > 阈值"的事件规则 | `GET /api/external/events` 出现一条 `source_id` 指向 light app 源的事件 |
| 3 | 专家订阅了该源 | 该事件走 **UC-1 相同的流水线**：Gate → Assessor → 工单 → Worker |
| 4 | Worker 用 `work_read_event` 读完整 payload | 拿到分页游标，可继续读 |
| 5 | 重复同步 | **不产生重复行**（`sync_run_id` 幂等），也不产生重复事件 |

**这条链路是"数据处理"与"主动工作"合流的证明**：两者共用一条 Trigger → Gate →
Assessor → Ticket 流水线，而不是两套机制。

---

## UC-5：安全阀（accept mode 的三档）

| 模式 | 事件到达 | 期望 |
|---|---|---|
| `off`（默认） | 任意 | **什么都不发生**：无 Assessor 调用、无工单、无收件箱记录 |
| `observe` | 程序级命中 | 记录"本来会做什么"，不执行 |
| `auto` | 程序级 + 语义都命中 | 开工 |

`off` 是出厂默认。**没有显式配置策略的 agent，事件到了也不动。**

---

## UC-6：跨用户隔离

| 步 | 动作 | 期望 |
|---|---|---|
| 1 | Bob 也建了一个专家，并订阅 Alice 的源 | `POST /api/external/subscriptions` 被拒（源属于 Alice） |
| 2 | Bob 直接 POST `/api/hooks/{alice_source_id}` 且签名正确 | 事件进入 Alice 的源；Alice 的专家收到，Bob 的收不到 |
| 3 | Alice 删除源 | 相关订阅、工单输入引用一并失效；后续事件不再入库 |

（现状已有 ACL 校验，见 `src/octop/infra/events/service.py` 的 `_validate_target`；
设计文档要保证新增的工单路径**同样**走这条校验。）

---

## 7. E2E 断言清单（可执行规格）

按仓库惯例，**断言行为，不断言配置**。下表每行对应一个测试函数。

| # | 测试名 | 关键断言 |
|---|---|---|
| E1 | `test_event_observe_mode_records_but_does_not_execute` | 工单 `declined`；收件箱有记录；fake agent 未被调用 |
| E2 | `test_event_auto_mode_creates_ticket_and_runs_worker_in_work_session` | 工单 `done`；`last_request` 的会话键 ≠ DM 会话键 |
| E3 | `test_duplicate_external_id_does_not_create_second_ticket` | 第二次推送返回 duplicate；工单数不变 |
| E4 | `test_same_stream_followup_bypasses_assessor` | Assessor 调用次数不增加；工单数不增加；输入历史新增一条 |
| E4b | `test_followup_after_done_resumes_within_window` | `done` 工单被直通输入拉回 `working`；超过窗口则不恢复 |
| E5 | `test_gate_rejects_by_type_scope_and_limits` | 三个变体各自留下 filtered / deferred / skipped 痕迹；事件本身不丢 |
| E6 | `test_expired_lease_after_start_is_not_retried` | 状态 `needs_review`；fake agent 未被二次调用 |
| E7 | `test_expired_lease_before_start_is_retried` | 退回 `pending` 并重新派发 |
| E8 | `test_proactive_scheduler_reschedules_after_error` | 抛异常后下一次触发仍发生 |
| E9 | `test_light_app_sync_emits_event_and_duplicate_sync_is_idempotent` | 事件入库一次；行数不翻倍 |
| E10 | `test_accept_mode_off_creates_nothing` | 无工单、无收件箱、fake agent 未被调用 |
| E11 | `test_cross_user_subscription_is_rejected` | 403/400；Alice 的收件箱不受影响 |
| E12 | `test_worker_question_blocks_ticket_and_resumes_on_answer` | `blocked` → 回答后 → `done` |

**建议落点**：`tests/integration/test_work_control_e2e.py`（新增），
复用 `tests/integration/test_external_events_api.py` 里的
`_post_webhook` / `_stop_worker` / `_drain_worker` 辅助函数
（`tests/integration/test_external_events_api.py:66`、`:91`、`:98`）。

**注意**：现有测试里 `_stop_worker` 是先停掉 worker 再手工 `tick()` 驱动，
避免后台协程与测试竞态。新增的 E2E 要沿用这个手法。

---

## 8. 尚未被任何 use case 覆盖的缺口

以下是本设计已知但**没有 use case 覆盖**的部分，实现时需要补：

| 缺口 | 需要补的 use case |
|---|---|
| 策略版本变更时的重校验 | `accept_mode` 从 `auto` 改回 `off` 后，一个 `inferred` 工单恢复时重校验失败 → 保持结束，输入回到 Assessor |
| `done` 工单的恢复次数上限 | 恢复 3 次后再来直通输入 → 不自动恢复，收件箱出人工介入项 |
| Outbox 的顺序保证 | 同一工单同一会话的两条结果消息，第二条在第一条成功前不发送 |
| `work_read_event` 的分页 | 超长 payload 分页读取，中途断点续读 |
| 团队平面与工单的边界 | 团队任务与工单互不引用状态（`docs/team-l3-roadmap.md` 的约束） |
| 上下文 Spill | 工单输入超过窗口预算时，历史只留引用链，不全量内联 |
