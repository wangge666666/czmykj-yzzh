# 交付证据：衣装智换客户版插件

创建日期：2026-09-06

## 2026-09-07 当前 Mac 安装与系统协议唤醒

- 用户明确同意安装新版及网页启动器。完整 0.5.0 ZIP 原哈希不变，安装到 `/Users/mykj/Applications/czmiyou-yzzh`；安装前 73 文件 release 验证和 setup/register dry-run 均通过。Python 3.12 私有依赖安装成功，未下载模型；安装后 73 载荷与原清单逐项一致。旧预览进程 68134 / 17872 和 `/tmp/yzzh-byok-preview-20260906` 保持不变，不迁移或覆盖旧任务。
- Codex 采用本机适配：`/Users/mykj/plugins/czmiyou-yzzh` 的 `.mcp.json` 指向 Applications 下固定 Python/launch.py，README 标记为本机登记副本；不将私人绝对路径回写客户 ZIP。官方 scaffold helper 在隔离目录验证新增 marketplace 结构后，仅向既有 personal marketplace 追加本插件，其他三条登记项及 interface 原样保留。原 marketplace 备份 `/Users/mykj/.codex/backups/czmiyou-yzzh-20260907/marketplace-before.json`。
- `codex plugin add czmiyou-yzzh@personal --json` 成功，缓存 `/Users/mykj/.codex/plugins/cache/personal/czmiyou-yzzh/0.5.0`；`codex plugin list --marketplace personal --json` 确认 installed=true、enabled=true、version=0.5.0，其他已安装插件保留。登记源与缓存全文件 diff 一致；manifest、Skill、roster 检查通过。没有修改 WorkBuddy。
- 通过包内 register_launcher.py 注册 `/Users/mykj/Applications/CZMIYOU衣装智换启动器.app`，事前查无同名应用或已注册的相同 URL 协议。只注册 `czmiyou-yzzh` 固定动作，不注册登录项，不覆盖其他协议。第一次 `open 'czmiyou-yzzh://start'` 前 7871 无监听，启动后进程 88326 监听 7871，实际 `/_plugin/launch` HTTP 200。初始立即检查连接失败是异步启动尚未完成，随后成功，不把首个探测计为服务失败。
- 从已安装 Codex 缓存的 MCP 配置实际启动 stdio：initialize 0.5.0、tools/list 12 项、health 0.5.0/byok/logged_in=false。未读取连接文件或 .env、未使用真实账户/Key、未产生付费请求。新数据目录 `/Users/mykj/.czmiyou-yzzh` 与旧预览隔离。
- 本轮证明 Mac 系统 URL 协议冷启动及已安装配置的 MCP 握手；不等同真实浏览器从线上 HTTPS 按钮唤醒与登录交接，也不等同 Codex 刷新后新任务触发成功。需用户重新打开 Codex/新任务后确认发现；统一登录系统仍未推送部署，GitHub Release 未发布，真实登录/时间卡、供应商、WorkBuddy 和成片验收仍待完成。没有自动重启 Codex或创建新任务。

## 0.5.0 统一登录入口预览（2026-09-06 构建证据）

- 客户 ZIP：`dist/czmiyou-yzzh-0.5.0-unified-login-preview.zip`；SHA256 `d2df1100d5b662a2fed3d54bbf5630e56e6d5aab52865bda241cfabfc693d522`。73 个载荷，解压包 release 校验、安装及启动器 dry-run、manifest/skill/roster 均通过。逐项源文件、已安装载荷和清单哈希一致，常见凭据模式扫描无命中；扫描不是对所有秘密格式的保证。
- 独立安装 `/tmp/yzzh-unified-package.77I4Bn/czmiyou-yzzh`，Python 3.12 私有 .venv，未下载模型或注册系统协议。实际包内 stdio 12 工具、本地 2 镜拆分、4 秒导出及两个 MP4 解码通过，报告 `dist/unified-login-bundle-evidence/runtime-report.json`，原任务 `48043a6914e7`。视频是合成图样，不是付费生成。
- 当前定向 Python 回归 109/109：launcher 7、auto_upload 10、BYOK 21、original/login 24、hybrid 47。中央登录入口 TS 3/3、TypeScript 检查通过、ESLint 0 错误及 6 条既有警告。三份本地前端脚本语法与 Python 编译通过。没有本次全仓库全量通过的结论。
- 真实 Chrome 联调两个回环来源：点击合成统一入口，打开原衣装四模式/六步骤页；附加面板显示合成统一登录账号及产品 4 有效时间卡，不重复显示密码表单。只读报告 logged_in=true、licensed=true、verify_calls=1、version=0.5.0，real_account/paid_generation/native_wake 均 false。未检测到时的 GitHub 兜底也实际触发。修复启动 JS 被原静态目录覆盖导致的 404，加入实际 GET 字节回归。
- 登录系统的配套变更仅产品 4 点击与公开入口状态，未改变其他产品、中央数据库、余额或卡。用户已有 next-env.d.ts 修改保留。两个仓库均未提交/推送/部署；GitHub Release 未上传。现有 17872 仍为 0.4.0，避免未部署的入口打断使用。
- 尚未验收：实际 URL 协议注册及冷启动、真实线上 HTTPS 登录交接、Codex/WorkBuddy 分别发现与触发、真实账户/时间卡边界、供应商人物审核和生成、用户最终成片。没有独立 reviewer 增量复查，不能执行严格最终交付通过标记。

## 历史：0.4.0 自动上传与 AIGC 前台

`dist/czmiyou-yzzh-0.4.0-aigc-preview.zip`，SHA256 `50a34b32b4cb2905fa1643dee5029a1abdd3b1a33cf7fd9add2aa69e88b9408d`。69 载荷独立解压和安装、stdio 与 FFmpeg 通过。真实 Litterbox 合成 PNG 1026 B / MP4 3325 B 上传及读取比对成功，没有客户媒体或付费生成；72 小时是所请求的服务保留期，不是已经等待 72 小时实测。保留 TOS 可选模式与逐次发送批准；仅 AIGC 人像，不提供真人活体授权。

## 历史：0.3.1 登录身份修复预览

- ZIP：`dist/czmiyou-yzzh-0.3.1-login-preview.zip`；SHA256：`73cae8821b736217afde3b2b004b0446f0b49e750150cbf3e732c3c160c3aea2`。67 个载荷逐项等于当前源码；不包含真实密钥、运行数据、模型或中央服务。
- 两个登录入口显式选择身份；员工不再被固定当作客户。已知拒绝原因使用中文提示，未知上游错误不外显；审批/产品 4 门禁和人工付费确认不变。
- 回归 92/92：新增登录 11、BYOK 21、原工作流 13、hybrid 47；未在本次重新跑全部原仓库测试，不把旧全量结果记为本次全量通过。
- 独立解压安装：`/tmp/yzzh-login-package.m9PKL3/czmiyou-yzzh`，Python 3.12 私有 venv；安装前 67 文件验证和 dry-run 通过。解压运行时的 11 项登录回归通过，plugin/skill/roster/project 和两个 JS 语法通过。未做全局宿主安装或模型下载。
- 包内真实本地运行：`dist/login-bundle-evidence/runtime-report.json`。实际 stdio 12 工具；合成参考经原流程产生 2 镜，job 85aec3cbeda9，原片段 MP4 解码通过；4 秒导出 SHA256 为 aaacbb9e27fbeccfea9e1d28c906a1106c77b3cadc35642b273149f7705969c1。该路径仍是假账号+实际 FFmpeg，不是付费生成。
- 真实用户局部验收：17872 预览重启到 0.3.1；浏览器核对员工选择框。用户自行登录后，页面及只读 `/api/state` 同时确认已登录、产品 4 时间卡有效；供应商配置仍为空。主代理未填写密码、读取登录 token 或执行供应商调用。此证据只覆盖当前账号，不推定所有审批/叠加卡/角色都线上通过。
- 原页面和业务源码未改；旧会话标签页刷新后正常使用已登录界面。未推送 Git、未修改账号中心/数据库；本次没有独立 reviewer 增量复查。

## 历史：0.3.0 原界面技术预览

- ZIP：`dist/czmiyou-yzzh-0.3.0-original-preview.zip`
- SHA256：`d0af2a1215eea98cf5221ace168226f2249ef9c441e47ccfe089501f753904f2`
- 67 个载荷与实际安装并测试的 /tmp/yzzh-original-package.Lyucwl/customer.zip 清单相同，并与当前源码逐项相同。不含 .env、运行数据库、素材、模型或中央生成网关。
- 源码报告：`dist/original-source-evidence/runtime-report.json`；实际原任务 f09cf8b9d18a，2 镜可解码。解压包报告：`dist/original-bundle-evidence/runtime-report.json`；实际原任务 46150c94cc33，2 镜可解码；两个报告另有单段 4 秒 MP4 导出。全部使用合成图样与回环假账号。
- 安装：新解压目录中 Python 3.12 私有 venv 安装成功，普通安装没有 Torch/Demucs 或模型下载。67 文件 release 验证、plugin/skill/roster、JS 语法与包结构测试通过。未全局安装、未运行真实宿主触发。
- 原 UI：实际 in-app browser 检查 17872 原项目首页、衣装四模式/六步骤及右下角附加面板，截图可见原绿色布局。未在该浏览器填写真实账号；服务重启后本地连接需由打开工作台入口刷新，不能把旧 cookie 失效当成线上登录故障。
- 原工作流 13/13（Python 3.12）、BYOK 21/21、hybrid 47/47；独立审核复查同原工作流 13/13。完整 Python 3.9 332 项：325 pass、6 同基线失败、1 因原引擎要求 3.10+ 跳过，跳过项另在 3.12 通过。
- 保留缺口：真实登录/叠加卡、供应商模型和账单、客户输出、两个宿主；插件内活体 H5 暂不支持。没有消耗客户 Key，未推送 Git。

## 当前结果：0.2.0 BYOK 技术预览

用户已授权改造。CZMIYOU 既有账号中心只验证登录与产品 4 时间卡；客户在本机填写自己的方舟/TOS 配置，本地程序直接上传、提交、查询和取回，不调用中央生成网关或扣费。原网页、旧 0.1.0 ZIP 和中央服务源码保留，新包不包含 yzzh_cloud。

已实现：按账号私有 .env、Key 不回显、设置表单会话绑定、输入/配置/会话变更使旧批准失效、排队执行及付费 POST 前再次验权、未知提交只恢复、到期允许既有任务取回、下载失败保留任务 ID。

| 验证层 | 当前实证 | 边界 |
|---|---|---|
| BYOK 回归 | `test_byok_runtime.py` 21/21；Python 3.9 开发环境和隔离 Python 3.12 均通过 | 授权及供应商为模拟，视频为实际合成素材 |
| 旧兼容及包 | `test_hybrid*.py` 47 项；其中 package 5/5，包含空模板被填值且重算清单仍拒绝 | 旧中央网关只保留历史回归，不能当作新计费路径 |
| 全量 | 319 项，313 通过，6 个原基线失败；日志 `/tmp/yzzh-byok-full-tests-20260906.log` | 失败名称与下方原版基线相同；未改旧网页来掩盖失败 |
| 源码及客户包真实运行 | `verify_hybrid_runtime.py`，两个 evidence 目录中的 runtime-report.json | stdio 10 工具→本地 HTTP→回环假账号产品 4→实际 FFmpeg 分镜/导出/解码；无付费调用 |
| 安装与包结构 | 隔离 Python 3.12 新建私有 .venv、安装全部依赖含 TOS；28 文件完整结构及哈希检查 | 未下载权重，未全局安装到宿主 |
| 正常参考格式 | 实际合成 960×544、24fps、4 秒视频通过 ArkProvider.preflight，TOS 实际导入 | 无网络/生成；模型开通、素材审核和上游约束仍待真实校验 |
| 浏览器 | 新预览 `http://127.0.0.1:17872/` 首屏实际读取 | BYOK 0.2.0、账户地址、配置说明与未登录禁用正常；未填真实凭据 |
| 独立复查 | 旧标签跨账号配置 P2 修复；最终独立 BYOK 21/21、package 5/5，28 个载荷与源码及清单逐项一致，无剩余已证实 P0—P2 | 仅本地源码与分发增量，不代替用户验收 |

源码证据 `dist/byok-source-evidence/`；解压包证据 `dist/byok-bundle-evidence/`。两者产物均为 4 秒 H.264 MP4，哈希 `aaacbb9e27fbeccfea9e1d28c906a1106c77b3cadc35642b273149f7705969c1`。它们是合成分镜输出，不是 AI 供应商生成的视频。

当前适配器字段已核对火山官方 SDK 的 [创建素材类型](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/types/content_generation/create_task_content_param.py) 与 [任务结果类型](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/types/content_generation/content_generation_task.py)（2026-09-06）。创建 API 文档正文抓取受动态页面限制，格式预检沿用本项目原约束；不声称任意模型/Key 均兼容，也不虚构价格。

发布门禁仍未完成：真实账号中心部署版本/产品 4 联调与账号安全加固，客户供应商 Key 的真实消费和成片，打码/深度权重与效果，Codex/WorkBuddy 分别安装与触发，用户最终验收。没有进行 Git 提交/推送或线上部署。

## 以下为 0.1.0 中央代收费预览的历史证据（不代表当前架构）

## 结果与范围

第一条“本地＋云端”单段参考重绘切片已实现并形成技术预览包，不是完成真实客户验收的正式版本。未全局安装、未部署、未推送 Git、未调用真实供应商或中央收费，没有上传客户私有素材。

- 源码基线：main `1e8fb7aa387f2f0b6e9832d722bb4e418e7b64fa`，回退后的源树对应 `7f30c61`。原网页业务文件保持未修改；新增本地插件和独立商业网关。
- 工作区根文档仍曾描述旧 CZMIYOU 集成，和回退代码不一致。本次以源代码为准，只在服务器包复用 `ab494ce` 的签名/产品 4/时间卡/账单模块，不恢复整批旧提交。
- 客户白名单包只包含本地引擎、工作台、MCP、Skill 和安装配置脚本，不含服务器账号模块、环境文件、个人路径、媒体、模型、状态数据库或虚拟环境。
- 原网页白模、人物资产库、表演分析和完整长视频编排尚未迁移，原网页功能仍保留。

## 已执行的验证

| 层次 | 命令或操作 | 实际结果与边界 |
|---|---|---|
| 插件元数据 | plugin-creator `validate_plugin.py`、skill-creator `quick_validate.py`、Plugin Manager `check_skill_roster.py` | 通过，1 个业务 Skill；不是宿主发现证据 |
| 静态检查 | Python compileall；`node --check yzzh_local/web/app.js` | 通过 |
| 新增回归 | `.venv-codex/bin/python -m unittest discover -s tests -p 'test_hybrid*.py' -v` | 46/46 通过，包含签名/产品 4/时间卡、事务计费、并发归属、人工审批、异常恢复和 ZIP 结构/篡改检测 |
| 客户端与网关联调 | test_hybrid_http.py，真实回环 HTTP 与 multipart，合成 MP4/PNG | 提交、模拟账单失败、仅对账恢复、已记账输出下载、导出及解码通过；身份/供应商/扣费使用测试替身 |
| 实际 stdio | `scripts/verify_hybrid_runtime.py --data-dir <隔离目录> --evidence-dir <隔离证据目录>` | initialize 协议 2025-06-18，10 个工具；真实导入、分镜和导出通过 |
| 解压包独立运行 | 同脚本加 `--bundle <解压包>` | 客户 scripts/launch.py 和自己的 Python 环境运行成功，不回退到服务端源码 |
| 真实浏览器 | Codex 内置浏览器访问回环工作台，刷新后点实际分镜“预览” | video readyState=4，320×180，4 秒；截图检查控件与视频显示正常。没有代点真实生成批准 |
| 安装 | 解压目录 `scripts/setup.py --dry-run`；Python 3.12 安装到包内 .venv | 通过。首次 uv Python 默认复制模式失败后修复为 POSIX symlinks；未改全局 Python/宿主配置，未下载模型 |
| 包结构 | `scripts/validate_release.py`、ZIP 白名单与 SHA256SUMS.json | 完整 24 文件结构及哈希检查；缺/多文件即使同步改清单也拒绝。完整性检查不等于数字签名 |

stdio 工具：health、open_workbench、list_projects、import_video、status、process、plan、submit、poll、export。登录凭据和人工批准没有对应 MCP 工具。

合成导出：4.0 秒、H.264 MP4，FFmpeg 解码通过，SHA256 为 `aaacbb9e27fbeccfea9e1d28c906a1106c77b3cadc35642b273149f7705969c1`。运行记录和样片放在仓库忽略目录 `dist/hybrid-preview-evidence/`，不打入客户包。

## 原版全量测试边界

最终全量命令运行 297 项，291 通过、6 失败；与开始时原版 251 项中的同 6 个失败一致，未改动无关网页测试来掩盖它们：

- 4 项受 macOS `/var` 与 `/private/var` 路径差异影响：两个 custom_generation 引用测试、scene_generation、web_generation 自动下载路径测试。
- `test_extracted_person_submission_requires_authorization`：基线先报缺少 AK/SK，测试期望先报授权。
- `test_partial_replacement_pages_share_ark_character_library`：基线静态资源引用字符串和测试预期不一致。

原环境 Python 3.9 / LibreSSL 有 urllib3 兼容警告；隔离客户包使用 Python 3.12 实装。Windows 和其它系统尚未实测。

## 尚未验收

- Codex 市场安装/新任务发现、WorkBuddy 桌面 MCP/技能导入与触发。原生插件清单不内置商业端点，当前要用 host_config.py 生成带管理员端点的手动 MCP 配置；不能宣称即装即登录。
- 实际登录中心、中央 MySQL、两个真实账户隔离、时间卡边界、产品停用、余额不足、真实供应商/TOS/输出域名、真实账单与幂等记录。
- 缺少模型权重时的本地打码、深度真实推理与隐私效果；本轮未运行模型下载。
- 用户视频的最终效果、人工审批和用户验收。自动化审批测试只是合成夹具，不作为真实用户批准。
- 正式服务器的队列/短事务、并发与磁盘配额、保留期、备份、HTTPS 和运维恢复。当前单机 SQLite 技术预览不作为规模化客户部署。

## 宿主依据（2026-09-06 在线核对）

- [Codex Build plugins](https://learn.chatgpt.com/docs/build-plugins)：插件可包含 skills、MCP 或两者；本地市场适用于开发测试，清单为 `.codex-plugin/plugin.json`。
- [腾讯插件 API 参考](https://cloud.tencent.com/document/product/1831/137036)：CodeBuddy 插件支持技能与 MCP，兼容 `.workbuddy-plugin/plugin.json`。这是其插件系统文档，不能替代 WorkBuddy 桌面客户端实测。
- [WorkBuddy 桌面 MCP 指南](https://www.workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)：搜索索引返回项目级与用户级 MCP 配置入口，但完整页面抓取失败，具体 UI 安装步骤仍需实际客户端复核。
- [OpenAI 插件认证](https://developers.openai.com/plugins/build/auth)：远程连接的 OAuth 授权码与 PKCE 是可评估方案；现有 CZMIYOU 签名 token 不因此自动成为兼容 OAuth 服务，必须单独设计适配。

## 最终产物

客户技术预览：`dist/czmiyou-yzzh-0.1.0-preview.zip`；源码入口 `plugins/czmiyou-yzzh/README.md`；独立服务器说明 `yzzh_cloud/README.md`。这不是正式发布，故不添加最终通过标记，项目校验只运行普通模式。下一步需确定云端部署位置并取得安装/真实业务联调授权。

该 ZIP 的 SHA256：`ae5fce53a220ee678c276ea16e4e24483669203bdd2610c521ce0f01bd3e59a5`。
