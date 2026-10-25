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
  另有三份**从本文件拆出的流程文档**（规则文件超 65,536 字节预算）：
  `docs/服务器同步.md`、`docs/部署与运维坑.md`、`docs/控制台前端约定.md`——
  只做对应的事时才翻，规则正文仍以本文件为准。
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
  `only:` 列的是 `SENSENOVA_API_KEY_01`~`_09`（2026-10-05 统一改名，原 `_10` 值移到 `_01`）。
  消耗器满速烧时，网关打这些账号的 kimi-k3 必然
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
- **`reload_config()` 曾经漏更新冷却策略，`cooldown:` 段改了不生效**（2026-10-13 运营者
  报「商汤只试一个 Key 就跳 NVIDIA」时挖出来）：`build_container` 一次性构造
  `CooldownPolicy` 塞进 `pool.policy`，而 `reload_config()` 重建 router / 别名 /
  凭据注册 / rate_limiter，**唯独不重建它**。于是 `cooldown.throttle_scopes` 属于
  「改完 YAML 自动生效」承诺里的一个洞——必须整进程重启才生效。实测后果：
  运营者 2026-10-08 把 `throughput` 从 `account` 改成 `credential`，日志里**仍**是
  `429 (throughput) has account-wide cooldown -> failing over instead of rotating keys`，
  行为一模一样；DB 佐证 09-30/10-01/10-05 各有 1151/1776/500 次请求**只试了 1 个
  商汤 Key 就换供应商**，其余 8 把健康 Key 一次都没被试。
  已修（`reload_config` 里两行）：`self.pool.policy = CooldownPolicy.from_mapping(...)`
  且 `self.pool.tracker.policy = self.pool.policy`——**tracker 那个也必须跟着换**，
  它才是真正算冷却时长的对象，只换 pool.policy 会让「字段对、行为不变」。
  回归测试 `tests/test_config_watch.py` 的 `test_editing_the_cooldown_section_reaches_the_pool`
  （钉字段 + 钉 tracker 同一个对象）与 `test_a_cooldown_edit_changes_the_failover_decision`
  （钉 `_normalize_throttle` 的真实决策），已用变异法验证鉴别力。
  ⚠ **凡 reload 时重建了一部分派生对象，就要问「还有谁持有旧配置的切片」**——
  这次漏的是 CooldownPolicy，同类候选：RetryPolicy、RateLimiter 的规则表。
  2026-10-24 同型又抓到两处并已修：①pool 的 `credential_affinity` 三参数
  只在建池时读一次（reload 现经 `pool.apply_affinity_config` 补读，测试
  `test_editing_the_affinity_section_reaches_the_pool` 钉住）；②scheduler
  部署粘性的 TTL/容量是构造期快照（已改成**现场读池子**——快照必然在热重载
  后变陈旧；顺带修掉了 getattr 猜错属性名恒回落魔数 64 的旧账）。教训的两半：
  **配置段要有人补读，派生副本不如活读**。
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
- **免费额度是「人机对话」的形状，不是 agent 负载的形状**：一次委派（5 条商品
  描述的 JSON 改写）CLI 自报 **prompt 36,853 tokens**（任务正文才 ~250），
  40 分钟内十几次请求就把 Gemini 免费配额打空（429 `exceeded your current quota`）。
  同一模型：直连 4~5s，经 CLI 8.8~18s（进程启动 ~2s + CLI 自己的路由/分类调用 +
  系统提示与工具清单的 token）。结论：免费档**只当 `delegate_cli` 的派单目标**，
  不进 `/v1` 热路径、不进 `zk-auto` 候选集。
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
- **CodeBuddy 的 `--output-format json` 是事件数组，不是单个对象**（2.175.0 实测）：
  `[{type:"message",…},{type:"reasoning"},{type:"result",result:"pong",
  total_cost_usd:0,usage:{…}}]`。前面几条 message 事件里带着**完整请求体**
  （system prompt 含项目 AGENTS.md）。按单个对象解析会失败并回落到裸文本，
  等于把整份上下文灌回父模型——`_codebuddy_result_event()` 现在只取最后那条
  `type=result`。回归测试 `test_codebuddy_event_array_yields_only_the_result`。
- **实测三条免费档执行体都能跑（登录后，2026-10-25 21:30）**：codebuddy
  `deepseek-v4.1-flash` 9.5s、**自报成本 $0.0000**（入 45,817 / 出 248，
  其中绝大部分是 CLI 自带脚手架）；qoder `DeepSeek-Flash` 29.8s（限时免费至 11-01）；
  gemini `gemini-2.5-flash` 12.2s（入 9,400 / 出 36）。角色名
  `cli-grinder` / `cli-grinder-qoder` / `cli-grinder-gem`。
- **Qoder 的模型名要用 `qoder --list-models` 的显示名**：`DeepSeek-Flash` /
  `GLM-5.3` / `GLM-5.3-Flash` / `Kimi-K3.1-Pro` / `Qwen3.8-Max` …，
  不是官网的 API ID（写 `deepseek-v4.1-flash` 不认）。自建自定义模型在列表里显示成
  `(qoder-custom-…)`，本机那两条正是指向 zkai 网关的。
- **执行体的 `effort` / `context_window` 是「上限提示」，不是扩容**：`effort` 映射
  `--effort`（codebuddy）/ `--reasoning-effort`（qoder），`context_window` 映射
  `--autocompact`（codebuddy，压缩阈值）/ `--context-window`（qoder，**不填按 128K**）。
  两家默认都不是满档。实测 max+1M：codebuddy 8.7s、qoder **56.9s**（默认档 29.8s）
  ——满档思考的代价是墙钟翻倍。**执行体拿不到会话历史**，要长上下文走 `/v1`。
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
  ⚠ 「停止」只改状态，**不 kill 子进程**（`run_delegation` 没有取消句柄）。
- **skill 不是接口，是提示包**：本机技能只在 `~/.agents/skills`、`~/.claude/skills`、
  `~/.codex/skills`、`~/.catpaw/skills` 四处（codebuddy/qoder/gemini 目录为空）。
  所以「用 skill」= 把活派给装了 skill 的 CLI，而不是给网关加工具。
  `cli-skill`（codex）自带 15 个技能，`cli-claude`（Claude Code）另有 9 个。
- **Claude Code 只能走 CC Switch 的模型**：`-p` 是 SDK 分支，客户端先校验模型归属，
  `zk-k3` / 连 `claude-sonnet-4-5` 都判 `unrecognized_model`，请求到不了网关。
  所以执行体 `model: ""`（不传 `--model`），模型由 CC Switch 决定、走
  `127.0.0.1:15721` 本地代理，**CC Switch 没开就调不通**。
  它也没有无头可用的只读档，`approval_mode: bypass` → `--dangerously-skip-permissions`
  （注册表为此开了显式白名单值，只给用户亲自 @ 的执行体用）。
  实测经生产路径 ok 但 **130s**——慢在 CC Switch 那条链路，不是我们的超时。
- **npm shim 指向的入口不一定是 JS**：Claude Code 的 `claude.cmd` 直接指向
  `bin/claude.exe`，`launcher` 原来一律交给 node，报 `ERR_UNKNOWN_FILE_EXTENSION`。
  现在 `.exe` 入口直接跑，只有脚本入口才用 node。
- **`--model` 可选值以 `codebuddy --help` 为准**（glm-5.3 / kimi-k3 /
  deepseek-v4.1-flash 都在列），但"在列"不等于"免费"——**以 JSON 里的
  `total_cost_usd` 为准**，这是唯一客观口径。

## 服务器化改造的坑

（已拆到 `docs/部署与运维坑.md`，做这类事时才需要翻。）

## 手机版控制台改造

（已拆到 `docs/控制台前端约定.md`，做这类事时才需要翻。）

## Agent 工作方式（2026-09-23，防半途而废）
- **本机 shell 是 PowerShell，here-string 会把中文和转义写坏**：写含中文的临时脚本时
  先用 Python 以 ASCII + `chr()` / `\uXXXX` 生成文件再执行，别直接把 here-string
  粘进命令行；多行一次性命令还可能被策略整段拒绝，表现为「做到一半停了」
- **回复全程用中文**：文件路径、命令、字段名保留原文，但叙述不用英文；用户把「回复夹英文」
  列为明确不满，动手写每句话前先确认这一点
- 临时脚本统一放 `exports/_*.py`，跑完即删；不要把审计脚本留在工作区

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
- 主干功能完整；消耗器已上线（费率实测校准、AIMD 自适应并发、账本持久化）。
  moonshot 已整块删除（2026-09-29，无 Key，3,102 次尝试 0 成功），要恢复需同时加回
  providers.yaml 的供应商与 models.yaml 的部署；`kimi-k3-nvidia` 仍保留，靠自动隔离兜底。

## 服务器怎么同步代码

（已拆到 `docs/服务器同步.md`，做这类事时才需要翻。）

