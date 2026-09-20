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

## 当前状态（2026-09-19）

- 主干功能完整；消耗器已上线（费率实测校准、AIMD 自适应并发、账本持久化）
- `retry.max_credentials_per_deployment` 已从 6 提到 8（试满全部商汤账号，
  **重启网关后生效**）；会话残留（*.mock.bak、*.stale、旧库备份）已清理
- 对抗审查（2026-09-12）：git 历史无密钥、鉴权常量时间比较且 None 安全、Docker
  不烤密钥（.dockerignore 已补）。**遗留决策**：`ZKAI_HOST=0.0.0.0` 且未设
  `ZKAI_API_TOKEN` → /v1/* 对局域网开放（启动时会警告）；要收紧就在 .env 设
  `ZKAI_API_TOKEN` 并同步所有客户端配置
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
- 门禁 ruff / mypy / pytest 全绿（2026-09-20 起 421 passed；推送走本机代理，
  直连常被重置——见记忆 github-push-via-local-proxy）
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
