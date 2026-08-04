#!/bin/bash
# Hermes 每日体检 wrapper (v2 host 直装, 2026-07-03)
# host crontab 0 9 * * * 调用。成功发简报, 失败发详报到飞书。
set -u
SCRIPT=/home/admin/.hermes/scripts/daily_healthcheck.sh
ALERT=/home/admin/.hermes/logs/healthcheck_alert.txt
SUMMARY=/home/admin/.hermes/logs/healthcheck_summary.txt
WLOG=/home/admin/.hermes/logs/healthcheck_wrapper.log
HERMES=/opt/hermes-agent/.venv/bin/hermes
export HERMES_HOME=/home/admin/.hermes
TS=$(date '+%Y-%m-%d %H:%M:%S'); mkdir -p "$(dirname "$WLOG")"
echo "[$TS] healthcheck_wrapper start" >> "$WLOG"
bash "$SCRIPT" >> "$WLOG" 2>&1; RC=$?
if [ "$RC" -eq 0 ]; then
  MSG=$(cat "$SUMMARY" 2>/dev/null)
  echo "[$TS] all green, sending daily brief" >> "$WLOG"
  echo "$MSG" | "$HERMES" send -t feishu 2>>"$WLOG" && echo "[$TS] brief sent" >> "$WLOG" || echo "[$TS] brief send FAILED" >> "$WLOG"
  exit 0
fi
# 失败: 发详报
if [ ! -s "$ALERT" ]; then
  MSG="⚠ Hermes 体检 exit=$RC 但 alert 文件空, 见 $WLOG"
else
  MSG="⚠ Hermes 体检发现风险 ($TS)：

$(cat "$ALERT")

详情见 $WLOG"
fi
echo "$MSG" | "$HERMES" send -t feishu 2>>"$WLOG" && echo "[$TS] alert sent" >> "$WLOG" || echo "[$TS] alert send FAILED" >> "$WLOG"
exit "$RC"
