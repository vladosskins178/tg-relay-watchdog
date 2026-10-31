#!/usr/bin/env bash
# tg_send.sh "text" [chat_id] - send a plain-text reply via the bot.
# chat_id defaults to the first id in TG_ALLOWED_CHAT_IDS. Retries 3 times.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${TG_RELAY_ENV_FILE:-$HERE/.env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
TEXT="${1:?usage: tg_send.sh \"text\" [chat_id]}"
CHAT="${2:-${TG_ALLOWED_CHAT_IDS%%,*}}"
for i in 1 2 3; do
  if curl -s --max-time 20 "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${TEXT}" | grep -q '"ok":true'; then
    exit 0
  fi
  sleep $((i * 2))
done
echo "tg_send: failed after 3 attempts" >&2; exit 1
