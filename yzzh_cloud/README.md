# 衣装智换中央网关（仅服务器）

和客户插件分开部署；绝不把此目录、运行数据或环境凭据发给客户。现有 `web_app.py` 保持原版，不是这个商业网关。运行示例：`python -m yzzh_cloud.app --data-dir /srv/yzzh-data --host 127.0.0.1 --port 7872`，HTTPS 反向代理到回环端口。先在独立虚拟环境安装本目录 requirements.txt，不使用原项目重模型 requirements.txt。

由管理员在服务器环境配置这些**变量名**：`AUTH_TOKEN_SECRET`、`MIYO_MYSQL_HOST`、`MIYO_MYSQL_PORT`、`MIYO_MYSQL_USER`、`MIYO_MYSQL_PASSWORD`、`MIYO_MYSQL_DATABASE`、`ARK_API_KEY`、`TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_BUCKET`、`TOS_REGION`、`TOS_ENDPOINT`。此外 `YZZH_MODELS` 是管理员核实可用的模型 ID 逗号列表；`YZZH_OUTPUT_HOSTS` 是真实供应商成片 HTTPS 域名的精确允许列表。未配置时失败关闭，不用任意 URL 兜底。

代码复用历史 `ab494ce` 的中央产品 4 签名/时间卡与账单契约，没有数据库迁移。`MIYO_AUTH_ENABLED=false` 不能绕过本服务验证。客户端不能传用户 ID、单价、用量、供应商 URL 或供应商任务 ID。

## 状态与计费

`POST /v1/quotes` 读取中央单价并保存 15 分钟价格/输入快照；`POST /v1/tasks/{quote_id}/submit` 重新检查权益和价格，校验所有上传字节的哈希，原子占用该 ID，再向供应商提交一次。重复 POST 返回原任务，不重复生成。`GET /v1/tasks/{id}` 按归属查询，先下载输出，再用 `hybrid:{id}` 作为中央 task_id 幂等记账。`GET /v1/tasks/{id}/output` 仅释放已记账且属于当前用户的输出。

`submit_uncertain` 或进程中断时保留状态和已保存的供应商 ID，禁止自动重新 POST。没有供应商 ID 的不明提交需要管理员人工对账；本版不提供“猜测找回”或自动取消/退款。`reconciliation_required` 只重查/重记账，不重新生成。运营人员不得删除任务数据后要求客户“再试一次”。

## 上线前仍需完成

- 当前存储是单主机 SQLite，状态检查会持锁执行查询/记账；这是技术预览，不支持多副本或高并发横向扩容。需正式队列、短事务状态领取与任务恢复后才能扩大客户量。
- 配置 HTTPS、请求大小与速率限制、磁盘配额、备份、客户隔离测试。该服务没有自动清理客户数据，保留期和清理需单独审批。TOS 输入保留期应通过专用前缀的生命周期策略管理，不将整个桶设为公开。
- 网关为账户中心客户角色联调；登录入口在客户端配置为真实 `/api/auth/login`。不改账号中心，不发明兼容 OAuth 声明。需真实登录、时间卡/产品停用、余额不足和两个账号隔离验收。
- 核实模型参数和中央价格配置、TOS 上传与供应商可取回素材、真实输出域名、生成输出、扣费和幂等记录；当前只复用已有项目协议，尚未付费调用验收。
- 这里的测试使用模拟账户/供应商，只证明控制逻辑。不得把这些测试报告给客户当成真实账单或生成验收。
