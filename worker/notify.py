"""Telegram alerts: video ready, a run failed, the free picture limit ran out, new money, A/B winner.

The bot token and chat id live in data/accounts/telegram.json (never in settings.json, never sent back
to the browser). Which events are sent is chosen in Settings -> Notifications. Sending never blocks
or breaks the pipeline: it runs in a background thread and only logs a short warning on failure.
"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pipeline
from pipeline import DATA, PipelineError

CONFIG = DATA / "accounts" / "telegram.json"
STATE = DATA / "notify_state.json"     # which one-per-day alerts were already sent
API = "https://api.telegram.org/bot{token}/{method}"
EVENTS = {"done": "NOTIFY_DONE", "error": "NOTIFY_ERRORS", "limit": "NOTIFY_LIMITS",
          "money": "NOTIFY_MONEY", "ab": "NOTIFY_AB", "posted": "NOTIFY_POSTED"}


def _config():
    return pipeline._read_json(CONFIG, {}) or {}


def _call(token, method, params=None, timeout=15):
    url = API.format(token=token, method=method)
    body = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:  # the message never contains the URL, so the token stays out of logs
        try:
            detail = json.loads(e.read()).get("description", "")
        except Exception:
            detail = ""
        raise PipelineError(f"Telegram answered {e.code} {detail}".strip())
    except urllib.error.URLError as e:
        raise PipelineError(f"cannot reach Telegram: {e.reason}")
    if not data.get("ok"):
        raise PipelineError(f"Telegram: {data.get('description', 'error')}")
    return data["result"]


def status():
    c = _config()
    return {"configured": bool(c.get("bot_token") and c.get("chat_id")), "has_token": bool(c.get("bot_token")),
            "bot": c.get("bot_name"), "chat": c.get("chat_name"),
            "events": {k: pipeline.flag(v, "true") for k, v in EVENTS.items()}}


def save(bot_token=None, chat_id=None):
    c = _config()
    if bot_token:
        me = _call(bot_token.strip(), "getMe")
        c.update(bot_token=bot_token.strip(), bot_name="@" + me.get("username", "bot"))
    if chat_id is not None:
        c["chat_id"] = str(chat_id).strip()
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    pipeline._write_json(CONFIG, c)
    return status()


def find_chat():
    """The chat of the last message sent to the bot: the creator opens the bot and presses Start."""
    c = _config()
    if not c.get("bot_token"):
        raise PipelineError("save the bot token first")
    updates = _call(c["bot_token"], "getUpdates", {"limit": 20, "timeout": 0})
    chats = [u.get("message", {}).get("chat") for u in updates if u.get("message")]
    if not chats:
        raise PipelineError(f"no message yet: open {c.get('bot_name', 'your bot')} in Telegram, press Start, then try again")
    chat = chats[-1]
    c.update(chat_id=str(chat["id"]), chat_name=chat.get("username") or chat.get("first_name") or chat.get("title"))
    pipeline._write_json(CONFIG, c)
    return status()


def forget():
    CONFIG.unlink(missing_ok=True)
    return status()


def _send_now(text):
    c = _config()
    _call(c["bot_token"], "sendMessage", {"chat_id": c["chat_id"], "text": text[:4000],
                                          "disable_web_page_preview": "true"})


def test():
    if not status()["configured"]:
        raise PipelineError("connect the bot first")
    _send_now("✅ Vid Studio is connected. You will get alerts here.")
    return {"sent": True}


def link(job_id):
    base = pipeline.env("PUBLIC_BASE_URL") or "http://localhost:8000"
    return f"{base.rstrip('/')}/#/video/{job_id}"


def send(event, text):
    """Fire and forget: returns at once; a Telegram problem never touches the video."""
    if not status()["configured"] or (event in EVENTS and not pipeline.flag(EVENTS[event], "true")):
        return False

    def run():
        try:
            _send_now(text)
        except Exception as e:
            print(f"[warn] telegram alert not sent: {e}", flush=True)

    threading.Thread(target=run, daemon=True).start()
    return True


def once(key, event, text):
    """Send at most once per day per key (e.g. 'the FLUX limit ran out')."""
    state = pipeline._read_json(STATE, {}) or {}
    today = pipeline._now()[:10]
    if state.get(key) == today:
        return False
    state[key] = today
    pipeline._write_json(STATE, state)
    return send(event, text)
