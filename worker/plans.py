"""Plans: what an account may do. New accounts have no plan (they see the menu and the plans page,
nothing else); an admin gives a plan by hand or through an invite link. Payments come later.

The daily limit is a rolling 24 hours: a video made at 10:15 frees its slot at 10:15 the next day.
A/B versions count as videos (they cost the same pictures). Admins have no limits.
"""
import datetime
import time

PLANS = {
    "none": {"label": "No plan", "videos_per_day": 0, "channels": 0, "price": None,
             "pitch": "Look around; ask for a trial to start making videos."},
    "trial": {"label": "Trial", "days": 3, "videos_per_day": 1, "channels": 1, "price": "Free",
              "pitch": "Three days to try the whole autopilot on one channel."},
    "standard": {"label": "Standard", "videos_per_day": 1, "channels": 1, "price": None,
                 "pitch": "One finished Short every day, posted for you."},
    "pro": {"label": "Pro", "videos_per_day": 5, "channels": 3, "price": None,
            "pitch": "Full access: up to five videos a day, A/B versions, three channels."},
}
GRANTABLE = ("none", "trial", "standard", "pro")
OLD_NAMES = {"free": "none"}          # accounts made before plans existed
WINDOW = 24 * 3600


def name(user):
    """The plan that is in force now: an ended trial counts as no plan."""
    plan = OLD_NAMES.get(user.get("plan"), user.get("plan")) or "none"
    if plan not in PLANS:
        plan = "none"
    until = user.get("trial_until")
    if plan == "trial" and until and until < datetime.date.today().isoformat():
        return "none"
    return plan


def unlimited(user):
    return (user or {}).get("role") in ("superadmin", "owner", "admin")


def trial_until():
    return (datetime.date.today() + datetime.timedelta(days=PLANS["trial"]["days"])).isoformat()


def recent(created_list, now=None):
    """Creation times (ISO, UTC) that fall in the last 24 hours, oldest first."""
    now = now or time.time()
    out = []
    for iso in created_list:
        try:
            t = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        if now - t < WINDOW:
            out.append(t)
    return sorted(out)


def access(user, created_list):
    """What this account may do right now. created_list: when each of its videos was started."""
    plan = name(user)
    info = PLANS[plan]
    if unlimited(user):
        return {"plan": plan, "label": "Admin", "active": True, "unlimited": True, "videos_per_day": None,
                "used": None, "left": None, "next_at": None, "channels": None, "trial_until": None}
    used = recent(created_list)
    limit = info["videos_per_day"]
    left = max(0, limit - len(used))
    next_at = None
    if limit and not left:
        next_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(used[len(used) - limit] + WINDOW))
    return {"plan": plan, "label": info["label"], "active": limit > 0, "unlimited": False,
            "videos_per_day": limit, "used": len(used), "left": left, "next_at": next_at,
            "channels": info["channels"], "trial_until": user.get("trial_until") if plan == "trial" else None}


def catalog():
    """The plans page: everything a visitor may see about the plans."""
    return [{"key": k, **{f: v.get(f) for f in ("label", "videos_per_day", "channels", "price", "pitch", "days")}}
            for k, v in PLANS.items() if k != "none"]
