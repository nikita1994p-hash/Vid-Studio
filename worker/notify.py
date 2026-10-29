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


def _keyboard(buttons):
    return json.dumps({"inline_keyboard": [
        [{"text": label, "callback_data": data[:64]} for label, data in row] for row in buttons]})


def _send_now(text, buttons=None):
    c = _config()
    params = {"chat_id": c["chat_id"], "text": text[:4000], "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = _keyboard(buttons)
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


def broadcast(event, text):
    """An alert that belongs to no single channel (n8n failed): once to every bot + chat in use."""
    sent = set()
    for token, channels in _bots().items():
        for cid, chat in channels:
            if (token, chat) in sent:
                continue
            with space.use(cid):
                if send(event, text):
                    sent.add((token, chat))
    return len(sent)


on_variants = None   # app.start_variants(job_id, count): set by app.py, which owns the "one video at a time" lock
COMMANDS = [("menu", "Last video: A/B versions, upload, publish")]


def _last_video():
    """The newest finished original video of the current channel (not a version of another one)."""
    for j in pipeline.list_jobs():
        if j["state"] == "done" and not j["mock"] and j["kind"] != "upload" and j["variant"] in (None, "A"):
            return j
    return None


def _youtube_line(yt):
    if yt.get("video_id"):
        mode = yt.get("mode") or "private"
        return f"YouTube: {mode} — {yt.get('url')}"
    return {"queued": "YouTube: waiting to upload", "uploading": "YouTube: uploading…"}.get(
        yt.get("state"), "YouTube: not uploaded")


def menu(cid):
    """The /menu message of one channel: its last video and what can be done with it."""
    with space.use(cid):
        name = space.get(cid).get("name") or "Channel"
        last = _last_video()
        if not last:
            return f"🎛 {name}\nNo finished video yet.", [[("🔄 Refresh", f"mnu|{cid}")]]
        job = last["job_id"]
        versions = sorted(j["variant"] for j in pipeline.list_jobs()
                          if j["variant"] not in (None, "A") and j["state"] == "done"
                          and (pipeline._read_json(pipeline.job_dir(j["job_id"]) / "status.json") or {}).get("variant_of") == job)
        yt = last.get("youtube") or {}
        lines = [f"🎛 {name}", f"Last video: “{last['title']}”", _youtube_line(yt),
                 "A/B versions: " + (", ".join(versions) if versions else "none yet"), link(job)]
        if pipeline.flag("VARIANT_NEW_IMAGES", "true"):
            lines.append("Each version draws its own pictures: about 1/10 of the free daily FLUX limit.")
        rows = [[("🅱 +1 A/B version", f"ab1|{job}"), ("🅱 +2 versions", f"ab2|{job}")]]
        if not yt.get("video_id") and yt.get("state") not in ("queued", "uploading"):
            rows.append([("📤 Upload to YouTube (private)", f"upl|{job}")])
        elif yt.get("video_id") and (yt.get("mode") or "private") == "private":
            rows.append([("🌍 Publish now", f"pub|{job}")])
        rows.append([("🔄 Refresh", f"mnu|{cid}")])
        return "\n".join(lines), rows


def _upload(job_id):
    """The 'Upload' button: a private YouTube upload in the background; the usual
    '📤 YouTube … waiting for your OK' message with Publish / Delete follows when it is done."""
    import social
    yt = pipeline.load_job(job_id)["status"].get("youtube") or {}
    if yt.get("video_id"):
        return "Already on YouTube: " + (yt.get("url") or "")
    if yt.get("state") in ("queued", "uploading"):
        return "Already uploading"
    if not social._account("youtube"):
        raise PipelineError("YouTube is not connected (Accounts page)")
    social.mark_queued(job_id, ["youtube"], "private")

    def run():
        try:
            social.post("youtube", job_id, "private")
        except Exception as e:
            print(f"[warn] telegram upload: {e}", flush=True)   # post() already sent the error alert

    space.thread(run)
    return "📤 Uploading privately… the Publish button comes in the next message"


def _press(token, channels, cq):
    """One button press. 'pub|<job>' makes the private upload public, 'del|<job>' deletes it,
    'upl|<job>' uploads it privately, 'ab1|<job>' / 'ab2|<job>' make A/B versions,
    'mnu|<channel>' refreshes the menu, 'tst|<channel>' only answers (the test message).
    Only the chat the alert went to may press it."""
    import social
    action, _, arg = (cq.get("data") or "").partition("|")
    chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
    cid = arg if action in ("tst", "mnu") else space.find_job(arg)
    if not cid or (cid, chat) not in channels:
        _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Not allowed here"})
        return
    msg = cq.get("message") or {}
    with space.use(cid):
        try:
            if action == "mnu":
                text, rows = menu(cid)
                _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Updated"})
                try:
                    _call(token, "editMessageText", {"chat_id": chat, "message_id": msg.get("message_id"), "text": text,
                                                     "reply_markup": _keyboard(rows), "disable_web_page_preview": "true"})
                except PipelineError:
                    pass   # "message is not modified": nothing changed since the last refresh
                return
            if action == "tst":
                done = "✅ The button works: Vid Studio got your press."
            elif action == "pub":
                done = "🌍 Published: " + social.youtube_make_public(arg)
            elif action == "del":
                social.youtube_delete(arg)
                done = "🗑 Deleted from YouTube (the video stays in the Studio)"
            elif action == "upl":
                done = _upload(arg)
            elif action in ("ab1", "ab2"):
                if not on_variants:
                    raise PipelineError("the Studio is still starting - try again in a minute")
                n = on_variants(arg, int(action[2]))
                done = (f"🅱 Making {n} A/B version(s), about 5-10 min each. "
                        "You will get a message with Upload buttons when they are ready.")
            else:
                return
        except Exception as e:
            _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": str(e)[:190],
                                                 "show_alert": "true"})
            return
    _call(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Done"})
    _call(token, "editMessageText", {"chat_id": chat, "message_id": msg.get("message_id"),
                                     "text": (msg.get("text") or "")[:3800] + "\n\n" + done,
                                     "disable_web_page_preview": "true"})


def _command(token, channels, message):
    """/menu (or /start) typed in a chat that gets alerts: one menu per channel using this chat."""
    text = (message.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
    if text not in ("/menu", "/start", "/ab"):
        return
    chat = str(message.get("chat", {}).get("id", ""))
    for cid, c in channels:
        if c != chat:
            continue
        body, rows = menu(cid)
        _call(token, "sendMessage", {"chat_id": chat, "text": body[:4000], "reply_markup": _keyboard(rows),
                                     "disable_web_page_preview": "true"})


_commands_set = set()


def _set_commands(token):
    """The Telegram 'Menu' button next to the message box lists /menu."""
    if token in _commands_set:
        return
    _call(token, "setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])})
    _commands_set.add(token)


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
                _set_commands(token)
                updates = _call(token, "getUpdates", {"offset": offsets.get(key, 0), "timeout": 0, "limit": 50})
            except Exception as e:
                print(f"[warn] telegram poll: {e}", flush=True)
                continue
            for u in updates:
                offsets[key], changed = u["update_id"] + 1, True
                if u.get("message"):
                    _last_chat[token] = u["message"].get("chat")
                    try:
                        _command(token, channels, u["message"])
                    except Exception as e:
                        print(f"[warn] telegram command: {e}", flush=True)
                if u.get("callback_query"):
                    try:
                        _press(token, channels, u["callback_query"])
                    except Exception as e:
                        print(f"[warn] telegram button: {e}", flush=True)
        if changed:
            pipeline._write_json(path, offsets)
        time.sleep(pause)
