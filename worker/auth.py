"""Studio accounts: e-mail + password, sessions, two-step codes, password resets, the marketing list.

Stored in /data/users.json and /data/sessions.json. Passwords are scrypt hashes; session tokens,
reset links and backup codes are kept only as SHA-256 hashes. For the SaaS version swap this module's
storage for a database; the functions keep their shape.

Lost phone and backup codes (owner / super admin):  docker compose exec worker python auth.py reset-2fa EMAIL
"""
import base64
import csv
import hashlib
import hmac
import io
import re
import secrets
import struct
import sys
import threading
import os
import time

import plans
import space

# Super admins see and control every account (SUPERADMIN_EMAILS in .env, comma separated). The first
# account of a fresh Studio is its 'owner' with the same powers; everybody else is a 'user'.
SUPERADMINS = {x.strip().lower() for x in os.environ.get("SUPERADMIN_EMAILS", "").split(",") if x.strip()}
FULL_ROLES = ("superadmin", "owner")         # every action on every account (but other super admins)
ADMIN_ROLES = FULL_ROLES + ("admin",)       # may open the Users page
ROLES = ("user", "admin", "owner")          # what a full admin may give; superadmin comes only from .env
ACTIONS = ("password", "block", "logout", "delete", "role", "plan", "twostep")
UNSUBSCRIBE_REASONS = ("too many e-mails", "not useful", "I stopped using Vid Studio", "never signed up for it", "other")
SOURCE_KEYS = ("ref", "utm_source", "utm_medium", "utm_campaign", "referrer", "landing", "wanted_plan")
INVITES = space.ROOT / "invites.json"
USERS = space.ROOT / "users.json"
SESSIONS = space.ROOT / "sessions.json"
RESETS = space.ROOT / "resets.json"
SESSION_DAYS = 30
MIN_PASSWORD = 8
LOCK_TRIES = 5            # wrong passwords or codes in a row ...
LOCK_MINUTES = 15         # ... lock the e-mail this long (a password reset unlocks it at once)
RESET_MINUTES = 30        # a reset link works this long, once
RESETS_PER_HOUR = 3       # reset e-mails per account per hour
CODE_MINUTES = 5          # time to type the two-step code after the password
ISSUER = "Vid Studio"
COMMON = {"password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123", "qwertyui",
          "11111111", "00000000", "iloveyou", "abc12345", "letmein1", "welcome1", "admin123", "qwerty12",
          "1q2w3e4r", "sunshine", "princess", "football", "baseball", "vidstudio", "youtube1"}
_lock = threading.Lock()
_failures = {}   # e-mail -> (wrong tries in a row, last try): locks out password guessing
_pending = {}    # hash of a two-step ticket -> (user id, expires): the password was right, the code is next


def alert(kind, text, key=""):
    """Security alerts for the admins; app.py points this at Telegram. Never raises."""


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


def _check_password(password, email=""):
    password = password or ""
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"the password needs at least {MIN_PASSWORD} characters")
    if len(password) > 200:
        raise AuthError("the password is too long (200 characters at most)")
    low = password.lower()
    if low in COMMON or len(set(low)) < 4 or (email and low in (email.lower(), email.lower().split("@")[0])):
        raise AuthError("this password is too easy to guess - pick another one")


def public(user):
    """What the browser may see about a user."""
    return {k: user.get(k) for k in ("id", "email", "role", "marketing", "marketing_at", "created",
                                      "last_login", "disabled", "plan", "trial_until", "timezone", "language")} | {
        "plan": plans.name(user), "source": source_label(user), "sessions": active_sessions(user["id"]),
        "two_step": bool(user.get("totp_on")), "two_step_required": two_step_required(user),
        "backup_codes_left": len(user.get("backup_codes") or []) if user.get("totp_on") else 0}


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


def register(email, password, marketing=False, source=None, timezone=None, language=None, invite=None):
    """A new account. The very first one is the owner and takes over the channels made before
    accounts existed. marketing: the separate, optional 'news and offers' tick box.
    source: where they came from (?ref= / utm_* of the sign-up link, the referring site).
    invite: an invite code - the account starts with the plan the invite gives; without one it has no plan."""
    email = _email(email)
    _check_password(password, email)
    with _lock:
        users = _users()
        if any(u["email"] == email for u in users):
            raise AuthError("this e-mail already has an account - sign in instead")
        plan = _use_invite(invite, email) if invite else "none"
        user = {"id": secrets.token_hex(8), "email": email, "password": _hash(password),
                "role": "superadmin" if email in SUPERADMINS else "owner" if not users else "user",
                "created": _now(), "last_login": None,
                "marketing": bool(marketing), "marketing_at": _now() if marketing else None,
                "disabled": False, "plan": plan, "trial_until": plans.trial_until() if plan == "trial" else None,
                "invite": invite if plan != "none" else None,
                "source": {k: _clean(v, 300) for k, v in (source or {}).items() if k in SOURCE_KEYS and v},
                **_locale(timezone, language)}
        users.append(user)
        _save_users(users)
    if user["role"] in FULL_ROLES and len(users) == 1:
        space.claim_unowned(user["id"])
    return user


def get(user_id):
    return next((u for u in _users() if u["id"] == user_id), None)


def _locked(email):
    """Minutes left of a lock after LOCK_TRIES wrong tries, else 0."""
    count, last = _failures.get(email, (0, 0))
    left = LOCK_MINUTES * 60 - (time.time() - last)
    return max(1, round(left / 60)) if count >= LOCK_TRIES and left > 0 else 0


def _lock_error(minutes):
    return AuthError(f"too many wrong tries - this account is locked for {minutes} more minute(s). "
                     "Forgot your password? Use “Forgot password?” to reset it.")


def _wrong(email, what):
    count, _ = _failures.get(email, (0, 0))
    count = 1 if count >= LOCK_TRIES else count + 1   # an earlier lock ran out: count again
    _failures[email] = (count, time.time())
    if count == LOCK_TRIES:
        alert("lock", f"🔒 {email}: {LOCK_TRIES} wrong {what} in a row - the account is locked for "
                      f"{LOCK_MINUTES} minutes.\nIf it was not its owner, someone is guessing.", email)


def authenticate(email, password, timezone=None, language=None):
    """The password step. With two-step sign-in on, the caller asks for the code next (start_two_step)."""
    email = (email or "").strip().lower()
    if _locked(email):
        raise _lock_error(_locked(email))
    user = next((u for u in _users() if u["email"] == email), None)
    if not user or not _matches(password or "", user["password"]):
        _wrong(email, "passwords")
        if _locked(email):
            raise _lock_error(LOCK_MINUTES)
        raise AuthError("wrong e-mail or password")
    if user.get("disabled"):
        raise AuthError("this account is blocked - ask the Studio admin")
    if not user.get("totp_on"):
        _signed_in(user, timezone, language)
    return user


def _signed_in(user, timezone=None, language=None):
    _failures.pop(user["email"], None)
    _update(user["id"], last_login=_now(), **_locale(timezone, language))


# ------------------------------------------------------------------ two-step sign-in (TOTP, RFC 6238)

def two_step_required(user):
    """The super admin and the Studio owner must use a code from an authenticator app (not in the sandbox,
    a test copy where it would only slow down testing; it can still be turned on there by hand)."""
    return bool(user) and user.get("role") in FULL_ROLES and os.environ.get("SANDBOX") != "1"


def _totp(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    at = mac[-1] & 15
    return "%06d" % ((struct.unpack(">I", mac[at:at + 4])[0] & 0x7FFFFFFF) % 1000000)


def _app_code_ok(secret, code, last_step=0):
    """The 30-second step the code belongs to (this one or the one before / after), else None.
    A step already used is refused, so a code seen over someone's shoulder cannot be used again."""
    now = int(time.time() // 30)
    for step in (now - 1, now, now + 1):
        if step > last_step and hmac.compare_digest(_totp(secret, step), code):
            return step
    return None


def _backup_hash(code):
    return hashlib.sha256(re.sub(r"[^a-z0-9]", "", code.lower()).encode()).hexdigest()


def check_code(user, code):
    """'app' or 'backup' when the code is right (a backup code is used up), else None."""
    code = re.sub(r"[\s-]", "", code or "").lower()
    if not user or not user.get("totp_on"):
        return None
    if re.fullmatch(r"\d{6}", code):
        step = _app_code_ok(user["totp_secret"], code, user.get("totp_step", 0))
        if step:
            _update(user["id"], totp_step=step)
            return "app"
    elif re.fullmatch(r"[a-z0-9]{10}", code):
        codes = user.get("backup_codes") or []
        if _backup_hash(code) in codes:
            _update(user["id"], backup_codes=[c for c in codes if c != _backup_hash(code)])
            alert("backup", f"🔑 {user['email']} signed in with a backup code ({len(codes) - 1} left).", user["email"])
            return "backup"
    return None


def start_two_step(user):
    """A short-lived ticket for the code step; no session until the code is right."""
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, (_, exp) in _pending.items() if exp < now]:
        _pending.pop(k, None)
    _pending[_token_key(ticket)] = (user["id"], now + CODE_MINUTES * 60)
    return ticket


def finish_two_step(ticket, code, timezone=None, language=None):
    key = _token_key(ticket or "")
    user_id, expires = _pending.get(key, (None, 0))
    user = get(user_id) if user_id and expires > time.time() else None
    if not user or user.get("disabled"):
        _pending.pop(key, None)
        raise AuthError("the sign-in took too long - enter your e-mail and password again")
    if _locked(user["email"]):
        _pending.pop(key, None)
        raise _lock_error(_locked(user["email"]))
    if not check_code(user, code):
        _wrong(user["email"], "two-step codes")
        if _locked(user["email"]):
            _pending.pop(key, None)
            raise _lock_error(LOCK_MINUTES)
        raise AuthError("wrong code - type the 6 digits your authenticator app shows now, or a backup code")
    _pending.pop(key, None)
    _signed_in(user, timezone, language)
    return get(user["id"])


def two_step_begin(user_id):
    """A new secret, not active until a code from it is typed (two_step_enable)."""
    import urllib.parse
    user = get(user_id)
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    _update(user_id, totp_pending=secret)
    q = urllib.parse.quote
    uri = (f"otpauth://totp/{q(ISSUER + ':' + user['email'], safe='')}?secret={secret}"
           f"&issuer={q(ISSUER, safe='')}&algorithm=SHA1&digits=6&period=30")
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "uri": uri}


def _new_backup_codes(user_id):
    codes = ["".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10)) for _ in range(8)]
    _update(user_id, backup_codes=[_backup_hash(c) for c in codes])
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def two_step_enable(user_id, code):
    """Turns it on when the code from the new secret is right; returns 8 backup codes (shown once)."""
    secret = get(user_id).get("totp_pending")
    code = re.sub(r"\s", "", code or "")
    step = _app_code_ok(secret, code) if secret and re.fullmatch(r"\d{6}", code) else None
    if not step:
        raise AuthError("this code does not match - check that the phone's clock is right and type the code shown now")
    _update(user_id, totp_on=True, totp_secret=secret, totp_step=step, totp_pending=None, totp_since=_now())
    return _new_backup_codes(user_id)


def two_step_disable(user_id, password, code):
    user = get(user_id)
    if two_step_required(user):
        raise AuthError("two-step sign-in is required for the super admin and the Studio owner")
    if not _matches(password or "", user["password"]) or not check_code(user, code):
        raise AuthError("the password or the code is wrong")
    _update(user_id, totp_on=False, totp_secret=None, totp_step=0, totp_pending=None, backup_codes=[])
    alert("2fa-off", f"🔓 {user['email']} turned two-step sign-in off.", user["email"])


def two_step_reset(user_id):
    """Off and forgotten, every device signed out (an admin for someone who lost the phone, or the
    reset-2fa command). The owner / super admin is asked to set it up again at the next sign-in."""
    _update(user_id, totp_on=False, totp_secret=None, totp_step=0, totp_pending=None, backup_codes=[])
    end_sessions(user_id)


def new_backup_codes(user_id, code):
    if not check_code(get(user_id), code):
        raise AuthError("wrong code")
    return _new_backup_codes(user_id)


# ------------------------------------------------------------------ password reset by e-mail

def _resets():
    data = space._read(RESETS, {}) or {}
    now = time.time()
    return {k: v for k, v in data.items() if v.get("created", 0) > now - 3600 or v.get("expires", 0) > now}


def request_reset(email):
    """(user, token) for a reset e-mail, or (None, None) when there is nothing to send: no such account,
    a blocked one, or RESETS_PER_HOUR already sent. The caller answers the same in every case, so the
    page never tells who has an account."""
    email = (email or "").strip().lower()
    user = next((u for u in _users() if u["email"] == email), None)
    if not user or user.get("disabled"):
        return None, None
    with _lock:
        data = _resets()
        mine = [k for k, v in data.items() if v["user"] == user["id"]]
        if len(mine) >= RESETS_PER_HOUR:
            return None, None
        for k in mine:                         # a new link makes the older ones stop working
            data[k]["used"] = True
        token = secrets.token_urlsafe(32)
        data[_token_key(token)] = {"user": user["id"], "created": time.time(),
                                   "expires": time.time() + RESET_MINUTES * 60, "used": False}
        space._write(RESETS, data)
    return user, token


def _reset_user(token):
    entry = _resets().get(_token_key(token or ""))
    if not entry or entry["used"] or entry["expires"] < time.time():
        return None
    user = get(entry["user"])
    return user if user and not user.get("disabled") else None


def reset_info(token):
    """The e-mail the link is for, partly hidden (j***@gmail.com), or None when the link does not work."""
    user = _reset_user(token)
    if not user:
        return None
    name, _, domain = user["email"].partition("@")
    return {"email": f"{name[:1]}***@{domain}", "two_step": bool(user.get("totp_on"))}


def finish_reset(token, new):
    user = _reset_user(token)
    if not user:
        raise AuthError("this link has expired or was already used - ask for a new one")
    _check_password(new, user["email"])
    with _lock:
        data = _resets()
        for v in data.values():
            if v["user"] == user["id"]:
                v["used"] = True
        space._write(RESETS, data)
    _update(user["id"], password=_hash(new))
    _failures.pop(user["email"], None)          # the reset unlocks the account
    end_sessions(user["id"])                    # every device signs in again
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
    _check_password(new, user["email"])
    _update(user_id, password=_hash(new))
    end_sessions(user_id, keep=None)


def set_password(user_id, new):
    """An admin sets someone's password (people can also reset their own by e-mail)."""
    _check_password(new, (get(user_id) or {}).get("email", ""))
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
    """none | trial (plans.PLANS days from today) | standard | pro. Payments come later; until then an admin sets it."""
    if plan not in plans.GRANTABLE:
        raise AuthError("unknown plan")
    return _update(user_id, plan=plan, trial_until=plans.trial_until() if plan == "trial" else None)


# ------------------------------------------------------------------ signed file links

SECRET_FILE = space.ROOT / "secret.key"   # made on first use, never leaves /data


def _secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip().encode()


def _file_sig(job_id, name, exp):
    return hmac.new(_secret(), f"{job_id}/{name}/{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def sign_file(job_id, name, hours=24):
    """A query string that lets a platform (Instagram, Threads) fetch one file of one video without
    signing in, for `hours` hours. Every other file needs a session."""
    exp = int(time.time()) + hours * 3600
    return f"exp={exp}&sig={_file_sig(job_id, name, exp)}"


def file_signed(job_id, name, exp, sig):
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    return exp >= time.time() and hmac.compare_digest(_file_sig(job_id, name, exp), sig or "")


# ------------------------------------------------------------------ invites

def _invites():
    return space._read(INVITES, []) or []


def create_invite(created_by, plan, uses=1, days=14, note=""):
    """A sign-up link that gives a plan: uses = how many accounts may use it, days = how long it works."""
    if plan not in plans.GRANTABLE or plan == "none":
        raise AuthError("an invite gives trial, standard or pro")
    invite = {"code": secrets.token_urlsafe(9), "plan": plan, "uses_left": max(1, min(500, int(uses))),
              "expires": time.strftime("%Y-%m-%d", time.gmtime(time.time() + max(1, min(365, int(days))) * 86400)),
              "note": _clean(note, 60), "created_by": created_by, "created": _now(), "used_by": []}
    with _lock:
        space._write(INVITES, _invites() + [invite])
    return invite


def invite_info(code):
    """What the sign-up page may show about an invite (None when it does not work any more)."""
    inv = next((i for i in _invites() if i["code"] == code), None)
    if not inv or inv["uses_left"] < 1 or inv["expires"] < time.strftime("%Y-%m-%d", time.gmtime()):
        return None
    return {"plan": inv["plan"], "label": plans.PLANS[inv["plan"]]["label"]}


def _use_invite(code, email):
    """Called under _lock by register(): spends one use and returns the plan it gives."""
    if not invite_info(code):
        raise AuthError("this invite link has expired or was already used - ask for a new one")
    invites = _invites()
    inv = next(i for i in invites if i["code"] == code)
    inv["uses_left"] -= 1
    inv["used_by"].append({"email": email, "at": _now()})
    space._write(INVITES, invites)
    return inv["plan"]


def list_invites():
    return sorted(_invites(), key=lambda i: i["created"], reverse=True)


def revoke_invite(code):
    with _lock:
        space._write(INVITES, [i for i in _invites() if i["code"] != code])


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
    Super admin / owner: everything, except to super admins. Admin: password, block, log out and plan
    of plain users only - never delete, never another admin. Nobody does any of it to themselves."""
    if not can_see(actor, target) or actor["id"] == target["id"] or target["role"] == "superadmin":
        return False
    if actor["role"] in FULL_ROLES:
        return not (actor["role"] == "owner" and target["role"] == "owner")
    return target["role"] == "user" and action in ("password", "block", "logout", "plan")


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
                         "wanted_plan": (u.get("source") or {}).get("wanted_plan") or "",
                         "timezone": u.get("timezone") or "", "language": u.get("language") or "",
                         "plan": plans.name(u), "trial_until": u.get("trial_until") or "",
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


# ------------------------------------------------------------------ command line

if __name__ == "__main__":
    # docker compose exec worker python auth.py reset-2fa EMAIL   - lost phone and backup codes
    if len(sys.argv) == 3 and sys.argv[1] == "reset-2fa":
        found = next((u for u in _users() if u["email"] == sys.argv[2].strip().lower()), None)
        if not found:
            sys.exit("no account with this e-mail")
        two_step_reset(found["id"])
        print(f"two-step sign-in is off for {found['email']}: it is set up again at the next sign-in")
    else:
        sys.exit("usage: python auth.py reset-2fa EMAIL")
