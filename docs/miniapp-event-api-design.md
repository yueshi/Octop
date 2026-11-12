# MiniApp 对外开放事件机制：评估与设计建议

> 状态：讨论稿，未实施（2026-11-11）
>
> 范围：对外事件入口、MiniApp 级寻址、事件契约、投递语义；与 CloudEvents / A2A / AG-UI / MCP 等公开协议的对照。
>
> 前置阅读：[external-events-design.md](./external-events-design.md)、[light-apps-architecture.md](./light-apps-architecture.md)。

---

## 0. 结论摘要

**作为“内部统一事件平面”，现有设计是够用的；作为“对外开放、可定位到 MiniApp 的事件机制”，还不够。** 不需要重写 outbox，缺的是四个一等公民：

1. **target 身份与 target 级授权**——现在是 source 中心：source 属于用户，订阅由 owner 预先配置，发布方不能寻址某个 MiniApp。
2. **事件契约**——`event_type` 是自由字符串，没有 schema、版本、目录；开放 API 的事件类型就是接口契约。
3. **MiniApp 语义**——`app` target 只有 `refresh/import`，且 import 固定 `replace`（`src/octop/infra/events/worker.py:628-633`），没有 append/upsert、UI 通知、agent turn。
4. **顺序与幂等**——outbox 是 at-least-once，app 并发 4（`worker.py:42`），没有 per-target 串行；`replace` 在乱序/重试下会覆盖新数据。

`eventtype@host:agent:miniapp` 作为**人读别名**可以，但不建议作为规范 wire format。它把 type、target、source 三个正交维度塞进一个字符串，分隔符会和 `host:port`、IPv6、名字中的 `:`/`@` 冲突；`host` 在单实例下冗余，在多实例下是联邦问题；`agent` 是可变绑定（`src/octop/infra/db/repos/apps.py:324` 可改），`name` 不唯一，只有 `app_id` 是稳定身份。

建议：**URL path 承载 target，CloudEvents 承载 type/source/id/time，`subject` 承载层级路径；单字符串只用于展示、日志、CLI。**

---

## 1. 假设

- “对外开放”指**外部系统把事件发进 Octop**，目标可以细到某个 MiniApp；不一定包含“外部订阅 Octop 的事件”。
- `host` 指 Octop 部署域名/实例，不是 agent 运行节点。
- 现有 `external_sources / subscriptions / deliveries` 是已上线或即将上线的资产，希望复用而不是推倒。

如果其中一条不成立，下面的授权模型和地址建议需要调整。

---

## 2. 现有设计评估

### 2.1 已经足够的部分

- DB inbox/outbox、lease、指数退避、dead-letter、启动 requeue（`worker.py:39-48`、`tick`）。
- fan-out 到多个 subscription，每条 delivery 独立重试；`UNIQUE(event_id, subscription_id)` 保证不重复入队。
- HMAC、timestamp ±300s、body cap 256KB、每 source 限流、审计与 30 天保留（`src/octop/infra/events/security.py:11-16`）。
- 三个 target adapter，且在订阅创建和投递时都做归属校验（`service.py:398-470`）。
- 分层干净：`infra/events` 是 domain，`api/routers/hooks.py:81` 只做 transport。

### 2.2 不够的部分

| 缺口 | 证据 | 影响 |
|---|---|---|
| **source 中心，不是 target 中心** | `service.py:117-146` 创建 source；`279-314` 由 owner 配 subscription | 发布方不能寻址 MiniApp；`eventtype@...:miniapp` 没有授权落点 |
| **app adapter 语义太窄** | `worker.py:585-637`；import 强制 `mode="replace"`（631） | 事件只能全量覆盖，不能幂等追加/更新 |
| **没有事件契约** | `service.py:79-98` 只做 exact/equals/contains；`MAX_EVENT_TYPE_CHARS=100` | 开放 API 无 schema/版本/目录，集成方无法自描述 |
| **没有 per-target 顺序** | `worker.py:42` `MAX_CONCURRENT_APP=4`；`tick` 一次 claim 多条 | 同一 app 的事件可并发/乱序，replace 会倒退 |
| **没有 target 级凭证** | `create_source` 只返回一个 source secret；`verify_webhook` 按 source 验签 | 泄漏一个 secret 影响该 source 全部订阅；无法只给单个 app 发 key |
| **没有 app 级事件日志/通知** | 管理 API 全是 `/api/external/*`，按 source 查 | 集成方无法按 app 调试；MiniApp UI 收不到事件 |
| **非标准信封/签名** | `X-Octop-*` + hex HMAC over `timestamp.body` | 生态 SDK 不兼容，集成成本高（不是功能缺陷，是采用成本） |
| **只能入站** | 没有 webhook egress target；cleanup 30 天 | 外部系统无法订阅 MiniApp 事件，也无法回放 |
| **dedupe 并发竞态** | `external_events.py:85-110` 先 SELECT 再 INSERT | PG 并发重复请求可能撞 UNIQUE 返回 500，而不是幂等 202 |
| **bulk 数据不适合走事件** | 256KB cap；无 artifact/claim-check | 大批量导入应走 CSV/import API 或对象存储引用 |

结论：**保留现有 plane，在它前面加一个 target-facing facade，在 app adapter 里补齐语义。**

---

## 3. 地址格式：建议怎么改

### 3.1 为什么单字符串不合适

- `eventtype` 和 `target` 是正交的。CloudEvents 用 `type` 表达“发生了什么”，用 `source` 表达“谁发的”，用 `subject` 表达“关于哪个资源”；这三个字段各自可过滤、可索引。合并后，任何一方变化都要改地址。
- `host` 不可信：反向代理下 `Host` 头可伪造；`public_base_url` 才应该是展示/链接来源（`src/octop/config.py:167`）。多实例/联邦时，host 需要 server identity + 信任注册表，不是一个字符串段能解决的。
- `agent` 不是身份：`light_apps.agent_id` 可以改；app 的 ACL 属于 owner/collaborator，agent 只是运行时绑定。把 agent 放进规范地址，app 迁移后外部地址就失效。
- `miniapp` 名字不唯一；`app_id` 才是全局唯一、不可变的。
- 通配符/订阅：如果以后要支持“某 agent 下所有 MiniApp”，单地址无法表达；需要 `subject` 前缀或 topic 层级。

### 3.2 推荐规范

**Ingress：target 放在 URL path，事件字段放 CloudEvents。**

```http
POST /api/apps/app_7F3K2Q/events
Authorization: Bearer octop_evt_...
Content-Type: application/cloudevents+json

{
  "specversion": "1.0",
  "id": "order-123-created",
  "source": "https://crm.example.com",
  "type": "com.acme.order.created",
  "subject": "miniapps/app_7F3K2Q/tables/orders",
  "time": "2026-11-11T09:30:00Z",
  "dataschema": "https://crm.example.com/schemas/order-created-v1.json",
  "data": {
    "order_id": "123",
    "amount": 1999,
    "summary": "订单 #123 已创建"
  }
}
```

- `type`：发布方自己的 reverse-DNS 命名空间；Octop 只做 allowlist/前缀匹配。
- `source`：发布方 URI，不是 Octop 的 `source_id`。当前 `external_events` 只有 `source_id`，建议补 `source_uri` / `subject` / `dataschema`，否则转发出去时丢失原始身份。
- `subject`：`miniapps/{app_id}` 或 `miniapps/{app_id}/tables/{table_key}`；app 迁移不影响。
- `id`：发布方稳定事件 id，用于 `(source, id)` 去重；没有 `id` 直接 400，不要用“payload hash + 分钟桶”兜底（`service.py:528-532` 现在会这么做，重试跨分钟会重复）。

如果确实需要 agent 层级，`subject` 可写成 `agents/{agent_id}/miniapps/{app_id}`，但**服务端以 `app_id` 解析为准**，agent 段只做一致性校验/展示；app 迁移后旧地址应重定向或报明确错误。

### 3.3 人读别名

```text
com.acme.order.created@octop.example.com/miniapps/app_7F3K2Q
```

或保留 `eventtype@host:agent:miniapp` 形式，但只用于 UI、日志、CLI、文档；**不要把它作为 API 的必填字段或内部路由键**。内部 schema 继续用 `target_type/target_id`，不要新增一个需要解析的字符串列。

---

## 4. 业界协议对照

| 协议 | 解决的问题 | 对本设计的用法 | 不要用来做 |
|---|---|---|---|
| **CloudEvents 1.0** | 事件信封与路由 | `type` + `source` + `subject` + `id` + `time` + `data`；去重/过滤/转发 | 任务状态机、agent 发现 |
| **A2A 1.0** | Agent 间任务与发现 | 对外 Agent 的 Agent Card、`message/send`、task states、push notification config | 高频业务事件总线 |
| **AG-UI 1.0** | Agent↔前端事件流 | MiniApp iframe/host 借鉴 `CUSTOM`、`STATE_SNAPSHOT`、`STATE_DELTA` | 服务间事件协议 |
| **MCP** | Agent↔工具/资源 | 把 MiniApp 表暴露为 MCP resources/tools；资源订阅 | 业务事件总线 |
| **AsyncAPI 3.0** | 事件 API 契约 | 描述 channel/message/schema，生成文档 | 运行时路由 |
| **Standard Webhooks** | Webhook 签名 | 标准签名头/密钥格式，兼容现有 HMAC | 授权模型 |
| **NATS / MQTT** | 主题与通配符 | 仅在需要外部订阅时借鉴 `*`/`>`、`+`/`#` | 引入 broker |
| **GitHub repository_dispatch** | 资源级事件入口 | target-in-path + `event_type` + bounded payload 的最佳先例 | — |

### 4.1 关键边界：事件平面 ≠ A2A 任务平面

- **A2A 是“把任务交给一个 Agent”**：有 task 生命周期（SUBMITTED/WORKING/INPUT_REQUIRED/COMPLETED/…）、流式更新、push notification config、Agent Card 发现。它适合外部 agent 来“做事”，不适合“订单创建了通知一下”这种高频、无状态事件。
- **CloudEvents 是“通知发生了什么”**：无任务状态、at-least-once、幂等消费。它适合业务系统 → MiniApp 的数据/通知。
- **不要为了统一而把两者硬合并**：事件要触发 agent 时，事件平面负责“收到并落库”，再由 `app.agent_turn` 创建一次受限 turn；而不是把每个事件都变成 A2A task。
- IBM 的 ACP 已并入 A2A（2025 年），不需要再设计一套 ACP 风格端点。
- MCP 与 A2A 目前同属 Agentic AI Foundation 生态：MCP 管工具/资源，A2A 管 agent 任务，事件平面管通知，三层各司其职。

**关于 MiniApp 是否是 A2A agent**：不建议。MiniApp 是“agent 下的 UI + 数据资源”，没有独立身份、记忆、预算；它的 read-only assistant 是按用户、只读的（`src/octop/infra/apps/assistant.py`）。外部 agent 要“操作 MiniApp”，应该找 owning agent（A2A），由 agent 调 `app_*` 工具；外部系统要“通知 MiniApp”，走事件平面。

---

## 5. 推荐目标模型

### 5.1 分层

```text
外部发布方
  │ POST /api/apps/{app_id}/events (app key)  或  /api/hooks/{source_id} (HMAC)
  ▼
ingress: CloudEvents 解析 / 验签 / 限流 / body cap
  ▼
event contract: type allowlist + JSON Schema + version
  ▼
external_events (inbox) + external_deliveries (outbox)
  ▼
target adapters:
  app.append | app.upsert | app.replace(guarded)
  app.refresh | app.notify | app.agent_turn
  expert.turn | team.judge | webhook(可选)
  ▼
app-scoped event log / dead-letter / replay
```

现有 source→subscription 模型保留，用于“owner 预先配置的可信集成”；新增 **app-scoped ingress**，用于“发布方直接寻址某个 MiniApp”。

### 5.2 授权：两种凭证，不要混用

| 模型 | 适用 | 凭证 | 发布方能否选 target |
|---|---|---|---|
| **source-scoped（现有）** | 可信系统集成、一个 source fan-out 多个 target | HMAC secret | 不能；target 由 owner 的 subscription 决定 |
| **app-scoped（新增）** | 外部 app builder / 业务系统只发一个 MiniApp | `octop_evt_...` Bearer，hash 存储，scoped to app + allowed types + optional table | 只能发 path 里的 app；不能改 mode/user/table |

实现上不要让 app key 直接复用 owner-wide source 的 secret。可以：

- 新增 `app_event_keys` 表（推荐）：`key_id`、`app_id`、`secret_hash`、`allowed_types_json`、`rate_limit`、`expires_at`、`revoked_at`、`last_used_at`、`created_by`。
- 或给 `external_sources` 增加 `bound_target_type/bound_target_id`，`kind='app_ingress'`，内部仍落到同一个 source 行；但权限边界要按 app ACL 校验，不能按 source owner 放大。

管理权限：app owner/admin 管理 key；发布方永远不能通过 payload 指定 `target/mode/table/user_id`。当前设计已经坚持“payload 不能指定 target”（`docs/external-events-design.md:309`），这一点必须保留。

### 5.3 MiniApp 的四种语义

| mode | 做什么 | 幂等/顺序 | 建议 |
|---|---|---|---|
| `app.append` / `app.upsert` | 写行；配置 `upsert_key` | `client_request_id=event_id` 或 `(source_id, source_key)`；append 与顺序无关 | **P0** |
| `app.replace` | 整表快照替换 | 必须带 `snapshot_version` 或单调 `occurred_at`；per-app 串行 | **P1，默认关闭** |
| `app.refresh` | 触发已有 `light_app_sources` 拉取 | 已有 source 状态/退避 | 已有 |
| `app.notify` | 事件推给打开的 MiniApp UI | 按 `event_id` 去重；best-effort；持久化可回放 | **P1** |
| `app.agent_turn` | 由 owning agent 跑一轮 | 按 agent 串行；owner 身份、预算、注入防护 | **P1** |
| `app.assistant_turn` | 只读 app assistant 回答 | per-user 历史；没有明确读者 | **P2 / 不建议** |

关键点：

- **append/upsert 是 P0**。`import_rows` 已支持 `mode="append"` 和 `upsert_key`（`src/octop/infra/apps/service.py:2404-2420`、`2576-2610`），`append_data_row` 也已支持 `client_request_id`（`src/octop/infra/db/repos/apps.py:334-343`）。但 worker 强制 `replace`，这两条能力在事件路径上等于没用。把 `event_id` 作为 `client_request_id`（单行）或 `source_key`（批量）传进去，重试就天然幂等。
- **replace 必须加保护**。当前 `_records` 只拒绝空列表（`worker.py:84-93`），但一个“部分快照”仍会覆盖全表。要求 `snapshot_version` 单调，或按 `(app, table, source)` 存 `last_event_at` 水位；旧事件直接 ignore + metric，不要静默覆盖。
- **notify 不需要新 broker**。事件已经落在 `external_events`；加 `GET /api/apps/{app_id}/events?after_id=...`，dashboard 按现有 `data_version` 轮询模式转发给 iframe；公开页走 token-scoped SSE/轮询。MiniApp SDK 增加 `onEvent(cb)`。低延迟再上 WS/SSE，不要一上来做完整 AG-UI。
- **agent_turn 的身份是 app owner，不是发布方**。事件是数据，不是用户指令；prompt 明确“外部内容只作数据”，预算记在 app owner，失败通知 owner。不要用只读的 app assistant 做动作类事件。

### 5.4 顺序、幂等、背压

- 给 `external_deliveries` 加 `ordering_key`（默认 `app:{app_id}:{table}`），claim 时保证同一 key 只有一个 in-flight；单进程用 `asyncio.Lock`，多进程用 DB lease/`FOR UPDATE SKIP LOCKED`。
- 去重键：`source_id + Ce-Id`；app ingress 强制要求 `Ce-Id`。修复 PG 并发竞态：`INSERT ... ON CONFLICT DO NOTHING` 后 SELECT，或捕获 `IntegrityError` 返回已有事件。
- 每 app 的 pending delivery 上限；超过返回 429/503 + `Retry-After`，不要让 DB 无限增长。
- 每 app key / 每 IP 限流；当前 `allow_webhook` 是 source 级（`service.py:474-479`），app ingress 要按 key + app 限。
- `max_per_minute` 现在超限是静默 skip subscription（`service.py:545-549`）；开放 API 下应返回 429 或记录 `rate_limited` delivery，让发布方可见。
- 语义写清楚：**at-least-once，消费方必须幂等**；不要承诺 exactly-once。

### 5.5 事件契约

- 新增 `event_types` 目录（表或 JSON registry）：`type`、`description`、`schema_json`、`dataschema`、`allowed_modes`、`app_id`（可空=全局）、示例。
- ingest 时按 JSON Schema 校验 `data`；失败 422 + 错误明细；不要只校验“是 JSON object”。
- 过滤保留现有 `event_type` / `equals` / `contains`，再加 **type 前缀匹配**（`com.acme.order.*`）和 `subject` 前缀即可；不要做 JSONPath/表达式引擎。
- 用 AsyncAPI 3.0 描述 channel/message/schema，作为文档和 SDK 生成来源；OpenAPI 只覆盖管理/ingress API。

### 5.6 兼容与迁移

- 同时接受 CloudEvents structured（`application/cloudevents+json`）和 binary（`ce-*` 头）；现有 `X-Octop-*` JSON 继续工作，标记 legacy。
- 签名可兼容 Standard Webhooks（`webhook-id` / `webhook-timestamp` / `webhook-signature: v1,<base64>`），现有 hex HMAC 保留一个 deprecation 窗口；key rotation 支持新旧 secret 重叠，而不是 `rotate_secret` 立即失效。
- 如果 027 迁移尚未发布，把 `source_uri` / `subject` / `dataschema` 折进 027；如果已发布，新增迁移，不要改已应用版本。

---

## 6. 最小改造路线

**P0：先把“能寻址、能幂等写、能按 app 看日志”做出来**

1. `POST /api/apps/{app_id}/events` + app-scoped key；内部映射到 bound source，复用 outbox。
2. CloudEvents 解析（structured + binary），legacy 兼容；`Ce-Id` 必填。
3. `app.append` / `app.upsert`：传 `client_request_id` / `source_key`；停止强制 `replace`。
4. `replace` 加 `snapshot_version`/`occurred_at` 水位保护。
5. 每 app 的 `ordering_key` / 单飞；修 dedupe 并发竞态。
6. `GET /api/apps/{app_id}/events`（owner 可查，含 delivery 状态/错误）。
7. 最小事件类型 allowlist + JSON Schema。

**P1：MiniApp 运行时与生态**

1. `app.notify`：事件查询 + dashboard 转发 iframe + SDK `onEvent`；公开页 SSE/轮询。
2. `app.agent_turn`：owning agent、owner 身份、预算、注入防护。
3. Standard Webhooks 兼容 + key 轮换重叠。
4. 可选 egress webhook target（复用 outbox，加 SSRF 防护）；dead-letter/replay UI。

**P2：协议化与规模化**

1. A2A endpoint / Agent Card（如果外部 agent 协作是目标）；task/context 映射到 thread。
2. AsyncAPI 文档 + 生成 SDK；schema registry UI。
3. 多实例 ordering/HA；跨 Octop 实例的 server identity / 信任注册表（如果真要做 `host` 路由）。
4. MCP resources/tools 暴露 MiniApp 表（如果外部 agent 需要 pull 而不是 push）。

---

## 7. 不建议做的事

- **不要重写 outbox**。现有 lease/retry/dead-letter 已经是正确的基础。
- **不要把 `eventtype@host:agent:miniapp` 作为内部路由键**。内部继续用结构化 `target_type/target_id`；字符串只在边界出现。
- **不要让 payload 决定 target/mode/table/user**。这是 confused-deputy 和 prompt injection 的入口。
- **不要给 MiniApp 加任意 JS 事件处理器**。它是 sandbox iframe，不是服务端函数；事件语义用声明式 mode 表达。
- **不要用 A2A 做通知，也不要用 CloudEvents 做任务状态**。两者可以并存，不要互相替代。
- **不要在单实例产品里引入 `host` 段**；如果未来要联邦，用显式 server identity + A2A 式发现，而不是 host 字符串。
- **不要用 app name 做身份**；用 `app_id`。名字只做展示和解析别名。

---

## 8. 待拍板

1. **事件到 MiniApp 的语义**：数据写入、UI 通知、agent turn，还是三者都要？P0 先做哪个？
2. **发布方能否选择 target**：是 owner 预先配订阅（现有模型），还是 app key 直接寻址（新模型）？这决定授权和迁移方式。
3. **只需要入站，还是也要外部订阅/egress/回放**？
4. **单实例还是多实例/联邦**：`host` 是部署域名还是 agent 节点？如果是后者，建议不要暴露。
5. **事件类型是开放命名空间，还是每 app allowlist + JSON Schema 必填**？
6. **是否接受 CloudEvents 作为规范 wire format**？如果已有合作方强依赖现有 `X-Octop-*`，需要保留多久的兼容窗口？

---

## 9. 参考资料

- [CloudEvents specification](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md)
- [CloudEvents primer](https://github.com/cloudevents/spec/blob/main/cloudevents/primer.md)
- [A2A Protocol specification](https://a2a-protocol.org/latest/specification/)
- [A2A GitHub project](https://github.com/a2aproject/A2A)
- [A2A v1.0 deep dive (2026)](https://github.com/ariffazil/aaa/blob/fcc2d2f0abe2f3e400ccd63d5a5269a9eec66226/skills/domains/general/aaa/onboarding/a2a-agent-card-registration/references/a2a-spec-deep-dive.md)
- [AG-UI 1.0 event stream spec](https://docs.ag-ui.com/spec/1.0/events/index)
- [AG-UI events concepts](https://docs.ag-ui.com/concepts/events)
- [Standard Webhooks](https://www.standardwebhooks.com/)
- [AsyncAPI 3.0 specification](https://www.asyncapi.com/docs/reference/specification/v3.0.0)
- [GitHub repository_dispatch API](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event)
- [Model Context Protocol specification](https://modelcontextprotocol.io/specification)
- [NATS subject-based messaging](https://docs.nats.io/nats-concepts/subjects)
