# AI 视频生产与经营系统（抖音电商）方案记录

> 状态：设计记录 / 待确认  
> 日期：2026-11-11  
> 场景：一家抖音视频卖货公司，现有流程为“拆解爆款提示词 → 替换产品图/改提示词 → 生成新视频 → 剪辑合成”  
> 目标：人提需求 + 产品图，AI 接管生产；具备原创编导能力；多店铺订单/成本可视化  
> 技术底座：Octop（octop-harness + Agents/Experts + Skills + MCP + Cron/Events + DB + Dashboard/MiniApp）
>
> 本文是设计记录，不是当前实现承诺；实现状态以代码与 tests 为准。

## 1. 评审结论

1. **R1 生产自动化可落地**，但“AI 接管”应定义为“AI 完成 70–90% 生产，人做方向选择、终审、发布”，不是 100% 无人；Phase 1 目标单条人工时间 ≤15 分钟。
2. **R2 原创编导可落地为“差异化生产系统”**，不能保证爆款；核心是把创意源从“爆款反推”换成“产品事实 + 人群洞察 + 叙事结构 + 人工策展 + 数据反馈”。
3. **R3 经营可视化可落地**：订单/售后可同步，成本需内部维护，视频→订单精确归因受平台开放能力限制，只能做优惠码/专属 SKU/时间窗的代理归因。
4. 现有“爆款复刻”不要丢掉，降级为**可选模式/结构分析工具**；默认走原创模式，避免同质化。
5. **主链路用 FFmpeg 确定性渲染**；剪映只做人工精修/草稿交付。视频生成用 Octop 原生 `generate_video`，不要先造 `jimeng-mcp`。
6. 必须补 **AIGC 标识与广告法合规闸门**；这是方案原稿漏掉、且会直接影响上架的部分。

## 2. 关键事实与硬约束

### 2.1 外部能力

| 主题 | 事实 | 对方案的影响 |
|---|---|---|
| 即梦低层 API | 视觉智能 `CVSync2AsyncSubmitTask`/`CVSync2AsyncGetResult`，`req_key`、`image_urls`、`frames` 121/241（5s/10s）、`aspect_ratio` 仅文生视频；任务约 12h 过期，视频 URL 约 1h 有效；图生视频通常 1–2 张图 | 适合逐镜头控制；生成后要及时下载 |
| 小云雀 Agent 2.0 | 另一条接口：`img_url_list` ≤50、`duration` 可选 ~15s/30s/40–60s/1min+、60s 平均约 10 分钟 | 适合整段故事板探索，不适合精确逐镜控制；不要与低层 API 混为一个 adapter |
| 即梦并发/成本 | 免费并发 1、付费 2；HTTP 429 + `50429`（QPS）/`50430`（并发）；非 VIP 排队实测可达 15h40m；Seedance 2.5 并发 1、RPM 约 60；即梦 3.0 720p ¥0.28/s、1080p ¥0.63/s、3.0 Pro ¥1/s；Seedance 2.5 720p 约 ¥1.5/s、1080p 约 ¥3.7/s | 单账号不能批量；必须队列 + 多 provider + 预算硬闸门 |
| 剪映 | 无公开官方 API；6.0+ `draft_content.json` AES 加密，模板模式 ≤5.9；批量导出 UI 自动化仅 Windows，7+ 失效；社区方案止步于草稿，导出要人工 | 不能作为主渲染；FFmpeg 主链路，剪映可选 |
| CapCut Mate / CapCutAPI / cutsdk | CapCut Mate `/openapi/capcut-mate/v1/`，托管 `gen_video` ¥0.3/分钟，自部署导出 Windows-only；CapCutAPI 有 HTTP + MCP（`:9000/mcp`），9 HTTP + 11 MCP 工具，只出草稿；cutsdk 1.1.8 支持 `createAndRenderDraft`，云渲染需 `CUT_API_TOKEN`，单 Spec 样式批量生效 | 可选；锁版本；评估 ToS 与素材数据合规 |
| 抖店 | 仅企业/个体户；审核约 1–3 个工作日；OAuth2.0；MD5/HMAC-SHA256 签名；IP 白名单；应用+接口双限流；订单列表 100 条/页、单次查询 ≤90 天；测试店 5 万次/天；限流码 10005 | 账号审核是关键路径；同步要增量游标 + 令牌桶 + 幂等 upsert |
| `mcp-cn-commerce` | 抖店仅真实联调订单列表/详情、售后列表/详情；商品/退款/店铺信息未验证；千川仅广告主信息/余额；项目自述 `live_verified=false` | 不能作生产依赖；自建官方签名 client |
| 发布 | 抖音“代替用户发布内容”能力 2024-07-20 起仅对党政机关/事业单位开放；普通开发者一般拿不到；替代是移动 SDK/H5 分享/人工上传 | 保留人工发布；有企业号/服务商资质再评估 |
| 归因 | 开放平台未明确暴露视频→订单精确归因；罗盘 T+1，UI 有“全域成交归因 3.0”，API 不明确 | 用优惠码/专属 SKU/时间窗代理；看板标注口径，不承诺精确到单条视频 |
| AIGC 标识 | 《人工智能生成合成内容标识办法》2025-09-01 生效；显式标识要求视频文字高度 ≥ 最短边 5%、持续 ≥2s；隐式标识写入元数据；抖音自动标记，抖音电商治理 AI 虚假商品展示/虚拟人冒充/未声明 AI | 成片 QA 必含 AIGC 标识；人工复核商品真实性与广告法 |
| MCP 版本 | 2025-11-25 增加 Tasks（长任务）；2026-07-28 改为无状态、移除 session；Octop 依赖 `mcp>=1.9,<2`，当前 1.28.1 | MCP server 锁 2025-06-18/2025-11-25 兼容；不要依赖 2026-07-28 新特性 |

### 2.2 Octop 能力事实

- Octop 已是 Harness：`octop-harness` 是 Python 库依赖，`HarnessAgent` 进程内运行；不需要自建运行时。
- 原生媒体生成：`generate_video` 支持 `prompt`、`first_frame`、`last_frame`、`reference_images`、`duration`（4–15）、`aspect_ratio`、`resolution`、`generate_audio`、`seed`、`watermark`（`octop_harness/builtin/tools/media_generation.py:83-131`）。
- 多 provider：Volcengine Seedance 2.5、DashScope Wan 3.0、MiniMax H3（`src/octop/infra/agents/settings/media_generation.py:64-108`）；Settings → Media Generation 可配置默认路由。
- 媒体生成是实例级配置，不能按 Agent 选 provider；per-shot 路由要在 pipeline 里用 `MediaManager.submit_video(provider_id=...)`（`octop_harness/media/manager.py:182-192`）。
- 默认媒体 job store 是内存，重启丢任务；需要 `video_jobs` 表 + worker。
- 自定义 MCP 支持 `streamable_http`/`stdio`，loopback/LAN 允许 HTTP（`src/octop/infra/connectors/custom_mcp.py:1,27,118-166`）；`stdio` 是本地命令执行面，生产要加管理员门禁。
- miniapp 数据源只支持进程内 gateway connector（`src/octop/infra/apps/sources.py:1-49`）；iframe 沙箱无 JWT，`asset` 返回 data URL；行 API 最大 500 行、每表 2 万行/5MB、最多 12 表，无 filter/sort/join/服务端聚合。适合摘要，不适合重 BI。
- `generate_video`/`generate_image` 不在 `ARTIFACT_TOOL_BASES`（`src/octop/infra/agents/threads/artifact.py:25-36`），生成视频不会自动进文件 dock；需补 middleware/扩展。
- `/media/preview`、`/workspace/download` 整文件读入内存，无 Range/流式；大视频需新增流式端点或对象存储签名 URL。
- 当前 schema v30（`tests/unit/db/test_db_pool.py:113`）；新表用 031。
- Teams 已有预算闸门/人工验收/inbox/metrics 基础版，但仍缺独立 Verifier、确定性验收、返工上限、容量路由、多进程队列；视频流水线用确定性 pipeline，不把 Teams 当全自动骨干。

## 3. 目标架构

```
┌─────────────────────────────────────────────────────────────┐
│ 交互层                                                       │
│  Chat（需求单 + 产品图上传） · React 生产台/审片台 · 经营看板 │
│  miniapp（聊天内摘要/轻量卡片，不做重 BI）                    │
├─────────────────────────────────────────────────────────────┤
│ 编排层（bounded agentic）                                    │
│  VideoPipeline 状态机（确定性）                               │
│  编导 Agent · 制作/异常 Agent · 独立质检 Agent · 经营分析 Agent│
├─────────────────────────────────────────────────────────────┤
│ Harness / Skills 层                                          │
│  octop-harness（循环、工具、上下文、HITL、模型重试、配额）     │
│  Skills：director / assembly / qa / commerce-analysis        │
├─────────────────────────────────────────────────────────────┤
│ 能力层                                                       │
│  Octop 原生 generate_image/generate_video                    │
│  （Seedance 2.5 / Wan 3.0 / MiniMax H3 多通道）               │
│  FFmpeg · TTS/STT（本地确定性工序）                           │
│  MCP：抖店/千川、剪映（可选）、即梦特殊接口（可选）           │
├─────────────────────────────────────────────────────────────┤
│ 数据层                                                       │
│  video_projects/shots/jobs/renders/performance               │
│  shops/orders/refunds/costs/ad_spend/daily_metrics           │
│  工作区/COS/S3 产物                                           │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 设计原则

1. **确定性优先**：生成任务提交、轮询、下载、装配、QA、ETL 都是代码；Agent 只做创意、例外处理、独立评审。
2. **Octop 原生优先**：视频生成用 `generate_video`，TTS 用 `infra/voice`，MCP 只接外部服务。
3. **人工闸门明确**：方向选择、终审、发布是产品的一部分，不是临时兜底。
4. **合规前置**：AIGC 标识、广告法、抖音电商 AI 规则在分镜和 QA 两处拦截。
5. **成本可归集**：每个镜头记录 provider/model/时长/重试/成本，项目级和日级预算硬闸门。

### 3.2 组件映射

| 能力 | Octop 落点 | 复用/新建 |
|---|---|---|
| Agent 运行时 | `octop-harness`，`HarnessAgent` | 复用 |
| 编导能力 | `video-production` 专家 + `director` skill | 新建 |
| 视频/图片生成 | 原生 `generate_video`/`generate_image` | 复用 |
| 多 provider 路由 | `MediaManager.submit_video(provider_id=...)` + Settings → Media Generation | 复用 + pipeline 代码 |
| 长任务 | `video_jobs` 表 + `VideoPipelineWorker`（仿 `infra/apps/scheduler.py` 的租约） | 新建 |
| 装配 | FFmpeg skill/tool | 新建 |
| 质检 | `video-qa` skill + 确定性检查 | 新建 |
| 语音 | `infra/voice` TTS/STT | 复用 |
| 抖店数据 | 进程内 gateway connector + `CommerceSyncWorker` | 新建 |
| 多店铺看板 | React 页面 + Recharts + `/api/commerce` | 新建 |
| MCP 外部能力 | 自定义 `streamable_http`/`stdio` | 复用 |
| 产物存储 | `BackendWorkspace` + COS/S3 | 复用 |
| 数据库 | SQLite/Postgres + migrations/repos | 复用 |
| 定时/事件 | `AppSourceScheduler`、cron、external events | 复用 |
| miniapp | 沙箱 iframe + app tables + 定时数据源 | 复用（有限场景） |

### 3.3 视频流水线

状态机：

```
intake → brief_ready → storyboard_draft → approved
→ generating → generated → assembling → assembled
→ qa_passed → review → published
```

1. **Intake**：Chat 需求单 + 产品图，结构化字段：品类、卖点、价格、场景、人群、证明、合规红线。
2. **事实校验**：信息不足则拒生成并列出补充项；不拿通用模板填充。
3. **编导**：3 个差异化方向，每个含结构、钩子、情绪、证明、理由；分镜 JSON 过 Schema。
4. **人选方向**：Phase 1 人工选；Phase 2 可基于历史数据自动排序，人只审片。
5. **TTS**：逐句生成口播，按句长对齐镜头时长。
6. **关键帧/生成**：`generate_image` 出关键帧；`MediaManager.submit_video` 逐镜提交，`video_jobs` 持久化，worker 轮询/下载。
7. **装配**：FFmpeg 拼接、产品图 cutaway、字幕、AIGC 标识、响度归一化。
8. **QA**：时长/比例/黑帧/字幕遮挡/音频削波/违规词 OCR/AIGC 标识/产品图存在性。
9. **审片**：人工接受/拒绝，记录原因；可选剪映草稿精修。
10. **发布与反馈**：人工发布；优惠码/专属 SKU/时间窗做代理归因，跑量数据回写编导。

### 3.4 电商数据

核心表：

- 店铺：`shops`（加密 token、状态、授权时间）。
- 订单：`orders`、`order_items`、`refunds`。
- 成本：`product_costs`（商品/包装/物流）、`ad_spend`。
- 汇总：`shop_daily_metrics`、`sku_daily_metrics`。
- 同步：`sync_cursors`、`sync_runs`。

利润口径：

```
结算金额 = 成交金额 − 退款 − 平台佣金 − 达人佣金 − 运费险 − 优惠券分摊
毛利     = 结算金额 − 商品成本 − 包装 − 物流 − 投流分摊
```

看板必须同时展示数据完整度：成本缺失的 SKU、投流未同步的店铺、同步延迟。

归因分层：

- L1 店铺/SKU：可靠。
- L2 优惠码/专属 SKU/时间窗：代理，可解释。
- L3 罗盘短视频 GMV：T+1，需导出/权限。
- L4 精确到单条视频：当前开放能力不保证，不承诺。

## 4. 功能清单

| 需求 | Phase 1（MVP） | Phase 2 | Phase 3 |
|---|---|---|---|
| R1 生产 | 需求单/图片上传、缺信息拦截、3 个方向、分镜 JSON、TTS、逐镜生成、FFmpeg 装配、QA、审片、发布清单、成本记录 | 批量生产、并行镜头、多 provider 路由、自动方向排序、剪映草稿交付（可选） | 模板复用、跨店铺批量、数字人（可选，需合规） |
| R2 原创 | 产品事实提取、结构库 v0.1、差异化校验、盲评、反馈记录 | 评论/问答洞察、结构权重学习、A/B、创意特征库 | 跨品类结构迁移、创意预测（谨慎） |
| R3 经营 | 1–5 店订单/售后同步、成本导入、利润口径、店铺/SKU 看板、日报 | 千川投流、退款原因、库存/物流成本、异常告警 | 优惠码/专属 SKU 归因、罗盘导出、预测 |
| 平台 | 角色/权限、审计、预算、日志、备份、i18n | 多租户/多品牌、SLA、容量规划 | 对外 API、开放平台 |

**Phase 1 明确不做**：全自动发布、精确视频归因、剪映 RPA、全自主多 Agent 团队、复杂 BI 钻取、自动投放。

## 5. 落地步骤

### Phase 0：立项与外部依赖（1–2 周）

| 任务 | 产出 | 验证 |
|---|---|---|
| 业务盘点：选 1 家店、3 个产品、10 条需求样本 | 需求单模板、验收 rubric、品牌/合规红线 | 3 个产品各 5–10 张真实图；10 条需求可执行 |
| 账号/资质：火山引擎企业认证、Seedance/即梦配额、抖店自研应用、千川授权、抖音发布资格核查 | 可用密钥/应用/测试店 | 提交 1 个视频任务拿到 URL；测试店拉到 1 笔订单 |
| 技术环境：Linux 服务器、FFmpeg、对象存储、备份 | 可运行环境 | `ffmpeg -version`；上传/下载/预览通过 |
| 合规评审：AIGC 标识、广告法、抖音电商 AI 规则、音乐/voice/肖像授权、剪映 ToS | 合规 checklist | 法务/负责人签字 |
| 基线：按现有流程手工做 3 条 | 时间/成本/质量基线 | 记录每条耗时、生成成本、修改轮次 |

### Phase 1：1 店 3 品 10 条垂直切片（4–6 周）

**Sprint 1（第 1–2 周）：数据模型 + 需求单 + 编导**

- 新增 `infra/video/`、`infra/commerce/` 骨架；031 迁移；repo/service/router 接线。
- Chat 需求单 + 产品图上传到 `inbound/`；结构化字段：品类、卖点、价格、场景、人群、证明、合规红线。
- `director` skill v0.1：结构库 10 种、3 个方向、分镜 JSON Schema、差异化校验。
- 验证：3 个产品 × 3 个方向；validator 拒绝“产品出现在 AI prompt”；人工盲评 ≥2 个方向不同。

**Sprint 2（第 2–4 周）：生成 + 装配**

- Settings → Media Generation 配置 Seedance/Wan/MiniMax；pipeline 按镜头选 provider。
- `video_jobs` 表 + `VideoPipelineWorker`：submit → poll → download → update；失败退避/换模型。
- TTS 逐句生成 → 时长对齐；FFmpeg 装配：拼接、产品图 cutaway、字幕、AIGC 标识、响度。
- 验证：10 条成片；生成成功率 ≥90%；产品标签错误 0；成本在预算内。

**Sprint 3（第 4–5 周）：QA + 审片 + 交付**

- QA：时长/比例/黑帧/字幕遮挡/音频削波/违规词 OCR/AIGC 标识/产品图存在性。
- React 审片页 + Range 流式预览；生成视频入 `threads.artifacts`。
- 发布 checklist：标题、封面、标签、AIGC 声明、商品链接、合规复核。
- 验证：首轮 QA ≥60%；人工接受率 ≥70%；AIGC 100%。

**Sprint 4（第 5–6 周）：评估与加固**

- 统计失败模式（镜头不连贯、手部畸变、字幕遮挡、生成超时），更新 skill/QA 阈值。
- 文档、runbook、成本看板；`make all` 绿。
- 验证：10–15 条成片；单条人工时间 ≤15 分钟；`uv run pytest` + `make all` 通过。

### Phase 2：规模化与原创反馈（6–8 周）

- 3–5 家店，30–50 条/周；批量队列、并发/背压、单日预算硬闸门。
- 多 provider 路由：简单镜头用便宜档，hero shot 用 Seedance 2.5 1080p；失败自动换通道。
- 创意反馈：记录结构/钩子/镜头特征 → 跑量数据；盲评 + 结构权重调整。
- 可选剪映草稿：CapCutAPI/CapCut Mate 生成草稿，人工打开剪映精修；不作为自动出片依赖。
- 验证：首轮 QA ≥75%；单条成本降 30%；人工盲评差异明显；产能达标。

### Phase 3：经营数据与闭环（4–6 周）

- `CommerceSyncWorker`：抖店订单/售后增量同步；消息推送 + 轮询对账；令牌桶/幂等 upsert。
- 成本表：商品/包装/物流/投流；CSV 导入或表单；数据完整度标记。
- 利润口径：结算金额、平台佣金、达人佣金、退款、运费险、优惠券、投流分摊。
- 经营看板：多店总览、店铺/SKU 下钻、退款率、毛利率、投流 ROI、数据质量。
- 经营分析 Agent：每周异常摘要 + 建议；不直接改数据。
- 归因：优惠码/专属 SKU/时间窗；罗盘导出 T+1 校验。
- 验证：同步延迟、对账偏差、每周 ≥3 条可执行异常。

## 6. 验收标准

| 维度 | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| R1 自动化 | 生成成功率 ≥90%；首轮 QA ≥60%；接受率 ≥70%；人工 ≤15 分钟/条 | 首轮 QA ≥75%；成本 −30%；30–50 条/周 | 跨店批量；模板复用 |
| R2 原创 | 3 方向；≥2 个明显不同；无直接复制 | 100 条样本后结构权重可调；盲评差异化 ≥70% | 跨品类结构迁移 |
| R3 经营 | 1–5 店；订单同步；利润口径；店铺/SKU 看板 | 千川/退款原因/异常告警 | 代理归因；罗盘校验 |
| 合规 | AIGC 100%；违规词 0；产品标签错误 0 | 同左 + 法务复核 | 同左 |
| 工程 | `make all` 绿；E2E 测试；无 PII 泄露 | 多实例/备份/监控 | SLA/容量 |

## 7. 风险与应对

| 风险 | 应对 |
|---|---|
| 即梦/Seedance 并发与排队 | 企业配额 + 工单扩容；多 provider（Seedance/Wan/MiniMax）；队列 + 令牌桶 + 日上限 |
| 剪映自动化脆弱 | FFmpeg 主链路；剪映只做人工精修；锁版本；不做 RPA 生产 |
| `mcp-cn-commerce` 未真实联调 | 自建官方签名 client；逐接口验收；不把它当生产依赖 |
| 发布 API 受限 | 人工发布；有企业号/服务商资质再评估 |
| 视频→订单归因不精确 | 优惠码/专属 SKU/时间窗；罗盘 T+1 校验；看板标注口径 |
| AIGC/广告法 | 显式 + 隐式标识；违规词 OCR；商品真实性复核；法务审 |
| 成本失控 | 单条/单日预算硬闸门；按镜头选档；重试上限；成本看板 |
| 数据安全 | token 加密、PII 脱敏、最小权限、保留期；MCP stdio 加管理员门禁 |
| 原创性无法保证 | 定义为“差异化 + 可测试 + 可迭代”；人工策展 + 数据反馈，不承诺爆款 |

## 8. 待确认决策

1. **成片主链路**：FFmpeg 自动合成（推荐）/ 剪映草稿人工导出 / 托管云渲染？
2. **部署环境**：只有 Linux 服务器，还是有 Windows 机器可跑剪映？剪映版本？
3. **规模与预算**：几家店、多少 SKU、月订单量、每天目标条数、时长/分辨率、单条成本上限？
4. **发布与资质**：人工上传可接受吗？是否有企业号/服务商资质？
5. **归因要求**：代理指标（优惠码/专属 SKU/时间窗）够用，还是必须精确到单条视频？是否有罗盘导出权限？

## 9. 参考

### Octop 代码

- 原生媒体生成：`octop_harness/builtin/tools/media_generation.py:83-131`
- 媒体 manager/job：`octop_harness/media/manager.py:27-53,170-245`
- 媒体 provider 配置：`src/octop/infra/agents/settings/media_generation.py:64-108,568-606`
- 自定义 MCP：`src/octop/infra/connectors/custom_mcp.py:1,27,118-166`
- 技能包：`src/octop/infra/skills/skill_packages.py:92-99`
- Cron/worker：`src/octop/infra/cron/manager.py:47-54`、`src/octop/infra/server.py:690-788`
- miniapp 数据源：`src/octop/infra/apps/sources.py:1-49`、`src/octop/infra/apps/scheduler.py:27-94`
- 产物缺口：`src/octop/infra/agents/threads/artifact.py:25-36`
- 大文件预览：`src/octop/api/routers/workspace.py:330-366,427-466`
- 迁移版本：`tests/unit/db/test_db_pool.py:113`（v30）
- 团队 L3：`docs/team-l3-roadmap.md:9-13,28-38,140-250`

### 外部资料

- 即梦官方视频异步 API（社区整理）：<https://github.com/glanderness/beeftv/blob/ca5ea4e50accac8fc81fed4c7816612790fc3c0b/backend/internal/protocol/docs/volcengine-jimeng-video.md>
- 小云雀 Agent 2.0 接口文档：<https://docs.volcengine.com/docs/JimengAI/Xiaoyunque-IntelligentLifeVideoAgent20NoReference-InterfaceDocumentation?lang=zh>
- 即梦视频计费说明：<https://docs.volcengine.com/docs/jimeng-ai/jimeng-ai-video-generation-billing-instructions?lang=zh>
- Seedance 2.5 API 指南：<https://www.seeddance.io/zh/blog/seedance-2-5-api-guide>
- CapCut Mate：<https://github.com/Hommy-master/capcut-mate>
- CapCutAPI：<https://github.com/sun-guannan/CapCutAPI>
- cutsdk：<https://www.npmjs.com/package/cutsdk>
- pyJianYingDraft：<https://github.com/GuanYixuan/pyJianYingDraft>
- 剪映 API 现状（2026）：<https://www.wireflow.ai/blog/capcut-api>
- 抖店签名算法：<https://op.jinritemai.com/docs/guide-docs/10/23>
- 抖店限流 FAQ：<https://op.jinritemai.com/docs/question-docs/97/3458>
- mcp-cn-commerce：<https://github.com/TonyWang-hub/mcp-cn-commerce>
- 抖音创建视频 API：<https://partner.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create>
- 抖音发布资格限制（2026）：<https://noobclaw.com/cn/blog/douyin-api-publish-video-2026/>
- AIGC 标识办法报道：<https://www.digitalchina.gov.cn/2025/xwzx/szkx/202510/t20251024_5221930.htm>
- MCP 2026-07-28 规范变更：<https://modelcontextprotocol.io/specification/2026-07-28/changelog>
- daihuo-fanpai 实战流水线：<https://github.com/wangcanyu/daihuo-fanpai>
