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
- `docs/` 放**一次性的排查/审查报告**（带日期的 `*.md`），不是常读文档——
  结论该沉淀的往上写进本文件或 `使用手册.md`，报告本身只作过程留档。
  另有三份**从本文件拆出的流程文档**（规则文件超 65,536 字节预算）：
  `docs/服务器同步.md`、`docs/部署与运维坑.md`、`docs/控制台前端约定.md`——
  只做对应的事时才翻，规则以本文件为准。
  第四份 `docs/消耗器内部机制.md`：消耗器已停用（见「当前状态」），内部实现细节不再需要每次会话都看。
  `docs/模型市场与预设.md`：加模型/动预设表时翻。**外部 CLI 执行体的模型清单也在
  那份文档里**——重点不是「哪个源对」，而是**任何清单都不能当权限用**：
  `codebuddy --help` 26 个名字 17 个不存在；桌面缓存 65 个 id 只有 35 个是对话模型；
  wb-proxy `/v1/models` 14 个有 8 个调不通。现在只有 `probe_models()` 真发请求算数
  （坑：`max_tokens=1` 会误杀所有推理模型，实测 16/39）。
  另：改完 `config/*.yaml` 跑 `scripts/check_config.py` 做自洽性体检
  （悬空部署、不可解析 target、启用了但 0 可用部署、外部改动未 reload）。
  第五份 `docs/排查过程详述.md`（2026-11-13 拆出）：「已知坑」里七条最长的
  **逐项排除过程**（.env 的 `\r\r\n`、reload 漏冷却策略、413 双闸、Codex 截图丢失、
  `~/.codex/config.toml` 外科 patch、token 低估 2.67 倍、uv trampoline）。
  AGENTS.md 里保留结论与索引，**排查同类问题时才翻这份**。
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
- **探针能判「这个 ID 能不能调」，判不了「你要的产品是哪个 ID」**（2026-09-29
  被用户当面纠正）：`deepseek-v4.1-flash` 403 之后，我据一篇 **09-04 的二手文章**
  断言「正确 id 是 deepseek-v4-flash」。用户拿出 **09-22 的官方公告**：
  「DeepSeek V4.1 Flash 正式上线，**Model ID：deepseek-flash**」——我是错的，
  而且 `deepseek-flash` 就在我拉到的 `/v1/models` 列表**第一行**，我看漏了。
  **两条教训**：①查资料先看**发布日期**，二手转述会被官方公告推翻；
  ②`/v1/models` 里同名家族的多个条目（deepseek-flash / -v4-flash / -v4-pro /
  -v4.1-flash）必须逐个实测，不能只看一个就下结论。
  **真正的缺口是产品名↔ID 的映射**：预设表原来只有 `deepseek-v4-flash`，
  没有 `deepseek-flash`，于是市场把它显示成裸 ID，没人知道它就是 V4.1 Flash。
  已补 `deepseek-flash → DeepSeek V4.1 Flash`、`qwen3.8-flash-next`、
  `deepseek-v4-pro`（标注 2026-10-08 下线）三条预设，并加回归测试。
  另：市场**探测后自动验证**（目录 ≤20 个时），不再等运营者手动点——
  死 ID 和可用 ID 长得一样，必须替他把结论摆出来。
- **`deepseek-v4-pro` 于 2026-10-08 下线**：过渡期内平台自动把它重定向到
  `deepseek-flash`。现役配置未用它。
- **核对官方文档后修正的三个 model id**（2026-09-29，先实测再查官方，两者缺一不可）：
  | 配置里的 | 官方/上游正确的 | 症状 |
  |---|---|---|
  | `deepseek-v4.1-flash` | **`deepseek-v4-flash`** | 403 套餐无权限 |
  | `qwen38-flash-next` | **`Qwen/Qwen3.8-Flash-Next`**（缺 `Qwen/` 前缀） | 400 Invalid model id |
  | `z-ai/glm-5.3` | 名字对，但**账号没权限**（见下条） | 挂死 / 404 |
  商汤公测**可调用列表**（官方文档）：`deepseek-v4-pro`、`deepseek-v4-flash`、
  `glm-5.2`、`kimi-k3`、`sensenova-6.8-flash-lite`、`sensenova-u1-fast`、
  `sensenova-u1.5-lite` —— **没有** `deepseek-v4.1-flash`，但目录里列了它。
  顺带证实积分规则当时是「5h 6 万 / 周 60 万」——**2026-10-27 已翻倍为 12 万 / 120 万**。
  已把 `glm-5.2`（实测 200）加进链，补 NVIDIA 失效后的 GLM 档位。
- **NVIDIA 404「Function Not Found」是账号权限问题，不是模型问题**（2026-09-29
  查官方 + 论坛确认）：官方 developer.nvidia.com/nim 明确写「**Developer Program
  会员**才能用 NVIDIA-hosted NIM API」；论坛同名帖
  「Request to enable Public API Endpoints ... 404 Function Not Found」讲的是
  组织需要开通 Public API Endpoints。实测佐证：`/v1/models` 正常（鉴权 OK），
  但**全部 81 个模型**（不只是 glm-5.3）要么 404 要么挂死 → 账号级失效。
  **这修不了配置，得去 build.nvidia.com 查账号/组织权限。**
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
- **健康检查必须真跑一次推理，不能只查目录**（2026-09-29 修）：`health_check` 原来只调
  `list_models()`（即 `GET /v1/models`），只证明「鉴权通过、目录可达」。NVIDIA 推理
  **100% 不可用**（glm-5.3 挂死、kimi-k3 报 `Function not found`、换 5 个家族全废），
  健康检查却**一直报绿灯** → 它留在所有故障转移链上，累计烧掉 **60.9 小时**纯等待。
  **和「模型市场只看目录」是同一个错**：把「目录里有」当成了「能调用」。同一会话里
  修了市场那处却漏了健康检查这处。现在目录通了再对前 N 个模型各发一次
  `max_tokens=1` 真实推理，**任意一个成功即健康**（目录可能混着无权限条目，
  只探一个会误报），全失败则如实报红。`health_probe_models=0` 回到旧行为。
- **NVIDIA 挂死/404 不是网关配置问题**（2026-09-29 逐项排除，见
  `docs/NVIDIA排查-20260929.md`）：适配器 payload 与裸请求逐字一致、官方文档
  原始示例同样超时（49.7s）、两个 Key 完全相同地失败且报**同一个 function id**、
  有无代理都超时、`/v1/models` 正常而推理全废 → **账号/组织的 NVCF function
  未部署**。论坛同名帖「Request to enable **Public API Endpoints** ... 404 Function
  Not Found」；官方页写明需 **Developer Program 会员**。改配置解决不了，
  要去 build.nvidia.com 查权限。
- **外部改配置文件曾会被长命进程静默回滚**（2026-09-29 实测，同类 bug 本会话第 2 次）：
  历史上网关不监听配置文件，内存里是启动时的值；控制台表单**用内存值渲染**，
  保存时把旧值写回文件。实测：08:20 在文件里禁用 nvidia，09:15 运营者在控制台
  打开 nvidia 页签并保存 → `enabled` 被改回 true（日志：
  `provider nvidia upserted via console` + `config file updated`）。
  **同日同型**：消耗器把 `burn_state.json` 的费率 761/2292 写回 830/2500。
  共同点：长命进程把内存状态写回文件，外部编辑在它看来不存在。
  已修（两道防线）：
  ①`config_writer` 陈旧检测闸（`ConfigStaleError`）——`load_app_config` 记 mtime，
    写前比对，文件更新过就**拒绝写入**并返回 **409 + 「先 reload」指引**；
    写后刷新记录，自己写的不会被自己判陈旧。`stale_config_files()` 供体检/健康检查用。
  ②`app/core/config_watch.py`（`ConfigWatcher`）——lifespan 启动后每 2s stat 轮询
    `AppConfig.source_files`，签名（mtime_ns, size）稳定 settle 窗口后自动 reload。
    改完 YAML **自动生效**，不再需要手动 reload 或重启。坏 YAML 保留旧配置 + 记
    `last_error`，改好后自动接上。`ZKAI_WATCH_CONFIG` / `ZKAI_WATCH_CONFIG_INTERVAL` 可控。
    `/health` 暴露 `config_watch`，控制台状态行显示「配置自动生效（已自动 reload N 次）」。
    纯标准库 stat 轮询（不引 watchdog/inotify），监听名单 = loader 真正读过的文件（不是 glob）。
- **`config.yaml` 的 `deploy:` 段不参与热加载**（2026-10-26 实测）：`reload_config`
  只重建 router / 别名 / 凭据 / 冷却策略，`load_app_config(self.settings)` 返回的
  新 Settings **没有写回 container**，所以改 `deploy.public_base_url` / `exposure`
  仍要重启网关——热加载日志会显示成功，`/health` 却还是旧值。启动期设置
  （host/port/log/db 一类）同理，别把「已自动 reload N 次」当成所有段都生效。
- **规则写进文档 ≠ 会执行；要工具化**（2026-09-29 自我反省）：我上一轮在本文档亲手写下
  「改完 YAML 必须跑 `load_app_config()` 验证」，下一轮自己没执行——禁用 nvidia 后
  只 `grep` 看到注释在就宣布完成，而实际值仍是 `enabled: true`。
  已建 `scripts/check_config.py`：改完配置跑一次，查悬空部署、不可解析 target、
  **模型 enabled 但 0 可用部署**、别名单供应商、外部改动未 reload。
  它本可以第一时间抓住我误删 `deepseek-v4.1-flash` 模型块那次事故。
- **别用 shell heredoc 写含转义序列的脚本**（本会话栽了四次）：Git-Bash 会改写
  heredoc 里的反斜杠——反斜杠-d 变斜杠-d（Python 正则静默失效，害我得出过错误结论）、
  反斜杠-n 变斜杠-n（YAML 首行坏掉）、内容被重复追加（测试文件三次重复定义 F811）。
  **第四次就发生在这条注释本身**：我用 heredoc 写这段文字时，`\n` 被替换成了真实换行，
  于是文档里出现了一个断成两行的反引号。**改用 Write / Edit 工具，不经 shell。**
- **供应商的 `/v1/models` 是「目录」不是「你账号能调的」**（2026-09-29 实测，
  补上缺失的验证环节）：市场原来只看目录，于是死模型被加进来，直到第一次真请求
  才暴露。三个真实反例，三个都通过了目录检查：
  | 供应商 | 模型 | 目录里有 | 实际调用 |
  |---|---|---|---|
  | 商汤 | `deepseek-v4.1-flash` | ✅ | 403 `model is not available in the current token plan` |
  | NVIDIA | `z-ai/glm-5.3` | ✅ | **挂死**（121s 无响应，不是慢） |
  | NVIDIA | `moonshotai/kimi-k3` | ✅ | 404 `Function id ... Not Found` |
  | 魔搭 | `qwen38-flash-next` | ✅ | 400 `Invalid model id` |
  新增 `POST /admin/providers/{id}/models/verify`：对每个模型发一次
  200 token 的真实请求（`_VERIFY_MAX_TOKENS`；2026-11-11 从 `max_tokens=1`
  提上来——推理模型把 token 全花在思考上，1 token 会把它们全部误杀，
  与执行体探测 `_PROBE_MAX_TOKENS` 同一个坑），短超时（默认 20s，
  「挂死」本身就是结论），
  返回 `ok / hang / not_found / no_entitlement / rate_limited / error`。
  控制台「🧺 模型市场 → 🔬 验证可调用」按结论标注并禁用「＋ 添加」。
  **一次扫描查出 9 个部署里 5 个是死的。**
- **NVIDIA 实测 0/3 全死，但运营者决定保持启用**（2026-09-29）：推理全废
  （glm-5.3 挂死 121s、kimi-k3 报 `Function not found`、换 5 个家族全废），
  而 `/v1/models` 正常——**不是网关配置问题**（见上面「NVIDIA 挂死/404」那条的
  逐项排除）。历史代价：超时 3,313 次 × 均 66s = **60.9 小时纯等待**。
  `providers.yaml` 的 `enabled` 保持 `true`（运营者要求「不要着急限制，先排查」），
  但要恢复真正可用得去 build.nvidia.com 开账号/组织的 Public API Endpoints 权限。
  **现在靠自动隔离兜底**（连续 3 次失败即屏蔽，见「部署级自动隔离」那条），
  所以它不会再像以前那样每个请求白等 60 秒。`glm-5.3`/`glm-5.3-flash` 当前
  **不在任何别名链里**，要用就点名模型名。
- **moonshot 已整块删除**（无 Key，3,102 次尝试 0 成功，纯 `no_available_credential`
  噪音）。要恢复需同时加回 providers.yaml 的供应商与 models.yaml 的部署。
- **接口模型 `front_model`：永远排第一、手动指定、随时可改**（2026-09-28 新增，
  `ModelAliasConfig.front_model`）：`zk-auto`/`zk-long` 已设 `step-5-preview`，
  因为 `glm-5.3` 在 NVIDIA 上持续 60s 超时、打头等于每个长上下文请求先赔 180 秒。
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
- **费率优先级「命令行 > burner.yaml > 账本」的来龙去脉见
  `docs/消耗器内部机制.md`**。规则本体仍有效且通用：**账本/缓存不许覆盖显式配置**
  ——记忆盖掉明确意图 = bug（同一类坑在 config 热重载上也犯过）。
  当前费率 `rate_in: 761` / `rate_out: 2292`。
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
- **`max_total_attempts` 是次数上限，不是时间上限**（2026-09-28 补
  `ZKAI_MAX_REQUEST_SECONDS`，默认 300s）：20 次尝试 × 60s（nvidia read timeout）
  = 最坏 20 分钟。实测一次请求磨了十几分钟、客户端一个字节都没收到——每次尝试单独看
  都没超时，所以次数闸认为还有预算。墙钟预算只在**两次尝试之间**检查，不会打断已在
  飞的流（那会截断客户端还能读的响应）。默认 300s = 现役最大上游超时（商汤 300s），
  一次成功的慢生成不会被误杀。
- **AMD 的 DeepSeek 流式：冷 prefill 静默 50-70s，60s 读超时掐死整轮**（2026-10-24
  服务器实锤，ds harness 反复报「流式读取中途超时」的根因）：
  `developer.amd.com.cn` 的 DeepSeek-V4.1-Flash 在**未命中缓存**时，113k token 的
  prefill 全程不吐一个字节、实测 50-70 秒；`timeout: 60` 的读超时把这类轮次掐死在
  中途——流式一旦开头发送就**无法重试**，客户端整轮报废。判定特征：中途超时的尝试
  总时长 ≈ X + 60s（X 是卡死前已流式的时间；实测 60.3/78.4/82.5/122.7/151.2s，
  全部落差恰为 60）。对照：命中缓存的轮次 prefill 秒过（cached=100% 的行总时长
  45-69s 全是生成时间）。已修（服务器+本机 providers.yaml）：`timeout: 60→300`
  （对齐商汤现役最大值）、`max_retries: 2→0`（NVIDIA 同款教训：挂起交给上层
  冷却+换渠道）。⚠ 粘性会话的**首轮（缓存冷启动）恰恰就是长 prefill 轮**——
  读超时太紧等于专挑粘性的第一枪打，修缓存粘性必须同步检查上游超时预算。
- **429 不能再默认「换一把兄弟 Key」**（2026-09-28 修）：分类器按正文分出
  `ThrottleKind`（`quota` 额度用尽 / `throughput` tpm / `frequency` rpm·rps /
  `unknown`），`cooldown.throttle_scopes` 决定冷却覆盖到哪：
  `credential` 只冷却这把（rpm 类，NVIDIA 按 Key 计 40rpm 就属这类）、
  `account` 同 `account-*` 标签一起冷、`provider` 整个供应商一起冷并**直接故障转移**。
  现役 `quota` 是 `account`、`throughput` 是 `credential`（2026-10-08 改：
  9 把 Key = 9 个独立账号，一把 429 只冷这把，让其余 8 把继续被试）。
  判「换 Key 还是换部署」只看 `CredentialPool.effective_throttle_scope()` 一个方法，
  池子与调度器共用，否则两边判断会漂移。
- **商汤公测账号的 rpm 桶极小，且越线会转入长冷却**（2026-10-13 实测，
  服务器 DB + 本机 DB 双口径）：服务器近 3 小时每把 Key 每分钟被打 3~6 次，
  **429 率 80%、04:56 起连续 3 小时无一把 Key 恢复**；本机 7 天 459 次成功里
  每 Key 实际通过的 rpm **中位 2、p90 6、p99 8、最大 8**，从没突破 8。
  且 429 时 prompt 中位数不足 5k tokens——**不是 tpm 打爆，是账号级 rpm 惩罚**。
  你换成官方 API 也一样：限流按账号算，不是按入口算。
  **已加主动 rpm 闸**（`providers.yaml` 的 `options.rate_limits`：
  `{scope: account, window_seconds: 60, max_requests: 3}`）——
  **事前**让配额见底的 Key 出列去换下一把，而不是撞了 429 才事后惩罚。
  取 3 不取 6：超发会被商汤惩罚数小时，保守的代价只是排队几十秒。
  扩容新 Key 时按同比例上调（每把 Key 独立计，互不挤占）。
  回归测试 `tests/test_limits.py` 的 `test_sensenova_has_a_tuned_rpm_rule_in_real_config`
  与 `test_the_rpm_rule_actually_gates_in_the_limiter` 钉住「规则必须存在、数值=3、
  第 4 个请求必须被拦、换账号不受牵连」。
  ⚠ **但「rpm 惩罚」不能解释 2026-10-24~25 的整池瘫痪**（2026-10-27 复查推翻
  这条的适用范围）：那两天每把 Key 每分钟只发 **1.2 次**（远在 rpm 3 之下），
  可是 9 把从 04:29 到 15:33 **连续 429、一次都没恢复**（每把 81~91 次）。
  同批请求平均 **18.7 万 token**，`tpm/rpm` 混合报错 577 次。
  结论：**小 prompt 时是 rpm 惩罚，长上下文时是 tpm**；同一个 `max_requests: 3`
  对后者无效。判据是「每分钟请求数 vs 每请求 token 量」，别只量前者。
- **一个请求扫满整池 Key，会让后续并发请求连一个上游都发不出去**（2026-10-27
  修）：`retry.max_credentials_per_deployment` 曾是 **9**（等于池大小），
  一个请求按序扫完 9 把、每把 429 挂 60s 冷却 → **整池瞬间真空**。紧接着的
  请求在 `/credential` 阶段就空转返回，`no_available_credential` **0.01 秒**
  命中、上游一次没发。实测 154 条失败请求里 **55 条（35.7%）是这个形状**。
  已降到 **3**：单请求不再有资格独占整池。容量本来就够（商汤 9×3=27 req/min
  vs 客户端峰值 10 req/min），缺的是让渡，不是总量。
  回归测试 `tests/test_limits.py` 钉「配置值是 3」。
- **墙钟预算只在两次尝试之间检查，单条慢腿能吃光整个请求**（2026-10-27 修）：
  `_check_wall_clock` 刻意不打断在飞的请求（截断会毁掉客户端还能读的响应），
  于是实测**一次 AMD 尝试跑 701 秒**，而请求预算只有 300 秒——48 条失败
  （31%）就这么死的，其余供应商全程没被试。
  已加 `retry.attempt_timeout_floor`（默认 20s）+ `attempt_timeout_margin`（5s）：
  每次尝试的读超时 = min(provider 超时, 剩余墙钟 ÷ 剩余部署数 - margin)。
  **floor 是硬下限**：给 100k prefill 切 15 秒等于「每家都失败」，比原来的病更糟。
  `attempt_timeout_floor: 0` 回到旧行为。
  误杀评估：商汤 227 次成功**全部在 30 秒内**，145 秒时间片误杀率 0%。
  ⚠ **它管不了链上最后一条腿**——最后一条按定义拿剩余全额预算。
  `ds` 链实际只有 2 条可用腿（`ds4.1m` 停用），AMD 是最后一条，所以
  「静默等 AMD 300 秒」只能靠降 provider 超时解决：压到 120s 会丢 59 次慢成功
  （235→176），是产品决策。
- **整池冷却时排队，而不是瞬时 503**（2026-10-27 加）：`_wait_for_credential`
  在上游无可用 Key 时先等（上限 10s、不越过墙钟）再重取。
  ⚠ **实测救 0 条**：那 55 条瞬时 503 之后，下一个商汤成功的中位等待是
  **333 秒**——整池是真的真空，不是差一点。所以它只是兜底（避免 0.01s 空跑），
  **不是解药**；真解药是容量（充值/加账号/分流）。别把这条当成已解决。
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

- **npm 装的 CLI 在 Windows 上不能直接 exec**：`shutil.which("codebuddy")` 过得了
  注册表校验（返回 `.cmd`），但 `create_subprocess_exec` 走 CreateProcess 会
  `FileNotFoundError`——qwen/claude/codex/codebuddy/qoder 全中，只是以前没人在
  Windows 上真跑过委派。已加 `app/services/agent/cli/launcher.py`：读 shim 里
  固定的 `"%dp0%\node_modules\..."` 行，换成 `node + 入口脚本`。
  **不要退回 `shell=True`**：那会把模型给的任务文本重新交给 cmd 解析，正好破坏
  「argv 直传、不经 shell」的安全前提；解析不出来就明确报错，不静默降级。
- **`proc.returncode` 要 `await proc.wait()` 之后才可信**：只等两路读流到 EOF 就读
  退出码，asyncio 的子进程回调可能还没跑，`returncode is None` → **成功的委派被判
  失败**（实测 gemini 已吐出完整 JSON，结果仍报【执行失败】、`exit=None`）。
  现写法是 `gather(读 stdout, 读 stderr, proc.wait())`，超时分支照旧 kill。
- **Gemini 的 `plan` 档不产出正文**：它把结果写进 `~/.gemini/tmp/<proj>/plans/*.md`，
  stdout 只回一句"已保存"。机械改写要用 `default`，`plan` 只适合只读分析。
  无头还必须 `GEMINI_CLI_TRUST_WORKSPACE=true`（**字面量 `true`，写 `1` 不认**）
  + `HTTPS_PROXY`；Node 的 fetch 默认不读代理，适配器补 `NODE_USE_ENV_PROXY=1`。
- **Qoder 的 `--permission-mode` 没有 plan 档**（default/accept_edits/
  bypass_permissions/dont_ask/auto），适配器把 `plan` 映射成 `dont_ask`：
  不弹审批、未预授权一律拒绝。直接透传 `default` 会在无头场景等审批等到超时
  （runner 的 stdin 是 DEVNULL）。
- **CodeBuddy/Qoder 的 CLI 登录态与桌面端不共享**：桌面端明明已登录，CLI 仍报
  `Authentication required` / `Not logged in`，首次要交互式 `/login`、`qoder login`。
  这两种原文已补进 AUTH 特征（`base.py`），否则会被当普通失败让父模型白重试。
- **控制台 `@` = 执行体直跑，不是聊天**：`@` 在输入框**任意位置**都能点名
  （`/` 只认行首，避免和路径打架），**一条消息可点名多个、按顺序串行**——
  `POST /zkadmin/agent/delegations`（`items` 最多 10 段，带 `session_id` 则追加）；
  第一段失败即停。分隔符含中文标点（`，。；、：`）；段首尾连接词（用/然后/再/并/和）
  会连剥多次，第一个 `@` 前的文字并进第一段。**每段在分段面板里单独选模型/思考/
  上下文**（默认展开可收起，默认值取自配置；改文本时同位置同角色保留选择）。
  单段直跑用 `POST /zkadmin/agent/executors`。数据源 `GET /executors`（读
  `cli_tools.yaml` 的 `models`/`free_models`）；模型清单优先用 CLI 自己的口径
  （`GET /executors/{role}/models`），免费标记只能手工维护。
  改 Python 要重启网关；只改 yaml 不用（每次新任务重读花名册）。
  ✅ 「停止」会 kill 子进程（`_Running.cancel_event` + runner watchdog，2026-11-12 修）；
  实时日志走 `cli_log` SSE（本地转发，不额外耗额度），超时/取消回显已产生的输出。
- **`--model` 可选值以 `codebuddy --help` 为准**（glm-5.3 / kimi-k3 /
  deepseek-v4.1-flash 都在列），但"在列"不等于"免费"——**以 JSON 里的
  `total_cost_usd` 为准**，这是唯一客观口径。

执行体各家 CLI 的**具体形态、报错原文与实测数字**（Windows shim 不能直接 exec、
CodeBuddy 未登录退出码 0、json 是事件数组、Qoder 登录态不共享、模型名口径、
Claude 只能走 CC Switch、免费额度的形状）已拆到
`docs/免费档执行体-20261025.md` 的**附录**——排查具体执行体时才翻。
**留在本文件的都是规则**：审批档映射、@ 委派语义、产品决策。

### 2026-11-13 委派层对标 Claude Code / Codex 后的九条（都是实测）

这层此前只做到「把一段字符串 argv 传进子进程、再把 stdout **头部** 8000 字返回」。
九条都配了变异验证过的回归测试（`test_agent_cli.py` / `test_agent_prompting.py` /
`test_agent.py`）。别再退回旧写法：

- **审批卡必须展示真正要执行的命令**：提权原来只在 runner 里做、预览用未提权的
  config 渲染 → 批的是「只读」，跑起来是 `--dangerously-skip-permissions`。
  已抽 `delegate.escalated()` 让**预览与执行共用同一判定**，`approval_mode` 进审批
  消息。同时**模型发起的 `delegate_cli` 一律不提权**——`agent_permission_mode: bypass`
  的语义只是「任务空间内不弹审批卡」，不是「把外部 CLI 的沙箱也关掉」。
- **任务信封**（`cli/prompting.py::build_task_prompt`）：**在哪**（绝对路径，任务
  空间不是工作区根）、**交什么**（结论写最后一段、落盘进 `outputs/` 并列出路径）、
  **能不能问**（不能，按假设继续并列出假设——这句最省额度）。MCP 后端**不套信封**
  （task 是结构化入参），`envelope=False` 可回退。
- **段间交接**（`with_context`）：多段 @ 此前只共享目录不共享上下文，「先让 A 查、再
  让 B 审」的 B 在审空气。现在上一段结论（截 2000 字）注入下一段。别拼整段历史——
  一次委派光 CLI 脚手架就 3.6 万 prompt tokens。
- **截断一律保尾**（`clip_output` / `failure_output`）：堆栈、pytest 摘要、结论都在
  末尾。失败详情改成 **stderr 在前**（诊断在 stderr，stdout 多是进度噪音）+ 退出码。
- **超时 / 取消不能归成 FAILED**（新增 `FailureKind.TIMEOUT` / `CANCELLED`）：FAILED
  的建议是「调整任务描述后重试」，对超时是**反向建议**。现在明说「可能已改文件，重试前先看」。
- **`idle_timeout` 改成逐执行体配置**（默认 0 = 不判空闲）：原来全局写死 180s，而
  claude/codex/gemini/codebuddy/qoder 都是「结束才一次性吐输出」，静默是常态 →
  300s timeout 被 180s 空闲判死提前截断（claude 实测 130s）。现役只有 codearts 配 180。
- **claude 走 `--output-format json`**（字段 `result`/`is_error`/`usage`/`total_cost_usd`
  /`session_id`）：收费档最贵的一条，此前账本对它永远空白。`session_ref` 落
  `state.json` 的 `handoff_session`，为 `--resume` 留口。codebuddy 的 `is_error: true`
  也从「挂备注」改成 `fatal_output` 判失败——上游的结论字段不是正文的一部分。
- **预算闸 `daily_calls` / `daily_cost_usd`**（0 = 不限）：额度是**事前**拦，不是撞了
  429 才解释（Gemini 免费档实测 40 分钟打空）。成本只累加**自报**的行——没自报的算 0
  会让熔断永不触发。
- **免打扰按「工具:角色」记**（`SessionApprovals._key`）：放行免费档不该等于放行
  `cli-claude`（收费 + bypass）。`write_file`/`run_command` 仍按工具名。
- **产物清单**：任务空间承诺「产物落 outputs/」却从没人读过——控制台永远「（暂无）」。
  现在三条派单路径都列 `outputs/` 并写 `state.json` 的 artifacts / handoff_brief。



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

### 2026-11-03 复查补的六条（都是实测出来的）

详细记录见 `docs/事件中台.md` §7.1 / §10，这里只留会写错的地方：

- **审批是闭环不是终点**：`decide()` 默认派生一条 `<父类型>.decided` 事件重走
  规则引擎（`followup: false` = 我自己去办）。**新加一条流水线 = 补一条
  `types: [xxx.decided]` 的规则**，不用改代码。
- **`.env` 不会自动进进程环境**：`ZKAI_ADMIN_TOKEN` 只在 `load_app_config()`
  之后才在 `os.environ` 里，而 argparse 的 `default` 在 `parse_args` 时求值。
  **新写的脚本要先加载再取令牌**，否则调用被 401 静默拒掉，
  而 `/health` 不需要令牌 → 很容易误判成「连通性正常、这轮没异常」。
- **`parallel` 是假并行**：worker 单循环串行，`parallel` 只限制同组 running 条数。
  设 3 不会快 3 倍，反会让排队事件被 `reclaim_stale` 当超时回收。
  **要真并行就多开 worker 进程**（claim 原子取一条，多进程安全）。
- **事件规则必须被机器校验**：`check_config.py` 会查 skill 是否存在、`to` 是否
  在花名册、`cwd` 是否存在、**规则监听的事件类型有没有产出方**。改完必跑。
- **`ZKAI_EVENTS_MAX_ATTEMPTS`（默认 3）**：认领 N 次仍没交活判 `failed`，
  不再无限重投刷手机。
- **`budget_cny` 目前是死字段，成本恒为 0**：worker 交活不带成本，
  `CliResult` 也没有成本字段。控制台与卡片上的花费数字**不可信**。
  **先接成本，再谈熔断。**

### 2026-11-04 借鉴 Agent-Autoloop 后定的四条硬规矩

卡兹克开源了 [agent-autoloop](https://github.com/KKKKhazix/agent-autoloop)。
**没照搬架构**（Node + 零依赖 + 无数据库服务；本项目有 FastAPI / SQLite / 控制台 /
路由层），细节见 `docs/事件中台.md` §10。

- **跳数熔断的深度由中台算**：`events.hops` 只从 `parent_id` 推导，事件体里写的
  **一律忽略**（执行体不能自己赦免自己）。上限 `ZKAI_EVENTS_MAX_HOPS`（默认 8）。
  ⚠ 派生事件的去重键必须带 `parent_id|hops`，否则同对象链路会被 30 分钟去重窗口
  吃掉、`hops` 停在 0，熔断形同虚设。**人工拍板也算一跳、不清零。**
- **`complete()` 必须幂等**：已结束 + 结论相同 = 同一次交活，只回一声不重复推送；
  结论不同**不覆盖**。重交绝不能把 `cost_tokens` 清零。
- **回执标记只有一处定义**（`RECEIPT_MARKER`）：提示词教的和解析器认的必须是
  同一个字面量，两处各写一份 = 配置漂移（系统照常跑，只是永远没人回执）。
- **SQLite 的 DateTime 列丢时区**：写 `03:48+00:00` 读出是 naive 的 `03:48`，
  而仓库层比较全用 `utcnow()`（UTC）。**任何参与时间比较的新列都必须存 UTC**，
  否则凭空差出时区偏移（实测差 8 小时 → 复查永不到期）。

⚠ **它的 `askFirst` 别照抄**：那是语义词拼进提示词，靠模型自己判断
「我干的算不算删数据」；它同时有执行体的 `access`（read/edit/full）才是真权限档，
**两套没绑定**。要做决策边界就该绑：`tell: needed` 的规则，
`to` 必须指向 `approval_mode: plan` 的执行体。

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

## 入站断连：「总是在中途断开」的四个根因（2026-11-13 实测定位）

背景：运营者反馈「ds harness / 其它 AI 工具调网关总是断开，只有 Claude Code 最好」。
29 万行 gateway.log + `data/zkai.db` 交叉核对后，**不是路由层的问题**：
`zk-auto` 6749 次请求只失败 1 次、`step-5-preview` 4452 次零失败。四处根因全部已修，
都用真机验证过。

- **① `/v1` 流式没有空闲心跳**（主因）。三条路径都写成
  `asyncio.wait({next_task, watcher})`——**没有 timeout**，上游静默时循环挂住、
  客户端一个字节都收不到，长静默后判连接失败。现在按 `request.stream_idle_timeout`
  （默认 120s）**一半**发 SSE 注释行 `: ping`：SDK 与 ds harness 都按规范忽略 `:`
  开头的行（零兼容风险），有数据流动时完全不发。
  ⚠ `stream_idle_timeout` 此前**是死配置**（读进 Settings 但全仓零引用、无 YAML 桥接，
  改 config.yaml 静默无效）——已补桥接，`test_config.py::test_stream_idle_timeout_is_bridged_from_yaml` 钉住。
  ⚠ 两个坑都踩过：**在飞的 `anext` 必须跨心跳保持同一个 task**（每轮新建会报
  `anext(): asynchronous generator is already running`）；**发完一格必须把 event
  置空**（否则心跳会重发同一个 chunk，实测 part0 被吐 5 次）。
- **② `max_credentials_per_deployment=3` 对单腿模型是致命的**。`kimi-k3` 只有一个
  部署（sensenova，9 把 Key），截 3 把 = 6 把从未被试，连撞 3 次 429 整条请求就失败。
  **让渡的前提是「还有别的腿可退」**，只剩一条时让渡 = 必然失败。已加
  `_is_only_fallible_leg`：链上最后一条可用腿可扫完整个池。真机验证：修复前 429，
  修复后 9 把全试、其中一把恢复、返回 200。
- **③ 429 不带 `Retry-After`**。商汤的 429 正文只有 `inference exceeds tpm/rpm limit`，
  既无秒数也无头，客户端只能瞎撞。现在 `_exhausted` 在「上游没给 + 是 throttle」时用
  **我们自己算的冷却时长**兜底（确定值，不告诉客户端等于浪费）。真机：`Retry-After: 1` / `60`。
- **④ `responses` 路径连断连检测都没有**。客户端走了，上游连接要跑到下一个 chunk
  才回收。已补 watcher（与 chat / messages 同一套）。

**三条流式路径现在结构一致**，改一条时另外两条要同步——不一致本身就是 bug 源。
`keepalive_interval()` 与 `SSE_PING` 定义在 `app/api/chat.py`，另外两处 import 复用。

### 还没做、别再拿它当「已修」

- `latency_ms` 在流式路径记的是 `meta.latency_ms`（首块延迟），不是整条流的墙钟，
  `client_disconnected` 显示 `latency_ms=0.0` 就是这个原因。改它会影响既有统计口径，
  属新工作、不是 bug。
- 上游 429 正文里的「多少秒后再来」没有解析（只认 `Retry-After` 头）。加「从自由文本
  猜秒数」有误判风险，**刻意不做**。

## 华为云两套东西：码道 CLI（可用）vs MaaS provider（已删）

**别把这两个混成一个**——它们凭据、协议、状态完全不同：

| | 码道 CLI（codearts） | MaaS provider（HW） |
|---|---|---|
| 凭据 | 华为云 **AK/SK**（`CODEARTS_CLI_AK/SK`） | `HW_API_KEY_01`（API Key） |
| 协议 | 码道自己的 CLI 协议 | OpenAI 兼容 `/openai/v1` |
| 实测 | ✅ 可用（GLM-5.2 / kimi-k3.1-pro，最快 32.7s） | ❌ 推理 **403**（目录 200 但无权限） |
| 现状 | 现役执行体 `cli-codearts` | **2026-11-14 已从服务器删除**（欠费） |

- 删 HW 时连带删了它独占的模型 `deepseek-v4.1-flash-1017`，`ds` 别名自动收敛成
  `ds4.1m → deepseek-flash → ds4.1`（三条腿全部真实可达）。备份在
  `config/*.bak-20261114-delHW`。
- **码道 CLI 的 AK/SK 只在 `load_app_config()` 之后才进 `os.environ`**：
  独立脚本里直接 `run_delegation` 会报「认证失败，没有设置环境变量
  CODEARTS_CLI_AK/SK」——先加载配置再取凭据（同「`.env` 不会自动进进程环境」条）。
- **codex 续接是子命令 `exec resume <id>`，且 resume 不接受 `-s/--sandbox`**
  （2026-11-14 真机：带了就退出码 2）。续接时沙箱沿用该会话首次创建时的策略，
  这正是想要的语义。真机验过上下文确实延续（问「刚才让你记住的词」答对）。
- qoder 的 json 输出**没有 session id**（只能 `--list-sessions` 事后查，
  并发时无法确定哪条是刚才的）→ 刻意不实现续接。codearts 的 `--format json`
  会触发「文件夹未被信任」→ 也不做。**宁可没有，也不要接错。**

## 代码体量与拆分（2026-11-14）

- **`agent/service.py` 已从 1,818 行拆成 6 个文件**：`service.py`(78) 只做门面，
  `service_session/loop/delegate/child.py` 是四个 mixin，`service_common.py`
  放共享常量与 `_Running`。**44 个方法体逐字节未改**（sha256 比对过），
  对调用方/测试/行为零影响。再往里加方法时按职责进对应 mixin，别再堆回门面。
- **mixin 的两个搬运坑**（只有"逐字节搬"才暴露）：`@staticmethod` 与
  `@dataclass(slots=True)` 装饰器在 `ast` 的 `node.lineno` **之前**，
  按行号取源码会丢；方法名搬走后 `monkeypatch.setattr("...service.run_delegation")`
  会失效——改测试指向真实模块（`service_delegate`），**别做 re-export 骗人**。
- **mypy 需要类型声明**：mixin 组合后静态检查器看不到 `self` 成员，
  在每个 mixin 里用 `if TYPE_CHECKING:` 写类级注解（运行期零开销）。
- **burner 配置键是单一来源 `config/burner_keys.json`**：脚本
  (`_CONFIG_KEYS`) 与 `config_io.CONFIG_KEYS` 都读它，脚本另留一份兜底
  （`_FALLBACK_CONFIG_KEYS`，保持独立运行）。**加新键只改 JSON**——
  测试 `test_burner_key_single_source.py` 三向比对（JSON/脚本/兜底）。
- **迭代期用 `scripts/quick_test.py` 挑测试，提交前才跑全量**（2026-11-14 实测：
  全量 136 秒 vs 改路由只跑相关 17 秒，**8 倍**）。它按 `git status` 的改动路径
  映射到测试文件；`app/main.py`/`container.py`/`errors.py`/`conftest.py` 这类
  横切文件**强制回落全量**，没登记的路径也回落全量（宁可慢，不许静默漏跑）。
  映射表本身有测试（`test_quick_test_map.py`：指向的文件必须真实存在）——
  第一版我就写错两个文件名，是它抓出来的。**它不是门禁替代品**。
- **AGENTS.md 65,536 字节预算现在有测试守着**：`test_doc_scale.py` 的两条，
  余量 < 2,048 就红。**规则写进文档 ≠ 会执行**，这次终于工具化了。
  撞墙时的正确做法是「结论上提、过程下沉」（见 `docs/排查过程详述.md`），不是删规则。
- **消耗器代码保留不删**（2026-11-14 修正上一轮的判断）：停用时零运行开销
  （无后台任务，只在打开控制台页时走 HTTP），而它随时可能恢复；
  删了会让恢复变大工程 + 控制台「消耗器」页全废。

## 流式耗时与委派续跑（2026-11-13，B-3/B-4）

- **`requests.stream_duration_ms` ≠ `latency_ms`**：后者是**首块延迟**（别改语义，
  控制台和历史统计都按它读），前者是**整条流墙钟**。`client_disconnected` 那批行
  `latency_ms=0.0` 是「没拿到首块」，不是「0 秒跑完」——排查断裂要看新字段。
  迁移：`Database._COLUMN_MIGRATIONS` 加一行幂等 ADD COLUMN。
- **委派续跑**（`CliAdapter.resume_args` + `resume_ref`）：`session_ref` 落
  `state.json` 的 `handoff_session` + `handoff_tool`，**同任务空间 + 同执行体**才复用
  （claude 的 uuid 给 codex 没意义）。目前只有 claude 映射 `--resume`（位置在 `-p`
  之前）；codex/qoder/codearts 的旗标未实测，`resume_args` 默认返回 `[]`——
  **别凭文档瞎拼**，实测通过再开。
- **轮换上限 3 vs 9 是两地有意不同**（本地让渡、服务器账号多），`check_config`
  现在会打印当前值 + 池规模，只报可疑关系（>池大小、或 =1 而池≥3），不评判取值。

## 当前状态（持续整理：2026-10-07）

- **`.env` 是运营配置，不是测试夹具**：绝不用 `read_text()` 读写它（CRLF 会被悄悄
  归一成 LF，半 LF 半 CRLF 混着写就是事故）；要么全程字节，要么 `open(..., newline="")`。
  门禁跑不了就停下来问，别为了跑测试改它。`malformed_env_names()` 是现成体检入口，
  返回空才算健康。2026-09-24 换机后那份 `.env` 115 行全 `\r\r\n`，症状是网关 bind
  失败却报 `getaddrinfo failed`——排查方法见「已知坑」。
- **战略判断：别再往 `/v1/*` 层加编排**。已量化过：四段编排（分工→规划→执行→审核）
  会让 Codex 周成本从 $653 涨到 $3,277（5 倍，44.7% 的工具回执也在付编排费），
  与用户「省 token」的核心诉求方向相反。要高质量产出走 `/ui/agent` 任务台
  （一次任务付一次编排成本），不走对外路径。**真正管 token 的是客户端侧**：
  claude-cli 每轮重发全量历史，网关分工管不了它，杠杆是「适时开新会话」。
- 主干功能完整。**消耗器 2026-10-27 起已灰色停用**（`burner.yaml` 的
  `disabled: true`）：商汤当天下线 Flash-Lite **消费返赠**，而消耗器的全部意义
  是「烧专属池 → 1:1 回充成 kimi-k3 积分」，返赠没了继续烧就是净消耗一份
  带到期日（2026-11-09）的固定余额。配置与启动按钮都保留，恢复只需改回
  `disabled: false`，**但恢复前先核实返赠机制是否回来**。
  moonshot 已整块删除（2026-09-29，无 Key，3,102 次尝试 0 成功），要恢复需同时加回
  providers.yaml 的供应商与 models.yaml 的部署；`kimi-k3-nvidia` 仍保留，靠自动隔离兜底。
- **商汤额度 2026-10-27 翻倍，改配置时必须同步三处**：5h 窗口 6万→**12 万**积分、
  周窗口 60万→**120 万**。公告同时说明会统一重置当前可用额度至新上限。
  三处是：①`providers.yaml` 的 `rate_limits`（token 口径，5h 3亿→**6 亿**、
  周 30亿→**60 亿**）②`burner.yaml` 的 `window_credits`/`weekly_credits`
  ③`scripts/burn_sensenova.py` 的 `DEFAULT_WINDOW_CREDITS`/`DEFAULT_WEEKLY_CREDITS`
  （status/reconcile 的兜底来源）。**只改前两处不够**——配置键缺失时会回落到
  代码常量，两者不一致就会出现「控制台按旧线画、脚本按旧线停」。
  顺带修掉 `config.get(x) or 默认值` 把合法的 **0** 当成缺失换掉的坑
  （额度写 0 是「不限制」的明确意图），改用 `is None` 判缺失。
- **Flash-Lite 现在连调都调不通**（2026-10-27 实测）：`sensenova-6.8-flash-lite`
  仍躺在 `/v1/models` 目录里，但实际调用返回 **404 `model route not found`**；
  `6.7-flash-lite`、`u1.5-lite` 同样 404，而 `deepseek-flash` 返回 429（说明有效）。
  所以「把那 119 万专属池余额当执行体免费后端」这条路**走不通**，
  已实测否决。又一处「目录里有 ≠ 能调用」（同 §已知坑 的三反例表）。

