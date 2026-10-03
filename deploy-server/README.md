# 服务器部署（ubuntu@120.53.28.29）

把 ZK-AI 的「网关 + 积分消耗器 + 公网入口」稳定跑在腾讯云轻量（2C2G / Ubuntu 24.04）上。
本机（Windows）与服务器**同时**运行，互不影响：本机账本在 `data/`，服务器账本在
`/var/lib/zkai/burner/`，两边各自独立记账。

## 现状一览（部署后实测）

| 组件 | 位置 | 状态 |
|---|---|---|
| 网关 `zkai` | `/opt/zkai`，systemd，`127.0.0.1:8318` | active，5/5 provider 可用 |
| 消耗器 `zkai-burner` | systemd，账本 `/var/lib/zkai/burner/` | active，48 并发保守档 |
| 公网入口 `zkai` | Caddy `/zkai/v1/*` + `/zkai/health` | 401/200 鉴权符合预期 |
| 公网控制台 `zkconsole` | Caddy `/zkconsole/ui/*` + `/zkconsole/admin/*` | 界面 200，管理接口四种鉴权组合全部符合预期 |
| 技能仓库 | `/opt/my-skills`，软链到 `~/.agents|.codex|.workbuddy-ai/skills` | 三个技能生效 |
| 其他在跑 | `sq`（智能询价，335MB RSS）/ `tandian` / `postgres` / `caddy` | 未受影响 |

## 一键同步

```bash
# Git Bash 或 WSL 里跑（需要 bash + scp + ssh）
cd /d/zhangkun/AI/ZK-AI        # 或对应挂载路径
bash deploy-server/sync-server.sh
```

只跑某一步：`SKIP_BURNER=1 SKIP_CADDY=1 bash deploy-server/sync-server.sh`
换服务器：`ZKAI_TARGET=ubuntu@<新IP> bash deploy-server/sync-server.sh`

幂等：密钥已存在会跳过；Caddyfile / burner.yaml / systemd 单元重复安装只会覆盖成同一份。

## 公网怎么调

```bash
curl https://120.53.28.29/zkai/v1/chat/completions \
  -H "Authorization: Bearer <ZKAI_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"model":"zk-auto","messages":[{"role":"user","content":"你好"}],"max_tokens":256}'
```

**控制台（浏览器打开）**：

```
https://120.53.28.29/zkconsole/ui/?token=<ZKAI_ADMIN_TOKEN>
```

- `/zkconsole/ui/` 控制台总览，`/zkconsole/ui/report/` 体检报告，
  `/zkconsole/ui/agent/` Agent 任务台。
- `?token=` 是网关 `index.html` 原生支持的直登参数，登入后地址栏会被
  `history.replaceState` 抹掉，不会留在浏览器历史里。
- 也可以不带 `?token=` 打开，然后在页面右上角「🔑 令牌」里粘一次，
  凭据只存在这台浏览器的 localStorage。
- 控制台能改的东西：供应商 / 凭据 / 配额 / 模型与别名 / burner 配置。
  改 `models.yaml`、`providers.yaml` 支持热加载，不用重启网关。



- base_url 填 `https://120.53.28.29/zkai/v1`（OpenAI SDK 用 `base_url`，路径别带 `/zkai/v1` 之外的前缀）。
- PowerShell：`-TimeoutSec 420`，网关整条链预算 300s，客户端必须比它大。
- token 就是 `/etc/zkai.env` 的 `ZKAI_API_TOKEN`（与本机 `.env` 同一份值）。

### 暴露面（重要）

一共三个前缀，各自独立鉴权，其余一律反代到智能询价：

| 前缀 | 内容 | 鉴权 |
|---|---|---|
| `/zkai/v1/*` | OpenAI 兼容接口 | `Authorization: Bearer <ZKAI_API_TOKEN>`，Caddy 精确比对 |
| `/zkai/health` | 健康检查 | 同上（不含敏感信息，但也不白送） |
| `/zkconsole/ui/*` | 控制台 HTML 外壳 | **不校验**（外壳零数据，见下） |
| `/zkconsole/admin/*` | 管理接口 | `Authorization: Bearer` 或 `X-Admin-Token` 带 `<ZKAI_ADMIN_TOKEN>`，Caddy + 网关双重校验 |
| `/zkconsole/health` | 控制台探活 | 不校验 |

`/zkai/admin/*`、`/zkai/ui/*`、`/zkai/web` **永不暴露**（实测 404）。

**为什么控制台 HTML 不校验 token**：`app/web/index.html` 是纯静态外壳，
每个数值都由 `/admin/*` 现取（见 `app/api/ui.py` 的模块 docstring）。
如果给 HTML 也加 token 门槛，浏览器首次打开时没有任何请求能带上 token，
页面会直接白屏。所以外壳放行、数据层校验——登录是控制台右上角「🔑 令牌」
或 URL 带 `?token=`，凭据只存浏览器 localStorage，不发往第三方。

**为什么在 Caddy 层就拦**：网关自身只对 `/v1/chat/completions` 校验 token，
`/v1/models` 和 `/health` 是裸的——本机部署假定，所以无 token 也返回 200。
一旦暴露到公网，「列模型 / 探活 / 拿路由拓扑」就变成免费信息。
`/admin/*` 虽然网关侧有 `require_admin`，但那是唯一一道闸，补一道 Caddy
更稳。两处都用**精确比对**，不是 `Bearer *` 通配：通配匹配等于任何非空串
都能过，拦不住扫描器（实测 wrong / near-miss token 都是 401）。

## 为什么 burner 是保守档

服务器是 2C2G，`sq` 单独占 335MB RSS，`tandian` 与 `zkai` 也在这台机器上。
直接套本机 96 并发会把整机拖进 swap（内存 1.9G，可用约 860MB）。
所以：

| 参数 | 本机 | 服务器 |
|---|---|---|
| `concurrency` | 96 | 48 |
| `per_account_start` / `max` | 16 | 8 |
| systemd `MemoryMax` / `MemoryHigh` | 无 | 400M / 320M |
| systemd `CPUQuota` | 无 | 50% |
| `Nice` / `IOSchedulingPriority` | 无 | 10 / 6 |

**账本不互通**：本机 `data/burn_state.json`（rate_in/rate_out 是按本机并发校准的，
anchors 是按账号逐个填的）**不复制**到服务器。服务器从零开始学自己的并发点，
重启不丢账本，速率校准保持各自独立。想统一看两边烧了多少，
各自 `journalctl -u zkai-burner` 与本地 `data/burn_sensenova.log` 的周期汇总行。

## 服务器档位改配置

改 `/opt/zkai/config/burner.yaml` 就行，**支持热加载**（下个汇总周期生效），不用重启：

```bash
ssh ubuntu@120.53.28.29 'sudo -n true'
ssh ubuntu@120.53.28.29 "grep -E '^(concurrency|per_account_start|per_account_max):' /opt/zkai/config/burner.yaml"
```

⚠️ `burn_sensenova.py` 的 `parse_args()` 里 `load_config()` **不接** `--config-file`，
固定读 `<cwd>/config/burner.yaml`。`--config-file` 只影响热加载监听的文件，
不影响初始配置来源。所以改档位就改 `/opt/zkai/config/burner.yaml`，
传 `--config-file` 到别处会**静默失效**（启动日志的「配置来源」会显示真实路径，以它为准）。

`model` / `only` / `account_groups` / `anchors` / `week_anchors` 改了要重启才生效。

## 日常运维命令

```bash
# 状态一条龙
ssh ubuntu@120.53.28.29 'systemctl is-active zkai zkai-burner caddy sq tandian'

# 网关健康 / provider / credential
ssh ubuntu@120.53.28.29 'curl -sS http://127.0.0.1:8318/health'

# burner 实时
ssh ubuntu@120.53.28.29 'journalctl -u zkai-burner -f'

# burner 摘要（每 60s 一行：周期积分、成功率、账号停靠数）
ssh ubuntu@120.53.28.29 'journalctl -u zkai-burner --no-pager | grep 周期 | tail -3'

# 资源占用（确认没把整机拖垮）
ssh ubuntu@120.53.28.29 'systemctl show zkai-burner -p MemoryCurrent -p CPUUsageNSec; free -m'
```

## 换机 / 迁移边界

- `/opt/my-skills` 是技能仓库的**服务器副本**（git clone 自 GitHub），
  新技能/改 SKILL.md 后本机 `git push`，服务器 `cd /opt/my-skills && git pull` 即可。
  软链已挂在 `~/.agents/skills`、`~/.codex/skills`、`~/.workbuddy-ai/skills` 三处。
- `/etc/caddy/Caddyfile` 改了先 `caddy validate` 再 reload，别跳过校验——
  Caddy 挂了会连带智能询价一起 502。
- `/etc/caddy/Caddyfile.sq` 是智能询价独占时的备份，需要还原就
  `sudo cp /etc/caddy/Caddyfile.sq /etc/caddy/Caddyfile && sudo caddy validate ... && sudo systemctl reload caddy`。
- 服务器上**没有任何本机专有的数据库**。`data/zkai.db` 在服务器是独立的
  （`/var/lib/zkai/zkai.db`），只记服务器这边的请求与用量。

## 本目录文件

| 文件 | 作用 |
|---|---|
| `sync-server.sh` | 一键部署/同步脚本（幂等） |
| `burner.yaml` | 服务器保守档 burner 配置 |
| `zkai-burner.service` | burner 的 systemd 单元（限额+优先级） |
| `Caddyfile` | Caddy 配置（`/zkai/*` 网关 API + `/zkconsole/*` 控制台） |

密钥一律不进这三个文件：SENSENOVA key 走 scp 到服务器 `.env`，
token 从 `/opt/zkai/.env` 现取写进 `/etc/caddy/zkai.env`（root:caddy 640）；
admin token 同样从 `.env` 现取写同一份文件，供控制台管理接口双保险用。