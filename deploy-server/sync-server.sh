#!/usr/bin/env bash
# ZK-AI server-side deploy / sync. Idempotent, safe to re-run.
#
#   bash deploy-server/sync-server.sh
#   SKIP_BURNER=1 SKIP_CADDY=1 bash deploy-server/sync-server.sh
#
# 1. Gateway: copy SENSENOVA_API_KEY_* from local .env to the server
#    (they were missing, so the sensenova provider was unavailable and 12
#    credentials sat disabled), restart zkai.
# 2. Burner: install the conservative profile (48 concurrency / per_account 8)
#    + zkai-burner.service (MemoryMax=400M / CPUQuota=50%, protecting sq and
#    tandian), enable + start.
# 3. Public entry: Caddyfile exposing /zkai/v1/* + /zkai/health (gateway API)
#    and /zkconsole/ui/* + /zkconsole/admin/* (operator console), with exact
#    Bearer comparisons in Caddy. The gateway itself only authenticates
#    /v1/chat/completions; /v1/models, /health and /admin/* are either open or
#    guarded by a single check, so Caddy adds the second one.
#
# Keys travel over scp/SSH only. This directory holds no secrets; the three
# committed files are burner.yaml, zkai-burner.service, Caddyfile.
set -euo pipefail

# This script needs bash + scp. Run it from Git Bash or WSL, not from cmd/PowerShell.
case "${OSTYPE:-}" in
  msys*|cygwin*|win32*) ;;
  *) : ;;
esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$REPO/.env" ]; then
  echo "ERROR: 找不到 $REPO/.env —— 请在 Git Bash / WSL 里跑本脚本，"
  echo "       或在 ZKAI_TARGET / 仓库根目录正确的前提下重试。"
  exit 1
fi
TARGET="${ZKAI_TARGET:-ubuntu@120.53.28.29}"
DEPLOY="$REPO/deploy-server"

remote() { ssh "$TARGET" "echo $1 | base64 -d | sudo bash"; }

# Only SENSENOVA_API_KEY*, never the whole .env (it carries many other keys).
STAGED="$(mktemp)"; trap 'rm -f "$STAGED"' EXIT
grep -E '^[[:space:]]*SENSENOVA_API_KEY(_[0-9]+)?=' "$REPO/.env" > "$STAGED" || true
[ -s "$STAGED" ] || { echo "ERROR: no SENSENOVA_API_KEY in .env"; exit 1; }
echo "==> $(wc -l < "$STAGED") SENSENOVA_API_KEY staged"

echo "==> 1/3 gateway (keys + provider health)"
scp "$STAGED" "$TARGET:/tmp/zkai-sensenova.env" >/dev/null
STEP1="set -e
if grep -q '^SENSENOVA_API_KEY' /opt/zkai/.env 2>/dev/null; then
  echo '[skip] /opt/zkai/.env already has SENSENOVA_API_KEY'
else
  cp -a /opt/zkai/.env \"/opt/zkai/.env.bak-\$(date +%Y%m%d-%H%M%S)\"
  printf '\\n# --- added by deploy ---\\n' >> /opt/zkai/.env
  cat /tmp/zkai-sensenova.env >> /opt/zkai/.env
  echo \"[ok] appended \$(grep -c '^SENSENOVA_API_KEY' /opt/zkai/.env) keys\"
fi
chown www-data:www-data /opt/zkai/.env; chmod 600 /opt/zkai/.env
grep -q '^SENSENOVA_API_KEY' /etc/zkai.env || { cat /tmp/zkai-sensenova.env >> /etc/zkai.env; chmod 600 /etc/zkai.env; }
rm -f /tmp/zkai-sensenova.env
systemctl restart zkai; sleep 6
curl -sS --max-time 10 http://127.0.0.1:8318/health | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"providers:\",d[\"providers\"][\"available\"],\"/\",d[\"providers\"][\"total\"],\"credentials usable:\",d[\"credentials\"][\"usable\"])'"
remote "$(printf '%s' "$STEP1" | base64 -w0)"

if [ "${SKIP_BURNER:-0}" != "1" ]; then
  echo "==> 2/3 burner (conservative profile)"
  scp "$DEPLOY/burner.yaml" "$TARGET:/tmp/zkai-burner.yaml" >/dev/null
  scp "$DEPLOY/zkai-burner.service" "$TARGET:/tmp/zkai-burner.service" >/dev/null
  STEP2="set -e
install -o root -g root -m 644 /tmp/zkai-burner.service /etc/systemd/system/zkai-burner.service
mkdir -p /var/lib/zkai/burner
chown www-data:www-data /var/lib/zkai/burner; chmod 750 /var/lib/zkai/burner
[ -f /opt/zkai/config/burner.yaml ] && cp -a /opt/zkai/config/burner.yaml \"/opt/zkai/config/burner.yaml.bak-\$(date +%Y%m%d-%H%M%S)\"
install -o www-data -g www-data -m 664 /tmp/zkai-burner.yaml /opt/zkai/config/burner.yaml
rm -f /tmp/zkai-burner.yaml /tmp/zkai-burner.service
systemctl daemon-reload
systemctl enable --now zkai-burner
sleep 8
systemctl is-active zkai-burner
journalctl -u zkai-burner --no-pager | grep '配置来源' | tail -1 | grep -o 'concurrency=[0-9]*'"
  remote "$(printf '%s' "$STEP2" | base64 -w0)"
fi

if [ "${SKIP_CADDY:-0}" != "1" ]; then
  echo "==> 3/3 public entry (Caddy subpath + exact Bearer match)"
  scp "$DEPLOY/Caddyfile" "$TARGET:/tmp/zkai-Caddyfile" >/dev/null
  STEP3="set -e
install -o root -g root -m 644 /tmp/zkai-Caddyfile /etc/caddy/Caddyfile
T=\$(grep '^ZKAI_API_TOKEN=' /opt/zkai/.env | cut -d= -f2-)
A=\$(grep '^ZKAI_ADMIN_TOKEN=' /opt/zkai/.env | cut -d= -f2-)
printf 'ZKAI_PUBLIC_TOKEN=%s\nZKAI_ADMIN_TOKEN_PUBLIC=%s\n' \"\$T\" \"\$A\" > /etc/caddy/zkai.env
chown root:caddy /etc/caddy/zkai.env; chmod 640 /etc/caddy/zkai.env
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\\nEnvironmentFile=/etc/caddy/zkai.env\\n' > /etc/systemd/system/caddy.service.d/zkai-token.conf
rm -f /tmp/zkai-Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
systemctl daemon-reload
systemctl reload caddy
sleep 3
A2=\$(grep '^ZKAI_ADMIN_TOKEN=' /opt/zkai/.env | cut -d= -f2-)
echo -n 'console ui:       '; curl -sS --max-time 15 -o /dev/null -w '%{http_code}\\n' https://120.53.28.29/zkconsole/ui/
echo -n 'api no-token:     '; curl -sS --max-time 15 -o /dev/null -w '%{http_code}\\n' https://120.53.28.29/zkai/health
echo -n 'api bad-token:    '; curl -sS --max-time 15 -o /dev/null -w '%{http_code}\\n' -H 'Authorization: Bearer wrong' https://120.53.28.29/zkai/health
echo -n 'admin no-token:   '; curl -sS --max-time 15 -o /dev/null -w '%{http_code}\\n' https://120.53.28.29/zkconsole/admin/providers
echo -n 'admin good-token: '; curl -sS --max-time 15 -o /dev/null -w '%{http_code}\\n' -H "Authorization: Bearer \"\$A2\"" https://120.53.28.29/zkconsole/admin/providers
echo -n 'tandian:          '; curl -sS --max-time 20 -o /dev/null -w '%{http_code}\\n' https://120.53.28.29/tandian/"
  remote "$(printf '%s' "$STEP3" | base64 -w0)"
fi

echo ""
echo "Done. Public endpoints:"
echo "  POST https://120.53.28.29/zkai/v1/chat/completions"
echo "  Header: Authorization: Bearer <ZKAI_API_TOKEN>"
echo "  Console: https://120.53.28.29/zkconsole/ui/?token=<ZKAI_ADMIN_TOKEN>"
