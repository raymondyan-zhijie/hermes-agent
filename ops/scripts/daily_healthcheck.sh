#!/bin/bash
# Hermes Agent 每日健康检查 (v5 — host 直装, 2026-07-03)
# 一套体检: 成功发简报, 失败发详报。host crontab 0 9 * * * 经 wrapper 调用。
set -u
LOG="/home/admin/.hermes/logs/healthcheck.log"
ALERT="/home/admin/.hermes/logs/healthcheck_alert.txt"
SUMMARY="/home/admin/.hermes/logs/healthcheck_summary.txt"
mkdir -p "$(dirname "$LOG")"
TS=$(date '+%Y-%m-%d %H:%M:%S'); ALERTS=()
R='\033[0;31m'; Y='\033[1;33m'; G='\033[0;32m'; N='\033[0m'
ok()   { printf "  ${G}✓${N} %s\n" "$1"; }
warn() { printf "  ${Y}⚠${N} %s\n" "$1"; ALERTS+=("$1"); }
err()  { printf "  ${R}✗${N} %s\n" "$1"; ALERTS+=("$1"); }
echo "════════════════════════════════════════" | tee -a "$LOG"
echo "  Hermes Health Check — $TS (host 直装)"   | tee -a "$LOG"
echo "════════════════════════════════════════" | tee -a "$LOG"
# ─── 1. 资源 ───
echo "" | tee -a "$LOG"; echo "▸ 本机资源" | tee -a "$LOG"
USEPCT=$(df / | awk 'NR==2 {gsub("%",""); print $5}'); AVAIL=$(df -h / | awk 'NR==2 {print $4}')
if   [ "$USEPCT" -ge 85 ]; then err  "磁盘 ${USEPCT}% (剩 ${AVAIL}) 严重"
elif [ "$USEPCT" -ge 75 ]; then warn "磁盘 ${USEPCT}% (剩 ${AVAIL}) 关注"
else ok "磁盘 ${USEPCT}% (剩 ${AVAIL})"; fi
MEM_AVAIL_MB=$(free -m | awk '/^Mem:/ {print $7}')
if   [ "$MEM_AVAIL_MB" -lt 300 ]; then err  "内存仅剩 ${MEM_AVAIL_MB}MB"
elif [ "$MEM_AVAIL_MB" -lt 800 ]; then warn "内存剩 ${MEM_AVAIL_MB}MB"
else ok "内存剩 ${MEM_AVAIL_MB}MB"; fi
LOAD=$(uptime | awk -F'load average:' '{print $2}' | awk '{print $1}' | tr -d ','); CORES=$(nproc); LOAD_INT=${LOAD%.*}
if   [ "$LOAD_INT" -ge $((CORES*2)) ]; then err  "load=${LOAD} (cores=${CORES}) 过高"
elif [ "$LOAD_INT" -ge "$CORES" ]; then warn "load=${LOAD} (cores=${CORES})"
else ok "load=${LOAD} (cores=${CORES})"; fi
# ─── 2. Hermes 服务 (systemd) ───
echo "" | tee -a "$LOG"; echo "▸ Hermes 服务" | tee -a "$LOG"
GW=$(systemctl is-active hermes-gateway 2>/dev/null); DASH=$(systemctl is-active hermes-dashboard 2>/dev/null)
[ "$GW" = "active" ] && ok "hermes-gateway active" || err "hermes-gateway ${GW:-DOWN}"
[ "$DASH" = "active" ] && ok "hermes-dashboard active" || err "hermes-dashboard ${DASH:-DOWN}"
NR=$(systemctl show hermes-gateway -p NRestarts --value 2>/dev/null)
[ "${NR:-0}" -le 3 ] && ok "gateway 重启次数 $NR" || warn "gateway 重启次数 $NR 偏高"
KEY=$(grep '^API_SERVER_KEY=' /home/admin/.hermes/.env 2>/dev/null | cut -d= -f2-)
API_CODE=$(curl -sS -o /dev/null -m 5 -H "Authorization: Bearer $KEY" -w "%{http_code}" http://172.17.0.1:8642/v1/models 2>/dev/null || echo 000)
[ "$API_CODE" = "200" ] && ok "Hermes API 8642 ($API_CODE)" || err "Hermes API 8642 ($API_CODE)"
DB=/home/admin/.hermes/state.db; DBSZ=$(du -h "$DB" 2>/dev/null | cut -f1)
if [ -f "$DB" ] && [ "$(head -c 15 "$DB" 2>/dev/null)" = "SQLite format 3" ]; then ok "state.db 有效 SQLite ($DBSZ)"; else err "state.db 异常"; fi
# ─── 3. 其他服务 ───
echo "" | tee -a "$LOG"; echo "▸ 其他服务" | tee -a "$LOG"
NGINX=$(systemctl is-active nginx 2>/dev/null); [ "$NGINX" = "active" ] && ok "nginx active" || err "nginx ${NGINX:-DOWN}"
CF=$(systemctl is-active cloudflared 2>/dev/null); [ "$CF" = "active" ] && ok "cloudflared active" || err "cloudflared ${CF:-DOWN}"
DOCKER_OK=0
for c in open-webui filebrowser; do
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${c}$" && { ok "docker: ${c} 运行中"; DOCKER_OK=$((DOCKER_OK+1)); } || err "docker: ${c} 未运行"
done
# ─── 4. 安全审计 ───
echo "" | tee -a "$LOG"; echo "▸ 安全审计" | tee -a "$LOG"
AUDIT_PRIMARY=/root/security-audit-2026-06-16
AUDIT_FALLBACK=/home/admin/.hermes/state/security-baseline
AUDIT="$AUDIT_PRIMARY"; [ ! -r "$AUDIT/known-good/authorized_keys.sha256" ] && AUDIT="$AUDIT_FALLBACK"; KNOWN_AK_HASH=$(cat $AUDIT/known-good/authorized_keys.sha256 2>/dev/null)
if [ -n "$KNOWN_AK_HASH" ]; then
  CUR_HASH=$(sha256sum /home/admin/.ssh/authorized_keys 2>/dev/null | awk '{print $1}')
  [ "$CUR_HASH" = "$KNOWN_AK_HASH" ] && ok "authorized_keys 未变" || err "authorized_keys 已变更!"
else warn "无 authorized_keys baseline"; fi
dpkg --audit 2>/dev/null | grep -q . && warn "dpkg --audit 异常" || ok "dpkg --audit 干净"
# SUID 变化监测 (排除 containerd/docker 镜像层 + snap, 避免容器更新假阳性)
find / -xdev -perm -4000 -type f -not -path '/var/lib/containerd/*' -not -path '/var/lib/docker/*' -not -path '/snap/*' 2>/dev/null | sort > /tmp/.suid.now
SUID_N=$(wc -l < /tmp/.suid.now)
if [ -f /tmp/.suid.prev ]; then
  if diff -q /tmp/.suid.prev /tmp/.suid.now >/dev/null 2>&1; then ok "SUID 列表无变化 ($SUID_N 个)"
  else warn "SUID 列表变化 (见 /tmp/.suid.{prev,now})"; fi
else ok "SUID baseline 建立 ($SUID_N 个, 首次不对比)"; fi
mv /tmp/.suid.now /tmp/.suid.prev
# ─── 5. 公网端点 ───
echo "" | tee -a "$LOG"; echo "▸ 公网端点" | tee -a "$LOG"
PUB_OK=0
for pair in "https://chat.leimengde.net|Open WebUI" "https://file.leimengde.net|Filebrowser" "https://dashboard.leimengde.net|Dashboard"; do
  name="${pair##*|}"; url="${pair%%|*}"
  code=$(curl -sS -o /dev/null -m 8 -L -w "%{http_code}" "$url" 2>/dev/null || echo 000)
  case "$code" in 200|401) ok "$name ($code)"; PUB_OK=$((PUB_OK+1));; *) err "$name ($code)";; esac
done
# ─── 6. 总结 + 摘要 ───
echo "" | tee -a "$LOG"
if [ ${#ALERTS[@]} -eq 0 ]; then
  echo "✓ 全部正常 — $TS" | tee -a "$LOG"
  echo "✅ Hermes 体检正常 ($TS) | 磁盘 ${USEPCT}% 内存${MEM_AVAIL_MB}M load${LOAD} | gw/dashboard active API${API_CODE} state.db${DBSZ} | nginx/cf active 容器${DOCKER_OK}/3 | 公网${PUB_OK}/3" > "$SUMMARY"; > "$ALERT"
  exit 0
else
  echo "⚠ ${#ALERTS[@]} 项风险:" | tee -a "$LOG"; printf '  - %s\n' "${ALERTS[@]}" | tee -a "$LOG" | tee "$ALERT"
  echo "⚠ Hermes 体检发现 ${#ALERTS[@]} 项风险 ($TS) | 磁盘${USEPCT}% 内存${MEM_AVAIL_MB}M gw=${GW} API${API_CODE} 容器${DOCKER_OK}/3 公网${PUB_OK}/3" > "$SUMMARY"
  exit 1
fi
