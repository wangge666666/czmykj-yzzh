# 任务日志：衣装智换客户版插件

创建日期：2026-09-06

## 2026-09-07 用户授权的 Mac 本机安装

完整包安装到 Applications/czmiyou-yzzh，新增个人 Codex marketplace 登记与固定运行路径，注册专用 URL 启动器。包内无 install.sh，因此使用已验证的 setup.py/register_launcher.py dry-run 与显式安装、官方 scaffold helper 和 codex plugin add；不套用 Plugin Manager 自身安装器。既有 marketplace 备份并逐项保留。安装后 release 73 载荷哈希一致，Codex installed/enabled/version、源与缓存一致、实际 stdio 12 工具/health 均通过；系统专用链接由 7871 未监听状态启动到 HTTP 200。保留旧 17872；不动旧账号/任务、不安装模型、不提交或部署、不操作付费。详见 07 新增安装记录。刷新 Codex 后的新任务发现、线上登录交接与 WorkBuddy 仍待验收。

## 0.5.0 统一登录入口

- 用户指定原 CZMIYOU 统一登录页面及产品 4 点击后本机打开/下载流程。同步修改登录系统 src/app/user/page.tsx、public-config 与新增 yzzh-launcher.ts；不改其他产品跳转或中央表。保留用户原 next-env.d.ts 修改。未提交、推送、部署或注册系统协议。
- 插件新增 launcher.py、launch.js、固定路径启动及显式注册脚本，未登录的 open_workbench 回到原登录页。nonce 防重放、精确窗口与来源、在线复核、账号隔离与时间卡门禁保留。README 和项目 Skill 同步更新；这些是本项目登录契约，不修改全局通用 Skill。
- 实际 Chrome 两个回环来源联调：首次发现 launch.js 因原 UI static_folder 覆盖而 404，修复并补 HTTP 字节回归；再次点击到原衣装页成功，设置显示合成账号及有效时间卡。报告 verify_calls=1、logged_in=true、licensed=true、version=0.5.0；real_account/paid_generation/native_wake 均 false。下载兜底已真实打开固定 GitHub 地址。
- Python 3.12 定向回归：launcher 7、auto_upload 10、BYOK 21、original/login 24、hybrid 47 均通过；中央 TS 3/3、tsc --noEmit 通过。pnpm exec 被仓库未批准 build scripts 门禁拦截，未替用户批准；用已安装的纯 JS TypeScript/ESLint 执行检查，未换包管理器。仅清理这次工具自动生成的无关 pnpm-workspace.yaml，不动用户文件。
- 源码版本 0.5.0 不替换实际 17872 的 0.4.0 预览，以免线上旧入口尚未发布时打断使用。系统协议冷启动、真实 HTTPS 入口、实际账号和双宿主安装不计为已通过。

## 0.4.0 自动上传与 AIGC 范围

- 修改 yzzh_local/media.py、original_media.py、provider/settings/runtime/original_worker 和附加面板，原 web/ 源文件未改。普通图片可随单段 API 内联，需公网的图片/视频走明确告知的 Litterbox；旧自有 TOS 保留，不静默切换。
- 实测排除了 temp.sh：返回 HTML 下载页而非模型可读取文件；独立实现 Litterbox 官方 multipart 协议，合成 PNG/MP4 上传并验证 Range 字节及总大小均通过。没有上传客户媒体、读取客户 Key 或调用付费接口。
- Python 3.12 auto_upload 10、original 13、BYOK 21、login 11、hybrid 47 全部通过；不替代供应商验收。新包 69 文件，manifest/skill、安装前 release 校验和 setup --dry-run 通过。隔离依赖安装仅在 /tmp，无权重和全局注册。
- 最终 0.4.0 包：dist/czmiyou-yzzh-0.4.0-aigc-preview.zip，SHA256 50a34b32b4cb2905fa1643dee5029a1abdd3b1a33cf7fd9add2aa69e88b9408d。实际 bundle stdio/FFmpeg 报告在 dist/autoupload-bundle-evidence；最后增量为显示文案，JS 检查及包校验通过。
- 原 17872 进程 63326 经只读检查（辅助任务 0、无原流程运行/网络记录）后正常停止；保持 /tmp/yzzh-byok-preview-20260906 数据目录，用 0.4.0 解压包启动。通过正常 open_workbench 入口在 Chrome 打开，不读取连接凭据，不代登录。浏览器实际显示写实虚拟人像入口；用户随后要求改用统一登录页及产品启动按钮，进入新的增量。

## 0.3.1 登录修复（2026-09-06）

- 输入：用户报告 LOGIN_REJECTED，确认身份为员工，并授权修复。源码定位固定 role=customer，另将账号密码错误与身份无效/未审批合并显示；只读知识线索保持 draft，不覆盖当前代码。
- 改动：account.py、runtime.py、app.py、portal.js、辅助 web 入口增加必选身份与严格角色白名单；受控错误映射覆盖 200/401/403 拒绝，禁止角色自动重试。两个 manifest、MCP/runtime 版本更新 0.3.1；原 web/ 与 web_app.py 未改。
- 回归命令：`/tmp/yzzh-original-package.Lyucwl/extracted/czmiyou-yzzh/.venv/bin/python -m unittest discover -s tests -p '<pattern>'`，pattern 分别 test_plugin_login.py（11）、test_byok*.py（21）、test_original_plugin.py（13）、test_hybrid*.py（47），共 92/92。node --check 两个前端 JS、git diff --check、plugin/skill/roster/project 验证均通过。
- 分发：新 ZIP `dist/czmiyou-yzzh-0.3.1-login-preview.zip`，不覆盖旧包；/tmp/yzzh-login-package.m9PKL3/czmiyou-yzzh 经 67 文件校验、setup --dry-run 和隔离 Python 3.12 安装，无模型下载/全局安装。解压包实际导入新运行时，11 登录测试通过；`scripts/verify_hybrid_runtime.py --data-dir /tmp/yzzh-login-bundle-check-20260906 --evidence-dir dist/login-bundle-evidence --bundle /tmp/yzzh-login-package.m9PKL3/czmiyou-yzzh` 通过，报告及可解码合成产物保留。
- 预览：停止前只读确认 17872 旧进程 60163 未登录、无项目，再正常终止并按原 data-dir 重启；没有删除数据。新服务 0.3.1；浏览器实际选择员工，未代填密码或点击登录。
- 用户自行登录后观察：页面显示已登录及产品 4 时间卡有效，服务只读状态二次确认 logged_in=true/product_4_licensed=true/provider_configured=false。刷新旧 guest 标签页后出现账号/Key 入口，无账号切换警告。未复制账号身份信息、密码或 token 到证据文件；未推送、部署或付费调用。

## 0.3.0 原 UI 修正

- 用户确认保留原界面。新增 original.py / original_worker.py / web/portal.js / offline_audio.py；主入口复用 web/，按账号私有进程接原 web_app。原业务和 web/ 文件未改，历史 runs 不归入未知账号。
- 补丁包括会话/配置绑定、外发冻结与逐次批准、网络记录持久化、未知响应拒绝重提、禁用全局 netrc/代理、原媒体路径绑定、角色图片私有代理、精确到期恢复白名单。
- 新增 original_analyze / original_status；单段工作台移到 /agent-workbench，open_workbench(view=agent) 可定位其审批。
- 首轮实测修复 Waitress 端口类型和日志目录创建；独立审核修复下载 query 冲突、异常 2xx 判定、缩略图签名/CSP、到期恢复门禁、netrc 干扰。人物 H5 未知回调明确先本地拒绝。
- Python 3.12：original 13/13、BYOK 21/21、hybrid（含 package）47/47；原页面资产对比通过。插件验证器缺 yaml 时换用已有 .venv-codex 后通过，没有向客户运行依赖增加 yaml。
- 完整 Python 3.9 回归 332 项，325 通过、6 项同原基线失败、1 项原引擎集成因 Python 版本跳过；该集成已在 Python 3.12 实际通过。日志 /tmp/yzzh-original-full-tests-20260906.log。
- 源码与解压包运行 scripts/verify_hybrid_runtime.py：12 个 stdio 工具、单段分镜/导出、原长视频双分镜/解码均通过。报告 dist/original-source-evidence/runtime-report.json、dist/original-bundle-evidence/runtime-report.json。
- 0.3.0 ZIP 包含 67 个白名单载荷；解压、验证、无模型依赖安装与内容对比完成。没有真实登录、Key 使用、付费调用、宿主全局安装、部署或 Git 推送。

| 时间 | 动作 | 证据 |
|---|---|---|
| 2026-09-06 | 创建项目档案 | 本文件夹及 `project.json` |
| 2026-09-06 | 用户确认面向客户并保留 CZMIYOU 登录、时间卡、中央计费 | `00-interview.md` |
| 2026-09-06 | 核验当前源代码和历史账号集成改动 | `git status --short --branch`、`git diff --stat 7f30c61 ab494ce`；当前 main 为 1e8fb7a，原版无 miyo_integration.py |
| 2026-09-06 | 盘点现有 WebJob、处理阶段、任务恢复与文件接口 | `web_app.py`；详细证据见 `07-delivery-evidence.md` |
| 2026-09-06 | 核对宿主官方文档并登记客户端密钥隔离要求 | `07-delivery-evidence.md`；未安装插件、未调用付费服务 |
| 2026-09-06 | 等待用户确认视频处理位置 | 仅更新需求档案；原业务代码和部署保持不变 |
| 2026-09-06 | 用户确认本地＋云端并要求继续，落实第一条单段切片 | 01—04 档案；不重写旧网页，不恢复整批历史提交 |
| 2026-09-06 | 新增本地工作台、持久任务、10 个 stdio MCP 工具 | yzzh_local、hybrid_shared.py；合成视频实际导入、分镜、恢复、导出 |
| 2026-09-06 | 新增独立中央网关，复用历史服务端产品 4 契约 | yzzh_cloud；模拟签名/时间卡/价格/计费，真实回环 HTTP multipart 联调 |
| 2026-09-06 | 修复写入门禁要求的技能 UI 元数据 | skills/yzzh/agents/openai.yaml；技能名单、插件清单和技能验证通过 |
| 2026-09-06 | 独立审核返工 | 报价/导入/提交账号竞态、待对账保护、审批素材快照预览、明确 409 重新规划；见 06-review.md |
| 2026-09-06 | Python 3.12 隔离安装首次失败，定位并修复 | uv Python 被 EnvBuilder 默认复制后无法找到 encodings；POSIX 改为 symlinks，与标准 venv 行为一致，重建解压目录后安装通过 |
| 2026-09-06 | 客户包独立运行与浏览器检查 | 解压包 scripts/launch.py → stdio → 独立本地后台 → 分镜 → MP4 导出和解码；浏览器预览 readyState=4、320×180、4 秒 |
| 2026-09-06 | 分发增量复查返工 | 补完整 24 文件结构与同步改清单的缺/多文件拒绝测试；清单路径改 as_posix，Windows/POSIX 相对路径回归通过 |
| 2026-09-06 | 最终 focused、全量与静态检查 | 46/46 新测试；297 全量中 6 个既有失败；compileall、node --check 和 ZIP 篡改检测通过 |
| 2026-09-06 | 交付边界 | 未安装到用户宿主，未部署、推送、真实登录、私有素材上传或收费；安装与业务验收等待下一步授权和云端环境 |
| 2026-09-06 | 用户确认登录和时间卡仍需保留，生成使用客户自己的 Key | 更新 00—03：账号中心只授权产品 4，供应商费用直付；04 标明旧架构进度不适用于新范围 |
| 2026-09-06 | 只读核验现有授权接口 | 账号中心 POST /api/auth/verify-token 已返回服务端筛选的有效订阅，包含叠加时间卡兼容；产品 4 线上实测仍待授权，不把本地源码当作线上部署证明 |
| 2026-09-06 | 本轮边界 | 仅说明方案及同步需求/设计文档；未改运行代码、旧 ZIP、账号中心或数据库，未部署、安装、推送、读取密钥或调用付费服务 |
| 2026-09-06 | 用户要求“改造”，完成 BYOK 主链路 | yzzh_local/account.py、settings.py、provider.py、runtime.py；产品 4 只授权、客户直付，旧运行时放入 yzzh_cloud/legacy_client.py 仅做历史回归 |
| 2026-09-06 | 工作台、技能与包改为 BYOK | 登录、私有设置页、目的地/费用审批、会话绑定；空 .env.example、TOS 依赖、默认账号接口；28 文件白名单，中央服务不进客户包 |
| 2026-09-06 | 独立审核发现并修复设置页跨账号竞态 | 旧标签保存/清除强制 expected_owner + expected_session，UI 切会话清 Key；独立 19/19 与双 HTTP 客户端复查通过 |
| 2026-09-06 | 本地验证 | BYOK 21/21、旧网关及包 47/47；全量 319 中 313 通过，原 6 项基线失败保持；Python/JS/插件/技能/结构验证通过 |
| 2026-09-06 | 实际源码与解压包运行 | dist/byok-source-evidence 和 dist/byok-bundle-evidence：stdio 10 工具、回环假账号产品 4、实际 FFmpeg 分镜、4 秒 MP4 导出解码；包内 Python 3.12 安装包含 TOS，无全局安装和权重下载 |
| 2026-09-06 | 独立真实格式预检 | 960×544 @ 24fps、4 秒合成参考视频通过 ArkProvider 本地预检，实际导入 TOS；未请求供应商，不作为真实生成验收 |
| 2026-09-06 | 新版首屏检查 | http://127.0.0.1:17872/ 实际浏览器显示 BYOK 0.2.0、默认账号地址、未登录设置禁用、用户费用说明；未填真实账号或 Key |
