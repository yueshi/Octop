# 新产品品牌定制清单（基于 Octop 白标改造）

> 状态：调研完成，待决策（2026-10-24）
>
> 目标：基于 Octop 定制一个全新 Agent 产品，第一步落地全新品牌 VI。
> 本文是品牌触点的完整盘点：需定制内容、对应修改位置、以及必须先拍板的决策。
>
> 调研方式：三路并行审计 —— Python 后端（`src/octop/`）、React 前端（`dashboard/`）、
> 仓库与发布层（README / docs / CI / 打包 / 测试）。行号以当前 `feature/light-apps` 分支为准。

---

## 1. 先决决策（决定工程量级，先于一切改动）

| # | 决策 | 选项与后果 |
|---|------|-----------|
| D1 | **运行时身份是否改名**：`octop` CLI 命令、`OCTOP_*` 环境变量、`~/.octop` 数据根、`octop.service`、wheel/PyPI 名 | **A 仅换肤**（推荐第一步）：保留内部 `octop` 标识，只改用户可见 VI 与文案 → 代码约 3–5 人日。<br>**B 完全改名**：`src/octop` 包（~550 个文件的 import）、523 个测试文件、全部部署迁移，且 `octop-harness` / `octop-memory` / `octop-gateway` / `octop-browser` 四个上游依赖包必须一并 fork 改名 → 约 15–25 人日 + 迁移风险。PyPI 项目名不可回收，B 是不可逆决策。 |
| D2 | **上游四个 `octop-*` 依赖包**是否 fork 换名 | 仅 D1 选 B 时相关；这些包名出现在 `pyproject.toml:24-36` 与专家模板文案里 |
| D3 | **对外渠道**：GitHub org（现 `TencentCloud/Octop`）、GHCR 镜像路径、Docker Hub、PyPI、COS `octop/release/` 前缀、帮助/反馈链接（`AvatarDropdown.tsx:58` → `github.com/TencentCloud/Octop`） | 纯品牌决策，但涉及 CI 工作流与发布脚本联动 |

> **VI 设计（本报告第 3 节）与 D1 无耦合**，可立即启动；D1 只影响第 5–7 节的改动范围。

---

## 2. 品牌面四层总览

```
┌─ L1 VI 视觉资产 ──── logo/吉祥物/主题色/文档图 —— 设计团队交付，代码侧 4 个收敛文件
├─ L2 品牌文案 ─────── ~135 处前端 i18n + ~30 处后端 i18n + API 文案 + 专家模板（~366 处）
├─ L3 内部标识 ─────── 包路径 / 类名 / CSS prefix —— 建议保留（用户不可见）
└─ L4 运行时身份 ──── CLI/env/数据目录/线协议头 —— 改名即破坏已部署实例（D1=B 才动）
```

---

## 3. L1 — VI 视觉资产（设计交付物 → 代码落点）

### 3.1 设计团队需交付的资产包

| 资产 | 规格 / 用途 | 替换位置 |
|------|------------|---------|
| favicon | SVG 方形 mark（现 33×33） | `dashboard/public/favico.svg`（`index.html:5-6`） |
| apple-touch-icon | 180×180 PNG | `dashboard/public/apple-touch-icon.png` |
| PWA 图标 | 192×192 + 512×512（512 maskable） | `dashboard/public/pwa-192.png`、`pwa-512.png`；**注意** 192 兼作侧栏收起态图标（`Sidebar.tsx:512`） |
| 水平 wordmark | 深/浅两版 PNG（渲染高 36–48px，宽 ≤260px） | `public/logo_horizontal_dark.png` / `_white.png`（`Sidebar.tsx:443-444`、`Header.tsx:81`） |
| 垂直/方形 logo | 深/浅两版 SVG（boot 启动屏 60×60、离线页 72×72） | `public/logo_vertical_dark.svg` / `_white.svg`（`index.html:282,289`、`offline.html:57`） |
| **吉祥物系统**（章鱼 IP 形象，新产品需全新形象） | 空态 PNG（现 1.6MB）、聊天入场 webp+webm 动画 ×2、任务态 PNG | `public/octop-mascot-empty.png`、`-peek.webp/.webm`、`-type.webp/.webm`、`-tasks.png`；引用点：`src/assets/mascot.ts:5`、`src/components/EmptyState/OctopEmptyMascot.tsx`、`src/pages/Chat/components/WelcomeScreen.tsx:8-9`、`ThinkingBubble.tsx:15`、`StreamConnectingIndicator/index.tsx:4` |
| 渠道图标 | Octop 自有渠道 icon | `src/assets/channels/octop.svg`（`pages/Agent/Channels/components/constants.ts:9,125`） |
| 专家头像（可选） | ~45 个 SVG，可保留或按新 VI 重绘 | `public/experts/avatars/*.svg` |
| MBTI 图标（可选） | 16 个 SVG，风格适配 | `public/assets/mbti/*.svg` |
| 文档资产 | README banner（中英）、产品截图、部署按钮、`octop-logo.svg` | `docs/assets/`（25 个文件，见 §8） |

### 3.2 主题色 —— 代码侧只有 4 个收敛文件

| 文件 | 内容 |
|------|------|
| `dashboard/src/styles/themePalettes.ts` | 8 套调色板的 antd token（`ANTD_BRAND_TOKENS`，L73-245）+ 色板（`PALETTE_SWATCH` L46-56）；**默认玫瑰色** `#E85D75`（light，L79-83）/ `#F08B9A`（dark，L85-96）；`DEFAULT_PALETTE="rose"`（L28） |
| `dashboard/src/styles/theme-vars.css` | 全部 `--fn-*` CSS 变量：品牌色 `--fn-color-brand`（light L71 / dark L255）、`--fn-logo-color`（L53）、7 个按 palette 的覆盖块（L377-891） |
| `dashboard/src/App.tsx` | antd `themeConfig` 中性色 token（L66-120：`colorBgBase:#0f1117` 等） |
| `dashboard/index.html` | boot 启动屏（内联 CSS：转圈颜色 `#e85d75`/`#f08b9a` L136-142、两个 logo `<img>` L282,289）、`theme-color` meta（L12-21） |

零散色值：`public/offline.html:36,51,58-59`、`public/manifest.json:10-11`（`background_color` / `theme_color`）。

**利好**：前端已内建 **8 套调色板 + 用户自定义色**机制（`themePalettes.ts` + `ThemeContext.tsx:132-140` 运行时注入）。新 VI 最省路径 = 把新品牌色做成新默认 palette（一行 `DEFAULT_PALETTE`），再清理默认 rose 硬编码。

**字体**：应用使用系统字体栈（`index.html:113-114`），无 webfont。若新 VI 要求专属字体需新增 `@font-face`；现有两个未引用的遗留字体文件（`public/fonts/FSPixelSansUnicode-Regular.ttf`、`src/assets/fonts/ark-pixel-*.woff2`）可顺手删除。

---

## 4. L2 — 品牌文案（用户可见字符串）

### 4.1 前端（`dashboard/`）

| 位置 | 内容 | 动作 |
|------|------|------|
| `index.html:32` / `:29` | `<title>Octop</title>`、apple-mobile-web-app-title | 改 |
| `public/manifest.json:3-5` | PWA `name` / `short_name` / description | 改 |
| `public/offline.html` | 标题、logo、文案、颜色 | 改 |
| `src/locales/en.json`（135 处）/ `zh.json`（134 处） | 产品名散布在 ~135 个 key 中，**无 `app.name` 聚合键** | 见下 |
| `Sidebar.tsx:513`、`Header.tsx:82`、`Login/index.tsx:276`、`Setup/index.tsx:265`、`WelcomeScreen.tsx:83` | logo `alt` / `aria-label` | 改 |
| `AvatarDropdown.tsx:58` | 帮助/反馈 `GITHUB_URL`（→ TencentCloud/Octop） | D3 |
| `channels/constants.ts:109,161` | `OctopBot` 渠道名、凭据获取 URL | 改 |

i18n 重点 key（en/zh 同路径）：`app.pageTitle`、`common.askOctopHint`、`setupWizard.subtitle`、`errors.BACKUP_SCHEMA_INCOMPATIBLE / BRIDGE_* / ACP_* / SSO_* / LDAP_* / LOCAL_DOWNLOAD_UNAVAILABLE`、`pwa.*`、`login.forgotPassword*`（内嵌 `octop user passwd` CLI 路径 —— 与 D1 联动）、`channels.label_octopbot`、`models.octop-assistant`、`browserEnv.manualCommand`（内嵌 `octop install-browsers`）、`settings.envVars.description`（`~/.octop/env` —— D1）。

**建议**：趁改造引入 `app.name` 聚合键 + 前端品牌常量模块，把 135 处收敛为引用 —— 这是本次最值得做的结构改进。

### 4.2 后端（`src/octop/`）

| 类别 | 位置 | 内容 |
|------|------|------|
| API 文档 | `api/app.py:110,315`；`api/openapi_meta.py:13,90,102,118` | FastAPI 标题 "Octop API"、Scalar 介绍全文、tag 描述 |
| 首启横幅 | `infra/server.py:739-746` | 控制台横幅 + `~/octop-login.txt` 提示 |
| CLI | `cli/main.py:53,91,121`、`cli/registry.py:8-36`、`cli/repl/render.py:325`、`cli/commands/{backup,plugin,clean,config,captcha,run}.py` | 版本行、帮助文本、REPL banner |
| 路由文案 | `api/routers/{update.py:324,436, bridge.py, agent_mcp.py:33-89, auth_ldap.py:73-82, envs.py:85}` | OpenAPI summary/description 中的 "Octop" |
| IM bot 问候 | `infra/gateway/bot_creators/feishu_bot_creator.py:52-62` | 发送到飞书/ Lark 平台的机器人问候语与描述（对外可见） |
| 后端 i18n | `src/octop/i18n/en.json` / `zh.json` ~30 处 | `slash.fields.octop:190`、`skills.octop-assistant:687-688`、`errors.BRIDGE_*` 等、`light_apps.system_prompt:792` |
| systemd | `infra/setup/service.py:404` | `Description=Octop AI Server`（:34-36 服务名/单元名见 L4） |
| 版权 | `LICENSE` | `Copyright (c) 2026 Octop` → 新主体 |

### 4.3 专家模板（随产品分发的 Agent 人格内容）

- 重灾区：`src/octop/infra/agents/experts/library/general-assistant/skills/octop-assistant/SKILL.md` —— **单文件 180 处**：frontmatter `name: octop-assistant`、标语「更聪明，更懂你」、产品介绍整节。skill id 被 i18n `skills.octop-assistant` 引用，改名需同步。
- 其余 62 个文件 / ~186 处：`clinical-learning-subscription`（13 文件）、`office-automation`、`business-operations`、`meituan-living-assistant` 等，多为 `~/.octop`、`octop run`、`octop-memory` 引用（与 D1/D2 联动）。
- 另有 `builtin_skills/skill-manager/SKILL.md`、`experts/manifest_generator_skill.md` 等 10+ 个运行时 .py/.md 提及。

---

## 5. L3 — 内部技术标识（建议保留；D1=B 才动）

| 标识 | 位置 | 说明 |
|------|------|------|
| 包路径 `src/octop` + `from octop.…` | ~550 个文件 | 用户不可见；改名是纯机械但量大 |
| `OctopServer` / `OctopConfig` / `OctopError` 类名；`app.state.octop_server`；`request.state.octop_user` | `infra/server.py:1`、`api/app.py:117` 等 | 内部 DI 键 |
| antd `prefixCls="octop"` | `App.tsx:125` | 所有 antd class 变 `octop-*`；改名需同步自定义 `.less` 选择器，不建议动 |
| `octop:ui-palette` 等 localStorage 键 | `themePalettes.ts:40` 等 | 用户浏览器存量，改名丢设置 |
| 依赖包名 `octop-harness/memory/gateway/browser` | `pyproject.toml:24-36` | D2 |
| DB schema | `infra/db/migrations/` | **全部品牌中性，无需改**（好消息） |
| JWT | `infra/auth/tokens.py:21-35` | payload 无 issuer/aud，**无品牌信息**（好消息） |

---

## 6. L4 — 运行时身份（D1=B 时的破坏性改名清单）

> 这一节的内容**同时是** D1=A 时文档里必须如实保留、且新运营文档要正确教用户的「产品内部名」。

| 类别 | 标识 | 破坏点 |
|------|------|--------|
| 数据根 | `~/.octop`（`infra/utils/paths.py:16-20`，`OCTOP_HOME`） | 所有用户数据、`config.json`、备份、插件、Agent 工作区；工作区内层 `.octop/`（auth、memory.sqlite、skills） |
| 环境变量 | `OCTOP_*` 约 20+ 个（`config.py:23-30,278-613` 等） | docker/`~/.octop/env`/部署脚本全量引用 |
| CLI | `octop` 命令（`pyproject.toml:97-98`） | 文档、脚本、用户肌肉记忆 |
| 服务 | `octop.service` / launchd `octop`（`infra/setup/service.py:34-36`） | 需卸载重装 |
| 首启令牌 | `~/octop-login.txt`（`infra/setup/password_file.py:9`） | 向后兼容窗口 |
| 默认 DB | sqlite `octop.db`、PG 库/用户 `octop`（`config.py:44,47-48`） | 已装实例保留旧名，新默认发散 → 需别名/迁移 |
| 线协议 | `X-Octop-Access-Token`（`api/deps.py:27`）、`X-Octop-Agent-Id`（`bridge_proxy.py:202`、`mbti.py`）、cookie `octop_sso_state`（`api/common/sso_cookie.py:8`） | Dashboard 与 bridge 对端必须同步改，混合版本断联 |
| 备份/状态文件 | `octop-(auto-)backup-` 前缀、`octop-feishu-bot*` 状态文件、`.octop-old` 升级暂存、证书 CN `octop-self-signed` | 恢复代码与既有档案假设这些名字 |
| 应用内 SDK | `window.octopApp`（`i18n light_apps.system_prompt`、已部署 HTML 应用） | JS API 契约，改名需双名兼容期 |
| CI/发布 | GHCR `ghcr.io/tencentcloud/octop`、Docker Hub、PyPI `octop`（不可回收）、COS `octop/release/`、fnos `manifest` `appname=octop`、`Makefile` 帮助、`scripts/install*` | §1 D3 |

---

## 7. 测试影响（改名/换文案的回归面）

- 523 个测试文件含 `octop`（多为 import 路径，随包改名）；断言级匹配 ~344 处。
- 密集文件：`tests/unit/infra/setup/test_self_update.py`（151）、`tests/unit/agents/test_agent_manager.py`（110）、`tests/unit/test_fnos_docker_persist.py`（89）、`test_connectors.py`（86）、`test_docker_entrypoint.py`（80）、`test_logging.py`（73）等。
- 断言的字面量多为 L4 标识（cookie、env、`octop.db`、`~/.octop`）—— **D1=A 时基本不动；D1=B 时与生产代码同步改**。
- i18n 键位对齐测试（`tests/unit/i18n/`）会在增删 key 时自动把关。

---

## 8. 仓库与文档层

| 范围 | 内容 | 动作 |
|------|------|------|
| 根文档 | `README.md`（119 处）/ `README_CN.md`（116）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`AGENTS.md`（54） | 重写（CHANGELOG 建议 forward-only） |
| `docs/` | 全部产品化文档；重度：`cli.md`（123）、`configuration.md`（87）、`user-guide.{md,html}`（66/67）、`ldap.md`（50）；文件名带品牌的：`octop-introduction.html`、`octop-ui-payload-offload.md` | 重写；`octop run` / `~/.octop` 引用随 D1 |
| 截图 | `docs/assets/` 25 个文件：README banner×2、聊天截图×2、部署按钮×2、UI 截图 15 张（setup-01…use-08）、专家图×2、`octop-logo.svg` | 新 VI 定稿后**统一重拍** |
| CI | `octop-desktop.yml`（产物名 `Octop-*`）、`docker-publish.yml`、`release.yml`（PyPI）、`fnos-build-fpk.yml`、`upload-release-cos.yml` | 随 D1/D3 |
| 发布脚本 | `.cursor/skills/publish/SKILL.md`、`docker/*`（compose 服务名/env/镜像）、`scripts/install*` | 随 D1/D3 |
| GitHub | issue 模板 ×3、仓库 org | 重写 / D3 |
| `docs/slides/light-apps-overview.html` | 本会话产物，34 处品牌名 | 视需要重制 |

---

## 9. 建议实施顺序

| 阶段 | 内容 | 依赖 | 工作量 |
|------|------|------|--------|
| 0 | 拍板 D1/D2/D3 | — | 决策会 |
| 1 | **VI 设计包**：logo 套件、吉祥物 4 态（含 2 组 webp/webm 动画）、色板、字体、文档模板 | — | 设计侧并行 |
| 2A | 换肤落地：§3.2 四个收敛文件 + 资产替换 + 引入 `app.name` 键 | 阶段 1 | 2–3 人日 |
| 2B | 文案清扫：前端 135 处 i18n + 后端 30 处 + CLI/banner/bot + `octop-assistant` skill 重写 | 2A | 2–3 人日 |
| 3 | 文档/README/截图重制 + CI/发布渠道 | 2B + D3 | 3–5 人日 |
| 4 | （仅 D1=B）运行时改名 + 迁移（env 别名、home 迁移脚本、header 双读、service 重装、包/import/测试全量改） | 决策后单独立项 | 15–25 人日 |

阶段 1+2A+2B 完成即达成「全新品牌 VI」第一步 —— 用户可见面上产品已是新品牌，内部仍跑 `octop` 运行时。

---

## 10. 明确不做（本次）

- 不改 DB schema、JWT 结构（已确认品牌中性）。
- 不做运行时白标配置化（如 `BRAND_NAME` env 注入）—— 硬编码换名已满足单产品定制；多租户白标是另一个量级的产品能力，需要时另立设计。
- 不动 `src/octop/dashboard/` 构建产物 —— 所有前端改动经 `dashboard/` 源码 + `make build-frontend`。
