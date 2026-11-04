"""HTTP face of the pipeline.

/login       sign in / sign up (static/login.html); everything else needs a session cookie
/            the Studio-style dashboard (static/)
/api/...     what the dashboard calls - always inside the signed-in user's open channel
/produce, /social/..., /jobs/<id>/...   what n8n calls (channel taken from the video, or every channel)
/files/<job>/<name>                     artifacts (video, images, voice)
"""
import datetime
import hmac
import json
import secrets
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import abtest
import auth
import earnings
import mail
import niches
import notify
import pipeline
import plans
import social
import space
import topics

app = FastAPI(title="Vid Studio worker", docs_url=None, redoc_url=None, openapi_url=None)   # no public API map
_busy = threading.Lock()
auth.alert = notify.security     # locked accounts, backup codes used, two-step switched off -> Telegram
# cookies ignore the port: the sandbox on localhost:8001 must not overwrite the real Studio's login
COOKIE = "vs_sandbox" if pipeline.SANDBOX else "vs_session"
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


def _redo_loop(pause=1200, give_up=48):
    """Work through data/redo_pictures.json: videos waiting for FLUX pictures. Each one is tried every
    20 minutes from its not_before time until FLUX answers (or 48 tries); Telegram says when it is done."""
    import time
    while True:
        time.sleep(60)
        queue = pipeline._read_json(pipeline.REDO_QUEUE, []) or []
        due = [x for x in queue if x["not_before"] <= pipeline._now()]
        if not due or not _busy.acquire(blocking=False):
            continue
        try:
            for item in due:
                try:
                    with space.use(item["channel"]):
                        made = pipeline.redo_pictures(item["job"])
                        title = (pipeline._read_json(pipeline.job_dir(item["job"]) / "result.json") or {}).get("title")
                        notify.send("done", f"🖼 AI pictures added ({made}) and the video re-rendered: {title}\n"
                                            f"Check it, then publish.\n{notify.link(item['job'])}")
                    queue = [x for x in queue if x["job"] != item["job"]]
                except Exception as e:
                    item["tries"] += 1
                    item["not_before"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + pause))
                    item["error"] = str(e)[:200]
                    print(f"[warn] redo pictures {item['job']}: try {item['tries']}: {str(e)[:120]}", flush=True)
                    if item["tries"] >= give_up:
                        queue = [x for x in queue if x["job"] != item["job"]]
                pipeline._write_json(pipeline.REDO_QUEUE, queue)
        finally:
            _busy.release()


if not pipeline.SANDBOX:   # the test copy must not answer the real bot's buttons or spend the picture quota
    threading.Thread(target=_stats_loop, daemon=True).start()
    threading.Thread(target=notify.poll_loop, daemon=True).start()   # Telegram button presses
    threading.Thread(target=_redo_loop, daemon=True).start()         # FLUX pictures for card-only videos
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
    choose: bool = False                # stop after the script: the creator picks the hook and title


class ChooseRequest(BaseModel):
    hook: str
    title: str
    variants: list[dict] = []           # [{hook, title}] for the A/B versions B, C, ... (empty = the Studio picks)


class PublishRequest(BaseModel):
    mode: str = "private"               # private | schedule | public
    platforms: list[str] = ["youtube"]
    direct: bool = False                # post from the worker now instead of through n8n
    tiktok: dict | None = None          # the TikTok form: privacy, title, comment/duet/stitch, disclose/your_brand/branded


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
    config_id: str = ""                 # Instagram's Facebook Login for Business configuration
    account_id: str = ""                # Instagram account ID, when the login cannot list it


class PageRequest(BaseModel):
    page_id: str


class VariantsRequest(BaseModel):
    count: int = 1
    picks: list[dict] | None = None     # [{hook, title}] per new version (empty = next-best hook and title)


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
    invite: str | None = None           # an invite link's code: the account starts with that plan
    source: dict | None = None          # ?ref= / utm_* of the sign-up link and the referring site
    timezone: str | None = None         # the browser's own time zone and language
    language: str | None = None


class SignInRequest(BaseModel):
    email: str
    password: str
    timezone: str | None = None
    language: str | None = None


class PasswordRequest(BaseModel):
    old: str = ""
    new: str


class CodeRequest(BaseModel):
    ticket: str = ""                    # from the password step
    code: str = ""                      # 6 digits from the authenticator app, or a backup code
    timezone: str | None = None
    language: str | None = None


class ResetRequest(BaseModel):
    email: str = ""


class ResetFinishRequest(BaseModel):
    token: str
    password: str


class TwoStepRequest(BaseModel):
    code: str = ""
    password: str = ""


class MarketingRequest(BaseModel):
    marketing: bool
    reason: str | None = None           # why they unsubscribed (optional)


class ChannelRequest(BaseModel):
    name: str
    template: str = "business"


class ChannelEditRequest(BaseModel):
    name: str | None = None
    daily: bool | None = None


class InviteRequest(BaseModel):
    plan: str = "trial"
    uses: int = 1
    days: int = 14
    note: str = ""


class UserEditRequest(BaseModel):
    disabled: bool | None = None
    password: str | None = None         # an admin sets a new password
    role: str | None = None             # user | admin | owner (super admin / owner only)
    plan: str | None = None             # free | trial | pro (super admin / owner only)


def _oops(e):
    """An unexpected error: the full text goes to the log under a short reference, the browser gets
    only the reference (no file paths, settings or library internals)."""
    ref = secrets.token_hex(3)
    print(f"[error {ref}] {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    return f"something went wrong (ref {ref}) - try again; if it repeats, tell the Studio admin this ref"


def _fail(e):
    if isinstance(e, (auth.AuthError, space.SpaceError)):
        raise HTTPException(409 if "create a channel" in str(e) else 400, str(e))
    if isinstance(e, pipeline.PipelineError):   # the Studio's own messages: safe and useful to show
        raise HTTPException(400 if "bad job id" in str(e) or "no such" in str(e) else 500, str(e))
    raise HTTPException(500, _oops(e))


@app.exception_handler(space.SpaceError)
def _no_channel(request, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(Exception)
def _unexpected(request, e):
    return JSONResponse({"detail": _oops(e)}, status_code=500)


# ------------------------------------------------------------------ sessions and channels

OPEN = ("/login", "/pricing", "/static/", "/api/auth/", "/health", "/tiktok")
INTERNAL = ("/produce", "/social/", "/jobs/", "/n8n/")   # n8n inside Docker; no browser, no cookie


def _from_public_proxy(request):
    """True when the request came in through the public tunnel (the Tailscale container, PUBLIC_PROXY_HOST)."""
    host = pipeline.env("PUBLIC_PROXY_HOST")
    if not host or not request.client:
        return False
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError:      # the tunnel container is not running: nothing can come through it
        return False
    return request.client.host in ips


def _client_ip(request):
    """The visitor's address: behind the public tunnel it is the last X-Forwarded-For entry (the one the
    tunnel added; earlier ones could be made up by the visitor)."""
    if _from_public_proxy(request):
        forwarded = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "?"


_hits = {}   # (what, address) -> recent request times
_hits_lock = threading.Lock()


def _throttle(request, what, limit, seconds):
    """At most `limit` requests per `seconds` from one address, so bots cannot guess passwords or mass-sign-up."""
    key, now = (what, _client_ip(request)), time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(key, []) if now - t < seconds]
        if len(recent) >= limit:
            notify.security("throttle", f"🚦 {limit} {what} attempts in {seconds // 60} min from one address "
                                        f"({key[1]}) - it is paused. Someone may be guessing passwords.", f"{what}|{key[1]}")
            raise HTTPException(429, "too many attempts from your network - wait a few minutes and try again")
        _hits[key] = recent + [now]
        if len(_hits) > 10000:   # forget addresses that went quiet
            for k in [k for k, v in _hits.items() if now - v[-1] > 3600]:
                _hits.pop(k, None)


# what an upload may be: its first bytes, not just its name, and its size
UPLOADS = {"video": ((b"ftyp", 4), (b"\x1a\x45\xdf\xa3", 0)),
           "image": ((b"\x89PNG", 0), (b"\xff\xd8\xff", 0), (b"WEBP", 8))}
UPLOAD_MB = {"video": 500, "image": 20}


def _check_upload(file, kind):
    f = file.file
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    head = f.read(16)
    f.seek(0)
    if size > UPLOAD_MB[kind] * 1024 * 1024:
        raise HTTPException(413, f"the file is larger than {UPLOAD_MB[kind]} MB")
    if not any(head[at:at + len(magic)] == magic for magic, at in UPLOADS[kind]):
        raise HTTPException(400, f"this file is not a {'video (.mp4, .mov, .webm, .mkv)' if kind == 'video' else 'picture (.png, .jpg, .webp)'}")


def _inline_hashes():
    """CSP hashes of the inline scripts in the login and pricing pages, so no other inline script runs."""
    import base64
    import hashlib
    import re
    out = []
    for name in ("login.html", "pricing.html"):
        for body in re.findall(r"<script>(.*?)</script>", (STATIC / name).read_text(encoding="utf-8"), re.S):
            out.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return " ".join(out)


def _security_headers():
    csp = ("default-src 'self'; "
           f"script-src 'self' {_inline_hashes()}; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
           "img-src 'self' data: blob: https:; media-src 'self' blob:; connect-src 'self'; "
           "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    headers = {"Content-Security-Policy": csp, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "strict-origin-when-cross-origin",
               "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}
    if pipeline.env("PUBLIC_BASE_URL", "").startswith("https://"):
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers


UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


def _foreign_origin(request):
    """True for a change (POST/PUT/PATCH/DELETE) a browser sends from another site's page: a second lock
    against cross-site requests next to the SameSite cookie. Requests without Origin (n8n, curl) pass."""
    if request.method not in UNSAFE:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return request.headers.get("sec-fetch-site") == "cross-site"
    host = urllib.parse.urlsplit(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower(), request.headers.get("x-forwarded-host", "").lower(),
               urllib.parse.urlsplit(pipeline.env("PUBLIC_BASE_URL", "")).netloc.lower()}
    return host not in allowed - {""}


# what an owner / super admin without two-step sign-in may still open: the page, their account, signing out
TWO_STEP_OPEN = ("/api/me", "/api/auth/", "/api/health")


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
    if _foreign_origin(request) and not path.startswith(INTERNAL):
        return JSONResponse({"detail": "this request came from another website and was refused"}, status_code=403)
    if path.startswith(OPEN) or (path.startswith("/oauth/") and path.endswith("/callback")):
        return await call_next(request)
    parts = path.split("/")
    if (path.startswith("/files/") and len(parts) == 4 and request.query_params.get("sig")
            and auth.file_signed(parts[2], parts[3], request.query_params.get("exp"), request.query_params.get("sig"))):
        cid = space.find_job(parts[2])          # a signed link for Instagram / Threads: this one file only
        if cid:
            space.set_current(cid)
            return await call_next(request)
    if path.startswith(INTERNAL):
        if _from_public_proxy(request):          # n8n's addresses are never reachable from the internet
            return JSONResponse({"detail": "not found"}, status_code=404)
        token = pipeline.env("INTERNAL_TOKEN")   # set it once the Studio is on a server
        if token and not hmac.compare_digest(request.headers.get("x-internal-token", ""), token):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        cid = space.find_job(_job_in(path))
        if cid:
            space.set_current(cid)
        return await call_next(request)
    user, sess = auth.session(request.cookies.get(COOKIE))
    if not user:
        if path == "/":   # keep ?ref= / utm_* of a sign-up link for the login page
            return RedirectResponse("/login" + (f"?{request.url.query}" if request.url.query else ""))
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    request.state.user = user
    if (auth.two_step_required(user) and not user.get("totp_on") and path.startswith(("/api/", "/files/", "/oauth/"))
            and not path.startswith(TWO_STEP_OPEN)):
        return JSONResponse({"detail": "turn on two-step sign-in first (My account)", "two_step_setup": True},
                            status_code=403)
    job_cid = space.find_job(_job_in(path))
    if job_cid and not auth.may_open(user, job_cid):
        return JSONResponse({"detail": "no such video"}, status_code=404)
    if (path.startswith("/api/") and request.method != "GET" and not path.startswith(FREE_ACTIONS)
            and not (path.startswith("/api/channels/") and path.endswith("/open"))
            and not plans.unlimited(user) and not plans.PLANS[plans.name(user)]["videos_per_day"]):
        return JSONResponse({"detail": "your account has no plan yet - see the plans page or ask the Studio admin"},
                            status_code=403)
    own = [c["id"] for c in auth.channels_of(user)]
    cid = job_cid or (sess.get("channel") if sess.get("channel") in own else own[0] if own else space.NONE)
    space.set_current(cid)
    return await call_next(request)


def _user(request):
    return request.state.user


# what an account without a plan may still change: its own password and e-mail choice
FREE_ACTIONS = ("/api/me/", "/api/auth/")


def _started(user_id):
    """When each video of this account's channels was started (for the 24-hour limit): finished or
    still running, A/B versions included; failed runs, test-mode videos and own uploads do not count."""
    out = []
    for c in space.all_channels():
        if c.get("owner") != user_id:
            continue
        with space.use(c["id"]):
            out += [j.get("created") or "" for j in pipeline.list_jobs()
                    if not j["mock"] and j["kind"] != "upload" and j["state"] != "error"]
    return out


def access(user):
    return plans.access(user or {}, _started(user["id"]) if user else [])


class LimitError(Exception):
    pass


def check_limit(count=1, actor=None):
    """Raises LimitError when the current channel's owner may not start `count` more videos now.
    An admin acting in the Studio is never limited."""
    if actor and plans.unlimited(actor):
        return
    owner = auth.get(space.get(space.current()).get("owner") or "")
    if not owner or plans.unlimited(owner):
        return
    a = access(owner)
    if not a["active"]:
        raise LimitError("this account has no plan - ask the Studio admin for a trial")
    if a["left"] < count:
        when = time.strftime("%H:%M UTC", time.gmtime(datetime.datetime.strptime(a["next_at"], "%Y-%m-%dT%H:%M:%SZ")
                                                      .replace(tzinfo=datetime.timezone.utc).timestamp())) if a["next_at"] else ""
        raise LimitError(f"the {a['label']} plan makes {a['videos_per_day']} video(s) per 24 hours"
                         + (f" - {a['left']} left now" if a["left"] else f" - the next one from {when}"))


def _owner(request, full=False):
    """The signed-in admin; full=True also excludes plain admins (super admin / owner only)."""
    user = _user(request)
    if not auth.is_admin(user) or (full and user["role"] not in auth.FULL_ROLES):
        raise HTTPException(403, "your role cannot do this")
    return user


def _managed(request, user_id, *actions):
    """The admin, if they may do these actions to this account; an account they may not even see is
    answered like one that does not exist."""
    admin = _owner(request)
    target = auth.get(user_id)
    if not auth.can_see(admin, target):
        raise HTTPException(404, "no such user")
    if not all(auth.can_manage(admin, target, a) for a in actions):
        raise HTTPException(403, "your role cannot do this to this account")
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
def register(req: SignUpRequest, response: Response, request: Request):
    _throttle(request, "sign-up", 10, 3600)
    try:
        user = auth.register(req.email, req.password, req.marketing, req.source, req.timezone, req.language,
                             (req.invite or "").strip() or None)
    except Exception as e:
        _fail(e)
    notify.new_account(user, auth.source_label(user))
    _sign_in(response, user)
    return {"user": auth.public(user)}


@app.post("/api/auth/login")
def login(req: SignInRequest, response: Response, request: Request):
    _throttle(request, "sign-in", 20, 600)
    try:
        user = auth.authenticate(req.email, req.password, req.timezone, req.language)
    except Exception as e:
        _fail(e)
    if user.get("totp_on"):          # the password was right: the code from the phone is next
        return {"two_step": True, "ticket": auth.start_two_step(user)}
    _sign_in(response, user)
    return {"user": auth.public(user)}


@app.post("/api/auth/login/code")
def login_code(req: CodeRequest, response: Response, request: Request):
    _throttle(request, "sign-in", 20, 600)
    try:
        user = auth.finish_two_step(req.ticket, req.code, req.timezone, req.language)
    except Exception as e:
        _fail(e)
    _sign_in(response, user)
    return {"user": auth.public(user)}


def _base_url(request):
    return (pipeline.env("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


@app.post("/api/auth/reset")
def reset_request(req: ResetRequest, request: Request):
    """'Forgot password?': a one-time link by e-mail. The answer is the same whether or not the e-mail
    has an account, so nobody can use this page to find out who uses the Studio."""
    _throttle(request, "password reset", 10, 3600)
    user, token = auth.request_reset(req.email)
    if user:
        link = f"{_base_url(request)}/login?reset={token}"
        if pipeline.SANDBOX:
            print(f"[sandbox] reset link for {user['email']}: {link}", flush=True)
        try:
            mail.send(user["email"], "Reset your Vid Studio password",
                      f"Someone (hopefully you) asked to reset the password of your Vid Studio account.\n\n"
                      f"Open this link to choose a new password:\n{link}\n\n"
                      f"It works once, for {auth.RESET_MINUTES} minutes. If you did not ask for it, ignore this "
                      "e-mail: your password stays the same.\n\n— Vid Studio")
        except mail.MailError as e:
            print(f"[warn] reset e-mail not sent: {e}", flush=True)
            if not pipeline.SANDBOX:
                notify.security("mail", f"✉ A password reset e-mail could not be sent: {e}", "mail")
    return {"sent": True, "minutes": auth.RESET_MINUTES}


@app.get("/api/auth/reset/{token}")
def reset_check(token: str, request: Request):
    _throttle(request, "reset link", 30, 3600)
    info = auth.reset_info(token)
    if not info:
        raise HTTPException(400, "this link has expired or was already used - ask for a new one")
    return info


@app.post("/api/auth/reset/finish")
def reset_finish(req: ResetFinishRequest, request: Request):
    _throttle(request, "reset link", 30, 3600)
    try:
        user = auth.finish_reset(req.token, req.password)
    except Exception as e:
        _fail(e)
    try:
        mail.send(user["email"], "Your Vid Studio password was changed",
                  "The password of your Vid Studio account was just changed with a reset link, and every device "
                  "was signed out.\n\nIf this was not you, reset the password again at once and tell the Studio "
                  "admin.\n\n— Vid Studio")
    except mail.MailError as e:
        print(f"[warn] password-changed e-mail not sent: {e}", flush=True)
    notify.security("reset", f"🔁 {user['email']} reset the password with an e-mail link "
                             f"(from {_client_ip(request)}).", user["email"])
    return {"changed": True, "two_step": bool(user.get("totp_on"))}


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
            "sandbox": pipeline.SANDBOX,
            "templates": {k: v["label"] for k, v in niches.TEMPLATES.items()},
            "unsubscribe_reasons": auth.UNSUBSCRIBE_REASONS, "plans": list(plans.GRANTABLE),
            "plan_names": {k: v["label"] for k, v in plans.PLANS.items()}, "access": access(user)}


@app.put("/api/me/password")
def change_password(req: PasswordRequest, request: Request, response: Response):
    try:
        auth.change_password(_user(request)["id"], req.old, req.new)
    except Exception as e:
        _fail(e)
    _sign_in(response, _user(request), space.current() if space.list_ids() else None)
    return {"changed": True}


@app.post("/api/me/2fa/setup")
def two_step_setup(request: Request):
    """A new secret and its QR code for the authenticator app; on only after a code from it is typed."""
    import segno
    out = auth.two_step_begin(_user(request)["id"])
    return {**out, "qr": segno.make(out["uri"], error="m").svg_data_uri(scale=5, border=2)}


@app.post("/api/me/2fa/enable")
def two_step_enable(req: TwoStepRequest, request: Request):
    try:
        codes = auth.two_step_enable(_user(request)["id"], req.code)
    except Exception as e:
        _fail(e)
    return {"on": True, "backup_codes": codes}


@app.post("/api/me/2fa/disable")
def two_step_disable(req: TwoStepRequest, request: Request):
    try:
        auth.two_step_disable(_user(request)["id"], req.password, req.code)
    except Exception as e:
        _fail(e)
    return {"on": False}


@app.post("/api/me/2fa/backup-codes")
def two_step_backup(req: TwoStepRequest, request: Request):
    try:
        return {"backup_codes": auth.new_backup_codes(_user(request)["id"], req.code)}
    except Exception as e:
        _fail(e)


@app.put("/api/me/marketing")
def change_marketing(req: MarketingRequest, request: Request):
    return auth.public(auth.set_marketing(_user(request)["id"], req.marketing, req.reason))


@app.post("/api/channels")
def create_channel(req: ChannelRequest, request: Request):
    """A new channel from a template: its settings, script rules and starter topics."""
    user = _user(request)
    a = access(user)
    if not a["unlimited"] and sum(1 for c in space.all_channels() if c.get("owner") == user["id"]) >= (a["channels"] or 0):
        raise HTTPException(403, f"the {a['label']} plan has {a['channels'] or 'no'} channel(s)"
                                 + (" - see the plans page" if a["channels"] else " - ask the Studio admin for a trial"))
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
    return [{**u, "channels": names.get(u["id"], []), "can": auth.rights(admin, full[u["id"]])}
            for u in auth.list_users(q) if auth.can_see(admin, full[u["id"]])]


@app.patch("/api/users/{user_id}")
def edit_user(user_id: str, req: UserEditRequest, request: Request):
    wanted = [a for a, v in (("block", req.disabled), ("password", req.password), ("role", req.role),
                             ("plan", req.plan)) if v is not None]
    admin = _managed(request, user_id, *wanted)
    target = auth.get(user_id)
    try:
        if req.disabled is not None:
            auth.set_disabled(user_id, req.disabled)
        if req.password:
            auth.set_password(user_id, req.password)
        if req.role:
            auth.set_role(user_id, req.role)
        if req.plan:
            auth.set_plan(user_id, req.plan)
    except Exception as e:
        _fail(e)
    who = f"{admin['email']} → {target['email']}"
    if req.role and req.role != target.get("role"):
        notify.security("role", f"👑 Role changed: {who}: {target.get('role')} → {req.role}", f"{user_id}|{req.role}")
    if req.password:
        notify.security("password", f"🔑 Password set by an admin: {who}", user_id)
    if req.disabled is not None and req.disabled != bool(target.get("disabled")):
        notify.security("block", f"{'⛔ Blocked' if req.disabled else '✅ Unblocked'}: {who}", f"{user_id}|{req.disabled}")
    return auth.public(auth.get(user_id))


@app.post("/api/users/{user_id}/reset-2fa")
def reset_two_step(user_id: str, request: Request):
    """Someone lost the phone and the backup codes: two-step goes off, every device is signed out."""
    admin = _managed(request, user_id, "twostep")
    auth.two_step_reset(user_id)
    notify.security("2fa-reset", f"🔓 Two-step sign-in reset: {admin['email']} → {auth.get(user_id)['email']}", user_id)
    return auth.public(auth.get(user_id))


@app.post("/api/users/{user_id}/logout")
def logout_user(user_id: str, request: Request):
    """Sign the user out of the Studio on every device."""
    _managed(request, user_id, "logout")
    auth.end_sessions(user_id)
    return {"signed_out": user_id}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, request: Request):
    """The account goes; its channels stay on disk and move to the admin who deleted it."""
    owner = _managed(request, user_id, "delete")
    gone = (auth.get(user_id) or {}).get("email")
    try:
        auth.delete(user_id)
    except Exception as e:
        _fail(e)
    for c in space.all_channels():
        if c.get("owner") == user_id:
            space.update(c["id"], owner=owner["id"])
    notify.security("delete", f"🗑 Account deleted: {owner['email']} → {gone}", user_id)
    return {"deleted": user_id}


def _usage(user_id):
    """How one account uses the Studio: (summary row, [(date, "video" | "upload")]). Only the Studio's
    own records, never data from Google/YouTube APIs (Limited Use).
    'stage' sorts people for e-mails: signed_up -> channel -> video -> published -> autopilot."""
    channels = [c for c in space.all_channels() if c.get("owner") == user_id]
    names, kinds, connected, used, events = [], set(), set(), set(), []
    made = versions = failed = uploads = 0
    first = last = ""
    telegram = daily = False
    for c in channels:
        names.append(c["name"])
        daily = daily or c.get("daily", True)
        with space.use(c["id"]):
            kinds.add(pipeline.niche().get("label") or pipeline.env("NICHE_KIND", "business"))
            connected |= {p for p in social.PLATFORMS if social._account(p)}
            telegram = telegram or notify.status()["configured"]
            for j in pipeline.list_jobs():
                if j["mock"] or j["kind"] == "upload":
                    continue
                if j["state"] == "error":
                    failed += 1
                if j["state"] != "done":
                    continue
                if j["variant"] not in (None, "A"):
                    versions += 1
                else:
                    made += 1
                created = j.get("created") or ""
                first, last = min(first or created, created), max(last, created)
                events.append((created[:10], "video"))
                status = pipeline._read_json(pipeline.job_dir(j["job_id"]) / "status.json") or {}
                posted = {p: v.get("at") or "" for p, v in (status.get("social") or {}).items() if v.get("state") == "posted"}
                yt = status.get("youtube") or {}
                if yt.get("video_id") and "youtube" not in posted:
                    posted["youtube"] = yt.get("at") or ""
                for at in posted.values():
                    events.append((at[:10] or created[:10], "upload"))
                uploads += len(posted)
                used |= set(posted)
    stage = ("autopilot" if uploads and daily and "youtube" in connected else "published" if uploads
             else "video" if made else "channel" if channels else "signed_up")
    active = max([d for d, _ in events] + [""])
    idle = ""
    if active:
        idle = (datetime.date.today() - datetime.date.fromisoformat(active)).days
    row = {"stage": stage, "channels_count": len(channels), "channels": "; ".join(names),
           "niches": "; ".join(sorted(kinds)), "videos_made": made, "ab_versions": versions, "failed_runs": failed,
           "first_video_at": first, "last_video_at": last, "uploads": uploads,
           "platforms_posted": "; ".join(sorted(used)), "platforms_connected": "; ".join(sorted(connected)),
           "telegram_alerts": "yes" if telegram else "no", "daily_autopilot": "yes" if channels and daily else "no",
           "last_activity_at": active, "days_inactive": idle}
    return row, events


def user_stats(user_id):
    """One row of the subscribers CSV (see _usage)."""
    return _usage(user_id)[0]


STAGES = ("signed_up", "channel", "video", "published", "autopilot")


@app.get("/api/stats")
def studio_stats(request: Request, days: int = 30):
    """The Studio stats page (super admin / owner): the funnel by stage, who is active, where people
    come from, why they unsubscribe, plans, and videos / uploads / sign-ups per day."""
    _owner(request, full=True)
    days = max(7, min(90, days))
    today = datetime.date.today()
    dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    per_day = {d: {"date": d, "signups": 0, "videos": 0, "uploads": 0} for d in dates}
    funnel = dict.fromkeys(STAGES, 0)
    sources, reasons, plans, zones = {}, {}, {}, {}
    active7 = active30 = subscribed = 0
    users = auth._users()
    for u in users:
        row, events = _usage(u["id"])
        reached = STAGES.index(row["stage"])
        for st in STAGES[:reached + 1]:
            funnel[st] += 1          # a person who publishes also counts as having a channel and a video
        last = max([d for d, _ in events] + [(u.get("last_login") or "")[:10]])
        if last and (today - datetime.date.fromisoformat(last)).days < 7:
            active7 += 1
        if last and (today - datetime.date.fromisoformat(last)).days < 30:
            active30 += 1
        signed = (u.get("created") or "")[:10]
        if signed in per_day:
            per_day[signed]["signups"] += 1
        for d, kind in events:
            if d in per_day:
                per_day[d]["videos" if kind == "video" else "uploads"] += 1
        src = auth.source_label(u)
        sources[src] = sources.get(src, 0) + 1
        plans[u.get("plan") or "free"] = plans.get(u.get("plan") or "free", 0) + 1
        zone = (u.get("timezone") or "unknown").split("/")[0]
        zones[zone] = zones.get(zone, 0) + 1
        if u.get("marketing") and not u.get("disabled"):
            subscribed += 1
        if u.get("unsubscribed_at"):
            r = u.get("unsubscribe_reason") or "no reason given"
            reasons[r] = reasons.get(r, 0) + 1
    top = lambda d: sorted(({"name": k, "count": v} for k, v in d.items()), key=lambda x: -x["count"])
    return {"users": len(users), "active_7": active7, "active_30": active30, "subscribed": subscribed,
            "unsubscribed": sum(reasons.values()), "funnel": [{"stage": k, "count": funnel[k]} for k in STAGES],
            "sources": top(sources), "unsubscribe_reasons": top(reasons), "plans": top(plans), "regions": top(zones),
            "days": list(per_day.values())}


@app.get("/api/auth/plans")
def plan_catalog():
    """The public plans page (no sign-in): the plans and what each one allows."""
    return {"plans": plans.catalog(), "payments": False}


@app.get("/api/auth/invite/{code}")
def invite_check(code: str):
    """The sign-up page asks what an invite link gives before the account exists."""
    info = auth.invite_info(code)
    if not info:
        raise HTTPException(404, "this invite link has expired or was already used")
    return info


@app.get("/pricing")
def pricing_page():
    return HTMLResponse((STATIC / "pricing.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/tiktok{code}.txt")
def tiktok_verification(code: str):
    """TikTok's site check for the public address: the file it gave you goes in data/verify/."""
    f = pipeline.DATA / "verify" / f"tiktok{code}.txt"
    if not code.isalnum() or not f.is_file():
        raise HTTPException(404)
    return PlainTextResponse(f.read_text(encoding="utf-8"))


@app.get("/api/invites")
def invites(request: Request):
    _owner(request, full=True)
    return auth.list_invites()


@app.post("/api/invites")
def new_invite(req: InviteRequest, request: Request):
    """A sign-up link that gives a plan (super admin / owner)."""
    admin = _owner(request, full=True)
    try:
        return auth.create_invite(admin["id"], req.plan, req.uses, req.days, req.note)
    except Exception as e:
        _fail(e)


@app.delete("/api/invites/{code}")
def drop_invite(code: str, request: Request):
    _owner(request, full=True)
    auth.revoke_invite(code)
    return {"revoked": code}


@app.get("/api/users/marketing.csv")
def marketing_list(request: Request):
    _owner(request, full=True)   # the mailing list stays with the super admin / owner
    return Response(auth.marketing_csv(user_stats), media_type="text/csv",
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


SECURITY_HEADERS = _security_headers()


@app.middleware("http")
async def revalidate_dashboard(request, call_next):
    """Make the browser re-check dashboard files on every load (cheap: ETag answers 304), so an
    update never leaves an old style.css or app.js running. Every answer carries the security headers."""
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
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
def start_job(request: Request, req: ProduceRequest | None = None):
    """Start a video in the background and return at once; the dashboard polls its status."""
    req = req or ProduceRequest()
    if not req.mock:
        try:
            check_limit(1 + max(0, min(4, (req.variants or 1) - 1)), _user(request))
        except LimitError as e:
            raise HTTPException(429, str(e))
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now")
    job_id = pipeline.new_job_id()
    pipeline.Job(job_id, mock=pipeline.flag("MOCK") if req.mock is None else req.mock, topic=req.topic)

    after = {"variants": max(0, min(4, (req.variants or 1) - 1)), "use_video_hook": req.use_video_hook,
             "options": req.options, "choose": req.choose}
    user = _user(request)

    def run():
        try:
            result = pipeline.produce(topic=(req.topic or "").strip() or None, mock=req.mock,
                                      use_video_hook=req.use_video_hook, job_id=job_id,
                                      options=req.options, script=req.script, choose=req.choose, after=after)
            _follow_up(result, after, user)
        except Exception:
            traceback.print_exc()  # already recorded in status.json by produce()
        finally:
            _busy.release()

    space.thread(run)
    return {"job_id": job_id}


def _follow_up(result, after, user):
    """Once a video is made: its A/B versions, then a split story's remaining parts, one video each.
    A video waiting for its hook does this later, after the pick."""
    while not result.get("waiting"):
        if after.get("variants"):
            pipeline.make_variants(result["job_id"], after["variants"], picks=after.pop("picks", None))
        if not result.get("next_part") or _over_limit(user):
            return
        nxt = pipeline.new_job_id()
        pipeline.Job(nxt, topic="next part")
        result = pipeline.produce(mock=False, use_video_hook=after.get("use_video_hook"), job_id=nxt,
                                  options=after.get("options"), choose=after.get("choose"), after=after)


@app.post("/api/jobs/{job_id}/choose")
def choose_hook(job_id: str, req: ChooseRequest, request: Request):
    """The creator's hook and title for a video that waits after its script; the rest is made now."""
    try:
        after = dict(pipeline.check_choice(job_id, req.hook, req.title)[3].get("after") or {})
        if req.variants:
            after["picks"] = pipeline.check_variant_picks(job_id, req.variants)
            after["variants"] = len(after["picks"])
    except pipeline.PipelineError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        _fail(e)
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "another video is being produced right now - try again when it is done")
    user = _user(request)

    def run():
        try:
            result = pipeline.choose_and_build(job_id, req.hook, req.title)
            if after.get("publish"):  # the daily video: posted where n8n would have posted it
                try:
                    _send_to_n8n(job_id, after["publish"]["mode"], after["publish"]["platforms"])
                except Exception as e:
                    traceback.print_exc()
                    notify.send("error", f"❌ The picked daily video was made but not sent to n8n: "
                                         f"{getattr(e, 'detail', e)}\n{notify.link(job_id)}")
            if not after.get("daily"):
                _follow_up(result, after, user)
            elif after.get("variants"):  # a split story's next part is tomorrow's daily video, as without the pick
                pipeline.make_variants(job_id, after["variants"], picks=after.get("picks"))
        except Exception:
            traceback.print_exc()  # recorded in status.json
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
    _check_upload(file, "video")
    try:
        return pipeline.create_upload(file.file, file.filename, title, description, tags)
    except Exception as e:
        _fail(e)


@app.post("/api/jobs/{job_id}/scenes/{idx}/image")
def scene_image(job_id: str, idx: int, file: UploadFile = File(...)):
    _check_upload(file, "image")
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
def variants(job_id: str, req: VariantsRequest, request: Request):
    """More versions of a finished video, made in the background."""
    try:
        return {"making": start_variants(job_id, req.count, _user(request), req.picks)}
    except BusyError as e:
        raise HTTPException(409, str(e))
    except LimitError as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        _fail(e)


class BusyError(Exception):
    pass


def _over_limit(actor=None):
    try:
        check_limit(1, actor)
        return False
    except LimitError:
        return True


def start_variants(job_id, count, actor=None, picks=None):
    """Shared by the Studio button and the Telegram bot (notify.on_variants; actor None = the channel owner's limit).
    picks: the creator's [{hook, title}] for each new version."""
    if pipeline.load_job(job_id)["status"]["state"] != "done":
        raise pipeline.PipelineError("the video is not finished yet")
    picks = pipeline.check_variant_picks(job_id, picks) if picks else None
    count = len(picks) if picks else count
    check_limit(max(0, min(4, count)), actor)
    if not _busy.acquire(blocking=False):
        raise BusyError("another video is being produced right now - try again later")

    def run():
        try:
            pipeline.make_variants(job_id, count, picks=picks)
        except Exception:
            traceback.print_exc()
        finally:
            _busy.release()

    space.thread(run)
    return max(0, min(4, count))


notify.on_variants = start_variants


@app.get("/api/jobs/{job_id}/tiktok")
def tiktok_form(job_id: str):
    """The TikTok post form: the connected creator, their allowed privacy levels and the default caption."""
    try:
        return social.tiktok_creator(job_id)
    except Exception as e:
        _fail(e)


@app.post("/api/jobs/{job_id}/publish")
def publish(job_id: str, req: PublishRequest):
    """Post the video to the chosen platforms: normally through n8n (which keeps a log of every
    run), or straight from the worker with direct=true."""
    if pipeline.SANDBOX:
        raise HTTPException(403, "sandbox: publishing is off - publish from the real Studio")
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
        tiktok = social.tiktok_options(req.tiktok) if "tiktok" in platforms and req.tiktok is not None else None
    except Exception as e:
        _fail(e)
    if tiktok:   # the creator's TikTok choices only travel with a post from the worker, never through n8n
        def run_tiktok():
            try:
                social.post("tiktok", job_id, req.mode, tiktok)
            except Exception:
                traceback.print_exc()

        social.mark_queued(job_id, ["tiktok"], req.mode)
        space.thread(run_tiktok)
        platforms = [p for p in platforms if p != "tiktok"]
        if not platforms:
            return {"queued": job_id, "platforms": ["tiktok"], "via": "worker"}
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
    _send_to_n8n(job_id, req.mode, platforms)
    return {"queued": job_id, "platforms": platforms, "via": "n8n"}


def _send_to_n8n(job_id, mode, platforms):
    """Hand a finished video to n8n's publish workflow (the Studio button and a picked daily video)."""
    if pipeline.SANDBOX:
        print(f"[sandbox] would send {job_id} to n8n: {mode} on {', '.join(platforms) or 'nothing'}", flush=True)
        return
    if not platforms:
        return
    url = pipeline.env("N8N_PUBLISH_WEBHOOK", "http://n8n:5678/webhook/vidauto-publish")
    body = json.dumps({"job_id": job_id, "mode": mode, "platforms": platforms}).encode()
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
    social.mark_queued(job_id, platforms, mode)


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


@app.post("/api/notify/topics")
def notify_topics():
    try:
        return notify.setup_topics()
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


@app.get("/api/accounts/{platform}/avatar")
def account_avatar(platform: str):
    try:
        return FileResponse(social.avatar(platform), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
    except Exception as e:
        _fail(e)


@app.put("/api/accounts/{platform}/app")
def put_app_keys(platform: str, req: AppKeysRequest):
    try:
        return social.save_app(platform, req.client_id, req.client_secret, req.config_id, req.account_id)
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

@app.post("/n8n/error")
def n8n_error(payload: dict):
    """n8n's error workflow: a run of 'VidAuto: Make & Publish' failed (Studio unreachable, timeout,
    a node broke). Telegram gets it on every channel's chat, since the run belongs to no one channel."""
    ex, wf = payload.get("execution") or {}, payload.get("workflow") or {}
    err = (ex.get("error") or {}).get("message") or (payload.get("trigger") or {}).get("error", {}).get("message") or "unknown error"
    text = (f"❌ n8n: “{wf.get('name') or 'workflow'}” failed"
            + (f" at “{ex['lastNodeExecuted']}”" if ex.get("lastNodeExecuted") else "")
            + f"\n{str(err)[:400]}" + (f"\n{ex['url']}" if ex.get("url") else ""))
    print(f"[warn] {text}", flush=True)
    return {"sent_to": notify.broadcast("error", text)}


@app.post("/produce")
def produce(req: ProduceRequest | None = None):
    """n8n's daily run: one video for every channel with 'Daily' on, one after another.
    Returns {videos: [{job_id, channel, platforms}]} - the platforms each channel posts to."""
    req = req or ProduceRequest()
    # the daily run waits for a re-render or picture redo that is still going instead of skipping the day
    if not _busy.acquire(timeout=900):
        raise HTTPException(409, "another video is being produced right now")
    videos, waiting, errors = [], [], []
    try:
        for meta in space.all_channels():
            if not meta.get("daily", True):
                continue
            try:
                with space.use(meta["id"]):
                    check_limit(1)    # the channel owner's plan
                    # PICK_HOOK: the video waits after its script; the Studio posts it once the creator picked
                    choose = pipeline.flag("PICK_HOOK") and not req.mock
                    after = {"daily": True, "publish": {"mode": "auto", "platforms": social.default_platforms()}}
                    result = pipeline.produce(topic=(req.topic or "").strip() or None,
                                              mock=req.mock, use_video_hook=req.use_video_hook,
                                              choose=choose, after=after if choose else None)
                    if result.get("waiting"):
                        waiting.append({"job_id": result["job_id"], "channel": meta["id"]})
                        continue
                    videos.append({"job_id": result["job_id"], "channel": meta["id"],
                                   "platforms": social.default_platforms()})
            except Exception as e:  # one channel's failure must not cost the others their video
                traceback.print_exc()
                errors.append(f"{meta['name']}: {str(e)[:200]}")
    finally:
        _busy.release()
    if not videos and not waiting and errors:
        raise HTTPException(500, " | ".join(errors))
    return {"videos": videos, "waiting": waiting, "errors": errors, "job_id": videos[0]["job_id"] if videos else None}


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
