"""What this channel's viewers watch to the end - learned from every posted video, not only A/B tests.

For each finished video posted to YouTube at least LEARN_MIN_DAYS ago with LEARN_MIN_VIEWS views, the
Studio joins its YouTube retention (average % viewed, from Earnings -> Refresh) with how it was made:
the hook and its formula, the first frame, the length, the words in the body, whether it came from a
reference. The References page shows the result; with LEARN_FROM_STATS on, the script prompt gets the
best and weakest hooks as examples and the hook formulas that hold viewers best.
"""
import datetime as dt

import pipeline
from pipeline import JOBS


def _parse(ts):
    try:
        return dt.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def videos():
    """Every posted, non-test video with YouTube retention data."""
    import earnings
    stats = (pipeline._read_json(earnings.STATS) or {}).get("posts", {})
    min_days = int(pipeline._num("LEARN_MIN_DAYS", 2))
    min_views = int(pipeline._num("LEARN_MIN_VIEWS", 10))
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    if not JOBS.exists():
        return out
    for d in JOBS.iterdir():
        st = pipeline._read_json(d / "status.json") or {}
        r = pipeline._read_json(d / "result.json") or {}
        if st.get("state") != "done" or not r or r.get("mock"):
            continue
        yt = st.get("youtube") or {}
        soc = (st.get("social") or {}).get("youtube") or {}
        yid = yt.get("video_id") if yt.get("state") == "uploaded" else soc.get("id") if soc.get("state") == "posted" else None
        at = _parse(yt.get("at") if yt.get("state") == "uploaded" else soc.get("at"))
        s = stats.get(f"youtube:{yid}") if yid else None
        if not s or s.get("avg_view_pct") is None or (s.get("views") or 0) < min_views:
            continue
        if at and (now - at).days < min_days:
            continue
        scenes = (pipeline._read_json(d / "scenes.json") or {}).get("scenes") or []
        body = [x.get("text", "") for x in scenes[1:] if not x.get("loop")]
        out.append({"job_id": d.name, "title": r.get("title"), "hook": r.get("hook"),
                    "formula": r.get("hook_formula") or "unknown", "frame": r.get("hook_frame") or "result",
                    "seconds": r.get("seconds"), "words": sum(len(x.split()) for x in body) or None,
                    "retention": s["avg_view_pct"], "avg_seconds": s.get("avg_view_s"), "views": s.get("views") or 0,
                    "likes": s.get("likes") or 0, "subs": s.get("subs") or 0, "youtube_id": yid,
                    "posted": at.isoformat() if at else None, "from_ref": bool(st.get("ref"))})
    return sorted(out, key=lambda v: v["retention"], reverse=True)


def _mid(vs):
    """Median % viewed. Shorts that loop can pass 100% (748% for one rewatched video), so one video must
    not drag the whole group: the median, with each value capped at 200%."""
    x = sorted(min(200.0, v["retention"]) for v in vs)
    n = len(x)
    return round((x[n // 2] if n % 2 else (x[n // 2 - 1] + x[n // 2]) / 2), 1) if n else None


def _group(vs, key):
    rows = {}
    for v in vs:
        rows.setdefault(v[key], []).append(v)
    out = [{"name": k, "videos": len(x), "retention": _mid(x),
            "views": round(sum(v["views"] for v in x) / len(x))} for k, x in rows.items()]
    return sorted(out, key=lambda r: -r["retention"])


def _length(vs):
    bands = [("under 30 s", 0, 30), ("30-40 s", 30, 40), ("over 40 s", 40, 999)]
    out = []
    for name, lo, hi in bands:
        x = [v for v in vs if v["seconds"] and lo <= v["seconds"] < hi]
        if x:
            out.append({"name": name, "videos": len(x), "retention": _mid(x),
                        "views": round(sum(v["views"] for v in x) / len(x))})
    return out


def insights():
    vs = videos()
    if not vs:
        return {"videos": 0, "best": [], "worst": [], "formula": [], "frame": [], "length": [], "advice": "",
                "learn": pipeline.flag("LEARN_FROM_STATS", "true")}
    avg = _mid(vs)
    formula = [r for r in _group(vs, "formula") if r["videos"] >= 2]
    frame = [r for r in _group(vs, "frame") if r["videos"] >= 2]
    advice = ""
    if len(vs) >= 5:
        advice = f"{len(vs)} videos, {avg}% viewed (median; a Short watched twice counts over 100%)."
        if len(formula) >= 2 and formula[0]["retention"] - formula[-1]["retention"] >= 5:
            advice += (f" Hooks of type '{formula[0]['name']}' hold best ({formula[0]['retention']}%),"
                       f" '{formula[-1]['name']}' worst ({formula[-1]['retention']}%).")
        ln = _length(vs)
        if len(ln) >= 2:
            b = max(ln, key=lambda r: r["retention"])
            advice += f" Length {b['name']} holds best ({b['retention']}%)."
    return {"videos": len(vs), "average": avg, "best": vs[:3], "worst": vs[-3:][::-1] if len(vs) >= 6 else [],
            "formula": formula, "frame": frame, "length": _length(vs), "advice": advice, "all": vs,
            "learn": pipeline.flag("LEARN_FROM_STATS", "true")}


def prompt_hint():
    """For the script prompt: real hooks of this channel ranked by how long people watched."""
    if not pipeline.flag("LEARN_FROM_STATS", "true"):
        return []
    vs = videos()
    if len(vs) < 5:
        return []
    avg = _mid(vs)
    best = [v for v in vs[:3] if v["retention"] > avg]
    worst = [v for v in vs[-3:] if v["retention"] < avg][::-1]
    lines = [f"WHAT THIS CHANNEL'S VIEWERS WATCH (YouTube, {len(vs)} past Shorts, median {avg:.0f}% viewed; over 100% = watched more than once).",
             "Learn the shape and the kind of tension - never reuse their words or figures:"]
    lines += [f"- held best ({v['retention']:.0f}% viewed, {v['seconds'] or '?'} s, formula {v['formula']}): {v['hook']}" for v in best]
    lines += [f"- lost viewers ({v['retention']:.0f}% viewed, {v['seconds'] or '?'} s, formula {v['formula']}): {v['hook']}" for v in worst]
    formula = [r for r in _group(vs, "formula") if r["videos"] >= 2]
    if len(formula) >= 2 and formula[0]["retention"] - formula[-1]["retention"] >= 5:
        lines.append(f"Hook formulas that hold best here: {', '.join(r['name'] for r in formula[:2])}; build at least"
                     f" two of the 5 hooks on them. Weakest here: {formula[-1]['name']}.")
    return lines + [""]
