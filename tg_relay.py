#!/usr/bin/env python3
"""
tg-relay — forward Telegram bot messages into a tmux session as typed input.

Use case: drive a long-running CLI (an AI coding agent, a REPL, a shell) from
your phone. You message your bot; the text is pasted into a tmux pane and Enter
is pressed. Replies go back however you like (e.g. the CLI calls `tg_send.sh`).

Features
  - long-polls getUpdates (no webhook, no public port needed)
  - allow-list of chat ids; everything else is ignored and logged
  - photos / documents are downloaded and their local path is included
  - single-instance lock (two pollers on one token split messages randomly)
  - update offset persisted to disk, so a restart does not lose or replay messages
  - standard library only (Python 3.8+)

All configuration comes from environment variables, see .env.example.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


def load_env_file(path: str | None) -> None:
    """Load KEY=VALUE lines into os.environ (does not override existing vars)."""
    if not path or not Path(path).is_file():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file(os.environ.get("TG_RELAY_ENV_FILE", ".env"))

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("TG_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
TMUX_TARGET = os.environ.get("TMUX_TARGET", "main")
TMUX_START_CMD = os.environ.get("TMUX_START_CMD", "")  # optional: command to start the session if missing
STATE_DIR = Path(os.environ.get("TG_RELAY_STATE_DIR", Path.home() / ".tg-relay"))
INBOX_DIR = Path(os.environ.get("TG_RELAY_INBOX_DIR", STATE_DIR / "inbox"))
POLL_TIMEOUT = int(os.environ.get("TG_POLL_TIMEOUT", "25"))
SKIP_BACKLOG = os.environ.get("TG_SKIP_BACKLOG", "0") == "1"

API = f"https://api.telegram.org/bot{TOKEN}"
OFFSET_FILE = STATE_DIR / "offset"
LOCK_FILE = STATE_DIR / "relay.lock"


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | tg-relay | {msg}", flush=True)


def api_get(method: str, http_timeout: int = 20, **params) -> dict:
    url = f"{API}/{method}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=http_timeout) as r:
        return json.loads(r.read())


def acquire_lock():
    """Exit if another relay instance already holds the lock."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another instance is running (lock held) - exiting")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep the handle open for the life of the process


def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_offset(offset: int) -> None:
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset))
    tmp.replace(OFFSET_FILE)


def download_file(file_id: str, suffix: str = "") -> str | None:
    try:
        data = api_get("getFile", file_id=file_id)
        if not data.get("ok"):
            log(f"getFile failed: {data.get('description')}")
            return None
        remote = data["result"]["file_path"]
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        ext = suffix or (Path(remote).suffix or ".bin")
        local = INBOX_DIR / f"{int(time.time())}_{file_id[:12]}{ext}"
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{remote}", timeout=60) as r:
            local.write_bytes(r.read())
        log(f"downloaded {local.name} ({local.stat().st_size} bytes)")
        return str(local)
    except Exception as e:  # network errors must never kill the loop
        log(f"download error: {e}")
        return None


def tmux_session_exists() -> bool:
    session = TMUX_TARGET.split(":")[0]
    return subprocess.run(["tmux", "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def inject(text: str) -> bool:
    """Paste text into the tmux target and press Enter. Returns True on success."""
    if not tmux_session_exists():
        if TMUX_START_CMD:
            session = TMUX_TARGET.split(":")[0]
            log(f"tmux session '{session}' missing - starting it")
            subprocess.run(["tmux", "new-session", "-d", "-s", session, TMUX_START_CMD], check=False)
            time.sleep(5)
        if not tmux_session_exists():
            log(f"tmux target '{TMUX_TARGET}' not available - message dropped")
            return False
    # load-buffer + paste-buffer keeps multi-line text intact (send-keys would not)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
        f.write(text)
        path = f.name
    buf = f"tgrelay_{os.getpid()}_{int(time.time() * 1000)}"
    try:
        subprocess.run(["tmux", "load-buffer", "-b", buf, path], check=True)
        subprocess.run(["tmux", "paste-buffer", "-b", buf, "-t", TMUX_TARGET, "-d"], check=True)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"], check=True)
        log(f"injected {len(text)} chars -> {TMUX_TARGET}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux error: {e}")
        return False
    finally:
        os.unlink(path)


def build_prompt(msg: dict) -> str | None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    user = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name", "user")
    text = msg.get("text") or msg.get("caption") or ""
    attachment = None

    if msg.get("photo"):
        attachment = download_file(msg["photo"][-1]["file_id"], ".jpg")  # last = largest
    elif msg.get("document"):
        doc = msg["document"]
        attachment = download_file(doc["file_id"], Path(doc.get("file_name", "")).suffix)
    elif msg.get("voice"):
        attachment = download_file(msg["voice"]["file_id"], ".oga")
    elif msg.get("audio"):
        attachment = download_file(msg["audio"]["file_id"], Path(msg["audio"].get("file_name", "")).suffix or ".mp3")

    if not text and not attachment:
        return None
    meta = f"TG @{user} chat_id={chat_id} msg_id={msg.get('message_id', 0)}"
    if attachment:
        meta += f" file={attachment}"
    return f"[{meta}]: {text or '(no caption)'}"


def main() -> None:
    if not TOKEN:
        log("TG_BOT_TOKEN is not set")
        sys.exit(1)
    if not ALLOWED:
        log("TG_ALLOWED_CHAT_IDS is not set - refusing to relay messages from anyone")
        sys.exit(1)
    _lock = acquire_lock()  # noqa: F841

    offset = read_offset()
    if SKIP_BACKLOG or offset == 0:
        backlog = api_get("getUpdates", offset=offset, timeout=0).get("result", [])
        if backlog:
            offset = backlog[-1]["update_id"] + 1
            write_offset(offset)
            log(f"skipped {len(backlog)} old updates")
    log(f"started: target={TMUX_TARGET} allowed={len(ALLOWED)} chat(s) offset={offset}")

    while True:
        try:
            data = api_get("getUpdates", http_timeout=POLL_TIMEOUT + 10,
                           offset=offset, timeout=POLL_TIMEOUT)
        except Exception as e:
            log(f"getUpdates error: {e}")
            time.sleep(3)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            write_offset(offset)  # persist before handling: never replay a message
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id not in ALLOWED:
                log(f"ignored message from chat {chat_id}")
                continue
            prompt = build_prompt(msg)
            if prompt:
                inject(prompt)


if __name__ == "__main__":
    main()
