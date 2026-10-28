"""Topic queue: the businesses still to film, in the order the creator wants them.

data/topics.json holds [{id, topic, state, added, job_id, used_at}], state = todo | used | skipped.
A run without a topic (dashboard with an empty topic field, or n8n's daily run) takes the first
'todo' item; it becomes 'used' only when its video is actually finished, so a failed run keeps it.
"""
import json
import re
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

import pipeline
from pipeline import DATA, PipelineError

TOPICS = DATA / "topics.json"
STARTER = Path(__file__).parent / "business_ideas.txt"   # 365 boring, startable businesses


def _load():
    return pipeline._read_json(TOPICS, []) or []


def _save(items):
    pipeline._write_json(TOPICS, items)
    return items


def _key(text):
    return " ".join(text.lower().split())


def add(lines, top=False):
    """Add one topic per line; duplicates of queued or already filmed topics are skipped."""
    queue = _load()
    seen = {_key(x["topic"]) for x in queue} | {_key(h.get("topic") or "") for h in pipeline._history()}
    new = []
    for line in lines:
        # drop list markers like "12.", "3)", "- ", "• " in front; keep the name itself intact
        t = re.sub(r"^(?:[-•*]+\s*|\d+\s*[.)]\s*)+", "", " ".join(str(line).split())).strip()[:200]
        if t and _key(t) not in seen:
            seen.add(_key(t))
            new.append({"id": uuid.uuid4().hex[:8], "topic": t, "state": "todo", "added": pipeline._now(),
                        "job_id": None, "used_at": None})
    _save(new + queue if top else queue + new)
    return {"added": len(new), "skipped": len([x for x in lines if str(x).strip()]) - len(new)}


class TopicVerdict(BaseModel):
    n: int = Field(description="the topic's number in the list")
    verdict: str = Field(description="ok, fix (spelling or wording corrected) or doubt (not a real, legal, startable small business)")
    topic: str = Field(description="the topic as it should read: same business, correct spelling, 2-6 words, sentence case")
    note: str = Field(description="for fix or doubt: what was wrong, a few words; empty for ok")


class TopicReview(BaseModel):
    topics: list[TopicVerdict]


def _review(names):
    """One model call over up to ~80 topic names -> {index: TopicVerdict}. Raises when no model answers."""
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(names, 1))
    prompt = "\n".join([
        "You check the topic list of the YouTube series "
        f"'{pipeline.env('SERIES_NAME') or '365 Businesses That Make Money'}' "
        f"(audience: {pipeline.env('CHANNEL_AUDIENCE', 'American viewers')}). Each topic is one small business.",
        "For every topic decide:",
        "- ok: a real business people actually run in that market, legal, honestly able to make money,",
        "  spelled correctly. Needing a license, permits, insurance, a lease or a big budget is FINE -",
        "  the video covers the startup cost and the catch;",
        "- fix: the same business but the name has a typo, wrong word, broken brackets, a brand name or more",
        "  than 6 words - give the corrected name;",
        "- doubt: ONLY when it is not a real business, illegal, a scam or get-rich-quick scheme, a",
        "  franchise/brand, too vague to film (e.g. 'Online business'), or a duplicate of an earlier topic",
        "  in this list.",
        "Never invent a different business. Answer for every number.",
        "",
        listing,
    ])
    client, errors = pipeline._client(), []
    for model in pipeline.text_models():
        try:
            text = pipeline._hard_timeout(lambda: pipeline._output(client.interactions.create(
                model=model, input=prompt, timeout=90,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": TopicReview.model_json_schema()}), "text"), 100)
            return {v.n: v for v in TopicReview.model_validate_json(text).topics}
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
    raise PipelineError("no model answered - " + " | ".join(errors))


def check(batch=80):
    """Check the next batch of unchecked 'to film' topics. Typos are corrected in place (the old name
    is kept in the note); doubtful ones are only marked - the creator decides. Call again until
    'left' is 0."""
    queue = _load()
    todo = [x for x in queue if x["state"] == "todo" and not x.get("check")][:batch]
    if not todo:
        return {"checked": 0, "fixed": 0, "doubt": 0, "left": 0}
    verdicts = _review([x["topic"] for x in todo])
    fixed = doubt = 0
    for i, item in enumerate(todo, 1):
        v = verdicts.get(i)
        if not v:
            continue
        verdict = v.verdict if v.verdict in ("ok", "fix", "doubt") else "ok"
        new = " ".join(v.topic.split())[:200]
        note = v.note.strip()[:200]
        if verdict == "fix" and new and _key(new) != _key(item["topic"]):
            note = f"was “{item['topic']}”" + (f" — {note}" if note else "")
            item["topic"] = new
            fixed += 1
        elif verdict == "fix":
            verdict = "ok"
        doubt += verdict == "doubt"
        item["check"] = {"verdict": verdict, "note": note, "at": pipeline._now()}
    _save(queue)
    left = sum(1 for x in queue if x["state"] == "todo" and not x.get("check"))
    return {"checked": len(todo), "fixed": fixed, "doubt": doubt, "left": left}


def load_starter():
    return add(STARTER.read_text(encoding="utf-8").splitlines())


def suggest(count=30):
    """Ask Gemini for fresh business ideas that are neither filmed nor queued yet."""
    count = max(5, min(100, int(count)))
    taken = [x["topic"] for x in _load()] + [h.get("topic") or "" for h in pipeline._history()]
    prompt = (f"List {count} real, boring, startable small businesses for the YouTube series "
              f"'{pipeline.env('SERIES_NAME') or '365 Businesses That Make Money'}' "
              f"(audience: {pipeline.env('CHANNEL_AUDIENCE', 'American viewers')}). Each one a short name of what the "
              "business does, 2-6 words, e.g. 'Mobile car detailing', 'Bounce house rentals'. No franchises, no "
              "brand names, nothing illegal or get-rich-quick. Mix startup costs from under $1K to $50K. "
              "Do not repeat any of these: " + "; ".join(taken[-300:]) +
              '\nAnswer with JSON only: {"topics": ["...", "..."]}')
    client, errors = pipeline._client(), []
    for model in pipeline.text_models():
        try:
            text = pipeline._hard_timeout(lambda: pipeline._output(client.interactions.create(
                model=model, input=prompt, timeout=60,
                response_format={"type": "text", "mime_type": "application/json"}), "text"), 70)
            ideas = [str(t) for t in json.loads(text).get("topics") or [] if str(t).strip()]
            break
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
    else:
        raise PipelineError("no model answered - " + " | ".join(errors))
    dropped = []
    if pipeline.flag("TOPIC_CHECK", "true"):
        try:
            verdicts = _review(ideas)
        except PipelineError:
            verdicts = {}   # the ideas still come in, just unchecked
        kept = []
        for i, idea in enumerate(ideas, 1):
            v = verdicts.get(i)
            if v and v.verdict == "doubt":
                dropped.append(f"{idea} ({v.note})" if v.note else idea)
            else:
                kept.append(v.topic if v and v.verdict == "fix" and v.topic.strip() else idea)
        checked = bool(verdicts)
    else:
        kept, checked = ideas, False
    res = add(kept)
    if checked:
        # mark the new ones as already checked so 'Check topics' skips them
        new = {_key(t) for t in kept}
        queue = _load()
        for x in queue:
            if x["state"] == "todo" and not x.get("check") and _key(x["topic"]) in new:
                x["check"] = {"verdict": "ok", "note": "", "at": pipeline._now()}
        _save(queue)
    return {**res, "dropped": dropped}


def update(item_id, topic=None, state=None):
    queue = _load()
    item = next((x for x in queue if x["id"] == item_id), None)
    if not item:
        raise PipelineError("no such topic")
    if topic is not None and topic.strip():
        item["topic"] = " ".join(topic.split())[:200]
        item.pop("check", None)
    if state in ("todo", "used", "skipped"):
        item["state"] = state
        if state == "todo":
            item["job_id"] = item["used_at"] = None
    _save(queue)
    return item


def reorder(ids):
    """New order from the drag and drop list; ids not mentioned keep their place at the end."""
    queue = _load()
    by_id = {x["id"]: x for x in queue}
    ordered = [by_id.pop(i) for i in ids if i in by_id]
    return _save(ordered + [x for x in queue if x["id"] in by_id])


def delete(item_id):
    queue = _load()
    left = [x for x in queue if x["id"] != item_id]
    if len(left) == len(queue):
        raise PipelineError("no such topic")
    _save(left)


def clear(state):
    """Remove every item in one state (e.g. all filmed ones)."""
    _save([x for x in _load() if x["state"] != state])


def next_item():
    return next((x for x in _load() if x["state"] == "todo"), None)


def mark_used(item_id, job_id):
    queue = _load()
    for x in queue:
        if x["id"] == item_id:
            x.update(state="used", job_id=job_id, used_at=pipeline._now())
    _save(queue)


def summary():
    queue = _load()
    return {"items": queue, "todo": sum(x["state"] == "todo" for x in queue),
            "used": sum(x["state"] == "used" for x in queue), "enabled": pipeline.flag("TOPIC_QUEUE", "true"),
            "unchecked": sum(x["state"] == "todo" and not x.get("check") for x in queue),
            "doubt": sum(x["state"] == "todo" and (x.get("check") or {}).get("verdict") == "doubt" for x in queue)}
