"""Channels: each one keeps its own videos, topics, settings, logins and stats in its own folder.

Every request and background job runs inside one channel (a ContextVar); the paths below follow
it. Shared things stay in /data: music, sound effects, previews, the platform app keys.
For the SaaS version this module (with auth.py) is the one to swap for a database and a bucket.
"""
import contextvars
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("DATA_DIR", "/data"))
CHANNELS = ROOT / "channels"
_current = contextvars.ContextVar("channel", default=None)
_lock = threading.Lock()

# what moves from /data into the first channel when the Studio upgrades from one channel to many
OWN = ["jobs", "topics.json", "history.json", "settings.json", "script_rules.md", "parts_queue.json",
       "stats.json", "income.json", "ab_notified.json", "notify_state.json"]
SHARED_ACCOUNTS = {"apps.json", "pending.json"}   # the platform app keys and logins in progress


class SpaceError(Exception):
    pass


# ------------------------------------------------------------------ current channel

NONE = ""   # set for a signed-in user who has no channel yet: never fall back to someone else's


def current():
    cid = _current.get()
    if cid:
        return cid
    if cid == NONE:
        raise SpaceError("create a channel first")
    first = list_ids()
    if not first:
        raise SpaceError("no channel yet - create one in the Studio")
    return first[0]   # background work started outside a request: the oldest channel


def folder(cid=None):
    cid = cid or current()
    if not re.fullmatch(r"[a-z0-9]{6,32}", cid or ""):
        raise SpaceError("bad channel id")
    return CHANNELS / cid


@contextmanager
def use(cid):
    """Run a block inside one channel."""
    folder(cid)  # validates
    token = _current.set(cid)
    try:
        yield cid
    finally:
        _current.reset(token)


def set_current(cid):
    """For request middleware: the rest of the request runs in this channel (NONE = no channel)."""
    if cid != NONE:
        folder(cid)
    _current.set(cid)


def thread(target, *args):
    """Start a background thread that stays in the caller's channel (threads do not inherit it)."""
    ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run, args=(target, *args), daemon=True)
    t.start()
    return t


class Here(os.PathLike):
    """A path inside the current channel, resolved each time it is used, so module-level names like
    JOBS keep working: JOBS / job_id, JOBS.exists(), open(TOPICS)..."""

    def __init__(self, name):
        self.name = name

    def resolve(self):
        return folder() / self.name

    def __fspath__(self):
        return str(self.resolve())

    def __truediv__(self, other):
        return self.resolve() / other

    def __getattr__(self, attr):
        return getattr(self.resolve(), attr)

    def __repr__(self):
        return f"Here({self.name!r})"


def account_file(name):
    """accounts/<name>: app keys are shared, logins (youtube.json, telegram.json...) per channel."""
    base = ROOT / "accounts" if name in SHARED_ACCOUNTS else folder() / "accounts"
    base.mkdir(parents=True, exist_ok=True)
    return base / name


# ------------------------------------------------------------------ channel records

def _read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_ids():
    if not CHANNELS.exists():
        return []
    found = [(_read(d / "channel.json", {}) or {}, d.name) for d in CHANNELS.iterdir() if d.is_dir()]
    return [cid for meta, cid in sorted(found, key=lambda x: x[0].get("created", "")) if meta]


def get(cid):
    meta = _read(folder(cid) / "channel.json")
    if not meta:
        raise SpaceError("no such channel")
    return meta


def all_channels():
    return [get(cid) for cid in list_ids()]


def create(name, owner, template="", settings=None):
    """A new empty channel. settings: the template's Studio settings to start from."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise SpaceError("give the channel a name")
    cid = secrets.token_hex(6)
    meta = {"id": cid, "name": name, "owner": owner, "template": template, "daily": True,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    d = folder(cid)
    d.mkdir(parents=True)
    (d / "jobs").mkdir()
    if settings:
        _write(d / "settings.json", settings)
    _write(d / "channel.json", meta)
    return meta


def update(cid, **changes):
    with _lock:
        meta = get(cid)
        for k, v in changes.items():
            if k == "name":
                v = " ".join((v or "").split())[:60]
                if not v:
                    raise SpaceError("give the channel a name")
            if k in ("name", "daily", "owner"):
                meta[k] = v
        _write(folder(cid) / "channel.json", meta)
        return meta


def delete(cid):
    get(cid)
    shutil.rmtree(folder(cid))


def migrate(owner=""):
    """One channel -> many: move the single channel's files from /data into channels/<id>/.
    Runs once; a no-op when channels already exist or there is nothing to move."""
    if list_ids() or not any((ROOT / x).exists() for x in OWN):
        return None
    meta = create(os.environ.get("SERIES_NAME") or "My channel", owner, template="business")
    d = folder(meta["id"])
    shutil.rmtree(d / "jobs")
    for name in OWN:
        if (ROOT / name).exists():
            shutil.move(str(ROOT / name), str(d / name))
    (d / "jobs").mkdir(exist_ok=True)
    accounts = ROOT / "accounts"
    for f in (accounts.iterdir() if accounts.exists() else []):
        if f.is_file() and f.name not in SHARED_ACCOUNTS:
            (d / "accounts").mkdir(exist_ok=True)
            shutil.move(str(f), str(d / "accounts" / f.name))
    return meta


def claim_unowned(owner):
    """The first account created after the upgrade becomes the owner of the moved channel."""
    for meta in all_channels():
        if not meta.get("owner"):
            update(meta["id"], owner=owner)


def find_job(job_id):
    """The channel a video belongs to (n8n and Telegram only know the video id)."""
    if not re.fullmatch(r"[\w-]+", job_id or ""):
        return None
    return next((cid for cid in list_ids() if (folder(cid) / "jobs" / job_id).is_dir()), None)
