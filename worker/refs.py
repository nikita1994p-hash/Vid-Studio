"""References: Shorts in the channel's niche that took off, found on YouTube, picked by the creator.

Every REF_EVERY_DAYS days (and on 'Search now') the Studio asks the YouTube Data API for the most viewed
Shorts of the last REF_DAYS days for each search phrase (REF_QUERIES), keeps the ones that beat their
own channel (views far above its subscriber count = the video itself took off, not a big channel), and
has the script model rate how well each fits this channel and what topic it would become here.

The creator opens each one (it plays in the Studio), then presses Take - it becomes the first topic in
the Topics queue, and its angle goes into that video's script prompt - or Skip. Taken references show
how our video did next to the original.

data/channels/<id>/refs.json = {"last_search": iso, "last_error": str, "items": [ref, ...]}
ref = {id, url, title, channel, channel_id, channel_subs, published, views, likes, comments, seconds,
       thumb, outlier, per_day, query, found_at, fit, why, topic, angle, state (new|taken|skipped),
       topic_id, taken_at}
TikTok and Instagram offer no API to search other people's videos, so the search is YouTube only.
"""
import datetime as dt
import json
import re

from pydantic import BaseModel, Field

import pipeline
import social
import space
from pipeline import PipelineError

REFS = space.Here("refs.json")
API = "https://www.googleapis.com/youtube/v3"
DEFAULT_QUERIES = {
    "business": "business ideas,ai side hustle,tiktok shop side hustle,reselling side hustle,how much I made side hustle,viral side hustle,side hustle ideas",
    "health": "health tips,exercise tips,healthy habits",
}


def _load():
    data = pipeline._read_json(REFS, {}) or {}
    data.setdefault("items", [])
    return data


def _save(data):
    pipeline._write_json(REFS, data)
    return data


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _parse(ts):
    try:
        return dt.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(iso):
    """ISO 8601 duration of the API ('PT1M5S') -> 65."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def queries():
    own = [q.strip() for q in pipeline.env("REF_QUERIES").split(",") if q.strip()]
    if own:
        return own[:8]
    kind = pipeline.env("NICHE_KIND", "business")
    return DEFAULT_QUERIES.get(kind, DEFAULT_QUERIES["business"]).split(",")


def _bearer():
    return {"Authorization": f"Bearer {social._token('youtube')['access_token']}"}


def _own_channel(bearer):
    try:
        items = social._call("GET", f"{API}/channels", params={"part": "id", "mine": "true"},
                             headers=bearer).get("items") or []
        return items[0]["id"] if items else None
    except Exception:
        return None


def _details(bearer, ids):
    """videos.list for up to 50 ids at a time (1 quota unit per call)."""
    out = {}
    for i in range(0, len(ids), 50):
        items = social._call("GET", f"{API}/videos", headers=bearer, params={
            "part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50])}).get("items") or []
        for v in items:
            out[v["id"]] = v
    return out


def _subs(bearer, channel_ids, countries=None):
    """{channel id: subscribers or None}; countries (a dict) also gets {channel id: 'IN', 'US'...}."""
    out = {}
    ids = sorted({c for c in channel_ids if c})
    for i in range(0, len(ids), 50):
        items = social._call("GET", f"{API}/channels", headers=bearer, params={
            "part": "statistics,snippet", "id": ",".join(ids[i:i + 50])}).get("items") or []
        for c in items:
            st = c.get("statistics") or {}
            out[c["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0)
            if countries is not None:
                countries[c["id"]] = ((c.get("snippet") or {}).get("country") or "").upper()
    return out


# Indian Shorts fill the results for money phrases even with regionCode=US, and their rupee economics
# never fit an American audience: they are dropped outright.
_INDIC = re.compile(r"[ऀ-෿]")
_INDIA_STRONG = re.compile(r"₹|\brs\.?\s?\d|\brupees?\b|\blakhs?\b|\blakhon\b|\bcrores?\b|\bindia\b|\bindian\b|"
                           r"\bkamai\b|\bkamao\b|\bpaise\b|\bpaisa\b|\bdesi\b|\bbharat\b", re.I)
_HINGLISH = {"hai", "ka", "ke", "ki", "se", "kaise", "kya", "wala", "wali", "wale", "banane", "karke", "karo",
             "bhai", "mein", "aur", "nahi", "sirf", "bas", "ghar", "kaam", "apna", "apne", "yeh", "ye", "bhi", "ho"}
_INDIAN_LANGS = ("hi", "en-in", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "ur", "or", "as")


def _indian(v, country=""):
    """True for a Short from India: the channel's country, the video's language, Indic script, rupee
    figures or Hinglish words in the title or description."""
    sn = v.get("snippet") or {}
    if country == "IN":
        return True
    langs = [(sn.get(k) or "").lower() for k in ("defaultAudioLanguage", "defaultLanguage")]
    if any(lang == code or lang.startswith(code + "-") for lang in langs for code in _INDIAN_LANGS):
        return True
    text = f"{sn.get('title', '')} {sn.get('description', '')} {sn.get('channelTitle', '')}"
    if _INDIC.search(text) or _INDIA_STRONG.search(text):
        return True
    words = re.findall(r"[a-z]+", (sn.get("title") or "").lower())
    return sum(w in _HINGLISH for w in words) >= 2


def _item(v, subs, query):
    sn, st = v.get("snippet") or {}, v.get("statistics") or {}
    views = int(st.get("viewCount", 0) or 0)
    published = sn.get("publishedAt")
    age = max(1.0, (_now() - (_parse(published) or _now())).total_seconds() / 86400)
    ch_subs = subs.get(sn.get("channelId"))
    thumbs = sn.get("thumbnails") or {}
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url") \
        or f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"
    return {"id": v["id"], "url": f"https://www.youtube.com/shorts/{v['id']}",
            "title": sn.get("title", ""), "description": (sn.get("description") or "")[:300],
            "channel": sn.get("channelTitle", ""), "channel_id": sn.get("channelId"),
            "channel_subs": ch_subs, "published": published, "views": views,
            "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
            "seconds": _seconds((v.get("contentDetails") or {}).get("duration")),
            "thumb": thumb, "outlier": round(views / max(ch_subs or 0, 100), 1) if ch_subs is not None else None,
            "per_day": round(views / age), "query": query, "found_at": pipeline._now(),
            "fit": None, "why": "", "topic": "", "angle": "", "state": "new", "topic_id": None, "taken_at": None}


class RefVerdict(BaseModel):
    n: int = Field(description="the video's number in the list")
    fit: int = Field(description="0-100: how well this video's idea can become an episode of THIS channel")
    why: str = Field(description="why it took off: the hook, the angle or the format, max 20 words")
    topic: str = Field(description="the topic it becomes on this channel, 2-6 words; empty if none fits")
    angle: str = Field(description="how to tell that topic on this channel using what worked, max 25 words")
    market: str = Field(description="who the video is made for: 'india' (Indian names, Hindi or Hinglish, rupees, "
                                    "Indian places or channel), 'non-english' (another language), or 'english'")


class RefReview(BaseModel):
    videos: list[RefVerdict]


def _rate(items):
    """One model call over the new references -> fit, why, topic, angle written into each item.
    A failure leaves them unrated (still shown)."""
    if not items:
        return
    N = pipeline.niche()
    listing = "\n".join(
        f"{i}. <video>title: {x['title']} | channel: {x['channel']} | views: {x['views']} | "
        f"{x['seconds']} s | description: {x['description'][:200]}</video>" for i, x in enumerate(items, 1))
    prompt = "\n".join([
        f"You pick reference videos for the YouTube Shorts channel about: {pipeline.env('CHANNEL_NICHE')}.",
        *([f"Series: '{pipeline.env('SERIES_NAME')}'. {pipeline.env('SERIES_RULES')}"] if pipeline.env("SERIES_NAME") else []),
        f"Audience: {pipeline.env('CHANNEL_AUDIENCE', 'American viewers')}. Every episode is {N['topic_item']}.",
        "These Shorts took off recently. For each one decide how well its idea can become an episode of THIS",
        "channel (fit 0-100: 80+ = a clear episode, 40-79 = usable with a twist, under 40 = off-topic, a meme,",
        "a celebrity, politics, a scam, a get-rich-quick promise or not a real topic for this channel).",
        "Investing, trading, options, crypto, bank or card sign-up bonuses, loans and any other financial product",
        "are never a topic here (that is financial advice): fit 0-20 and an empty topic.",
        "A video in another language or about another country's market (rupees, lakh...) can still lend an idea",
        "that works for this audience, but its fit is at most 60; a fit above 60 needs an idea this audience",
        "can start at home. This channel only covers what is going viral or trending right now (new tech and AI,",
        "reselling trends, creator-economy and online work, seasonal booms): a classic local service with nothing",
        "new about it (laundromat, vending machines, car wash, cleaning...) gets a fit of at most 40.",
        "For each one also give why it took off (the hook, the angle or the format), the",
        "topic it becomes here and the angle that keeps what worked. Never copy its title or words.",
        "Answer for every number.",
        "",
        listing,
    ])
    client = pipeline._client()
    for model in pipeline.text_models():
        try:
            text = pipeline._hard_timeout(lambda: pipeline._output(client.interactions.create(
                model=model, input=prompt, timeout=90, system_instruction=pipeline.GUARD,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": RefReview.model_json_schema()}), "text"), 100)
            verdicts = {v.n: v for v in RefReview.model_validate_json(text).videos}
            break
        except Exception as e:
            print(f"[warn] references: {model} could not rate: {str(e)[:120]}", flush=True)
    else:
        return
    for i, x in enumerate(items, 1):
        v = verdicts.get(i)
        if v:
            x["market"] = (v.market or "").strip().lower()
            x.update(fit=max(0, min(100, v.fit)), why=pipeline.strip_contacts(v.why)[:200],
                     topic=" ".join(v.topic.split())[:80], angle=pipeline.strip_contacts(v.angle)[:240])


def _short(n):
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def search(force=False):
    """Find new references. Without force only when the last search is REF_EVERY_DAYS old.
    Returns {"added": n, "fit": n, "searched": [queries]} or {"skipped": reason}."""
    data = _load()
    every = max(1, int(pipeline._num("REF_EVERY_DAYS", 2)))
    last = _parse(data.get("last_search"))
    if not force and last and _now() - last < dt.timedelta(days=every):
        return {"skipped": "not due yet"}
    bearer = _bearer()
    own = _own_channel(bearer)
    days = max(3, min(90, int(pipeline._num("REF_DAYS", 30))))
    after = (_now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    known = {x["id"] for x in data["items"]}
    found, done = {}, []
    try:
        for q in queries():
            res = social._call("GET", f"{API}/search", headers=bearer, params={   # 100 quota units each
                "part": "id", "q": q, "type": "video", "videoDuration": "short", "order": "viewCount",
                "publishedAfter": after, "maxResults": 50, "regionCode": pipeline.env("YOUTUBE_REGION", "US"),
                "relevanceLanguage": pipeline.env("CHANNEL_LANGUAGE", "en"), "safeSearch": "strict"})
            for r in res.get("items") or []:
                vid = (r.get("id") or {}).get("videoId")
                if vid and vid not in known:
                    found.setdefault(vid, q)
            done.append(q)
        details = _details(bearer, list(found))
        countries = {}
        subs = _subs(bearer, [(v.get("snippet") or {}).get("channelId") for v in details.values()], countries)
    except Exception as e:
        data["last_error"] = str(e)[:300]
        data["last_search"] = pipeline._now()   # a quota error must not retry every 15 minutes
        _save(data)
        raise
    min_views = int(pipeline._num("REF_MIN_VIEWS", 20000))
    new, indian = [], 0
    for vid, v in details.items():
        x = _item(v, subs, found[vid])
        if x["channel_id"] == own or x["seconds"] > 180 or x["views"] < min_views:
            continue   # our own videos, long videos and small ones
        if _indian(v, countries.get(x["channel_id"], "")):
            indian += 1
            continue
        new.append(x)
    # the video beat its channel first (a big channel's average Short is no lesson), then raw views
    new.sort(key=lambda x: (x["outlier"] or 0, x["views"]), reverse=True)
    keep = max(4, min(30, int(pipeline._num("REF_KEEP", 12))))
    new = new[:keep * 2]   # rated, then Indian and non-English ones the API could not tell are dropped
    _rate(new)
    foreign = [x for x in new if x.get("market") in ("india", "non-english")]
    indian += len(foreign)
    new = [x for x in new if x not in foreign][:keep]
    data["items"] = new + data["items"]
    data["last_search"], data["last_error"] = pipeline._now(), ""
    _save(data)
    good = [x for x in new if (x["fit"] or 0) >= 60]
    if new:
        import notify
        top = sorted(good or new, key=lambda x: (x["fit"] or 0, x["outlier"] or 0), reverse=True)[:3]
        base = (pipeline.env("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
        notify.send("refs", "\n".join(
            [f"🔎 {len(new)} new references ({len(good)} fit the channel)"]
            + [f"• {x['title'][:70]} — {_short(x['views'])} views"
               + (f", ×{x['outlier']} its subscribers" if x["outlier"] else "") for x in top]
            + [f"{base}/#/refs"]))
    return {"added": len(new), "fit": len(good), "dropped_indian": indian, "searched": done}


def purge_indian():
    """Remove Indian Shorts already in References (new or skipped; a taken one stays with its topic)."""
    data = _load()
    ids = [x["id"] for x in data["items"] if x["state"] != "taken"]
    if not ids:
        return {"removed": 0}
    bearer = _bearer()
    details = _details(bearer, ids)
    countries = {}
    _subs(bearer, [(v.get("snippet") or {}).get("channelId") for v in details.values()], countries)
    drop = {i for i, v in details.items() if _indian(v, countries.get((v.get("snippet") or {}).get("channelId"), ""))}
    rest = [x for x in data["items"] if x["state"] != "taken" and x["id"] not in drop]
    _rate(rest)   # the model also tells Indian and non-English Shorts by their names, words and places
    drop |= {x["id"] for x in rest if x.get("market") in ("india", "non-english")}
    data["items"] = [x for x in data["items"] if x["id"] not in drop]
    _save(data)
    return {"removed": len(drop), "left": len(data["items"])}


def add_url(url):
    """A YouTube video the creator found themselves (Shorts, watch or youtu.be link)."""
    m = re.search(r"(?:shorts/|v=|youtu\.be/|embed/)([\w-]{11})", url or "")
    if not m:
        raise PipelineError("paste a YouTube link (youtube.com/shorts/…, watch?v=… or youtu.be/…) - "
                            "TikTok and Instagram have no API to read other people's videos")
    data = _load()
    if any(x["id"] == m.group(1) for x in data["items"]):
        raise PipelineError("this video is already in References")
    bearer = _bearer()
    details = _details(bearer, [m.group(1)])
    if not details:
        raise PipelineError("YouTube does not know this video (private or deleted?)")
    v = details[m.group(1)]
    x = _item(v, _subs(bearer, [(v.get("snippet") or {}).get("channelId")]), "added by you")
    _rate([x])
    data["items"].insert(0, x)
    _save(data)
    return x


def _find(data, ref_id):
    x = next((i for i in data["items"] if i["id"] == ref_id), None)
    if not x:
        raise PipelineError("no such reference")
    return x


def take(ref_id, topic=None):
    """Put the reference's topic first in the Topics queue, with the reference attached for the script."""
    import topics
    data = _load()
    x = _find(data, ref_id)
    name = " ".join((topic or x.get("topic") or "").split())[:80]
    if not name:
        raise PipelineError("give this reference a topic first")
    pipeline.check_input(name, "the topic")
    res = topics.add([name], top=True)
    queue = topics._load()
    item = next((t for t in queue if topics._key(t["topic"]) == topics._key(name) and t["state"] == "todo"), None)
    if not item:
        raise PipelineError(f"“{name}” was already filmed - give it another topic")
    if not res["added"]:   # already waiting further down: move it to the top
        queue = [item] + [t for t in queue if t is not item]
    item["ref"] = {"id": x["id"], "url": x["url"], "title": x["title"], "views": x["views"],
                   "outlier": x["outlier"], "why": x["why"], "angle": x["angle"]}
    topics._save(queue)
    x.update(state="taken", topic=name, topic_id=item["id"], taken_at=pipeline._now())
    _save(data)
    return x


def set_state(ref_id, state):
    """Skip a reference, or bring a skipped one back. A taken one goes back by deleting its topic."""
    if state not in ("new", "skipped"):
        raise PipelineError("state must be new or skipped")
    data = _load()
    x = _find(data, ref_id)
    if x["state"] == "taken":
        raise PipelineError("this one is already a topic - remove it in Topics")
    x["state"] = state
    _save(data)
    return x


def _ours(topic_ids):
    """For taken references: our video made from it and how it did (YouTube)."""
    import earnings
    import topics
    by_topic = {t["id"]: t for t in topics._load()}
    stats = (pipeline._read_json(earnings.STATS) or {}).get("posts", {})
    out = {}
    for tid in topic_ids:
        t = by_topic.get(tid)
        if not t:
            continue
        o = {"topic_state": t["state"], "job_id": t.get("job_id")}
        if t.get("job_id"):
            d = pipeline.job_dir(t["job_id"])
            st = pipeline._read_json(d / "status.json") or {}
            res = pipeline._read_json(d / "result.json") or {}
            yt = st.get("youtube") or {}
            yid = yt.get("video_id") if yt.get("state") == "uploaded" else ((st.get("social") or {}).get("youtube") or {}).get("id")
            s = stats.get(f"youtube:{yid}", {}) if yid else {}
            o.update(title=res.get("title"), youtube_id=yid, views=s.get("views"),
                     retention=s.get("avg_view_pct"), likes=s.get("likes"))
        out[tid] = o
    return out


def summary():
    data = _load()
    items = data["items"]
    ours = _ours([x["topic_id"] for x in items if x["state"] == "taken" and x.get("topic_id")])
    for x in items:
        x["ours"] = ours.get(x.get("topic_id")) if x["state"] == "taken" else None
    every = max(1, int(pipeline._num("REF_EVERY_DAYS", 2)))
    last = _parse(data.get("last_search"))
    return {"items": items, "last_search": data.get("last_search"), "last_error": data.get("last_error") or "",
            "next_search": (last + dt.timedelta(days=every)).isoformat() if last else None,
            "every_days": every, "enabled": pipeline.flag("REF_SEARCH", "true"), "queries": queries(),
            "connected": bool((social._account("youtube") or {}).get("access_token")),
            "counts": {s: sum(x["state"] == s for x in items) for s in ("new", "taken", "skipped")}}


def due_channels():
    """Background loop: search every channel that has it on, YouTube connected and is due."""
    for cid in space.list_ids():
        try:
            with space.use(cid):
                if not pipeline.flag("REF_SEARCH", "true") or not (social._account("youtube") or {}).get("access_token"):
                    continue
                r = search()
                if "added" in r:
                    print(f"[info] references ({cid}): {json.dumps(r)}", flush=True)
        except Exception as e:
            print(f"[warn] reference search ({cid}): {str(e)[:200]}", flush=True)
