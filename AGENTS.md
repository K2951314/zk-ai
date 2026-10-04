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
- `docs/` 放**一次性的排查/审查报告**（带日期的 `*.md`），不是常读文档——
  结论该沉淀的往上写进本文件或 `使用手册.md`，报告本身只作过程留档。
  另：改完 `config/*.yaml` 跑 `scripts/check_config.py` 做自洽性体检
  （悬空部署、不可解析 target、启用了但 0 可用部署、外部改动未 reload）。
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
  `only:` 列的是 `SENSENOVA_API_KEY_02`~`_10`——9 把里 8 把重合（只有不带后缀的
  `SENSENOVA_API_KEY` 是网关独享）。消耗器满速烧时，网关打这些账号的 kimi-k3 必然
  撞 tpm/rpm 429。**诊断网关 429 先看消耗器是否在烧**，这不是网关的错。
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
  顺带证实积分规则就是「5h 6 万 / 周 60 万」，与 burner.yaml 一致。
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
  `max_tokens=1` 的真实请求，短超时（默认 20s，「挂死」本身就是结论），
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
- **消耗器改费率：改 `burner.yaml` 必须重启，改 `burn_state.json` 不用**（2026-09-29
  踩到）：加载顺序是 `args`(yaml) → `_load_rates_from_state()` 用 state **覆盖** args →
  进内存；而 `save_state()` 又把内存值**写回** state。这个环意味着
  **手工改 `burn_state.json` 会长命进程的下一次保存抹掉**——看起来「校准生效过又没了」。
  已加 `_rate_fields()` 守护：进程记住自己加载时见过的费率，发现文件被外部改过就
  保留文件值并打 WARN（要生效仍需重启，因为记账用的是内存里的旧值）。
  回归测试 `test_external_rate_calibration_survives_a_running_process`。
  ⚠ 别把 `_loaded_rate_in == 0` 当成「外部改成了 0」——那只是本进程还没加载过账本。

- **费率优先级是「命令行 > burner.yaml > 账本」，账本不许覆盖配置文件**（2026-09-30
  修，K_02 提前停的根因）：`_load_rates_from_state()` 原来无条件用账本覆盖 args，于是
  运营者在 `config/burner.yaml` 里写死的校准值被账本里的旧值盖掉——日志实锤
  「已从账本恢复校准费率：入830/出2500」而 yaml 写的是 761/2292。费率偏高 9.1%
  的后果不是「算错数」而是**真金白银少烧**：K_02 按虚高记账撞上 54 万熔断线停靠
  （账本估 539,964），真实只烧 495,085、控制台剩 104,488——运营者看到「明明还剩
  10 万却在限流」。判据是 `args._config_used`（`parse_args` 记下哪些键来自配置文件），
  只有 yaml 没提的键才回落账本；生效值同时写回账本，否则下次重启旧值又被当成
  「上次校准」恢复。**凡「账本/缓存覆盖显式配置」的写法都要产质疑一遍**：记忆盖掉
  明确意图 = bug。回归测试三条（yaml 赢账本 / 账本兜底 / 命令行最高）。
- **`ModelAliasConfig.front_model` 以控制台为准，且会写回 `models.yaml`**：
  `POST /admin/aliases` 走 `_write_alias_file()` 落盘，所以「配置文件里是 A、实际
  生效是 B」通常不是 bug，而是有人在控制台改过。排查前先 `grep front_model= data/gateway.log`
  看它实际用的是什么、什么时候变的。
- **`max_total_attempts` 是次数上限，不是时间上限**（2026-09-28 补
  `ZKAI_MAX_REQUEST_SECONDS`，默认 300s）：20 次尝试 × 60s（nvidia read timeout）
  = 最坏 20 分钟。实测一次请求磨了十几分钟、客户端一个字节都没收到——每次尝试单独看
  都没超时，所以次数闸认为还有预算。墙钟预算只在**两次尝试之间**检查，不会打断已在
  飞的流（那会截断客户端还能读的响应）。默认 300s = 现役最大上游超时（商汤 300s），
  一次成功的慢生成不会被误杀。
- **429 不能再默认「换一把兄弟 Key」**（2026-09-28 修）：分类器按正文分出
  `ThrottleKind`（`quota` 额度用尽 / `throughput` tpm / `frequency` rpm·rps /
  `unknown`），`cooldown.throttle_scopes` 决定冷却覆盖到哪：
  `credential` 只冷却这把（rpm 类，NVIDIA 按 Key 计 40rpm 就属这类）、
  `account` 同 `account-*` 标签一起冷、`provider` 整个供应商一起冷并**直接故障转移**。
  默认 `quota`/`throughput` 是 `provider`——商汤这两个桶账号间共享，扫 7 个账号
  只会拿回同一个 429（19.7% 尝试纯空转的由来）。判「换 Key 还是换部署」只看
  `CredentialPool.effective_throttle_scope()` 一个方法，池子与调度器共用，
  否则两边判断会漂移。
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
- **消耗器的账号间 TPM/RPM 争抢，以及 AIMD 会饿死账号**（2026-09-19 首见、
  2026-09-28 修）：商汤的 `tpm`/`rpm`/`rps` 是**账号间共享的桶**，一个实例里多个
  账号在烧就是在互相挤。
  - 现象：某个账号稳定吃到高并发（实测并发 16、43 次采样全是 16 从不回落），
    其余账号撞 429 → AIMD 减半 → 锁在 1 → 永远挤不进来。实测五个账号连续
    77 次采样 **0 成功**，全站 19,714 次 429 对 80,345 次成功（19.7% 纯空转）。
  - 根因：`scripts/burn_sensenova.py` 的加性增只看「自己 60s 没撞 429」就 +1，
    **不看同实例其他账号占了多少**——马太效应，零让出机制。
  - 修法（`_rebalance_concurrency`）：饥饿救济。饿超 `--starve-after`（默认 300s）
    的账号临时借 1~2 级并发，成功一次收回；**三条限制必须一起看**：
    ①`--starve-grace`（默认 120s）启动宽限期——旧账本没 `last_ok`，不宽限会让
    所有账号同时算「从未成功」、一秒内一起借出（实测 7 个账号同时借出后
    tpm 35 / rpm 20 / rps 19 三种 429 齐炸）；
    ②**频率类限流不救**——`rpm`/`rps`/`qps`/`tps` 卡的是"发得多频"，加并发只会
    撞得更狠，只救 `tpm`（吞吐类）；
    ③借出后仍撞 429 立刻收回，不爬到硬顶。
  - **诊断时先分清 429 类型**：`tpm exhausted`=吞吐不够，
    `rpm/rps exhausted`=发得太频，`entitlement exhausted`=额度真用尽（长停靠）。
    控制台「积分消耗器」页已把三者拆开显示（限流 / 额度用尽 / 已救济）。

      - **限流停靠：治「挤了也没用」的空转**（2026-09-30 加，`rate_park_after` /
        `rate_park_seconds`，默认 5 次 / 600s）。饥饿救济治「从没挤进去过」，这条治
        「挤了也没用」：账号被 AIMD 压到并发 1 后，每 60s 冷却期满又去撞一次，撞完
        继续被压回 1——**永远在试，永远失败**。判据是账号级 `rate_streak`（成功即清零，
        不用 Key 级 `streak`：一个账号可能有多把 Key，只看一把会把「换把 Key 就好」
        误判成死锁），达到阈值就把 `rate_park_until` 推到 10 分钟后，`pick_key` 跳过。
        停靠与预算熔断**互相独立**：预算够但挤不进桶 → 限流停靠；桶空了但积分烧完 →
        `budget_allow` 停靠到窗口边界。两个都落盘，重启不清零。
        ⚠ 改它要同步三处：`burn_sensenova.py` 的 `_CONFIG_KEYS`、`app/services/
        burner_service.py` 的 `CONFIG_KEYS`（控制台白名单）、`config/burner.example.yaml`。
  - 老坑仍然有效：满速烧 flash-lite 时同账号 K3 报 `inference exceeds tpm/rpm
    limit`——K3 频繁 429 先查消耗器是否在烧，用它时把消耗器停掉或调低
    `--per-account-max`
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
- **导入迁移包前必须先停网关**（2026-09-21 为这条流过血）：换机包覆盖 `data/zkai.db`
  时，若同目录还留着上一代的 `-wal`/`-shm`，下次开库会把别人的 WAL 帧回放到新库上 →
  `database disk image is malformed`，且**不是警告是真损坏**（本机 `requests`/
  `usage_records` 就是这么废的；SQLite 的 `-wal`/`-shm` 不属于迁移包内容）。一键换机
  Skill 引擎的 `import` 会先备份 side-car 再删除；但最稳的做法是先从托盘退出网关再导入。
  验证损坏库要用 sqlite3 backup API 取一致快照再 `PRAGMA integrity_check`——直接 `cp`
  正在写的库必然报假阳性
- **`~/.codex/config.toml` 归 ChatGPT app 所有，只能外科手术式 patch**（2026-09-22）：
  整文件重写会毁掉 app 的 mcp_servers/plugins/projects/desktop 段落（真实文件上百行）。
  `chatgpt_service.patch_text` 只增改我们的键：顶层键只可能在第一个 `[section]` 前动，
  provider 表只在其表体内动；`apply_config` 读文件必须 `newline=""`（`read_text()` 会把
  CRLF 归一化成 LF，悄悄改掉 app 的换行风格）；`auth.json` **永远不写**（运营决策）。
  改 `env_key`/`ZKAI_API_TOKEN` 后记得桌面版要重启才读到新环境变量；**换机后这一步要
  手工做**——一键换机 Skill 只搬文件，不碰 `~/.codex/config.toml` 和用户级环境变量
  （`.migrate/manifest.toml` 有标注）。`POST /admin/chatgpt/sync-env` 或控制台面板
  「🔧 同步令牌」可以代劳，但不会自动触发。
  桌面版报 `Missing environment variable: X` = 用户级环境变量缺 X：诊断看
  `GET /admin/chatgpt` 的 `env_key_visible`/`env_key_matches_gateway`（陈旧令牌会 401），
  一键修复 `POST /admin/chatgpt/sync-env`，然后重启桌面版。
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

## 服务器化改造的坑（2026-10-03）

- **公网入口只能靠 `ZKAI_PUBLIC_BASE_URL` 显式配置，别用 host:port 猜**：网关只听得到
  自己的 host:port，看不到 Caddy 加的 `/zkai` 前缀。猜出来的地址在公网上打不开，
  而运营者会把它抄给客户端——这种错误静默且难查。判据集中在
  `Settings.public_base_url_effective` / `client_base_url` / `is_remote_deploy` 三个
  属性上，`/health` 的 `deploy` 块与控制台接入面板共用同一份，别在第二处再判一次。
  未配置时回落 `http://<host>:<port>`，本机零配置可用。

- **消耗器账本校准必须注入事件，不能覆盖 burned**：`apply_reconcile()` 写回 burned 时
  是往 `events` 里追加一个差值事件（负数 = 账本多记了），不是改 burned 值——
  burner 重启后按「最近 5h 的 events」重算，直接覆盖会让新旧口径在窗口边界上跳变，
  运营者看到的数字和消耗器实际用的从此对不上。回归测试
  `tests/test_burner_reconcile.py::TestApplyReconcile::
  test_calibration_event_shifts_the_window_without_rebuilding_it`。

- **消耗器重启是双形态，别默认托盘**：`request_restart_ex()` 检测到 systemd 有
  `zkai-burner` unit 就 `systemctl restart`，否则走托盘的 restart.request 文件，
  两者都没有才回 `manual` + 手动命令。服务器上托盘不存在，沿用旧的
  「写文件等托盘看」会永远不生效。响应带 `restart_mode`
  （`tray` / `systemd` / `manual`），界面据此提示。

- **消耗器日志 SSE 的 `flush_interval -1` 四个分支都要有**：`/admin/*` 按认证分了
  四个 `reverse_proxy` 块，漏一个该路径下的 SSE 就被 Caddy 缓冲成一次性返回，
  「实时」变「日志滚完才给你」。另外 `_follow()` 的预热底料和游标必须同一次算出来
  （`_warm_up()` 返回 `(text, offset)`）——分开算的话两者之间新追加的内容会被游标
  跳过，半行测试盯的就是这条。

- **burner 的账本/日志目录可以和网关 `data_dir` 不是同一层，别拼 `data_dir`**（2026-10-03
  上线时才踩到）：服务器上 `zkai-burner.service` 的 ExecStart 用
  `--state-file/--log-file /var/lib/zkai/burner/`，比网关的 `ZKAI_DATA_DIR`
  （`/var/lib/zkai`）深一层。网关侧的 `_burner_paths()` 原来拼
  `resolved_data_dir / burn_state.json`，控制台的「账号核对」于是去读写一个
  **不存在的账本**——运营者填了后台真实消耗、点了保存、看到「已应用」，
  而真正的 burner 什么都没看到。比没有这个功能更糟：它让运营者以为校准生效了。
  现在走 `Settings.burner_state_path` / `burner_log_path`（`ZKAI_BURNER_DIR`
  覆盖，默认仍与 data_dir 同目录，本机行为不变），systemd unit 里用
  `Environment=ZKAI_BURNER_DIR=/var/lib/zkai/burner` 与 ExecStart 对齐。
  `/health` 的 `deploy.burner_state_path` / `burner_log_path` 把真实路径暴露出来，
  两边不一致时一眼能看到。回归测试 `tests/test_deploy_config.py::TestBurnerDir`。

## 手机版控制台改造（2026-10-04）

- **三件事同时成立，少一个就只是「桌面仪表盘被撞窄」**：底部四条挤指可及的 Tab（总览/模型/消耗器/更多，其余收进抽屉）、表格卡片化（`tbody td::before` + `data-label`，列名显示在值左侧）、长页面手风琴。前端只有一个断点：`@media (max-width: 860px)`，桌面端不变（`#mnav` 默认 `display: none`，只在媒体查询里打开）。
- **固定定位的底部 Tab 会被子元素带飞**：实测到一个 392px 的 `modelscope → deepseek-ai/...` 把 `#mnav` 带到 869px，Tab 翻到屏幕外。所以三层都要：根节点 `overflow-x: hidden`、`td > * { min-width: 0 }`、长串 `word-break: break-all`（`break-word` 在没空格的链路上仍不断行）。
- **账号核对表单的锚点列恒为空（2026-10-04 修）**：`GET /admin/burner/reconcile` 写死 `_fmt_hhmm(0.0)`，而它对 0 恒返回 `""`。运营者看到空表单会以为账号没配锚点，实际配了。现在走 `account_anchor_ts` / `account_week_anchor_ts`（按账号优先、回落全局、坏值不抛异常）。
- **重启网关只能通过托盘**：gateway 是 `tray_launcher.py` spawn 的子进程，而 `RESTART_REQUEST` 邮箱**只对 burner 模式生效**（`_consume_restart_request` 只在 `self._mode == "burner"` 时调），且托盘不会自动拉起已退出的子进程。所以改代码后重启网关 = `python scripts/launch_hidden.py gateway`（它先杀同模式旧托盘，新托盘的 `_guard_port()` 再清旧 uvicorn）。看 `launch_hidden` 的 docstring 会以为它只防双弹托盘，其实它就是重启入口。

## Agent 工作方式（2026-09-23，防半途而废）
- **本机 shell 是 PowerShell，here-string 会把中文和转义写坏**：写含中文的临时脚本时
  先用 Python 以 ASCII + `chr()` / `\uXXXX` 生成文件再执行，别直接把 here-string
  粘进命令行；多行一次性命令还可能被策略整段拒绝，表现为「做到一半停了」
- **回复全程用中文**：文件路径、命令、字段名保留原文，但叙述不用英文；用户把「回复夹英文」
  列为明确不满，动手写每句话前先确认这一点
- 临时脚本统一放 `exports/_*.py`，跑完即删；不要把审计脚本留在工作区

## 当前状态（持续整理：2026-10-04）

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
  ③`malformed_env_names()` 是现成的体检入口，返回空才算健康。
  ④换机已改用一键换机 Skill（项目 `migrate.py` 已删），Skill 引擎不检查 `.env` 换行，
  但启动日志仍会替你把关。
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
- ~~待办~~（已解决）：moonshot 已整块删除（2026-09-29，无 Key，3,102 次尝试 0 成功），
  `kimi-k3-moonshot` 部署不存在了。`kimi-k3-nvidia` 仍保留但靠自动隔离兜底
  （见上面「部署级自动隔离」那条）。

