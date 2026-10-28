"""A/B results: which version of a video held viewers best, and which hook types win over time.

A test = an original video plus its versions (variant_of). Once every posted version is AB_DAYS old
and has at least AB_MIN_VIEWS YouTube views, the version with the highest average percentage viewed
(YouTube Analytics, i.e. retention) wins; views break a tie. Across all finished tests the Studio adds up
wins and retention per hook formula and per voice, and - with AB_LEARN on - tells the script writer
which hook formulas this channel's viewers stay for.

Numbers come from data/stats.json (Earnings -> Refresh); nothing here calls a platform itself.
"""
import datetime as dt

import pipeline
from pipeline import DATA, JOBS

NOTIFIED = DATA / "ab_notified.json"


def _parse(ts):
    try:
        return dt.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _formula(result, plan):
    if result.get("hook_formula"):
        return result["hook_formula"]
    hook = " ".join((result.get("hook") or "").split())
    for h in (plan or {}).get("hooks") or []:
        if " ".join(h.get("text", "").split()) == hook:
            return h.get("formula_id") or "unknown"
    return "unknown"


def _videos():
    out = {}
    if not JOBS.exists():
        return out
    for d in JOBS.iterdir():
        status = pipeline._read_json(d / "status.json") or {}
        result = pipeline._read_json(d / "result.json") or {}
        if status.get("state") != "done" or not result or result.get("mock"):
            continue
        out[d.name] = {"status": status, "result": result, "plan": pipeline._read_json(d / "plan.json") or {}}
    return out


def groups():
    stats = (pipeline._read_json(DATA / "stats.json") or {}).get("posts", {})
    days, min_views = int(pipeline._num("AB_DAYS", 7)), int(pipeline._num("AB_MIN_VIEWS", 100))
    videos = _videos()
    bases = sorted({v["status"].get("variant_of") for v in videos.values() if v["status"].get("variant_of")} & set(videos))
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for base in bases:
        ids = [base] + sorted((k for k, v in videos.items() if v["status"].get("variant_of") == base),
                              key=lambda k: videos[k]["result"].get("variant") or "")
        members = []
        for k in ids:
            v = videos[k]
            r, st = v["result"], v["status"]
            yt = st.get("youtube") or {}
            social = st.get("social") or {}
            yt_id = yt.get("video_id") if yt.get("state") == "uploaded" else (social.get("youtube") or {}).get("id")
            posted_at = yt.get("at") if yt_id and yt.get("state") == "uploaded" else (social.get("youtube") or {}).get("at")
            s = stats.get(f"youtube:{yt_id}", {}) if yt_id else {}
            other = {p: (stats.get(f"{p}:{x.get('id')}") or {}).get("views") for p, x in social.items()
                     if p != "youtube" and x.get("state") == "posted"}
            members.append({"job_id": k, "letter": r.get("variant") or "A", "title": r.get("title"),
                            "hook": r.get("hook"), "formula": _formula(r, v["plan"]), "voice": r.get("voice"),
                            "music": r.get("music"), "youtube_id": yt_id, "posted_at": posted_at,
                            "views": s.get("views"), "likes": s.get("likes"),
                            "retention": s.get("avg_view_pct"), "avg_seconds": s.get("avg_view_s"),
                            "seconds": r.get("seconds"), "subs": s.get("subs"), "other_views": other})
        posted = [m for m in members if m["youtube_id"]]
        oldest = min((_parse(m["posted_at"]) for m in posted if _parse(m["posted_at"])), default=None)
        newest = max((_parse(m["posted_at"]) for m in posted if _parse(m["posted_at"])), default=None)
        age = (now - newest).days if newest else 0
        if len(posted) < 2:
            state, note = "not posted", "post at least two versions to YouTube"
        elif age < days:
            state, note = "collecting", f"{days - age} more day(s) of data"
        elif any((m["views"] or 0) < min_views or m["retention"] is None for m in posted):
            state, note = "too few views", f"every version needs {min_views}+ views and retention data (Earnings → Refresh)"
        else:
            state, note = "decided", ""
        winner = None
        if state == "decided":
            winner = max(posted, key=lambda m: (m["retention"], m["views"]))["job_id"]
        out.append({"base": base, "topic": videos[base]["result"].get("topic"), "members": members,
                    "state": state, "note": note, "winner": winner,
                    "started": oldest.isoformat() if oldest else None})
    return sorted(out, key=lambda g: g["started"] or "", reverse=True)


def leaderboard(gs=None):
    """Per hook formula and per voice: tests, wins, average retention and views (decided tests only)."""
    gs = groups() if gs is None else gs
    boards = {"formula": {}, "voice": {}}
    for g in (x for x in gs if x["state"] == "decided"):
        for m in (x for x in g["members"] if x["youtube_id"]):
            for kind in boards:
                row = boards[kind].setdefault(m[kind] or "unknown", {"name": m[kind] or "unknown", "tests": 0, "wins": 0,
                                                                      "_ret": [], "_views": []})
                row["tests"] += 1
                row["wins"] += m["job_id"] == g["winner"]
                row["_ret"].append(m["retention"])
                row["_views"].append(m["views"] or 0)
    out = {}
    for kind, rows in boards.items():
        lst = []
        for r in rows.values():
            lst.append({"name": r["name"], "tests": r["tests"], "wins": r["wins"],
                        "win_rate": round(r["wins"] / r["tests"] * 100) if r["tests"] else 0,
                        "retention": round(sum(r["_ret"]) / len(r["_ret"]), 1) if r["_ret"] else None,
                        "views": round(sum(r["_views"]) / len(r["_views"])) if r["_views"] else 0})
        out[kind] = sorted(lst, key=lambda r: (-r["win_rate"], -(r["retention"] or 0)))
    return out


def advice(board=None):
    """One plain sentence for the creator and for the script writer, or '' with too little data."""
    board = leaderboard() if board is None else board
    rows = [r for r in board["formula"] if r["tests"] >= 2]
    if len(rows) < 2:
        return ""
    best, worst = rows[0], rows[-1]
    text = (f"In {sum(r['wins'] for r in board['formula'])} finished A/B tests, hooks of type '{best['name']}' won "
            f"{best['wins']} of {best['tests']} ({best['retention']}% viewed on average)")
    if worst["name"] != best["name"]:
        text += f"; '{worst['name']}' did worst ({worst['retention']}% viewed)"
    return text + "."


def prompt_hint():
    """Fed into the script prompt when AB_LEARN is on: build more hooks on the formulas that win."""
    if not pipeline.flag("AB_LEARN", "true"):
        return ""
    board = leaderboard()
    rows = [r for r in board["formula"] if r["tests"] >= 2]
    if len(rows) < 2:
        return ""
    good = [r["name"] for r in rows if r["win_rate"] >= 50][:3]
    bad = [r["name"] for r in rows if r["win_rate"] == 0][-2:]
    if not good:
        return ""
    line = (f"PAST A/B TESTS on this channel: viewers stayed longest after hooks built on the formulas "
            f"{', '.join(good)}. Build at least two of the 5 hooks on them.")
    return line + (f" Avoid {', '.join(bad)} unless the topic really needs it." if bad else "")


def summary():
    gs = groups()
    board = leaderboard(gs)
    return {"groups": gs, "leaderboard": board, "advice": advice(board),
            "days": int(pipeline._num("AB_DAYS", 7)), "min_views": int(pipeline._num("AB_MIN_VIEWS", 100)),
            "learn": pipeline.flag("AB_LEARN", "true")}


def notify_finished():
    """After a stats refresh: one Telegram message per test that has just been decided."""
    import notify
    done = set(pipeline._read_json(NOTIFIED, []) or [])
    gs = groups()
    for g in (x for x in gs if x["state"] == "decided" and x["base"] not in done):
        w = next(m for m in g["members"] if m["job_id"] == g["winner"])
        lines = [f"🏆 A/B test finished: {g['topic']}",
                 f"Winner: version {w['letter']} — {w['retention']}% viewed, {w['views']} views",
                 f"Hook ({w['formula']}): {w['hook']}"]
        lines += [f"  {m['letter']}: {m['retention']}% viewed, {m['views']} views" for m in g["members"]
                  if m["youtube_id"] and m["job_id"] != w["job_id"]]
        tip = advice(leaderboard(gs))
        notify.send("ab", "\n".join(lines + ([tip] if tip else []) + [notify.link(g["base"])]))
        done.add(g["base"])
    pipeline._write_json(NOTIFIED, sorted(done))
