## 怎么跑

- 双击 `scripts\start_gateway.cmd`（默认端口 8317，可传参换端口）；手动等价命令：
  `.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8317`
- 依赖用 uv 管理（`uv sync` 自动装 Python 3.12+）；**门禁/测试要
  `uv sync --extra dev`**（pytest/ruff/mypy 是 optional extra）；
  首次先 `python scripts/init_db.py` 建表
- 提交前门禁必须全绿：`ruff check` + `mypy` + `pytest`（README §20）

## 技术栈

FastAPI + httpx + SQLAlchemy 2.x + pydantic-settings；Python ≥ 3.12；uv。

## 目录与约定

- `app/` 交付代码（api/services/routing/providers/credentials/retry/database）；
  `tests/` 全 Mock 离线测试；`scripts/` 运维脚本与启动器
- **密钥红线**：真实 Key 只存 `.env`（已 gitignore）。所有被 git 跟踪的文件
  （含 `.env.example`、README、config/*.example.yaml）绝不出现真实密钥
- `config/*.yaml` 是本地现役配置（已 gitignore），`config/*.example.yaml` 是可提交模板；
  行为变更两边同步
- **`.cmd`/`.bat` 必须纯 ASCII + CRLF**：中文会让 cmd 字节错位误解析（生成乱码文件/跳行）；
  CRLF 由 `.gitattributes` 强制，ASCII 靠自觉；中文文案放对应 Python 脚本里
- 文档入口：`使用手册.md`（面向使用）→ `README.md`（全量参考）
- `docs/` 分两类：**带日期的一次性排查/审查报告**（只作过程留档，结论往上沉淀）
  与**常读的主题文档**（从本文件拆出；规则仍以本文件为准，做对应的事时才翻）：
  - `docs/额度与限流.md`：429 的 rpm/tpm 判据、商汤额度窗口、扫池与整池真空、墙钟与
    AMD 冷 prefill。
  - `docs/部署与运维坑.md`：配置热加载/回写、heredoc、venv、DB 串行化、迁移包纪律。
  - `docs/模型市场与预设.md`：加模型/预设、模型 ID 与「目录 ≠ 能调」、NVIDIA 权限、
    华为云两套东西。**外部 CLI 的模型清单也在这里**——任何清单都不能当权限用，只有
    `probe_models()` 真发请求算数（`max_tokens=1` 会误杀推理模型）。
  - `docs/免费档执行体-20261025.md`：CLI 运行形态、审批档、@ 语义、任务信封、续跑。
  - `docs/事件中台.md`：事件流设计、规则字段、状态机与「会写错的细节」。
  - `docs/排查过程详述.md`：已知坑的逐项排除过程、入站断连定位、mixin 搬运、代码体量。
  - `docs/控制台前端约定.md`、`docs/服务器同步.md`：做对应的事时才翻。
  - `docs/当前状态.md`：**会过时的运营状态**（消耗器停用、额度翻倍、已删供应商）。
  - `docs/消耗器内部机制.md`：消耗器已停用，内部实现不再每次会话都看。
  改完 `config/*.yaml` 跑 `scripts/check_config.py` 做自洽性体检（悬空部署、不可解析
  target、启用了但 0 可用部署、外部改动未 reload）。
- `config/*.bak-*` 是人工备份，每个文件**只留「最早（基线）+ 最新（可回滚点）」**，
  中间态及时删——堆到 18 个时已经分不清哪个能回滚。
  ⚠ 纯 `.bak`（不带日期）是 `config_writer.atomic_write` 的**滚动备份，功能性，别删**。

- **所有面向用户的提示都必须是中文，且有测试守门**（2026-09-30 补全）。
  覆盖：控制台三页（`app/web/*.html`）、API 报错与 toast、argparse 的 `--help` 和报错分支、
  `scripts/` 下的运维脚本输出与 `.cmd` 启动器的失败提示、托盘菜单；
  守卫在 `tests/test_console_zh.py`（前端英文句 + 后端用户可见英文）与
  `tests/test_start_msg.py`（.cmd ASCII 契约 + helper 中文），
  另有一条 `tests/test_burn_budget.py` 的 argparse 中文断言。
  **新增页面/脚本/提示键时，这些守卫按目录或元组自动覆盖，不要手写清单**——
  历史上三次漏网（`report.html` 页面、export/import 两个 `.cmd`、`scripts/` 整目录）
  全是手写清单漏改导致的。
  .cmd 必须保持纯 ASCII（见上），所以它的中文文案统一放 `scripts/start_msg.py`。
  有意保留的英文只有两类：上游报错原文（运营者要拿去比对日志）和
  `mock_upstream.py` 的 mock 响应体（测的就是真实错误形状）——白名单里有，别删。
  还有一类**扫描器结构上查不到**的：前端把后端枚举值翻译成中文时用的映射表
  （`index.html` 的 `VERDICT` / `STATUS_PILL` / `ERR_TYPE_ZH`）。这类是
  「后端新增一个枚举值，前端映射表没跟着加」——文案本身是中文，扫英文句扫不出来，
  而 `_probe_verdict` 加一种结论（2026-09-30 的 `timeout`）就会让运营者看到一个
  裸英文词。所以**新加后端枚举值时要同步前端映射表**，守卫
  `test_marketplace_verdict_labels_cover_the_backend_vocabulary`
  从后端源码推导 `VERDICT` 词表来钉这件事（同套路：`BURNER_LABELS` 加参数自动撞）。

## 已知坑（不看会犯错）

- 网关出站走 **Windows 系统代理**（httpx 默认 trust_env 读注册表代理 127.0.0.1:10808）；
  代理进程没开 → 所有上游 ConnectError。独立脚本调国内 API 应 `trust_env=False` 直连
  （`scripts/burn_sensenova.py` 已这样做），网关不能一刀切关（NVIDIA 需要代理）
- 商汤积分池：flash-lite 扣完专属池会**静默**溢出扣通用池（kimi-k3 的积分），
  API 无任何池信号；消耗器靠按积分记账 + 预算熔断防溢出，勿轻易关预算
- 商汤窗口刷新模型默认按滚动记账；控制台显示的每账号「重置时间」可能是固定锚点
  （判定方法见使用手册），确认后用 `--anchors "1=HH:MM;..."` 切固定窗口爆发模式
- 商汤额度按**账号**算：01+02 同账号，07~09 归属待核对；额度上限见 providers.yaml 注释
- **商汤的 tpm/rpm 桶是按「账号」分的，不是全供应商共享**（2026-09-28 实测推翻
  旧结论）：同一时刻账号 04/05 正以并发 16 稳定烧穿，而 03/06/07/08/09 全部 429。
  所以 `cooldown.throttle_scopes` 的 `quota`/`throughput` 默认是 **account** 不是
  provider。曾按「账号间共享」判成 provider，代价实测很重：网关一次 kimi-k3 撞 429
  （那把 Key 与积分消耗器**共用同一个账号**，见下条）→ 8 把 sensenova 全部停靠 52s，
  其中 6 个账号本来健康 → 请求被迫去打已经超时死的 nvidia → 客户端十几分钟收不到
  任何字节。**既然后来发现是按账号分，就别再改回 provider**；真看到全供应商一起 429
  再改，并且先确认不是同一个账号的多把 Key。
- **网关和积分消耗器共用同一批商汤 Key**：`config/providers.yaml` 的
  `sensenova-02` 用的就是 `SENSENOVA_API_KEY_02`，而消耗器 `config/burner.yaml` 的
  `only:` 列的是 `SENSENOVA_API_KEY_01`~`_09`（2026-10-05 统一改名，原 `_10` 值移到 `_01`）。
  消耗器满速烧时，网关打这些账号的 kimi-k3 必然
  撞 tpm/rpm 429。**诊断网关 429 先看消耗器是否在烧**，这不是网关的错。
  ⚠ **2026-10-27 起消耗器已灰色停用**（`burner.yaml` 的 `disabled: true`），
  所以现在看到 429 就是**纯 tpm 容量问题**，别再往消耗器上想。停用原因见下条。
- **模型 ID 与「目录 ≠ 能调」**：探针只能判「这个 ID 能不能调」，判不了「你要的产品
  是哪个 ID」，查资料先看发布日期。`/v1/models` 是目录，不是账号权限——只有
  verify/probe 真发请求算数。核对官方文档修正过三个 id（`deepseek-v4-flash` /
  `Qwen/Qwen3.8-Flash-Next` / NVIDIA 账号无权限），`deepseek-v4-pro` 2026-10-08
  下线。**NVIDIA 404/挂死是账号权限问题，改配置无用**（需 build.nvidia.com 开
  Public API Endpoints）。健康检查必须真跑一次推理，只查目录会一直假绿。
  细节与实测见 `docs/模型市场与预设.md`「模型 ID、目录与能调用的边界」。
- **部署级自动隔离：连续失败才屏蔽，偶发事件不误伤**（2026-09-29，运营者要求
  「总是检测失败的模型应该自动屏蔽，而不是每次都重新检测，但要智能一些」）：
  旧逻辑只有 `_deployment_cooldowns` 的**单次 30s 冷却**，于是 NVIDIA 的 glm-5.3
  「撞 60s → 冷 30s → 再撞 60s」无限循环。新增 `DeploymentHealth`（跨请求连续计数）+
  `is_quarantine_worthy()` 判别：
  | 计入隔离 | 不计入 |
  |---|---|
  | `timeout`（ReadTimeout/ConnectTimeout） | 429 限流（偶发，属凭据层） |
  | 503 / 504（availability / transient） | 401/403（换把 Key 可能就好） |
  | 404 / `model_not_found`（上游没这个模型） | 400/413/422（调用方的错） |
  **阈值 3 次**（容忍单次抖动）→ 阶梯 `60s → 5m → 30m → 2h → 6h` 封顶；
  **任意一次成功立即清零并解除**（半开恢复，否则修好也进不来）；
  **距上次失败超 1 小时则计数衰减归零**（避免陈旧计数让一次抖动重罚）。
  `quarantined_deployments()` 暴露到 `/health` + 控制台顶部横幅——
  隔离是自动的，**必须让运营者看见谁被屏蔽、为什么、还有多久**，否则渠道悄悄消失
  会被当成路由 bug 去查。回归测试 8 例（含端到端「隔离后不再调用它一次」）。
- **外部改配置会被长命进程静默回滚，热加载有完整性要求**：已修两道防线
  （`config_writer` 陈旧检测闸 + `config_watch` 2s 轮询自动 reload）；改完 YAML
  **自动生效**，坏 YAML 保留旧配置。`reload_config` 用
  `RUNTIME_RELOADABLE_FIELDS` / `STARTUP_ONLY_FIELDS` 两集合分类，**新字段不分类
  就红**，`config.restart_required` 会点名需重启的项。改完配置跑
  `scripts/check_config.py`（悬空部署 / 不可解析 target / 0 可用部署 / 未 reload）。
  过程与守卫见 `docs/部署与运维坑.md`「配置热加载」。
- **别用 shell heredoc 写含转义序列的脚本**：Git-Bash 会改写反斜杠（正则失效、
  YAML 首行坏掉、内容重复追加）。改用 Write / Edit 工具，不经 shell。
- **健康检查必须真跑一次推理，不能只查目录**（2026-09-29 修）：NVIDIA 推理 100%
  不可用却一直报绿灯，累计烧掉 **60.9 小时**纯等待。现在目录通了再对前 N 个模型
  各发一次真实推理，任意成功即健康；`health_probe_models=0` 回旧行为。
- **市场只认实测**：目录 ≠ 权限，`POST /admin/providers/{id}/models/verify` 真发
  请求才作数（一次扫描 9 个部署里 5 个是死的）；NVIDIA 是账号级失效（目录正常、
  推理全废），`glm-5.3*` 不在链里，服务器 `n-k3.1p` 已 `enabled: false`（可回滚）。
  moonshot 已整块删除（无 Key，3,102 次尝试 0 成功）。详见
  `docs/模型市场与预设.md`「/v1/models 是目录不是权限」。
- **接口模型 `front_model`：永远排第一、手动指定、随时可改**（2026-09-28 新增，
  `ModelAliasConfig.front_model`）。**当前值以 `config/models.yaml` 为准**
  （2026-11-14 核对：`zk-auto` 是 `deepseek-flash`；`zk-long` 早已不存在）——
  这个字段会被控制台写回文件，文档里记死一个值必然过时。当初设它是为了避开
  `glm-5.3` 在 NVIDIA 上的 60s 超时。
  三条设计约束：①只提升**有资格**的候选——被 `request_requires` 挡掉的接口模型
  绝不能硬提到第一，那会把能跑的请求变成必败；②是**重排不是过滤**，能力路由的
  结果完整留在后面当备用与故障转移顺序；③拼错时静默回落（配置文件容忍历史遗留），
  但 `POST /admin/aliases` 显式调用会 400——运营者显式换模型不该静默无效。
  **开关**：控制台「模型与别名 → 编辑别名」里有开关+下拉框，关掉 = 不送
  front_model = 越过首位回纯权重排序。语义上「有没有值」就是开关，服务端**不额外
  存布尔位**——两处状态必然不一致（开关关着但字段还有值）。别名表多一列显示
  `📌 模型名` / `权重排序`。⚠ 复现路由行为时注意：
  `ModelAliasConfig.strategy` 默认是 **PRIORITY**（只有 `tests/conftest.py` 的
  `make_alias` 默认 CAPABILITY）；直接构造 ModelAliasConfig 忘写 strategy 会走到
  按 targets 顺序的优先级策略，看起来像「关闭开关没生效」，其实是策略不同。
  与 `pin_first` 的区别：`pin_first` 钉 `targets[0]` 且由控制台热切换托管，
  `front_model` 独立于 targets 顺序，换它不用重排列表。
- **账本/缓存不许覆盖显式配置**（费率优先级「命令行 > burner.yaml > 账本」，
  同类坑在 config 热重载上也犯过）。费率数值以 `config/burner.yaml` 为准，
  细节见 `docs/消耗器内部机制.md` 与 `docs/额度与限流.md`。
- **`ModelAliasConfig.front_model` 以控制台为准，且会写回 `models.yaml`**：
  `POST /admin/aliases` 走 `_write_alias_file()` 落盘，所以「配置文件里是 A、实际
  生效是 B」通常不是 bug，而是有人在控制台改过。排查前先 `grep front_model= data/gateway.log`
  看它实际用的是什么、什么时候变的。
- **控制台部署卡片 `collectDeps` 只收表单里有的字段，其余必须显式回传**（2026-10-23
  修推理等级时踩到）：原来保存会把 YAML 里的 `options` / `supported_params` /
  `request_requires` / `weight` 等**整段抹掉**（部署是替换不是合并）。现在
  `DEP_PRESERVE` 列表把这些连同 `reasoning_efforts`（档位证据）原样带回。
  新增部署字段时**必须同时进这个列表**，否则控制台一保存就丢。
- **推理等级默认值永不覆盖客户端**（2026-10-23）：`DeploymentConfig.reasoning_effort`
  只在 payload 里还没有 `reasoning_effort` 时注入（Codex 的 `reasoning.effort` 走
  extra_params 进来）。优先级是 **客户端 > 部署默认 > 不指定**。别改成
  `payload.update` 强写——会把客户端明确的思考强度盖掉。
- **档位词表要信官方文档，不能只信 `/models` 探测**（2026-10-23 被运营者纠正）：
  商汤 `GET /models` **根本不回** `reasoning_effort_support_list`，只靠探测会让
  `deepseek-flash` 这种明明支持 `none/low/high/max` 的模型永远显示「档位未知」、
  没法配置。已核实词表必须落进 `ModelPreset.reasoning_efforts`（官方文档出处写注释），
  市场合成逻辑是 **探测 > 预设 > 空**；两路都没有才是「不支持调档」。
  deepseek-flash 另有兼容映射 `minimal→low, medium/xhigh→high, ultra→max`
  （`reasoning_effort_aliases`）。档位未知时表单给手填入口，不许是死胡同。
- **`max_total_attempts` 是次数上限，不是时间上限**（`ZKAI_MAX_REQUEST_SECONDS`，
  默认 300s）：墙钟预算只在**两次尝试之间**检查，不打断在飞的流。**单条慢腿能吃光
  整个请求**（实测 AMD 701s 吃光 300s 预算，48 条失败）→
  `attempt_timeout_floor`（20s）+ `margin`（5s）按剩余预算均分读超时，floor 是硬
  下限；它管不了链上最后一条腿。
- **AMD 冷 prefill 静默 50-70s**（2026-10-24 服务器实锤）：未命中缓存时 113k token
  prefill 不吐字节，60s 读超时掐死整轮且流式无法重试。已设 `timeout: 300`、
  `max_retries: 0`。⚠ 粘性会话首轮恰是长 prefill 轮，别把读超时压太紧。
  判定特征、误杀评估与调参见 `docs/额度与限流.md`。
- **429 先分清 rpm 还是 tpm，再决定换 Key 还是换部署**：现役
  `throttle_scopes` 是 `quota=account`、`throughput=credential`，判据只看
  `CredentialPool.effective_throttle_scope()` 一个方法（池子与调度器共用）。
  商汤 rpm 桶极小（实测中位 2、p99 8），已加主动闸 `max_requests: 3`；
  **小 prompt 是 rpm 惩罚，长上下文（18.7 万 token）是 tpm**，同一闸对后者无效。
  实测数字与回归见 `docs/额度与限流.md`「rpm 桶与主动闸」。
- **单请求不许扫满整池**：`max_credentials_per_deployment=3`（曾 9，等于池大小 →
  一个请求扫完 9 把、每把 429 冷却 60s → 整池瞬间真空；实测 154 条失败里 55 条
  （35.7%）是 0.01s `no_available_credential`）。`only_leg` 让链上最后一条可用腿
  扫完整池（让渡的前提是还有别的腿可退）。回归 `tests/test_limits.py` 钉值 = 3。
- **整池冷却时排队，而不是瞬时 503**：`_wait_for_credential` 先等（上限 10s、不越过
  墙钟）再重取。⚠ **实测救 0 条**（之后下一个商汤成功的中位等待 333 秒）——整池是真
  真空，它只是兜底，**不是解药**；解药是容量（充值/加账号/分流）。
  细节见 `docs/额度与限流.md`。
- **K3 的 `request_requires: {reasoning: 4.0}` 已删除**（2026-09-28）：它拿
  `capability.py` 的**关键词命中**当硬门槛，而 zk-auto 别名地板把 reasoning 权重
  钉在 2.0，于是非推理关键词请求永远 `gated_out`——「你好」、短代码、工具回执
  全落到同一个模型，显式调 `kimi-k3` 还会触发「全部门槛失败、回退无门槛排序」。
  机械流量改由**形状**接管：`agent_auto.rewrite_model` 见「历史里已有工具结果」
  就改写成 `zk-long`（tool 轮 84% 是 ack/diff/status），另有超长 / 批量工具 /
  有图三条。关键词是软信号，只能加权，不能当闸。
- **`zk-long`/`zk-vision` 不存在时改写要降级**，不能让整个 agent 工作负载 404：
  改写目标从罕见边角（长上下文）变成常规路径（每个工具轮）之后，
  `models.yaml` 里没定义 `zk-long` 就是全量故障。`Router.plan` 传
  `known_aliases` 给 `rewrite_model`，缺目标就回落 `zk-auto` 并打 WARNING。
  回归测试 `tests/test_agent_auto.py` 的 missing-* 三例（`tests/test_agent.py`
  的 16 个失败就是这么发现的）。
- **历史裁剪默认关闭**（`ZKAI_TRIM_HISTORY_TOKENS`，0=关）：删上下文可能改变
  答案，是产品决策不是默认值。开启后 `app/services/history.py` 只删旧轮次，
  system 前言 / 最近若干轮 / 未闭合 tool_call 配对一律保留。
  **后缀不许以 `tool` 开头**（其 assistant call 已被丢，上游直接 400）；
  `assistant` 开头是合法的——占位符本身是 `user`，由它来合法开场，
  这一点以前搞错，白丢了 40% 预算。
- **`ProviderConfig.max_retries` 曾经是死配置**（2026-09-28 修）：字段存在、
  providers.yaml 给 NVIDIA 设 0 还写了理由，但适配器建 `httpx.AsyncClient` 时
  根本没读它。现已接 `AsyncHTTPTransport(retries=...)`（**连接级**重试，
  不是读重试——挂起的请求仍旧只能靠上层冷却+换渠道）。注意 `trust_env=True`
  必须显式写，替换默认 transport 正是丢掉 Windows 系统代理的方式。
- **消耗器已停用**（2026-10-27，见「当前状态」）。它内部的 AIMD 饥饿救济、
  限流停靠、费率校准优先级等实现细节已拆到 `docs/消耗器内部机制.md`——
  要重启它或排查积分账本时才翻。**一条老坑仍然有效**：满速烧 flash-lite 时
  同账号 K3 报 `inference exceeds tpm/rpm limit`，所以即便恢复也要错开使用。
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
- **导入迁移包前必须先停网关**（2026-09-21 为这条流过血）：换机包覆盖 `data/zkai.db`
  时，若同目录还留着上一代的 `-wal`/`-shm`，下次开库会把别人的 WAL 帧回放到新库上 →
  `database disk image is malformed`，且**不是警告是真损坏**（本机 `requests`/
  `usage_records` 就是这么废的；SQLite 的 `-wal`/`-shm` 不属于迁移包内容）。一键换机
  Skill 引擎的 `import` 会先备份 side-car 再删除；但最稳的做法是先从托盘退出网关再导入。
  验证损坏库要用 sqlite3 backup API 取一致快照再 `PRAGMA integrity_check`——直接 `cp`
  正在写的库必然报假阳性

以下几条的**逐项排除过程**已拆到 `docs/排查过程详述.md`（结论仍以本文件为准）：

- **`reload_config()` 曾经漏更新冷却策略，`cooldown:` 段改了不生效**
- **`estimated_input_tokens()` 曾低估 2.67 倍，`context_fits` 因此空转**
- **413 有两道闸，别混为一谈**
- **Codex 截图的两种丢法，与 2026-09-25 的修复**
- **`uv trampoline failed to spawn Python child process` = uv 托管的解释器被删了**
- **`~/.codex/config.toml` 归 ChatGPT app 所有，只能外科手术式 patch**
- **`.env` 的值尾部混进 `\r` → 网关 bind 失败，报错却写成 `getaddrinfo failed`**

## 外部 CLI 执行体：免费档（2026-10-25）

已接 5 个 `delegate_cli` 执行体：`gemini` / `codebuddy` / `qoder`（免费档）、
`codex` / `claude`（付费档，走网关额度或 CC Switch）。本地配置在
`config/cli_tools.yaml`（gitignore），模板同步在 `*example.yaml`。
实测报告：`docs/免费档执行体-20261025.md`。

**规则速记**（具体形态、报错原文与实测数字见 `docs/免费档执行体-20261025.md`）：

- **Windows 上 npm CLI 不能直接 exec**：走 `app/services/agent/cli/launcher.py` 解
  shim 成 `node + 入口脚本`，**不要退回 `shell=True`**（argv 直传是安全前提）。
- **`proc.returncode` 要 `await proc.wait()` 之后才可信**，否则成功的委派会被判失败。
- **审批档映射**：`plan` 在各 CLI 里对应不同档（Gemini 的 plan 不产出正文、Qoder 没有
  plan 档映射 `dont_ask`）；CodeBuddy/Qoder 的 CLI 登录态与桌面端不共享。
- **`@` = 执行体直跑，不是聊天**：可多点名串行（`POST /zkadmin/agent/delegations`，
  最多 10 段）；每段独立选模型/思考/上下文；改 Python 要重启网关，改 yaml 不用。
- **费用以 CLI 自报的 `total_cost_usd` 为准**，"在 --help 列表里"不等于免费。

### 2026-11-13 委派层对标 Claude Code / Codex 后的九条（都是实测）

规则速览（每条都有变异验证过的回归；完整细节见 `docs/免费档执行体-20261025.md`）：
①**审批卡展示真正要执行的命令**，预览与执行共用 `delegate.escalated()`；模型发起的
`delegate_cli` 一律不提权（`bypass` 只是任务空间内不弹卡，不是关外部沙箱）。②任务
信封交代「在哪 / 交什么 / 能不能问」。③段间交接只注入上一段结论（截 2000 字）。
④截断一律保尾，失败详情 stderr 在前 + 退出码。⑤超时/取消用 `TIMEOUT`/`CANCELLED`，
不归 FAILED。⑥`idle_timeout` 逐执行体配置（默认 0；只有 codearts 配 180）。⑦claude
走 `--output-format json`，`session_ref` 落 `handoff_session`。⑧预算闸
`daily_calls`/`daily_cost_usd` 事前拦，成本只累加自报的行。⑨免打扰按「工具:角色」记；
三条派单路径都列 `outputs/`。



## 事件中台（2026-11-02，自动化调度）

给已有 Agent 执行层装「点火器」：**让任务起点从人身上移开**。完整设计见
`docs/事件中台.md`，这里只留会踩的规则。

- 三层各管各的，别混：
  **事件规则只决定 `to: 角色`**（`config/events.yaml`）→
  **角色决定 CLI/模型/成本档**（`config/cli_tools.yaml`）→
  **模型决定供应商/Key/失败转移**（`config/models.yaml` + 路由层）。
  想换模型改第二层一行，别去动事件规则。
- **`enabled: true` ≠ 装了**（2026-11-02 实测）：`cli_tools.yaml` 里 `claude` 标着启用，
  但本机根本没有 `claude` 命令 → 注册表把 `cli-claude` 角色整个忽略，
  规则派过去就是每单必失败。所以事件规则默认写 `cli-skill`（codex，自带 skills）。
  **改规则里的 `to` 之前，先看 `agent_worker.py` 启动日志打印的「可用角色」那一行。**
- **去重键必须是语义键**（source+type+object+指纹），不是消息 id：
  来源侧网络抖一下会重发、同事补一句话又是一条。按消息 id 去重等于没去重。
- **未命中规则的事件也落库**（`state=dropped`）。不落库的话
  「为什么这条没触发」就是一句无法回答的话——而这是运营者第一个会问的问题。
- **worker 只能主动拉，不能被动推**：本机在 NAT 后、中台在公网服务器上，
  长轮询（`POST /zkadmin/events/claim`）是唯一方向。锁屏不休眠就够，
  休眠会让任务积压但不会丢。
- **`Database.session()` 是进程级串行锁**：长轮询**不能**持锁 sleep，
  必须是「短事务取一次 → 放锁 → sleep 再取」。
- **结论里的决策标记是契约**：worker 侧指令要求最后一行输出
  `[[NEED_DECISION]] yes|no`，中台据此决定要不要弹卡片。
  靠自然语言猜「这句话算不算要人拍板」是不可靠的，而这个判断决定半夜是否吵醒你。
- 审批推送走**飞书群自定义机器人 webhook**（`${FEISHU_WEBHOOK}` 在 `.env`），
  卡片按钮只跳控制台、不带回调 —— 自建应用 + 卡片回调是第二阶段的事。
- 入口：控制台侧栏「事件流」（内置视图，独立路由 `/ui/events` 仍可用）、`scripts/event_watch.py`（采信号）、
  `scripts/agent_worker.py`（接单）。`ZKAI_EVENTS_ENABLED=false` 整体下线。

### 复查与借鉴补的硬规矩（2026-11-03 / 11-04，实测）

- **审批是闭环**：`decide()` 派生 `<父类型>.decided` 重走规则；新流水线 = 补一条
  `types: [xxx.decided]` 规则，不改代码。人工拍板也算一跳、不清零。
- **`.env` 不会自动进进程环境**：脚本必须先 `load_app_config()` 再取
  `ZKAI_ADMIN_TOKEN`，否则 401 静默拒（`/health` 不需要令牌，极易误判）。
- **`parallel` 是假并行**：worker 单循环串行，要真并行就多开进程（claim 原子安全）。
- **事件规则必须机器校验**：`check_config.py` 查 skill / `to` / `cwd` / 事件类型
  有没有产出方，改完必跑。
- **`complete()` 幂等**；回执标记只有一处定义 `RECEIPT_MARKER`；`hops` 只从
  `parent_id` 推导，去重键必须带 `parent_id|hops`；**DateTime 列必须存 UTC**。
- **`budget_cny` 仍是死字段**（成本恒 0），先接成本再谈熔断。
  细节与 autoloop 的 `askFirst` 教训见 `docs/事件中台.md`「会写错的细节」。

## 手机版控制台改造

（已拆到 `docs/控制台前端约定.md`，做这类事时才翻。）

## Agent 工作方式（2026-09-23，防半途而废）
- **本机 shell 是 PowerShell，here-string 会把中文和转义写坏**：写含中文的临时脚本时
  先用 Python 以 ASCII + `chr()` / `\uXXXX` 生成文件再执行，别直接把 here-string
  粘进命令行；多行一次性命令还可能被策略整段拒绝，表现为「做到一半停了」
- **回复全程用中文**：文件路径、命令、字段名保留原文，但叙述不用英文；用户把「回复夹英文」
  列为明确不满，动手写每句话前先确认这一点
- 临时脚本统一放 `exports/_*.py`，跑完即删；不要把审计脚本留在工作区
- **并发测试夹具要加锁**（2026-11-04）：`ask_batch` 的 `stub_post` 是多线程共写的 list，
  裸 `append` 撞出顺序错——单跑必绿、全量偶红，最能骗人的那种。已加 `threading.Lock`。

## 入站断连：「总是在中途断开」的四个根因（2026-11-13 实测定位，全部已修）

29 万行日志 + DB 交叉核对：**不是路由层的问题**（`zk-auto` 6749 次请求只失败 1 次）。
四个根因与修法：①`/v1` 流式没有空闲心跳 → 按 `request.stream_idle_timeout`（默认
120s）**一半**发 SSE 注释行 `: ping`，有数据流动时不发；②`max_credentials_per_deployment=3`
对单腿模型致命（让渡的前提是还有别的腿可退）→ 链上最后一条可用腿可扫完整池
（`_is_only_fallible_leg`）；③429 不带 `Retry-After` → 用我们自己算的冷却时长兜底；
④`responses` 路径没有断连检测 → 补 watcher（与 chat/messages 同一套）。
**定位过程、实测数字与两个实现坑（在飞 `anext` 跨心跳保持同一 task；发完一格
必须把 event 置空）详见 `docs/排查过程详述.md`**——动流式循环前先翻。

**三条流式路径现在结构一致**，改一条时另外两条要同步——不一致本身就是 bug 源。
`keepalive_interval()` 与 `SSE_PING` 定义在 `app/api/chat.py`，另外两处 import 复用。

**2026-11-19 审查补的两条不变量**（都有回归测试）：①客户端 `max_tokens` 截断到
部署 `max_output_tokens` 的保护在基类 `_clamped_max_tokens`，四个适配器
（OpenAI 兼容/Anthropic/Gemini/Ollama）共用——11-18 只修了 OpenAI 路径，
其余三家同款 400 漏洞仍在；②SSE 注释行 UA 白名单 = 内置名单（含 OpenAI SDK
新旧两种 UA 写法）+ `request.sse_comment_user_agents` 配置追加（可热更），
硬编码名单必然随客户端生态漂移。

### 还没做、别再拿它当「已修」

- `latency_ms` 在流式路径记的是 `meta.latency_ms`（首块延迟），不是整条流的墙钟，
  `client_disconnected` 显示 `latency_ms=0.0` 就是这个原因。改它会影响既有统计口径，
  属新工作、不是 bug。
- 上游 429 正文里的「多少秒后再来」没有解析（只认 `Retry-After` 头）。加「从自由文本
  猜秒数」有误判风险，**刻意不做**。

### 2026-11-23 / 11-26：harness 与 undici 的心跳缺口（已修，真机验证过）

三个并列缺口：①UA 名单写错（`ds-harness` vs 线上 `deepseek-harness`，618 条零 ping）；
②预取窗口零字节（`stream_prefetch_timeout=10s`，超时**不取消**在飞的 `anext`）；
③窗口外 `ZKAIError` 被 Starlette 吞掉 → 三条路径都转成流内 error（responses 走
`assembler.failed()` + `SSE_DONE`，失败分支**绝不发** `response.completed`）。
2026-11-26：undici 补进白名单（55 条 499 全部是它、59.7~59.9s），`cancelled` 改记
`client_disconnected`；**名单必须靠线上 UA 原文验证**。完整证据与回归见
`docs/harness中断根因-20261123.md`；多供应商池见
`docs/多供应商池与入站中断设计-20261126.md` v2。

## 华为云两套东西：码道 CLI（可用）vs MaaS provider（已删）

码道 CLI 走 **AK/SK**（`CODEARTS_CLI_AK/SK`，现役 `cli-codearts`，实测可用）；
MaaS provider 走 `HW_API_KEY_01`（OpenAI 兼容），推理 **403**，2026-11-14 已从服务器
删除（欠费）。**别把两者混成一个**。AK/SK 只在 `load_app_config()` 之后进
`os.environ`，独立脚本先加载配置再取凭据。删 HW 时 `ds` 别名自动收敛成
`ds4.1m → deepseek-flash → ds4.1`。细节见 `docs/模型市场与预设.md`。

## 代码体量与开发流程（2026-11-14）

- `agent/service.py` 已拆成 6 文件（门面 + 四个 mixin + common）：**加方法按职责进
  对应 mixin，别堆回门面**；mixin 用 `if TYPE_CHECKING:` 写类级注解给 mypy 看。
- **burner 配置键单一来源 `config/burner_keys.json`**，加新键只改 JSON
  （`test_burner_key_single_source.py` 三向比对）。
- **迭代期用 `scripts/quick_test.py` 挑测试，提交前必须跑全量**；横切文件与未登记
  路径强制回落全量。它不是门禁替代品。细节见 `docs/排查过程详述.md`。
- `AGENTS.md` 65,536 字节预算由 `test_doc_scale.py` 守着（余量 < 2KB 就红）；
  撞墙时「结论上提、过程下沉」，不删规则。
- 消耗器代码保留不删（停用时零运行开销，随时可能恢复）。

## 流式耗时与委派续跑（2026-11-13）

- `requests.stream_duration_ms`（整条流墙钟）≠ `latency_ms`（首块延迟，别改语义）；
  `client_disconnected` 的 `latency_ms=0.0` 是「没拿到首块」。
- 委派续跑：`session_ref` 落 `state.json`，**同任务空间 + 同执行体**才复用；目前只有
  claude 实测可 `--resume`，其余旗标未实测默认 `[]`——**别凭文档瞎拼**。详见
  `docs/免费档执行体-20261025.md`「委派续跑」。
- **轮换上限 3 vs 9 是两地有意不同**（本地让渡、服务器账号多），`check_config` 只报
  可疑关系，不评判取值。

## 当前状态

运营状态与会过时的事实见 `docs/当前状态.md`（消耗器停用、额度翻倍三处同步、
Flash-Lite 404、`.env` 与迁移包纪律、11-26 多供应商池进展）。

- **战略：别再往 `/v1/*` 层加编排**（四段编排让 Codex 周成本 ×5）；要高质量产出走
  `/ui/agent` 任务台，真正管 token 的是客户端侧（适时开新会话）。
- **`.env` 是运营配置不是测试夹具**：绝不用 `read_text()` 读写它（CRLF 会被悄悄
  归一），要么全程字节，要么 `open(..., newline="")`。门禁跑不了就停下来问，
  别为了跑测试改它。`malformed_env_names()` 返回空才算健康。

