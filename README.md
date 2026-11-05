# tg-relay-watchdog

> **Archived.** This project continues as [agent-ops-telegram](https://github.com/vladosskins178/agent-ops-telegram).


Drive a terminal program from Telegram. Messages you send to your bot are pasted into a tmux pane and Enter is pressed, so any long-running CLI (an AI coding agent, a REPL, a shell) can take instructions from your phone. A cron watchdog keeps the relay alive.

We run this pattern in production to talk to AI agents on our servers. This is a clean, generic rewrite of it.

```
 You (Telegram) ──> Bot API ──> tg_relay.py ──> tmux pane ──> your CLI
        ^                                                        │
        └──────────────────── tg_send.sh <───────────────────────┘
                         watchdog.sh (cron, every 2 min)
```

## Why the watchdog

`systemd Restart=always` only catches a process that exits. In practice the failures we saw were different:

| Failure | Symptom | What the watchdog does |
|---|---|---|
| Two pollers on one token | Messages "arrive sometimes": Telegram splits updates between them | Stops the service, kills strays, starts one clean instance |
| Relay alive but wedged | Nothing arrives, process looks healthy | Restarts if `pending_update_count` did not drain since the last check |
| tmux session gone (reboot) | Messages pasted into nothing | Recreates the session from `TMUX_START_CMD` |
| Service stopped | Nothing arrives | Restarts it |

The relay also takes a file lock on start, so a second copy exits immediately instead of competing for updates. The update offset is saved to disk, so a restart neither loses nor replays messages.

## Features

- Long polling. No webhook, no open port, no domain needed.
- Allow-list of chat ids. Messages from anyone else are ignored and logged.
- Photos, documents, voice and audio are downloaded; the local path is included in the injected line, e.g. `[TG @you chat_id=1 msg_id=42 file=/home/me/.tg-relay/inbox/....jpg]: what's wrong here?`
- Multi-line text stays intact (uses `tmux load-buffer` / `paste-buffer`, not `send-keys`).
- Python 3.8+ standard library only. Bash + curl for the scripts.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your chat id (for example from @userinfobot).
2. Install:
   ```bash
   sudo git clone https://github.com/vladosskins178/tg-relay-watchdog /opt/tg-relay-watchdog
   cd /opt/tg-relay-watchdog
   cp .env.example .env && chmod 600 .env   # fill in TG_BOT_TOKEN, TG_ALLOWED_CHAT_IDS, TMUX_TARGET
   ```
3. Start the tmux session your CLI runs in (it must match `TMUX_TARGET`):
   ```bash
   tmux new-session -d -s main
   ```
4. Run the relay as a service (edit paths, and add `User=` if the tmux session belongs to a non-root user):
   ```bash
   sudo cp tg-relay.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now tg-relay
   journalctl -u tg-relay -f
   ```
5. Add the watchdog to the same user's crontab as the service manager:
   ```
   */2 * * * * /opt/tg-relay-watchdog/watchdog.sh >/dev/null 2>&1
   ```
6. Reply from your CLI or scripts:
   ```bash
   ./tg_send.sh "Build finished"
   ```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TG_BOT_TOKEN` | required | Bot token from BotFather |
| `TG_ALLOWED_CHAT_IDS` | required | Comma-separated chat ids allowed to send input |
| `TMUX_TARGET` | `main` | tmux target, `session` or `session:window.pane` |
| `TMUX_START_CMD` | empty | If set, used to recreate a missing session |
| `TG_RELAY_ENV_FILE` | `.env` | Path to the env file |
| `TG_RELAY_STATE_DIR` | `~/.tg-relay` | Lock, offset, inbox and watchdog log |
| `TG_POLL_TIMEOUT` | `25` | Long-poll seconds |
| `TG_SKIP_BACKLOG` | `0` | `1` drops messages sent while the relay was down |
| `TG_RELAY_SERVICE` | `tg-relay` | systemd unit name the watchdog manages |

## Security notes

Anyone in the allow-list can type into your terminal. Treat the bot token like a password, keep `.env` at `chmod 600`, and only list chat ids you control. If the pane runs a shell, a message is a shell command.

## License

MIT. See [LICENSE](LICENSE). Built by [VL Studio](https://vlstudio.app).
