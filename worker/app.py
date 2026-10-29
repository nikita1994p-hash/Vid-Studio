"""HTTP face of the pipeline.

/login       sign in / sign up (static/login.html); everything else needs a session cookie
/            the Studio-style dashboard (static/)
/api/...     what the dashboard calls - always inside the signed-in user's open channel
/produce, /social/..., /jobs/<id>/...   what n8n calls (channel taken from the video, or every channel)
/files/<job>/<name>                     artifacts (video, images, voice)
"""
import hmac
import json
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import abtest
import auth
import earnings
import niches
import notify
import pipeline
import social
import space
import topics

app = FastAPI(title="Vid Studio worker")
_busy = threading.Lock()
COOKIE = "vs_session"
space.migrate()
for _cid in space.list_ids():
    with space.use(_cid):
        pipeline.recover_interrupted()


def _stats_loop():
    """Fetch the numbers every 6 hours even when nobody opens the Studio, so money and A/B alerts
    reach Telegram on time - for every channel."""
    import time
    while True:
        time.sleep(900)
        for cid in space.list_ids():
            try:
                with space.use(cid):
                    if earnings.stale(6):
                        earnings.refresh()
            except Exception as e:
                print(f"[warn] background stats refresh ({cid}): {e}", flush=True)


threading.Thread(target=_stats_loop, daemon=True).start()
threading.Thread(target=notify.poll_loop, daemon=True).start()   # Telegram button presses
STATIC = Path(__file__).parent / "static"
MEDIA = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".wav": "audio/wav",
         ".mp3": "audio/mpeg", ".json": "application/json", ".ass": "text/plain", ".txt": "text/plain"}


class ProduceRequest(BaseModel):
    topic: str | None = None            # empty = the model picks a fresh topic
    mock: bool | None = None            # None = use MOCK from .env
    use_video_hook: bool | None = None  # None = use USE_VIDEO_HOOK from .env
    options: dict | None = None         # settings for this video only (keys of the Settings page)
    script: str | None = None           # your own narration: line 1 = hook, then one line per scene
    variants: int = 1                   # 1-5 versions (A/B test): different hook, title, voice, music, pictures


class PublishRequest(BaseModel):
    mode: str = "private"               # private | schedule | public
    platforms: list[str] = ["youtube"]
    direct: bool = False                # post from the worker now instead of through n8n


class SocialPostRequest(BaseModel):
    job_id: str
    mode: str = "auto"


class SettingsRequest(BaseModel):
    changes: dict                       # {KEY: value}; null = back to the .env value


class PreviewRequest(BaseModel):
    text: str | None = None
    options: dict | None = None


class AppKeysRequest(BaseModel):
    client_id: str
    client_secret: str = ""             # empty keeps the saved secret


class PageRequest(BaseModel):
    page_id: str


class VariantsRequest(BaseModel):
    count: int = 1


class IncomeRequest(BaseModel):
    date: str | None = None             # YYYY-MM-DD, empty = today
    source: str = "Other"
    amount: float
    note: str = ""
    job_id: str | None = None


class TopicsRequest(BaseModel):
    lines: list[str] = []               # one topic per item
    top: bool = False                   # put them first in the queue


class TopicEditRequest(BaseModel):
    topic: str | None = None
    state: str | None = None            # todo | used | skipped


class OrderRequest(BaseModel):
    ids: list[str]


class SuggestRequest(BaseModel):
    count: int = 30


class TelegramRequest(BaseModel):
    bot_token: str | None = None        # empty keeps the saved token
    chat_id: str | None = None


class CoverRequest(BaseModel):
    lines: list[str] = []               # empty = the AI's cover lines
    scene: int | None = None            # picture of this scene; None = Settings -> Cover picture


class UploadedRequest(BaseModel):
    video_id: str
    mode: str | None = None


class RulesRequest(BaseModel):
    rules: str = ""                     # empty = back to the default rules


class MetaRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: str | None = None


class SignUpRequest(BaseModel):
    email: str
    password: str
    marketing: bool = False             # the separate 'news and offers' tick box, off by default


class SignInRequest(BaseModel):
    email: str
    password: str


class PasswordRequest(BaseModel):
    old: str = ""
    new: str


class MarketingRequest(BaseModel):
    marketing: bool


class ChannelRequest(BaseModel):
    name: str
    template: str = "business"


class ChannelEditRequest(BaseModel):
    name: str | None = None
    daily: bool | None = None


class UserEditRequest(BaseModel):
    disabled: bool | None = None
    password: str | None = None         # the owner sets a new password


def _fail(e):
    if isinstance(e, (auth.AuthError, space.SpaceError)):
        raise HTTPException(409 if "create a channel" in str(e) else 400, str(e))
    if isinstance(e, pipeline.PipelineError):
        raise HTTPException(400 if "bad job id" in str(e) or "no such" in str(e) else 500, str(e))
    traceback.print_exc()
    raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.exception_handler(space.SpaceError)
def _no_channel(request, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


# ------------------------------------------------------------------ sessions and channels

OPEN = ("/login", "/static/", "/api/auth/", "/health")
INTERNAL = ("/produce", "/social/", "/jobs/")   # n8n inside Docker; no browser, no cookie


def _job_in(path):
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
        return parts[2]
    if len(parts) >= 2 and parts[0] in ("files", "jobs"):
        return parts[1]
    return None


@app.middleware("http")
async def studio_session(request, call_next):
    """Who is asking and in which channel. A browser needs a session; the page or API call then runs
    inside the user's open channel - or the channel of the video in the URL, if it is theirs."""
    path = request.url.path
    if path.startswith(OPEN) or (path.startswith("/oauth/") and path.endswith("/callback")):
        return await call_next(request)
    if path.startswith(INTERNAL):
        token = pipeline.env("INTERNAL_TOKEN")   # set it once the Studio is on a server
        if token and not hmac.compare_digest(request.headers.get("x-internal-token", ""), token):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        cid = space.find_job(_job_in(path))
        if cid:
            space.set_current(cid)
        return await call_next(request)
    user, sess = auth.session(request.cookies.get(COOKIE))
    if not user:
        if path == "/":
            return RedirectResponse("/login")
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    request.state.user = user
    job_cid = space.find_job(_job_in(path))
    if job_cid and not auth.may_open(user, job_cid):
        return JSONResponse({"detail": "no such video"}, status_code=404)
    own = [c["id"] for c in auth.channels_of(user)]
    cid = job_cid or (sess.get("channel") if sess.get("channel") in own else own[0] if own else space.NONE)
    space.set_current(cid)
    return await call_next(request)


def _user(request):
    return request.state.user


def _owner(request):
    if not auth.is_admin(_user(request)):
        raise HTTPException(403, "only the Studio owner or a super admin can do this")
    return _user(request)


def _managed(request, user_id):
    """The admin, if they may change this account (not themselves, not another super admin)."""
    admin = _owner(request)
    if not auth.can_manage(admin, auth.get(user_id)):
        raise HTTPException(403, "you cannot change this account")
    return admin


def _sign_in(response, user, channel=None):
    own = [c["id"] for c in auth.channels_of(user)]
    token = auth.start_session(user["id"], channel or (own[0] if own else None))
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=pipeline.env("PUBLIC_BASE_URL", "").startswith("https://"))


@app.get("/login")
def login_page():
    return HTMLResponse((STATIC / "login.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/api/auth/state")
def auth_state(request: Request):
    user, _ = auth.session(request.cookies.get(COOKIE))
    return {"signed_in": bool(user), "first_account": not auth.has_users()}


@app.post("/api/auth/register")
def register(req: SignUpRequest, response: Response):
    try:
        user = auth.register(req.email, req.password, req.marketing)
    except Exception as e:
        _fail(e)
    _sign_in(response, user)
    return {"user": auth.public(user)}


@app.post("/api/auth/login")
def login(req: SignInRequest, response: Response):
    try:
        user = auth.authenticate(req.email, req.password)
    except Exception as e:
        _fail(e)
    _sign_in(response, user)
    return {"user": auth.public(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"signed_out": True}


@app.get("/api/me")
def me(request: Request):
    user = _user(request)
    try:
        current = space.current()
    except space.SpaceError:
        current = None
    return {"user": auth.public(user), "channels": auth.channels_of(user), "channel": current,
            "templates": {k: v["label"] for k, v in niches.TEMPLATES.items()}}


@app.put("/api/me/password")
def change_password(req: PasswordRequest, request: Request, response: Response):
    try:
        auth.change_password(_user(request)["id"], req.old, req.new)
    except Exception as e:
        _fail(e)
    _sign_in(response, _user(request), space.current() if space.list_ids() else None)
    return {"changed": True}


@app.put("/api/me/marketing")
def change_marketing(req: MarketingRequest, request: Request):
    return auth.public(auth.set_marketing(_user(request)["id"], req.marketing))


@app.post("/api/channels")
def create_channel(req: ChannelRequest, request: Request):
    """A new channel from a template: its settings, script rules and starter topics."""
    tpl = niches.template(req.template)
    try:
        meta = space.create(req.name, _user(request)["id"], req.template, tpl["settings"])
        with space.use(meta["id"]):
            if tpl["rules"]:
                pipeline.save_script_rules(tpl["rules"])
            topics.load_starter()
    except Exception as e:
        _fail(e)
    auth.set_session_channel(request.cookies.get(COOKIE), meta["id"])
    return meta


def _mine(request, cid):
    if not auth.may_open(_user(request), cid):
        raise HTTPException(404, "no such channel")


@app.post("/api/channels/{cid}/open")
def open_channel(cid: str, request: Request):
    _mine(request, cid)
    auth.set_session_channel(request.cookies.get(COOKIE), cid)
    return space.get(cid)


@app.patch("/api/channels/{cid}")
def edit_channel(cid: str, req: ChannelEditRequest, request: Request):
    _mine(request, cid)
    try:
        return space.update(cid, **{k: v for k, v in req.model_dump().items() if v is not None})
    except Exception as e:
        _fail(e)


@app.delete("/api/channels/{cid}")
def delete_channel(cid: str, request: Request, confirm: str = ""):
    """Deletes every video, setting and login of the channel: the name must be typed to confirm."""
    _mine(request, cid)
    if confirm.strip() != space.get(cid)["name"]:
        raise HTTPException(400, "type the channel name to confirm")
    space.delete(cid)
    return {"deleted": cid}


@app.get("/api/users")
def users(request: Request, q: str = ""):
    """Every account (q: part of an e-mail or id) with its channel names and what this admin may do."""
    admin = _owner(request)
    names = {}
    for c in space.all_channels():
        names.setdefault(c.get("owner"), []).append(c["name"])
    full = {u["id"]: u for u in auth._users()}
    return [{**u, "channels": names.get(u["id"], []), "manageable": auth.can_manage(admin, full[u["id"]])}
            for u in auth.list_users(q)]


@app.patch("/api/users/{user_id}")
def edit_user(user_id: str, req: UserEditRequest, request: Request):
    _managed(request, user_id)
    try:
        if req.disabled is not None:
            auth.set_disabled(user_id, req.disabled)
        if req.password:
            auth.set_password(user_id, req.password)
    except Exception as e:
        _fail(e)
    return auth.public(auth.get(user_id))


@app.post("/api/users/{user_id}/logout")
def logout_user(user_id: str, request: Request):
    """Sign the user out of the Studio on every device."""
    _managed(request, user_id)
    auth.end_sessions(user_id)
    return {"signed_out": user_id}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, request: Request):
    """The account goes; its channels stay on disk and move to the admin who deleted it."""
    owner = _managed(request, user_id)
    try:
        auth.delete(user_id)
    except Exception as e:
        _fail(e)
    for c in space.all_channels():
        if c.get("owner") == user_id:
            space.update(c["id"], owner=owner["id"])
    return {"deleted": user_id}


@app.get("/api/users/marketing.csv")
def marketing_list(request: Request):
    _owner(request)
    return Response(auth.marketing_csv(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=vid-studio-subscribers.csv"})


# ------------------------------------------------------------------ dashboard

@app.get("/")
def index():
    # stamp asset URLs with their change time: a browser that cached an older app.js or
    # style.css has to fetch the new one, so an update never runs with stale dashboard code
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        stamp = int((STATIC / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_dashboard(request, call_next):
    """Make the browser re-check dashboard files on every load (cheap: ETag answers 304), so an
    update never leaves an old style.css or app.js running."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
def alive():
    """Docker's health check: no session, so nothing about any channel."""
    return {"ok": True, "busy": _busy.locked()}


@app.get("/api/health")
def health():
    return {**pipeline.health(), "busy": _busy.locked()}


@app.get("/api/script-rules")
def get_rules():
    return {"rules": pipeline.script_rules(raw=True), "default": pipeline.DEFAULT_RULES}


@app.put("/api/script-rules")
def put_rules(req: RulesRequest):
    return {"rules": pipeline.save_script_rules(req.rules)}


@app.get("/api/jobs")
def jobs():
    return pipeline.list_jobs()


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    try:
        return pipeline.load_job(job_id)
    except Exception as e:
        _fail(e)


@app.post("/api/jobs")
def start_job(req: ProduceRequest | None = None):
    """Start a video in the background and return at once; the dashboard polls its status."""
    req = req or ProduceRequest()
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now")
    job_id = pipeline.new_job_id()
    pipeline.Job(job_id, mock=pipeline.flag("MOCK") if req.mock is None else req.mock, topic=req.topic)

    extra = max(0, min(4, (req.variants or 1) - 1))

    def run():
        try:
            result = pipeline.produce(topic=(req.topic or "").strip() or None, mock=req.mock,
                                      use_video_hook=req.use_video_hook, job_id=job_id,
                                      options=req.options, script=req.script)
            if extra:
                pipeline.make_variants(result["job_id"], extra)
            # a story split into parts: make the remaining parts right away, one video each
            while result.get("next_part"):
                nxt = pipeline.new_job_id()
                pipeline.Job(nxt, topic="next part")
                result = pipeline.produce(mock=False, use_video_hook=req.use_video_hook, job_id=nxt,
                                          options=req.options)
                if extra:
                    pipeline.make_variants(result["job_id"], extra)
        except Exception:
            traceback.print_exc()  # already recorded in status.json by produce()
        finally:
            _busy.release()

    space.thread(run)
    return {"job_id": job_id}


@app.patch("/api/jobs/{job_id}")
def edit_job(job_id: str, req: MetaRequest):
    try:
        return pipeline.update_meta(job_id, req.title, req.description, req.tags)
    except Exception as e:
        _fail(e)


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str):
    try:
        pipeline.delete_job(job_id)
        return {"deleted": job_id}
    except Exception as e:
        _fail(e)


@app.post("/api/upload")
def upload(file: UploadFile = File(...), title: str = Form(...), description: str = Form(""),
           tags: str = Form("")):
    try:
        return pipeline.create_upload(file.file, file.filename, title, description, tags)
    except Exception as e:
        _fail(e)


@app.post("/api/jobs/{job_id}/scenes/{idx}/image")
def scene_image(job_id: str, idx: int, file: UploadFile = File(...)):
    try:
        return pipeline.replace_scene_image(job_id, idx, file.file, file.filename)
    except Exception as e:
        _fail(e)


@app.post("/api/jobs/{job_id}/rerender")
def rerender(job_id: str):
    """Rebuild the video in the background after pictures were replaced."""
    try:
        pipeline.load_scenes(pipeline.job_dir(job_id))
    except Exception as e:
        _fail(e)
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now")

    def run():
        try:
            pipeline.rerender(job_id)
        except Exception:
            traceback.print_exc()  # recorded in status.json by rerender()
        finally:
            _busy.release()

    space.thread(run)
    return {"rerendering": job_id}


@app.post("/api/jobs/{job_id}/variants")
def variants(job_id: str, req: VariantsRequest):
    """More versions of a finished video, made in the background."""
    try:
        if pipeline.load_job(job_id)["status"]["state"] != "done":
            raise pipeline.PipelineError("the video is not finished yet")
    except Exception as e:
        _fail(e)
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now")

    def run():
        try:
            pipeline.make_variants(job_id, req.count)
        except Exception:
            traceback.print_exc()
        finally:
            _busy.release()

    space.thread(run)
    return {"making": max(0, min(4, req.count))}


@app.post("/api/jobs/{job_id}/publish")
def publish(job_id: str, req: PublishRequest):
    """Post the video to the chosen platforms: normally through n8n (which keeps a log of every
    run), or straight from the worker with direct=true."""
    if req.mode not in ("private", "schedule", "public"):
        raise HTTPException(400, "mode must be private, schedule or public")
    platforms = [p for p in dict.fromkeys(req.platforms) if p in social.PLATFORMS]
    if not platforms:
        raise HTTPException(400, "choose at least one platform")
    try:
        pipeline.load_job(job_id)
        missing = [social.PLATFORMS[p]["name"] for p in platforms if not social._account(p)]
        if missing:
            raise social.SocialError("not connected: " + ", ".join(missing) + " (Accounts page)")
    except Exception as e:
        _fail(e)
    if req.direct:
        def run():
            for p in platforms:
                try:
                    social.post(p, job_id, req.mode)
                except Exception:
                    traceback.print_exc()  # recorded in the video's status

        social.mark_queued(job_id, platforms, req.mode)
        space.thread(run)
        return {"queued": job_id, "platforms": platforms, "via": "worker"}
    url = pipeline.env("N8N_PUBLISH_WEBHOOK", "http://n8n:5678/webhook/vidauto-publish")
    body = json.dumps({"job_id": job_id, "mode": req.mode, "platforms": platforms}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=15).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(502, "n8n does not know the publish webhook yet: open the workflow "
                                     "'VidAuto: Publish' in n8n and press Publish - or tick 'Post directly'")
        raise HTTPException(502, f"n8n answered {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"cannot reach n8n: {e.reason}")
    social.mark_queued(job_id, platforms, req.mode)
    return {"queued": job_id, "platforms": platforms, "via": "n8n"}


# ------------------------------------------------------------------ settings

@app.get("/api/settings")
def get_settings():
    return {"fields": pipeline.settings_view()}


@app.put("/api/settings")
def put_settings(req: SettingsRequest):
    try:
        pipeline.save_settings(req.changes)
    except Exception as e:
        _fail(e)
    return {"fields": pipeline.settings_view()}


@app.get("/api/voices")
def voices():
    try:
        return pipeline.list_voices()
    except Exception as e:
        raise HTTPException(502, f"cannot load the voice list from Microsoft: {e}")


@app.post("/api/preview/voice")
def preview_voice(req: PreviewRequest):
    try:
        return FileResponse(pipeline.voice_preview(req.text, req.options), media_type="audio/wav",
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        _fail(e)


@app.post("/api/preview/captions")
def preview_captions(req: PreviewRequest):
    try:
        return FileResponse(pipeline.caption_preview(req.options), media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        _fail(e)


# ------------------------------------------------------------------ earnings

@app.get("/api/earnings")
def get_earnings():
    return earnings.summary()


@app.post("/api/earnings/refresh")
def refresh_earnings():
    try:
        earnings.refresh()
    except Exception as e:
        _fail(e)
    return earnings.summary()


@app.post("/api/earnings/income")
def add_income(req: IncomeRequest):
    try:
        return earnings.add_income(req.date, req.source, req.amount, req.note, req.job_id)
    except Exception as e:
        _fail(e)


@app.delete("/api/earnings/income/{entry_id}")
def delete_income(entry_id: str):
    try:
        earnings.delete_income(entry_id)
        return {"deleted": entry_id}
    except Exception as e:
        _fail(e)


# ------------------------------------------------------------------ topics queue

@app.get("/api/topics")
def get_topics():
    return topics.summary()


@app.post("/api/topics")
def add_topics(req: TopicsRequest):
    return topics.add(req.lines, req.top)


@app.post("/api/topics/starter")
def starter_topics():
    return topics.load_starter()


@app.post("/api/topics/suggest")
def suggest_topics(req: SuggestRequest):
    try:
        return topics.suggest(req.count)
    except Exception as e:
        _fail(e)


@app.post("/api/topics/check")
def check_topics():
    try:
        return topics.check()
    except pipeline.PipelineError as e:
        raise HTTPException(502, str(e))


@app.put("/api/topics/order")
def order_topics(req: OrderRequest):
    topics.reorder(req.ids)
    return topics.summary()


@app.patch("/api/topics/{item_id}")
def edit_topic(item_id: str, req: TopicEditRequest):
    try:
        return topics.update(item_id, req.topic, req.state)
    except Exception as e:
        _fail(e)


@app.delete("/api/topics/{item_id}")
def delete_topic(item_id: str):
    try:
        topics.delete(item_id)
        return {"deleted": item_id}
    except Exception as e:
        _fail(e)


@app.delete("/api/topics")
def clear_topics(state: str = "used"):
    topics.clear(state)
    return topics.summary()


# ------------------------------------------------------------------ telegram

@app.get("/api/notify")
def get_notify():
    return notify.status()


@app.put("/api/notify")
def put_notify(req: TelegramRequest):
    try:
        return notify.save(req.bot_token, req.chat_id)
    except Exception as e:
        _fail(e)


@app.post("/api/notify/find-chat")
def find_chat():
    try:
        return notify.find_chat()
    except Exception as e:
        _fail(e)


@app.post("/api/notify/test")
def test_notify():
    try:
        return notify.test()
    except Exception as e:
        _fail(e)


@app.delete("/api/notify")
def forget_notify():
    return notify.forget()


# ------------------------------------------------------------------ A/B results

@app.get("/api/ab")
def ab_results():
    return abtest.summary()


@app.post("/api/jobs/{job_id}/cover")
def remake_cover(job_id: str, req: CoverRequest):
    try:
        return pipeline.cover(job_id, req.lines, req.scene)
    except Exception as e:
        _fail(e)


# ------------------------------------------------------------------ accounts

def _base(request: Request):
    return pipeline.env("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")


@app.get("/api/accounts")
def get_accounts(request: Request):
    return social.accounts(_base(request))


@app.put("/api/accounts/{platform}/app")
def put_app_keys(platform: str, req: AppKeysRequest):
    try:
        return social.save_app(platform, req.client_id, req.client_secret)
    except Exception as e:
        _fail(e)


@app.post("/api/accounts/{platform}/logout")
def account_logout(platform: str):
    try:
        return social.logout(platform)
    except Exception as e:
        _fail(e)


@app.post("/api/accounts/{platform}/page")
def account_page(platform: str, req: PageRequest):
    try:
        return social.choose_page(platform, req.page_id)
    except Exception as e:
        _fail(e)


@app.get("/oauth/{platform}/start")
def oauth_start(platform: str, request: Request):
    try:
        return RedirectResponse(social.start_login(platform, _base(request)))
    except Exception as e:
        return RedirectResponse("/#/accounts?error=" + urllib.parse.quote(str(e)[:300]))


@app.get("/oauth/{platform}/callback")
def oauth_callback(platform: str, code: str = "", state: str = "", error: str = "",
                   error_description: str = ""):
    if error or not code:
        msg = error_description or error or "the platform sent no login code"
        return RedirectResponse("/#/accounts?error=" + urllib.parse.quote(msg[:300]))
    try:
        social.finish_login(platform, code, state)
    except Exception as e:
        traceback.print_exc()
        return RedirectResponse("/#/accounts?error=" + urllib.parse.quote(str(e)[:300]))
    return RedirectResponse(f"/#/accounts?connected={platform}")


# ------------------------------------------------------------------- n8n side

@app.post("/produce")
def produce(req: ProduceRequest | None = None):
    """n8n's daily run: one video for every channel with 'Daily' on, one after another.
    Returns {videos: [{job_id, channel, platforms}]} - the platforms each channel posts to."""
    req = req or ProduceRequest()
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now")
    videos, errors = [], []
    try:
        for meta in space.all_channels():
            if not meta.get("daily", True):
                continue
            try:
                with space.use(meta["id"]):
                    result = pipeline.produce(topic=(req.topic or "").strip() or None,
                                              mock=req.mock, use_video_hook=req.use_video_hook)
                    videos.append({"job_id": result["job_id"], "channel": meta["id"],
                                   "platforms": social.default_platforms()})
            except Exception as e:  # one channel's failure must not cost the others their video
                traceback.print_exc()
                errors.append(f"{meta['name']}: {str(e)[:200]}")
    finally:
        _busy.release()
    if not videos and errors:
        raise HTTPException(500, " | ".join(errors))
    return {"videos": videos, "errors": errors, "job_id": videos[0]["job_id"] if videos else None}


@app.post("/social/{platform}/post")
def social_post(platform: str, req: SocialPostRequest):
    """n8n: upload one finished video to one platform (takes a while; n8n waits)."""
    cid = space.find_job(req.job_id)
    if not cid:
        raise HTTPException(404, "no such video")
    try:
        with space.use(cid):
            return social.post(platform, req.job_id, req.mode)
    except Exception as e:
        _fail(e)


@app.get("/social/defaults")
def social_defaults():
    """n8n's daily run: the platforms to post to (DEFAULT_PLATFORMS that are connected)."""
    return {"platforms": social.default_platforms()}


@app.get("/jobs/{job_id}/publish-data")
def publish_data(job_id: str, mode: str = "auto"):
    try:
        return pipeline.publish_data(job_id, mode)
    except Exception as e:
        _fail(e)


@app.post("/jobs/{job_id}/uploaded")
def uploaded(job_id: str, req: UploadedRequest):
    try:
        return pipeline.mark_uploaded(job_id, req.video_id, req.mode)
    except Exception as e:
        _fail(e)


# ---------------------------------------------------------------------- files

@app.api_route("/files/{job_id}/{name}", methods=["GET", "HEAD"])
def files(job_id: str, name: str, download: bool = False):
    try:
        folder = pipeline.job_dir(job_id)
    except Exception as e:
        _fail(e)
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad path")
    path = folder / name
    if not path.is_file():
        raise HTTPException(404, "no such file")
    media = MEDIA.get(path.suffix.lower())
    if download:
        return FileResponse(path, media_type=media, filename=f"{job_id}-{name}")
    return FileResponse(path, media_type=media)
