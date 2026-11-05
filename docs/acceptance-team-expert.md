# 验收案例：团队协作与数字员工

> 目标：用一条业务主线，端到端验证 **team（多成员团队）** 与 **expert（数字员工）** 两条链路，
> 以及它们共享的底座（状态机 / CAS / 事件账本 / 预算 / 验收 / 恢复）。
>
> 适用于任何部署（本地或服务端）。所有命令基于 `/api`，每个阶段都有**预期结果**与**观察点**。
> 标记 💰 的步骤会调用真实 LLM，产生 token 消耗；其余为纯状态操作。

---

## 0. 准备

```bash
API=http://127.0.0.1:8088          # 按实际部署调整
TOKEN=<your-jwt>                    # 见下方「获取 token」
AUTH="Authorization: Bearer $TOKEN"
JSON="Content-Type: application/json"
```

**获取 token**（服务端部署，管理员）：

```bash
SECRET_HEX=$(sqlite3 /root/.octop/octop.db "SELECT hex(v) FROM secrets WHERE k='jwt'")
/root/.octop/venv/bin/python -c "
import binascii, sys
sys.path.insert(0, '/root/.octop/venv/lib/python3.12/site-packages')
from octop.infra.auth.tokens import sign_token
print(sign_token(binascii.unhexlify('$SECRET_HEX'), sub=1, uname='admin', role='admin'))
"
```

**基线记录**（验收前后对比，确认清理干净）：

```bash
curl -s -H "$AUTH" "$API/api/agents" | python3 -c "
import json,sys
print('\n'.join(f\"{a['agent_id']} {a['kind']} {a['name']}\" for a in json.load(sys.stdin)))"
```

记下这些行，最后应恢复原状。

---

## Case A — 团队协作（多成员）

### A1. 创建成员与团队

```bash
# 两个成员（普通 expert）
ANALYST=$(curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d '{"name":"分析师-小析","config":{"providers":["openai"],"default_model":"openai:gpt-4o-mini"}}' \
  "$API/api/agents" | python3 -c "import json,sys;print(json.load(sys.stdin)['agent_id'])")
WRITER=$(curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d '{"name":"文案-小文","config":{"providers":["openai"],"default_model":"openai:gpt-4o-mini"}}' \
  "$API/api/agents" | python3 -c "import json,sys;print(json.load(sys.stdin)['agent_id'])")

# 团队（host 自动创建）
TEAM=$(curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d "{\"name\":\"季度运营组\",\"member_ids\":[\"$ANALYST\",\"$WRITER\"]}" \
  "$API/api/teams" | python3 -c "import json,sys;print(json.load(sys.stdin)['agent_id'])")
echo "TEAM=$TEAM"
```

**预期**：团队创建成功，`kind=team`，`member_ids` 含两个成员。

**反例验证**（必须拒绝）：

```bash
# 少于 2 个成员 → 400 TEAM_MEMBERS_TOO_FEW
curl -s -o /dev/null -w "成员不足: %{http_code}\n" -X POST -H "$AUTH" -H "$JSON" \
  -d "{\"name\":\"单人组\",\"member_ids\":[\"$ANALYST\"]}" "$API/api/teams"
```

### A2. 设定目标（不触发 LLM）

```bash
curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d '{"title":"产出 Q3 运营报告","description":"覆盖增长、留存、成本三条线","success_criteria":"三条线各有一页结论 + 数据来源"}' \
  "$API/api/teams/$TEAM/goals"
```

**预期**：返回 `status=active`；再次调用会**归档旧目标**（一个团队只有一个 active 目标）。

**观察点**：

```bash
sqlite3 /root/.octop/octop.db \
  "SELECT title, status FROM team_goals WHERE team_id='$TEAM' ORDER BY created_at;"
# 应看到旧的 archived、新的 active
```

### A3. 启动驱动循环 💰

```bash
curl -s -X POST -H "$AUTH" -H "$JSON" -d '{"heartbeat_interval_sec":900}' \
  "$API/api/teams/$TEAM/drive/start" | python3 -m json.tool | head -12
```

**预期**：`status=running`、`mode=autopilot`、`next_heartbeat_at` 已设置。

**观察点（等待 1–3 分钟）**：

```bash
# 看板任务（host 拆解出的）
curl -s -H "$AUTH" "$API/api/teams/$TEAM/tasks" | python3 -c "
import json,sys
for t in json.load(sys.stdin): print(t['task_id'], t['status'], t['assignee'], t['title'])"

# 事件账本（可审计）
sqlite3 /root/.octop/octop.db \
  "SELECT seq, type, actor, substr(payload_json,1,70) FROM team_events WHERE team_id='$TEAM' ORDER BY seq;"
```

**应看到的事件序列**（顺序可能因并发略有差异）：

```
runtime.boot        | user:1      | {}
goal.changed        | user:1      | {"goal_id":..., "status":"active"}
task.changed        | host        | {"task_id":..., "status":"proposed"}
task.changed        | coordinator | {"task_id":..., "status":"assigned"}
task.changed        | coordinator | {"task_id":..., "status":"running"}
member.replied      | member:<id> | {"status":"done", ...}
task.changed        | member:<id> | {"task_id":..., "status":"done"}
```

**关键断言**：

- `task.changed` 的 `actor` 必须是 `coordinator`（派发）或 `member:*`（回叫），**不应出现 `host` 直接改 running**
- `member.replied` 出现后，同一 `task_id` 的 `wait` 状态应变为 `settled`

### A4. 等待与审批（HITL）

```bash
curl -s -H "$AUTH" "$API/api/teams/$TEAM/waits" | python3 -m json.tool
```

**两种 wait 要区分**：

| kind | 含义 | 处理入口 |
|---|---|---|
| `member_reply` | 等成员执行 | 自动（回叫时 settle） |
| `approval`（无 `external_ref`） | 团队级审批 | `POST /teams/{id}/waits/{wid}/resolve` |
| `approval`（有 `external_ref`） | 成员 HITL 暂停 | **必须在会话卡片里答**，API 会 409 |

审批操作：

```bash
WAIT=$(curl -s -H "$AUTH" "$API/api/teams/$TEAM/waits?status=waiting" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[0]['wait_id'] if d else '')")
[ -n "$WAIT" ] && curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d '{"status":"approved","note":"同意"}' "$API/api/teams/$TEAM/waits/$WAIT/resolve"
```

**反例**：用成员 HITL 的 wait 调这个接口 → **409 TEAM_WAIT_CONFLICT**（这是设计约束，不是 bug）。

### A5. 暂停 / 恢复 / 完成 / 停止

```bash
curl -s -X POST -H "$AUTH" -H "$JSON" -d '{"reason":"人工介入"}' "$API/api/teams/$TEAM/drive/pause"
curl -s -X POST -H "$AUTH" -H "$JSON" -d '{}' "$API/api/teams/$TEAM/drive/resume"
curl -s -X POST -H "$AUTH" -H "$JSON" -d '{}' "$API/api/teams/$TEAM/drive/finish"
```

**预期**：

| 动作 | runtime status | 副作用 |
|---|---|---|
| pause | `paused_user` | 新决策/派发停止；在途 wait 保留 |
| resume | `running` | deferred wait 重新激活 |
| finish | `finishing` | active goal → `done`，不再接新工作 |
| stop | `stopped` | 取消 open 任务、清 inbox、结算 wait |

```bash
curl -s -X POST -H "$AUTH" "$API/api/teams/$TEAM/drive/stop" | python3 -m json.tool
# 预期：{"disabled_jobs":0,"drained_jobs":N,"cancelled_tasks":M,"cancelled_waits":K}
```

**停止后验证**：

```bash
sqlite3 /root/.octop/octop.db \
  "SELECT status, COUNT(*) FROM team_tasks WHERE team_id='$TEAM' GROUP BY status;"
# 应无 running / assigned / proposed（全部 cancelled 或 done）
```

---

## Case B — 数字员工（单人自治）

> 与团队共用 `team_tasks` / `task_state` / `team_waits`，但执行单元不同：
> 团队派给别人，数字员工执行自己（harness 禁止自派发）。

### B1. 雇用

```bash
SOLO=$ANALYST          # 复用 Case A 的分析师，也可新建一个
curl -s -X PUT -H "$AUTH" -H "$JSON" -d '{"enabled":true}' \
  "$API/api/agents/$SOLO/autonomy"
```

**预期**：`{"agent_id":"...","enabled":true}`

**观察点**：

```bash
sqlite3 /root/.octop/octop.db \
  "SELECT autonomy_enabled FROM agents WHERE agent_id='$SOLO';
   SELECT status, mode FROM team_runtime WHERE team_id='$SOLO';"
# 预期：1 / running / autopilot
```

**反例**：对 `kind=team` 的 agent 调用 → **400**（团队有自己的 autopilot 开关）。

### B2. 设定岗位

```bash
curl -s -X PUT -H "$AUTH" -H "$JSON" -d '{
  "active_hours_start":"09:00","active_hours_end":"22:00",
  "budget_limit_tokens":200000,"budget_window":"day",
  "sla_tasks_per_day":5,
  "responsible_scope":"负责运营数据整理，产出结构化结论",
  "tool_scope_deny":"execute"
}' "$API/api/agents/$SOLO/autonomy-policy"
```

**反例**：`active_hours_start >= active_hours_end` → **422**。

### B3. 下限（不指定 assignee）

```bash
TASK=$(curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d '{"title":"整理 9 月留存数据"}' "$API/api/agents/$SOLO/tasks" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['task_id'],d['assignee'])")
echo "$TASK"
```

**预期**：`assignee` **等于该 agent 自己**（数字员工没有可派工对象，默认落到自己）。

### B4. 自执行 💰

**等待 1–3 分钟**，然后：

```bash
curl -s -H "$AUTH" "$API/api/agents/$SOLO/tasks" | python3 -c "
import json,sys
for t in json.load(sys.stdin): print(t['task_id'], t['status'], t['title'])"

# 计量（关键：自执行必须记账）
sqlite3 -header -column /root/.octop/octop.db \
  "SELECT COUNT(*) rows, SUM(total_tokens) tokens, MAX(source) src
   FROM usage_log WHERE team_id='$SOLO';
   SELECT budget_used FROM team_runtime WHERE team_id='$SOLO';"
```

**必须满足**：

- 任务状态推进到 `done`
- `usage_log` 有 `source=self_execute` 的行
- `budget_used` **等于**这些行的 token 之和

> ⚠️ 若 `budget_used` 恒为 0，说明自执行绕过了记账——预算闸门等于不存在。

### B5. 预算闸门

```bash
# 把上限压到已用量以下
curl -s -X PUT -H "$AUTH" -H "$JSON" -d '{"budget_limit_tokens":1}' \
  "$API/api/agents/$SOLO/autonomy-policy" >/dev/null

# 新建任务 → 应停在 proposed，不被派发
T2=$(curl -s -X POST -H "$AUTH" -H "$JSON" -d '{"title":"闸门验证"}' \
  "$API/api/agents/$SOLO/tasks" | python3 -c "import json,sys;print(json.load(sys.stdin)['task_id'])")
sleep 30
sqlite3 /root/.octop/octop.db "SELECT status FROM team_tasks WHERE task_id='$T2';"
# 预期：proposed（不是 running）
```

**收件箱应报告预算**：

```bash
curl -s -H "$AUTH" "$API/api/agents/$SOLO/inbox" | python3 -m json.tool
# 预期：counts 含 "budget": 1
```

### B6. 验收（review）

```bash
V=$(sqlite3 /root/.octop/octop.db "SELECT version FROM team_tasks WHERE task_id='$T2'")
# 手动推进到 review（跳过预算限制：API 只校验状态机）
curl -s -X PATCH -H "$AUTH" -H "$JSON" -d "{\"status\":\"running\",\"expected_version\":$V}" \
  "$API/api/agents/$SOLO/tasks/$T2" >/dev/null
V=$((V+1))
curl -s -X PATCH -H "$AUTH" -H "$JSON" -d "{\"status\":\"review\",\"expected_version\":$V}" \
  "$API/api/agents/$SOLO/tasks/$T2" >/dev/null
V=$((V+1))

# 驳回 → 任务回到 proposed（返工边）
curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d "{\"decision\":\"rejected\",\"comment\":\"缺少数据来源\",\"expected_version\":$V}" \
  "$API/api/agents/$SOLO/tasks/$T2/review" | python3 -m json.tool
```

**预期**：`task.status = proposed`；`review.reviewer = user:1`、`comment` 被记录。

**查询验收历史**：

```bash
curl -s -H "$AUTH" "$API/api/agents/$SOLO/tasks/$T2/reviews"
# 预期：[{"decision":"rejected","comment":"缺少数据来源","reviewer":"user:1",...}]
```

### B7. KPI

```bash
curl -s -H "$AUTH" "$API/api/agents/$SOLO/metrics" | python3 -m json.tool
```

**预期**：

- `tasks` 按状态计数 + `open`
- `usage.total_tokens` 与 `usage_log` 一致
- `budget.remaining = max(0, limit - used)`

---

## Case C — 横切能力（两条线共用）

### C1. 状态机（非法流转必须被拒）

```bash
T3=$(curl -s -X POST -H "$AUTH" -H "$JSON" -d '{"title":"状态机验证"}' \
  "$API/api/agents/$SOLO/tasks" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['task_id'],d['version'])")
```

| 操作 | 预期 |
|---|---|
| `proposed → running` | 200 |
| `running → proposed` | **400 INVALID_TRANSITION** |
| `proposed → review` | **400**（review 只能从 running 进） |
| `done → running` | **400**（终态） |
| 同版本重复 PATCH | **409 CONFLICT**（CAS） |

**额外检查**（服务端不能违反自己的状态机）：

```bash
# 派发失败的服务端写路径：应落到 blocked，不是 failed
sqlite3 /root/.octop/octop.db \
  "SELECT status FROM team_tasks WHERE team_id='$SOLO' AND blocked_reason LIKE 'dispatch failed%';"
# 若存在，必须是 blocked
```

### C2. 事件账本可审计

```bash
sqlite3 -header -column /root/.octop/octop.db "
  SELECT type, actor, COUNT(*) n FROM team_events
  WHERE team_id IN ('$TEAM','$SOLO') GROUP BY type, actor ORDER BY type;"
```

**应满足**：

- 每条 `task.changed` 的 `dedupe_key` 唯一，且**同一秒内两次编辑都能入账**（goal 的 key 含 nonce）
- `actor` 可区分 `user:*` / `host` / `coordinator` / `member:*` / `sweeper`

### C3. 重启恢复

```bash
systemctl restart octop && sleep 20
curl -s "$API/api/health"
```

**恢复后检查**：

```bash
# 1) 在途 member_reply wait 应被回收（任务转 blocked，不静默丢）
sqlite3 /root/.octop/octop.db \
  "SELECT task_id, status, blocked_reason FROM team_tasks
   WHERE team_id='$TEAM' AND status IN ('assigned','running');"
# 预期：无（全部被 sweeper 回收为 blocked 或已结算）

# 2) HITL 暂停的卡片仍可答复（pending_id 复用持久化值）
sqlite3 /root/.octop/octop.db \
  "SELECT status, COUNT(*) FROM hitl_pending GROUP BY status;"
```

> 若重启前有 `pending` 的审批，重启后**用原卡片上的 id 答复仍应生效**（不是 `hitl.expired`）。

### C4. 权限边界

```bash
# bob 不是 owner → 403
curl -s -o /dev/null -w "bob→tasks: %{http_code}\n" -H "Authorization: Bearer $BOB_TOKEN" \
  "$API/api/agents/$SOLO/tasks"
```

**预期**：`403`（admin 可绕过）。

### C5. 跨团队隔离

```bash
# 把 A 团队的任务挂到 B 团队的 goal 上 → 必须拒绝
curl -s -X POST -H "$AUTH" -H "$JSON" \
  -d "{\"title\":\"越界\",\"goal_id\":\"<B团队的goal_id>\"}" \
  "$API/api/agents/$SOLO/tasks" -w " HTTP %{http_code}\n"
# 预期：404（goal 不属于该 agent）
```

---

## 清理

```bash
curl -s -X PUT -H "$AUTH" -H "$JSON" -d '{"enabled":false}' "$API/api/agents/$SOLO/autonomy"
curl -s -X POST -H "$AUTH" "$API/api/teams/$TEAM/drive/stop" >/dev/null
curl -s -o /dev/null -w "删团队 %{http_code}\n" -X DELETE -H "$AUTH" "$API/api/teams/$TEAM"
curl -s -o /dev/null -w "删分析师 %{http_code}\n" -X DELETE -H "$AUTH" "$API/api/agents/$ANALYST"
curl -s -o /dev/null -w "删文案 %{http_code}\n" -X DELETE -H "$AUTH" "$API/api/agents/$WRITER"

# 确认回到基线
curl -s -H "$AUTH" "$API/api/agents" | python3 -c "
import json,sys; print(len(json.load(sys.stdin)), 'agents')"
```

---

## 验收清单

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 团队成员下限 | <2 成员被拒（400） |
| 2 | 目标唯一性 | 新目标归档旧目标 |
| 3 | 驱动启动 | runtime 进入 `running`/`autopilot` |
| 4 | 自动拆解 | 事件账本出现 `task.changed` + `host` actor |
| 5 | 派工 | `coordinator` 把任务置 `assigned`/`running` |
| 6 | 成员回叫 | `member.replied` 出现，wait 变 `settled` |
| 7 | 审批分流 | 无 external_ref 走 API；有则 409 |
| 8 | 暂停/恢复/完成/停止 | 四种状态迁移正确，stop 清空 open 任务 |
| 9 | 雇用 | `autonomy_enabled=1` + runtime autopilot |
| 10 | 团队不可设 autonomy | 400 |
| 11 | 岗位策略校验 | 时间窗倒置 422 |
| 12 | 默认指派 | 不传 assignee → 落到自己 |
| 13 | 自执行 | 任务推进到 done |
| 14 | **自执行记账** | `usage_log` 有 `self_execute`，`budget_used` 同步 |
| 15 | 预算闸门 | 耗尽后新任务停在 `proposed` |
| 16 | 收件箱 | 报告 `budget` / `failed` / `overdue` |
| 17 | 验收 | 驳回 → `proposed`，记录 reviewer + comment |
| 18 | KPI | 任务计数、token、remaining 一致 |
| 19 | 状态机 | 非法流转 400、CAS 冲突 409 |
| 20 | 重启恢复 | 在途 wait 回收、审批卡仍可答 |
| 21 | 权限 | 非 owner 403 |
| 22 | 跨团队隔离 | 外团队 goal 被拒 |
| 23 | 清理 | agent 数回到基线，无残留任务 |

---

## 已知限制（不是缺陷）

| 现象 | 原因 |
|---|---|
| 闸门是**滞后**的 | `budget_used` 在回合结束后累加，拦的是下一轮；单轮可超支 |
| `escalate_to` 不生效 | 字段只存储，未接入 HITL 推送路由 |
| 策略无 UI | 岗位参数只能通过 `PUT /autonomy-policy` 设置 |
| 团队 roster 不可 SQL 查 | 存在 host workspace 的 `.octop/manifest.json` |
| 数字员工可能"过度发挥" | 给定宽泛任务时它会自建子任务；用 `responsible_scope` 收窄职责 |
