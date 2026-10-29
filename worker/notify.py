"""Telegram alerts: video ready, a run failed, the free picture limit ran out, new money, A/B winner.

The bot token and chat id live in data/accounts/telegram.json (never in settings.json, never sent back
to the browser). Which events are sent is chosen in Settings -> Notifications. Sending never blocks
or breaks the pipeline: it runs in a background thread and only logs a short warning on failure.
"""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

import pipeline
import space
from pipeline import DATA, PipelineError

CONFIG = space.Here("accounts/telegram.json")
STATE = space.Here("notify_state.json")     # which one-per-day alerts were already sent
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
    if not chats and _last_chat.get(c["bot_token"]):   # another channel's poller read it first
        chats = [_last_chat[c["bot_token"]]]
    if not chats:
        raise PipelineError(f"no message yet: open {c.get('bot_name', 'your bot')} in Telegram, press Start, then try again")
    chat = chats[-1]
    c.update(chat_id=str(chat["id"]), chat_name=chat.get("username") or chat.get("first_name") or chat.get("title"))
    pipeline._write_json(CONFIG, c)
    return status()


def forget():
    CONFIG.unlink(missing_ok=True)
    return status()


def _send_now(text, buttons=None):
    c = _config()
    params = {"chat_id": c["chat_id"], "text": text[:4000], "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = json.dumps({"inline_keyboard": [
            [{"text": label, "callback_data": data[:64]} for label, data in row] for row in buttons]})
    _call(c["bot_token"], "sendMessage", params)


def test():
    if not status()["configured"]:
        raise PipelineError("connect the bot first")
    _send_now("✅ Vid Studio is connected. You will get alerts here.\nPress the button to check that buttons work:",
              [[("👆 Test button", f"tst|{space.current()}")]])
    return {"sent": True}


def link(job_id):
    base = pipeline.env("PUBLIC_BASE_URL") or "http://localhost:8000"
    return f"{base.rstrip('/')}/#/video/{job_id}"


def send(event, text, buttons=None):
    """Fire and forget: returns at once; a Telegram problem never touches the video.
    buttons: rows of (label, callback data) shown under the message; presses arrive in poll_loop()."""
    if not status()["configured"] or (event in EVENTS and not pipeline.flag(EVENTS[event], "true")):
        return False

    def run():
        try:
            _send_now(text, buttons)
        except Exception as e:
            print(f"[warn] telegram alert not sent: {e}", flush=True)

    space.thread(run)   # keeps this channel's bot and chat
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


# ------------------------------------------------------------------ buttons

_last_chat = {}   # bot token -> the chat of the last plain message the poller saw (for find_chat)


def _bots():
    """bot token -> the channels using it (several channels may share one bot)."""
    bots = {}
    for cid in space.list_ids():
        with space.use(cid):
            c = _config()
        if c.get("bot_token") and c.get("chat_id"):
            bots.setdefault(c["bot_token"], []).append((cid, str(c["chat_id"])))
    return bots


def _press(token, channels, cq):
    """One button press: 'pub|<job>' makes the private upload public, 'del|<job>' deletes it,
    'tst|<channel>' only answers (the test message). Only the chat the alert went to may press it."""
    import social
    action, _, job_id = (cq.get("data") or "").partition("|")
    chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
    cid = job_id if action == "tst" else space.find_job(job_id)
    if not cid or (cid, chat) not in channels:
        _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Not allowed here"})
        return
    with space.use(cid):
        try:
            if action == "tst":
                done = "✅ The button works: Vid Studio got your press."
            elif action == "pub":
                done = "🌍 Published: " + social.youtube_make_public(job_id)
            elif action == "del":
                social.youtube_delete(job_id)
                done = "🗑 Deleted from YouTube (the video stays in the Studio)"
            else:
                return
        except Exception as e:
            _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": str(e)[:190],
                                                 "show_alert": "true"})
            return
    _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Done"})
    msg = cq.get("message") or {}
    _call(token, "editMessageText", {"chat_id": chat, "message_id": msg.get("message_id"),
                                     "text": (msg.get("text") or "")[:3800] + "\n\n" + done,
                                     "disable_web_page_preview": "true"})


def poll_loop(pause=4):
    """Background thread: read button presses for every channel's bot. Offsets live in
    /data/telegram_offsets.json, so a restart does not replay old presses."""
    import time
    path = DATA / "telegram_offsets.json"
    while True:
        offsets = pipeline._read_json(path, {}) or {}
        changed = False
        for token, channels in _bots().items():
            key = hashlib.sha256(token.encode()).hexdigest()[:16]
            try:
                updates = _call(token, "getUpdates", {"offset": offsets.get(key, 0), "timeout": 0, "limit": 50})
            except Exception as e:
                print(f"[warn] telegram poll: {e}", flush=True)
                continue
            for u in updates:
                offsets[key], changed = u["update_id"] + 1, True
                if u.get("message"):
                    _last_chat[token] = u["message"].get("chat")
                if u.get("callback_query"):
                    try:
                        _press(token, channels, u["callback_query"])
                    except Exception as e:
                        print(f"[warn] telegram button: {e}", flush=True)
        if changed:
            pipeline._write_json(path, offsets)
        time.sleep(pause)
