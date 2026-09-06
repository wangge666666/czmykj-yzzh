# DepthFlow 正式网站部署

当前包已接入 CZMIYOU 账号中心：使用产品 ID `4` / `miyo_fashion` 校验启用状态和有效时间卡，任务与长视频工作区按账号归属隔离，方舟成功用量写入中央 `token_usage` 并原子扣减 `users.balance`。它仍是单实例文件型任务服务，不适合在补齐队列、限流、隐私删除和运维监控前直接大规模开放。

## 服务器要求

- Linux 云服务器，建议至少 8 核 CPU、16 GB 内存、100 GB SSD；本地视频分析越多，对 CPU、内存和磁盘要求越高。
- 已备案或可正常解析到服务器的域名。
- Docker Engine 与 Docker Compose。
- 服务器能够访问火山方舟、TOS及当前项目实际使用的外部服务。

## 上线步骤

1. 把项目完整上传到服务器，例如 `/opt/depthflow`。
2. 将 `deploy/.env.production.example` 复制为 `deploy/.env.production`，填写域名、与登录中心相同的 `AUTH_TOKEN_SECRET`、中央 MySQL 和现有方舟配置。
3. 在 `deploy` 目录执行 `docker compose up -d --build`。
4. 将域名 A 记录解析到服务器公网 IP。Caddy 会自动申请和续期 HTTPS 证书。
5. 在客户登录系统设置 `YIZHUAN_URL=https://你的域名`，再打开 `/api/health` 核对产品 4 与入口地址。
6. 打开 `https://你的域名/healthz` 检查服务，然后从 CZMIYOU 用户中心的“米哟衣装智换”进入。

## 数据与安全

- `runs`、模型和日志使用 Docker 命名卷持久化，重新发布不会清空任务。
- `.env.production` 不得提交到 Git，也不要发到聊天或截图中。
- “打开本地结果目录”在服务器上不可用；正式网站应使用页面下载按钮。
- 当前任务内存状态以单进程为主，不能横向启动多个 app 副本。
- `MIYO_AUTH_ENABLED=true` 时会停用旧的浏览器自动化生成通道，只允许可取得用量并写入中央账单的方舟 API 通道。
- 管理员后台必须先为实际模型配置并启用 `model_pricing`。Seedance 使用 Token 价；Seedream 可使用 `tokens` 或 `per_image`；表演分析使用 Token 价。代码没有价格兜底，缺价时不会提交付费请求。
- 若供应商已成功、但用量或中央记账异常，`runs/**/billing_events.json` 会保留待对账记录，任务不会自动重复生成。

## 扩大运营前仍需补齐

- 对象存储、数据库任务表、后台任务队列和并发限流。
- 费用配额、集中式对账告警、隐私删除与授权记录。
- 将 API 密钥移入云端 Secret Manager，而不是普通文件。
