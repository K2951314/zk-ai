# AGENTS.md — ZK-AI

个人 AI 网关：OpenAI 兼容的多供应商密钥池网关（商汤日日新 / NVIDIA NIM / 魔搭 / Kimi 官方），
带能力路由、别名、429 冷却与用量统计；另含一个积分池感知的商汤积分消耗器。

## 怎么跑

- 双击 `scripts\start_gateway.cmd`（默认端口 8317，可传参换端口）；手动等价命令：
  `.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8317`
- 依赖用 uv 管理（`uv sync` 自动装 Python 3.12+）；首次先 `python scripts/init_db.py` 建表
- 提交前门禁必须全绿：`ruff check` + `mypy` + `pytest`（README §20）

## 技术栈

FastAPI + httpx + SQLAlchemy 2.x (async/sqlite) + pydantic-settings；Python ≥ 3.12；uv。

## 目录与约定

- `app/` 交付代码（api/services/routing/providers/credentials/retry/database）；
  `tests/` 全 Mock 离线测试；`scripts/` 运维脚本与启动器
- **密钥红线**：真实 Key 只存 `.env`（已 gitignore）。所有被 git 跟踪的文件
  （含 `.env.example`、README、config/*.example.yaml）绝不出现真实密钥
- `config/*.yaml` 是本地现役配置（已 gitignore），`config/*.example.yaml` 是可提交模板；
  行为变更两边同步
- **`.cmd`/`.bat` 必须纯 ASCII + CRLF**：中文会让 cmd 字节错位误解析（生成乱码文件/跳行）；
  CRLF 由 `.gitattributes` 强制，ASCII 靠自觉；中文文案放对应 Python 脚本里
- 文档入口：`使用手册.md`（面向使用，先看这个）→ `README.md`（全量参考）

## 已知坑（不看会犯错）

- 网关出站走 **Windows 系统代理**（httpx 默认 trust_env 读注册表代理 127.0.0.1:10808）；
  代理进程没开 → 所有上游 ConnectError。独立脚本调国内 API 应 `trust_env=False` 直连
  （`scripts/burn_sensenova.py` 已这样做），网关不能一刀切关（NVIDIA 需要代理）
- 商汤积分池：flash-lite 扣完专属池会**静默**溢出扣通用池（kimi-k3 的积分），
  API 无任何池信号；消耗器靠按积分记账 + 预算熔断防溢出，勿轻易关预算
- 商汤窗口刷新模型默认按滚动记账；控制台显示的每账号「重置时间」可能是固定锚点
  （判定方法见使用手册），确认后用 `--anchors "1=HH:MM;..."` 切固定窗口爆发模式
- 商汤额度按**账号**算：01+02 同账号，07~09 归属待核对；额度上限见 providers.yaml 注释
- **消耗器会挤占 K3 的 TPM**（2026-09-19 实测）：满速烧 flash-lite 时同账号 K3 报
  `inference exceeds tpm/rpm limit`（疑似账号级 TPM 跨模型共享）——K3 频繁 429 先查
  消耗器是否在烧，用它时把消耗器停掉或调低 `--per-account-max`
- **`retry.max_deployments` 是路由计划硬顶**：按 zk-k3 的 3 部署估成 4，zk-auto 全链
  6 部署时 DeepSeek/Qwen 根本进不了计划，坏渠道烧光 `max_total_attempts` 就整体失败
  （已改 8）。**NVIDIA `max_retries` 会让挂起重试翻倍墙钟**（60s 超时→120s+/次），
  已设 0——挂起交给上层冷却+换渠道，别盲目重试
- **数据库并发串行化（2026-09-19）**：`:memory:`/池化 aiosqlite 是单连接，两个
  AsyncSession 在同一连接上事务互相踩——实测表现为 UPDATE 静默丢失、空事务
  COMMIT 报错。`Database.session()` 已加进程级 asyncio.Lock 把事务串行化；
  新 repository 别绕过它自己开连接，也别在持有 session 时做长耗时工作
- **`.venv` 不会跟着 `pyproject.toml` 自己长**（2026-09-21 实测）：加了依赖不 `uv sync`，
  表现就是「双击后闪退、任务栏连图标都没有」——`app.main` 在 `ruamel.yaml` 上
  ImportError（网关秒退），托盘在 `PIL`/`pystray` 上 ImportError（**托盘自己没起来**，
  所以不是蓝图标而是压根没图标），而旧版 `launch_hidden.py` 把托盘输出丢进 DEVNULL、
  `.cmd` 无条件关窗，全程一个字都看不到。现在：托盘自身输出落 `data/tray_<mode>.log`，
  起来就退 1 并打印日志尾部，`start_gateway.cmd` 前台等端口 30s，两条失败路径都
  pause+指日志。**本机装包用 `uv sync --default-index
  https://pypi.tuna.tsinghua.edu.cn/simple`**：PyPI 直连和走 10808 代理都能挂死，
  镜像几秒完成；代价是它会把 `uv.lock` 里的 URL 改成镜像域（哈希不变，纯 URL churn），
  同步完 `git checkout -- uv.lock` 复原
- **导入迁移包前必须先停网关**（2026-09-21 为这条流过血）：`import` 覆盖 `data/zkai.db`
  时，若同目录还留着上一代的 `-wal`/`-shm`，下次开库会把别人的 WAL 帧回放到新库上 →
  `database disk image is malformed`，且**不是警告是真损坏**（本机 `requests`/
  `usage_records` 就是这么废的；SQLite 的 `-wal`/`-shm` 不属于迁移包内容）。现在
  `import_package` 先把 side-car 备份进 `imports_backup/<时间戳>/` 再删除，CLI 侧只要
  端口有人听就直接拒绝。验证损坏库要用 sqlite3 backup API 取一致快照再 `PRAGMA
  integrity_check`——直接 `cp` 正在写的库必然报假阳性
- **`~/.codex/config.toml` 归 ChatGPT app 所有，只能外科手术式 patch**（2026-09-22）：
  整文件重写会毁掉 app 的 mcp_servers/plugins/projects/desktop 段落（真实文件上百行）。
  `chatgpt_service.patch_text` 只增改我们的键：顶层键只可能在第一个 `[section]` 前动，
  provider 表只在其表体内动；`apply_config` 读文件必须 `newline=""`（`read_text()` 会把
  CRLF 归一化成 LF，悄悄改掉 app 的换行风格）；`auth.json` **永远不写**（运营决策）。
  改 `env_key`/`ZKAI_API_TOKEN` 后记得桌面版要重启才读到新环境变量；换机导入时
  `migrate._provision_chatgpt_env` 会把它写进 HKCU\Environment 并广播+回读校验，别在测试里
  直接跑那个函数（会碰真实用户环境，测试要 patch `write_user_env_var`）。
  桌面版报 `Missing environment variable: X` = 用户级环境变量缺 X：诊断看
  `GET /admin/chatgpt` 的 `env_key_visible`/`env_key_matches_gateway`（陈旧令牌会 401），
  一键修复 `POST /admin/chatgpt/sync-env`（或导入输出的 setx 命令），然后重启桌面版。
  **客户端 `model` 写错（如 'zk'）桌面版每条消息 404**：`PUT /admin/chatgpt/client`
  在 zk-ai 模式下按 `config.known_names()`（全部模型+别名，即路由器准入口径）
  硬拦截未知模型并附可用清单（09-23 从警告升级为 400）；路由器的
  `ModelNotFoundError` 文案会带相近名（前缀优先 + difflib 兜底）。
  配置目录可用 `ZKAI_CODEX_HOME` 覆盖（多开用户 / 测试隔离），默认 `~/.codex`。

## Agent 工作方式（2026-09-23，防半途而废）
- **本机 shell 是 PowerShell，here-string 会把中文和转义写坏**：写含中文的临时脚本时
  先用 Python 以 ASCII + `chr()` / `\uXXXX` 生成文件再执行，别直接把 here-string
  粘进命令行；多行一次性命令还可能被策略整段拒绝，表现为「做到一半停了」
- **回复全程用中文**：文件路径、命令、字段名保留原文，但叙述不用英文；用户把「回复夹英文」
  列为明确不满，动手写每句话前先确认这一点
- 临时脚本统一放 `exports/_*.py`，跑完即删；不要把审计脚本留在工作区

## 当前状态（2026-09-19）

- 主干功能完整；消耗器已上线（费率实测校准、AIMD 自适应并发、账本持久化）
- `retry.max_credentials_per_deployment` 已从 6 提到 8（试满全部商汤账号，
  **重启网关后生效**）；会话残留已清干净（`*.mock.bak` 于 9-19，`data/*.stale` 与
  `data/zkai.db.bak-*` 于 2026-09-21）
- 对抗审查（2026-09-12）：git 历史无密钥、鉴权常量时间比较且 None 安全、Docker
  不烤密钥（.dockerignore 已补）。**遗留决策（2026-09-20 已收紧）**：
  `ZKAI_HOST=0.0.0.0` 曾且未设 `ZKAI_API_TOKEN` → /v1/* 对局域网开放；现已设
  `ZKAI_API_TOKEN=zk-ai-local`（.env + 用户级环境变量——桌面版 app 从 explorer
  启动不继承 shell 变量，两个来源都要有），POST /v1/* 需要
  `Authorization: Bearer zk-ai-local`（GET /v1/models、/health 按设计免鉴权）。
  **换机器/清环境变量后要同步所有客户端**（ZCode provider、cc-switch 等都发这个值）
- 2026-09-13：`ZKAI_STRIP_REASONING=true` 折叠思考已生效（含回填进正文的思考，
  README §18 缺陷 11）；`.env.example` 补了 `ZKAI_API_TOKEN` 可发现行；
  双窗口模型（`--anchors`）就绪，待用户从控制台判定滚动/锚点后填值
- 2026-09-13（晚）：新增原生 **`/v1/messages`（Anthropic 协议）端点**  （`app/api/messages.py` + `app/models/anthropic.py`，`ZKAI_ANTHROPIC_DEFAULT_MODEL`
  兜底 claude-* 模型名）。起因：CC Switch 的 openai_chat 翻译层缺
  `content_block_stop` → Claude Code 收到空回复（对照实验实锤）；CC Switch 里
  zk-ai 供应商已切 `anthropic` 直通（BASE_URL 去掉 `/v1`），`claude -p` 与
  工具调用端到端验证通过。次要问题待观察：商汤 K3 对 2.5 万 token+ 请求会
  TPM 429（重试风暴）、moonshot 部署无可用 Key、nvidia 偶发流式连接超时
- 2026-09-17：①新增 **NVIDIA glm-5.3 / glm-5.3-flash** 模型，且 `/ui` 控制台支持
  运行时**新建/编辑/删除模型与别名**。②新增**主动配额限速**
  `app/routing/limits.py`：滑动窗口，支持按请求数（`max_requests`）与 token 数
  （`max_tokens`）双口径、credential/account/provider 三作用域；NVIDIA 单账号
  40rpm、商汤按账号 5h+周 token 预算（约 300M/3G，积分折算的保守值，控制台可校准）。
  scheduler 选中即记账、成功后回喂 token；到线的 Key 直接跳过。③限额可在控制台
  「凭据池 → ⚙ 限额」运行时改（`PUT/DELETE /admin/providers/{id}/limits`）。
  **坑**：真实模型响应常 >60s，串行测 rpm 限额会因窗口滑走而全过——验证要用并发 burst。
  计数持久化 `data/rate_limits.json`（脏才写，测试不污染）
- 2026-09-18：①**控制台写操作改为直接回写 YAML**（`app/core/config_writer.py`，
  ruamel 保注释往回编辑；删除走文本行拼接，因为 ruamel 重排会乱注释归属）。
  之前只存 DB 镜像 → 删掉的模型/别名在 reload/重启后被 YAML 复活。现在
  **文件是唯一真相**：写失败整体 500 回滚，models/aliases 不再从 DB 回叠
  （`apply_db_overrides` 只留限额一项兜底）。每步留 `<name>.yaml.bak`。
  ②**网关/消耗器收进系统托盘**（`scripts/tray_launcher.py`，pystray 自绘蓝绿图标；
  绿=运行、蓝=停止/静默超时；右键查看日志/打开控制台/重启/退出）。
  **坑**：uv 的 `.venv\Scripts\python(w).exe` 是 trampoline，会再启动一次真解释器，
  `CREATE_NO_WINDOW|DETACHED_PROCESS` 不会跟着传递 → 最内层 python.exe 自己新建
  控制台窗口（任务栏残留）。必须读 `pyvenv.cfg` 的 `home` 直接用 base 目录的真
  `pythonw.exe` + `__PYVENV_LAUNCHER__` 指回 venv；port_guard 及其
  powershell/taskkill 子进程也都要 `CREATE_NO_WINDOW`
- 2026-09-18（晚）：①控制台**增删供应商**（POST/DELETE `/admin/providers`，
  回写 `providers.yaml`，编辑不动已有 Key/限额；`config_writer.upsert_provider`
  保护 credentials 列表——`_sync_entry` 会把 payload 没有的字段删掉，这是修复过的
  坑）。②模型市场加**实时搜索**（搜模型名/说明）。③凭据池页加「➕ 加 Key」
  「🏢 供应商」按钮。④README §13.2 端点表与控制台描述补齐。
- 2026-09-23（全站汉化 + 控制台视觉重构）：**界面、接口报错、脚本输出全部中文**。
  - `app/web/index.html` 重做为「**左侧导航 + 内容区**」：侧栏分组（监控 / 配置）、
    顶栏只留连接状态 + 令牌 / ChatGPT / 主题 / 刷新；KPI 改大数字卡（`.kpi`），
    供应商卡 `.prov-card`，「需要处理」面板改 `.attention` 分级列表，弹窗统一
    `.modal-actions` / `.modal-sub` / `.facts-card`；深浅双主题、窄屏侧栏降级为
    顶部横滑。**JS 一行未改**，DOM id / `data-*` 钩子 / 函数名全部保留——
    重构靠 `tests/test_console_zh.py` 守住契约（视图/弹窗/导航/顶栏 id 与
    `data-view` 逐项断言）+ 无残留英文 UI 文案。
  - `app/web/agent.html` 视觉对齐（同一套 CSS 变量与组件语言），交互逻辑未动；
    会话状态码加了 `AGENT_STATUS_ZH` 中文映射（pending/执行中/待批准/…）。
  - 接口报错 message 全中文（`app/api/*.py`、`app/routing/router.py`），
    **`error.type` 机器码与 HTTP 状态码一律不变**；`summary=` / `Query(description=)`
    （`/docs` 里给人看的）也一并汉化。
  - 脚本输出汉化：`launch_hidden.py` / `open_console.py` / `health_check.py` /
    `tray_launcher.py`。`.cmd` 仍保持纯 ASCII + CRLF，中文只放 Python 侧。
  - 测试断言同步更新（`test_launch_helpers.py` 的两处英文提示）；门禁
    ruff / mypy / **523 passed**。
- 门禁 ruff / mypy / pytest 全绿（2026-09-23 全站汉化与控制台重构后 **525 passed**，含 `tests/test_console_zh.py` 11 例守卫）；`git push` 走本机代理
  （`git -c http.proxy=http://127.0.0.1:10808 push`），直连 github.com 常被重置
- 消耗器费率已两次控制台实测交叉校准（实际 ≈入111/出333 积分/百万token，区间
  111~240/333~720），默认 120/360 显示贴合实扣；账本持久化在 `data/burn_state.json`
  （重启不清零），`--calibrate-actual 实扣数` 可随时精校准；并发 AIMD 自适应
  （全局≤128、单账号 8→24、429 减半/静默每分钟 +1），429 频繁就调低 `--per-account-max`
- 2026-09-19：①**修积分池溢出事故**（用户实测专属池烧穿、吃掉 K3 通用池积分）：
  根因 = 周预算滚动记账「遗忘」旧消耗 + 费率取实测区间下沿 → 熔断线（54 万/周）
  按烧速（≈27 万/周/账号）物理上永远触不到。修复：周预算改**固定窗口**（自
  `--week-anchor` 默认周一 00:00 起累计，到线停靠至下周锚点）、`--safety-margin`
  默认 0.9→0.45（内建 2 倍费率不确定性）、新增 `--pool-total-credits` 绝对上限
  （到线永久停靠）、额度耗尽错误升级为「溢出实锤」ERROR 告警。行为变化：全速烧
  到周预算线（≈27 万积分/账号）会停靠等下周——**预期行为，不是 bug**。测试
  `tests/test_burn_budget.py`。②新增 **ZK-Agent 任务台** `/ui/agent`（Codex 式
  批量任务跑批器，网关进程内）：`app/services/agent/`（loop/tools/confirm/prompts）
  + `/admin/agent/*`（REST+SSE）+ `app/web/agent.html`；6 工具两段式审批
  （写文件/跑命令需用户批准，diff/命令预览，破坏性命令硬黑名单），工作区路径
  锁定（resolve 后必须落在 `ZKAI_AGENT_WORKSPACE` 内），步数/并发双熔断，
  transcript 行即 LLM 重放历史（`agent_sessions`/`agent_messages` 表，重启可续）。
  托盘菜单加「打开 Agent」；`ZKAI_AGENT_*` 配置见 .env.example。定位：批量跑批
  + 网关展示窗，不做 IDE 替代品（Web 先行，CLI 壳/模型分级路由留待后续）
- 门禁 ruff / mypy / pytest 全绿（371 passed）；E2E 实测（8400 临时实例）：会话
  创建→真实路由→tool_calls→审批→命令执行→结果回填全链路通；第二步因当天
  K3/GLM 链路整体超时正确转入 failed（上游波动，非 Agent bug）
- 2026-09-20（供应商生命周期 + 易用性收口，实测坑）：
  - **运行时改 provider 必须同步 adapter 表**：`Router._adapters` 只由构造/reload 重建，
    运行时增删/禁用 provider 要走 `upsert_adapter()` / `remove_adapter()`，否则下一步
    探测模型报 `provider 'X' is unknown or disabled`（500，极具误导性）
  - **Key 只进 `.env`**：控制台 `write_env` 写 `.env` 并即时写 `os.environ`（无需重启生效），
    YAML 只记变量名；`looks_like_secret()` 拦「40+ 位无分隔串填进 ID 栏」（真出过事：
    Key 曾以 credential id 形态流进 yaml/db/界面/日志）。改已泄露的条目记得连 `.bak`
    和 `data/gateway.log` 一起清
  - **Anthropic 系供应商两个坑**：①`/messages` 认 `x-api-key` 但 `/models` 可能只认
    `Authorization: Bearer`（StepFun 即如此），`list_models`/`model_catalogue` 要带 Bearer
    回退；②`thinking` 块不能丢——小 `max_tokens` 截断在思考阶段会让客户端拿到空正文，
    按项目约定进 `reasoning` extra 走 `_recover_reasoning_only_content` 回填
    （该 helper 在 `ProviderAdapter` 基类；tool_calls 轮不回填，否则给工具调用粘上思考文本）
  - **启动即用**：`scripts/open_console.py` 等端口后带 `?token=` 开控制台（令牌变了才需重粘）；
    `scripts/first_run.py` 自动从 `.example` 建 `.env`/`config/*.yaml`；首页「⚠️ 需要处理」
    面板列出没值的 Key / 没挂模型的供应商，每行一键修复；`/providers/{id}/models` 失败
    返回带 note 的 200 而不是 500
  - **模型能力以实测为准**：`app/models/discovery.py` 归一各厂 `GET /models` 的元数据
    （**未提及的字段恒为 None，绝不编造**），实测压过手写预设，前端区分「实测/预估」；
    无实测时才退回 `presets.py`
  - **`upsert_list_entry` 的 `_sync_entry` 会删 payload 里没有的键**——改模型条目必须把
    `deployments` 原样带回，否则整条被清空（已靠滚动 `.bak` 恢复过一次）
  - 测试环境（`environment == "test"`）的容器不写 `data/rate_limits.json`（此前一直
    在污染，旧文档声称的「测试不污染」不成立）
  - 新增供应商保存后直接弹出加 Key 窗口（不用回首页再想起"供应商还不能直接用"）；
    加 Key 表单选好供应商后自动起好 id/变量名。前端注意：**先绑定 onclick 再触发
    click**，否则"自动探测"是空转（实测踩过）
  - `.gitignore` 的 `config/*.bak` 已放宽为 `config/*.bak*`（`.bak-xxx` 这类后缀
    原来匹配不到，`git add -A` 会把带密钥的备份带进仓库）
- 2026-09-20（晚）：①**`/v1/responses` 补齐到 Codex 可用**（`app/api/responses.py`
  的 `_ResponsesAssembler` + `ResponsesRequest` 全量翻译）：Codex CLI 0.154 起
  **删掉了 chat wire**（配 `wire_api="chat"` 直接报错），只走 Responses，旧实现
  （文本子集、无工具、SSE 直接发 `output_text.delta`）会被 codex 报
  `OutputTextDelta without active item` 丢回复。现在：扁平 tools→嵌套、
  `function_call`/`function_call_output` input items→assistant.tool_calls/tool
  消息、输出侧组装 function_call items、流式按标准事件链
  （created→output_item.added→content_part.added→delta→done→completed 完整
  负载）。**临时实例 E2E 实测通过**：codex exec 写文件→执行→验证全闭环。
  codex 接入配置见使用手册 §3.4（`[model_providers.zkai]` 纯增量，不依赖
  ChatGPT 账号）。测试 `tests/test_responses_api.py`。②**商汤 Key 结构调整**：
  01/02（同账号 A）已被操作员禁用（DB `credentials` 表 status=disabled），
  现役 = 03–09 共 **7 账号 7 Key，每账号一把**；`max_credentials_per_deployment`
  随之改为「单请求可轮换的账号数」，**删这行不会取消限制**（`from_mapping`
  回落默认 3），加账号时同步 +1。yaml 里 01/02 两条僵尸条目待清理；
  providers.yaml 注释 / README / 使用手册里「9 把 Key、最多 8 账号」的旧描述
  已一并更正
- 2026-09-20（深夜，托盘/桌面版接入实测坑）：
  - **`CREATE_NO_WINDOW | DETACHED_PROCESS` 会让 powershell 的 CIM 查询静默返回空**
    （rc=0、stdout 空）——`port_guard` 一直没被发现是因为它有 `/health` HTTP
    兜底识别。短命探测进程（CIM/taskkill）要用裸 `CREATE_NO_WINDOW`
    （`launch_hidden._PROBE_FLAGS`）；托盘 spawn 仍可用组合标志
  - **托盘「重启」原来会卡死菜单**：`_ChildProcess.start()` 最长阻塞 ~40s
    （terminate + port_guard + spawn）全跑在 pystray 菜单线程上，点击像死了。
    已改 `restart_child()` 后台线程执行，心跳（3s）负责重绘图标
  - **`launch_hidden.py` 加了防双开**：双击 cmd 原来会起第二个托盘，新托盘的
    port_guard 杀掉旧托盘的孩子 → 旧托盘变蓝、通知区留幽灵图标。现在启动前
    CIM 找同 mode 旧托盘，先 `taskkill`（无 /F，WM_CLOSE 让 pystray 自己撤图标），
    5s 宽限后 /F /T。测试 `test_launch_helpers.py` / `test_tray_launcher.py`
  - **ChatGPT 桌面版（26.915）接网关三要素**：`~/.codex/config.toml` 的
    `[model_providers.zkai]`（wire_api="responses"）+ `ZKAI_API_TOKEN` 必须设为
    **Windows 用户级环境变量**（app 从 explorer 启动不继承 shell 变量，缺了会回
    "Missing environment variable"）+ **重启 app**（运行中的实例不重读配置）。
    该版本桌面版**没有模型选择器**，模型完全由 config 的 `model` 决定；app 会用
    picker 里的旧官方模型名探测网关（收到 404 model_not_found，无害）后落到
    config 的 model。API key 登录态存 `~/.codex/auth.json`，重启不丢
- 2026-09-20（商汤工具历史校验坑，实测复现）：**sensenova 对 chat 工具历史的
  校验比 OpenAI 严**——①`function_call_output` 的 call_id 没有对应
  function_call（孤儿输出，如编辑/打断过的会话重放）②对话以未应答的
  function_call 结尾（悬空调用）——两者都直接 HTTP 400 `inference request is
  invalid` 且**不故障转移**（400 归类为客户错误），桌面版一旦重放到这种历史就
  永远打不通。`ResponsesRequest._translate_input` 已修复：连续 function_call
  合并进单个 assistant 消息、孤儿输出丢弃、悬空调用立刻补一条
  `(no output recorded)` 的 tool 消息（紧跟其 assistant 消息）。回归测试
  `tests/test_responses_api.py`。另：本机 .env 设了 `ZKAI_API_TOKEN` 后会泄漏进
  测试容器导致全体 401——`make_config` 已钉 `api_token=None`（与既有
  strip_reasoning 钉法同源）

- 2026-09-21（路由两连修）：
  ①**控制台「实际模型」与供应商/凭据错配**——`resolved_model` 原本写别名计划链第一个
    （targets[0]），而非实际命中的模型；故障转移时必然与 provider/credential 列打架
    （例：显示 kimi-k3 但实际走了 StepFun）。`scheduler._final_meta` 已改为
    `candidate.model.id`。回归测试 `tests/test_api.py::test_resolved_model_reflects_the_served_target`。
  ②**ChatGPT 热切换原来不生效**——`zk-auto` 用 `strategy=capability`，targets 顺序
    不决定首选，能力分最高的 kimi-k3 恒赢。新增 `ModelAliasConfig.pin_first`（默认 False），
    `POST /admin/chatgpt` 切换时置 True，capability 策略检测到 pin 就把 targets[0]
    钉在首位、其余仍按能力分排。回归测试
    `tests/test_router.py::test_capability_strategy_pin_first_overrides_the_score_ranking`
    + `tests/test_api.py::test_chatgpt_hot_swap_takes_effect_on_the_next_request`。

- 2026-09-21（一键换机）：新增 `scripts/migrate.py`（export/import）+ 双击入口
  `scripts\export_machine.cmd` / `scripts\import_machine.cmd`，README §20.1、
  使用手册第 1 步均有说明。要搬的 = `.env`（全部 Key）+ `config/*.yaml` +
  `data/zkai.db`（用量/会话/限额）+ `burn_state.json` / `rate_limits.json`。
  **坑**：①迁移包和 `imports_backup/` 含明文 Key，`.gitignore` 已收
  `exports/` 与 `imports_backup/`；②加密只用标准库（XOR + PBKDF2 + 内嵌
  SHA-256），不为换机引入 `cryptography`/7-Zip 依赖——新机器只有 `.venv`
  可用；③`_snapshot_database` 走 sqlite3 backup API，网关在写也不会拿到
  半提交页；④导入默认**拒绝覆盖**，`--overwrite` 才覆盖且先备份到
  `imports_backup/<时间戳>/`。测试 `tests/test_migrate.py`（12 例：载荷清单、
  密文无明文 Key、往返、错密码 fail-closed、拒覆盖、备份优先、stale WAL 清理、
  无库时不动 side-car、网关在跑时拒绝、端口取自 .env、冲突退出码、错密码给人话）。

- 2026-09-21（晚，闪退排查→两处收口）：本机双击后窗口秒关、任务栏无图标，根因是
  `.venv` 停在 9-12 从未再 `uv sync`，`ruamel-yaml`/`pystray`/`pillow` 全缺（详见
  已知坑）。修的过程中补了两处能力：
  ①**启动失败必须说话**：`launch_hidden.py` 不再把托盘输出丢进 DEVNULL，落
  `data/tray_<mode>.log`，spawn 后 `_SETTLE_SECONDS`（1.5s）内托盘退出就打印退出码 +
  日志尾部并返回 1；`start_gateway.cmd` 据此分 `:trayfailed`，并把 `open_console.py`
  从 `--detach` 改成**前台等端口 30s**（`:nolisten` 分支），窗口只在真正失败时留下；
  `start_burner.cmd` 同步加检查。副作用：正常启动窗口多停 ~1.5s+网关启动时间才关。
  ②**导入不再制造损坏**：`import_package` 恢复 `data/zkai.db` 时先备份再删除目标机的
  `-wal`/`-shm`（`_DB_SIDECARS`），CLI 侧 `_gateway_is_listening()` 发现端口有人听直接
  拒绝（library 函数不探测，保持可脚本化）；`import_machine.cmd` 撞到覆盖守卫会问一句
  就地带 `--overwrite` 重试。测试共 +9（`test_launch_helpers.py` 托盘崩溃/日志落盘/
  存活三分支、`test_migrate.py` 上条所列 6 例），门禁 ruff / mypy / **445 passed**。
  ③**`tests/test_burn_budget.py` 一直在污染真实日志**：`_burner()` 没传 `--log-file`，
  `parse_args` 默认落到 `data/burn_sensenova.log`，于是每跑一次测试就往运营日志里追加
  几行「账号 K1 预算触顶 / 永久停靠」——而这个文件正是托盘心跳判绿/蓝的依据。已加
  autouse fixture 把 `DEFAULT_LOG` / `STATE_FILE` 重定向到 `tmp_path`（与 9-20 修
  `rate_limits.json` 污染同源，那次只修了网关容器这一处）。
  ④**migrate 的退出码成了 .cmd 的契约**：`_EXIT_CONFLICTS=3`（文件已存在，只有它才
  值得问 `--overwrite`）、`_EXIT_BAD_PASSPHRASE=2`（密码错/包损坏——原来直接甩
  ValueError traceback，手册却写着会给一句人话）、`_EXIT_ERROR=1`（含"网关在跑"拒绝）。
  改这几个码要同步 `import_machine.cmd` 的 `if errorlevel 3` 分支。
  **本机数据现状（已恢复）**：操作员 23:47 用 `exports/zkai-machine-20260921-221207.zip`
  重导，一致快照 `PRAGMA integrity_check -> ok`，9 张表全可读（requests 10402 /
  usage_records 5420 / request_attempts 29520 行）；导入前的旧文件在
  `imports_backup/20260921-234749/`，里面能看到被换掉的 `zkai.db-wal`(4.1MB)/`-shm`
  ——即本节 ② 那条新代码在真实导入里确实跑了。23:48 双击 `start_gateway.cmd`
  重启，新链路（托盘日志 + 前台等端口 + 自动开控制台）实测走通。

- 2026-09-22（ChatGPT 客户端配置修改 + 换机自动配置）：原来控制台「🤖 ChatGPT」只能把
  模型提到 `zk-auto` 链首热切换，`~/.codex/config.toml` 全程只读。现在：
  ①**期望配置落 `config/chatgpt.yaml`**（gitignore，模板 `chatgpt.example.yaml` 可提交；
  密钥不进去，值永远取 `.env` 的 `ZKAI_API_TOKEN`）。②**`app/services/chatgpt_service.py`**
  （纯 stdlib+pyyaml，migrate 也复用）：tomllib 读、**文本级外科手术写**——只动
  `model`/`model_provider`/`model_reasoning_effort` 三个顶层键和 `[model_providers.<名>]`
  表内的 `name/base_url/wire_api/env_key`，app 自管的 mcp_servers/plugins/projects/
  desktop 段落**字节级保留**；写前 `config.toml.bak-<时间戳>`；无变更=不写不备份（幂等）；
  保留 CRLF；支持 `mode: official` 一键切回官方（删 model_provider 行、provider 表留着
  随时切回）。**auth.json 永远不写**（操作员 2026-09-22 明确决定）。③控制台端点：
  `GET /admin/chatgpt`（desired/disk/drift/auth/gateway 全量状态）、
  `PUT /admin/chatgpt/client`（保存期望配置，`apply=true` 同时落盘）、
  `POST /admin/chatgpt/apply`（按期望配置重写本机）；`POST /admin/chatgpt` 热切换语义不变。
  ④**换机**：`config/chatgpt.yaml` 进迁移包；`import_package` 恢复后自动 patch 新机
  `~/.codex/config.toml`（新机器无 app 也先建最小配置）；`main()` 再把 `env_key` 指向的
  令牌写进 **Windows 用户级环境变量 HKCU\Environment**（winreg + 广播
  WM_SETTINGCHANGE，旧值备份进 `imports_backup/<stamp>/chatgpt_user_env.json` 并打印还原
  命令）——explorer 启动的桌面版读不到 shell/.env 变量，这是「上来就能用」最后一环。
  **09-22 晚事故修正**：provision 原本被 chatgpt.yaml 门控，漏掉「源机没存过它 +
  操作员手抄 ~/.codex」这条真实换机路径（桌面版报 Missing environment variable）。
  现改为 `.env` 门控；变量名按「磁盘 config.toml → chatgpt.yaml → 默认」解析
  （`_resolve_env_key_name`，磁盘是第一真相）；写后**回读校验**，`.env` 缺值时输出直接给
  setx 修复路径。配套：`GET /admin/chatgpt` 增 `env_key_name/env_key_visible/
  env_key_matches_gateway` 字段；控制台状态条标红 + 「🔧 同步令牌到用户环境变量」按钮
  （`POST /admin/chatgpt/sync-env`，写后同样回读校验）。
  测试：`tests/test_chatgpt_config.py` 44 例 + migrate 24 例 + test_api 11 例；门禁全绿。
  09-22 深夜对抗审查又加固 `apply_config`（坏 TOML 不碰 / 落盘前 tomllib+re-plan
  双验证 / 备份时间戳防同秒覆盖 / 替换行尾注释保留 / 标量字段禁换行）+
  `_provision_chatgpt_env` 接住注册表 OSError；配 9 个对抗回归测试，
  变异抽查 6/6 被抓（详见当日日志）。

