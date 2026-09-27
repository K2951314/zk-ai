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
- **`estimated_input_tokens()` 曾低估 2.67 倍，`context_fits` 因此空转（2026-09-25
  修）**：日志记 269,162 est，上游实报 719,090 prompt_tokens（中文负载低估 4.0 倍）。
  根因两条：①只累加 `message.text()`，**完全无视 `tool_calls.function.arguments`**
  ——真实 Codex 会话 1274 个 call 共 746,953 字符全部漏算；②`tools` 用
  `len(str(self.tools))`，Python dict 字面量比 JSON 短（单引号、`True`/`None`），
  31 个工具的 schema 被系统性低估。已修（`app/models/request.py`），回归测试
  `tests/test_token_estimator.py`（13 例，含图片 token 与「不许重复 //4」）。
  **注意 `context_fits` 还留着 10% 余量**，那是给分词器误差的；低估 2.67 倍时
  它反而帮倒忙，将来若接「估算值校准」要先想清楚这 10% 怎么处理。
  **2026-09-25 又补了图片 token**：图发自上游后（见下条）这部分是真实成本，
  实测 25 张真实截图 ≈ 34,699 tokens。**两个 part-type 集合别混**：
  Responses 侧是 `input_image/output_image/image`，chat 侧是
  `image_url/image/input_image`——翻译后 type 会变，估算器按像素算 token
  （解 PNG IHDR / JPEG SOF / GIF / WebP 头），**不能按 base64 长度**（实测
  每千字符 3,193~114,970 px，差 36 倍），且图片 token **不参与最后那个 //4**
  （否则 34,699 会被压成 8,676）。
- **413 有两道闸，别混为一谈（2026-09-25 梳理）**：字节闸（`app/main.py` 中间件，
  `ZKAI_MAX_REQUEST_BODY_MB`，默认 10MB）与 token 闸（`RequestService._guard_input_size`，
  `ZKAI_MAX_INPUT_TOKENS`，默认 0 关闭）。坑有四个：
  ①**字节闸只看 `content-length` 首部**，chunked 上传不带这个头 → `declared=0` →
     整个闸失效（Codex 的 `responses_retry` 带这个头，所以它撞的是"太宽"不是"太松"）；
  ②**字节闸的 413 早退分支以前不写日志**（跳过中间件末尾统一的 `logger.info`），
     本机 53,402 行 gateway.log 实测 `-> 413` 命中 0 次——客户端只看到一句
     「请求体过大」，网关侧查无实据。已补 WARNING（`test_api.py::
     test_oversized_body_leaves_a_log_trace` 守住，已验证原始代码上它会失败）；
  ③**两个字段都没有 YAML 桥接**（全仓 grep `ZKAI_MAX_REQUEST_BODY`/`ZKAI_MAX_INPUT_TOKENS`
     零命中），改 `config.yaml` 是静默无效——这正是"Codex 让你改 .env 而不是 config.yaml"的原因；
  ④**调大字节阈值的方向是反的**：10MB ≈ 290 万 est tokens，而现役最大上游窗口只有 1M
     （K3/GLM-5.3/StepFun），实测击穿点 body 4.99MB / 1.25M est tokens 时 0 个部署幸存
     ——放过去只会吃上游 400/429。要放行先有 token 闸兜着。
  token 闸挂在 `RequestService.chat` + `stream`（三条对外路径的收敛点，含流式——
  **流式不经过 `chat()`**，只挂一处会漏掉最大的那批），报错复用既有
  `ContextLengthExceededError`（413 + `context_length_exceeded`，`tests/test_errors.py`
  钉住它不换 Key 不故障转移），文案指出最大几段 + 怎么调。
- **Codex 截图的两种丢法，与 2026-09-25 的修复**：`app/models/request.py` 的
  `_flatten_text` 只认 `input_text/output_text/text/refusal`，`input_image` 被吞。
  实测同会话 `json.dumps(input)` 14,862,637 B 而 `chat.model_dump_json()` 仅
  2,983,328 B（**比值 4.98**），base64 图片 11,390,912 B 占 76.6%。
  顺带纠正一个流传的误判：本机 Codex 会话的**文本**工具输出中位数仅 445 字符、
  最大 9,282 字符，**没有任何文本巨物**；942KB 以上的全是 view_image 的 base64。
  所以「别再 cat 整份文件」打错了靶子。两种丢法都不是好事：
  ①**裸 `input_image` item**（真实 rollout 里 24/25 张是这种，直接躺在 input
     数组）被 `if kind and kind != "message": continue` **整条丢弃**——连
     "这里有过图"都不留；②混在 `function_call_output` 里的图变成**空字符串
     tool 消息**——发到上游等于撒谎「这个工具没返回内容」，模型据此判断会答错。
  **已修**（`_translate_input` 三处都转成 `image_url` content part），开关
  `ZKAI_FORWARD_IMAGES`（默认 true），关掉则回落一行占位文本。
  **代价必须一起校准**：图发自上游后上游流量从 2.8MB 涨到 29MB（**10 倍**），
  10MB 字节闸直接被触发——所以同时给估算器补了图片 token（见下面那条），
  否则 token 闸对图片失明（实测只报 50 tokens）。
  **`/v1/messages` 路径本来就没这个 bug**（`_image_part` 早已实现），不用改。
  测试 `tests/test_token_estimator.py` 5 例；真实 E2E 验过图片确实到上游。
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
- **`.env` 的值尾部混进 `\r` → 网关 bind 失败，报错却写成 `getaddrinfo failed`**
  （2026-09-24 换机后实测，排查绕了远路）：那份 `.env` 的**每一行**换行都是
  `\r\r\n`（多一个回车，共 115 行），于是 `ZKAI_HOST=0.0.0.0\r\r`。
  `str.splitlines()` 只吃掉作行尾的那个 CR，**值里剩的一个 CR 会一路传下去**：
  托盘进程环境 `ZKAI_HOST='0.0.0.0\r'` → uvicorn argv `--host '0.0.0.0\r'` →
  `socket.bind((host, port))` 走 `getaddrinfo` 解析 → `OSError [Errno 11001]
  getaddrinfo failed` → uvicorn `logger.error(exc)` + `sys.exit(3)`。
  **别把这个 errno 当 DNS 问题**：Windows 上 11001 是「解析地址/服务失败」，
  bind 解析 host 时抛的也是它。实测复现：`bind(('0.0.0.0\r', 8317))` 与
  `bind(('8317', 8317))` 都是这个错，而 `bind(('0.0.0.0', 8317))` 正常。
  **判定三连**（照顺序看，30 秒定案）：
  ① 日志里**有没有 `Uvicorn running on http://...`**——它只在 bind 成功后才打印，
     失败的那次永远没有；且报错紧跟在 `Application startup complete` 之后、
     `Waiting for application shutdown` 之前的**同一秒**（DNS 故障不可能这么快）。
  ② `python -c "raw=open('.env','rb').read(); print(raw.count(b'\r\r'), raw.count(b'\r\n'))"`
     ——两个数相等就是全文件 `\r\r\n`，写个开头为 `\r\r\n` 的替换即可（改前先备份，
     改后逐行比对确认内容零差异）。
  ③ 值含不可见字符时直接读进程环境块（PEB，`ReadProcessMemory`）比猜快：
     本机就是这么看到 `'0.0.0.0\r'` 的。
  **别再被 `shadowed by pre-existing process environment` 警告带偏**：它点名
  `ZKAI_HOST`/`ZKAI_PORT` 时，真相往往是**文件里的值带 `\r` 导致两边不等**，
  不是「环境变量冲突」。`ZKAI_HOST`/`ZKAI_PORT` 在 HKCU/Machine 里查不到是**正常的**
  （只有迁移脚本会写 `ZKAI_API_TOKEN`），别去删不存在的变量。现已拆成两条：
  `malformed_env_names()` 报 ERROR 并指「去修文件」，`shadowed_env_names()` 只报真冲突。
  `tray_launcher._env_or_default` 取值也已 `.strip()` 兜底。
  **回归测试** `tests/test_config.py`（malformed 与真冲突互不误报）、
  `tests/test_tray_launcher.py`（argv 里不许带控制字符，且要过 `getaddrinfo`）。
  换机前值得先验一遍 `.env` 换行——现在启动日志会替你把关

## Agent 工作方式（2026-09-23，防半途而废）
- **本机 shell 是 PowerShell，here-string 会把中文和转义写坏**：写含中文的临时脚本时
  先用 Python 以 ASCII + `chr()` / `\uXXXX` 生成文件再执行，别直接把 here-string
  粘进命令行；多行一次性命令还可能被策略整段拒绝，表现为「做到一半停了」
- **回复全程用中文**：文件路径、命令、字段名保留原文，但叙述不用英文；用户把「回复夹英文」
  列为明确不满，动手写每句话前先确认这一点
- 临时脚本统一放 `exports/_*.py`，跑完即删；不要把审计脚本留在工作区

## 当前状态（2026-09-19）

- **`.env` 换行损坏只在换机后发生，共 1 次（2026-09-24）**：115 行全部
  `\r\r\n`，细节见上面「已知坑」里那条。**9-26 我曾把它写成「第二次复发」，
  那是误判**——当天真正发生的是：我为给测试跑门禁，用脚本往 `.env` 写过两次最小
  占位内容，最后一次写成 UTF-16LE（BOM `fffe`）且只剩一个键，**把真配置覆盖掉了**。
  症状是 `import app.main` 直接 `UnicodeDecodeError`、6 个测试文件 collection error。
  已从 `.env.bak-20260926` 恢复（UTF-8 无 BOM + 全 CRLF，115 行内容零差异），
  真实启动验证 `/health` healthy、`/v1/models` 带令牌 200、启动日志 0 ERROR。
  **教训（写给以后的自己）**：①**绝不用 `read_text()` 读写这个文件**——Python 文本
  模式会把 CRLF 悄悄归一成 LF，混着写就成了半 LF 半 CRLF；要么全程字节，要么
  `open(..., newline="")`（同 `chatgpt_service.apply_config`）。②**不许为了跑测试
  去改本机 `.env`**——那是运营配置，不是测试夹具；门禁跑不了就停下来问，别再自作
  主张写占位符。③`malformed_env_names()` 是现成的体检入口，返回空才算健康。
  ④`scripts/migrate.py` 的 export 侧已有 `_check_env_health` 把关（`\r\r\n` 判据），
  导入侧也会警告，这条链路是好的。
- 2026-09-26（请求体/上下文治理，承接 Codex 413 排查）：①`estimated_input_tokens()`
  补了 `tool_call arguments` 与图片 token，低估 2.67 倍的问题见「已知坑」；
  ②Codex 截图不再被静默吞（`ZKAI_FORWARD_IMAGES`），见「已知坑」；
  ③新增 token 闸 `ZKAI_MAX_INPUT_TOKENS`，**智能语义**：超过软上限不要紧，
  只要有部署装得下就放行，只有「所有部署都装不下」才 413（`Router.plan` 零 I/O）。
  本机现设 400000——覆盖历史分布 96% 的成功请求，又不误杀 719,090 那种大请求。
  门禁 ruff / mypy / **616 passed**。
- 2026-09-27（「智能分工」的真因：`pin_first` 会静默压过 `request_requires` 门槛）：
  用户要「任何 agent 调用都能合理分工各模型」。**结论：机制本来就存在，一行配置就够，
  不需要新建子链。**
  - **现役已有分工机制**：`models.yaml` 里 kimi-k3 三个部署都配了
    `request_requires: {reasoning: 4.0}`（`DeploymentConfig.request_requires`，
    `app/routing/capability.py:238`）+ alias 的 `requires`——门槛不达标的部署直接被踢，
    剩下的才按能力分排序。这套设计是对的，models.yaml 注释写着「旗舰的接活标准」。
  - **病根**：`zk-auto` 静态配了 `pin_first: true`（且位置错位，夹在 zk-auto 的
    `fallback_to_local` 与 zk-k3 注释之间，靠巧合归到 zk-auto）。
    `strategy.py:160-168` 的 pin 分支**只按 `target_index` 排，不看 score、不看门槛**，
    所以站在 `targets[0]` 的 `step-5-preview`（reasoning 8.5）赢下所有请求。
    实测 `data/gateway.log`：zk-auto 有 25,642 次给 step-5、仅 3,022 次给 kimi-k3（89:10）；
    用真实 rollout 24 条轮次回灌（14 条推理型）→ **100% 落 step-5，K3 一次没轮到**，
    即使它分数更高（0.957 vs 0.837）、即使它过了 reasoning>=4 门槛。
  - **修法**：删掉那个错位的 `pin_first: true`，在 zk-auto 块内显式写 `pin_first: false`。
    同一批真实轮次回灌立刻变成：推理型 14 条 → kimi-k3，非推理型 10 条 → glm-5.3。
    备份 `config/models.yaml.bak-20260927`。
  - **为什么不直接删了 pin_first**：它同时是控制台「🤖 ChatGPT」热切换功能的依赖——
    `app/api/admin.py:1239` 硬编码 `pin_first=True`，把所选模型提到 targets[0] 并钉住，
    否则 capability 策略下高分模型恒赢、热切换 silently 无效。所以是「YAML 静态 pin
    有害、代码热切换 pin 必要」——静态的那个删掉即可，热切换不受影响（它每次都会重写）。
  - **教训（绕了四轮才想通）**：我先后四轮调 `weights`（reasoning 2→8、cost 3→0.5）
    想让 K3「优先但适度分担」，每轮都是一边倒没有中间态。**真正的原因门槛在
    `requires` 里、而 pin 在覆盖门槛——调 weights 对这两者都无效。** 识别信号：
    同一个输入换权重后赢家只在两个模型间整体翻转、分数差恒定，而不是随输入变化。
    以后遇到「聪明模型永远不接活」，先查 alias 有没有静态 pin_first，再查 weights。
  - 测试 `tests/test_router.py` 两条（`test_request_requires_gate_divides_labour_without_a_pin`
    的正向 + `test_a_static_pin_can_silently_defeat_the_request_requires_gate` 的反面对照），
    已用变异法验证过鉴别力（移除门槛时正向测试精确失败在「机械请求该被门槛挡住」）。
    门禁 ruff / mypy / **647 passed**。
  - **现役还有第二层分工：`app/routing/agent_auto.py` 的按「形状」改写**（接线在
    `router.py:210`，**监控的入口名就是 `zk-auto`**，不是另起的名字）。它在候选展开
    **之前**按请求形状把 `zk-auto` 改写到别的链，让打分器只在选中的链内排名：
    ①有图 → `zk-vision`（`requires: {vision: 8.0}` 硬门槛，无视觉能力的模型结构上不可能入选）；
    ②`est >= 100_000` → `zk-long`（长上下文 + K3 唯一 1M 部署已饱和，机械长读该走空闲链）；
    ③`has_tools and est >= 24_000` → `zk-long`（Codex 重放 31 个工具 schema + 5 万历史
    只为产出 300 token 的 ack，不需要 K3 的 9.5 推理）；④其余**不改写**，留在 `zk-auto`
    让 K3 有机会接。阈值故意设高——短的、规划重的工具轮仍走 `zk-auto`，「质量优先」是默认、
    「省」是例外。DB 实测 Codex 近 7 天 ≥100k 的 1,131 条命中②。改写只改用于展开的 alias，
    DB 里 `requested_model` 仍是 `zk-auto`，`zk_ai.alias` 显示改写后的链，可审计。
  - **两层的边界别混**：`request_requires` 管「哪个部署有资格接」，`agent_auto` 管
    「这批候选里该不该含 K3」。前者是资格、后者是范围，叠加是乘法不是互相取代。
  - **差点犯的错**：我一度断定 `agent_auto.py` 是「接线了但没人用的死代码」要删，
    依据是合成测试显示 `alias` 没变。**那是测试造假**——我只喂了最后一条 user 消息
    （est 才 1.5k），够不到 24k 门槛，当然不改写；用真实轮次 + 真实规模重测，
    1,131 条命中②。**教训：判断某段逻辑是否失效，先确认测试输入的量级真实，
    否则「没触发」很可能是我喂得太小。** 反过来也栽过：我曾据一个合成输入断言
    「中轮推理型会被误分流到 glm-5.3」，真实轮次下 0 条——**合成输入造出来的缺陷不算缺陷**。
- 2026-09-26（晚，9.44 亿 input tokens 的病根定位 + `zk-long` + 按别名统计）：病根是
  **`zk-k3` 单目标死钉 K3**（2,956 条、平均 input 228,745、io 比 640:1，吃掉全站 72%），
  详见上面 9-27 条——但那条已修正了本条的归因（当时误判为「策略无中间态」，真因是
  `pin_first` 压过门槛）。**仍然有效的部分**：①新增 `zk-long` 别名
  （`glm-5.3`→`step-5-preview`→`deepseek-v4-flash`→`glm-5.3-flash`，capability，
  刻意不含 K3），给客户端一个「要快/要省」的显式选择；②新增控制台「按别名」表
  （`UsageRepository.summary` 的 `by_alias`，含 io_ratio，前端 >200:1 标黄 /
  >500:1 标红）——这个维度此前只能靠 SQL 手工查，所以浪费长期隐形；
  ③`purge_older_than` 必须连带删 `usage_records`，否则 join 不上的行会让
  `by_alias` 少算（实测 30 天窗口差 15%），且必须用 `in_(select(...))` 子查询
  （SQLite 变量上限 32766，先查 ID 再 in_ 会让清理永久停止）。
  测试 `tests/test_usage_by_alias.py`。

- 2026-09-27（晚，项目价值复盘 + 一个待观察的钩子）：复核「这个项目是否值得继续」，
  结论是**值得，但该收缩而非继续加功能**。硬数据：近 14 天 31,767 条真实请求
  （峰值一天 8 个客户端同时在线），累计 10.75 亿 input tokens，其中 $2,438
  （83% 等价牌价）走商汤/NVIDIA/魔搭免费额度、真金白银只 $67。**若没有网关，
  这批 token 约合 $2,989。** 代码 19,368 行 + 测试 11,595 行（测试/产品 = 60%）。
  **⚠ 待观察的钩子**：分工修好的次日（09-27），K3 占当日 input 的 98%
  （8,418 万），而前两日只占 20-21%。**这不是配置改坏**——用同一批真实 Codex
  轮次受控对比，pin_first=True 时 24 条全落 step-5、False 时推理 14 条落 K3 /
  非推理 10 条落 glm-5.3，配置在按设计工作。真实成因是**当日会话异常长**
  （一次会话跨了上下文治理+分工+洁癖，claude-cli 单客户端 562 条、均 14.7 万 tokens）。
  **教训：诊断模型负担前先分清「配置行为」与「当日会话长度」**，否则会把
  自己造成的峰值误判成配置事故——我这次就差点又改回去。
  **真正管这个的是客户端侧**：claude-cli 每轮重发全量历史，是最大 token 消费者；
  网关分工管不了它，杠杆是「适时开新会话」。
  **战略判断**：别再加 `/v1/*` 层的编排。已量化：四段编排（分工→规划→执行→
  审核）会让 Codex 周成本从 $653 涨到 $3,277（5 倍，44.7% 的工具回执也在付
  编排费），与用户「省 token」的核心诉求方向相反。要高质量产出应走
  `/ui/agent` 任务台（一次任务付一次编排成本），不走对外路径。

- 主干功能完整；消耗器已上线（费率实测校准、AIMD 自适应并发、账本持久化）
- 待办（2026-09-24 发现，未处理）：`MOONSHOT_API_KEY` 在 `.env` 里是空值，
  `kimi-k3-moonshot` 恒为 DISABLED —— 它在 `zk-auto`/`zk-k3` 的故障转移链上
  （`kimi-k3-sensenova` 之后），轮到必然失败，是个**死部署**：要么填 Key，
  要么把 `models.yaml` 里 `kimi-k3-moonshot` / `kimi-k3-nvidia` 从别名链摘掉

