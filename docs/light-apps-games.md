# MiniApp 外部游戏接入

Status: implemented
Date: 2026-11-07

MiniApp 里的游戏入口可以打开**独立域名上的游戏页面**，并在平台侧栏里运行。
游戏写入的数据回落到 MiniApp 自己的表里，游戏关闭后 MiniApp 自动刷新。

这不是把游戏塞进 MiniApp 的沙箱 iframe： MiniApp iframe 的 CSP 只允许单文件、无外部网络；
游戏页在侧栏里作为独立、受信任的页面运行，二者通过一次性会话连接。

```
MiniApp iframe (sandbox)
  │ octopApp.launchGame("duck")
  ▼
Dashboard 父页面
  │ POST /api/apps/{app_id}/game-sessions  {game_id}
  ▼
后端校验准入 + 生成一次性 token
  │ {session_id, url, token, expires_at, sandbox}
  ▼
侧栏 Game 标签页
  │ iframe src = <url>#token=…&session_id=…&api=…
  ▼
游戏页运行，结束时 POST <api>/game-sessions/<token>/results
  ▼
后端按行写回 MiniApp 表
  ▼
MiniApp 重新加载 → KPI / 台账更新
```

## 1. 配置游戏白名单

游戏只能来自 `config.json` 的白名单， MiniApp 不能传任意 URL。

```json
{
  "games": [
    {
      "id": "duck",
      "name": "小鸭养成",
      "url": "https://games.example.com/duck/index.html"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `[a-z][a-z0-9_-]{0,63}`， MiniApp 用它启动游戏 |
| `name` | 否 | 侧栏标签页标题；缺省等于 `id` |
| `url` | 是 | 游戏入口，必须是 http(s) |
| `origin` | 否 | 仅校验用；必须等于 `url` 的 `scheme://host[:port]`，由后端自动推导 |
| `sandbox` | 否 | iframe sandbox 策略；默认含 `allow-scripts`、`allow-same-origin`、`allow-forms`、`allow-popups`、`allow-modals`、`allow-downloads` |

游戏域名会自动加入后端 CORS 允许列表，因此游戏页可以直接调用结果写入接口。
配置修改后需要重启 `octop run`（配置在启动时加载）。

## 2. MiniApp 侧启动

```js
const session = await octopApp.launchGame("duck");
// { sessionId: "game_…", status: "opened" }
```

`launchGame` 会：

1. 调用 `POST /api/apps/{app_id}/game-sessions`；
2. 在对话侧栏新增（或重启）该应用的 Game 标签页；
3. 返回会话 id。它不会等到游戏结束。

要求调用者对应用至少有 `edit_data` 权限——游戏结果会写进应用的表。
没有权限时接口返回 403。

## 3. 游戏页契约

游戏页通过 URL fragment 收到会话参数（fragment 不会发送给游戏服务器，
也不会出现在 Octop 的访问日志里）：

```js
const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("token");
const sessionId = params.get("session_id");
const apiBase = params.get("api"); // 例如 https://octop.example.com/api
```

游戏结束时提交结果：

```ts
POST `${apiBase}/game-sessions/${token}/results`
Content-Type: application/json

{
  "session_id": sessionId,
  "operations": [
    {
      "table": "main",
      "mode": "append",
      "rows": [{ "title": "喂食", "status": "done" }]
    },
    {
      "table": "main",
      "mode": "upsert",
      "upsert_key": ["duck_id"],
      "rows": [{ "duck_id": "duck_1", "level": "3", "coins": "120" }]
    }
  ]
}
```

| 规则 | 说明 |
|---|---|
| `table` | 目标表，默认 `main` |
| `mode: "append"` | 追加行；同一个会话重复提交不会重复写入（按 `session_id + 操作序号 + 行序号` 幂等） |
| `mode: "upsert"` | 按 `upsert_key` 匹配已有行并更新；找不到则追加。key 值有空串时退化为追加 |
| 单次上限 | 最多 10 个 operation、每个 operation 最多 1000 行 |
| 匹配不到表/列 | 返回 400 `APP_ROW_INVALID` |
| 权限 | 写入时重新校验 `edit_data`，权限被撤销后旧 token 也写不进去 |

响应：

```json
{
  "session_id": "game_…",
  "imported": 2,
  "updated": 1,
  "tables": { "main": 3 }
}
```

token 默认 2 小时有效，绑定 `app_id + user_id + game_id + session_id`。
它只能写结果，不能读应用数据，也不是 dashboard JWT。

## 4. 通知 MiniApp 刷新

游戏页完成一次结果写入后，可以通知父页面：

```js
parent.postMessage(
  { source: "octop-game", type: "session-finished", session_id: sessionId },
  "*"
);
```

- `session-finished`：游戏结束。侧栏保留标签页， MiniApp 重新加载数据。
- `result-written`：只写入了部分结果，游戏继续。 MiniApp 也会刷新数据，但不会关闭游戏。
- 关闭 Game 标签页等价于 `session-finished`。

即使游戏不发消息，关闭标签页时 MiniApp 也会刷新，所以游戏页不接 postMessage 也能工作。

## 5. 安全边界

- 游戏 URL 必须来自 `config.json` 白名单； MiniApp 只能传 `game_id`。
- 游戏运行在独立 origin，不持有 dashboard JWT；token 只放在 fragment。
- 侧栏加载外部游戏时使用配置里的 sandbox 策略，不继承 MiniApp 的 `APP_CSP`。
- 若把游戏配在与 dashboard 同一个 origin，前端会去掉 sandbox 里的
  `allow-same-origin`，避免游戏页读到 dashboard 存在 localStorage 的访问令牌。
- 结果接口免 JWT（token 即凭证），但仍走应用 ACL 和审计日志
  （`miniapp.game_session` / `miniapp.game_result`）。
- 公开分享链接（`/p/{token}`）是只读的，`launchGame` 会直接拒绝。
