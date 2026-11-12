# Fern 上线手册（72 小时）

> 对应 DESIGN §11–§12。代码都在 `infra/`：`aws/`（Terraform，OpenTofu 可用）、`k8s/`（清单）、`cloudflare/`（路由 Worker）、`deploy.mk`（make 目标）。

## 架构

```
访客 ─► Cloudflare（waken.dev）
          ├─ /apps/fern/*  ─► Worker fern-router ─► fern-origin.waken.dev（Cloudflare Tunnel）
          │                                          └─► EKS · cloudflared ×2 ─► fern-api ×2 ─► RDS Postgres 16（Multi-AZ）
          │                                                                               └─► ElastiCache Redis
          │                                                                               └─► Column / Plaid / OpenAI（出站）
          └─ 其他路径 ─► GitHub Pages（博客，不变）
```

- **没有负载均衡器，也没有入站端口。** cloudflared 从集群内主动连到 Cloudflare，流量全程加密，不需要证书。
- **没有 NAT 网关。** 节点放在公有子网，安全组不开入站；RDS 和 Redis 在数据库子网，只允许节点访问。
- 节点是 2 台 t4g.medium（Graviton），镜像按 arm64 构建。
- Worker 去掉 `/apps/fern` 前缀后转发；API 设置了 `FERN_ROOT_PATH=/apps/fern`，所以 `/docs` 的链接是对的。
- Worker 有三种模式：`live`（转发到集群）、`paused`（写请求返回 503，读请求照常）、`archive`（转到 GitHub Pages 上的静态归档）。

## 需要你本人做的事（一次性）

1. **AWS：** 准备一个有管理员权限的账号，然后运行 `brew install awscli` 和 `aws login`（或 `aws configure sso`）。确认 `aws sts get-caller-identity` 能返回账号。
2. **Cloudflare 接管 DNS（建议提前一周）：**
   - 在 Cloudflare 添加站点 `waken.dev`（免费版），核对导入的 GitHub Pages 记录。
   - 在 Gandi 把 nameserver 改成 Cloudflare 给的两个。
   - SSL 模式选 **Full**。
   - 生效后，确认博客仍能正常打开。
   - 风险：GitHub Pages 证书续期时，如果记录是橙色代理状态，续期可能失败。如果出现证书问题，先把博客的记录改成灰色（DNS only），Worker 路由只需要 `waken.dev` 本身是代理状态。
3. **Cloudflare Tunnel：** 运行 `cloudflared tunnel login`（浏览器里选 `waken.dev`），之后由 `make tunnel` 完成：创建隧道 `fern`、把 `fern-origin.waken.dev` 指向它、把 token 写进 `.env.deploy`。cloudflared 用 `--url http://fern-api.fern.svc:80` 转发，不需要在面板里配置。
4. **密钥：** 运行 `cp .env.deploy.example .env.deploy`，然后填写：
   - 新的 `FERN_SESSION_SECRET`（用 `openssl rand -hex 32` 生成）；
   - **轮换过的** Column 和 Plaid sandbox key（旧 key 在聊天记录里出现过）；
   - OpenAI key（`FERN_OPENAI_API_KEY`）；
   - tunnel token（`make tunnel` 会自动填）。
5. **Wrangler 登录：** 运行 `wrangler login`。

## 上线（H0–H4）

在仓库根目录依次运行：

```bash
make infra-up        # 15–20 分钟：VPC、EKS、RDS、Redis、ECR
make deploy          # 构建并推送镜像 → 写入 k8s 密钥 → 跑迁移 Job → 部署 api 和 cloudflared
make k8s-pool        # 约 3 分钟：在 Column 上开 5 个空间并注资
make k8s-status
(cd infra/cloudflare && npx wrangler@4 deploy)
curl -s https://waken.dev/apps/fern/healthz      # {"status":"ok"}
curl -s https://waken.dev/apps/fern/spaces       # 显示 ready: 5
```

`kubectl` 只使用 `infra/.kubeconfig`，不会碰到你默认 context 里的其他集群。

## 运行中（H4–H68）

- **发新版：** 提交后运行 `make deploy`，镜像 tag 是 git sha。
- **暂停写入：** 把 `wrangler.toml` 里的 `MODE` 改成 `"paused"`，再运行 `npx wrangler@4 deploy`。
- **补充账户池：** 运行 `make k8s-pool`（会补满到 5 个）。
- **查看日志：** 运行 `kubectl --kubeconfig infra/.kubeconfig -n fern logs deploy/fern-api -f`。

## 下线（H68–H72）

1. 生成静态归档（冻结的 JSON 快照和前端），发布到 GitHub Pages。
2. 在 `wrangler.toml` 里设置 `ARCHIVE_ORIGIN` 并把 `MODE` 改成 `"archive"`，然后运行 `npx wrangler@4 deploy`。
3. 运行 `make infra-down`，删除 namespace 和全部 AWS 资源。
4. 运行 `cloudflared tunnel delete -f fern`，删除隧道。
5. 轮换或作废 Column、Plaid、OpenAI 的 key。

## 费用估算（us-west-2，72 小时）

| 项目 | 单价 | 72 小时 |
|---|---|---|
| EKS 控制面 | $0.10/h | $7.20 |
| 2 × t4g.medium | $0.0336/h | $4.84 |
| RDS db.t4g.micro Multi-AZ | 约 $0.032/h | $2.30 |
| ElastiCache cache.t4g.micro | $0.016/h | $1.15 |
| 公网 IPv4（节点 2 个） | $0.005/h | $0.72 |
| EBS、ECR、流量 | — | < $2 |
| **合计** | | **约 $18** |

预演（每次几个小时，`make infra-up` 后接着 `make infra-down`）每次花费 1–2 美元。

## 本地验证过的部分

- `docker buildx build --platform linux/arm64` 能构建。用最近一次提交构建的镜像，连本地 Postgres 跑通了迁移、`POST /spaces`、S1 和 `/me`。
- `tofu validate` 和 `tofu fmt -check` 通过。
- Worker 的 5 个路由测试通过：`node --test infra/cloudflare/worker.test.mjs`。
- 还没在真实的 AWS 和 Cloudflare 上跑过，需要上面那些账号。
