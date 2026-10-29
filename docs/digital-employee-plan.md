# 数字员工与主动团队 · 最终实施计划（v4）

> 日期：2026-10-28 · 状态：**最终实施计划**（可执行）
> **v3 → v4 变化**：合并自我反思发现的 7 处问题——① 预算粒度改为 **agent/team 级**（已拍板）、② M1.1 覆盖三处越权路径、③ 预算累加必须原子、④ 修正 M2/M3 阶段循环、⑤ 补「关掉自治」语义、⑥ 补 Gate 通则、⑦ 补首次上岗 / 跨平台 / build-frontend / `review → proposed`。
> **编号稳定性**：`S1.x` / `P0.x` / `S2.x` / `S3.x` 不重排（代码注释已引用）；修复项用 `M0.x` / `M1.x`。

## 上游文档与单一真相源

| 文档 | 角色 | 采信方式 |
|---|---|---|
| `docs/team-drive-v2.md` | **运行时真相源** | 数字员工挂载其上（阶段 0–3 已实现，4/5 未实现） |
| `docs/team-autopilot-design.html` | **历史归档**（P1–P4 已落地） | 驱动架构已被 v2 取代；仅采信已落地的 P1/P3/P4 |
| `docs/proactive-expert-design.md` | **可行性分析** | 采信结论；其 C1（HITL 挂起）**已过时** |
| `docs/team-l3-roadmap.md` | **L3 自治路线** | 采信缺口表；A0–A4 重叠部分已归一 |
| `docs/digital-employee-design.html` | 本文的设计版 | 定位判断 + 架构图，与本文同步 |

---

## 0. 目标与非目标

### 0.1 目标

让一个普通专家（`agents.kind='expert'`）通过开关变成**数字员工**：拥有自己的 goal/task 看板、自主醒来干活、受预算约束、可被考核与干预；同时把**团队侧**收敛成单一驱动机制。

### 0.2 非目标

| 不做 | 理由 |
|---|---|
| 新建 `digital_employees` 表 | agent 行就是它（D5） |
| 改 `proactive_care_config` 表名 | 001 已发布，改名需双份重建脚本 |
| 降低 `TEAM_MIN_MEMBERS = 2` | 数字员工是单兵 expert，不是「一个人的 team」 |
| 单人场景复用 `TeamDispatcher.submit_peer` | **harness 硬禁自派发**（`octop_harness/teams/team_manager.py:316-317`） |
| 单人场景复用 `TeamDecisionService` | 决策单元是为「拆给多成员」而生；单人自己就是决策者 + 执行者 |
| 合并 / 重命名 `task_state.py` 的**状态集合** | `team-l3-roadmap.md:275`：`assigned` / `needs_input` 是承重的。**但允许加边**（见 S3.4） |
| 复制一份 `patch_task` 给 agent 路由 | 会产生第二份状态机真相 |
| **task 粒度的预算归集** | 记账点在 harness 层，拿不到 task 上下文（D10，见 §8.2） |

---

## 1. 已拍板决策

| # | 决策 | 取值 | 状态 |
|---|---|---|---|
| D1 | 岗位规格存哪 | 泛化 `proactive_care_config` → autonomy policy（**加列不改表名**） | ✅ 已定 |
| D2 | 第一个可交付 slice | Slice 1 = E1 底座通用化 | ✅ 已定 |
| D3 | 计划落点 | 本文件 | ✅ 已定 |
| D4 | 迁移落点 | 折进 **024**（未发布最高号），版本断言保持 `v == 24` | ⚠️ 待签字 |
| D5 | 自治开关落点 | `agents.autonomy_enabled`（不放 policy 表；顺带让在岗名单可 SQL 查询） | ⚠️ 待签字 |
| D6 | 数字员工挂哪套驱动 | **team-drive-v2 的持久化协调者**，不是 v1/v3 的 per-team cron | ✅ 已定 |
| D7 | 执行单元 | **self-execute**（跑自己一个回合），与 `MemberDispatchExecute` 平级 | ✅ 已定 |
| D8 | 成本预算归属 | **v2 阶段 4 治理项**，团队与数字员工共用唯一实现 | ✅ 已定 |
| **D10** | **预算粒度** | **agent / team 级**。**不做 task 粒度**——记账点在 harness 层，拿不到 task 上下文 | ✅ **已拍板** |
| **D9** | **阶段顺序** | **M0 → M1 → M2 → M3 → M4 → M5 → M6** | ⚠️ 待签字 |

### 1.1 D1 的实现落点

```
proactive_care_config 表（001_initial.sql:246-256 已发布；022 已含加列 DDL）
  + budget_limit_tokens / budget_window / sla_tasks_per_day
  + escalate_to / responsible_scope / tool_scope_allow / tool_scope_deny

代码层：
  infra/db/repos/proactive_care_config.py   ← 不动，care 推送继续用
  infra/db/repos/autonomy_policy.py         ← 新增，读同一张表
```

### 1.2 D4 的依据

022/023/024 **全部未进 main 与 develop**（`git cat-file -e main:...` 全为 N），本分支整块发布 → 折进 **024**，版本断言保持 `v == 24`。若 024 被单独切出发布则改开 025。

---

## 2. 目标架构

```
        ┌──────────────── 人类侧：创建人 / 管理者 ────────────────┐
        │  设岗位 · 开闸门 · 看板（拖拽/审批/收件箱）· 对话干预     │
        └───────────────┬───────────────────────────┬─────────────┘
                        │                           │
        ┌───────────────▼─────────────┐   ┌─────────▼──────────────┐
        │  数字员工（单兵 expert）      │   │  团队（多成员 team）    │
        │  kind=expert + autonomy      │   │  kind=team             │
        │  执行单元 = self-execute      │   │  执行单元 = 成员派发    │
        │  决策 = 自己（跳过 decision） │   │  决策 = decision unit   │
        └───────────────┬─────────────┘   └─────────┬──────────────┘
                        │                           │
        ┌───────────────▼───────────────────────────▼─────────────┐
        │  共享底座（零 kind 约束，全部复用）                       │
        │  team_goals / team_tasks  ·  task_state.py 迁移表         │
        │  team_waits / team_events  ·  team_runtime + sweeper     │
        │  autonomy_policy（岗位） · 成本预算闸门 · HITL 挂起        │
        └──────────────────────────────────────────────────────────┘
```

### 2.1 复用边界

| 组件 | 数字员工 | 理由 |
|---|---|---|
| `team_goals` / `team_tasks` | ✅ 复用 | 外键无 kind 约束（`022_light_apps.sql:98-133`） |
| `task_state.py` 状态机 | ✅ 复用 | 唯一强校验点，不能分裂 |
| `TeamGoalRepo` / `TeamTaskRepo` | ✅ 复用 | repo 层零 kind 过滤 |
| 板工具 `build_team_board_tools` | ✅ 复用 | 同一实现，仅挂载条件与 `self_only` 不同 |
| 看板页 | ✅ 复用 | 按 kind 渲染差异 |
| 主动关怀调度器 | ✅ 泛化 | 策略存储位复用，求值逻辑泛化 |
| `TeamDispatcher.submit_peer` | ❌ 不可用 | **harness 硬禁自派发** |
| **self-execute** | 🆕 新增 | 与成员派发**平级**：`agent_manager.stream(agent_id, request)` |
| `TeamDecisionService` | ❌ 跳过 | 单人自己就是决策者 + 执行者 |
| per-team drive cron（v1/v3） | ❌ 不复用 | v2 附录 A 已明确弃用 |

### 2.2 四条必须成立的不变量

> **INV-1**：凡被挂载 `ask_creator` 工具的 agent，其 `interrupt_on` 必须包含 `ask_creator`。
> **INV-2**：任务状态的**每一个**写入点都必须过 `is_valid_task_transition`，包括服务端自己写的那些。
> **INV-3**：预算累加必须**原子**（SQL 侧 `SET col = col + ?`），不得 read-modify-write。
> **INV-4**：用量归集到「预算 owner」的口径必须**统一**在三处记账点（对话 / cron / team 房间）。

**INV-1 / INV-2 目前不成立**（§3.3 的 ① 和 ⑦）。**INV-3 目前必然不成立**——`team_runtime_repo` 只有 `update()`（version CAS）和 `reset_budget()`，**没有原子累加方法**。

### 2.3 Gate 通则

> **GATE-RULE**：**断言行为，不断言配置项存在。**
>
> 反例：`assert "ask_creator" in tool_names` —— 只证明工具挂上了，**不证明它能中断**。
> 正例：调 `ask_creator` → 产生 `hitl_required` 且带 request payload → `hitl_pending` 落一行 → 创建者收到帧。
>
> 现有 `tests/unit/agents/test_autonomy_mount.py:4` 的 docstring 声称「the same runtime a team host gets」却只断言工具存在——**这就是 ① 在 2019 个全绿测试下漏网的原因**。所有新 Gate 按 GATE-RULE 写。

---

## 3. 当前基线

### 3.1 已实现（工作区现状）

| 项 | 位置 | 备注 |
|---|---|---|
| S1.1 迁移 + 幂等 helper | `022_light_apps.sql:283,287-293`、`.pg.sql:282,286+`、`migrate.py:2012-2032`（post-loop `2517` + v22 分支 `2345`） | ✅ canonical DDL 齐 |
| S1.2 `AgentRow.autonomy_enabled` | `repos/agents.py:78,93-97` | ✅ 带 `KeyError` 兜底 |
| S1.3 挂载放开 + `ask_creator` | `manager.py:3815,3867,3906` | ⚠️ 引入 ① |
| S1.4 assignee 自我锁 | `board_tools.py:142,214,279` + `manager.py:3887` | ✅ `self_only=autonomous and not team_host` |
| 团队侧 | P1–P4 已落地；v2 阶段 0–3 已落地 | 驱动 0–3 阶段 4/5 未做 |

### 3.2 待实现

S1.5（task 通用 API）· S1.6（policy repo + API）· S1.7（前端）· S1.8（i18n）· P0.1–P0.4（团队侧收敛）· S2.1–S2.5（自治闭环）· S3.1–S3.6（岗位与考核）。

### 3.3 已发现问题（14 条，全部已核实）

| # | 问题 | 位置 | 级别 | 归属 |
|---|---|---|---|---|
| ① | 自治专家 `ask_creator` **静默自答**（工具挂上了但 `interrupt_on` 没设） | `manager.py:3906` / `:4313` / `:4344`、`ask_creator.py:50` | **P0** | M0.1 ✅ |
| ② | HITL 重启后 **approval 卡永远答不了**（恢复时另起 nonce，transcript 里是旧 id） | `hitl/store.py:397`、`stream_project.py:199`、`MessageBubble.tsx:689` | **P0** | M0.2 ✅ |
| ③ | `write_scope` 变 session 级永久放行 → 无人值守 cron 可发第三方邮件 | `cron/delivery.py:166,282` | **P0** | M0.3 ✅ |
| ④ | **两条越权路径**：① 板工具只查「agent 存在」不查花名册、goal 不查归属；② 决策路径 roster **读失败时静默降级**成 `member_ids=None`，把 `_known_member` 放宽成「任何 agent 都行」——并连带让 `plan_apply` 入口的校验失效 | `board_tools.py:211-220`、`decision.py:630-632` | P1 | M1.1 ✅ |
| ⑤ | `import_rows(mode="replace")` 空集**抹掉整张表** | `apps/service.py:1181-1187` | P1 | M1.2 ✅ |
| ⑥ | `HitlPendingRepo.create()` 用 `SELECT MAX(id)` 取 id —— **竞态** | `repos/hitl.py:102-107` | P1 | M1.3 ✅ |
| ⑦ | dispatcher 写 `assigned → failed`，**违反自己的状态机** | `dispatcher.py:388-396` vs `task_state.py:31` | P1 | M1.4 ✅ |
| ⑧ | goal 的 `dedupe_key` 用秒级 `updated_at` → 同秒二次编辑被吞 | `coordinator.py:447`（同文件 `:426` 的 task 用 version 是安全的） | P2 | M1.5 ✅ |
| ⑨ | 限流在验签**之前**，key 是攻击者可控的 `source_id` | `hooks.py:100` vs `:114` | P2 | M1.6 ✅ |
| ⑩ | 同一 goal 两条路由两种结构（`deliverables` 缺失） | `teams.py:372` vs `goals.py:107` | P2 | M1.7 |
| ⑪ | notify WS 每个消费者开一条连接 | `useDashboardNotifySocket.ts:50` | P2 | M1.8 ✅ |
| ⑫ | 自治专家 `peer_invoke_mode="sync"` + `ask_agent` 仍可用 → 同步阻塞且绕过 board | `manager.py:4314`、harness `team_enabled=True` | P2 | M1.9 ✅ |
| ⑬ | 服务端生成的中文硬编码（违反 AGENTS.md §7） | `events/expert_target.py:23,155` | P3 | M1.10 |
| ⑭ | `enable_team_drive` 生产不可达的遗留分支 | `drive.py:131-170` | P3 | P0.2 |

> **② 的适用范围**：只对 **approval 类卡**成立。ask 类卡 pending 时被 `MessageBubble.tsx:664` 从 transcript 隐藏、跑进 composer dock（那里拿当前 pending 列表 = 新 id）→ **不受影响**。M0.2 的 Gate 必须测 approval 路径。
>
> **测试现状**：`tests/unit/{agents,db,gateway,api}` → **2019 passed, 1 skipped**；`tests/unit/i18n` → 82 passed。**14 条一条都没被抓到**——是覆盖盲区，不是 CI 抓到的回归。

---

## 4. 阶段总表

| 阶段 | 目标 | Gate | 依赖 |
|---|---|---|---|
| **M0 止血** | 自主系统的安全边界 | ①的自问自答消失；②的 approval 卡能答；③的放行被限定 | — |
| **M1 数据正确性** | 账本可信、状态机自洽、契约唯一 | ④⑤⑥⑦⑨⑩⑫ 全修 + 每条一个**行为**测试 | M0 |
| **M2 底座通用化 + 路由收敛**（S1.5–S1.8 + **P0.5**） | 数字员工**能被人看见和干预**；**`patch_task` 只抽一次** | agent 通用看板可用；能建任务改状态；**全仓只有一份 `patch_task`** | M1 |
| **M3 团队侧收敛**（P0.1–P0.4） | 只有一套驱动机制 + 成本闸门接通 | v1 drive 创建路径不可达；预算超限产出 `defer_budget` | M2 |
| **M4 自治闭环**（S2.1–S2.5） | 数字员工**能自己醒来干活**，且不越权、不乱花钱 | self-execute 回路通；预算闸门拦得住；HITL 能挂起能续跑 | M0 M1 M3 |
| **M5 岗位与考核**（S3.1–S3.5） | **能当员工管** | policy 全字段生效；KPI 可查；收件箱可处理 | M4 |
| **M6 状态自证**（S3.6） | 状态不漂移 | 能从 `team_waits` + `team_events` 重建并对账 | M4，独立立项 |

**v4 修正**：原表把 **P0.5（路由收敛）记在 M3**、又说「M3 依赖 M2」——**同一件事记在两阶段**。现改为：**P0.5 的执行归 M2**（与 S1.5/S1.7 同批），**M3 只留 P0.1–P0.4**（去双驱动 + 成本预算）。

**排序理由**：M0 最先——一个「以为问过人了、实际没问」的自主员工比没有员工更危险。M1 在 M2 前——看板与账本要可信才配让人用。M3 在 M4 前——两套驱动并存时挂哪套都是赌，且成本闸门必须在任何自动唤醒之前。M4 在 M5 前——没有运行数据，KPI 与收件箱是空的。M6 独立立项——它属于所有主动系统。

---

## 5. M0 止血 ✅ **已实施（2026-10-28）**

> **变更**：① `manager.py` 把 `interrupt_on[ask_creator]` 从 team 分支提出来，跟工具挂载点绑定（新增 `_ask_creator_interrupt` helper）；② `hitl/store.py` 的 `load_persisted()` 复用持久化的 `pending_id`；③ `cron/delivery.py` 的写权限解锁改为 **per-job 显式 opt-in + session 批准**，`TEAM_DRIVE_META` 增加 `allow_writes`。
>
> **测试**：新增 5 个测试（`test_ask_creator_tool_and_interrupt_travel_together`、`test_autonomous_expert_can_interrupt_on_ask_creator`、`test_plain_expert_gets_no_ask_creator_interrupt`、`test_store_boot_recovery_keeps_pending_id_answerable`、`test_plain_cron_stays_write_blocked_after_a_chat_approval` + 3 个单元测试）。
> **已逐条验证**：回退任一修复 → 对应测试**必失败**（M0.1→2 fail、M0.2→1 fail、M0.3→1 fail）；恢复后 61 passed。
> **回归**：`tests/unit/{agents,gateway,cron,db,api}` → **2173 passed, 1 skipped**（修复前 2172，+1 为新增测试）；`ruff check src/octop tests` 全绿；`mypy --strict src/octop` → 644 files, no issues。

### M0.1 · `ask_creator` 的 interrupt 注册（INV-1）

**问题**：`manager.py:3906` 对 `team_host or autonomous` 挂载 `ask_creator`；但 `interrupt_on["ask_creator"]` 只在 `_apply_team_host_config` 的 team 分支里设置（`:4313` 非 team 早退，`:4344` 唯一写入点）。自治专家拿到工具却拿不到 interrupt → 工具体无条件返回 `HEADLESS_FALLBACK`（`ask_creator.py:50`）→ **静默自答**。

**改法**：把 `interrupt_on[ask_creator]` 的设置从 team 分支**提出来**，跟工具挂载点放在一起；`tools_disabled` / `team_peers` / `peer_invoke_mode=async` / `mcp_server_configs` / `skills_dir` / `tools` 这些 team-only 项**保持不动**。

**Gate（GATE-RULE）**：自治 expert 调 `ask_creator` → 图里产生 `hitl_required` 且**带 request payload** → `hitl_pending` 落一行 → 创建者收到 `approval_required` 帧。**同时验证无 request payload 的路径仍 raise**（`delivery.py:187-190`），避免把「静默」换成「卡死」。

### M0.2 · HITL 恢复复用持久化的 `pending_id`

**问题**：`store.py:397` 恢复时 `secrets.token_hex(2)` 另起 nonce；approval 卡的 `pending_id` 是旧的（`stream_project.py:199` 烤进消息文本 → `MessageBubble.tsx:689` 原样传给答复动作）。重启后看板用旧 id 答复 → `resolve_pending_for_thread` 返回 None → 路由吐 `hitl.expired`。**持久化的唯一目的被自己的 nonce 破坏。**

**改法**：`load_persisted()` 复用 `row.payload["pending_id"]`（`_record_payload` 已持久化它），仅在缺失时铸造新的。

**Gate（GATE-RULE，测 approval 路径）**：注册**工具审批** → 重启（`load_persisted`）→ 用**原始** pending_id POST resume → 审批生效（非 `hitl.expired`）。**不测 ask 路径**（ask 卡走 composer dock，不受影响）。

### M0.3 · `write_scope` 放行范围收紧 ✅ 已实施

**问题**：`delivery.py:166` 从无条件 `allowed=False` 改成 `allowed=self._session_write_approved(command.session_key)`；`_session_write_approved`（`:282`）只按 session_key 扫历史。cron 默认 session 就是该 agent 的 DM 会话 → **用户在聊天里批准过一次发信，该 agent 之后所有无人值守 cron 永久获得发信权**。

**最终规则**（两个条件都满足才放行）：

1. **该 cron job 显式 opt-in**：`command.meta.get("allow_writes")` 为真；
2. **session 里确有被批准的邮件写动作**：`_session_write_approved(session_key)`。

`TEAM_DRIVE_META`（`drive.py:28`）已加 `"allow_writes": True`，保留 §7.4-5 的 drive wake 能力。

> **为什么是 per-job opt-in 而不是「只限 team_drive」**：v2 的 drive 走 sweeper heartbeat + self-execute，**根本不是 cron run** → 「只限 team_drive」等于把 drive 能力也砍了，且把个人定时任务的合法发信一并砍掉。per-job opt-in 才是「**显式授权、逐 job 生效**」：开一个 job 不影响另一个。
>
> ⚠️ **已知后续**：`CronCreateBody`（`api/routers/cron.py:25-36`）**没有 `meta` 字段** → 用户目前无法为个人 cron 开 `allow_writes`。**默认安全**（个人 cron 不会无人值守发信），但该能力需要开 API 字段才可达。列为 M3 的跟进项。

**Gate（GATE-RULE，已实施）**：
- 端到端：普通 cron job（无 opt-in）+ session 有批准 → `writes_allowed()` 仍为 **False**
- 端到端：opt-in 的 job + session 有批准 → **True**；opt-in 但无批准 → **False**
- `TEAM_DRIVE_META["allow_writes"] is True`

---

## 6. M1 数据正确性 · **10/10 已实施（2026-10-28）**

> **M1.7** 随 M2 完成：goal/task 的 **序列化**（含 `deliverables`）与 `patch_task` 的**校验/CAS 逻辑**已全部收敛到 `api/common/team_board.py`，看板也切到了 `goalsApi`/`tasksApi`。**未做**：`teams.py` 的 goal 路由仍是自己的 handler（只共享 payload），完全转薄委托属低优先——逻辑已无二份。
>
> **每项都做了回退验证**（临时还原修复 → 对应测试必失败 → 恢复）：
> M1.1 → 3+1 failed · M1.2 → 1 failed · M1.3 → 见下方局限 · M1.4 → 1 failed · M1.5 → 1 failed · M1.6 → 1 failed · M1.8 → 2 failed · M1.9 → 1 failed
>
> **一处事实修正**：原文把 ③「`plan_apply` 完全不校验 assignee」列为独立漏洞，**核实后不成立** —— `apply_decision_plan` 在入口就调 `validate_decision_plan(..., member_ids=...)`，调用方也传了。真正的洞是 ② 让 `member_ids` 静默变 `None`，把这条校验整体降级。

| # | 改法 | Gate（GATE-RULE） |
|---|---|---|
| **M1.1**（④）✅ | 两处：① `board_tools` 新增 `_assignee_error()`（花名册 + 自治自我锁），`task_create`/`task_update` 共用；`goal_id` 增加归属校验；`manager.py` 注入 `member_ids_fn`。② `decision.py` roster 读失败 → **fail-closed**（记 `roster unavailable` 违规，决策判 failed） | 跨团队 assignee / 跨团队 goal_id / roster 抛错 → 全部拒绝。**已验证**：回退 → 3 failed（board）+ 1 failed（decision）。**注**：`plan_apply` 本就在入口调 `validate_decision_plan(..., member_ids=...)`，③ 并不成立——真正的洞是 ② 把它降级了 |
| **M1.2**（⑤）✅ | `import_rows` 的守卫改成 `if not imported_rows: raise`（不看 `clean`） | replace + 空记录 → 抛错且**表内容不变** |
| **M1.3**（⑥）✅ | `HitlPendingRepo.create` 改用仓库既有 helper `_base.insert_returning_id`（`INSERT … RETURNING id`，SQLite 3.35+/PG） | 并发 create 各自绑定自己的行。**局限**：SQLite `BEGIN IMMEDIATE` 会串行化写事务，单元测试无法稳定复现交错；竞态在 PG + 连接池下才真实存在，`RETURNING` 消除窗口本身即是保证 |
| **M1.4**（⑦）✅ | dispatcher 派发失败走 `is_valid_task_transition`：`assigned → blocked` + `blocked_reason`（`failed` 是终态，决策单元不会再碰它，与紧随其后的 escalation 事件矛盾） | dispatch 失败后任务状态**在迁移表内**（`is_valid_task_transition("assigned", status)`）；回退验证 **1 failed** |
| **M1.5**（⑧）✅ | goal 的 `dedupe_key` 改 version/纳秒 nonce | 一秒内两次改目标 → **两个事件都入账** |
| **M1.6**（⑨）✅ | `hooks.py` 把 `allow_webhook` 移到 `verify_webhook` 之后。**移动顺序同时解决两个症状**：未认证方既不能消耗真实源配额，也不能用随机 id 创建 limiter 条目（key 空间 = 真实 source 数，有界） | 验签失败 → `allow_webhook` **未被调用**；验签通过 → 调用。回退验证 **1 failed** |
| **M1.7**（⑩） | goal 路由收敛：`teams.py` 的 goal 路由转薄委托到 `goals.py`；看板切 `goalsApi` | 两条路由返回**同一结构**（含 `deliverables`）；看板与抽屉数据一致 |
| **M1.8**（⑪）✅ | `useDashboardNotifySocket` 改为**模块级单例** + `Set<handler>` 订阅者；引用计数为 0 才关闭 socket | 挂 3 个消费者 → **只有 1 条 WS**；帧扇出到全部订阅者；最后一个卸载才 close。回退验证 **2 failed**（`vitest`） |
| **M1.9**（⑫）✅ | 自治 expert：**摘掉 `ask_agent`**（或改 async + 建 wait 账本） | 自治 expert 的工具集里**没有** `ask_agent` |
| **M1.10**（⑬）✅ | 新增 `external` 命名空间（`guard` / `prompt_header` / `expert_stopped`）；`EXTERNAL_GUARD` 常量 → `external_guard(locale)`；`external_prompt` 增 `locale` 参数；`_notify_stopped` 用 `resolve_user_locale` | `tests/unit/i18n` + `tests/unit/events` → 115 passed |

> **M1.7 与 S1.5/S1.7 必须同批**（同属 M2）：两边各抽一次公共层 = 两份 `patch_task`。

---

## 7. M2 底座通用化 + 路由收敛 · **已实施（2026-10-28）**

> **落地**：`api/common/team_board.py`（共享校验 + CAS + payload）· `api/routers/tasks.py`（`GET/POST/PATCH /agents/{id}/tasks`）· `api/routers/autonomy.py`（开关 + 岗位策略）· `repos/autonomy_policy.py` · 前端 `api/modules/tasks.ts` + `/agents/:agentId/board` 路由 + 「工作台」入口。
>
> **M1.7 一并完成**：`teams.py` 的 goal/task payload 与 `patch_team_task` 全部委托到共享层，两条路由返回**同一结构**（含 `deliverables`）。
>
> **测试**：`tests/integration/test_agent_tasks_api.py`（6）· `test_autonomy_api.py`（5）· `test_teams_board_api.py` 回归（13）· `tests/unit/i18n`（82）。
>
> **自治开关 UI 已补齐**（原顺延项）：`GET /agents` payload 暴露 `autonomy_enabled` → `OctopAgent` 类型 → `api/modules/autonomy.ts` 客户端 → `AgentMoreActions` 的「聘为数字员工 / 解除聘用」菜单项（team host 不显示）。**岗位策略表单**（`active_hours` / 预算 / SLA / 职责）仍留 M5。

> S1.1–S1.4 已完成（§3.1）。

### S1.5 · task 的 agent 通用 API

**新文件**：`src/octop/api/routers/tasks.py`（与 `goals.py` 对称；**不要**新建 `agent_work.py` 装三种资源）

```
GET   /agents/{agent_id}/tasks            ← 新增
POST  /agents/{agent_id}/tasks            ← 新增（全仓不存在 POST task 路由）
PATCH /agents/{agent_id}/tasks/{task_id}  ← 新增
```

**三条铁律**

1. **状态机逻辑只许一份**：`patch_task` 主体（状态机校验、approval 冲突检查、CAS、终态结算 wait、WS 推送、事件记录）抽成共享实现（`api/common/team_board.py`），`tasks.py` 与 `teams.py` 都调它。
2. **鉴权**用 kind 无关的 `require_agent_owner_row`（`api/common/agent.py:68-77`）。
3. **team 专属副作用**用 `if is_team_agent(row):` 收口——照抄 `goals.py:242-244`。

**`POST` 的规则**：`title` / `spec` / `goal_id` / `assignee` / `priority` / `due_at`；assignee 必须本团队（或自治专家=自己）；goal 必须本团队；尊重 `TEAM_DRIVE_OPEN_TASK_LIMIT`；写入后推 `team.task.updated` 帧 + 记事件。**这条关掉 `team-l3-roadmap.md:48` 的缺口。**

### S1.6 · 自治开关与岗位策略 API

**新文件**：`src/octop/api/routers/autonomy.py` + `infra/db/repos/autonomy_policy.py`

```
PUT  /agents/{agent_id}/autonomy            ← 写 agents.autonomy_enabled
GET  /agents/{agent_id}/autonomy-policy
PUT  /agents/{agent_id}/autonomy-policy
```

**三条必须定义的语义（v4 补）**

1. **关闭自治（`enabled: true → false`）**：复用 `TeamAutopilot.stop()` 的语义——停 wake、取消 open task、清 open wait、`team_runtime` 置 `stopped`。**不能只改一个布尔值**（否则留下「员工还在干活但没人承认他」的状态）。
2. **拒绝给 `kind='team'` 的 agent 设 autonomy**：team 已有自己的 autopilot 开关，两套语义叠加会歧义。API 层显式 400。
3. **首次上岗路径**：`autonomy_enabled` 打开但**没有 active goal** 时，第一跳 wake 必须给出明确引导（「你还没有目标，先 `goal_set`」），而不是空转。引导内容用 `responsible_scope` + 「尚未设定目标」两种文案分支。

### S1.7 · 前端

- 新 `dashboard/src/api/modules/tasks.ts`（镜像 `goals.ts` 形状）
- 路由加 `/agents/:agentId/board`；组件兼容 `agentId ?? teamId`
- **看板切到 `goalsApi` + `tasksApi`**（M1.7 同批）→ `teams.ts` 的旧 goal/task 客户端变无引用 → 删除（属本 slice 造成的后续清理）
- 自治专家卡片加「工作台」菜单项 + **开关**（含关闭确认）
- WS：`useTeamTaskEvents` 的帧对自治专家同样会发，无需改

### S1.8 · i18n

新增 `agents.autonomy.*`（开关/标题/工作台/策略表单/首次引导文案）；`teams.board.*` 直接复用。

### Gate

- agent 通用看板可用（team 与自治专家同一页渲染）；**全仓只有一份 `patch_task`**
- 能建任务、改状态、非法流转 400、版本冲突 409、越权 403
- 关闭自治后：wake 停、open task 被取消、`team_runtime` 变 `stopped`
- **前端**：`cd dashboard && npx tsc -b && npm run lint` **+ `make build-frontend`**（pre-commit 会跑 `npm run build`，产物 `src/octop/dashboard/` 不可手改）
- **跨平台**：M2 涉及持久化的新测试必须遵守 AGENTS.md §7（`monkeypatch.setenv("OCTOP_HOME", str(tmp_path))`、`Path` 断言、`posix_only`），Windows CI 会跑

---

## 8. M3 团队侧收敛 · **全部完成（2026-10-28/29）**

> **P0.2** 移除 v1 创建路径：`enable_team_drive` / `drive_template` / `TEAM_DRIVE_META` 全部删除，`drive_enable/disable/list` 在无 autopilot 时返回错误而不是回落 cron。
> **P0.3** `dispatch_ledger` / `sleep_until` 早已不存在；**保留** `_prepare_team_wake`（存量 v1 cron 行在迁移前的 stale 回收安全网）。
> **P0.4d** 月窗口：`reset_budget(day=, month=)` 按 policy 的 `budget_window` 分别滚动，`team_runtime.budget_day` 存当前周期键（`YYYY-MM-DD` 或 `YYYY-MM`）。
> **P0.6** `CronCreateBody.meta` 上线，但**只放行 `allow_writes`**——其他 key 不透传，避免客户端伪造 drive kind / wake 标记。
>
> **测试**：`test_team_drive.py` 改为 v2-only（7）· `test_team_budget_gate.py` 新增月/日窗口（2）· `test_cron_api.py` 新增 meta 白名单（2）。**全部回退验证**（月窗口 → 1 failed；meta 白名单 → 1 failed）。

| # | 步骤 | 位置 |
|---|---|---|
| P0.1 | `drive_enable` 改写 `team_runtime`（`mode=autopilot`），不再建 cron 行 | `drive.py:131-170` → `runtime.py:150-161` |
| P0.2 ✅ | 移除 v1 的**创建**路径（`enable_team_drive` / `drive_template` / `TEAM_DRIVE_META`），**保留** `is_team_drive_job` / `stop_team_drive` 用于迁移与 kill switch | `drive.py`、`drive_tools.py` |
| P0.3 ✅ | `dispatch_ledger` / `sleep_until` 已不存在；`_prepare_team_wake` **保留**（存量 v1 cron 行在迁移窗口内的 stale 回收） | `team-drive-v2.md` §11.1 第 5 条 |
| **P0.4** | **成本预算闸门**（v2 阶段 4 治理项）——见 §8.1–8.3 | |
| **P0.6** ✅ | **`CronCreateBody` 补 `meta`**，只放行 `allow_writes`（其他 key 丢弃） | `api/routers/cron.py` |

### 8.1 P0.4 的归属

成本预算被四份文档同时规划（`team-autopilot` A3/D22、`team-drive-v2` 阶段 4、`team-l3-roadmap` A1、`proactive-expert` C2）。**做两次 = 两个预算字段 + 两套闸门 = 第三次返工。** 归属定在 v2 阶段 4。

### 8.2 粒度：agent / team 级（**D10，已拍板**）

**为什么不是 task 级**：记账点在 harness 层，回合结束时只有 `agent_id / thread_id`，**没有 task 上下文**：

```python
# infra/cron/delivery.py:207-217
record_turn_usage(repo, agent_id=..., user_id=..., thread_id=..., usage=..., source=..., team_id=...)
# infra/gateway/process/processor.py:1767-1774
record_turn_usage(repo, agent_id=..., user_id=..., thread_id=..., usage=...,
                   team_id=(agent_id if is_team_agent(row) else None))
```

task_id 只存在于调用方（dispatcher 的 wait、self-execute 的单元）手里。**要做 task 粒度得引入上下文传递（ContextVar / session metadata），改动面大、收益是精细化而非止损。** v1 先做 agent/team 级。

**好消息：team 级已经基本就绪。** `team_manager.py:405-435` 的 `_record_team_turn_usage` **已经把成员回合归到 team**（`team_id=team_id`，`source="team_member"` / `"team_host"`）。

### 8.3 P0.4 的四个具体改动 · **a/b/c 已实施（2026-10-28）**

| # | 做什么 | 位置 | 说明 |
|---|---|---|---|
| a ✅ | **统一「预算 owner」口径**（INV-4）：`teams/service.py` 新增 `usage_owner_id(row)`——team host 与 **autonomy_enabled 的 agent** 都返回自己的 agent_id，其余 None | 三处记账点已统一：`processor.py`、`cron/delivery.py`、`team_manager.py`（后者显式传 team_id） | ✅ 测试 `test_usage_owner.py`（4） |
| b ✅ | **原子累加**（INV-3）：`TeamRuntimeRepo.add_budget_used(team_id, tokens)` → `UPDATE ... SET budget_used = budget_used + ?`；由 `record_turn_usage(..., runtime_repo=, team_id=)` 在记账后调用 | `repos/team_runtime.py` | ✅ 回退验证：不累加 → 1 failed |
| c ✅ | `budget_remaining` 接真值：新增 `dispatcher.budget_remaining(repos, team_id)`（limit 来自 `autonomy_policy.budget_limit_tokens`，0 = 不限）；**`dispatch_ready` 现在真的把它传给 `plan_dispatches`**，决策快照也用它 | `dispatcher.py`、`decision.py` | ✅ 回退验证：恒 `None` → 2 failed | 闸门生效后：预算不足时产出 `defer_budget` 步而非派发 |
| d ✅ | `budget_window`（日/月）切换与清零：`reset_budget(day=, month=)` 按 policy 分别滚动 | `repos/team_runtime.py` | — |

> ⚠️ **b 最容易被漏**：只加列不改累加、或者用 CAS 读改写，闸门依然是死的。
>
> ✅ **已落地的行为**：`test_team_budget_gate.py`（5）证明——无 policy → 不限；limit-used 计算正确；**耗尽时 `plan_dispatches` 产出 `defer_budget` 而非 `dispatch`**；有余量时正常派发。
>
> ⚠️ **闸门是滞后的**：`budget_used` 在回合**结束后**才累加，闸门拦的是「下一轮」不是「这一轮」。单轮可超支——v1 接受此语义，但**必须写进文档**（不能当 bug 报）。
>
> ⚠️ **open tasks ≤10 是数量闸门，不是成本闸门**，不能当预算已做。

### 8.4 Gate

- `meta.kind=team_drive` 的 cron **创建**路径全仓不可达
- 预算超限时 `plan_dispatches` 产出 `defer_budget` 步而非继续派发
- **并发上报不丢账**：两个并发用量写入后 `budget_used` == 两者之和
- 自治员工的用量能归到自己的 `team_runtime` 行

---

## 9. M4 自治闭环（S2.1 – S2.6）· **已实施（2026-10-28）**

> **落地**：`TeamAutopilot.submit_peer` 在 `assignee == team_id` 时分流到 `_submit_self`（`runtime.py`）——harness 硬禁自派发，所以数字员工跑自己一个回合；回合结束按成员回叫路径 `record_member_reply` + `handle_member_reply` 回报，结算同一条 dispatch wait。
>
> **配套**：`solo_gate` 让 coordinator 跳过 decision unit（`coordinator.py` + `gateway.py` 接线）· `agents.employee_briefing` 注入（中英）· `self_only` 下 `task_create` 默认 `assignee=self`（否则 dispatcher 判 `no assignee` 永远 escalate）· `PUT /autonomy` 开启时调 `autopilot.start()`（否则有工具但永不醒来）。
>
> **回归（2026-10-29）**：`tests/unit` → **4764 passed, 3 skipped**；`tests/integration` → **686 passed, 18 skipped**；`mypy --strict` 649 files 无问题；`ruff` 全绿；dashboard `tsc -b` + `vite build` 成功。
>
> **测试**：`test_team_self_execute.py`（3，含 HITL 暂停不关任务）· `test_team_coordinator_autopilot.py` 新增 solo gate（1）· `test_autonomy_mount.py` briefing（2）· `test_team_board_tools.py` 默认 assignee（1）。**全部回退验证**（self-execute → 3 failed；solo gate → 1 failed；briefing → 1 failed）。

| # | 步骤 | 说明 |
|---|---|---|
| S2.1 | 把「执行单元」从 dispatcher 抽成接口，两种实现平级 | `MemberDispatchExecute`（现有）/ `SelfExecute`（新） |
| **S2.2** ✅ | **SelfExecute**：`submit_peer` 分流 → `_submit_self` → `agent_manager.stream` 跑一回合 → 回叫。**用分流而非新建接口类**，比原计划更小的改动 | 与 cron delivery 同一入口，不需要 per-team cron、不需要新 harness 能力 |
| S2.3 ✅ | 触发路径接 v2 heartbeat（`PUT /autonomy` 开启即 `autopilot.start`） | `sweeper._sweep_heartbeats`（`sweeper.py:245-261`）已在按 `next_heartbeat_at` kick；solo 复用同一 heartbeat，只是 kick 后走 self-execute |
| S2.4 ✅ | 自治 prompt 注入（`teams.employee_briefing`） | `manager.py:3994-3998` 同款钩子；注入 `responsible_scope` + 板工具指引 |
| S2.5 ✅ | solo **跳过 decision unit**（`solo_gate`） | wake 回合里自己读板、拆任务、执行、回写 |
| **S2.6** | **self-execute 回合的用量记账接入预算 owner** | self-execute 走 `agent_manager.stream()` → 普通对话路径（`processor.py`），**不走 cron delivery** ⇒ P0.4a 的三处记账点必须覆盖它，否则自治员工的预算**永远记不上** |

> **S2.2 为什么不用 cron**：v2 的立意就是消除 per-team cron。`TeamAutopilot` 已持有 `self._agent_manager`，sweeper 是全局的，heartbeat 已存在——**self-execute 只需要一个「跑一回合」的调用，不需要新调度器**。
>
> ⚠️ **S2.6 是 v4 新增的接口缺口**：P0.4 与 M4 之间的记账路径必须对齐，否则「预算闸门」对数字员工形同虚设。

### Gate

- sweeper heartbeat → solo agent 跑一个回合 → 板上有新任务/状态变更 → 事件入账
- 预算超限 → 观察到 `defer_budget` 而不是继续派发；**且 solo 的用量确实计入 `budget_used`**
- 无人值守触发审批 → 任务进 `needs_input`、创建者收到通知、**答复后任务回到 `running`**（依赖 M0.2）
- 重启后 in-flight 任务被正确回收（`sweeper.recover_inflight()`）

---

## 10. M5 岗位与考核（S3.1 – S3.5）· **S3.1 / S3.2 / S3.3 / S3.4 已实施**

> **已生效**：`responsible_scope` 注入 system prompt（`teams.employee_scope`）· `tool_scope_allow/deny` 真正裁剪工具（deny 优先；allow 存在时禁用所有未列出的 builtin）。
>
> **S3.4 独立验收已落地**：`team_task_reviews` 表（迁移 023 + 幂等 helper）· `task_state` 加 **`review → proposed`** 返工边 · `POST /agents/{id}/tasks/{task_id}/review`（approve → `done`，reject → `proposed`，记录 reviewer/comment）· `GET .../reviews`。**每一条验收现在有 who/why/when**。
>
> **S3.2 KPI** ✅ `GET /agents/{id}/metrics`：任务按状态计数 + `open` · `usage_log` 按窗口聚合 · 预算 used/limit/remaining。
> **S3.3 异常收件箱** ✅ `GET /agents/{id}/inbox`：待审批 / 失败 / 超期 / 预算耗尽，附 counts。
> **`active_hours` 值班窗** ✅ sweeper 的 heartbeat kick 受 duty window 约束。
>
> ⚠️ **一处设计修正**：`AutonomyPolicy` 的默认值就是 `09:00-22:00`，所以「无 policy」不等于「无窗口」。初版实现会让**所有团队**（含未配置策略的）在夜间被静默停止唤醒。改为 `get_optional()`——**只有显式配置了 policy 的 agent 才受窗口约束**。
>
> **未做，且必须如实标注**：
> - `escalate_to` **只存不读**（全仓除迁移与 API 序列化外无消费者）。它需要改 HITL 的定向推送目标，与「成员 HITL 在会话卡片答复」的现有语义交叉，属独立设计项。
> - **策略表单 UI** —— 所有 policy 字段目前只能通过 `PUT /agents/{id}/autonomy-policy` 设置。
> - **编制视图** 部分可用：`GET /agents?autonomy=true` 返回在岗数字员工（该参数在 `mine` 与 `all` 两个分支都生效）。**团队 roster 仍只在 host workspace 的 `.octop/manifest.json`**，不可 SQL 查询——这部分未做。
>
> ⚠️ **一处实现教训**：首次实现 `tool_scope` 时用了变量名 `policy`，覆盖了同函数里已有的 agent policy，导致 9 个测试失败。改名 `spec` / `autonomy_policy_repo` 后恢复——测试抓住了这个冲突。

### S3.1 `autonomy_policy` 全字段生效

| 字段 | 默认 | 生效内容 |
|---|---|---|
| `enabled` | `false` | 自治总开关 |
| `active_hours_start` / `_end` | `09:00` / `22:00` | 值班窗口 |
| `min_interval_hours` / `max_interval_hours` | `5` / `24` | 心跳间隔 / 频控 |
| `budget_limit_tokens` | `0`（不限） | 周期预算上限 |
| `budget_window` | `day` | 预算周期（日 / 月） |
| `sla_tasks_per_day` | `0`（不限） | 产能 SLA |
| `escalate_to` | `""` | 汇报对象 |
| `responsible_scope` | `""` | 职责边界（注入 prompt） |
| `tool_scope_allow` / `_deny` | `""` | 权限范围（真正裁剪工具） |

### S3.2 KPI · S3.3 异常收件箱 · S3.4 独立验收 · S3.5 编制视图

- S3.2 `GET /agents/{id}/metrics`；指标源 = `usage_log` + `team_tasks` 统计（L3 A3/A4）
- S3.3 `GET /agents/{id}/inbox`——「待你 N 条」（待审批 / 已失败 / 超期 / 预算停摆）（L3 A3）
- **S3.4** ✅ `team_task_reviews` 表 + `POST .../review` + `GET .../reviews`；打回走 **`review → proposed`**。
  ✅ 已在 `ALLOWED_TASK_TRANSITIONS["review"]` 里加 `proposed`。
  ⚠️ 这**不违反** §0.2 —— §0.2 禁的是「合并/重命名**状态集合**」，**加边是允许的**（`team-l3-roadmap.md` A0 明确要求这条边）。
- S3.5 roster 出 `agents` 表（现在只在 workspace manifest，不可查询）

---

## 11. M6 状态自证（S3.6，独立立项）

从 `team_waits` + `team_events` **重建** `team_tasks.status` / `team_runtime.status` 的纯函数 + 对账 job。

**为什么必须有**：Octop **没有任何 reconcile / rebuild 代码**（grep `rebuild|recompute|reconcile|repair|drift` → 0 命中），而
- `coordinator.record()` 故意吞掉存储错误（`:173-175`）
- `coordinator._set_status()` 吞掉 CAS 失败（`:353-354`）
- `TeamAutopilot._update()` 重试一次后静默返回旧行（`:425-436`）

**主动系统里没人盯，缓存漂移 = 永久性故障。** 数字员工是第一个「没人天天盯」的消费者，会最先暴露它。

---

## 12. 验证矩阵

> **通则（GATE-RULE）**：**断言行为，不断言配置项存在。** 每条 Gate 都要能被「行为缺失」判失败。

| 阶段 | 用例 | 命令 |
|---|---|---|
| M0 | ① 自问自答消失（**产生 hitl_required 且带 request**）· ② **approval 卡**重启后能答 · ③ 非 drive cron 不解锁写 | `tests/unit/agents/test_autonomy_mount.py`、`tests/unit/gateway/test_hitl_channel.py`、`tests/unit/cron` |
| M1 | ④–⑬ 每条一个**行为**测试（见 §6 的 Gate 列） | `tests/unit/{agents,db,gateway,api}` |
| M2 | agent 通用看板 / 建任务 / 状态流转 / 越权 / **关掉自治的清理** / **全仓一份 `patch_task`** | 新 `tests/integration/test_agent_tasks_api.py` |
| M3 | v1 drive 不可达 / 预算超限 `defer_budget` / **并发不丢账** / 自治员工用量归集 | `tests/unit/agents/test_team_*`、`tests/unit/db` |
| M4 | heartbeat→self-execute→板变更→事件 / 预算（含 solo 计入）/ HITL / 重启回收 | `tests/unit/agents/test_team_autopilot.py` 等 |
| M5 | policy 生效 / KPI 可查 / 收件箱可处理 / `review → proposed` 可走通 | 新 `tests/integration/test_autonomy_policy.py` |
| 全量 | ship bar | `make all` |
| 前端 | 类型 + lint + **构建** | `cd dashboard && npx tsc -b && npm run lint` + `make build-frontend` |
| i18n | 前后端键对齐 | `uv run pytest tests/unit/i18n -q` |

---

## 13. 风险登记

| 风险 | 阶段 | 缓解 |
|---|---|---|
| 数字员工把任务甩给别的 agent（无派工语义 → 永久悬置） | M1 | M1.1 三处越权路径 + M1.9 摘 `ask_agent` |
| 自治员工「以为问过人了」 | M0 | M0.1 INV-1 |
| 无人值守 cron 越权发信 | M0 ✅ | M0.3 per-job 显式 opt-in + session 批准 |
| 双驱动并存导致挂错运行时 | M3 | P0.1/P0.2 |
| 成本闸门做两遍 | M3 | P0.4 唯一归属 v2 阶段 4 |
| `patch_task` 做两遍 | M2 | M1.7 + S1.5/S1.7 同批 |
| **预算累加丢账（并发）** | M3 | **INV-3 原子方法**（P0.4b） |
| **自治员工预算记不上** | M4 | **S2.6 记账路径对齐** |
| **关闭自治留下悬空任务** | M2 | S1.6 关闭语义复用 `TeamAutopilot.stop()` |
| 状态漂移不可检测 | M6 | S3.6 |
| 通用 API 越权访问他人 agent 的任务 | M2 | `require_agent_owner_row` + admin bypass |
| 迁移在旧库上丢列（开关永远为关且静默） | M2 | S1.1 helper 挂 post-loop 永久块 |

**必须保留的人工闸门（不可 bypass）**：kill switch · `write_scope` 禁写默认 · active hours 默认窗 · 工具审批 mandatory 清单 · `ask_creator` 升级通道。
**自治员工的「自由度」必须小于对话态员工——无人值守是更严的上下文，不是更松的。**

---

## 14. 明确不做

见 §0.2。补充：

| 不做 | 理由 |
|---|---|
| 在 M4 之前接 cron 自动唤醒 | 需先有预算闸门（P0.4）与 HITL 正确性（M0） |
| 在 M5 之前加 KPI / 收件箱 | 依赖 M4 的运行数据 |
| 为数字员工单独建一套任务表 / 看板 / 预算 | 会分裂成两套真相 |
| 让数字员工复用 `TeamDispatcher.submit_peer` | harness 硬禁自派发 |
| **task 粒度的预算归集** | D10：记账点拿不到 task 上下文 |

---

## 15. 一句话总结

> **先止血，再修账本，然后才让它自己醒。**
>
> M0 三处改动让「自主」不等于「失控」；M1 让任务账本可信；M2 让数字员工拥有看板并把路由只抽一次；M3 把团队收敛成一套机制并接通成本闸门；M4 让它自己醒来干活且不越权不乱花钱；M5 让它像员工一样被管；M6 让状态不漂移。
>
> 而所有这些的**共同地基**是 `team_tasks` + `task_state.py` 这套**本来就没有 kind 约束**的底座——数字员工不是新角色，是同一套运行时上的一个新 profile。

---

## 16. 附录：待验证假设

| # | 假设 | 何时 | 备注 |
|---|---|---|---|
| V1 | 板工具挂到 expert 后 `current_tool_scope()` 能返回该 expert 的 agent_id | M2 | team 路径已验证，**expert 路径未验证** |
| V2 | 自治专家的 `goal_set` 创建的 `team_runtime` 行不会被 sweeper 误当 autopilot team 拾取 | M2 | `mode` 默认 `manual`，预期安全，未跑过 |
| V3 | `goal_set` 写 `runtime.goal_id` 时不会与 `expected_version` 竞态 | M2 | — |
| V4 | 通用 API 路由不与既有路由撞车 | ✅ 已查 | goal 侧已通用；task 侧无冲突 |
| V5 | `proactive_care_config` 加列后既有 `SELECT`（显式列名）不受影响 | M2 | — |
| V6 | `agents` 加列后无按位置下标读该表的路径 | M2 | `from_row` 按列名取值 |
| V7 | `agent_manager.stream(agent_id, request)` 可用于自执行回合 | M4 | 与 `cron/delivery.py:167` 同入口 |
| V8 | `ask_creator` 的 respond-only interrupt 是否带 request payload | M0 | **M0.1 的 Gate 会直接测出** |
| V9 | v1 drive cron 行的存量规模（决定 P0.2 保留读取多久） | M3 | — |
| **V10** | **`usage_owner_id` 统一后，自治员工的每一笔用量都能归到自己的 `team_runtime` 行**（含 self-execute 路径） | M3/M4 | **P0.4a + S2.6 的联合 Gate**；未验证 |
| **V11** | **agent/team 级预算足以拦住「烧钱」**（粗粒度够用） | M3 | v1 决策，事后用 M5 的 KPI 复盘；若不够再上 task 粒度 |
