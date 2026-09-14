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

## 当前状态（2026-09-12）

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
- 2026-09-13（晚）：新增原生 **`/v1/messages`（Anthropic 协议）端点**
  （`app/api/messages.py` + `app/models/anthropic.py`，`ZKAI_ANTHROPIC_DEFAULT_MODEL`
  兜底 claude-* 模型名）。起因：CC Switch 的 openai_chat 翻译层缺
  `content_block_stop` → Claude Code 收到空回复（对照实验实锤）；CC Switch 里
  zk-ai 供应商已切 `anthropic` 直通（BASE_URL 去掉 `/v1`），`claude -p` 与
  工具调用端到端验证通过。次要问题待观察：商汤 K3 对 2.5 万 token+ 请求会
  TPM 429（重试风暴）、moonshot 部署无可用 Key、nvidia 偶发流式连接超时
- 门禁 ruff / mypy / pytest 全绿（263 passed）；2026-09-12 已提交推送
- 消耗器费率已两次控制台实测交叉校准（实际 ≈入111/出333 积分/百万token，区间
  111~240/333~720），默认 120/360 显示贴合实扣；账本持久化在 `data/burn_state.json`
  （重启不清零），`--calibrate-actual 实扣数` 可随时精校准；并发 AIMD 自适应
  （全局≤128、单账号 8→24、429 减半/静默每分钟 +1），429 频繁就调低 `--per-account-max`
