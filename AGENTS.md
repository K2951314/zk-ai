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
- 门禁 ruff / mypy / pytest 全绿（2026-09-19 起 371 passed；推送走本机代理，
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
