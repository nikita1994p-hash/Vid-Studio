"""Social accounts (log in, log out, switch) and posting a finished video to them.

Every platform logs in with OAuth in the browser: Studio -> Accounts -> Connect. Its app keys
(client id / secret, from the platform's developer site) are saved by the Studio in
data/accounts/apps.json; the login itself in data/accounts/<platform>.json. Nothing leaves this
computer except the calls to the platform itself.

n8n decides WHEN to post (daily schedule, or the Studio's Publish button) and calls
/social/<platform>/post; the upload itself happens here, because every platform needs a different
multi-step upload that is easier to keep correct in one Python file than in n8n nodes.

The endpoints follow each platform's public docs as of 2026. They are exercised for real only
when an account is connected, so the first post on each platform is the real test.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import notify
import pipeline
from pipeline import DATA, PipelineError, env, flag

ACCOUNTS = DATA / "accounts"
GRAPH = "https://graph.facebook.com/v21.0"
IG_GRAPH = "https://graph.instagram.com/v21.0"
THREADS = "https://graph.threads.net/v1.0"

PLATFORMS = {
    "youtube": {
        "name": "YouTube", "dev_url": "https://console.cloud.google.com/apis/credentials",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload",
                   "https://www.googleapis.com/auth/youtube.readonly",
                   # views, watch time and (once monetized) revenue for the Earnings page
                   "https://www.googleapis.com/auth/yt-analytics.readonly",
                   "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"],
        "auth_extra": {"access_type": "offline", "prompt": "consent select_account"},
        "how": "Google Cloud: enable 'YouTube Data API v3' and 'YouTube Analytics API', OAuth consent screen 'In production', "
               "Credentials -> OAuth client ID -> Web application, add the redirect URI below.",
    },
    "tiktok": {
        "name": "TikTok", "dev_url": "https://developers.tiktok.com/apps",
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": ["user.info.basic", "video.publish", "video.list"], "scope_sep": ",", "client_param": "client_key",
        "how": "TikTok for Developers: create an app, add Login Kit + Content Posting API (Direct Post), "
               "add the redirect URI below. Until TikTok audits the app, posts can only be private (Only me).",
    },
    "instagram": {
        "name": "Instagram", "dev_url": "https://developers.facebook.com/apps",
        "auth_url": "https://www.instagram.com/oauth/authorize",
        "token_url": "https://api.instagram.com/oauth/access_token",
        "scopes": ["instagram_business_basic", "instagram_business_content_publish", "instagram_business_manage_insights"], "scope_sep": ",",
        "how": "Meta for Developers: app with 'Instagram API with Instagram Login', a Business or Creator "
               "Instagram account, add the redirect URI below.",
    },
    "facebook": {
        "name": "Facebook Page", "dev_url": "https://developers.facebook.com/apps",
        "auth_url": "https://www.facebook.com/v21.0/dialog/oauth",
        "token_url": f"{GRAPH}/oauth/access_token",
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "publish_video"],
        "scope_sep": ",", "token_method": "GET",
        "how": "Meta for Developers: app with 'Facebook Login for Business', add the redirect URI below. "
               "Posts go to a Facebook Page you manage (Reels).",
    },
    "threads": {
        "name": "Threads", "dev_url": "https://developers.facebook.com/apps",
        "auth_url": "https://threads.net/oauth/authorize",
        "token_url": "https://graph.threads.net/oauth/access_token",
        "scopes": ["threads_basic", "threads_content_publish", "threads_manage_insights"], "scope_sep": ",",
        "needs_public_url": True,
        "how": "Meta for Developers: app with the Threads use case, add the redirect URI below. Threads "
               "downloads the video itself, so PUBLIC_BASE_URL must be a public https link to this Studio.",
    },
    "x": {
        "name": "X (Twitter)", "dev_url": "https://developer.x.com/en/portal/dashboard",
        "auth_url": "https://x.com/i/oauth2/authorize",
        "token_url": "https://api.x.com/2/oauth2/token",
        "scopes": ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"],
        "pkce": True, "basic_auth": True,
        "how": "X Developer Portal: project + app, User authentication settings -> OAuth 2.0, type "
               "'Web App', add the redirect URI below. Posting video needs an API plan that allows it.",
    },
}


class SocialError(PipelineError):
    pass


# ------------------------------------------------------------------ storage

def _path(name):
    ACCOUNTS.mkdir(parents=True, exist_ok=True)
    return ACCOUNTS / name


def _apps():
    return pipeline._read_json(_path("apps.json"), {}) or {}


def _account(platform):
    return pipeline._read_json(_path(f"{platform}.json"))


def _save_account(platform, data):
    pipeline._write_json(_path(f"{platform}.json"), data)


def _check(platform):
    if platform not in PLATFORMS:
        raise SocialError(f"unknown platform {platform}")
    return PLATFORMS[platform]


def base_url(fallback="http://localhost:8000"):
    return (env("PUBLIC_BASE_URL") or fallback).rstrip("/")


def redirect_uri(platform, base=None):
    return f"{(base or base_url()).rstrip('/')}/oauth/{platform}/callback"


def accounts(base=None):
    """What the Accounts page shows: per platform, whether it is set up and who is logged in."""
    apps, out = _apps(), []
    for key, p in PLATFORMS.items():
        acct = _account(key) or {}
        app = apps.get(key) or {}
        out.append({
            "id": key, "name": p["name"], "how": p["how"], "dev_url": p["dev_url"],
            "redirect_uri": redirect_uri(key, base),
            "app_configured": bool(app.get("client_id") and app.get("client_secret")),
            "client_id": app.get("client_id", ""),
            "connected": bool(acct.get("access_token")),
            "account": acct.get("profile"),
            "pages": [{"id": x["id"], "name": x["name"]} for x in acct.get("pages", [])],
            "page_id": acct.get("page_id"),
            "connected_at": acct.get("connected_at"),
            "needs_public_url": p.get("needs_public_url", False),
            "public_url": env("PUBLIC_BASE_URL") or None,
        })
    return out


def save_app(platform, client_id, client_secret):
    _check(platform)
    apps = _apps()
    old = apps.get(platform) or {}
    apps[platform] = {"client_id": (client_id or "").strip(),
                      # an empty secret field keeps the saved one: the page never shows secrets back
                      "client_secret": (client_secret or "").strip() or old.get("client_secret", "")}
    pipeline._write_json(_path("apps.json"), apps)
    return {"saved": platform}


def logout(platform):
    _check(platform)
    _path(f"{platform}.json").unlink(missing_ok=True)
    return {"logged_out": platform}


def choose_page(platform, page_id):
    acct = _account(platform) or {}
    page = next((x for x in acct.get("pages", []) if x["id"] == page_id), None)
    if not page:
        raise SocialError("no such page on this account")
    acct["page_id"] = page_id
    acct["profile"] = {**(acct.get("profile") or {}), "name": page["name"], "id": page_id,
                       "avatar": page.get("avatar")}
    _save_account(platform, acct)
    return acct["profile"]


# --------------------------------------------------------------------- http

def _request(method, url, params=None, form=None, body=None, json_body=None, headers=None, timeout=120):
    """(status, headers, parsed body). Raises SocialError with the platform's own message."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif body is not None:
        data = body
    headers.setdefault("User-Agent", "VidAuto/1.0")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status, resp_headers = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raise SocialError(f"{urllib.parse.urlsplit(url).netloc} answered {e.code}: "
                          f"{e.read()[:500].decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise SocialError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {e.reason}")
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"raw": raw[:500].decode("utf-8", "replace")}
    return status, resp_headers, parsed


def _call(method, url, **kw):
    return _request(method, url, **kw)[2]


def _multipart(fields, files):
    """multipart/form-data body: fields {name: str}, files {name: (filename, bytes, type)}."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, blob, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _basic(app):
    return "Basic " + base64.b64encode(f"{app['client_id']}:{app['client_secret']}".encode()).decode()


# ------------------------------------------------------------------- log in

def start_login(platform, base=None):
    """The platform's consent-page URL. Remembers state (and the PKCE verifier) for the callback."""
    p = _check(platform)
    app = _apps().get(platform) or {}
    if not app.get("client_id") or not app.get("client_secret"):
        raise SocialError(f"first paste the {p['name']} app keys (Accounts -> {p['name']} -> App keys)")
    state = secrets.token_urlsafe(24)
    pending = pipeline._read_json(_path("pending.json"), {}) or {}
    entry = {"platform": platform, "at": time.time(), "redirect_uri": redirect_uri(platform, base)}
    q = {p.get("client_param", "client_id"): app["client_id"], "redirect_uri": entry["redirect_uri"],
         "response_type": "code", "scope": p.get("scope_sep", " ").join(p["scopes"]), "state": state,
         **p.get("auth_extra", {})}
    if p.get("pkce"):
        verifier = secrets.token_urlsafe(48)
        entry["verifier"] = verifier
        q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q["code_challenge_method"] = "S256"
    # forget logins that were started but never finished
    pending = {k: v for k, v in pending.items() if time.time() - v.get("at", 0) < 3600}
    pending[state] = entry
    pipeline._write_json(_path("pending.json"), pending)
    return p["auth_url"] + "?" + urllib.parse.urlencode(q)


def finish_login(platform, code, state):
    p = _check(platform)
    pending = pipeline._read_json(_path("pending.json"), {}) or {}
    entry = pending.pop(state, None)
    pipeline._write_json(_path("pending.json"), pending)
    if not entry or entry["platform"] != platform:
        raise SocialError("this login link is old or was opened twice - press Connect again")
    app = _apps()[platform]
    form = {p.get("client_param", "client_id"): app["client_id"], "client_secret": app["client_secret"],
            "code": code, "grant_type": "authorization_code", "redirect_uri": entry["redirect_uri"]}
    headers = {}
    if entry.get("verifier"):
        form["code_verifier"] = entry["verifier"]
    if p.get("basic_auth"):
        headers["Authorization"] = _basic(app)
        form.pop("client_secret")
    if p.get("token_method") == "GET":
        tok = _call("GET", p["token_url"], params=form)
    else:
        tok = _call("POST", p["token_url"], form=form, headers=headers)
    tok = _long_lived(platform, tok, app)
    acct = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
            "expires_at": time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None,
            "user_id": tok.get("user_id") or tok.get("open_id"),
            "connected_at": pipeline._now()}
    acct.update(_profile(platform, acct))
    _save_account(platform, acct)
    return acct.get("profile")


def _long_lived(platform, tok, app):
    """Meta hands out one-hour tokens; swap them for the long-lived kind (60 days / no expiry for Pages)."""
    if platform == "facebook":
        return _call("GET", f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app["client_id"],
            "client_secret": app["client_secret"], "fb_exchange_token": tok["access_token"]})
    if platform == "instagram":
        long = _call("GET", "https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": app["client_secret"],
            "access_token": tok["access_token"]})
        return {**long, "user_id": tok.get("user_id")}
    if platform == "threads":
        long = _call("GET", "https://graph.threads.net/access_token", params={
            "grant_type": "th_exchange_token", "client_secret": app["client_secret"],
            "access_token": tok["access_token"]})
        return {**long, "user_id": tok.get("user_id")}
    return tok


def _profile(platform, acct):
    """Who is logged in - shown in the Studio so you can see where a video will go."""
    t = acct["access_token"]
    bearer = {"Authorization": f"Bearer {t}"}
    if platform == "youtube":
        items = _call("GET", "https://www.googleapis.com/youtube/v3/channels",
                      params={"part": "snippet", "mine": "true"}, headers=bearer).get("items") or []
        if not items:
            raise SocialError("this Google account has no YouTube channel")
        sn = items[0]["snippet"]
        return {"profile": {"id": items[0]["id"], "name": sn["title"],
                            "avatar": sn.get("thumbnails", {}).get("default", {}).get("url"),
                            "url": f"https://youtube.com/channel/{items[0]['id']}"}}
    if platform == "facebook":
        pages = _call("GET", f"{GRAPH}/me/accounts", params={
            "fields": "id,name,access_token,picture{url}", "access_token": t}).get("data") or []
        if not pages:
            raise SocialError("this Facebook account manages no Page - create a Page first")
        pages = [{"id": x["id"], "name": x["name"], "access_token": x["access_token"],
                  "avatar": (x.get("picture") or {}).get("data", {}).get("url")} for x in pages]
        first = pages[0]
        return {"pages": pages, "page_id": first["id"],
                "profile": {"id": first["id"], "name": first["name"], "avatar": first["avatar"],
                            "url": f"https://facebook.com/{first['id']}"}}
    if platform == "instagram":
        me = _call("GET", f"{IG_GRAPH}/me", params={"fields": "user_id,username,profile_picture_url",
                                                   "access_token": t})
        return {"user_id": me.get("user_id") or me.get("id"),
                "profile": {"id": me.get("user_id") or me.get("id"), "name": "@" + me.get("username", ""),
                            "avatar": me.get("profile_picture_url"),
                            "url": f"https://instagram.com/{me.get('username', '')}"}}
    if platform == "threads":
        me = _call("GET", f"{THREADS}/me", params={"fields": "id,username,threads_profile_picture_url",
                                                  "access_token": t})
        return {"user_id": me["id"], "profile": {"id": me["id"], "name": "@" + me.get("username", ""),
                                                 "avatar": me.get("threads_profile_picture_url"),
                                                 "url": f"https://threads.net/@{me.get('username', '')}"}}
    if platform == "tiktok":
        u = _call("GET", "https://open.tiktokapis.com/v2/user/info/",
                  params={"fields": "open_id,display_name,avatar_url"}, headers=bearer).get("data", {}).get("user", {})
        return {"profile": {"id": u.get("open_id"), "name": u.get("display_name"), "avatar": u.get("avatar_url")}}
    if platform == "x":
        u = _call("GET", "https://api.x.com/2/users/me", params={"user.fields": "profile_image_url"},
                  headers=bearer).get("data", {})
        return {"user_id": u.get("id"), "profile": {"id": u.get("id"), "name": f"@{u.get('username', '')}",
                                                    "avatar": u.get("profile_image_url"),
                                                    "url": f"https://x.com/{u.get('username', '')}"}}
    return {}


def _token(platform):
    """A working login, refreshed when its token is about to expire."""
    p, acct = PLATFORMS[platform], _account(platform)
    if not acct or not acct.get("access_token"):
        raise SocialError(f"{p['name']} is not connected - Studio -> Accounts -> Connect")
    if not acct.get("expires_at") or acct["expires_at"] - time.time() > 24 * 3600:
        return acct
    app = _apps().get(platform) or {}
    if platform in ("instagram", "threads"):
        url = ("https://graph.instagram.com/refresh_access_token" if platform == "instagram"
               else "https://graph.threads.net/refresh_access_token")
        tok = _call("GET", url, params={"grant_type": "ig_refresh_token" if platform == "instagram"
                                        else "th_refresh_token", "access_token": acct["access_token"]})
    elif acct.get("refresh_token"):
        form = {p.get("client_param", "client_id"): app["client_id"], "client_secret": app["client_secret"],
                "grant_type": "refresh_token", "refresh_token": acct["refresh_token"]}
        headers = {}
        if p.get("basic_auth"):
            headers["Authorization"] = _basic(app)
            form.pop("client_secret")
        tok = _call("POST", p["token_url"], form=form, headers=headers)
    else:
        return acct
    acct["access_token"] = tok["access_token"]
    acct["refresh_token"] = tok.get("refresh_token") or acct.get("refresh_token")
    acct["expires_at"] = time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None
    _save_account(platform, acct)
    return acct


# --------------------------------------------------------------------- post

def _caption(result, limit, with_title=True):
    title, desc = result.get("title") or "", result.get("description") or ""
    tags = " ".join(w for w in desc.split() if w.startswith("#"))
    text = f"{title}\n\n{desc}" if with_title else desc
    if len(text) > limit:
        text = (f"{title} {tags}" if with_title else tags).strip()
    return text[:limit]


def _wait(check, what, tries=60, pause=5):
    for _ in range(tries):
        state = check()
        if state is True:
            return
        if isinstance(state, str):
            raise SocialError(f"{what}: {state}")
        time.sleep(pause)
    raise SocialError(f"{what}: still processing after {tries * pause // 60} minutes")


def _cover(job_id):
    """cover.jpg bytes when the video has one and covers are on, else None."""
    path = pipeline.job_dir(job_id) / "cover.jpg"
    return path.read_bytes() if flag("COVERS", "true") and path.exists() else None


def _public_video_url(job_id):
    if not env("PUBLIC_BASE_URL"):
        return None
    return f"{env('PUBLIC_BASE_URL').rstrip('/')}/files/{job_id}/final.mp4"


def _post_youtube(acct, job_id, video, result, mode):
    bearer = {"Authorization": f"Bearer {acct['access_token']}"}
    meta = {"snippet": {"title": result["title"][:100], "description": result.get("description", "")[:4900],
                        "tags": [t for t in (result.get("tags") or "").split(",") if t][:30],
                        "categoryId": result.get("category_id") or env("YOUTUBE_CATEGORY_ID", "27"),
                        "defaultLanguage": result.get("language") or "en",
                        "defaultAudioLanguage": result.get("language") or "en"},
            "status": pipeline.youtube_status(mode)}
    _, headers, _ = _request("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                             params={"uploadType": "resumable", "part": "snippet,status"}, json_body=meta,
                             headers={**bearer, "X-Upload-Content-Type": "video/mp4",
                                      "X-Upload-Content-Length": str(len(video))})
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise SocialError("YouTube did not open an upload session")
    up = _call("PUT", location, body=video, headers={**bearer, "Content-Type": "video/mp4"}, timeout=900)
    vid = up.get("id")
    if not vid:
        raise SocialError(f"YouTube upload finished without a video id: {str(up)[:300]}")
    pipeline.mark_uploaded(job_id, vid, mode)
    note = None
    cover = _cover(job_id)
    if cover:
        try:  # custom thumbnails need a phone-verified channel; the video is up either way
            _call("POST", "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                  params={"videoId": vid, "uploadType": "media"}, body=cover,
                  headers={**bearer, "Content-Type": "image/jpeg"})
            note = "cover set"
        except Exception as e:
            note = f"cover not set: {str(e)[:200]}"
    return {"url": f"https://youtube.com/shorts/{vid}", "id": vid, "note": note}


def _post_facebook(acct, job_id, video, result, mode):
    page = next((x for x in acct.get("pages", []) if x["id"] == acct.get("page_id")), None)
    if not page:
        raise SocialError("choose a Facebook Page in Accounts")
    pt = page["access_token"]
    start = _call("POST", f"{GRAPH}/{page['id']}/video_reels", json_body={"upload_phase": "start", "access_token": pt})
    _call("POST", start["upload_url"], body=video, timeout=900, headers={
        "Authorization": f"OAuth {pt}", "offset": "0", "file_size": str(len(video))})
    _call("POST", f"{GRAPH}/{page['id']}/video_reels", params={
        "upload_phase": "finish", "video_id": start["video_id"], "video_state": "PUBLISHED",
        "description": _caption(result, 2200), "access_token": pt})
    note, cover = None, _cover(job_id)
    if cover:
        try:
            body, ctype = _multipart({"is_preferred": "true", "access_token": pt},
                                     {"source": ("cover.jpg", cover, "image/jpeg")})
            _call("POST", f"{GRAPH}/{start['video_id']}/thumbnails", body=body, headers={"Content-Type": ctype})
            note = "cover set"
        except Exception as e:
            note = f"cover not set: {str(e)[:200]}"
    return {"url": f"https://www.facebook.com/reel/{start['video_id']}", "id": start["video_id"], "note": note}


def _post_instagram(acct, job_id, video, result, mode):
    t, uid = acct["access_token"], acct.get("user_id")
    params = {"media_type": "REELS", "caption": _caption(result, 2200), "access_token": t}
    public = _public_video_url(job_id)
    if public and _cover(job_id):  # Instagram fetches the cover from a link, so it needs PUBLIC_BASE_URL too
        params["cover_url"] = public.rsplit("/", 1)[0] + "/cover.jpg"
    if public:
        params["video_url"] = public
        container = _call("POST", f"{IG_GRAPH}/{uid}/media", params=params)
    else:  # no public link: hand the file over directly (resumable upload)
        container = _call("POST", f"{IG_GRAPH}/{uid}/media", params={**params, "upload_type": "resumable"})
        _call("POST", container["uri"], body=video, timeout=900, headers={
            "Authorization": f"OAuth {t}", "offset": "0", "file_size": str(len(video))})
    cid = container["id"]

    def ready():
        s = _call("GET", f"{IG_GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": t})
        if s.get("status_code") == "FINISHED":
            return True
        return (s.get("status") or "rejected") if s.get("status_code") in ("ERROR", "EXPIRED") else None

    _wait(ready, "Instagram processing")
    media = _call("POST", f"{IG_GRAPH}/{uid}/media_publish", params={"creation_id": cid, "access_token": t})
    link = _call("GET", f"{IG_GRAPH}/{media['id']}", params={"fields": "permalink", "access_token": t})
    return {"url": link.get("permalink"), "id": media["id"]}


def _post_threads(acct, job_id, video, result, mode):
    public = _public_video_url(job_id)
    if not public:
        raise SocialError("Threads downloads the video from a public link: set PUBLIC_BASE_URL in .env "
                          "(e.g. a Cloudflare Tunnel to http://localhost:8000)")
    t = acct["access_token"]
    c = _call("POST", f"{THREADS}/me/threads", params={"media_type": "VIDEO", "video_url": public,
                                                       "text": _caption(result, 500), "access_token": t})

    def ready():
        s = _call("GET", f"{THREADS}/{c['id']}", params={"fields": "status,error_message", "access_token": t})
        if s.get("status") == "FINISHED":
            return True
        return (s.get("error_message") or "rejected") if s.get("status") in ("ERROR", "EXPIRED") else None

    _wait(ready, "Threads processing")
    post_ = _call("POST", f"{THREADS}/me/threads_publish", params={"creation_id": c["id"], "access_token": t})
    link = _call("GET", f"{THREADS}/{post_['id']}", params={"fields": "permalink", "access_token": t})
    return {"url": link.get("permalink"), "id": post_["id"]}


def _post_tiktok(acct, job_id, video, result, mode):
    bearer = {"Authorization": f"Bearer {acct['access_token']}"}
    info = _call("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                 json_body={}, headers=bearer).get("data", {})
    allowed = info.get("privacy_level_options") or ["SELF_ONLY"]
    privacy = "PUBLIC_TO_EVERYONE" if mode == "public" and "PUBLIC_TO_EVERYONE" in allowed else (
        "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0])
    init = _call("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/", headers=bearer, json_body={
        "post_info": {"title": _caption(result, 2200), "privacy_level": privacy,
                      "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                      "is_aigc": flag("AI_DISCLOSURE", "true")},
        "source_info": {"source": "FILE_UPLOAD", "video_size": len(video), "chunk_size": len(video),
                        "total_chunk_count": 1}}).get("data", {})
    _call("PUT", init["upload_url"], body=video, timeout=900, headers={
        "Content-Type": "video/mp4", "Content-Range": f"bytes 0-{len(video) - 1}/{len(video)}"})

    def done():
        s = _call("POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/", headers=bearer,
                  json_body={"publish_id": init["publish_id"]}).get("data", {})
        if s.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return True
        return (s.get("fail_reason") or "failed") if s.get("status") == "FAILED" else None

    _wait(done, "TikTok processing")
    # TikTok's API takes no cover image: pick the cover in the TikTok app (Studio -> Download cover)
    return {"url": None, "id": init["publish_id"], "note": f"privacy: {privacy}"
            + ("; set the cover in the TikTok app" if _cover(job_id) else "")}


def _post_x(acct, job_id, video, result, mode):
    bearer = {"Authorization": f"Bearer {acct['access_token']}"}
    init = _call("POST", "https://api.x.com/2/media/upload/initialize", headers=bearer, json_body={
        "media_type": "video/mp4", "total_bytes": len(video), "media_category": "amplify_video"}).get("data", {})
    mid = init["id"]
    step = 4 * 1024 * 1024
    for i, at in enumerate(range(0, len(video), step)):
        body, ctype = _multipart({"segment_index": str(i)}, {"media": ("final.mp4", video[at:at + step], "video/mp4")})
        _call("POST", f"https://api.x.com/2/media/upload/{mid}/append", body=body,
              headers={**bearer, "Content-Type": ctype}, timeout=300)
    _call("POST", f"https://api.x.com/2/media/upload/{mid}/finalize", headers=bearer, body=b"")

    def ready():
        s = _call("GET", "https://api.x.com/2/media/upload", params={"media_id": mid, "command": "STATUS"},
                  headers=bearer).get("data", {}).get("processing_info") or {}
        if s.get("state") in (None, "succeeded"):
            return True
        return str(s.get("error") or "failed") if s.get("state") == "failed" else None

    _wait(ready, "X processing")
    tweet = _call("POST", "https://api.x.com/2/tweets", headers=bearer, json_body={
        "text": _caption(result, 280), "media": {"media_ids": [mid]}}).get("data", {})
    return {"url": f"https://x.com/i/status/{tweet.get('id')}", "id": tweet.get("id")}


POSTERS = {"youtube": _post_youtube, "facebook": _post_facebook, "instagram": _post_instagram,
           "threads": _post_threads, "tiktok": _post_tiktok, "x": _post_x}


def _mark(job_id, platform, **state):
    job = pipeline.Job(job_id)
    social = job.status.setdefault("social", {})
    social[platform] = {**state, "at": pipeline._now()}
    job.save()


def mark_queued(job_id, platforms, mode):
    for p in platforms:
        _mark(job_id, p, state="queued", mode=mode)


def post(platform, job_id, mode="auto"):
    """Upload one finished video to one platform; the result lands in the video's status."""
    p = _check(platform)
    data = pipeline.load_job(job_id)
    if data["status"]["state"] != "done" or not data["result"]:
        raise SocialError("this video is not finished yet")
    try:
        acct = _token(platform)
    except Exception as e:
        _mark(job_id, platform, state="error", mode=mode, error=str(e)[:500])
        raise
    who = (acct.get("profile") or {}).get("name")
    _mark(job_id, platform, state="uploading", mode=mode, account=who)
    try:
        video = (pipeline.job_dir(job_id) / "final.mp4").read_bytes()
        out = POSTERS[platform](acct, job_id, video, data["result"], mode)
    except Exception as e:
        _mark(job_id, platform, state="error", mode=mode, error=str(e)[:500], account=who)
        notify.send("error", f"❌ {p['name']}: posting “{data['result'].get('title')}” failed\n{str(e)[:300]}\n"
                             f"{notify.link(job_id)}")
        raise
    _mark(job_id, platform, state="posted", mode=mode, url=out.get("url"), id=out.get("id"),
          note=out.get("note"), account=who)
    notify.send("posted", f"📤 {p['name']} ({who or 'account'}): “{data['result'].get('title')}”"
                          + (f"\n{out['url']}" if out.get("url") else "") + (f"\n{out['note']}" if out.get("note") else ""))
    return {"platform": platform, "name": p["name"], **out}


def default_platforms():
    """What the daily n8n run posts to: DEFAULT_PLATFORMS that are actually connected."""
    wanted = [x.strip() for x in env("DEFAULT_PLATFORMS", "youtube").split(",") if x.strip() in PLATFORMS]
    return [x for x in wanted if (_account(x) or {}).get("access_token")]
