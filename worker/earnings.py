"""Money and reach for the Studio's Earnings page.

Where the numbers come from:
- YouTube Data + Analytics API: subscribers, views, watch time, Shorts views and - once the channel
  is in the YouTube Partner Program - estimated revenue, per day and per video.
- The other platforms: views / likes per post where their API offers it (best effort).
- Income you type in yourself: sponsors, affiliate links, TikTok Creator Rewards, anything.
- Costs: each video's estimated AI cost plus MONTHLY_COSTS (your subscriptions and tools).

Stats are fetched on demand (Earnings page -> Refresh, or automatically when older than 6 hours)
and cached in data/stats.json; income entries live in data/income.json.
"""
import datetime as dt
import time
import uuid

import pipeline
import social
from pipeline import DATA, JOBS, PipelineError

STATS = DATA / "stats.json"
INCOME = DATA / "income.json"
YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
INCOME_SOURCES = ["YouTube (paid out)", "TikTok Creator Rewards", "Instagram / Facebook", "X", "Sponsor",
                  "Affiliate", "Digital product", "Other"]

# YouTube Partner Program thresholds for Shorts creators (as published by YouTube in 2025;
# check https://support.google.com/youtube/answer/72851 if YouTube changes them)
YPP_TIERS = [
    {"name": "Fan funding & Shopping", "subs": 500, "shorts_views_90d": 3_000_000, "watch_hours": 3000},
    {"name": "Ad revenue (full YPP)", "subs": 1000, "shorts_views_90d": 10_000_000, "watch_hours": 4000},
]


# ------------------------------------------------------------------ helpers

def _today():
    return dt.date.today()


def _posts():
    """Every post VidAuto made: job, platform, the platform's id and link, title."""
    out = []
    if not JOBS.exists():
        return out
    for d in JOBS.iterdir():
        status = pipeline._read_json(d / "status.json") or {}
        result = pipeline._read_json(d / "result.json") or {}
        posted = {p: s for p, s in (status.get("social") or {}).items() if s.get("state") == "posted"}
        yt = status.get("youtube") or {}
        if yt.get("state") == "uploaded" and "youtube" not in posted:
            posted["youtube"] = {"id": yt.get("video_id"), "url": yt.get("url")}
        for platform, s in posted.items():
            out.append({"job_id": d.name, "platform": platform, "id": s.get("id"), "url": s.get("url"),
                        "title": result.get("title") or status.get("topic")})
    return out


def _connected(platform):
    return bool((social._account(platform) or {}).get("access_token"))


# ------------------------------------------------------------------ YouTube

def _yt_report(bearer, start, end, metrics, dimensions=None, **extra):
    params = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
              "metrics": metrics, **extra}
    if dimensions:
        params["dimensions"] = dimensions
    data = social._call("GET", YT_ANALYTICS, params=params, headers=bearer)
    cols = [c["name"] for c in data.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in data.get("rows") or []]


def _youtube(stats, posts):
    acct = social._token("youtube")
    bearer = {"Authorization": f"Bearer {acct['access_token']}"}
    yt = stats["youtube"]
    ch = (social._call("GET", "https://www.googleapis.com/youtube/v3/channels",
                       params={"part": "statistics", "mine": "true"}, headers=bearer).get("items") or [{}])[0]
    st = ch.get("statistics", {})
    yt["subscribers"] = int(st.get("subscriberCount", 0) or 0)
    yt["total_views"] = int(st.get("viewCount", 0) or 0)

    ids = [p["id"] for p in posts if p["platform"] == "youtube" and p["id"]]
    for i in range(0, len(ids), 50):
        items = social._call("GET", "https://www.googleapis.com/youtube/v3/videos", headers=bearer,
                             params={"part": "statistics", "id": ",".join(ids[i:i + 50])}).get("items") or []
        for v in items:
            s = v.get("statistics", {})
            stats["posts"][f"youtube:{v['id']}"] = {"views": int(s.get("viewCount", 0) or 0),
                                                    "likes": int(s.get("likeCount", 0) or 0),
                                                    "comments": int(s.get("commentCount", 0) or 0)}

    end, start90, start365 = _today(), _today() - dt.timedelta(days=89), _today() - dt.timedelta(days=364)
    # retention per video, for the A/B results: how much of each Short people actually watched
    for i in range(0, len(ids), 50):
        try:
            rows = _yt_report(bearer, start365, end, "views,averageViewDuration,averageViewPercentage,subscribersGained",
                              "video", filters="video==" + ",".join(ids[i:i + 50]))
        except PipelineError as e:
            stats["errors"]["youtube retention"] = str(e)[:300]
            break
        for r in rows:
            key = f"youtube:{r['video']}"
            if key in stats["posts"]:
                stats["posts"][key].update(avg_view_pct=round(float(r.get("averageViewPercentage", 0) or 0), 1),
                                           avg_view_s=round(float(r.get("averageViewDuration", 0) or 0), 1),
                                           subs=int(r.get("subscribersGained", 0) or 0))
    # revenue needs the YouTube Partner Program and the monetary scope; without them ask without it
    try:
        daily = _yt_report(bearer, start90, end, "views,estimatedMinutesWatched,subscribersGained,"
                           "subscribersLost,estimatedRevenue", "day", sort="day")
        yt["revenue_available"] = True
    except PipelineError as e:
        yt["revenue_available"], yt["revenue_note"] = False, str(e)[:300]
        daily = _yt_report(bearer, start90, end, "views,estimatedMinutesWatched,subscribersGained,subscribersLost",
                           "day", sort="day")
    yt["daily"] = [{"date": r["day"], "views": int(r.get("views", 0)),
                    "minutes": float(r.get("estimatedMinutesWatched", 0)),
                    "subs": int(r.get("subscribersGained", 0)) - int(r.get("subscribersLost", 0)),
                    "revenue": float(r.get("estimatedRevenue", 0) or 0)} for r in daily]
    if yt["revenue_available"]:
        for r in _yt_report(bearer, start365, end, "views,estimatedRevenue", "video", sort="-views", maxResults=200):
            key = f"youtube:{r['video']}"
            if key in stats["posts"]:
                stats["posts"][key]["revenue"] = float(r.get("estimatedRevenue", 0) or 0)
    try:
        rows = _yt_report(bearer, start90, end, "views", "creatorContentType")
        yt["shorts_views_90d"] = sum(int(r["views"]) for r in rows
                                     if str(r.get("creatorContentType", "")).upper() == "SHORTS")
    except PipelineError:  # API change or no data: all views as an upper bound
        yt["shorts_views_90d"] = sum(x["views"] for x in yt["daily"])
    minutes = _yt_report(bearer, start365, end, "estimatedMinutesWatched")
    yt["watch_hours_365d"] = round(sum(float(r.get("estimatedMinutesWatched", 0)) for r in minutes) / 60, 1)


# ------------------------------------------------------------------ other platforms (best effort)

def _tiktok(stats, posts):
    bearer = {"Authorization": f"Bearer {social._token('tiktok')['access_token']}"}
    videos = social._call("POST", "https://open.tiktokapis.com/v2/video/list/", headers=bearer,
                          params={"fields": "id,title,share_url,view_count,like_count,comment_count"},
                          json_body={"max_count": 20}).get("data", {}).get("videos") or []
    # TikTok only hands back a publish id at upload time, so posts are matched by their title
    for p in (x for x in posts if x["platform"] == "tiktok"):
        title = (p["title"] or "").lower()[:40]
        v = next((v for v in videos if title and (v.get("title") or "").lower().startswith(title)), None)
        if v:
            stats["posts"][f"tiktok:{p['id']}"] = {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
                                                   "comments": v.get("comment_count", 0), "url": v.get("share_url")}


def _instagram(stats, posts):
    t = social._token("instagram")["access_token"]
    for p in (x for x in posts if x["platform"] == "instagram" and x["id"]):
        m = social._call("GET", f"{social.IG_GRAPH}/{p['id']}",
                         params={"fields": "like_count,comments_count", "access_token": t})
        views = 0
        try:
            ins = social._call("GET", f"{social.IG_GRAPH}/{p['id']}/insights", params={"metric": "views", "access_token": t})
            views = ((ins.get("data") or [{}])[0].get("values") or [{}])[0].get("value", 0)
        except PipelineError:
            pass
        stats["posts"][f"instagram:{p['id']}"] = {"views": views, "likes": m.get("like_count", 0),
                                                  "comments": m.get("comments_count", 0)}


def _threads(stats, posts):
    t = social._token("threads")["access_token"]
    for p in (x for x in posts if x["platform"] == "threads" and x["id"]):
        ins = social._call("GET", f"{social.THREADS}/{p['id']}/insights",
                           params={"metric": "views,likes,replies", "access_token": t}).get("data") or []
        val = {m["name"]: ((m.get("values") or [{}])[0].get("value", 0)) for m in ins}
        stats["posts"][f"threads:{p['id']}"] = {"views": val.get("views", 0), "likes": val.get("likes", 0),
                                                "comments": val.get("replies", 0)}


def _x(stats, posts):
    bearer = {"Authorization": f"Bearer {social._token('x')['access_token']}"}
    ids = [p["id"] for p in posts if p["platform"] == "x" and p["id"]]
    for i in range(0, len(ids), 100):
        for tw in social._call("GET", "https://api.x.com/2/tweets", headers=bearer,
                               params={"ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics"}).get("data") or []:
            m = tw.get("public_metrics", {})
            stats["posts"][f"x:{tw['id']}"] = {"views": m.get("impression_count", 0), "likes": m.get("like_count", 0),
                                               "comments": m.get("reply_count", 0)}


FETCHERS = {"youtube": _youtube, "tiktok": _tiktok, "instagram": _instagram, "threads": _threads, "x": _x}


def refresh():
    """Fetch fresh numbers from every connected platform. One failing platform does not stop the rest."""
    before = pipeline._read_json(STATS) or {}
    stats = {"at": pipeline._now(), "at_ts": time.time(), "posts": {}, "youtube": {}, "errors": {}}
    posts = _posts()
    for platform, fetch in FETCHERS.items():
        if not _connected(platform):
            continue
        try:
            fetch(stats, posts)
        except Exception as e:
            stats["errors"][platform] = str(e)[:400]
    pipeline._write_json(STATS, stats)
    _alerts(before, stats)
    return stats


def _alerts(before, after):
    """Telegram: new YouTube revenue, a monetization threshold reached, finished A/B tests."""
    import abtest
    import notify
    try:
        old = {x["date"]: x["revenue"] for x in (before.get("youtube") or {}).get("daily") or []}
        new = (after.get("youtube") or {}).get("daily") or []
        # day by day against the previous fetch, so days sliding out of the 90-day window do not count
        gained = sum(x["revenue"] - old.get(x["date"], 0) for x in new)
        if old and gained >= 0.01:
            month = sum(x["revenue"] for x in new if x["date"][:7] == _today().isoformat()[:7])
            notify.send("money", f"💰 +${gained:,.2f} YouTube revenue since the last check "
                                 f"(${month:,.2f} this month so far).")
        yt_b, yt_a = before.get("youtube") or {}, after.get("youtube") or {}
        for tier in YPP_TIERS:
            def met(yt):
                return (yt.get("subscribers") or 0) >= tier["subs"] and (
                    (yt.get("shorts_views_90d") or 0) >= tier["shorts_views_90d"]
                    or (yt.get("watch_hours_365d") or 0) >= tier["watch_hours"])
            if yt_b and met(yt_a) and not met(yt_b):
                notify.send("money", f"🎉 Monetization threshold reached: {tier['name']}. "
                                     "Apply in YouTube Studio → Earn.")
        abtest.notify_finished()
    except Exception as e:  # alerts are a bonus; the numbers are already saved
        print(f"[warn] earnings alerts: {e}", flush=True)


def stale(hours=6):
    s = pipeline._read_json(STATS) or {}
    return time.time() - s.get("at_ts", 0) > hours * 3600 and any(_connected(p) for p in FETCHERS)


# ------------------------------------------------------------------ manual income

def income():
    return pipeline._read_json(INCOME, []) or []


def add_income(date, source, amount, note="", job_id=None):
    try:
        day = dt.date.fromisoformat(date or _today().isoformat())
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise PipelineError("date must be YYYY-MM-DD and amount a number")
    entry = {"id": uuid.uuid4().hex[:10], "date": day.isoformat(), "source": (source or "Other")[:60],
             "amount": amount, "note": (note or "")[:300], "job_id": job_id or None}
    entries = income() + [entry]
    pipeline._write_json(INCOME, sorted(entries, key=lambda x: x["date"], reverse=True))
    return entry


def delete_income(entry_id):
    entries = income()
    left = [x for x in entries if x["id"] != entry_id]
    if len(left) == len(entries):
        raise PipelineError("no such entry")
    pipeline._write_json(INCOME, left)


# ------------------------------------------------------------------ summary

def summary():
    """Everything the Earnings page shows, already added up."""
    stats = pipeline._read_json(STATS) or {"posts": {}, "youtube": {}, "errors": {}}
    yt = stats.get("youtube") or {}
    daily = yt.get("daily") or []
    entries = income()
    today = _today()
    monthly_fixed = pipeline._num("MONTHLY_COSTS", 0)

    jobs = []
    for d in (JOBS.iterdir() if JOBS.exists() else []):
        r = pipeline._read_json(d / "result.json") or {}
        s = pipeline._read_json(d / "status.json") or {}
        if r and not r.get("mock"):
            jobs.append({"cost": float(r.get("estimated_cost_usd") or 0), "month": (s.get("created") or "")[:7]})
    first = min([j["month"] for j in jobs if j["month"]] or [today.isoformat()[:7]])

    months, y, m = [], today.year, today.month
    for _ in range(6):  # the last 6 calendar months, oldest first
        months.append(f"{y:04}-{m:02}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    by_month = []
    for mo in reversed(months):
        yt_rev = sum(x["revenue"] for x in daily if x["date"][:7] == mo)
        manual = sum(x["amount"] for x in entries if x["date"][:7] == mo)
        costs = sum(j["cost"] for j in jobs if j["month"] == mo) + (monthly_fixed if mo >= first else 0)
        by_month.append({"month": mo, "youtube": round(yt_rev, 2), "manual": round(manual, 2),
                         "income": round(yt_rev + manual, 2), "costs": round(costs, 2),
                         "profit": round(yt_rev + manual - costs, 2),
                         "videos": sum(1 for j in jobs if j["month"] == mo)})

    last30 = [x for x in daily if x["date"] >= (today - dt.timedelta(days=29)).isoformat()]
    views90, rev90 = sum(x["views"] for x in daily), sum(x["revenue"] for x in daily)
    rpm = round(rev90 / views90 * 1000, 2) if yt.get("revenue_available") and views90 else None

    per_video = {}
    for p in _posts():
        s = stats["posts"].get(f"{p['platform']}:{p['id']}", {})
        t = per_video.setdefault(p["job_id"], {"job_id": p["job_id"], "title": p["title"], "views": 0, "likes": 0,
                                               "revenue": 0.0, "platforms": {}})
        t["platforms"][p["platform"]] = {"views": s.get("views"), "url": s.get("url") or p["url"]}
        t["views"] += int(s.get("views") or 0)
        t["likes"] += int(s.get("likes") or 0)
        t["revenue"] += float(s.get("revenue") or 0)
    for e in entries:
        if e.get("job_id") in per_video:
            per_video[e["job_id"]]["revenue"] += e["amount"]
    platform_views = {}
    for t in per_video.values():
        for p, s in t["platforms"].items():
            platform_views[p] = platform_views.get(p, 0) + int(s.get("views") or 0)

    subs = yt.get("subscribers")
    ypp = [{**tier, "met": subs is not None and subs >= tier["subs"] and (
        (yt.get("shorts_views_90d") or 0) >= tier["shorts_views_90d"]
        or (yt.get("watch_hours_365d") or 0) >= tier["watch_hours"])} for tier in YPP_TIERS]

    return {
        "refreshed_at": stats.get("at"), "errors": stats.get("errors", {}),
        "connected": {p: _connected(p) for p in social.PLATFORMS},
        "month": by_month[-1], "months": by_month,
        "six_months": {"income": round(sum(x["income"] for x in by_month), 2),
                       "costs": round(sum(x["costs"] for x in by_month), 2),
                       "profit": round(sum(x["profit"] for x in by_month), 2)},
        "youtube": {"subscribers": subs, "total_views": yt.get("total_views"),
                    "views_30d": sum(x["views"] for x in last30), "subs_30d": sum(x["subs"] for x in last30),
                    "revenue_30d": round(sum(x["revenue"] for x in last30), 2),
                    "revenue_available": yt.get("revenue_available"), "revenue_note": yt.get("revenue_note"),
                    "rpm": rpm, "shorts_views_90d": yt.get("shorts_views_90d"),
                    "watch_hours_365d": yt.get("watch_hours_365d"), "daily": last30},
        "ypp": ypp, "platform_views": platform_views,
        "top": sorted(per_video.values(), key=lambda x: -x["views"])[:20],
        "income": entries[:100], "sources": INCOME_SOURCES, "monthly_costs": monthly_fixed,
    }
