# Octop 架构总览：现状、缺口与路径

Status: reference
Date: 2026-11-08
Scope: 面向决策者的全景文档——Octop 现在是什么、能干什么、边界在哪、接下来该补什么。
技术细节见 `docs/architecture.md`（英文，168 行）；模块边界与改动规则见 `AGENTS.md` §5；
本文不重复它们。

> **数字说明**：本文的模块规模、表数量、通道数均为 2026-11-08 在本仓库实测，
> 会随迭代变化。引用时以代码为准。

---

## 0. 三句话

1. **Octop 是一个成熟的「单节点多 Agent 平台」**，强在工程完备（多用户、10 个 IM 通道、
   自托管运维全套），弱在控制面（准入、授权、成本、审计没有统一抽象）。
2. **Super-agent 借鉴补的正是控制面**——但设计文档里只有「方案 A」和「带边界的目标」
   是当前真需求，工单/直通模型属于第二阶段储备。
3. **分布式不是 Octop 的演进方向**。正确形态是「每节点一个 Octop + 外部协调层」，
   而不是把 `SharedServices` 改成分布式。

---

## 1. 一句话定位与规模

**一个单进程、自包含、多用户的多 Agent 平台**：`pip install` 之后
`octop init && octop run` 即可运行，除一个 LLM provider 外不依赖任何外部服务。

| 维度 | 规模（2026-11-08 实测） |
|---|---|
| `infra/` Python | 约 15 万行，23 个子包 |
| API 路由模块 | 66 |
| CLI 命令模块 | 24 |
| 数据库表 | 50（SQLite / PostgreSQL 共用一套 schema） |
| 连接器 | 30 |
| 专家模板 | 26 |
| IM 通道 | 10（`dingtalk` `discord` `feishu` `mqtt` `qq` `telegram` `wecom` `weixin` `xiaoyi` `yuanbao`） |
| Dashboard 页面 | 14 |

**最大的子包**（`src/octop/infra/`）：

| 子包 | 文件 | 行数 | 职责 |
|---|---:|---:|---|
| `agents/` | 188 | 51,548 | Agent 运行时、专家、团队、插件、记忆、provider |
| `gateway/` | 59 | 14,274 | IM 消息处理、HITL、slash、steering |
| `db/` | 51 | 13,380 | 连接池、迁移、repos、SharedServices |
| `connectors/` | 48 | 9,843 | 目录、OAuth、MCP gateway、凭据加密 |
| `apps/` | 16 | 7,450 | **MiniApp** |
| `utils/` | 34 | 4,838 | 纯工具（路径、ULID、locale…） |
| `bridge/` | 10 | 3,778 | Octop ↔ Octop 实例桥 |
| `auth/` | 26 | 3,585 | OIDC / LDAP / SSO |
| `skills/` | 12 | 3,337 | 技能包与 SkillHub |
| `setup/` | 15 | 3,331 | 首启向导、TLS、服务安装 |
| `history/` | 17 | 2,683 | 版本化历史、轨迹、投影 |
| `backup/` | 9 | 2,396 | 手动与自动备份 |
| `knowledge/` | 17 | 2,325 | 知识库 |
| `users/` | 11 | 2,292 | 用户、角色、密码 |
| `backend/` | 12 | 2,091 | 工作区存储适配（本地/远程/COS/S3/Docker） |
| `desktop/` | 5 | 1,919 | 远程桌面 |
| `mobile/` | 10 | 1,885 | Android adb 探测与远控 |
| `events/` | 8 | 1,811 | 外部事件平面 |
| `cron/` | 7 | 1,592 | 定时任务 |
| `voice/` | 5 | 1,113 | STT / TTS |
| `proactive/` | 4 | 913 | 主动照护 |
| `browser/` | 2 | 550 | Playwright 会话 |

---

## 2. 架构骨架

```
                    ┌──────────────┐  ┌──────────────┐
                    │  dashboard/  │  │     cli/     │
                    │   (React)    │  │   (Click)    │
                    └──────┬───────┘  └──────┬───────┘
                           │ HTTP            │
                    ┌──────▼─────────────────▼──────┐
                    │        api/  (66 routers)      │
                    │  FastAPI · JWT · SSE · WS      │
                    └──────────────┬────────────────┘
                                   │
                    ┌──────────────▼────────────────┐
                    │        launch.py               │
                    │  OctopServer + build_app       │
                    └──────────────┬────────────────┘
                                   │
        ┌──────────────────────────▼───────────────────────────┐
        │                    infra/  (domain)                   │
        │  agents · gateway · db · connectors · apps · bridge   │
        └──────────────────────────┬───────────────────────────┘
                                   │
        ┌──────────────────────────▼───────────────────────────┐
        │   infra/db/repos  (只剩 SQL)     infra/utils (纯函数)  │
        │   SQLite ⇄ PostgreSQL 同一套 schema                   │
        └───────────────────────────────────────────────────────┘

   外部运行时（独立 PyPI 包，Octop 不实现）：
   octop-harness (LangGraph agent loop) · octop-gateway (IM 通道) · octop-memory
```

### 五项硬约束

来自 `AGENTS.md` §5 与 `docs/adr/001-single-process-model.md`：

| # | 约束 | 含义 |
|---|---|---|
| 1 | **依赖向内** | `api/` → `infra/` → `db/`、`utils/`；`infra/` 不得 import `api/`、`cli/`、`launch.py` |
| 2 | **repo 只做 SQL** | `infra/db/repos/` 不 import 其他 `infra` 包 |
| 3 | **单进程** | 无外部队列、无 worker 进程；`SharedServices` 进程全局 |
| 4 | **双 ID** | 资源表用整数 `id` 作 PK、字符串 `{entity}_id` 作对外标识 |
| 5 | **异步全链路** | 阻塞调用必须 `run_in_executor` |

---

## 3. 功能全景

### 3.1 Agent 运行时

生命周期（创建/启动/停止/重载）、工作区隔离（本地/远程/COS/S3/Docker 沙箱）、
多 provider + 本地 Ollama、MCP 工具挂载、插件工具、技能、子代理（`ask_agent`）、
人格（MBTI + SOUL.md）、记忆与压缩、SecurityPolicy 与 PII 脱敏。

### 3.2 多用户与认证

JWT、邀请码、OIDC SSO、LDAP；管理员/普通用户；资源级 ACL
（agent / MiniApp / 知识库 / 频道）；`audit_log` 记录**管理动作**。

### 3.3 通道

10 个 IM 通道（见 §1），外加 Dashboard、CLI、MCP、ACP、Bridge。
第三方插件可通过 `src/octop/infra/gateway/plugin_channels.py` 注册新通道，
与内置通道同等待遇。

### 3.4 会话与历史

`threads` + `thread_messages` 投影；可选的分段归档（`history_v2.sqlite`，默认关闭）；
完整执行轨迹 `trajectory_events`；上下文压缩与记忆 slim。

### 3.5 团队与自主

| 能力 | 实现 |
|---|---|
| 目标 | `team_goals` + `goal_deliverables` |
| 任务 | `team_tasks`（9 态）+ 依赖 + 优先级 + 截止 |
| 编排 | `TeamCoordinator` + `TeamDispatcher` + `DecisionRunner` |
| 自主 | `TeamAutopilot` + `TeamSweeper`（60 秒心跳/超时/预算） |
| 预算 | `budget_remaining` 按日 token 限额，**已生效**（`infra/agents/teams/dispatcher.py:77`） |
| 审批 | `hitl_pending` 持久化 + 频道内审批 |
| 收件箱 | `GET /api/agents/{id}/inbox` |

### 3.6 外部事件

webhook（HMAC）+ connector_poll（30 秒扫描）；订阅支持过滤/模板/合并窗口/限流；
目标为专家 turn / 团队 judge / MiniApp 刷新；
投递走 DB outbox + 条件 UPDATE 租约 + 指数退避 + 死信；保留 30 天。
详见 `docs/external-events-design.md`。

### 3.7 MiniApp

数据（schema + rows，JSON 列值，CAS 版本，2 万行上限）、视图
（dashboard / ledger / board，自包含 HTML）、生成（模板 / 无 LLM 快速模式 / LLM）、
数据源（CSV 导入 + MCP 快照同步）、只读发布链接（token hash，可撤销/过期）、
应用内只读问答助手、外部游戏接入、19 个 `app_*` agent 工具。

**定位见 §7。**

### 3.8 技能 · 插件 · 连接器

技能包与 SkillHub 市场；20+ 内置插件 + 上传安装；30 个连接器；
外部 MCP server 挂载 + 内部 MCP gateway。

### 3.9 运维与接入

首启向导、Let's Encrypt 自动签发、systemd 安装、手动/自动备份、在线更新、
Android adb 远控、远程桌面、Playwright 浏览器、STT/TTS、知识库、
zh/en 双语、metrics + usage + observability。

---

## 4. 架构特点

### 特点 1：单进程自包含，零外部依赖

`docs/adr/001-single-process-model.md` 明确选择：不引入 Redis / RabbitMQ / Celery，
`OctopServer.start()` 从 SQLite 重建整棵运行时树。

- **收益**：部署成本极低；重启语义简单（无外部状态要对账）
- **代价**：只能纵向扩展；重 CPU 任务阻塞事件循环；**没有跨节点能力**

### 特点 2：单向下行的分层依赖

`dashboard → api → infra → db/utils`，且 `infra/` 永不 import `api/`。
这条规则在 `AGENTS.md` §5 里有硬性约束和反例清单，是本仓库最稳定的架构资产。

### 特点 3：一套 schema，两种数据库

SQLite（默认，WAL）与 PostgreSQL 共用 `00N.sql` / `00N.pg.sql` 迁移对，
版本断言在 `tests/unit/db/test_db_pool.py:113`（当前 v29）。

- **收益**：小机器开箱即用，大客户可迁 PG。**这是分布式的物理前提**——
  中心节点用 PG 是现成的。
- **代价**：SQLite 不支持的重建要写幂等助手（`migrate.py` 已有 20+ 个 `_ensure_*`）

### 特点 4：资源表双 ID

整数 `id` 作主键，字符串 `{entity}_id`（ULID）作对外标识，子表 FK 指向字符串 ID。

**让"换数据库"和"导出/导入"都不破坏外部引用。**

### 特点 5：异步全链路，单进程撑多用户

LLM 调用是 I/O 密集，asyncio 天然适配。`SharedServices` 进程全局，
因为进程拥有全部状态——这也是它无法分布式的根因。

### 特点 6：运行时外置（最被低估的特点）

`octop-harness`（agent loop / 工具 / 记忆 / 浏览器）、`octop-gateway`（IM）、
`octop-memory` 都是独立 PyPI 包。Octop 只做平台层。

**意义**：理论上可以换掉 agent runtime 而不动平台；Octop 不必背模型适配的债。

### 特点 7：通道插件化

内置 10 个通道在 `octop-gateway`；Octop 侧通过 `plugin_channels.py`
让第三方插件注册新通道。通道与平台通过 `ThreadRegistry` 的会话键解耦。

### 特点 8：先落库，再执行

外部事件投递、HITL 审批、cron 运行都是"状态先持久化，动作后执行"，
崩溃后靠数据库状态恢复，不靠内存。

**已知的两个例外**（见 §8 的 B1/B2）：照护调度器异常后不重排；
外部投递租约过期后盲翻重跑。

### 特点 9：历史与投影分离

`thread_messages` / `thread_history_projection` 是读模型，
`history_v2.sqlite` 是可选归档，`trajectory_events` 是完整轨迹。读写分离，归档可开关。

### 特点 10：运维内建

Setup 向导、TLS 自动签发、systemd 服务、备份、在线更新、i18n——
这些在多数开源 Agent 项目里缺失，Octop 把它们当作产品的一部分。

---

## 5. 能力矩阵

| 域 | 强度 | 说明 |
|---|---|---|
| Agent 运行时 | ★★★★★ | 成熟；多后端、多模型、工具生态完整 |
| 多用户与认证 | ★★★★★ | JWT / SSO / LDAP / 邀请码 / 角色 / ACL 齐全 |
| 通道接入 | ★★★★☆ | 10 个 IM + 插件机制；企微只有扫码绑定 |
| 团队编排 | ★★★★☆ | 目标/任务/派发/审批/预算齐全；缺独立验收人 |
| 外部事件 | ★★★★☆ | inbox/outbox/租约/退避/死信完整；缺语义级过滤 |
| MiniApp | ★★★★☆ | 数据+视图+发布+数据源+助手完整；2 万行上限 |
| 技能/插件/连接器 | ★★★★☆ | 30 连接器 + 市场 + 上传 |
| 运维自托管 | ★★★★☆ | setup / TLS / backup / update 齐全 |
| 跨实例 | ★★★☆☆ | **Bridge 已实现**（见 §6） |
| 主动工作 | ★★☆☆☆ | 三条路径并存，无统一决策点，跑在人类 DM 线程 |
| 成本治理 | ★★☆☆☆ | 有硬顶，无降级；粒度是 token 不是钱 |
| 能力调用审计 | ★☆☆☆☆ | 只有管理动作审计，无工具调用审计 |
| 分布式 / 多租户 | ★☆☆☆☆ | 无节点、无租户、无分区 |

---

## 6. 被低估的能力：Bridge

`src/octop/infra/bridge/`（3,778 行）实现了 Octop ↔ Octop 的跨实例连接：

| 能力 | 实现 |
|---|---|
| 连接粒度 | 用户级：地址 + 用户名 + 密码（加密存储，自动重连） |
| 传输 | WebSocket 联邦长连接 + 通用 HTTP 隧道 |
| 寻址 | `bridge:{connection_id}:{remote_agent_id}` |
| 双向 | 两边都可主动建连，共用 `connection_id` |
| 能力 | 远程专家列表、按需拉历史、对话中继、文件上传代理 |
| 边界 | 对话只在专家所在实例执行；发起侧不落完整历史 |
| 状态 | Phase 0–3 已落地；Phase 4（多连接稳定性、token 刷新）待做 |

详见 `docs/bridge.md`。

**对分布式的意义**：Bridge 已经验证了跨实例认证、隧道、寻址、历史按需拉取四件事。
它不是节点集群，但**是节点协调层可以复用的地基**。
缺的是节点发现、策略下发、批量审计汇总。

---

## 7. MiniApp 的位置与定位

### 位置

MiniApp 不是一个平级模块，而是一条**穿透三层**的纵向链路：

| 层 | 触点 |
|---|---|
| 执行层 | 19 个 `app_*` agent 工具 |
| 数据层 | `light_apps` / `light_app_rows` / `light_app_tables` / `light_app_sources` |
| 交互层 | iframe `srcDoc` 渲染 + publication 只读公开链接 |

### 定位

> **Agent 交付物的着陆场，是投影层而非权威源。**

三条边界：

| 它是什么 | 它不是什么 |
|---|---|
| Agent 产出的结构化数据 + 界面 | 不是业务数据库的权威源 |
| 非技术用户可直接消费的载体 | 不是审批系统（审批在 `hitl_pending`） |
| 外部数据的汇聚点（CSV / MCP） | 不是审计链（审计在 `audit_log` + 新的审计表） |
| 单实例内的展示层 | 不是多租户隔离的载体 |

**这个定位在 Octop 内部已有明确决策**：`docs/team-autopilot-design.html` 记录
「任务实体落原生表，MiniApp 只做投影展示层（P5）」。

### 与相邻概念的边界

| 概念 | 存什么 | 谁写 | 谁读 | 生命周期 |
|---|---|---|---|---|
| `threads` / `thread_messages` | 对话 | agent + 用户 | 人 | 会话级 |
| `memory` | 记忆与情节 | agent | agent | 长期 |
| `knowledge_bases` | 文档（RAG） | 用户 | agent | 长期 |
| `trajectory_events` | 执行轨迹 | 系统 | 调试者 | 审计 |
| **MiniApp** | **结构化数据 + 界面** | **agent + 人 + 外部同步** | **人（+agent 读回）** | **产品级** |

对话记录的是 agent 怎么想的，MiniApp 记录的是 agent 交付了什么。

---

## 8. 已知缺陷（无论做什么都该修）

四条**已存在**的缺陷，与任何新架构无关，修复成本极低：

| 编号 | 缺陷 | 位置 | 影响 |
|---|---|---|---|
| B1 | 照护调度器异常后静默死亡，不重排 | `infra/proactive/scheduler.py:259` | 该 agent 的照护永久消失直到重启 |
| B2 | 外部投递租约过期后无条件重跑 | `infra/db/repos/external_deliveries.py:146` | 已执行一半的工作可能重复执行外部动作 |
| B3 | 限流命中静默丢弃事件 | `infra/events/service.py` 限流分支 | 事件在库里但无人知道它被丢掉 |
| B4 | 主动工作跑在人类 DM 线程 | `infra/cron/manager.py:129`、`infra/agents/teams/runtime.py:376` | 抢 session lock、污染转录 |

合计约 **3–4 人日**（B4 含面板分组）。

---

## 9. 三条线索的交汇

```
线索 A：Octop 现状                    线索 B：Super-agent 借鉴
──────────────                     ──────────────────
Agent 运行时 ★★★★★                  两级过滤（Gate + Assessor）
多用户/通道/运维 ★★★★★               订阅直通
团队编排 ★★★★☆                      工单 + 授权（Mandate）
外部事件 ★★★★☆                      accept mode 三档
MiniApp ★★★★☆                       会话隔离 / 看门狗 / Outbox
控制面 ★★☆☆☆  ←──────── 补的正是这里
分布式 ☆☆☆☆☆

                    线索 C：分布式 Agent OS 需求
                    ─────────────────────────
                    带边界的目标（一等公民）
                    双模执行（工作流 / 编排）
                    能力合约 + 边界三态 + 审计链
                    成本硬顶 + 模型降级
                    盒子 + 集装箱（分布式）
```

**三条线交汇在同一个点：Octop 缺一层「治理」。**
Super-agent 提供这层的设计模式（`docs/superagent-borrowing-design.md`），
美业需求提供这层的具体验收标准。

---

## 10. 对齐矩阵：需求 vs 现状

| 需求 | Octop 现状 | 差距性质 | 应对 |
|---|---|---|---|
| 多用户隔离 | JWT + 角色 + ACL 完整 | ✅ **直接可用** | — |
| 路径 B：目标导向编排 | `TeamAutopilot` 全套 | ✅ **直接可用** | 补策略面板 |
| 成本硬顶 | `budget_remaining` 已生效 | ✅ **可用**（token 粒度待调） | 加金额维度 |
| 老板看板 / 门店屏幕 | MiniApp dashboard + publication | ✅ **直接可用** | 验证大屏轮询与断网 |
| MCP 工具挂载 | 成熟，30 个连接器 | ✅ **直接可用** | — |
| 带边界的目标 | `team_goals` 只有 `success_criteria`；预算在 `autonomy_policy` | ⚠️ **缺绑定** | 加 `bounds_json` |
| 路径 A：确定性工作流 | cron + `team_tasks`，**无工作流引擎** | ⚠️ **缺功能** | 外挂状态机或建模成 task |
| 能力合约 | `SecurityPolicy` 是**工具级** HITL | ⚠️ **层级不对** | 在其上加合约层 |
| 边界三态（放行/推送/拒绝） | 无 | ⚠️ **缺功能** | 新增，5–8 人日 |
| 能力调用审计 | 只有管理动作 | ⚠️ **缺功能** | harness 工具钩子埋点 |
| 模型自动降级 | 无（只有拒绝） | ⚠️ **缺功能** | 新增，3–5 人日 |
| 企微客户联系 | 只有扫码绑定 | ⚠️ **缺通道，可绕过** | `wecom-crm-mcp` 挂 MCP |
| 企微客服 | 无 | ⚠️ **缺通道** | gateway 插件机制 |
| 电话 PSTN | 无 | ⚠️ **缺 + 依赖外部** | 封装为能力合约 |
| 分布式 / 盒子 | 无节点；Bridge 提供跨实例隧道 | 🔴 **架构冲突** | 另建协调层 |
| 数据分区合规 | 无租户概念 | 🔴 **架构冲突** | 每节点一实例可规避 |
| AI 运维自治 | 无 | 🔴 **缺功能且缺抽象** | 自研 |

**读数：约 40% 直接可用，35% 缺功能可补，25% 是架构冲突或外部依赖。**

---

## 11. 差距的三种性质

### A 类：直接可用（约 40%）

多用户、团队编排、成本硬顶、MiniApp、MCP、通道。
**这是 Octop 的护城河**，自研要一年以上。

### B 类：缺功能，可补（约 35%）

| 项 | 人日 | 是否阻塞 MVP |
|---|---:|---|
| 能力合约 schema | 3–5 | **是** |
| 边界三态判断 | 5–8 | **是** |
| 能力调用审计 | 3–5 | **是** |
| `team_goals.bounds_json` | 2 | 是 |
| 企微客户联系（MCP） | 3–5 | 是 |
| 成本降级 | 3–5 | 否 |
| 企微客服通道 | 5–8 | 否（P1） |
| 电话 AI 封装 | 5–8 + 外部 | 是（可并行） |

**合计约 30–45 人日**，全部在单节点内完成，不改架构。

### C 类：架构冲突（约 25%）

| 项 | 冲突点 | 处置 |
|---|---|---|
| 分布式节点 | ADR 001 单进程 + `SharedServices` 进程全局 | **每节点一个 Octop + 外部协调层** |
| 多租户分区 | 无 tenant 维度 | 每门店一实例可规避 |
| AI 运维 | 无节点、无健康聚合 | 自研 |

**C 类不能靠扩展 Octop 解决，只能靠换形态**：让 Octop 待在它擅长的地方
（单节点自包含），把跨节点的事交给外面的协调层。

---

## 12. 建议路径

### 阶段 0：止血（3–4 人日，与任何产品无关）

修 §8 的 B1–B4。**无论做什么，这四条都该修。**

### 阶段 1：业务 MVP（30–45 人日 + 外部依赖）

```
能力合约 schema → 边界三态 → 审计埋点 → 企微 MCP → MiniApp 屏幕
     3-5d          5-8d       3-5d       3-5d       2-3d
        │
        └─ 并行：电话 AI 封装（5-8d + 云服务采购）
```

**前置门槛**：先验证盒子硬件能跑（Python 3.12 + FastAPI + LangGraph + 多 agent 并行）。

### 阶段 2：主动型团队（按需）

只有当阶段 1 的数据证明「同一件事确实跨多轮」时，才做
`docs/superagent-borrowing-design.md` 的方案 B（工单 / 直通 / Mandate）。

### 阶段 3：分布式（换形态，不改内核）

```
每盒子一个 Octop（SQLite 自包含）
        ↕ 复用 Bridge 的认证 + 隧道
中心一个 Octop（PostgreSQL）
        +
自研节点协调层（策略下发 / 审计汇总 / 任务重派）
```

**不要在 Octop 内部做分布式**——那是与 ADR 001 的核心假设对抗。

---

## 13. 待决策

| # | 问题 | 影响 |
|---|---|---|
| 1 | 业务产品是否立项 | 决定阶段 1 的 30–45 人日是否投入 |
| 2 | 盒子硬件规格 | 决定 Octop 能否直接跑在边缘，是否要裁剪 |
| 3 | 分布式是否真需要 | 门店数 < 50 时，单机 + 中心 Portal 可能就够 |
| 4 | 电话通道自建还是采购 | 决定能力合约封装形态与成本结构 |

---

## 14. 相关文档

| 问题 | 文档 |
|---|---|
| 技术细节（分层、进程、存储、i18n） | `docs/architecture.md` |
| 模块边界、改动规则、硬性禁令 | `AGENTS.md` §5 |
| 单进程决策 | `docs/adr/001-single-process-model.md` |
| 外部事件平面 | `docs/external-events-design.md` |
| 团队 L3 路线图 | `docs/team-l3-roadmap.md` |
| MiniApp 数据与协作 | `docs/light-apps-architecture.md` |
| MiniApp 增强计划 | `docs/light-apps-enhancement-plan.md` |
| Bridge 跨实例 | `docs/bridge.md` |
| 连接器清单 | `docs/connectors.md` |

### Super-agent 系列文档的取舍

这四份文档是一次调研的产物，**不是一次性要读完的**。按下面的状态取用：

| 文档 / 章节 | 状态 | 何时读 |
|---|---|---|
| `superagent-borrowing-design.md` **§0–§1** | ✅ **现在该做** | 立刻：方案 A（3–4 人日）= 4 个 bug + 会话隔离 + accept_mode |
| 同上 **§2 起**（方案 B：工单 / 直通 / Mandate / Gate / Assessor） | ⏸ **储备** | 阶段 1 的数据证明「同一件事确实跨多轮」时 |
| `superagent-borrowing-architecture.md` | ⏸ 储备 | 做方案 B 时——模块划分、端口、并发模型 |
| `superagent-borrowing-e2e.md` | ⏸ 储备 | 做方案 B 时——12 条断言规格 |
| `superagent-borrowing-e2e-story.md` | ⏸ 储备 | 做方案 B 时；或需要向非技术方解释这套机制时 |

**判断依据**：方案 B 的价值依赖「同一业务对象的后续事件占比高」这个前提。
若阶段 1 的数据显示多数工作一轮就结束，方案 B 的一半价值不成立。
