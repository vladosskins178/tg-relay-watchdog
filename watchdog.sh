#!/usr/bin/env bash
# watchdog.sh - keep tg-relay healthy. Run from cron every 2 minutes:
#   */2 * * * * /opt/tg-relay-watchdog/watchdog.sh >/dev/null 2>&1
#
# Catches what systemd Restart=always cannot:
#   1) service not active                     -> restart
#   2) poller count != 1 (crashed or duplicate) -> kill strays / restart
#   3) relay alive but stuck: Telegram's pending update count is not draining -> restart
#   4) optional: tmux session missing          -> start it (TMUX_START_CMD)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${TG_RELAY_ENV_FILE:-$HERE/.env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

SERVICE="${TG_RELAY_SERVICE:-tg-relay}"
STATE_DIR="${TG_RELAY_STATE_DIR:-$HOME/.tg-relay}"
LOG="$STATE_DIR/watchdog.log"
PENDING_STATE="$STATE_DIR/pending.last"
PATTERN="${TG_RELAY_PROC_PATTERN:-tg_relay.py}"
mkdir -p "$STATE_DIR"

note() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" >> "$LOG"; }
restart() { note "RESTART ($*)"; systemctl restart "$SERVICE"; }

# 0) target tmux session (optional)
if [ -n "${TMUX_START_CMD:-}" ]; then
  SESSION="${TMUX_TARGET%%:*}"
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    note "tmux session '$SESSION' missing -> starting"
    tmux new-session -d -s "$SESSION" "$TMUX_START_CMD"
  fi
fi

# 1) service active?
if ! systemctl is-active --quiet "$SERVICE"; then
  restart "service not active"; exit 0
fi

# 2) exactly one poller? (two pollers on one token split messages randomly)
N=$(pgrep -f "python3? .*$PATTERN" | wc -l)
if [ "$N" -eq 0 ]; then
  restart "no poller process"; exit 0
elif [ "$N" -gt 1 ]; then
  note "found $N pollers -> restarting clean"
  systemctl stop "$SERVICE"; pkill -f "python3? .*$PATTERN"; sleep 1
  systemctl start "$SERVICE"; exit 0
fi

# 3) stuck detection: pending updates must drain between two checks
if [ -n "${TG_BOT_TOKEN:-}" ]; then
  PEND=$(curl -s --max-time 15 "https://api.telegram.org/bot${TG_BOT_TOKEN}/getWebhookInfo" \
         | grep -oE '"pending_update_count":[0-9]+' | grep -oE '[0-9]+$')
  PEND=${PEND:-0}
  PREV=$(cat "$PENDING_STATE" 2>/dev/null || echo 0)
  echo "$PEND" > "$PENDING_STATE"
  if [ "$PEND" -gt 0 ] && [ "$PREV" -gt 0 ] && [ "$PEND" -ge "$PREV" ]; then
    restart "stuck: pending=$PEND (previous check $PREV)"; exit 0
  fi
fi
exit 0
