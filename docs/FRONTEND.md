# Fern 前端设计

> 状态：v0.3 · 2026-11-11 · 配套原型：`design/prototype/index.html`（本地预览：`python3 -m http.server 8765 --directory design/prototype`）
> v0.3：按「一分钟讲完」重做信息架构。首页就是 Demo，每个操作都产生一条 Trace；Engineering 只留 Architecture、Benchmark、API 三页；Operations 合成一页。
> 原则同后端：只服务 5 个场景（S1–S5）和常见故障，不做多余的东西。

---

## 1. 演示脚本（一分钟）

1. **Demo**：一句话介绍 Fern，以及接入了哪些服务（Column、Plaid、Claude、AWS）。点 S1–S5，右边出现 Trace：经过哪些节点、每条 SQL、每次银行调用、记了哪几笔账。重点讲 S4：Column 超时后钱一直被 hold 住，恢复任务用同一个幂等键确认，不会重复打款。
2. **Engineering → Architecture**：AWS 架构图，带每个节点的状态；上次请求经过的节点会被描边标出。下面是第三方服务监控：Column、Plaid、Claude 的延迟、错误率和调用量。
3. **Engineering → Benchmark**：峰值吞吐、持续吞吐、并发用户数，以及「压测后零资金错误」，还有历次压测记录。
4. **Engineering → API**：接口文档，可以直接 Try it，并跳到这次调用的 Trace。

Operations 是备用页，被问到「内部怎么运营」时再打开。

---

## 2. 页面结构

顶部导航 3 项：**Demo**（`/`）、**Operations**（`/ops`）、**Engineering**（`/eng/architecture | benchmark | api`），外加常驻的「Sandbox · No real money」标记。

| 页面 | 内容 |
|---|---|
| **Demo** `/` | 简介和已接入的服务 → S1–S5 场景按钮和演示时钟 → 左边手机 App，右边 Trace 面板 |
| **Operations** `/ops` | 4 个指标（账本平衡、银行与账本差额、待人工处理数、在途资金）→ 待处理队列 + 客户表 → 对账 → AI 日报 |
| **Engineering** `/eng/architecture` | 6 个指标（接入银行数、第三方 API 数、AWS 组件数、Pod、API p95、可用性）→ AWS 架构图 → 第三方服务卡片（各带延迟曲线）→ AWS 组件状态表 |
| `/eng/benchmark` | 4 个结论数字 → 吞吐 / 并发 / 延迟三张图 → 分端点延迟 → 压测后的资金正确性检查 → 历次压测记录与方法 |
| `/eng/api` | 按 Customer / Bank / Ops / Demo 分组的接口文档：说明、认证与幂等标记、请求体字段、curl、Try it、响应、跳转到对应 Trace；错误码表 |

---

## 3. Trace 面板（Demo 页右侧）

每个用户操作、webhook、后台任务都会产生一条 trace，结构和线上服务导出的 OpenTelemetry span 一致。

- **Trace 列表**：最近的 trace 排成一行胶囊，标签为 `S1`–`S5` 或 `Ops`，显示标题和耗时，点击即可切换。
- **头部**：入口（如 `POST /v1/withdrawals`）、状态码、总耗时、span 数、服务数、trace id。
- **服务节点图**：调用方 → Cloudflare → ALB → fern-api / fern-worker → Postgres、Redis → Column、Plaid、Claude，按 Cloudflare / AWS VPC / 第三方分组。本次经过的节点和连线会点亮，连线上标出调用次数（如「12 queries」）；出错的连线标红并写「timed out」。新 trace 出现时，有小圆点按 span 顺序沿连线流动（`prefers-reduced-motion` 时不播放）。
- **瀑布图**：每行包含服务、操作和时间条。数据库事务默认折叠为一行（`db.tx request_withdrawal · 7 SQL`），可以展开看每条语句。出错的 span 标红。
- **Span 详情**：SQL 原文（带高亮）、银行请求体和幂等键、全部属性（如 `column.status`、`lock.order`、`why`）。默认选中最值得看的 span：先选出错的，其次第三方调用，再次事务。
- **记账结果**：本次 trace 产生的分录（效果、借方、贷方、金额）、操作后的余额和可用余额，以及不变量检查。没有记账时说明原因，例如「结果未知，只有 hold，等同键重试确认」。

服务按三层着色：Fern on AWS、Data stores、External。三色是已校验的 `--s1` / `--s2` / `--s3`；每行都有服务名文字，颜色只做辅助。

---

## 4. 各场景的 trace

| 场景 | 入口 | 经过的节点 | 要点 |
|---|---|---|---|
| S1 Get paid | Column webhook | CF → ALB → api → Postgres、Column | 验签 → 查 inbox 去重 → 回读 Column → 一个事务内记账，并写 inbox |
| S2 Add money | `POST /v1/pulls` | CF → ALB → api → Redis、Postgres、Plaid、Column | 限流 → 事务 1（幂等键、建转账）→ Plaid 余额检查 → Column DEBIT → 事务 2（accept、restriction） |
| S2 后续 | webhook `settled`；任务 `release_restrictions` | api → Column、Postgres；worker → Postgres | 到账才记现金；释放限制不记账 |
| S3 Pay Alex | `POST /v1/p2p` | api → Redis、Postgres | 一个事务；按 id 顺序锁两个账户；没有银行参与 |
| S4 Withdraw（演练） | `POST /v1/withdrawals` | api → Redis、Postgres、Column（超时） | 10 秒超时，结果标为 unknown，hold 保持 |
| S4 恢复 | 任务 `recover_unknown` | worker → Postgres、Column | 同一个键重试，Column 返回原转账；比对金额和类型后再记账 |
| S5 Chargeback | webhook `returned` | api → Column、Postgres | 只有退回能把余额记成负数；trace 中注明 R10 来自模拟器 |
| S5 销账 | `POST /v1/ops/write-offs`、`.../approve` | api → Postgres | 由 CHECK 约束强制两人审批 |
| 对账 | 任务 `reconcile` | worker → Column、Postgres | 只读，只产生差异记录 |
| AI 日报 | `POST /v1/ops/reports` | api → Redis、Claude、Postgres | 先在 Redis 预占预算 → 规划 → 只读工具 → 起草 → 按实际用量结算 |

---

## 5. 示例数据

架构页的节点状态、第三方延迟和压测数字在原型里是**示例数据**，页面顶部用紫色条标注「Prototype: …」。上线前必须换成真实数据，换不了就不展示这个数字。

| 数据 | 线上来源 |
|---|---|
| Trace | 应用用 OpenTelemetry 导出到集群内的 Tempo；API 响应带 `traceparent`，前端通过 `GET /v1/traces/{trace_id}`（后端代理查询 Tempo）取 span |
| 节点状态 | CloudWatch（ALB、RDS、ElastiCache）、Kubernetes API（Pod 状态）、Prometheus（应用指标） |
| 第三方监控 | 应用自己的出站调用直方图 `fern_bank_call_duration_seconds` 等，按 provider 分组 |
| 压测 | k6 输出的 JSON 汇总 + 参考模型比对结果，作为静态 JSON 随版本发布 |

---

## 6. 视觉语言

**参考 green.com 的气质**：近黑底、单一的亮绿色、衬线小型大写标题、胶囊形控件、克制的留白、背景有很淡的动态纹理。**Fern 不复用它的任何身份元素**。

- 色彩：`--bg #060907`，`--fern #52E08A` 是唯一的强调色；`--amber` 表示 hold 或 unknown，`--red` 表示退回、负数和错误
- 字体：标题用 Cormorant Garamond small-caps；界面文字用 Inter Tight；金额、ID、SQL 用 JetBrains Mono（关闭连字）
- 工程图：分组框加区域标签（CLOUDFLARE / AWS · US-WEST-2 / VPC / AZ / EKS / THIRD-PARTY APIS），节点左侧色条表示层级，节点内显示一行状态指标，连线带协议或端口
- 图表：2px 折线、十字准线加悬停提示、可展开为表格；多条曲线时有图例和线尾标注；三色系列通过 dataviz 校验器检查

---

## 7. 技术方案

- React + TypeScript + Vite，单个 SPA，`base: '/apps/fern/'`，打包进 nginx 镜像
- 所有资金操作带 `Idempotency-Key`，按「操作 + 点击」生成，重试时复用
- 每次操作后重新拉取 `/v1/me` 和本次请求的 trace；有在途转账时每 2 秒轮询一次
- 不引入组件库；服务节点图和瀑布图用 SVG 和 CSS 自己画
- 72 小时后的静态归档复用同一套组件，数据换成冻结的 JSON 快照（含 trace）

---

## 8. 后端需要补的东西

| 项 | 用途 |
|---|---|
| OpenTelemetry 埋点（FastAPI、asyncpg、httpx、redis）+ Tempo | Trace 面板 |
| `GET /v1/traces/{trace_id}` | 按 trace id 取 span（只返回当前 space 自己的 trace） |
| Redis 限流（token bucket）、AI 预算预占 | trace 里的 Redis 节点；DESIGN 已规划，代码里还没有 |
| Plaid 余额检查（`/accounts/balance/get`） | S2 trace 里的 Plaid 节点；配置已有，调用还没写 |
| `POST /v1/ops/reconciliation/run`、`recon_runs` / `recon_exceptions` 表 | 对账 |
| `GET /v1/status`（聚合 CloudWatch、K8s、Prometheus） | 架构页的节点状态 |
| `POST /v1/ops/reports` | AI 日报 |
