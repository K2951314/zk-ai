# ZK-AI

> **只想赶紧用起来？直接看 👉 [使用手册.md](使用手册.md)** —— 三步上手：双击启动 → 改配置 → 调用。
> 本文档讲的是**为什么这么设计**以及完整参考，不需要从头读完才能用。

**个人 AI Gateway / Model Router** — 把 OpenAI、Anthropic、Gemini、OpenRouter、Ollama
和任意 OpenAI 兼容端点收拢成**一个** OpenAI 兼容接口，自带多 Key 池、错误分类驱动的
重试与故障转移、可解释的能力路由、用量统计。

客户端永远只认一个 `base_url` 和一组稳定的别名（`zk-coding` / `zk-reasoning` /
`zk-cheap` …），背后换模型、换供应商、换 Key 都不需要改一行调用代码。

```
                  ┌──────────────────────────────────────────────┐
  OpenAI SDK ──▶  │  /v1/chat/completions   /v1/responses        │
  LangChain  ──▶  │  /v1/models             /health              │
  curl       ──▶  │  /admin/*   (运维面)                          │
                  └───────────────────┬──────────────────────────┘
                                      │  Model Alias 解析
                                      ▼
                  ┌──────────────────────────────────────────────┐
                  │  Capability Router   (可解释加权打分 + 硬门槛) │
                  └───────────────────┬──────────────────────────┘
                                      │  有序候选 (deployment)
                                      ▼
                  ┌──────────────────────────────────────────────┐
                  │  Scheduler  重试 / 轮换 / 冷却 / 故障转移      │
                  │  ← Error Classifier 决定每一步往哪走          │
                  └───────────────────┬──────────────────────────┘
                                      │
      ┌───────────────┬───────────────┼───────────────┬───────────────┐
      ▼               ▼               ▼               ▼               ▼
   OpenAI         Anthropic        Gemini        OpenRouter       Ollama
                    + 任意 OpenAI 兼容端点（商汤日日新 / NVIDIA NIM / Kimi…）
   (Key Pool: HEALTHY / COOLDOWN / UNHEALTHY / DISABLED)
```

> **想直接接你自己的 Key？** 跳到 [§3.5 接入真实供应商](#35-接入真实供应商商汤日日新--nvidia--kimi)，
> 两份配置模板复制即用（商汤日日新一把 Key 调 Kimi K3 + NVIDIA NIM + Kimi 官方兜底）。

---

## 目录

- [1. 核心特性](#1-核心特性)
- [2. 快速开始](#2-快速开始)
- [3. 离线端到端验证（零真实花费）](#3-离线端到端验证零真实花费)
- [3.5 接入真实供应商（商汤日日新 / NVIDIA / Kimi）](#35-接入真实供应商商汤日日新--nvidia--kimi)
- [3.6 在本地客户端里调用（ZCode / Claude Code / OpenAI SDK）](#36-在本地客户端里调用zcode--claude-code--任意-openai-sdk)
- [4. 架构分层](#4-架构分层)
- [5. 目录结构](#5-目录结构)
- [6. 配置](#6-配置)
- [7. 供应商与已验证的协议细节](#7-供应商与已验证的协议细节)
- [8. 模型别名](#8-模型别名)
- [9. 能力路由](#9-能力路由)
- [10. Key 池与状态机](#10-key-池与状态机)
- [11. 错误分类决策矩阵](#11-错误分类决策矩阵)
- [12. 重试、退避与故障转移](#12-重试退避与故障转移)
- [13. API 参考](#13-api-参考)
- [14. 流式输出](#14-流式输出)
- [15. 健康检查](#15-健康检查)
- [16. 数据库](#16-数据库)
- [17. 日志与安全](#17-日志与安全)
- [18. 测试](#18-测试)
- [19. Docker](#19-docker)
- [20. 开发约定](#20-开发约定)
- [21. 已知限制与后续计划](#21-已知限制与后续计划)

---

## 1. 核心特性

| 能力 | 说明 |
|---|---|
| **OpenAI 兼容** | `/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/health`。请求体接受任意 OpenAI SDK 会发送的参数，未知参数**记录而非静默丢弃**。 |
| **四层抽象** | Provider → Deployment → Model → Credential。一个模型可以有多个 Deployment（多供应商/多区域），失败自动切换。 |
| **Key Pool** | 显式状态机 `HEALTHY / COOLDOWN / UNHEALTHY / DISABLED`，5 种轮换策略，`threading.RLock` 保证选择原子性。 |
| **错误分类驱动** | 26 种错误原因 → `retryable / switch_credential / switch_provider / cooldown`。400/413 **绝不**轮换 Key；401 → UNHEALTHY；429 → 分钟限流指数冷却 / 额度耗尽长休；529 → deployment 冷却 + 故障转移。 |
| **Web 控制台** | 浏览器打开 `http://127.0.0.1:8317/ui`：总览 / 凭据池（冷却倒计时+一键解禁）/ 请求日志筛选+attempt 明细 / 用量统计 / 路由预览。单文件零依赖，数据仍走鉴权的 `/admin/*`。 |
| **有界重试** | 指数退避 + 抖动，`max_total_attempts` 是硬顶，结构上不可能死循环。 |
| **可解释路由** | 8 个能力维度加权打分 + 硬门槛，每个响应都带 `zk_ai.routing_reason` 与逐维度得分。 |
| **别名热更新** | `POST /admin/aliases` 运行时改线，客户端零感知。 |
| **流式** | 纯 `data:` + `data: [DONE]`，OpenAI SDK 直接可用；路由元数据走 SSE **注释行**，不污染 chunk 流。 |
| **零秘密泄漏** | Key 只从环境变量解析；日志、数据库、admin API 一律脱敏。 |
| **可审计** | SQLite/SQLAlchemy 2.x 落库：requests、request_attempts、usage_records、health_checks。 |

---

## 2. 快速开始

需要 **Python ≥ 3.12**（推荐用 [uv](https://github.com/astral-sh/uv) 管理）。

```bash
git clone <your-repo> zk-ai && cd zk-ai

# 依赖
uv venv && uv pip install -e ".[dev]"
#   或者： python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"

# 配置
cp config/config.example.yaml  config/config.yaml
cp config/providers.example.yaml config/providers.yaml
cp config/models.example.yaml   config/models.yaml
cp .env.example .env          # 填入真实 Key（.env 已被 .gitignore 忽略）

# 建表
python scripts/init_db.py

# 启动
uv run uvicorn app.main:app --host 0.0.0.0 --port 8317
scripts\start_gateway.cmd 9000   # 或用启动脚本，端口作参数传入
```

> `config.yaml` / `providers.yaml` / `models.yaml` 缺省时会自动回退到同目录的
> `*.example.yaml`，所以未配置也能启动（只是没有可用凭据）。

调用：

```bash
curl -s http://127.0.0.1:8317/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
        "model": "zk-coding",
        "messages": [{"role": "user", "content": "用 Python 写一个快速排序"}]
      }'
```

> Windows 上如果 `curl` 不在 PATH（Git Bash 环境常见），改用系统自带的 `curl.exe`，
> 本文所有 `curl` 示例都等价可用。
>
> 若本机设置了 `HTTP_PROXY` / `HTTPS_PROXY`，访问本地端口前先
> `export NO_PROXY=127.0.0.1,localhost`，否则请求会被代理拦走，拿到的是 502 而不是网关响应。

Python / OpenAI SDK 直接指向本网关即可，**不需要任何改动**：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="unused")

resp = client.chat.completions.create(
    model="zk-coding",                       # 稳定的别名，不是真实模型名
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

---

## 3. 离线端到端验证（零真实花费）

仓库自带一个**无依赖的 mock 上游**和故障注入能力，可以在不消耗任何真实配额的前提下
验证整条链路（路由 → 重试 → 轮换 → 冷却 → 故障转移 → 落库）：

```bash
# 终端 1：mock 上游（支持 fail-400/401/429/500/503/529、slow、broken 注入）
python scripts/mock_upstream.py --port 8099

# 终端 2：网关（config/providers.yaml 已指向 mock，3 个 Key）
MOCK_KEY_01=mock-key-1 MOCK_KEY_02=mock-key-2 MOCK_KEY_03=mock-key-3 \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8317

# 终端 3：17 项端到端断言
python scripts/smoke_test.py
```

实测输出：

```
[PASS] GET /health  -- 200 status=healthy db=True
[PASS] GET /v1/models  -- 16 entries, 11 aliases
[PASS] POST /v1/chat/completions (model=zk-coding)  -- 200 provider=mock model=mock-smart attempt=1
[PASS] routing metadata is attached  -- fallback_used=False
[PASS] capability router picked the coding model  -- resolved_model=mock-smart
[PASS] streaming returns SSE  -- 200 text/event-stream; charset=utf-8
[PASS] SSE shape (chunks + [DONE] + routing comment)  -- 13 chunks, 1 comment(s), done=True
[PASS] upstream 400 is mirrored to the client  -- 400 type=invalid_request_error
[PASS] 400 does NOT rotate keys (no credential penalised)  -- failure_count 0 -> 0
[PASS] 429 upstream exhausts keys and returns 429 (not 401)  -- 429 type=rate_limit_error
[PASS] throttled keys entered COOLDOWN (rotation happened)  -- cooldown=['mock-01','mock-02','mock-03']
[PASS] GET /admin/router/preview  -- 200 order=mock-smart strategy=capability
[PASS] GET /admin/stats  -- 200 keys=3 requests=5
[PASS] admin requires a valid token  -- 401
[PASS] GET /admin/health  -- 200 providers_available=0
[PASS] requests are persisted with the right statuses  -- 4 recent rows, statuses=['error', 'success']
[PASS] GET /admin/models  -- 200 models=5

17/17 checks passed
```

> 若机器上设置了 `HTTP_PROXY`，请让 localhost 绕过代理：
> `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost`。

---

## 3.6 在本地客户端里调用（ZCode / Claude Code / 任意 OpenAI SDK）

网关起来之后，客户端只需要一个 `base_url` 和一个 API Key（网关**不校验**客户端凭据，
填任意非空串即可——真正的密钥由网关按凭据池轮换）。

| 客户端 | 填什么 |
|---|---|
| OpenAI SDK / LangChain | `base_url="http://127.0.0.1:8317/v1"`，`api_key="任意非空"` |
| curl | `http://127.0.0.1:8317/v1/chat/completions` |
| ZCode（`D:\ZCode\ZCode.exe`） | 见下方一键脚本；`kind: openai-compatible` |
| Claude Code | ⚠ 见下方「关于 Anthropic 协议」 |

### 3.6.1 ZCode

ZCode 的 provider 存在 `~/.zcode/v2/config.json` 的 `provider` 键下，支持
`kind: "openai-compatible"` 的自定义端点，所以可以**直接指向 ZK-AI**：

```bash
# 预览将要写入的内容（不落盘）
python scripts/setup_zcode.py

# 实际写入（会自动备份 config.json）
python scripts/setup_zcode.py --apply

# 后悔了：只移除本脚本加的 provider，其余原样保留
python scripts/setup_zcode.py --remove
```

脚本会注册一个名为 **ZK-AI Gateway** 的 provider，暴露 8 个别名
（`zk-auto` / `zk-lite` / `zk-coding` / `zk-reasoning` / `zk-vision` /
`zk-long` / `zk-fast` / `zk-cheap`）。写完**重启 ZCode**，在模型选择器里选
`ZK-AI Gateway → zk-*` 即可。

这样做的好处：ZCode 里切模型只改别名，背后是商汤 / NVIDIA / 魔搭哪一家、
哪把 Key，ZCode 完全不用知道；某家挂了网关自动转移。

### 3.6.2 关于 Anthropic 协议（Claude Code / Cline 等）

ZK-AI 目前**只暴露 OpenAI 协议**（`/v1/chat/completions`、`/v1/responses`），
**没有** `/v1/messages`。而 Claude Code（以及任何 `ANTHROPIC_BASE_URL` 型客户端）
走的是 Anthropic Messages 协议，因此**不能直接**把
`ANTHROPIC_BASE_URL` 指向 ZK-AI——会得到 `404`。

可选做法：

1. **直接用原生的 `openai-compatible` 客户端**（ZCode / OpenAI SDK / Cherry Studio 等），
   不需要 Anthropic 协议；
2. 如果一定要给 Claude Code 用，需要一个 Anthropic→OpenAI 的转换层。
   最稳的是用 `claude-code-router` 这类成熟项目，把它的后端指向 ZK-AI；
3. 或者在 ZK-AI 里实现 `/v1/messages`（当前未实现，见 §21 后续计划）。

> 注：`~/.claude/settings.json` 里若已设 `ANTHROPIC_BASE_URL` 指向别的本地代理，
> 那个代理必须先起起来，否则 Claude Code 会直接连不上。

---

## 3.5 接入真实供应商（商汤日日新 / NVIDIA / Kimi）

仓库里已带两份**可直接复制**的真实配置模板：

```bash
cp config/providers.real.example.yaml config/providers.yaml
cp config/models.real.example.yaml    config/models.yaml
cp .env.example .env                  # 填入真实密钥
```

`.env` 是唯一放密钥的地方，启动时会被读入进程环境（见 §18 缺陷 6）。
`config/*.yaml` 与 `.env` 都已被 git 忽略。

### 3.5.1 商汤日日新 SenseNova —— 一把 Key 调 Kimi K3

| 项 | 值 |
|---|---|
| Base URL | `https://token.sensenova.cn/v1` |
| 协议 | OpenAI 兼容（也可用 Anthropic 兼容端点 `POST /v1/messages`） |
| 鉴权 | `Authorization: Bearer sk-xxx` |
| 申请 | <https://platform.sensenova.cn> → 控制台 → API Keys |
| 计费 | 公测期免费，但走**积分制**：滚动 5 小时 + 周额度，双池（通用池 / Flash-Lite 专属池） |

可用 model id（**都填在 `deployments[].model`，不是 `provider` 上**）：

| model id | 说明 |
|---|---|
| `kimi-k3` | 月之暗面旗舰，2.8T 参数、原生视觉、1M 上下文 ← **本方案的主角** |
| `deepseek-v4-flash` | 日常主力，1M 上下文，速度快 |
| `deepseek-v4-pro` | 旗舰推理，1M 上下文 |
| `glm-5.2` | 智谱旗舰，长程 Coding，1M 上下文 |
| `sensenova-6.8-flash-lite` | 商汤自家，看图 / OCR / 图表解读，256K |
| `sensenova-u1-fast` / `sensenova-u1.5-lite` | 生图专用，走 `/v1/images/generations`，**不是** chat 接口 |

> **`kimi-k3` 目前不在官方文档的模型清单里，但接口能正常识别并调用。**
> 属于平台提前开放的模型，随时可能调整——所以下面把 Kimi 官方平台配成第二部署做兜底。

```yaml
# config/providers.yaml
providers:
  - id: sensenova
    type: openai_compatible      # 关键：协议族是 OpenAI 兼容，不是「商汤」专属
    base_url: https://token.sensenova.cn/v1
    timeout: 300                 # K3 开思考模式会跑很久，别用默认 120
    credentials:
      - id: sensenova-01
        env: SENSENOVA_API_KEY   # 变量「名」，不要写成 ${SENSENOVA_API_KEY}
        priority: 100
```

### 3.5.2 NVIDIA NIM

| 项 | 值 |
|---|---|
| Base URL | `https://integrate.api.nvidia.com/v1` |
| 鉴权 | `Authorization: Bearer nvapi-xxx` |
| 申请 | <https://build.nvidia.com> → 任一模型页 → Get API Key |
| 注意 | 调用名**必须带厂商前缀**，如 `nvidia/llama-3.3-nemotron-super-49b-v1` |

```yaml
  - id: nvidia
    type: openai_compatible
    base_url: https://integrate.api.nvidia.com/v1
    credentials:
      - id: nvidia-01
        env: NVIDIA_API_KEY
```

### 3.5.3 Kimi 官方平台（可选，作为兜底）

商汤的 `kimi-k3` 是免费的，但没有 SLA 且 429 频繁。想要稳定就在
<https://platform.moonshot.cn> 开一个 Key，配成同一模型的**第二个部署**：
两个部署共用 `kimi-k3` 这个对外名字，商汤限流时调度器会自动切到官方。

```yaml
# config/models.yaml
models:
  - id: kimi-k3
    context_window: 1000000
    capabilities: { coding: 9.5, reasoning: 9.5, tool_use: 9.0, vision: 9.5, long_context: 10.0 }
    deployments:
      - id: kimi-k3-sensenova          # 部署 1：免费，优先
        provider_id: sensenova
        model: kimi-k3
        priority: 120
      - id: kimi-k3-moonshot           # 部署 2：付费，兜底
        provider_id: moonshot
        model: kimi-k3
        priority: 70                   # 数字小 = 排在后面
```

### 3.5.4 三层概念别混

| 层 | 写在哪 | 表示什么 | 例子 |
|---|---|---|---|
| Provider | `providers.yaml` | 一个上游端点 + 一组 Key | `sensenova` 指向 `token.sensenova.cn` |
| Deployment | `models.yaml` 的 `deployments[]` | 「哪个 provider 上的哪个上游模型」= **故障转移的最小单位** | `kimi-k3` 跑在 `sensenova` 上 |
| Model | `models.yaml` 的 `models[].id` | 你对外暴露、可被路由筛选的模型 | `kimi-k3` |
| Alias | `models.yaml` 的 `aliases[]` | 稳定入口，客户端只认它 | `zk-coding` |

`kimi-k3` / `high` 这类**模型级参数**（`context_window`、`capabilities`）写在模型上；
**部署级参数**（`priority`、`max_output_tokens`、`input_cost_per_mtok`、
`options`、`supported_params`）写在 deployment 上。写错层会直接报
`Extra inputs are not permitted`。

`max_output_tokens` 是**部署级**字段：

```yaml
    deployments:
      - id: kimi-k3-sensenova
        provider_id: sensenova
        model: kimi-k3
        max_output_tokens: 32768   # ✅ 在这里
```

### 3.5.5 启动与验证

```bash
# 填好 .env 后
uv run python scripts/init_db.py
uv run uvicorn app.main:app --host 127.0.0.1 --port 8317 --log-level info
```

启动日志会打印 provider / model / alias / credential 数量和**配置告警**，先看这里：

```
zkai.container: ZK-AI ready: 3 provider(s), 5 model(s), 7 alias(es)
```

然后：

```bash
curl.exe -s http://127.0.0.1:8317/health                     # 看 credentials.usable 是否 > 0
curl.exe -s "http://127.0.0.1:8317/admin/credentials" -H "X-Admin-Token: $ZKAI_ADMIN_TOKEN"
curl.exe -s "http://127.0.0.1:8317/admin/router/preview?model=zk-coding&prompt=write%20a%20function"
```

`/health` 的 `credentials.usable` 为 0 基本只有两个原因：变量名写错（看启动告警），
或者 Key 还没填进 `.env`。

调用 Kimi K3（用别名，客户端不用知道后端是谁）：

```bash
curl.exe -s -X POST http://127.0.0.1:8317/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"zk-coding","messages":[{"role":"user","content":"写一个快速排序"}]}'
```

响应里的 `zk_ai` 字段会告诉你**实际**命中了哪个 provider / 模型 / Key：

```json
"zk_ai": {
  "requested_model": "zk-coding",
  "alias": "zk-coding",
  "resolved_model": "kimi-k3",
  "provider": "sensenova",
  "credential_id": "sensenova-01",
  "attempt": 1,
  "fallback_used": false,
  "routing_reason": "... strategy=capability | order=[...] ...",
  "capability_scores": { "coding": 0.46, ... }
}
```

### 3.5.6 切换模型的三种方式

| 方式 | 怎么做 | 适用 |
|---|---|---|
| 选一个别名 | `"model": "zk-coding"` | 最常用，后端模型变了客户端不用改 |
| 直接点名模型 | `"model": "kimi-k3"` | 临时指定 |
| 改别名指向 | `PUT /admin/aliases/zk-coding` 热更新 | 不重启换后端 |

### 3.5.7 关于 429

商汤免费额度是「滚动 5 小时 + 周」双周期，跑推理模型几分钟就能烧完一个窗口，
撞 429 属正常。网关会按 §11 的决策矩阵自动处理：**换同家下一把 Key → 全部冷却则
转移到下一个部署**。所以最有效的三件事是：

1. **多用几个账号**——额度按**账号**算，不是按 Key 算，见下方说明；
2. 把 `kimi-k3` 的第二部署指到 Kimi 官方（免费窗口用尽时自动花钱兜底）；
3. 非推理类请求走 `zk-fast` / `zk-cheap`（DeepSeek V4 Flash、6.8 Flash Lite），
   把贵的额度留给 `zk-reasoning`。

> **额度是按账号算的，不是按 Key 算的。**
> 同账号的多把 Key **共享同一份配额**，撞限流会一起撞；只有**跨账号**的 Key
> 才是互相独立的额度。所以「10 把同账号的 Key」并不比「1 把」多出多少余量。
>
> 网关对此的处理是**逐把独立冷却**：某把 Key 429 只冷却它自己，调度器立刻
> 换下一把。因此跨账号的额度会**自动叠加**，不需要额外配置。
> 唯一要确认的是 `config.yaml` 的 `retry.max_credentials_per_deployment`
> 不小于实际账号数（否则试到一半就停了）——本仓库配的是 `6`。
>
> 本项目的商汤 6 把 Key 分属 **5 个账号**（01+02 同一账号，03/04/05/06 各一个），
> 所以实际有 5 份独立额度。`config/providers.yaml` 里用 `tags: ["account-x"]`
> 标出归属（**只是给人看的标签，不参与调度**）。
>
> 实测效果：`zk-k3` 在 try 到第 4 把（跨 4 个账号）时才拿到额度并成功返回。

> 注意：商汤与 Kimi 官方对 `kimi-k3` 的**计费方式不同**（积分 vs 按量付费）。
> 免费窗口里看起来「不要钱」，切到兜底部署就会真实扣费——`fallback_used: true`
> 就是发生转移的信号，`/admin/stats` 里能复盘每次转移。

---

## 4. 架构分层

| 层 | 模块 | 职责 | 依赖方向 |
|---|---|---|---|
| 接口层 | `app/api/` | FastAPI 路由、鉴权、错误信封 | → services |
| 编排层 | `app/services/` | 请求生命周期、落库、健康检查、统计 | → routing / database |
| 路由层 | `app/routing/` | 别名解析、能力打分、候选排序、**执行**（Scheduler） | → providers / credentials |
| 凭据层 | `app/credentials/` | Key 池、状态机、轮换、冷却 | → models |
| 供应商层 | `app/providers/` | 各协议适配器（payload 构造 / 响应归一） | → core |
| 重试层 | `app/retry/` | 错误分类、退避计算、重试策略 | 无外部依赖 |
| 持久化 | `app/database/` | SQLAlchemy 2.x async 模型与仓储 | → core |
| 基础设施 | `app/core/` | 配置、错误类型、日志、脱敏、DI 容器 | 最底层 |

**关键设计**：Router 只做规划、**不做 I/O**；执行全部交给 Scheduler。因此路由逻辑
可以纯单元测试，不需要任何网络。

---

## 5. 目录结构

```
ZK-AI/
├── app/
│   ├── api/              # FastAPI 路由：chat / responses / models / health / admin
│   ├── core/             # config, container(DI), errors, logging, security
│   ├── credentials/      # cooldown, health(状态机), pool(Key 池), rotation
│   ├── database/         # db, models(ORM), repository
│   ├── models/           # Pydantic 领域模型: provider / credential / request / response
│   ├── providers/        # base, openai, anthropic, gemini, openrouter, ollama, factory
│   ├── retry/            # backoff, classifier(错误分类), policy
│   ├── routing/          # aliases, capability, router, scheduler, strategy
│   ├── services/         # health_service, model_service, request_service, usage_service
│   └── main.py           # create_app 工厂
├── config/               # *.example.yaml（可提交）+ *.yaml（本地，已忽略）
│                         # 另有 providers.real.example.yaml / models.real.example.yaml
│                         # —— 商汤日日新 + NVIDIA + Kimi 的可复制模板，见 §3.5
├── scripts/              # init_db, health_check, benchmark, mock_upstream, smoke_test, setup_zcode, port_guard
├── tests/                # conftest + 8 个测试模块，228 个用例，全部 Mock
├── 使用手册.md            # ⭐ 面向使用者：三步上手、改配置、常见问题（先看这个）
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml        # 依赖 + ruff/mypy/pytest 配置
└── LICENSE               # MIT
```

规模：`app/` 52 个文件约 10,270 行，`tests/` 约 2,720 行，`scripts/` 约 750 行。

---

## 6. 配置

三层，**环境变量优先级最高**：

1. `.env`（本地秘密）
2. `config/config.yaml`（应用设置）、`config/providers.yaml`、`config/models.yaml`
3. 代码默认值

任意 YAML 值都支持 `${VAR}` / `${VAR:-default}` 插值。

### 6.1 `config.yaml` 常用项

```yaml
app:
  host: 0.0.0.0
  port: 8317
logging:
  level: INFO
  json: false                  # true = 每行一个 JSON 对象
database:
  url: ${ZKAI_DATABASE_URL:-sqlite+aiosqlite:///./data/zkai.db}
admin:
  enabled: true
  token: ${ZKAI_ADMIN_TOKEN:-}  # 空 = 不鉴权（仅限本地开发）
request:
  timeout: 120
  default_max_tokens: 1024
credential_rotation: priority   # priority | round_robin | weighted | least_failures | fastest
retry:
  max_retries_per_credential: 2
  max_credentials_per_deployment: 3
  max_deployments: 3
  max_total_attempts: 8         # 单请求硬顶
  stream_open_retries: 1
  base_delay: 0.5
  max_delay: 8.0
  multiplier: 2.0
  jitter: 0.3                   # ±30%
  jitter_mode: full             # full | equal | none
  respect_retry_after: true
health_check:
  mode: startup                 # manual | startup | scheduled | off
  interval_seconds: 900
  timeout: 20
cooldown:                       # 凭据冷却（秒）
  rate_limit_base: 60           # 分钟级 429：首次冷却，指数 ×2
  rate_limit_max: 900
  quota_cooldown: 1800          # 额度耗尽类 429：平铺长休，不指数放大
  quota_cooldown_max: 14400
  server_error_cooldown: 15
  deployment_cooldown: 30
  jitter: 0.15                  # ±15%，避免全池锁步复活
security:
  allow_inline_secrets: false   # true 允许 providers.yaml 内联密钥（仅开发）
```

### 6.2 环境变量

`ZKAI_HOST` `ZKAI_PORT` `ZKAI_LOG_LEVEL` `ZKAI_LOG_JSON` `ZKAI_DATABASE_URL`
`ZKAI_ADMIN_TOKEN` `ZKAI_CONFIG_DIR` `ZKAI_DATA_DIR` `ZKAI_REQUEST_TIMEOUT`
`ZKAI_HEALTH_CHECK_MODE` `ZKAI_HEALTH_CHECK_INTERVAL` `ZKAI_ALLOW_INLINE_SECRETS`
`ZKAI_CREDENTIAL_ROTATION` `ZKAI_ENVIRONMENT`
`ZKAI_RATE_LIMIT_BASE` `ZKAI_RATE_LIMIT_MAX` `ZKAI_QUOTA_COOLDOWN` `ZKAI_QUOTA_COOLDOWN_MAX`

### 6.3 `providers.yaml`

```yaml
providers:
  - id: openai
    type: openai                      # openai | anthropic | gemini | openrouter | ollama | openai_compatible
    base_url: ${OPENAI_BASE_URL:-https://api.openai.com/v1}
    timeout: 120
    max_retries: 2
    credentials:
      - id: openai-01                 # 出现在日志/统计/admin API
        env: ${OPENAI_KEY_01_ENV:-OPENAI_KEY_01}
        priority: 100                 # 越大越优先
        weight: 1.0                   # weighted 轮换用
        max_consecutive_failures: 3   # 连续 5xx/超时多少次后转入冷却
```

> **秘密只写环境变量名，不写值。** `allow_inline_secrets: false` 时内联 `value:` 会被拒绝。
>
> **`env:` 里放的是「变量名」，不是「引用」。** `env: ${OPENAI_KEY_01_ENV:-OPENAI_KEY_01}`
> 的含义是「用 `OPENAI_KEY_01_ENV` 指向**另一个变量名**，未设置则用 `OPENAI_KEY_01`」，
> 用来在不改 YAML 的前提下把某个槽位改指到别的变量。
>
> 写成 `env: ${OPENAI_KEY_01}` 是**错的**：YAML 插值会把密钥本身展开进来，
> `env` 于是变成密钥值而非变量名。此时该 Key 状态仍是 `healthy`、不会有「变量未设置」
> 的报错，但每个请求都会 401；若密钥恰好只含标识符字符，还会被当成变量名写进日志和
> 数据库。启动时会打印 `... is not a valid environment variable name` 告警，
> 同时出现在 `/admin/health` 的 `config.warnings` 中。正确写法：
>
> ```yaml
>       - id: openai-01
>         env: ${OPENAI_KEY_01_ENV:-OPENAI_KEY_01}   # ✅ 变量名
>         # env: ${OPENAI_KEY_01}                    # ❌ 会展开成密钥本身
>         # env_var: OPENAI_KEY_01                   # ✅ 等价写法，最直白
> ```

### 6.4 `models.yaml`

```yaml
models:
  - id: gpt-5
    context_window: 400000
    capabilities: { coding: 9.5, reasoning: 9.5, vision: 8.5, speed: 6.0, cost: 4.0 }
    deployments:
      - id: gpt-5-openai             # 同一模型的主/备部署
        provider_id: openai
        model: gpt-5                 # 上游真实模型名
        priority: 120
        input_cost_per_mtok: 1.25
        options: { max_tokens_param: max_completion_tokens }
      - id: gpt-5-openrouter
        provider_id: openrouter
        model: openai/gpt-5
        priority: 80
```

---

## 7. 供应商与已验证的协议细节

所有适配器都对照官方文档核过，没有凭记忆臆造参数：

| Provider | 认证 | 端点 | 关键差异 |
|---|---|---|---|
| **OpenAI** | `Authorization: Bearer` | `POST /chat/completions` | 推理模型拒绝 `max_tokens`，部署级 `options.max_tokens_param` 可切到 `max_completion_tokens` |
| **Anthropic** | `x-api-key` + `anthropic-version: 2023-06-01` | `POST /messages` | `system` 是顶层字段（不在 messages 里）；工具是 `tool_use` / `tool_result` 内容块；流式走 `content_block_delta` |
| **Gemini** | `x-goog-api-key` 头（刻意不用 `?key=`，避免密钥进代理日志） | `POST /models/{model}:generateContent` / `:streamGenerateContent?alt=sse` | `contents[].parts[]`；工具是 `functionCall` / `functionResponse`；`generationConfig` |
| **OpenRouter** | `Authorization: Bearer` | 同 OpenAI | 额外带 `HTTP-Referer` / `X-Title` 归因头 |
| **Ollama** | 无需密钥 | `POST /api/chat` | **NDJSON** 流（每行一个 JSON），不是 SSE；本地默认关闭 |
| **`openai_compatible`** | 跟随上游 | 同 OpenAI | 任意 OpenAI 兼容端点的通用适配器 |

### 已用 `openai_compatible` 接过的真实端点

| 服务 | Base URL | 调用名/注意 | 计费 |
|---|---|---|---|
| 商汤日日新 SenseNova | `https://token.sensenova.cn/v1` | `kimi-k3` / `deepseek-v4-flash` / `glm-5.2` / `sensenova-6.8-flash-lite`；另有 Anthropic 兼容端点 `POST /v1/messages` | 公测免费，积分制双周期，429 常见 |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | 必须带厂商前缀，如 `nvidia/llama-3.3-nemotron-super-49b-v1` | build.nvidia.com 免费 credit |
| Moonshot / Kimi 官方 | `https://api.moonshot.cn/v1` | `kimi-k3`、`kimi-k2.7-code` | 按量付费 |

加自定义端点：把 `type` 设为 `openai_compatible`，或实现
`app.providers.base.ProviderAdapter` 后用 `register_adapter(type, cls)` 注册。

---

## 8. 模型别名

客户端只认别名。别名可以在 `config/models.yaml` 里声明，也可以运行时通过 admin API 改线。

> **⚠️ 模型名只在一个地方配：`config/models.yaml`。**
> `.env` / `.env.example` **只放密钥和服务地址**。往里写 `XXX_MODEL=...` 完全无效——
> 程序不会读它，也不会报错，你会以为改了其实没改。真正生效的两处是：
>
> | 想改什么 | 改哪里 |
> |---|---|
> | 某模型走哪个上游、叫什么名字 | `models[].deployments[].model` |
> | 别名指向哪些模型、优先级如何 | `aliases[].targets`（**顺序即优先级**） |
> | 用哪把 Key 调 | `providers[].credentials[].env`（填变量**名**，不是密钥本身） |
>
> 改完重启网关即可生效（当前配置：4 provider / 7 model / 10 deployment / 10 alias）。

内置兜底别名（`models.yaml` 未定义别名时自动生成）：

| 别名 | 策略 | 说明 |
|---|---|---|
| `zk-auto` | capability | 默认入口，按请求内容自动选 |
| `zk-coding` | capability | 代码生成 / 重构 |
| `zk-reasoning` | capability | 多步推理、分析 |
| `zk-fast` | speed | 低延迟优先 |
| `zk-cheap` | cost | 成本优先 |
| `zk-long` | capability | 超长上下文 |
| `zk-vision` | capability | 图像理解（`requires.vision: 1`） |
| `zk-local` | cost | 本地 / 自托管 |
| `zk-erpnext` | capability | 业务场景定制 |

本仓库 `config/models.yaml` 里实际声明的 10 个别名（覆盖上面的默认值）：

| 别名 | 策略 | 首位模型 | 说明 |
|---|---|---|---|
| `zk-auto` | capability | `kimi-k3` | 默认入口：K3 优先，挂了自动落到 GLM / DeepSeek / 6.8 Lite |
| `zk-k3` | priority | `kimi-k3` | **只用 K3，不兜底**——K3 异常时直接报错，便于看清真实状态 |
| `zk-lite` | priority | `sensenova-6.8-flash-lite` | 固定用商汤轻量模型，**完全不碰 K3** |
| `zk-coding` | capability | `kimi-k3` | 编程，门槛 `coding ≥ 7.0` |
| `zk-reasoning` | capability | `kimi-k3` | 深度推理，门槛 `reasoning ≥ 7.5` |
| `zk-vision` | capability | `kimi-k3` | 看图 / OCR，门槛 `vision ≥ 8.0` |
| `zk-long` | capability | `kimi-k3` | >256K 长上下文，门槛 `long_context ≥ 9.0` |
| `zk-fast` | speed | `sensenova-6.8-flash-lite` | 低延迟（不含 K3） |
| `zk-cheap` | cost | `sensenova-6.8-flash-lite` | 便宜优先，不消耗 K3 额度 |
| `zk-local` | priority | `nemotron-3-super` | 免费额度兜底：NVIDIA + 魔搭 |

`kimi-k3` 自身有 **3 个部署**（按 priority 降序尝试）：

| priority | provider | 上游模型名 | 备注 |
|---|---|---|---|
| 130 | `sensenova` | `kimi-k3` | 公测期免费；实测常报 `token plan entitlement exhausted` |
| 120 | `moonshot` | `kimi-k3` | 官方按量付费，最稳；key 未填时该部署自动跳过 |
| 40 | `nvidia` | `moonshotai/kimi-k3` | 免费预览；实测常报 `429 Too Many Requests` |

因此想稳定用 K3，**给 Moonshot 官方充值**是唯一可靠路径；否则 `zk-k3` 大概率返回 429，
而 `zk-auto` 会在耗尽重试后自动兜底到其它模型（请求仍然成功，只是不是 K3 跑的）。

别名支持**嵌套**（`zk-coding` → `[a, b]`，`a` 本身又是别名）与**环检测**。

运行时改线：

```bash
curl -s -X POST http://127.0.0.1:8317/admin/aliases \
  -H 'X-Admin-Token: <token>' -H 'content-type: application/json' \
  -d '{"name":"zk-coding","targets":["claude-sonnet","gpt-5"],"strategy":"capability"}'
```

实测：改线前 `zk-coding` 的候选是 `[mock-smart, mock-general]`，改线后立刻变成
`[mock-cheap]`，**客户端代码零改动**。

### 别名策略

| 策略 | 排序依据 |
|---|---|
| `capability` | 能力总分（默认） |
| `priority` | 别名的 targets 顺序优先，其次部署 priority |
| `cost` | cost 维度得分高者优先（10 = 最便宜） |
| `speed` | speed 维度得分高者优先 |
| `round_robin` | 带游标的轮询 |
| `weighted` | 按 priority 分带内加权随机 |

---

## 9. 能力路由

8 个维度（0–10 分）：`coding`、`reasoning`、`tool_use`、`vision`、`long_context`、
`structured_output`、`speed`、`cost`。

**第一条规则：硬门槛必须同时支配打分。** 任何进入 `minimums` 的维度，权重至少为
`8.0`（`capability.GATE_WEIGHT`）——否则门槛只起过滤作用，幸存者仍会被无关维度排序。

**需求推断**（从请求体读）：

| 信号 | 效果 |
|---|---|
| 消息里含图片 | `vision >= 1`（硬门槛） |
| 有 `tools` | `tool_use >= 1`（硬门槛） |
| 有 `response_format` | `structured_output >= 1`（硬门槛） |
| 命中代码关键词 / 代码块 | `coding` 权重 → 5.0 |
| 命中推理关键词（含中文「为什么/推理/证明/根因」） | `reasoning` 权重 → 5.0 |
| 估算输入 ≥ 24k tokens | `long_context` 权重 → 5.0 |
| 估算输入 ≥ 100k tokens | `long_context >= 1`（硬门槛） |

**优先级**：别名 `requires`（硬门槛）> 别名 `weights`（显式权重）> 请求推断。

**另有两类硬门槛**：上下文窗口不足（留 10% 余量给分词器差异）、部署被禁用。

`/admin/router/preview` 会完整回放这个决策过程：

```json
{
  "strategy": "capability",
  "alias": "zk-coding",
  "plan": [
    {"model": "mock-smart",   "score": 0.949647, "eligible": true},
    {"model": "mock-general", "score": 0.818098, "eligible": true}
  ],
  "minimums": {"tool_use": 1.0}
}
```

每个响应的 `zk_ai.capability_scores` 给出**逐维度贡献值**，可以解释「为什么是它」。

---

## 10. Key 池与状态机

```
         ┌──────────┐  429 / 连续 5xx 到阈值   ┌───────────┐
         │ HEALTHY  │ ───────────────────────▶ │ COOLDOWN  │
         └────┬─────┘                          └─────┬─────┘
              │ 401 / 403                            │ 冷却到期（惰性恢复）
              ▼                                      │
        ┌────────────┐                               │
        │ UNHEALTHY  │ ◀── 只能由健康检查/运维恢复    │
        └────────────┘                               │
              ▲                                      │
              │                                      ▼
        ┌────────────┐                         (回到 HEALTHY)
        │  DISABLED  │  ← 运维手动 / 环境变量缺失
        └────────────┘
```

| 失败类型 | 状态迁移 |
|---|---|
| 401 认证失败 | → `UNHEALTHY`（需健康检查或运维恢复） |
| 403 无权限 | → `UNHEALTHY` |
| 429 分钟级限流（tpm/rpm） | → `COOLDOWN`，尊重 `Retry-After`，连续限流指数增长（60→900s） |
| 429 额度耗尽（`entitlement exhausted` 等） | → `COOLDOWN` **固定长休**（`quota_cooldown` 默认 30 分钟），不指数放大 |
| 5xx / 超时 / 连接错误 | 只计数；**连续**达到 `max_consecutive_failures` 才 → `COOLDOWN` |
| 400 / 404 / 413 / 409 / 422 | **不计数、不迁移**（调用方的错，见 §11） |
| 网关内部错误（INTERNAL：解析失败等） | **不计数、不迁移**——自家 bug 不惩罚健康的 Key |
| 成功 | → `HEALTHY`，计数清零，更新延迟 EMA |
| 冷却到期 | 下次选择时惰性 → `HEALTHY`（时长带 ±15% 抖动错峰复活；复活前已静默满 `rate_limit_max` 的，限流阶梯归零——「等待换不来恢复」的修复，见 §18 缺陷 10） |

> **529 刻意不冷却 Key**：它是模型/供应商级别的过载，应冷却 deployment 并故障转移，
> 而不是把一把好 Key 停掉。冷却时长见 `config.yaml` 的 `cooldown:` 段（§6.1）。
>
> **整池冷却时**，请求直接返回 503 `no_available_credential` 并携带 `Retry-After`
> 头（= 池中最早复活 Key 的剩余秒数），客户端可礼貌退避而非空转重试。

轮换策略：`priority`（默认，按优先级）、`round_robin`、`weighted`、
`least_failures`（连续失败数 → 成功率 → 优先级）、`fastest`（延迟 EMA）。

并发：整个池由 `threading.RLock` 保护，`pick()` 的选择 + 标记在用是原子的，
`in_flight` 计数让负载可见。

### 会话亲和（sticky session）

供应商的前缀缓存（prompt cache）**按账号计**：同一通对话若每轮被轮换到不同 Key，
等于反复放弃热缓存、让模型**重读并重算整段历史**（token 与延迟都会翻倍，思考模型
还会"重新思考"）。开启 `credential_affinity` 后，网关把每通对话钉到**上一步成功
的那把 Key** 上：

- 会话键优先取客户端传入的 `session_id`（次选 `user`），否则用「模型 + 首条消息」
  的哈希指纹——**与对话长度无关**，保证整通对话稳定映射到同一把 Key。
- 亲和是**偏好不是锁**：被钉的 Key 一旦冷却/故障，立即回退常规轮换；下一次成功
  自动把锚点挪到新 Key（即「换池子也有连续性」，等价于在 CC Switch 上换 key）。
- 滑动 TTL：每次成功续命 `ttl_seconds`，空闲超时才解除；`max_sessions` 做 LRU 上限。
- 控制台「凭据池」用 📌N 显示每把 Key 当前钉住的会话数。

> 注意：token 数**不会**因换 Key 而"累积"——对话历史本来就要每轮随请求重发，这是
> OpenAI/Anthropic 协议的无状态设计。亲和省的是**前缀缓存重算**，不是历史本身。

---

## 11. 错误分类决策矩阵

`app/retry/classifier.py` 是重试/轮换/转移的**唯一真相来源**：

| 状态 / 原因 | retryable | switch_credential | switch_provider | 冷却范围 |
|---|:---:|:---:|:---:|---|
| 400 请求错误 | ✗ | ✗ | ✗ | — |
| 401 Key 无效 | ✗ | **✓** | ✗ | credential |
| 403 无权限 | ✗ | **✓** | ✗ | credential |
| 404 模型不存在 | ✗ | ✗ | **✓** | — |
| 408 请求超时 | ✓ | ✗ | ✗ | — |
| 409 冲突 | ✗ | ✗ | ✗ | — |
| 413 上下文过长 | ✗ | ✗ | ✗ | — |
| 422 无法处理 | ✗ | ✗ | ✗ | — |
| 429 限流 | ✗ | **✓** | ✗ | credential |
| 500 上游内部错误 | ✓ | **✓** | ✓ | — |
| 502 网关错误 | ✓ | **✓** | ✓ | — |
| 503 服务不可用 | ✓ | ✗ | **✓** | deployment |
| 504 网关超时 | ✓ | ✗ | **✓** | deployment |
| 529 过载（Anthropic） | ✓ | ✗ | **✓** | deployment |
| timeout（传输层） | ✓ | ✗ | **✓** | deployment |
| connection_error | ✓ | **✓** | ✓ | credential |
| unknown | ✗ | ✗ | ✓ | — |

`switch_credential` 的含义是：**先把该 Key 的重试预算用尽，再换同一部署下的兄弟 Key，
最后才故障转移到下一个部署**。

**返回给客户端的状态码规则**（`Scheduler._client_status_for`）：调用方错误与瞬时
错误**照映上游**；限流 → 429；凭据/可用性问题 → **503（绝不 401**，否则会误导客户端
以为自己的 Key 无效）。

---

## 12. 重试、退避与故障转移

三层嵌套循环：

```
for deployment in plan.candidates:              # 故障转移层（max_deployments）
    for credential in pool.candidates(provider):  # Key 轮换层（max_credentials_per_deployment）
        while retries <= max_retries_per_credential:  # 退避层
            attempt()
```

**有界性保证**：`max_total_attempts` 是全局硬顶（默认 8），循环入口处检查，结构上
不可能无限重试。

**退避**：`base_delay * multiplier^(n-1)`，上限 `max_delay`，叠加抖动
（`full` = ±jitter，`equal` = [delay*(1-j), delay]，`none` = 确定性）。
`respect_retry_after: true` 时尊重上游 `Retry-After`（上限放宽到 `max_delay * 4`）。

**流式重试只发生在第一个 chunk 到达之前**；已经开始输出后无法"撤回"，此时错误以
SSE `error` 事件形式下发。

**预检失败返回真实 HTTP 状态码**：若一个字节都没发出，网关返回 JSON 错误 + 正确状态码
（如 400/503），而不是 200 + 空流——否则客户端会把失败当成成功。

---

## 13. API 参考

### 13.1 OpenAI 兼容面

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/chat/completions` | 主入口，支持 `stream` |
| `POST` | `/v1/responses` | Responses API 子集（内部转译为 chat） |
| `GET` | `/v1/models` | 模型 + 别名清单 |
| `GET` | `/v1/models/{id}` | 单个模型/别名详情 |
| `GET` | `/health` | 存活与池状态 |
| `GET` | `/docs` | Swagger UI |

**响应头**（每次请求都带）：

```
X-ZKAI-Request-Id: chatcmpl-...
X-ZKAI-Provider: mock
X-ZKAI-Credential: mock-01
X-ZKAI-Model: mock-smart
X-ZKAI-Attempt: 1
X-ZKAI-Fallback: true          # 仅在发生故障转移时出现
```

**响应体**额外带 `zk_ai` 路由元数据：

```json
{
  "id": "mock-1789032138347",
  "object": "chat.completion",
  "model": "zk-coding",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 5, "completion_tokens": 20, "total_tokens": 25},
  "zk_ai": {
    "request_id": "chatcmpl-d8bb6aca298b4ea18d25aa04",
    "requested_model": "zk-coding",
    "alias": "zk-coding",
    "resolved_model": "mock-smart",
    "provider": "mock",
    "credential_id": "mock-01",
    "attempt": 1,
    "routing_reason": "requested=zk-coding | alias=zk-coding | strategy=capability | order=['mock-smart-dep','mock-general-dep'] | hints: code-like content detected -> coding weight raised",
    "capability_scores": {"coding": 0.2914, "reasoning": 0.1202, "tool_use": 0.4417, "priority": 0.0475},
    "latency_ms": 11.29,
    "finish_reason": "stop",
    "fallback_used": false,
    "cooldowns": {}
  }
}
```

### 13.2 运维面 `/admin/*` 与 Web 控制台

**浏览器打开 `http://127.0.0.1:8317/ui`** 即可使用内置 Web 控制台（单文件、零依赖、
无需构建）：五个视图——总览（供应商可用性 + 池成功率 + 一键健康检查/热重载/清冷却）、
凭据池（状态徽章 + 冷却倒计时 + 逐把启用/禁用）、请求记录（多条件筛选 + 分页 +
点击行看每次 attempt 的上游错误原文）、用量统计（按天/供应商/模型/凭据）、
模型与别名（含路由预览）。页面壳公开、不含数据，所有数据仍走下面鉴权的 `/admin/*`。

`/admin/*` 除 `/docs` 外全部需要 `X-Admin-Token` 或 `Authorization: Bearer`（仅在配置了
`admin.token` 时；`admin.enabled: false` 会整体返回 404）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/ui` | Web 管理控制台（页面壳公开，数据接口仍鉴权） |
| `GET` | `/admin/providers` | 供应商及其凭据概览 |
| `GET` | `/admin/models` | 模型、部署、能力分 |
| `GET` | `/admin/credentials` | Key 池快照（脱敏）+ 统计 |
| `POST` | `/admin/credentials/{id}/enable` | 启用 Key |
| `POST` | `/admin/credentials/{id}/disable` | 禁用 Key |
| `POST` | `/admin/credentials/cooldowns/clear` | 清空冷却（立即重试） |
| `GET` | `/admin/aliases` | 别名表 + 解析结果 |
| `POST` | `/admin/aliases` | 创建/替换别名（热更新） |
| `DELETE` | `/admin/aliases/{name}` | 删除别名 |
| `GET` | `/admin/router/preview` | **解释**路由决策，不执行 |
| `GET` | `/admin/health` | 详细健康报告 + 探测历史 |
| `POST` | `/admin/health/check` | 立即探测（只调 `GET /models`，不消耗推理配额） |
| `GET` | `/admin/stats` | 用量、成本、错误率、最近请求 |
| `GET` | `/admin/requests` | 请求日志：状态/供应商/别名/模型/错误 筛选 + 分页 |
| `GET` | `/admin/requests/{id}` | 单请求详情 + 全部 attempt（含上游错误原文） |
| `POST` | `/admin/config/reload` | 重读 YAML，热替换别名与适配器 |

`/admin/router/preview` 支持模拟各种请求形态，用来回答「为什么走了这个模型」：

```bash
curl -s "http://127.0.0.1:8317/admin/router/preview?model=zk-coding&prompt=write%20a%20python%20function&tools=2&json_mode=true" \
  -H 'X-Admin-Token: <token>'
```

---

## 14. 流式输出

请求里带 `"stream": true` 即返回 `text/event-stream`：

```
data: {"id":"mock-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"mock "}}]}

data: {"id":"mock-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"reply "}}]}

...

data: {"id":"mock-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{...}}

: zkai-meta {"request_id":"chatcmpl-...","alias":"zk-auto","resolved_model":"mock-smart","provider":"mock","credential_id":"mock-01","attempt":1,"routing_reason":"...","capability_scores":{...},"latency_ms":11.29}

data: [DONE]
```

设计要点：

- 纯 `data:` + `data: [DONE]`，OpenAI SDK 原样可解析。
- 路由元数据走 **SSE 注释行**（`:` 开头），任何 SSE 解析器都会忽略，**不可能污染 chunk 流**。
- `usage` 只在 `stream_options.include_usage: true` 时出现，与 OpenAI 一致。
- 客户端断开 → 通过 `request.is_disconnected()` 轮询感知，取消上游请求、关闭上游连接，
  请求记录为 `cancelled`。
- 正常消费完 `end` 事件后关闭生成器**不算**断连（这是一个真实踩过的坑，见 §18）。

---

## 15. 健康检查

| 模式 | 行为 |
|---|---|
| `manual` | 只在 `POST /admin/health/check` 时探测 |
| `startup` | 启动时探测一次 |
| `scheduled` | 按 `interval_seconds` 周期探测 |
| `off` | 完全关闭 |

探测只调供应商的 `GET /models`，**不消耗推理配额**。探测结果会驱动 Key 状态：
健康 → 恢复 `UNHEALTHY`/`COOLDOWN` 的 Key；失败 → 标记 `UNHEALTHY`。

`/health` 返回 `healthy` / `degraded` / `starting`：所有供应商都不可用时为 `degraded`。

`scripts/health_check.py` 提供无需启动服务的命令行探测（适合 cron / 监控探针）。

---

## 16. 数据库

默认 SQLite（`data/zkai.db`），任何 SQLAlchemy async URL 都可替换
（如 `postgresql+asyncpg://user:pass@host:5432/zkai`）。

| 表 | 内容 |
|---|---|
| `providers` | 供应商镜像（不含密钥） |
| `models` | 模型与能力分 |
| `deployments` | (provider, upstream model) 及优先级/成本 |
| `credentials` | Key 元数据 + 计数器 + `secret_ref`（环境变量名，**不是密钥**） |
| `model_aliases` | 别名定义 |
| `requests` | 每次请求：状态、路由原因、延迟、token、成本 |
| `request_attempts` | 每次尝试：provider/model/credential/状态/错误类型/延迟 |
| `usage_records` | token 与成本明细（按天聚合用） |
| `health_checks` | 探测历史 |

```bash
python scripts/init_db.py                        # 建表 + 同步配置镜像
python scripts/init_db.py --show                 # 打印已落库的行
python scripts/benchmark.py --runs 50 --model zk-auto   # 压测 + 延迟分位
python scripts/benchmark.py --stream --json             # 压流式路径，输出 JSON
```

---

## 17. 日志与安全

**日志**：`app/core/logging.py`。支持文本与 JSON 两种格式，通过 `contextvars` 自动给
每条日志带上 `request_id / attempt / provider / credential / model`：

```json
{"ts":"2026-09-10T09:22:18.834Z","level":"WARNING","logger":"zkai.routing.scheduler",
 "msg":"attempt 1 failed: rate_limit_error (429) on mock/mock-model-fail-429",
 "request_id":"chatcmpl-...","attempt":1,"provider":"mock","credential":"mock-01"}
```

**安全边界**（`app/core/security.py`）：

- 密钥**只**从环境变量解析，`${VAR}` / `${VAR:-default}`。
- 日志中的 `authorization` / `x-api-key` / `api_key` / `token` / `secret` 等字段自动
  替换为 `***`；`redact_text()` 还会清洗散落在自由文本里的 `sk-...` 形态字符串。
- 数据库只存 `secret_ref`（环境变量名）与不可逆指纹，**从不存密钥本体**。
- admin API 返回的凭据只有掩码（`sk-...abcd`）与指纹。
- admin 鉴权用 `hmac.compare_digest` 常量时间比较，防时序侧信道。
- `admin.enabled: false` 时整个 `/admin` 面返回 404，攻击面归零。

---

## 18. 测试

**228 个用例，全部通过，零网络、零真实配额。**

```bash
uv run pytest -q                                   # 全量
uv run pytest tests/test_retry.py -v               # 单模块
uv run pytest --cov=app --cov-report=term-missing  # 覆盖率
```

| 文件 | 覆盖内容 |
|---|---|
| `tests/conftest.py` | `FakeAdapter`（可编排行为、记录每次调用）、`Harness`（真实容器 + 内存 SQLite + 零耗时 sleeper） |
| `tests/test_errors.py` | 26 种错误分类的**契约表**（状态 → retryable/switch_*/冷却） |
| `tests/test_retry.py` | 退避与抖动边界、策略上限、重试预算、Key 轮换、冷却、故障转移深度、取消传播 |
| `tests/test_key_pool.py` | 状态机全部迁移、5 种轮换、冷却到期惰性恢复、并发安全 |
| `tests/test_router.py` | 别名解析/嵌套/环检测、能力打分、硬门槛、4 种策略排序、preview |
| `tests/test_api.py` | 全部 HTTP 端点、SSE 形状、流式预检状态码、admin 鉴权、断开连接 |
| `tests/test_config.py` | `.env` 注入进程环境、`${VAR}` 插值与默认值、env 优先于 YAML、三文件端到端加载、`*.example.yaml` 兜底、凭证配置告警 |
| `tests/test_adapters.py` | **只在真实端点上才会暴露的形状**：推理模型只回思考内容、`base_url` 尾斜杠、带厂商前缀的模型名 |

所有测试都用 `FakeAdapter`，**不会**发出任何真实请求。

### 开发过程中被测试/冒烟测出来的 10 个真实缺陷

1. **Scheduler 控制流错误**：`break` 只跳出退避循环，未能正确推进到下一个 Key/部署，
   导致 429/超时的轮换与故障转移不可靠，取消记录也会丢。→ 引入 `_Next` 枚举，
   用 `_decide()` / `_decide_open_stream()` 显式表达「下一步去哪」。
2. **流式预检失败被吞成 200**：`RequestService.stream` 把异常降级成 SSE `error` 事件，
   HTTP 层永远看到 200。→ 未发出任何数据前**重新抛出**，让上层返回真实状态码。
3. **400 污染 Key 统计**：客户端错误虽然不轮换 Key，却仍计入 `failure_count`，
   会拉低 `success_rate`、影响 `least_failures` 轮换、并虚高 `/admin/stats` 错误率。
   → 客户端错误只记录观测，不动计数器。
4. **正常结束的流被误记为 `cancelled`**：消费方收到 `end` 后 `break` 并 `aclose()`，
   生成器仍悬挂在 `yield end` 上，`GeneratorExit` 被误判为断连。
   → 用 `terminated` 标志区分「正常终止后关闭」与「中途断连」。
5. **无可用 Key 时的伪尝试编号重复**：某部署的 Key 全在冷却中时，`execute()` 把编号
   内联成 `attempt_number + 1` 却不落库、`stream()` 分支干脆不记录，
   审计表里于是出现重复的 `attempt: 1`。→ 两个分支都先自增再记录，
   与真实尝试共用同一套编号与落库路径。
6. **`.env` 被静默忽略**：`pydantic-settings` 只把 `.env` 读进 `Settings` 对象，
   而凭证是由 `resolve_env_reference()` 直接读 `os.environ` 的——
   所以密钥写在 `.env` 里永远不会生效，且没有任何提示。
   → 新增 `load_dotenv_file()`（`override=False`，真实环境变量优先），
   在 `load_app_config()` 开头调用。
7. **`env:` 被插值展开成密钥本身**（见 6.3）：Key 仍显示 `healthy`、不报「变量未设置」，
   但每个请求 401；含标识符字符的密钥还会被当作变量名写进日志与数据库。
   → `AppConfig.validate()` 增加两条校验（`env` 不是合法变量名 / `env`、`value` 全空
   却被启用），启动日志与 `/admin/health` 的 `config.warnings` 都会给出提示。
8. **推理模型的回答被思考内容挤空**：商汤 `*-flash-lite`、DeepSeek `*-pro`、
   Kimi K3、GLM-5.2 都是推理模型，先输出思考再输出答案。当 `max_tokens` 偏小时，
   上游返回 `content: ""` + `finish_reason: "length"`，思考内容在 `reasoning` 字段里。
   网关原样透传，调用方拿到 **HTTP 200 + 空字符串**，且没有任何提示。
   → `normalize_response()` 在 `content` 为空时回填 `reasoning`
   （兼容 `reasoning` / `reasoning_content` 两种命名），并加
   `content_recovered_from_reasoning: true` 标记，同时确保 `finish_reason` 为 `length`。
   **这条只有接真实供应商才会暴露**，mock 上游永远返回正常形状。
9. **流式路径把整段回答丢在 `delta.reasoning_content` 里**：魔搭 ModelScope
   （`Qwen/Qwen3.8-Flash-Next`、`deepseek-ai/DeepSeek-V4-Flash-0731` 实测）
   在**流式**下把答案全部塞进 `delta.reasoning_content`，而 `delta.content`
   自始至终是空字符串。`ChunkDelta` 的 `extra="allow"` 让这个字段能通过校验，
   于是网关忠实地转发了一串**空 delta**——客户端渲染出空白消息，
   但上游其实 200 成功、用量也照常计。缺陷 8 只修了非流式路径，
   流式是**同一个坑的另一半**。
   → `ChatCompletionChunkChoice.recover_reasoning_only_delta()` 在 `content` 为空时
   把 `reasoning_content` 提升为 `content`（同样兼容两种命名、同样打标）；
   `OpenAICompatibleAdapter._recover_reasoning_only_chunks()` 在流式循环里逐块调用。
   实测修复后同一请求从「0 字节输出」变为 27 个 chunk / 252 字符。
   **教训**：推理型供应商的「思考字段」在流式与非流式下是两套代码路径，
   修一处必须同时验证另一处。
10. **限流计数只增不减，用户等待换不来恢复**：`consecutive_rate_limits` 只在成功或
   手动启用时归零，冷却到期自动恢复（`refresh()`）时保留。线上实测（ZCode 连续
   调 `zk-k3` 写代码）：每轮请求把 5 个账号的 Key 逐个撞 429，计数器一路爬到
   冷却 900s 封顶；随后即使额度窗口部分恢复，下一次 429 仍从高位继续 +1——
   **zk-k3 连续 503 了 15 分钟，用户的等待反而背上了更高的历史包袱**。
   → `refresh()` 恢复时检查：距上次 429 已安静超过 `rate_limit_max`（900s）
   才清零计数。短冷却（60s→立刻再撞→120s）的指数退避语义保持不变；
   只有「躺满整个最长冷却周期」才视为重新开始计。加 2 个回归测试
   （短歇保梯子 / 满歇清零）。

另外修掉了 `"stop" if saw_content else "stop"` 这类死分支、`Repository` 里跨类型复用
`row` 变量等 40+ 个静态检查问题。

**静态检查**：

```bash
uv run ruff check app tests scripts   # All checks passed!
uv run mypy app scripts               # Success: no issues found in 57 source files
```

> `mypy` 只检查交付代码（`app/`、`scripts/`）。`tests/` 在 `pyproject.toml` 中显式排除：
> 测试大量使用鸭子类型替身（`StubRequest`、可空的 `pool.get()`），
> 为它们补 `cast` 无助于证明 `app/` 的正确性。

---

## 19. Docker

```bash
docker compose up --build          # 网关 :8317
docker build -t zk-ai .
docker run --rm -p 8317:8317 \
  -e OPENAI_KEY_01=sk-... \
  -e ZKAI_ADMIN_TOKEN=change-me \
  -v "$PWD/data:/app/data" \
  -v "$PWD/config:/app/config:ro" \
  zk-ai
```

镜像基于 `ghcr.io/astral-sh/uv`，多阶段构建；以非 root 用户运行；`HEALTHCHECK`
直接打 `/health`。`docker-compose.yml` 里已配好数据卷、日志轮转和环境变量占位。

---

## 20. 开发约定

```bash
# 提交前必须全绿
uv run ruff check app tests scripts
uv run mypy app
uv run pytest
```

- **行宽 110**、`target-version = py312`。
- 全量 `async/await`；I/O 全部 `httpx.AsyncClient`（连接池复用、每 provider 一个）。
- 领域模型一律 Pydantic v2（`ConfigDict(extra="forbid")` 防拼写错误）。
- 分层的依赖方向单向向下；`Router` 不做 I/O，`Scheduler` 不做路由决策。
- 新增供应商：实现 `ProviderAdapter`，在 `app/providers/factory.py` 注册，
  再加一组 `FakeAdapter` 驱动的测试。
- 新增错误码：先改 `classifier.py` 的决策矩阵，再补 `tests/test_errors.py` 契约表。

---

## 21. 已知限制与后续计划

- **`/v1/responses` 只覆盖常用子集**：`input` → `messages`、文本输出、usage。
  函数调用、`previous_response_id` 等尚未实现。
- **凭据的加密存储**：当前依赖环境变量/密钥管理系统；若要在库里存密文，
  需要接入 KMS（会引入新的威胁模型）。
- **多租户与配额**：目前是个人网关，没有租户维度；若要多用户共享，
  需要在 `requests` 上加 tenant 维度并做门控。
- **成本为「公开牌价等价成本」**：`input_cost_per_mtok` / `output_cost_per_mtok`（USD/百万 token）
  是配置的公开牌价，**免费渠道也按牌价折算**，用于回答"这趟相当于花了多少"；
  付费渠道这里即真实成本。按**实际命中的部署**计价（`meta.deployment_id` 贯穿 usage/requests）。
  不区分缓存命中/阶梯定价；牌价改动只影响**之后**的记录，历史可用
  `python scripts/backfill_cost.py --apply` 幂等回填。控制台同时显示 ≈¥（`CNY_RATE`）。
- **只有 OpenAI 协议，没有 `/v1/messages`**：Claude Code、Cline 等走 Anthropic
  Messages 协议的客户端**不能**直接把 `ANTHROPIC_BASE_URL` 指过来（会 404）。
  对接它们需要外挂一层协议转换（如 `claude-code-router`），或在网关内实现
  `/v1/messages` + 流式事件映射——后者工作量不小（`message_start` /
  `content_block_delta` / `message_delta` 一整套事件模型）。
  眼下用 `openai-compatible` 型客户端（ZCode / OpenAI SDK / Cherry Studio）最省事。
- **推理模型的空回复**：`max_tokens` 过小时，推理模型会把预算全花在思考上。
  网关已回填 `reasoning` 并打标（见 §18 缺陷 8），但**治本办法是给够 `max_tokens`**
  （建议 ≥ 800，或让网关按模型自动加预算）。

---

## License

MIT — 见 [LICENSE](LICENSE)。
