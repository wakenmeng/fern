# Fern 前端设计

> 状态：v0.2 · 2026-11-11 · 配套原型：`design/prototype/index.html`（本地预览：`python3 -m http.server 8765 --directory design/prototype`）
> v0.2：按「全套展示」要求补齐运营对账、AI 报告、API、银行接入监控、服务监控、压测报告，Ops 和 Engineering 拆成子页。
> 原则同后端：只服务 5 个场景（S1–S5）和常见故障，不做多余的东西。

---

## 1. 核心理念：一屏两面

普通的银行 App 只给用户看余额。Fern 要展示的是**后端**，所以每个操作都同时给出两面：

- **左边是用户看到的**：一个手机宽度的银行 App，内容是余额、按钮和状态。
- **右边是银行账本里实际发生的**：时间线、复式分录、银行侧状态、不变量检查。

访客点一次「提现」，左边显示「处理中」，右边同时出现 `hold_placed → bank_outcome_unknown → 同键重试 → ach_credit.accept / cash_out`，以及借贷两边的分录。**这个对照就是整个作品的展示方式**，不需要另外解释后端做了什么。

窄屏（手机）上两面变成两个标签页：「App」和「Under the hood」。

---

## 2. 页面结构

顶部导航 4 项：Home、Customer app、Operations、Engineering，外加常驻的「Sandbox · No real money」标记。Operations 和 Engineering 内部用左侧子导航（窄屏变成横向胶囊）。

| 区域 | 子页 | 覆盖的要求 |
|---|---|---|
| **Home** `/` | 落地页：介绍、5 个场景卡片、三条导览路线（客户 / 团队 / 工程师） | — |
| **Customer app** `/app` | 一屏两面：手机 App + Under the hood | 面向用户、全流程 |
| **Operations** `/ops/*` | `overview` 客户 360 + 负余额核销 + AI 问答 | 面向内部 |
| | `transfers` 转账列表（unknown 置顶） | 面向内部 |
| | `recon` 对账：银行余额 vs 账本、逐笔匹配、差异队列、运行记录；可「注入差异」 | 运营对账 |
| | `reports` AI 每日运营报告：展示工具调用过程，报告中的数字都带引用 | AI 协助生成报告 |
| **Engineering** `/eng/*` | `design` 架构图、S4 故障时序图、关键决策 | 系统设计 |
| | `api` 端点列表、curl 示例、「Try it」在当前空间里真实执行 | API |
| | `ledger` 不变量、试算平衡、效果模板、全部分录 | 账本 |
| | `bank` Column 连接状态、调用日志、webhook 投递记录（含重复和乱序）、unknown 数量、延迟曲线 | 银行接入状态监控 |
| | `monitoring` 关键指标卡、API 延迟 p50/p95/p99、吞吐、5xx、任务队列、告警列表 | 服务监控 |
| | `loadtest` 环境与负载曲线、吞吐和 p95 曲线、分端点延迟、**资金正确性断言**、瓶颈结论 | 压测报告 |
| | `drills` 6 个故障演练、通过标准、CI 状态、跳转到能看到它的页面 | 全流程验证 |

---

## 3. 主体验 `/app`

### 3.1 场景导轨（Scenario rail）

页面顶部横向排列 5 个步骤，每个是一个胶囊按钮，显示编号、标题和状态（未开始 / 进行中 / 完成）。点击即执行该场景，或打开对应的操作面板。按顺序走完约 5 分钟，也可以自由操作。

| 步骤 | 按钮文案 | 点击后 |
|---|---|---|
| S1 | Get paid | 直接收到 $2,000 工资 |
| S2 | Pull in $500 | 发起拉款 → 显示「处理中」→ 提示「推进演示时钟」 |
| S3 | Pay Alex | 打开转账面板，预填 $50 |
| S4 | Withdraw | 打开提现面板，带「模拟银行吞掉响应」开关（默认打开） |
| S5 | Chargeback | 对 S2 那笔拉款触发未授权退回 |

导轨右侧是**演示时钟**按钮：「Advance clock」。点击后推进结算、解除到期的资金限制，并注明「真实世界：1–2 个工作日」。

### 3.2 左面：手机 App

- **首页**：大号余额（衬线、等宽数字）；下面一行小字分别显示可用、处理中、受限金额，每项旁边有 ⓘ 解释；三个主按钮：Add money、Send、Withdraw；下面是最近活动列表。
- **活动项**：图标 + 标题 + 状态胶囊 + 金额。状态颜色：处理中（蓝）、hold / 受限（琥珀）、结果未知（琥珀、慢速呼吸动画）、已到账（绿）、已退回（红）。
- **转账详情**（底部抽屉）：竖向状态步骤条，每一步显示「演示时钟时间」和「真实世界中通常需要的时间」。unknown 状态下显示「我们在向银行确认，你的钱是安全的」，以及「Retry with same key」按钮。
- **负余额状态**（S5）：首页余额变红，顶部出现横幅「Account frozen: balance is negative」，Send 和 Withdraw 置灰。

### 3.3 右面：Under the hood

4 个标签，默认跟随最近一次操作：

1. **Timeline**：选中转账的事件流，每条带时间和来源（API / Bank / Clock / Ops）。
2. **Ledger**：本次涉及的分录，以「借 / 贷」两栏的 T 字账形式展示，科目名用人能读懂的叫法（如 `Customer deposit · Jordan`），金额用等宽字体。新分录出现时有一次淡入高亮。
3. **Bank**：银行侧看到的转账状态和银行交易（Column 的状态名，如 `initiated` / `settled` / `returned`）。
4. **Checks**：两个不变量：「借方合计 = 贷方合计」「余额 = 分录汇总」，以及「每笔银行交易只入账一次」。全部显示绿色对勾和最近检查时间。

---

## 4. 运营后台 `/ops`

- **Customer 360**：客户卡片（余额、可用、held、on hold、标记 Negative / Frozen）。负余额时出现核销卡片：「Request write-off」→ 两个审批人头像（「You」和明确标注为模拟的「Simulated approver」）→ 「Approve」→ 余额归零。下方是 AI 问答，回答中每个结论都带引用胶囊，点击跳到对应分录。
- **Transfers**：全部转账，unknown 置顶并有说明。
- **Reconciliation**：4 张指标卡（银行余额、账本现金、差额、未关闭差异）；差异表（类型、状态、「Escalate」）；逐笔匹配视图（银行交易 ↔ 分录）；运行记录。「Inject a mismatch」会在银行侧放入一笔账本不知道的 −$25.00，下一次对账必须发现它。仍处于 unknown 的转账产生的未匹配交易会被标成「In flight」，确认后自动关闭。
- **Reports**：「Generate report」→ 先列出 agent 调用的只读工具和结果，再给出报告：摘要、资金流表格、事故、退回与损失、对账、建议。报告里的数字来自工具结果，并带引用；对账有差额时摘要会如实写出。

---

## 5. 工程展示 `/eng`（面向技术面试）

原则：每个说法都能落到代码、测试或沙箱实测上；每页底部有「上一页 / 下一页」，可以按顺序读完。19 个子页分 5 组：

| 组 | 页面 | 内容 |
|---|---|---|
| Start | Start here | 关键数字、五个难题及解法、10 分钟阅读路线 |
| Design | Architecture | 架构、一次提现的请求路径（两个短事务夹着银行调用）、模块职责与源文件 |
| | Data model | ER 图、「真正起作用的约束」表（每个唯一约束防止什么）、金额表示方式 |
| | Money flows | S1–S5 切换：状态流、每一步的分录、时序图、对应的测试 |
| | Consistency | 「至少一次输入 → 恰好一次效果」的核心思路、事务边界、锁规则、四层幂等、各类事实的权威来源 |
| | Failure modes | 16 种故障：系统怎么做、靠什么发现、由哪个测试证明 |
| | Decisions | 9 条 ADR：背景、决定、被否决的备选及原因、代价 |
| | Scaling & limits | 现在的瓶颈、10× / 100× 的改法、演示版的已知限制 |
| Build | API | 约定（认证、金额、幂等、异步结果）、端点 + Try it、错误码与可否重试 |
| | Ledger | 实时不变量、同样检查的 SQL 版本、试算平衡、效果模板、全部分录 |
| | Code walkthrough | 5 段真实代码摘录（记账守卫、按银行状态推导分录、银行调用的三种结局、Column 现金时点、安全重试）和讲解 |
| | Testing | 测试分层、24 个测试逐条说明、Column 沙箱实测行为及代码对应处理 |
| Run | Infrastructure | 拓扑、CI/CD、72 小时窗口、成本 |
| | Bank integration | 指标卡、Column 状态映射、错误分类、延迟、调用日志、webhook 投递 |
| | Monitoring & SLOs | 服务 SLO 和资金 SLO、指标卡、延迟/吞吐/5xx/在途图、告警规则 + runbook、应用导出的指标 |
| | Incident review | 一次演练写成的事故复盘：时间线、分析、行动项 |
| Prove | Load test report | 环境与负载、k6 方法、参考模型校验流程、结果、分端点延迟、资金正确性断言、瓶颈 |
| | Failure drills | 6 个演练与通过标准 |
| | Hard questions | 10 个评审时最可能被追问的问题和回答 |

代码讲解和测试页引用的是仓库里的真实代码（`ledger.py`、`payments.py`、`column.py`），沙箱行为来自 `scripts/column_probe.py` 的实测。

### 5.1 示例数据的处理

原型里监控、银行延迟和压测报告用的是**示例数据**，页面顶部有紫色的「Sample data」标注。真实版本的数据来源：

| 页面 | 数据来源 |
|---|---|
| 服务监控 | Prometheus（应用暴露的 HTTP 直方图、任务队列、资金指标），后端提供只读的聚合接口 |
| 银行接入 | 银行客户端的调用记录表、webhook inbox 表 |
| 压测报告 | k6 输出的 JSON 汇总 + 压测后参考模型比对的结果，作为静态 JSON 随版本发布 |

上线前所有「Sample data」标注都必须被真实数据替换，否则不发布这个页面。

### 5.2 图表规范

- 折线 2px、网格和坐标轴弱化、十字准线 + 悬停提示、单条曲线不显示图例；多条曲线同时有图例和线尾直接标注（标注会自动错开）
- 每张图下方有「View as table」，可以展开为表格
- 三色系列 `#2FA866` / `#3B82D9` / `#C2610F` 经 dataviz 校验器在暗色卡片（`#0D120F`）上全部通过：色盲区分度最差一对 ΔE 21.6，对比度 ≥ 3:1
- 窄卡片里的图使用更窄的 viewBox，保证坐标文字缩放后仍可读

---

## 6. 视觉语言

**参考 green.com 的气质**：近黑底、单一的亮绿色、衬线小型大写标题、胶囊形控件、克制的留白、背景有很淡的动态纹理。**Fern 不复用它的任何身份元素**：名称、标志、字体、文案、纹理图案全部不同。

### 6.1 色彩

| Token | 值 | 用途 |
|---|---|---|
| `--bg` | `#060907` | 页面底色，比纯黑略带绿 |
| `--surface` | `rgba(255,255,255,0.04)` | 卡片 |
| `--surface-2` | `rgba(255,255,255,0.08)` | 输入框、悬停 |
| `--line` | `rgba(255,255,255,0.09)` | 分隔线 |
| `--text` | `#F1F7F2` | 正文 |
| `--muted` | `rgba(241,247,242,0.6)` | 次要文字 |
| `--fern` | `#52E08A` | 唯一的强调色：主按钮、借方高亮、通过的检查 |
| `--fern-ink` | `#03140A` | 主按钮上的文字 |
| `--amber` | `#F2B84B` | hold、受限、unknown |
| `--blue` | `#7CC0FF` | 处理中 |
| `--red` | `#FF6F61` | 退回、负余额 |

只用暗色主题。这是品牌选择，和参考站一致。

### 6.2 字体

| 用途 | 字体 | 说明 |
|---|---|---|
| 展示标题、大号余额 | **Cormorant Garamond**，small-caps，字距 0.04em | 高对比衬线，与参考站的气质相近，但是不同的字体 |
| UI 文本 | **Inter Tight** 400 / 500 / 600 | 紧凑的现代无衬线 |
| 金额、ID、分录 | **JetBrains Mono**，tabular-nums | 等宽，便于对齐借贷金额 |

### 6.3 形状与动效

- 按钮、输入框、状态标签：胶囊形（`border-radius: 999px`）；卡片：圆角 20px
- 主按钮：`--fern` 底、`--fern-ink` 字；次按钮：`--surface-2` 底
- 背景纹理：一层很淡的**蕨类叶脉线**（从中心向两侧分叉的细线，透明度约 6%），缓慢漂移；`prefers-reduced-motion` 时静止
- 新分录淡入加一次 `--fern` 描边闪烁；unknown 状态胶囊慢速呼吸
- 标志：一个卷曲的**蕨菜嫩芽（fiddlehead）**螺旋 + 小型大写的 FERN 字标

---

## 7. 技术方案

- React + TypeScript + Vite，单个 SPA，`base: '/apps/fern/'`，打包进 nginx 镜像（见 DESIGN §3.2）
- 数据：直接调用 `/api`；会话 token 存在内存和 `sessionStorage` 里；所有资金操作带 `Idempotency-Key`（前端按「操作 + 点击」生成 UUID，重试时复用）
- 刷新：每次操作后重新拉取 `/me`、`/transfers/{id}`、`/ledger/trial-balance`；有处理中的转账时每 2 秒轮询一次，不引入 WebSocket
- 不引入组件库，只用少量自写组件：Pill、Card、Sheet（底部抽屉）、TAccount、Timeline、StatusChip
- 关闭后的静态归档复用同一套组件，数据换成冻结的 JSON 快照（DESIGN §12.4）

---

## 8. 后端需要补的接口（为前端服务的最小集合）

| 接口 | 用途 |
|---|---|
| `GET /transfers?status=&limit=` | 活动列表、运营后台转账表 |
| `GET /ledger/journals?transfer_id=` | Ledger 标签、全部分录 |
| `POST /recon/runs`、`GET /recon/runs`、`GET /recon/exceptions` | 对账页 |
| `GET /bank/calls`、`GET /bank/webhooks` | 银行接入页 |
| `GET /metrics/summary?window=` | 服务监控页（从 Prometheus 聚合） |
| `POST /ai/investigate`、`POST /ai/reports` | AI 问答和报告 |
| `POST /demo/inject-mismatch` | 对账演示 |
