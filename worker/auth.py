"""Studio accounts: e-mail + password, sessions, the marketing-consent list.

Stored in /data/users.json and /data/sessions.json. Passwords are scrypt hashes, session tokens are
kept only as SHA-256 hashes. For the SaaS version swap this module's storage for a database;
the functions keep their shape.
"""
import csv
import hashlib
import hmac
import io
import re
import secrets
import threading
import os
import time

import space

# Super admins see and control every account (SUPERADMIN_EMAILS in .env, comma separated). The first
# account of a fresh Studio is its 'owner' with the same powers; everybody else is a 'user'.
SUPERADMINS = {x.strip().lower() for x in os.environ.get("SUPERADMIN_EMAILS", "").split(",") if x.strip()}
FULL_ROLES = ("superadmin", "owner")         # every action on every account (but other super admins)
ADMIN_ROLES = FULL_ROLES + ("admin",)       # may open the Users page
ROLES = ("user", "admin", "owner")          # what a full admin may give; superadmin comes only from .env
ACTIONS = ("password", "block", "logout", "delete", "role", "plan")
PLANS = ("free", "trial", "pro")          # SaaS prep: no payments yet, the super admin sets it by hand
TRIAL_DAYS = 14
UNSUBSCRIBE_REASONS = ("too many e-mails", "not useful", "I stopped using Vid Studio", "never signed up for it", "other")
SOURCE_KEYS = ("ref", "utm_source", "utm_medium", "utm_campaign", "referrer", "landing")
USERS = space.ROOT / "users.json"
SESSIONS = space.ROOT / "sessions.json"
SESSION_DAYS = 30
MIN_PASSWORD = 8
_lock = threading.Lock()
_failures = {}   # e-mail -> (count, last attempt): slows down password guessing


class AuthError(Exception):
    pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _users():
    users = space._read(USERS, []) or []
    for u in users:   # an e-mail added to SUPERADMIN_EMAILS later is promoted on the next read
        if u["email"] in SUPERADMINS:
            u["role"] = "superadmin"
    return users


def is_admin(user):
    return bool(user) and user.get("role") in ADMIN_ROLES


def _save_users(users):
    space._write(USERS, users)


def _email(value):
    email = (value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        raise AuthError("enter a valid e-mail")
    return email[:200]


def _hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _matches(password, stored):
    try:
        _, salt, _digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt)), stored)


def _check_password(password):
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"the password needs at least {MIN_PASSWORD} characters")


def public(user):
    """What the browser may see about a user."""
    return {k: user.get(k) for k in ("id", "email", "role", "marketing", "marketing_at", "created",
                                      "last_login", "disabled", "plan", "trial_until", "timezone", "language")} | {
        "plan": user.get("plan") or "free", "source": source_label(user), "sessions": active_sessions(user["id"])}


def _clean(value, size=100):
    return " ".join(str(value or "").split())[:size]


def source_label(user):
    """Where the account came from, in one word: ref, else utm_source, else the referring site, else 'direct'."""
    s = user.get("source") or {}
    if s.get("ref") or s.get("utm_source"):
        return s.get("ref") or s.get("utm_source")
    ref = s.get("referrer") or ""
    host = re.sub(r"^https?://(www\.)?", "", ref).split("/")[0]
    return host or "direct"


def _locale(timezone=None, language=None):
    """The browser's own time zone and language (no GPS, no IP lookup): mail at the right hour, per market."""
    out = {}
    if timezone and re.fullmatch(r"[A-Za-z_+\-/0-9]{1,60}", timezone):
        out["timezone"] = timezone
    if language and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", language):
        out["language"] = language[:20]
    return out


# ------------------------------------------------------------------ accounts

def has_users():
    return bool(_users())


def register(email, password, marketing=False, source=None, timezone=None, language=None):
    """A new account. The very first one is the owner and takes over the channels made before
    accounts existed. marketing: the separate, optional 'news and offers' tick box.
    source: where they came from (?ref= / utm_* of the sign-up link, the referring site)."""
    email = _email(email)
    _check_password(password)
    with _lock:
        users = _users()
        if any(u["email"] == email for u in users):
            raise AuthError("this e-mail already has an account - sign in instead")
        user = {"id": secrets.token_hex(8), "email": email, "password": _hash(password),
                "role": "superadmin" if email in SUPERADMINS else "owner" if not users else "user",
                "created": _now(), "last_login": None,
                "marketing": bool(marketing), "marketing_at": _now() if marketing else None,
                "disabled": False, "plan": "free", "trial_until": None,
                "source": {k: _clean(v, 300) for k, v in (source or {}).items() if k in SOURCE_KEYS and v},
                **_locale(timezone, language)}
        users.append(user)
        _save_users(users)
    if user["role"] in FULL_ROLES and len(users) == 1:
        space.claim_unowned(user["id"])
    return user


def get(user_id):
    return next((u for u in _users() if u["id"] == user_id), None)


def authenticate(email, password, timezone=None, language=None):
    email = (email or "").strip().lower()
    count, last = _failures.get(email, (0, 0))
    if count >= 5 and time.time() - last < 60 * min(count - 4, 15):
        raise AuthError("too many wrong passwords - wait a few minutes")
    user = next((u for u in _users() if u["email"] == email), None)
    if not user or not _matches(password or "", user["password"]):
        _failures[email] = (count + 1, time.time())
        raise AuthError("wrong e-mail or password")
    if user.get("disabled"):
        raise AuthError("this account is blocked - ask the Studio admin")
    _failures.pop(email, None)
    _update(user["id"], last_login=_now(), **_locale(timezone, language))
    return user


def _update(user_id, **changes):
    with _lock:
        users = _users()
        user = next((u for u in users if u["id"] == user_id), None)
        if not user:
            raise AuthError("no such user")
        user.update(changes)
        _save_users(users)
        return user


def change_password(user_id, old, new):
    user = get(user_id)
    if not user or not _matches(old or "", user["password"]):
        raise AuthError("the current password is wrong")
    _check_password(new)
    _update(user_id, password=_hash(new))
    end_sessions(user_id, keep=None)


def set_password(user_id, new):
    """The owner resets someone's password (until e-mail resets exist)."""
    _check_password(new)
    _update(user_id, password=_hash(new))
    end_sessions(user_id)


def set_marketing(user_id, on, reason=None):
    """reason: why they unsubscribed (one of UNSUBSCRIBE_REASONS, optional) - kept for the Studio stats."""
    if on:
        return _update(user_id, marketing=True, marketing_at=_now(), unsubscribed_at=None, unsubscribe_reason=None)
    was = (get(user_id) or {}).get("marketing")
    changes = {"marketing": False, "marketing_at": None}
    if was:
        changes.update(unsubscribed_at=_now(),
                       unsubscribe_reason=reason if reason in UNSUBSCRIBE_REASONS else None)
    return _update(user_id, **changes)


def set_plan(user_id, plan):
    """free | trial (TRIAL_DAYS from today) | pro. Payments come later; until then the super admin sets it."""
    if plan not in PLANS:
        raise AuthError("the plan must be free, trial or pro")
    until = time.strftime("%Y-%m-%d", time.gmtime(time.time() + TRIAL_DAYS * 86400)) if plan == "trial" else None
    _update(user_id, plan=plan, trial_until=until)


def set_disabled(user_id, off):
    user = get(user_id)
    if user and user["role"] == "superadmin":
        raise AuthError("a super admin cannot be blocked")
    _update(user_id, disabled=bool(off))
    if off:
        end_sessions(user_id)


def delete(user_id):
    user = get(user_id)
    if not user:
        raise AuthError("no such user")
    if user["role"] == "superadmin":
        raise AuthError("a super admin cannot be deleted")
    with _lock:
        _save_users([u for u in _users() if u["id"] != user_id])
    end_sessions(user_id)


def can_see(actor, target):
    """Admins see everybody except super admins; super admins and the owner see all."""
    if not actor or not target or actor.get("role") not in ADMIN_ROLES:
        return False
    return actor["role"] in FULL_ROLES or target["role"] != "superadmin"


def can_manage(actor, target, action):
    """What one account may do to another (action: password | block | logout | delete | role).
    Super admin / owner: everything, except to super admins. Admin: password, block and log out of
    plain users only - never delete, never another admin. Nobody does any of it to themselves."""
    if not can_see(actor, target) or actor["id"] == target["id"] or target["role"] == "superadmin":
        return False
    if actor["role"] in FULL_ROLES:
        return not (actor["role"] == "owner" and target["role"] == "owner")
    if action == "plan":
        return False
    return target["role"] == "user" and action in ("password", "block", "logout")


def rights(actor, target):
    return {a: can_manage(actor, target, a) for a in ACTIONS}


def set_role(user_id, role):
    if role not in ROLES:
        raise AuthError("the role must be user, admin or owner")
    user = get(user_id)
    if not user or user["role"] == "superadmin":
        raise AuthError("this role cannot be changed here")
    _update(user_id, role=role)


def list_users(query=""):
    """All accounts, newest first; query matches part of the e-mail or the id."""
    q = (query or "").strip().lower()
    found = [u for u in _users() if not q or q in u["email"] or q in u["id"]]
    return [public(u) for u in sorted(found, key=lambda u: u.get("created") or "", reverse=True)]


def marketing_csv(usage=None):
    """Only people who ticked 'news and offers', with the date they agreed.
    usage(user_id) -> {column: value}: how they use the Studio (channels, videos, uploads...)."""
    rows = []
    for u in _users():
        if u.get("marketing") and not u.get("disabled"):
            rows.append({"email": u["email"], "user_id": u["id"], "role": u.get("role") or "user",
                         "consent_at": u.get("marketing_at") or "", "signed_up": u.get("created") or "",
                         "last_login": u.get("last_login") or "", "source": source_label(u),
                         "utm_campaign": (u.get("source") or {}).get("utm_campaign") or "",
                         "timezone": u.get("timezone") or "", "language": u.get("language") or "",
                         "plan": u.get("plan") or "free", "trial_until": u.get("trial_until") or "",
                         **(usage(u["id"]) if usage else {})})
    out = io.StringIO()
    columns = list(rows[0]) if rows else ["email", "user_id", "role", "consent_at", "signed_up", "last_login"]
    w = csv.DictWriter(out, fieldnames=columns)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


# ------------------------------------------------------------------ sessions

def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _sessions():
    data = space._read(SESSIONS, {}) or {}
    now = time.time()
    return {k: v for k, v in data.items() if v.get("expires", 0) > now}


def start_session(user_id, channel=None):
    token = secrets.token_urlsafe(32)
    with _lock:
        data = _sessions()
        data[_token_key(token)] = {"user": user_id, "channel": channel,
                                   "expires": time.time() + SESSION_DAYS * 86400}
        space._write(SESSIONS, data)
    return token


def session(token):
    """(user, session) for a cookie value, or (None, None)."""
    if not token:
        return None, None
    s = _sessions().get(_token_key(token))
    user = get(s["user"]) if s else None
    if not user or user.get("disabled"):
        return None, None
    return user, s


def set_session_channel(token, channel):
    with _lock:
        data = _sessions()
        key = _token_key(token)
        if key in data:
            data[key]["channel"] = channel
            space._write(SESSIONS, data)


def end_session(token):
    with _lock:
        data = _sessions()
        data.pop(_token_key(token or ""), None)
        space._write(SESSIONS, data)


def active_sessions(user_id):
    return sum(1 for v in _sessions().values() if v["user"] == user_id)


def end_sessions(user_id, keep=None):
    with _lock:
        data = {k: v for k, v in _sessions().items() if v["user"] != user_id or k == keep}
        space._write(SESSIONS, data)


# ------------------------------------------------------------------ channels a user may open

def channels_of(user):
    return [c for c in space.all_channels() if c.get("owner") == user["id"]]


def may_open(user, cid):
    try:
        return space.get(cid).get("owner") == user["id"]
    except space.SpaceError:
        return False
