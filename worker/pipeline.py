"""The whole Short, from idea to final.mp4.

plan -> pick hook/title with the yt-skills scripts -> voice-over per scene -> images (+ optional
video hook) -> FFmpeg render. Every step writes into data/jobs/<job_id>/ and reports its progress
to status.json, which the dashboard polls.
"""
import asyncio
import base64
import contextvars
import datetime as dt
import json

import hookscore_shorts
import niches
import math
import space
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

DATA = space.ROOT                         # shared: music, sfx, previews, platform app keys
JOBS = space.Here("jobs")                 # the rest belongs to the current channel
SKILLS = Path(os.environ.get("SKILLS_MOUNT", "/skills"))
VOICE_FILE = Path(os.environ.get("VOICE_FILE", "/youtube/voice.md"))
RULES_FILE = space.Here("script_rules.md")     # editable in the dashboard (Settings) or Notepad
PARTS_FILE = space.Here("parts_queue.json")    # the remaining parts of a story split into 2-3 Shorts
HISTORY = space.Here("history.json")

DEFAULT_RULES = """\
# Script rules. VidAuto reads this file every time it writes a script.
# Edit it in the dashboard (Settings) or in Notepad: data/script_rules.md.
# Lines that start with # are notes for you; the AI never sees them.

Shape: HOOK -> SETUP -> MIDDLE -> PAYOFF -> LOOP. Every line sounds like one friend telling another
about a business over coffee: full spoken sentences that connect to the previous one, never a list of labels.

1. HOOK (first 3-5 seconds): one bold, concrete claim or question about this business, built on its real
   cost, price or profit. It promises a payoff that the video really delivers.
2. SETUP (scene 1): what the business is in plain words - who pays, and for what.
3. MIDDLE (the scenes in between): each scene answers the question the previous one raised and opens a new
   one, so the viewer has a reason to stay: what it costs to start, how the money comes in, what is left
   after costs, the catch. One idea per scene. Use spoken transitions: "Here's the thing.",
   "So where's the money?", "But there's a catch."
4. PAYOFF (last body scene): the first concrete step, told as an action the viewer can take this week
   (e.g. "This week, call three general contractors and ask who hauls their trash."). It closes the
   promise of the hook.
5. LOOP (loop_line): the final sentence leads straight back into the hook, so a replay feels like one
   continuous video (e.g. "...and it all starts with a trailer hitch, because" -> hook "If you have a
   trailer hitch and five thousand dollars...").

Never:
- end with a recap of labels ("Startup: fifteen grand. Profit: four grand. Step one: ...");
- say the episode number in the narration (it is already on screen);
- use two different numbers for the same thing (if the hook says five thousand dollars to start, the body
  says the same);
- leave a sentence unfinished, except the loop_line, which the hook completes.
- make up percentages or failure rates ("ninety percent of laundromats fail"): use only the business's
  own typical cost, price and profit.
"""

W, H, FPS = 1080, 1920, 30
SCENE_PAD = 0.25          # breath after each scene, seconds
IMAGE_PRICE = 0.0336      # gemini-3.1-flash-lite-image / nano-banana-2.1 at 1K, USD
VIDEO_PRICE_PER_S = 0.10  # gemini-omni-1.1-flash at 720p, USD
SCRIPT_PRICE = float(os.environ.get("SCRIPT_COST_USD", "0"))  # 0 on the free tier; ≈0.02 on paid

STAGES = [
    ("script", "Script & hooks"),
    ("check", "Fact check"),
    ("voice", "Voice-over"),
    ("visuals", "Images & video"),
    ("render", "Editing & subtitles"),
    ("youtube", "YouTube"),
]

_status_lock = threading.Lock()


class PipelineError(RuntimeError):
    pass


SETTINGS_FILE = space.Here("settings.json")   # what the Studio Settings page saves; wins over .env
_job_options = contextvars.ContextVar("job_options", default=None)  # one video's own choices
_settings_cache = {}   # channel folder -> (mtime, data)

# Everything the Studio can change. Keys (API tokens) are deliberately not here: they stay in .env.
# type: text | textarea | number | bool | select | color | signed (a "+8%"-style value, unit in "unit")
SETTINGS_SCHEMA = [
    # content
    {"key": "CHANNEL_LANGUAGE", "group": "Content", "label": "Text language (script, titles, captions)", "type": "select",
     "options": ["en", "es", "pt", "de", "fr", "it", "pl", "uk", "ru", "hi", "ja", "ko"], "default": "en"},
    {"key": "CHANNEL_NICHE", "group": "Content", "label": "Channel niche", "type": "textarea"},
    {"key": "NICHE_KIND", "group": "Content", "label": "Niche kind (what the checks and hook scores look for)",
     "type": "select", "options": list(niches.KINDS), "default": "business"},
    {"key": "CHANNEL_AUDIENCE", "group": "Content", "label": "Audience", "type": "textarea"},
    {"key": "SERIES_NAME", "group": "Content", "label": "Series name", "type": "text"},
    {"key": "SERIES_RULES", "group": "Content", "label": "Series rules", "type": "textarea"},
    {"key": "SERIES_LABEL", "group": "Content", "label": "Badge on the video ({n} = episode)", "type": "text"},
    {"key": "SERIES_TITLE_PREFIX", "group": "Content", "label": "Title prefix ({n} = episode)", "type": "text"},
    {"key": "BODY_SCENES", "group": "Content", "label": "Scenes after the hook", "type": "number", "min": 3, "max": 10, "default": "6"},
    {"key": "HOOK_MIN_SCORE", "group": "Content", "label": "Hook score to aim for (rewrite weaker hooks)", "type": "number", "min": 0, "max": 95, "default": "55"},
    {"key": "HOOK_SCORER", "group": "Content", "label": "Hook scorer", "type": "select", "options": ["shorts", "skill"], "default": "shorts"},
    {"key": "HOOK_ROUNDS", "group": "Content", "label": "Hook rewrite rounds (0 = off)", "type": "number", "min": 0, "max": 4, "default": "3"},
    {"key": "LOOP_ENDING", "group": "Content", "label": "Loop ending (last frame = first frame)", "type": "bool", "default": "true"},
    {"key": "MAX_PARTS", "group": "Content", "label": "Split long stories into up to N parts", "type": "number", "min": 1, "max": 3, "default": "3"},
    {"key": "DESCRIPTION_FOOTER", "group": "Content", "label": "Description footer", "type": "text"},
    {"key": "TOPIC_QUEUE", "group": "Content", "label": "Empty topic = take the next one from the Topics queue", "type": "bool", "default": "true"},
    {"key": "TOPIC_CHECK", "group": "Content", "label": "Check new topics (spelling, real, safe, legal) before they join the queue", "type": "bool", "default": "true"},
    {"key": "FACT_CHECK", "group": "Content", "label": "Fact check before the voice-over", "type": "select",
     "options": ["flag", "fix", "off"], "default": "flag"},
    # voice
    {"key": "TTS_PROVIDER", "group": "Voice", "label": "Voice engine", "type": "select", "options": ["edge", "gemini"], "default": "edge"},
    {"key": "VOICE_LOCALE", "group": "Voice", "label": "Voice language / accent", "type": "select", "options": [], "default": "en-US"},
    {"key": "VOICE_GENDER", "group": "Voice", "label": "Voice gender", "type": "select", "options": ["Any", "Male", "Female"], "default": "Any"},
    {"key": "EDGE_VOICE", "group": "Voice", "label": "Voice", "type": "select", "options": [], "default": "en-US-AndrewMultilingualNeural"},
    {"key": "EDGE_RATE", "group": "Voice", "label": "Speed", "type": "signed", "unit": "%", "min": -30, "max": 50, "default": "+8%"},
    {"key": "EDGE_PITCH", "group": "Voice", "label": "Pitch (tone)", "type": "signed", "unit": "Hz", "min": -30, "max": 30, "default": "+0Hz"},
    {"key": "EDGE_VOLUME", "group": "Voice", "label": "Voice loudness", "type": "signed", "unit": "%", "min": -50, "max": 50, "default": "+0%"},
    {"key": "TTS_VOICE", "group": "Voice", "label": "Gemini voice (paid engine only)", "type": "text", "default": "Puck"},
    {"key": "TTS_STYLE", "group": "Voice", "label": "Gemini voice style (paid engine only)", "type": "text"},
    # captions
    {"key": "CAPTIONS", "group": "Captions", "label": "Show captions", "type": "bool", "default": "true"},
    {"key": "CAPTION_FONT", "group": "Captions", "label": "Font", "type": "select",
     "options": ["DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono"], "default": "DejaVu Sans"},
    {"key": "CAPTION_SIZE", "group": "Captions", "label": "Size", "type": "number", "min": 40, "max": 130, "default": "84"},
    {"key": "CAPTION_COLOR", "group": "Captions", "label": "Text colour", "type": "color", "default": "#FFFFFF"},
    {"key": "CAPTION_HIGHLIGHT", "group": "Captions", "label": "Every other chunk colour", "type": "color", "default": "#FFF200"},
    {"key": "CAPTION_OUTLINE_COLOR", "group": "Captions", "label": "Outline colour", "type": "color", "default": "#000000"},
    {"key": "CAPTION_OUTLINE", "group": "Captions", "label": "Outline thickness", "type": "number", "min": 0, "max": 14, "default": "7"},
    {"key": "CAPTION_SHADOW", "group": "Captions", "label": "Shadow", "type": "number", "min": 0, "max": 10, "default": "3"},
    {"key": "CAPTION_BOX", "group": "Captions", "label": "Dark box behind the text", "type": "bool", "default": "false"},
    {"key": "CAPTION_POSITION", "group": "Captions", "label": "Position", "type": "select",
     "options": ["lower third", "bottom", "middle", "top"], "default": "lower third"},
    {"key": "CAPTION_WORDS", "group": "Captions", "label": "Words on screen at once", "type": "number", "min": 1, "max": 6, "default": "3"},
    {"key": "CAPTION_UPPERCASE", "group": "Captions", "label": "UPPERCASE", "type": "bool", "default": "true"},
    {"key": "CAPTION_POP", "group": "Captions", "label": "Pop-in animation", "type": "bool", "default": "true"},
    # on-screen text and pictures
    {"key": "OVERLAY_TEXT", "group": "Picture", "label": "Big key-number text on pictures", "type": "bool", "default": "true"},
    {"key": "OVERLAY_COLOR", "group": "Picture", "label": "Key-number colour", "type": "color", "default": "#FFD54A"},
    {"key": "LABEL_COLOR", "group": "Picture", "label": "Series badge colour", "type": "color", "default": "#FF0033"},
    {"key": "SHADE", "group": "Picture", "label": "Darken pictures (0-0.7)", "type": "number", "min": 0, "max": 0.7, "step": 0.05, "default": "0.35"},
    {"key": "ZOOM", "group": "Picture", "label": "Camera motion (0 = still, 0.25 = strong)", "type": "number", "min": 0, "max": 0.25, "step": 0.01, "default": "0.12"},
    {"key": "VISUAL_PROVIDER", "group": "Picture", "label": "Picture source", "type": "select",
     "options": ["auto", "flux", "openverse", "cards", "pexels", "gemini"], "default": "auto"},
    {"key": "IMAGE_STYLE", "group": "Picture", "label": "Picture style (added to every AI prompt)", "type": "text",
     "default": "photorealistic, cinematic light"},
    {"key": "PROMPT_CHECK", "group": "Picture", "label": "Check picture prompts before FLUX (removes things AI draws badly)", "type": "bool", "default": "true"},
    {"key": "FLUX_STEPS", "group": "Picture", "label": "FLUX quality steps (1-8)", "type": "number", "min": 1, "max": 8, "default": "6"},
    # sound
    {"key": "MUSIC_TRACK", "group": "Sound", "label": "Music", "type": "select", "options": [], "default": "auto"},
    {"key": "MUSIC_VOLUME", "group": "Sound", "label": "Music volume (0-0.4)", "type": "number", "min": 0, "max": 0.4, "step": 0.01, "default": "0.12"},
    {"key": "SFX_VOLUME", "group": "Sound", "label": "Whoosh on scene cuts (0 = off)", "type": "number", "min": 0, "max": 1, "step": 0.05, "default": "0.5"},
    # variants
    {"key": "VARIANT_VOICES", "group": "Variants", "label": "Voices that variants rotate through (comma separated)", "type": "text",
     "default": "en-US-BrianMultilingualNeural,en-US-AvaMultilingualNeural,en-US-ChristopherNeural,en-US-EmmaMultilingualNeural,en-US-AndrewMultilingualNeural"},
    {"key": "VARIANT_NEW_IMAGES", "group": "Variants", "label": "Variants get freshly generated pictures", "type": "bool", "default": "true"},
    {"key": "AB_DAYS", "group": "Variants", "label": "A/B test: days before a winner is picked", "type": "number", "min": 1, "max": 60, "default": "7"},
    {"key": "AB_MIN_VIEWS", "group": "Variants", "label": "A/B test: minimum YouTube views per version", "type": "number", "min": 10, "max": 100000, "default": "100"},
    {"key": "AB_LEARN", "group": "Variants", "label": "Use A/B winners when writing new hooks", "type": "bool", "default": "true"},
    # cover
    {"key": "COVERS", "group": "Cover", "label": "Make a cover for every video (YouTube, Instagram, Facebook)", "type": "bool", "default": "true"},
    {"key": "COVER_SCENE", "group": "Cover", "label": "Cover picture", "type": "select",
     "options": ["hook", "best photo", "money scene"], "default": "hook"},
    {"key": "COVER_MONEY_COLOR", "group": "Cover", "label": "Big number colour", "type": "color", "default": "#FFE200"},
    {"key": "COVER_BOX_COLOR", "group": "Cover", "label": "Box behind the business name", "type": "color", "default": "#E50914"},
    {"key": "COVER_DARKEN", "group": "Cover", "label": "Darken the picture (0-0.7)", "type": "number", "min": 0, "max": 0.7, "step": 0.05, "default": "0.3"},
    # notifications
    {"key": "NOTIFY_DONE", "group": "Notifications", "label": "Telegram: video ready", "type": "bool", "default": "true"},
    {"key": "NOTIFY_ERRORS", "group": "Notifications", "label": "Telegram: a run or a post failed", "type": "bool", "default": "true"},
    {"key": "NOTIFY_LIMITS", "group": "Notifications", "label": "Telegram: free FLUX picture limit ran out", "type": "bool", "default": "true"},
    {"key": "NOTIFY_POSTED", "group": "Notifications", "label": "Telegram: posted to a platform", "type": "bool", "default": "true"},
    {"key": "NOTIFY_MONEY", "group": "Notifications", "label": "Telegram: new revenue / monetization reached", "type": "bool", "default": "true"},
    {"key": "NOTIFY_AB", "group": "Notifications", "label": "Telegram: A/B test winner", "type": "bool", "default": "true"},
    # publishing
    {"key": "DEFAULT_PLATFORMS", "group": "Publishing", "label": "Where the daily n8n run posts (comma separated)", "type": "text", "default": "youtube"},
    {"key": "AUTO_PUBLISH", "group": "Publishing", "label": "YouTube: publish on schedule (off = private)", "type": "bool", "default": "false"},
    {"key": "PUBLISH_TIME", "group": "Publishing", "label": "Publish time", "type": "text", "default": "18:00"},
    {"key": "PUBLISH_TIMEZONE", "group": "Publishing", "label": "Publish time zone", "type": "text", "default": "America/New_York"},
    {"key": "AI_DISCLOSURE", "group": "Publishing", "label": "Mark as AI-generated", "type": "bool", "default": "true"},
    {"key": "YOUTUBE_CATEGORY_ID", "group": "Publishing", "label": "YouTube category id (27 = Education, 22 = People)", "type": "text", "default": "27"},
    # money
    {"key": "DESCRIPTION_CTA", "group": "Monetization", "label": "Line added to every description (your link: newsletter, affiliate, product)", "type": "text"},
    {"key": "MONTHLY_COSTS", "group": "Monetization", "label": "Fixed monthly costs, $ (subscriptions, tools)", "type": "number", "min": 0, "max": 10000, "step": 1, "default": "0"},
    # models
    {"key": "TEXT_MODEL", "group": "AI models", "label": "Script model", "type": "text", "default": "gemini-3.8-flash"},
    {"key": "TEXT_FALLBACK_MODELS", "group": "AI models", "label": "Fallback models (comma separated)", "type": "text"},
]
SETTING_KEYS = {s["key"] for s in SETTINGS_SCHEMA}
SETTING_DEFAULTS = {s["key"]: s["default"] for s in SETTINGS_SCHEMA if "default" in s}


def studio_settings():
    """The channel's settings.json, re-read only when the file changes."""
    try:
        path = SETTINGS_FILE.resolve()
        mtime = path.stat().st_mtime
    except (FileNotFoundError, space.SpaceError):
        return {}
    cached = _settings_cache.get(path)
    if not cached or cached[0] != mtime:
        cached = _settings_cache[path] = (mtime, _read_json(path, {}) or {})
    return cached[1]


def save_settings(changes):
    """changes: {KEY: value}; None removes the Studio value so .env applies again."""
    data = dict(studio_settings())
    for k, v in changes.items():
        if k not in SETTING_KEYS:
            raise PipelineError(f"unknown setting {k}")
        if v is None:
            data.pop(k, None)
        else:
            data[k] = str(v).strip()[:4000]
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_json(SETTINGS_FILE, data)
    return data


def env(name, default=""):
    """A setting: this video's own choices (New video dialog) -> Studio settings -> .env."""
    for src in (_job_options.get() or {}, studio_settings()):
        if src.get(name) is not None:
            return str(src[name]).strip()
    value = os.environ.get(name, "").strip()
    # a setting missing from .env falls back to the caller's default, then to the Settings default
    return value or default or SETTING_DEFAULTS.get(name, "")


def clean_options(options):
    """Keep only known settings with a value: an empty field in the dialog means 'as in Settings'."""
    return {k: str(v).strip() for k, v in (options or {}).items()
            if k in SETTING_KEYS and v is not None and str(v).strip() != ""}


def _hex_ass(color, alpha="00"):
    """#RRGGBB -> ASS &HAABBGGRR."""
    c = (color or "#FFFFFF").lstrip("#")
    c = c if re.fullmatch(r"[0-9a-fA-F]{6}", c) else "FFFFFF"
    return f"&H{alpha}{c[4:6]}{c[2:4]}{c[0:2]}".upper()


def _hex_ff(color, fallback):
    c = (color or "").lstrip("#")
    return "0x" + (c if re.fullmatch(r"[0-9a-fA-F]{6}", c) else fallback)


def _num(name, default):
    try:
        return float(env(name, str(default)))
    except ValueError:
        return float(default)


def flag(name, default="false"):
    return env(name, default).lower() in ("1", "true", "yes", "on")


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ------------------------------------------------------------------------ jobs

def new_job_id():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def job_dir(job_id):
    if not re.fullmatch(r"[\w-]+", job_id or ""):
        raise PipelineError("bad job id")
    return JOBS / job_id


class Job:
    """Progress of one video, persisted to status.json so the dashboard can follow it."""

    def __init__(self, job_id, kind="generated", mock=False, topic=None):
        self.id = job_id
        self.dir = job_dir(job_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "status.json"
        self.status = _read_json(self.path) or {
            "job_id": job_id,
            "kind": kind,
            "mock": mock,
            "topic": topic,
            "created": _now(),
            "state": "queued",
            "stage": None,
            "error": None,
            "stages": {k: {"label": label, "state": "pending", "detail": None} for k, label in STAGES},
            "youtube": {"state": "none"},
        }
        self.save()

    def save(self):
        with _status_lock:
            self.status["updated"] = _now()
            _write_json(self.path, self.status)

    def stage(self, key, state, detail=None):
        # videos made before a stage existed (e.g. the fact check) get it added on first use
        s = self.status["stages"].setdefault(key, {"label": dict(STAGES).get(key, key), "state": "pending", "detail": None})
        s["state"] = state
        if detail is not None:
            s["detail"] = detail
        if state == "running":
            s["started"] = s.get("started") or _now()
            self.status["state"] = "running"
            self.status["stage"] = key
        if state in ("done", "skipped", "error"):
            s["finished"] = _now()
        self.save()

    def fail(self, message):
        key = self.status.get("stage")
        if key and self.status["stages"][key]["state"] == "running":
            self.status["stages"][key]["state"] = "error"
        self.status["state"] = "error"
        self.status["error"] = message
        self.save()

    def finish(self):
        self.status["state"] = "done"
        self.status["stage"] = None
        self.save()


def load_job(job_id):
    d = job_dir(job_id)
    status = _read_json(d / "status.json")
    if status is None:
        raise PipelineError("no such video")
    return {
        "status": status,
        "result": _read_json(d / "result.json"),
        "plan": _read_json(d / "plan.json"),
        "scores": _read_json(d / "scores.json"),
        "scenes": _read_json(d / "scenes.json"),
        "factcheck": _read_json(d / "factcheck.json"),
        "promptcheck": _read_json(d / "promptcheck.json"),
        "hookcheck": _read_json(d / "hookcheck.json"),
        "cover": _read_json(d / "cover.json"),
        "files": sorted(p.name for p in d.iterdir() if p.is_file()),
    }


def list_jobs():
    out = []
    if not JOBS.exists():
        return out
    for d in sorted(JOBS.iterdir(), reverse=True):
        status = _read_json(d / "status.json")
        if not status:
            continue
        result = _read_json(d / "result.json") or {}
        out.append({
            "job_id": d.name,
            "kind": status.get("kind"),
            "state": status.get("state"),
            "stage": status.get("stage"),
            "mock": status.get("mock"),
            "created": status.get("created"),
            "title": result.get("title") or status.get("topic") or "(working on it)",
            "variant": result.get("variant"),
            "seconds": result.get("seconds"),
            "cost": result.get("estimated_cost_usd"),
            "thumb": (d / "thumb.jpg").exists(),
            "youtube": status.get("youtube", {}),
            "error": status.get("error"),
        })
    return out


def recover_interrupted():
    """A restart (rebuild, reboot, crash) kills the render thread; without this its video would
    show 'Working' forever. Called once when the worker starts."""
    if not JOBS.exists():
        return
    for d in JOBS.iterdir():
        status = _read_json(d / "status.json")
        if status and status.get("state") in ("queued", "running"):
            Job(d.name).fail("interrupted: the worker restarted while this video was being made. "
                             "Delete it and create a new one.")


def delete_job(job_id):
    d = job_dir(job_id)
    if not d.is_dir():
        raise PipelineError("no such video")
    shutil.rmtree(d)


def update_meta(job_id, title=None, description=None, tags=None):
    path = job_dir(job_id) / "result.json"
    result = _read_json(path)
    if result is None:
        raise PipelineError("this video has no metadata yet")
    if title is not None:
        result["title"] = title.strip()[:100]
    if description is not None:
        result["description"] = description.strip()[:4900]
    if tags is not None:
        result["tags"] = ",".join(t.strip().replace(",", " ") for t in tags.split(",") if t.strip())[:450]
    _write_json(path, result)
    return result


# --------------------------------------------------------------------------- plan

class Hook(BaseModel):
    formula_id: str = Field(description="id of the hook formula used, from the list given")
    text: str = Field(description="the spoken hook, one or two short sentences")
    loop_line: str = Field(description="the very last sentence of the video, written for THIS hook: it "
                                       "leads straight back into the hook's first words when the Short replays")


class Scene(BaseModel):
    narration: str = Field(description="what the voice says in this scene")
    image_prompt: str = Field(description="English prompt for a vertical illustration of this scene")
    stock_query: str = Field(description="2-4 English words to find real stock footage of this scene, e.g. 'laundromat washing machines'")
    on_screen: str = Field(description="the key fact of this scene as big on-screen text, max 5 words, e.g. '$12,000 to start'")


class Plan(BaseModel):
    topic: str
    hooks: list[Hook]
    hook_visual: str = Field(description="English prompt for the opening visual under the hook")
    hook_stock_query: str = Field(description="2-4 English words for stock footage under the hook")
    hook_on_screen: str = Field(description="big on-screen text for the hook, max 5 words")
    scenes: list[Scene] = Field(description="the body that follows the hook")
    parts: list[str] = Field(description="empty list when the story fits one Short; otherwise one-sentence "
                                         "outlines of all 2-3 parts, and this plan is part 1")
    titles: list[str]
    description: str
    tags: list[str]
    cover_lines: list[str] = Field(default_factory=list, description=(
        "2-3 lines for the video's cover picture, max 14 characters each, no emoji: line 1 is the key "
        "number (e.g. '$4K/MONTH', '10 MIN/DAY'), the rest name the topic in 1-3 words (e.g. 'MOBILE', 'DETAILING')"))


class FactIssue(BaseModel):
    scene: int = Field(description="0 = one of the hooks, 1..n = body scene number")
    hook: int = Field(description="when scene is 0: which hook, 1-5; otherwise 0")
    claim: str = Field(description="the doubtful words, quoted from the script")
    problem: str = Field(description="what is wrong or unlikely, with the realistic range, in one sentence")
    severity: str = Field(description="high (clearly wrong or contradicts another line), medium (unlikely), low (worth a look)")
    fixed_narration: str = Field(description="the whole line rewritten with a realistic figure, same style and length; empty for a hook")
    fixed_on_screen: str = Field(description="corrected on-screen text for that scene if it carries the figure, else empty")


class HookSet(BaseModel):
    hooks: list[Hook]


class HookVerdict(BaseModel):
    n: int = Field(description="the hook's number in the list")
    ok: bool = Field(description="true only if it passes every test")
    reason: str = Field(description="for a failed hook: which test failed, a few words; else empty")


class HookReview(BaseModel):
    verdicts: list[HookVerdict]


class PromptFix(BaseModel):
    scene: int = Field(description="0 = the hook visual, 1..n = body scene number")
    problem: str = Field(description="what the image model would likely get wrong, a few words")
    prompt: str = Field(description="the whole prompt rewritten so it draws cleanly, same subject and mood")


class PromptCheck(BaseModel):
    fixes: list[PromptFix]


class FactCheck(BaseModel):
    verdict: str = Field(description="ok or doubtful")
    summary: str = Field(description="one sentence for the creator")
    issues: list[FactIssue]


def _formulas():
    path = SKILLS / "yt-script" / "hooks.json"
    if not path.exists():
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("rules", []), data.get("hooks", [])


def _voice_profile():
    """The channel's own voice.md; the shared one from the yt skills only for the first channel,
    whose voice it was written for."""
    own = space.folder() / "voice.md"
    first = space.list_ids()[:1] == [space.current()]
    path = own if own.exists() else VOICE_FILE if first else None
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    # The shipped template is instructions, not a voice. Using it would make every script
    # sound like a README.
    return "" if "Copy this to" in text else text[:6000]


def _history():
    path = HISTORY.resolve()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def episode_number():
    """Position in the numbered series: episodes made so far + 1. The parts of one split story share
    their episode number; videos made before parts existed count one each."""
    n = 0
    for h in _history():
        n = max(n, h.get("episode") or n + 1)
    return n + 1


def script_rules(raw=False):
    """The structure rules from data/script_rules.md (written with the defaults on first use).
    raw=True returns the file as the user sees it; otherwise the # notes are dropped."""
    if not RULES_FILE.exists():
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")
    text = RULES_FILE.read_text(encoding="utf-8")
    if raw:
        return text
    return "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#")).strip()


def save_script_rules(text):
    text = (text or "").replace("\r\n", "\n").strip()
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text((text or DEFAULT_RULES.strip())[:12000] + "\n", encoding="utf-8")
    return script_rules(raw=True)


def max_parts():
    return max(1, min(3, int(env("MAX_PARTS", "3") or 1)))


def parts_queue():
    """The next part of a split story, or None: {episode, topic, outline, total, next, said}."""
    q = _read_json(PARTS_FILE)
    return q if q and q.get("next", 99) <= q.get("total", 0) else None


def close_sentence(text):
    """Drop a dangling 'because', 'and', 'so'... from the end and finish with a full stop."""
    t = re.sub(r"[\s,;:—-]*\b(because|and|so|but|which|that|since|cause)\s*[,.…]*$", "", text.strip(), flags=re.I)
    t = t.rstrip(" ,;:—-")
    return t if t.endswith((".", "!", "?")) else t + "."


def _prompt(topic, history, episode, part=None, script=None):
    rules, formulas = _formulas()
    lang = env("CHANNEL_LANGUAGE", "en")
    n_body = int(env("BODY_SCENES", "6"))
    series = env("SERIES_NAME")
    N = niche()
    lines = [
        f"You write a YouTube Short for a channel about: {env('CHANNEL_NICHE', 'surprising science facts')}.",
        *([f"This is episode #{episode} of the series '{series}'. {env('SERIES_RULES')}"] if series else []),
        f"Audience: {env('CHANNEL_AUDIENCE', 'American viewers in the United States')}. Use their spelling,"
        " units (miles, feet, °F, dollars) and cultural references.",
        f"All viewer-facing text (hooks, narration, titles, description, tags) must be in language '{lang}'.",
        "image_prompt and hook_visual are always in English.",
        "Narration is read aloud by a text-to-speech voice: write numbers the way they should be"
        " spoken, avoid abbreviations, emoji and symbols in narration.",
        f"Topic: {topic}" if topic else
        "Pick ONE fresh, specific topic that a viewer of this niche would stop scrolling for.",
        "",
        "SCRIPT RULES - the structure every script must follow:",
        script_rules(),
        "",
        "HOOK - write exactly 5 different hooks, each built on a different formula below. The hook is the",
        "first 3-5 seconds: it confirms the promise, opens a question the viewer cannot close, and",
        "proves the payoff exists. Max 18 words. Never open with who we are or 'in this video'.",
        "Each hook has its own loop_line (max 14 words): the last sentence of the whole video. Read",
        "loop_line and then the hook aloud: together they must sound like one natural sentence or thought,",
        "so the Short loops seamlessly. The loop_line never repeats the hook and never says 'watch again'.",
        "Each hook is scored 0-100 on five things and the weakest one drags the score down, so hit ALL",
        "five: (1) SPECIFIC - a concrete number, price or name from the body; (2) YOU - 'you' or 'your' in",
        "the first six words; (3) STAKES - what it costs, what you lose or waste, what to stop or never do;",
        "(4) CURIOSITY - an open gap (why, how, what, but, until, nobody) that the hook does NOT answer",
        "itself, so never 'because', 'so that', 'which means'; (5) 9-18 words. It must still sound like a",
        f"person talking, stay on this exact {N['subject']} and use only figures the body states.",
        "Hook rules: " + " | ".join(rules),
        "Formulas (id - name - shape - example):",
    ]
    for f in formulas:
        lines.append(f"- {f['id']} - {f['name']} - {f['shape']} - e.g. \"{f['example']}\"")
    lines += [
        "",
        f"BODY - exactly {n_body} scenes that follow ANY of the hooks naturally (the numbers in every hook",
        "must match the body). 1-2 complete spoken sentences per scene, 90-120 words for the whole body.",
        "Concrete facts, numbers, names. Every scene earns the next one; the last body scene is the PAYOFF",
        "from the script rules and ends with a full stop - the loop_line comes after it and is the ONLY",
        "line that may end open. No 'subscribe' begging.",
        "image_prompt: a vivid vertical 9:16 illustration, consistent visual style across scenes,",
        "NO text, NO letters, NO captions, NO watermarks, NO real people's faces or logos.",
        "Write prompts an image model draws cleanly: ONE clear subject, a plain setting, a named camera",
        "angle and light. Avoid what it draws badly: close-up hands or fingers, people holding small tools,",
        "crowds, several people touching or interacting, readable screens, signs, price tags, receipts,",
        "banknotes, mirrors, keyboards and intricate machinery up close. Show a person from behind, at a",
        "distance or as a silhouette; prefer the place, the equipment or the finished result.",
        "stock_query: 2-3 plain English nouns for a PHOTO of what is literally visible (e.g. 'laundromat',",
        "'vending machine', 'pressure washer driveway'), never abstract ideas, people's names or brands. on_screen: the one number or fact the viewer must",
        f"remember from that scene, max 5 words. on_screen ALWAYS {N['on_screen_rule']}",
        f"(e.g. {N['on_screen']}) - never spelled out, unlike the narration.",
        "",
        "TITLES - 5 options, max 60 characters, subject first, a concrete number or name if possible,",
        "must not repeat the hook word for word.",
        "DESCRIPTION - 2-3 sentences that answer the query a real person would type, then 3 hashtags",
        "including #shorts. TAGS - 8-12 short search phrases, no '#', no commas inside a tag.",
        "COVER_LINES - the cover picture viewers see before they tap: 2-3 lines, max 14 characters each,",
        f"line 1 {N['cover']}",
        "in 1-3 plain words. The figure must be one the script really says.",
        "",
    ]
    try:
        import abtest
        hint = abtest.prompt_hint()
    except Exception as e:  # missing stats must never stop a video
        print(f"[warn] no A/B hint: {e}", flush=True)
        hint = ""
    if hint:
        lines += [hint, ""]
    if script:
        lines += [
            "SCRIPT GIVEN - the creator already wrote the narration. Keep every line word for word:",
            f"hooks[0].text is exactly: \"{script[0]}\" (the other 4 hooks are alternatives in the same spirit).",
            f"The body has exactly {len(script) - 1} scenes; scene k's narration is exactly line k:",
            *[f"{i}. {x}" for i, x in enumerate(script[1:], 1)],
            "Your job: image_prompt, stock_query and on_screen for every scene, hook_visual, hook_on_screen,",
            "the loop_lines, titles, description and tags. 'parts' is an empty list.",
        ]
    elif part:
        k, total = part["next"], part["total"]
        outline = " ".join(f"{i}) {x}" for i, x in enumerate(part["outline"], 1))
        lines += [
            f"PARTS - this Short is PART {k} of {total} of one story. Outline of all parts: {outline}",
            f"This part covers ONLY point {k}. The earlier parts already said: \"{' '.join(part['said'])[-1500:]}\"",
            "Do not repeat them. The hook must still work for a viewer who never saw the earlier parts.",
            "Leave 'parts' empty.",
            f"The last body scene ends with one natural sentence teasing part {k + 1}." if k < total else
            "This is the final part: the PAYOFF closes the whole story.",
        ]
    elif max_parts() > 1:
        lines += [
            "PARTS - most businesses fit ONE Short: then 'parts' is an empty list. Only when this business",
            "genuinely needs more than about 60 seconds of narration to be useful, split it: put 2-"
            f"{max_parts()} one-sentence outlines in 'parts' (what each part covers) and write THIS plan as",
            "part 1 only, covering outline 1; its last body scene ends with one natural sentence teasing part 2.",
        ]
    else:
        lines.append("PARTS - always an empty list.")
    voice = _voice_profile()
    if voice:
        lines += ["", "Write in this creator's voice:", voice]
    if history and not part:  # a later part is about the same topic on purpose
        recent = "; ".join(h["topic"] for h in history[-40:])
        lines += ["", f"Do NOT repeat or closely paraphrase these previous topics: {recent}"]
    return "\n".join(lines)


def _client():
    from google import genai
    key = env("GEMINI_API_KEY")
    if not key:
        raise PipelineError("GEMINI_API_KEY is empty - put it in .env and restart, or set MOCK=true")
    return genai.Client(api_key=key)


def _retry(fn, what, tries=3):
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # the SDKs raise several error types; all are retried the same way
            if attempt == tries:
                raise PipelineError(f"{what} failed after {tries} tries: {e}") from e
            time.sleep(5 * attempt)


def _output(it, kind):
    """The convenience field is None when the model returned nothing (e.g. a safety block).
    Raising here, inside the retried call, turns that into a retry instead of a crash."""
    out = getattr(it, f"output_{kind}", None)
    if kind == "text":
        if not out:
            raise PipelineError("model returned no text")
        return out
    if out is None or not getattr(out, "data", None):
        raise PipelineError(f"model returned no {kind}")
    return base64.b64decode(out.data)


def _hard_timeout(fn, seconds):
    """The SDK retries 503/429 on its own and can sit for many minutes; give each call a real
    deadline so an overloaded model fails fast and the next one in the chain gets a turn."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as Timeout
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # the call runs in the pool's thread: give it this channel and this video's options
        return pool.submit(contextvars.copy_context().run, fn).result(timeout=seconds)
    except Timeout:
        raise PipelineError(f"no answer within {seconds}s (model overloaded?)")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def niche():
    """The wording of this channel's niche kind (NICHE_KIND) for prompts and checks."""
    return niches.kind(env("NICHE_KIND", "business"))


def text_models():
    """TEXT_MODEL first, then the fallbacks: free-tier models get overloaded (503) or run out of
    daily quota (429) independently of each other."""
    chain = [env("TEXT_MODEL", "gemini-3.8-flash")]
    chain += [m.strip() for m in env("TEXT_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.5-flash").split(",")]
    return [m for i, m in enumerate(chain) if m and m not in chain[:i]]


def make_plan(client, topic, history, episode, part=None, script=None):
    prompt, errors = _prompt(topic, history, episode, part, script), []
    for model in text_models():
        try:
            plan = _hard_timeout(lambda: Plan.model_validate_json(_output(client.interactions.create(
                model=model,
                input=prompt,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": Plan.model_json_schema()},
                timeout=80,
            ), "text")), 90)
            print(f"[info] script written by {model}", flush=True)
            return plan
        except Exception as e:
            print(f"[warn] {model} failed: {str(e)[:200]}", flush=True)
            errors.append(f"{model}: {str(e)[:160]}")
    raise PipelineError("all text models failed - " + " | ".join(errors))


def fact_check(job, client, plan, own_script=False):
    """A second, independent model call that checks every figure before anything is voiced.
    FACT_CHECK=flag only lists doubtful claims; fix also rewrites the body lines it is sure about
    (never the creator's own words, never a hook - the hooks carry their own loop lines).
    Returns the plan (changed or not). A failing check never stops the video."""
    mode = env("FACT_CHECK", "flag")
    if mode == "off":
        job.stage("check", "skipped", "off in Settings")
        return plan
    job.stage("check", "running", "checking every figure")
    N = niche()
    script = "\n".join([f"HOOK {i}: {h.text}" for i, h in enumerate(plan.hooks, 1)] +
                       [f"SCENE {i}: {s.narration}   [on screen: {s.on_screen}]" for i, s in enumerate(plan.scenes, 1)] +
                       ([f"COVER: {' / '.join(plan.cover_lines)}"] if plan.cover_lines else []))
    prompt = "\n".join([
        f"You are the fact checker for a YouTube Short about this {N['subject']}: {plan.topic}.",
        f"Audience: {env('CHANNEL_AUDIENCE', 'American viewers')}. Today is {dt.date.today().isoformat()}.",
        *N["fact_focus"],
        "Report only real problems: a figure far outside the typical range, a contradiction, an invented",
        "statistic or percentage, or a legal/safety claim that is wrong. A realistic figure is NOT an issue,",
        "even if other sources say a bit more or less. If everything is fine, verdict 'ok' and no issues.",
        f"For a body scene give fixed_narration: the whole line rewritten with {N['fact_fix']}, same voice",
        "and about the same length, spoken numbers (words, not digits). For a hook leave fixed_narration empty.",
        "",
        "SCRIPT:",
        script,
    ])
    report, model_used, errors = None, None, []
    for model in text_models():
        try:
            report = _hard_timeout(lambda: FactCheck.model_validate_json(_output(client.interactions.create(
                model=model, input=prompt, timeout=60,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": FactCheck.model_json_schema()}), "text")), 70)
            model_used = model
            break
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
    if report is None:
        _write_json(job.dir / "factcheck.json", {"mode": mode, "error": " | ".join(errors)})
        job.stage("check", "skipped", "no model answered - not checked")
        return plan

    applied = []
    if mode == "fix" and not own_script:
        for issue in report.issues:
            if issue.severity == "high" and 1 <= issue.scene <= len(plan.scenes) and issue.fixed_narration.strip():
                s = plan.scenes[issue.scene - 1]
                fixed = issue.fixed_narration.strip()
                if len(fixed) < 0.6 * len(s.narration):
                    # the model rewrote only the wrong sentence: swap that sentence, keep the rest of the line
                    sentences = re.split(r"(?<=[.!?])\s+", s.narration)
                    hit = next((k for k, x in enumerate(sentences) if issue.claim.lower()[:30] in x.lower()), None)
                    if hit is None:
                        continue
                    sentences[hit] = fixed
                    fixed = " ".join(sentences)
                s.narration = fixed
                if issue.fixed_on_screen.strip():
                    s.on_screen = issue.fixed_on_screen.strip()[:40]
                applied.append(issue.scene)
        if applied:
            (job.dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    _write_json(job.dir / "factcheck.json", {"mode": mode, "model": model_used, **report.model_dump(),
                                             "applied": applied, "at": _now()})
    n = len(report.issues)
    job.stage("check", "done", "all figures look realistic" if not n else
              f"{n} doubtful claim(s)" + (f", {len(applied)} fixed" if applied else " flagged - see Script tab"))
    return plan


def prompt_check(job, client, plan):
    """Review the picture prompts before FLUX spends neurons on them: rewrite the ones that ask for
    what image models draw badly (hands, text, crowds, screens...) or that don't show the scene.
    Writes promptcheck.json; a failing check never stops the video."""
    if not flag("PROMPT_CHECK", "true"):
        return plan
    listing = "\n".join([f"SCENE 0 (hook): {plan.hook_visual}"] +
                        [f"SCENE {i}: {x.image_prompt}   [voice: {x.narration}]" for i, x in enumerate(plan.scenes, 1)])
    prompt = "\n".join([
        f"You review picture prompts for FLUX.1 schnell, for a YouTube Short about: {plan.topic}.",
        f"Every picture gets this style added: {env('IMAGE_STYLE', 'photorealistic, cinematic light')}.",
        "Rewrite a prompt ONLY if it is likely to give a defective or wrong picture:",
        "- hands, fingers or people holding/using small objects in close-up; faces in close-up;",
        "- crowds or several people interacting; distorted bodies are likely;",
        "- any readable text: signs, screens, price tags, receipts, labels, money with figures, charts;",
        "- mirrors, reflections, keyboards, intricate machinery or many small identical parts up close;",
        "- several unrelated subjects in one frame, or a subject that does not show what the voice says;",
        "- abstract ideas (profit, success) instead of something visible.",
        "A rewrite keeps the scene's meaning: one clear subject, a plain setting, a camera angle and light,",
        "people from behind, at a distance or as silhouettes. Max 45 words, English, no text in the picture.",
        "Return only the prompts you changed; an empty list if all are fine.",
        "",
        listing,
    ])
    report, errors = None, []
    for model in text_models():
        try:
            report = _hard_timeout(lambda: PromptCheck.model_validate_json(_output(client.interactions.create(
                model=model, input=prompt, timeout=60,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": PromptCheck.model_json_schema()}), "text")), 70)
            break
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
    if report is None:
        _write_json(job.dir / "promptcheck.json", {"error": " | ".join(errors), "at": _now()})
        return plan
    changed = []
    for fix in report.fixes:
        new = " ".join(fix.prompt.split())[:400]
        if not new:
            continue
        if fix.scene == 0:
            changed.append({"scene": 0, "problem": fix.problem, "before": plan.hook_visual, "after": new})
            plan.hook_visual = new
        elif 1 <= fix.scene <= len(plan.scenes):
            s = plan.scenes[fix.scene - 1]
            changed.append({"scene": fix.scene, "problem": fix.problem, "before": s.image_prompt, "after": new})
            s.image_prompt = new
    if changed:
        (job.dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    _write_json(job.dir / "promptcheck.json", {"changed": changed, "at": _now()})
    detail = (job.status["stages"].get("check") or {}).get("detail") or ""
    if changed:
        job.stage("check", (job.status["stages"].get("check") or {}).get("state") or "done",
                  (detail + " · " if detail else "") + f"{len(changed)} picture prompt(s) cleaned")
    return plan


def mock_plan(topic):
    body = [
        ("An octopus tastes its food with its arms, and they hold hundreds of millions of neurons.",
         "octopus arm tasting a shell, macro"),
        ("About two thirds of its neurons live in those arms, not in its head.",
         "glowing neural network inside octopus arms"),
        ("Each arm decides what to grab while the head is busy with something else.",
         "octopus arm grabbing a crab underwater"),
        ("A severed arm will keep reaching for food for a while on its own.",
         "lone octopus arm reaching toward food"),
        ("Engineers copy this distributed brain to build softer, smarter robot hands.",
         "soft robot arm inspired by octopus, lab"),
        ("So when an octopus looks at you, its arms have already made up their minds.",
         "octopus eye close-up looking at camera"),
    ]
    return Plan(
        topic=topic or "How an octopus thinks with its arms",
        hooks=[
            Hook(formula_id="the-statistic", text="Two thirds of an octopus's brain isn't in its head.",
                 loop_line="And the strangest part? Most of that thinking happens far from its eyes, because"),
            Hook(formula_id="the-question", text="Why does an octopus arm keep hunting after it's cut off?",
                 loop_line="Which leaves one question nobody can quite answer:"),
            Hook(formula_id="the-claim", text="An octopus thinks with its arms. Literally.",
                 loop_line="So don't let anyone tell you it's just a figure of speech."),
        ],
        hook_visual="dramatic octopus in deep blue water, cinematic light",
        hook_stock_query="octopus underwater",
        hook_on_screen="2/3 of its brain",
        scenes=[Scene(narration=n, image_prompt=p, stock_query=" ".join(p.split()[:3]),
                      on_screen=f"Fact #{i + 1}") for i, (n, p) in enumerate(body)],
        parts=[],
        titles=["An Octopus Thinks With Its Arms: 2/3 of Its Brain",
                "Octopus Arms Have Their Own Minds",
                "Why Octopus Arms Are Smarter Than You Think"],
        description="How an octopus's nervous system works and why each arm can decide on its own. "
                    "#shorts #science #animals",
        tags=["octopus", "octopus brain", "animal facts", "science", "biology"],
    )


# ------------------------------------------------------------- yt-skills scoring

def _run_skill(script, path):
    exe = SKILLS / script
    if not exe.exists():
        return None
    res = subprocess.run(["python3", str(exe), "--json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


_digits = hookscore_shorts.digits


def score_hooks(job_dir, texts, topic=""):
    """Score hooks in the input order: each result has the spoken text in 'hook', the digit form that
    was scored in 'scored_as', 'properties' and 'verdict'. HOOK_SCORER=shorts (default) uses this
    channel's scorer (hookscore_shorts.py); 'skill' uses the yt-script skill's general hookscore.py."""
    texts = [t.replace("\n", " ").strip() for t in texts]
    if env("HOOK_SCORER", "shorts") != "skill":
        return [hookscore_shorts.score(t, topic, env("NICHE_KIND", "business")) for t in texts]
    forms = [_digits(t) for t in texts]
    path = job_dir / "hooks.txt"
    path.write_text("\n".join(forms) + "\n", encoding="utf-8")
    scored = _run_skill("yt-script/hookscore.py", path)
    if not scored:
        return None
    by_form = {}
    for r in scored:
        by_form.setdefault(r["hook"].strip(), r)
    out = []
    for t, f in zip(texts, forms):
        r = dict(by_form.get(f.strip()) or {"verdict": 0, "band": "WEAK", "formula": "", "properties": {}})
        out.append({**r, "hook": t, "scored_as": f})
    return out


HOOK_FIX = {
    "SPECIFICITY": "put one concrete number, price or name from the body in it",
    "ADDRESS": "say 'you' or 'your' in the first six words",
    "STAKES": "name what it costs the viewer, what they lose or waste, or what to stop doing",
    "CURIOSITY": "open a gap (why / how / but / until) and do not answer it in the same sentence",
    "BREVITY": "9 to 18 words",
}


def _ask(client, prompt, model_cls, limit=70):
    errors = []
    for model in text_models():
        try:
            return _hard_timeout(lambda: model_cls.model_validate_json(_output(client.interactions.create(
                model=model, input=prompt, timeout=60,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": model_cls.model_json_schema()}), "text")), limit)
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
    raise PipelineError("no model answered - " + " | ".join(errors))


def improve_hooks(job, client, plan):
    """Rewrite the hooks until the best one reaches HOOK_MIN_SCORE (default 70) or HOOK_ROUNDS run out.
    Every rewrite goes through a second, strict review - on this exact business, every figure backed by
    the body, natural speech, no false promise - so a hook never buys points by drifting off topic or
    stuffing keywords. The 5 best hooks that passed are kept; hookcheck.json records each round."""
    target, rounds = int(env("HOOK_MIN_SCORE", "55")), int(env("HOOK_ROUNDS", "3"))
    first = score_hooks(job.dir, [h.text for h in plan.hooks], plan.topic)
    if not first or target <= 0 or rounds <= 0:
        return plan
    pool = [(r["verdict"], h, r) for h, r in zip(plan.hooks, first)]
    log = [{"round": 0, "best": max(v for v, _, _ in pool), "hooks": [r["verdict"] for _, _, r in pool]}]
    body = "\n".join(f"{i}. {s.narration}" for i, s in enumerate(plan.scenes, 1))
    N = niche()
    for rnd in range(1, rounds + 1):
        best = max(v for v, _, _ in pool)
        if best >= target:
            break
        job.stage("script", "running", f"best hook {best}/100 - rewriting for {target}+ (round {rnd} of {rounds})")
        notes = []
        for v, h, r in sorted(pool, key=lambda x: -x[0])[:5]:
            props = r.get("properties") or {}
            low = sorted(props, key=props.get)[:2]
            fixes = {**HOOK_FIX, **hookscore_shorts.fix(env("NICHE_KIND", "business"))}
            notes.append(f"- [{h.formula_id}] \"{h.text}\" scores {v}; weakest: " +
                         "; ".join(f"{k} {props[k]} -> {fixes.get(k, '')}" for k in low))
        prompt = "\n".join([
            f"You sharpen hooks for a YouTube Short in the series about one {N['subject']}: {plan.topic}.",
            f"Audience: {env('CHANNEL_AUDIENCE', 'American viewers')}. Language: {env('CHANNEL_LANGUAGE', 'English')}.",
            f"Each hook is scored 0-100; the target is {target}+. The score is 70% the average and 30% the",
            "WEAKEST of five properties, so fix the weakest ones first:",
            *[f"- {k}: {d}" for k, d in hookscore_shorts.describe(env("NICHE_KIND", "business")).items()],
            "A strong hook has a figure from the body in its first five words, says what is won or lost",
            f"{N['hook_stakes']} and leaves a gap the video closes, e.g. {N['hook_example']}",
            "- but only with figures and facts from THIS body.",
            "",
            "Current hooks and what holds each back:",
            *notes,
            "",
            "Write 2 new versions of each hook above (10 in total), keeping its formula_id. Rules:",
            f"- about THIS {N['subject']} only; the viewer must know within the hook what it is about;",
            "- every figure must be one the body states (below) - never invent or inflate numbers;",
            "- spoken English a real person would say; numbers in words, no digits or symbols;",
            "- no false promise, no 'guaranteed', no clickbait the video does not pay off;",
            "- 'you' at most twice; no keyword lists.",
            "Each needs its own loop_line (max 14 words): the video's last sentence that flows straight into",
            "the hook when the Short replays; it never repeats the hook.",
            "",
            "BODY OF THE VIDEO:",
            body,
        ])
        try:
            candidates = [h for h in _ask(client, prompt, HookSet).hooks if h.text.strip()][:12]
        except PipelineError as e:
            log.append({"round": rnd, "error": str(e)[:300]})
            break
        listing = "\n".join(f"{i}. {h.text}   [loop_line said right before it: {h.loop_line}]"
                            for i, h in enumerate(candidates, 1))
        review = "\n".join([
            f"You are a strict editor for a YouTube Short about this {N['subject']}: {plan.topic}.",
            f"Pass a hook ONLY if all are true: (1) it is clearly about this exact {N['subject']}; (2) every number,",
            f"statistic or fact about the {N['subject']} is stated in, or follows directly from, the body below -",
            "nothing invented or inflated; (3) it sounds like natural speech, not keyword stuffing; (4) it",
            "promises nothing the body does not deliver; (5) no spelling or grammar mistakes; (6) a listener",
            "understands it on first hearing - reject anything forced, contradictory or meaningless read aloud",
            "(e.g. 'how can you never risk your savings before...'); (7) its loop_line, said right before it,",
            "flows into it as one natural thought.",
            f"Framing the viewer's own situation ({N['viewer_framing']}) is",
            f"fine and is NOT an invented fact - judge only claims about the {N['subject']} and its numbers.",
            "",
            "BODY:", body, "", "HOOKS:", listing,
        ])
        try:
            verdicts = {v.n: v for v in _ask(client, review, HookReview).verdicts}
        except PipelineError as e:
            log.append({"round": rnd, "error": str(e)[:300]})
            break
        passed = [h for i, h in enumerate(candidates, 1) if verdicts.get(i) and verdicts[i].ok]
        rejected = [{"hook": h.text, "reason": verdicts[i].reason if verdicts.get(i) else "no verdict"}
                    for i, h in enumerate(candidates, 1) if not (verdicts.get(i) and verdicts[i].ok)]
        scored = score_hooks(job.dir, [h.text for h in passed], plan.topic) or []
        known = {h.text.strip().lower() for _, h, _ in pool}
        for h, r in zip(passed, scored):
            if h.text.strip().lower() not in known:
                pool.append((r["verdict"], h, r))
                known.add(h.text.strip().lower())
        log.append({"round": rnd, "best": max(v for v, _, _ in pool), "written": len(candidates),
                    "passed": [{"hook": h.text, "score": r["verdict"]} for h, r in zip(passed, scored)],
                    "rejected": rejected})
    pool.sort(key=lambda x: -x[0])
    plan.hooks = [h for _, h, _ in pool[:5]]
    (job.dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    _write_json(job.dir / "hookcheck.json", {"target": target, "best": pool[0][0], "rounds": log, "at": _now()})
    return plan


def pick_hook(job, hooks, topic=""):
    scored = score_hooks(job, [h.text for h in hooks], topic)
    if not scored:
        return hooks[0].text, None, []
    best = max(scored, key=lambda r: r["verdict"])
    return best["hook"], best, scored


def pick_title(job, titles):
    titles = [t.strip()[:100] for t in titles if t.strip()]
    path = job / "titles.txt"
    path.write_text("\n".join(titles) + "\n", encoding="utf-8")
    scored = _run_skill("yt-package/title.py", path)
    if not scored:
        return titles[0], None, []
    best = max(scored, key=lambda r: r["score"])
    return best["title"], best, scored


# ---------------------------------------------------------------------- voice

def _write_wav(path, pcm, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _restore_punctuation(text, words):
    """edge-tts word boundaries come without punctuation; captions break on it, so copy the
    trailing punctuation back from the script by walking both in order."""
    pos = 0
    for wd in words:
        at = text.find(wd["w"], pos)
        if at < 0:
            continue
        end = at + len(wd["w"])
        while end < len(text) and text[end] in ".,!?;:…\"')—":
            end += 1
        wd["w"] = text[at:end]
        pos = end


def tts_edge(text, path):
    """Free Microsoft Edge neural voices. Returns word timings for exact subtitles."""
    import edge_tts

    async def run():
        com = edge_tts.Communicate(text, env("EDGE_VOICE", "en-US-AndrewMultilingualNeural"),
                                   rate=env("EDGE_RATE", "+8%"), pitch=env("EDGE_PITCH", "+0Hz"),
                                   volume=env("EDGE_VOLUME", "+0%"), boundary="WordBoundary")
        audio, words = bytearray(), []
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
            elif chunk["type"] == "WordBoundary":
                words.append({"w": chunk["text"], "s": chunk["offset"] / 1e7,
                              "e": (chunk["offset"] + chunk["duration"]) / 1e7})
        if not audio:
            raise PipelineError("edge-tts returned no audio")
        return bytes(audio), words

    mp3, words = _retry(lambda: asyncio.run(run()), "voice-over (edge-tts)")
    _restore_punctuation(text, words)
    src = path.with_suffix(".mp3")
    src.write_bytes(mp3)
    _ffmpeg(["-i", str(src), "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", str(path)])
    return words


def tts_gemini(client, text, path):
    raw = _retry(lambda: _output(client.interactions.create(
        model=env("TTS_MODEL", "gemini-3.8-flash-tts"),
        input=[{"type": "user_input", "content": [{
            "type": "text", "text": text,
            "annotations": [{"type": "speech_metadata",
                             "style": env("TTS_STYLE", "energetic, confident, fast-paced")}],
        }]}],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": env("TTS_VOICE", "Puck")}]},
        timeout=180,
    ), "audio"), "voice-over (gemini)")
    if raw[:4] == b"RIFF":
        path.write_bytes(raw)
    else:  # headerless 24 kHz mono s16le, per the TTS docs
        _write_wav(path, raw)
    return None


def mock_tts(text, path):
    seconds = max(1.5, len(text.split()) * 0.38)
    rate = 24000
    frames = bytearray()
    for i in range(int(seconds * rate)):
        frames += struct.pack("<h", int(1500 * math.sin(2 * math.pi * 220 * i / rate)))
    _write_wav(path, bytes(frames), rate)
    return None


def voice_over(client, text, path, mock):
    """edge-tts is free, so test runs use it too; the tone is only a fallback for offline tests."""
    provider = env("TTS_PROVIDER", "edge")
    if provider == "edge":
        try:
            return tts_edge(text, path)
        except PipelineError:
            if not mock:
                raise
            return mock_tts(text, path)
    if mock:
        return mock_tts(text, path)
    return tts_gemini(client, text, path)


# --------------------------------------------------------------------- visuals

def image(client, prompt, path):
    path.write_bytes(_retry(lambda: _output(client.interactions.create(
        model=env("IMAGE_MODEL", "gemini-3.1-flash-lite-image"),
        input=f"{prompt}. Vertical 9:16 composition, no text, no letters, no watermark.",
        response_format={"type": "image", "aspect_ratio": "9:16"},
        timeout=180,
    ), "image"), "image"))


def mock_image(idx, path):
    _ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=768x1376:r=1", "-vf", f"hue=h={idx * 50}",
             "-frames:v", "1", str(path)])


def hook_video(client, prompt, path):
    """Live-action opener via Gemini Omni Flash. Returns True on success, False on any failure
    so the run falls back to an image instead of dying."""
    try:
        it = client.interactions.create(
            model=env("VIDEO_MODEL", "gemini-omni-1.1-flash"),
            input=f"{prompt}. Vertical, dynamic camera move, no text, no speech.",
            response_format={"type": "video", "aspect_ratio": "9:16", "delivery": "uri"},
            timeout=300,  # with polling below this stays inside n8n's 20-minute request timeout
        )
        video = it.output_video
        if video is None:
            return False
        if getattr(video, "data", None):
            path.write_bytes(base64.b64decode(video.data))
            return True
        name = "files/" + video.uri.split("/files/")[-1].split(":")[0].split("?")[0]
        for _ in range(60):
            state = client.files.get(name=name).state.name
            if state == "ACTIVE":
                break
            if state == "FAILED":
                return False
            time.sleep(5)
        path.write_bytes(client.files.download(file=video.uri))
        return True
    except Exception as e:
        print(f"[warn] video hook failed, using an image instead: {e}", flush=True)
        return False


# ---------------------------------------------------------------------- render

def _ffmpeg(args, cwd=None):
    res = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                         cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise PipelineError("ffmpeg failed: " + res.stderr[-1500:])


def _wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _probe_seconds(path):
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def _encode_args():
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-an"]


def segment_from_image(img, frames, idx, out, overlay="", loop=False):
    zm = max(0.0, min(0.3, _num("ZOOM", 0.12)))
    z = f"1+{zm}*on/{frames}" if idx % 2 == 0 else f"{1 + zm}-{zm}*on/{frames}"
    # alternate the drift so consecutive scenes do not move the same way
    x = ["iw/2-(iw/zoom/2)",
         f"(iw-iw/zoom)*on/{frames}",
         f"(iw-iw/zoom)*(1-on/{frames})"][idx % 3]
    if loop:
        # the loop ending shows the hook picture and zooms out to exactly the hook's first frame
        # (zoom 1, centred), so when the Short replays the cut is invisible
        z, x = f"{1 + zm}-{zm}*on/{max(1, frames - 1)}", "iw/2-(iw/zoom/2)"
    # zoompan cost grows with the source size; 1.4x is smooth enough and ~2x faster than 2x on a laptop CPU
    sw, sh = int(W * 1.4) // 2 * 2, int(H * 1.4) // 2 * 2
    vf = (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
          f"zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}"
          + (f",{overlay}" if overlay else ""))
    _ffmpeg(["-i", str(img), "-vf", vf, "-frames:v", str(frames), *_encode_args(), out.name],
            cwd=out.parent)


def segment_from_video(clip, frames, out, overlay=""):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
          "tpad=stop_mode=clone:stop_duration=60" + (f",{overlay}" if overlay else ""))
    _ffmpeg(["-i", str(clip), "-vf", vf, "-frames:v", str(frames), *_encode_args(), out.name],
            cwd=out.parent)


FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CARD_PALETTES = [("0x0b3d2e", "0x020806"), ("0x14213d", "0x05070d"),
                 ("0x3d2b0b", "0x0a0703"), ("0x2b0b3d", "0x07020d")]


def _wrap(text, width):
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines[:4]


def text_overlay(job, idx, text, label, shade=False, force_text=False):
    """drawtext filters for the scene's key fact (big, gold, centred above the captions) and the
    series tag at the top. Text goes through files so no character ever needs escaping.
    The hook (idx 0) shows its text from the very first frame, the frame a loop ending returns to."""
    fade = "" if idx == 0 else ":alpha='min(1,t/0.25)'"
    darkness = max(0.0, min(0.9, _num("SHADE", 0.35)))
    parts = [f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{darkness}:t=fill"] if shade and darkness else []
    show = force_text or flag("OVERLAY_TEXT", "true")  # a number card is nothing without its text
    lines = _wrap(text.upper(), 13) if show else []
    colour = _hex_ff(env("OVERLAY_COLOR", "#FFD54A"), "FFD54A")
    if lines:
        longest = max(len(x) for x in lines)
        size = 150 if longest <= 9 else 116 if longest <= 11 else 96
        top = 780 - len(lines) * size * 1.15 / 2
        for k, line in enumerate(lines):
            f = job / f"text_{idx:02}_{k}.txt"
            f.write_text(line, encoding="utf-8")
            parts.append(f"drawtext=fontfile={FONT_BOLD}:textfile={f.name}:fontsize={size}:fontcolor={colour}"
                         f":borderw=7:bordercolor=black@0.7:x=(w-text_w)/2:y={int(top + k * size * 1.15)}"
                         + fade)
    if label:
        f = job / f"text_{idx:02}_label.txt"
        f.write_text(label, encoding="utf-8")
        parts.append(f"drawtext=fontfile={FONT_BOLD}:textfile={f.name}:fontsize=52:fontcolor=white"
                     f":box=1:boxcolor={_hex_ff(env('LABEL_COLOR', '#FF0033'), 'FF0033')}@0.92:boxborderw=18"
                     ":x=(w-text_w)/2:y=290")
    return ",".join(parts)


def segment_card(job, text, label, frames, idx, out, loop=False):
    """Free visual: a slowly moving dark gradient with the scene's key number on it.
    A loop ending reuses the hook's colours so the replay starts on the same look."""
    c0, c1 = CARD_PALETTES[0 if loop else idx % len(CARD_PALETTES)]
    src = (f"gradients=s={W}x{H}:c0={c0}:c1={c1}:x0=0:y0=0:x1={W}:y1={H}:speed=0.02"
           f":r={FPS}:d={frames / FPS + 0.5:.3f}")
    vf = text_overlay(job, idx, text, label, force_text=True) or "null"
    _ffmpeg(["-f", "lavfi", "-i", src, "-vf", vf, "-frames:v", str(frames), *_encode_args(), out.name],
            cwd=job)


def visual_provider():
    p = env("VISUAL_PROVIDER", "auto")
    if p == "auto":
        if env("CF_API_TOKEN") and env("CF_ACCOUNT_ID"):
            return "flux"
        return "pexels" if env("PEXELS_API_KEY") else "openverse"
    return p


def flux_image(prompt, path):
    """Free AI picture: FLUX.1 schnell (Apache 2.0) on Cloudflare Workers AI, 10k neurons/day.
    Returns a square JPEG; the renderer crops it to 9:16."""
    import urllib.error
    import urllib.request
    url = (f"https://api.cloudflare.com/client/v4/accounts/{env('CF_ACCOUNT_ID')}/ai/run/"
           f"{env('FLUX_MODEL', '@cf/black-forest-labs/flux-1-schnell')}")
    body = json.dumps({
        "prompt": f"{prompt}. Vertical composition, subject centered, "
                  f"{env('IMAGE_STYLE', 'photorealistic, cinematic light')}, sharp focus, natural proportions, "
                  "anatomically correct, clean uncluttered background, no text, no letters, no logos, no watermark.",
        "steps": int(env("FLUX_STEPS", "6")),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {env('CF_API_TOKEN')}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PipelineError(f"cloudflare HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
    image = (data.get("result") or {}).get("image")
    if not data.get("success", True) or not image:
        raise PipelineError(f"cloudflare returned no image: {str(data.get('errors'))[:300]}")
    raw = path.with_suffix(".flux")
    raw.write_bytes(base64.b64decode(image))
    try:
        _to_jpg(raw, path)
    finally:
        raw.unlink(missing_ok=True)


def _to_jpg(src, out):
    """Normalise any downloaded or uploaded picture (png, webp, heic-less jpg...) to a plain jpg."""
    _ffmpeg(["-i", str(src), "-frames:v", "1", "-vf", "scale='min(2160,iw)':-2", "-q:v", "3", str(out)])


def openverse_image(query, path, used):
    """Free, keyless real photos from Openverse (WordPress.org's CC search). Only CC0, public
    domain and CC BY: ShareAlike would put the whole video under its licence. Returns a credit
    line, or None when nothing usable was found."""
    import urllib.parse
    import urllib.request
    headers = {"User-Agent": "VidAuto/1.0 (personal YouTube Shorts tool)"}
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {"q": query, "license": "cc0,pdm,by", "category": "photograph", "page_size": 20, "mature": "false"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        results = json.loads(r.read()).get("results", [])
    # Openverse ranks loosely ("yard" finds an army depot), so demand that the photo's own title
    # and tags share at least two words with the query (one if the query has only one).
    keys = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2}
    need = min(2, len(keys))
    for item in results:
        if item.get("id") in used or (item.get("width") or 1000) < 640:
            continue
        about = " ".join([item.get("title") or ""] + [t.get("name", "") for t in item.get("tags") or []]).lower()
        if sum(1 for k in keys if k in about) < need:
            continue
        raw = path.with_suffix(".download")
        try:
            req = urllib.request.Request(item["url"], headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, open(raw, "wb") as out:
                shutil.copyfileobj(r, out)
            _to_jpg(raw, path)
        except Exception as e:  # dead link or not an image: try the next result
            print(f"[warn] openverse image skipped: {e}", flush=True)
            continue
        finally:
            raw.unlink(missing_ok=True)
        used.add(item.get("id"))
        if item.get("license") in ("cc0", "pdm"):
            return f"Photo: \"{item.get('title') or 'untitled'}\" ({item.get('license').upper()}) via Openverse"
        return item.get("attribution") or f"Photo by {item.get('creator')} (CC BY) via Openverse"
    return None


def pexels_clip(query, path, used):
    """Free stock video (Pexels license: free for commercial use). Returns a credit line, or None
    when nothing suitable was found."""
    import urllib.parse
    import urllib.request
    headers = {"Authorization": env("PEXELS_API_KEY"), "User-Agent": "VidAuto/1.0"}
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "orientation": "portrait", "size": "medium", "per_page": 15})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = json.loads(r.read())
    for v in data.get("videos", []):
        if v.get("id") in used or (v.get("duration") or 0) < 3:
            continue
        files = [f for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 1280]
        if not files:
            continue
        best = min(files, key=lambda f: abs((f.get("height") or 0) - H))
        req = urllib.request.Request(best["link"], headers={"User-Agent": "VidAuto/1.0"})
        with urllib.request.urlopen(req, timeout=180) as r, open(path, "wb") as out:
            shutil.copyfileobj(r, out)
        used.add(v.get("id"))
        return f"Video by {v.get('user', {}).get('name', 'Pexels')} on Pexels: {v.get('url', 'https://www.pexels.com')}"
    return None


def _mix_sfx(pcm, rate, starts):
    """Lay the whoosh from data/sfx/transition.wav under every scene cut."""
    from array import array
    sfx = DATA / "sfx" / "transition.wav"
    vol = float(env("SFX_VOLUME", "0.5") or 0)
    if vol <= 0 or not sfx.exists() or len(starts) < 2:
        return pcm
    with wave.open(str(sfx), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, rate):
            return pcm
        fx = array("h", w.readframes(w.getnframes()))
    out = array("h", bytes(pcm))
    for st in starts[1:]:
        at = max(0, int((st - 0.2) * rate))
        for k, v in enumerate(fx[: max(0, len(out) - at)]):
            out[at + k] = max(-32768, min(32767, out[at + k] + int(v * vol)))
    return bytearray(out.tobytes())


def _ass_time(t):
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def _chunk_full(cur, text, last_word):
    """A caption chunk ends after CAPTION_WORDS words, when it gets too wide, or at punctuation."""
    n = int(max(1, min(6, _num("CAPTION_WORDS", 3))))
    return len(cur) >= n or len(text) >= 6 * n - 2 or bool(re.search(r"[.!?,:;—]$", last_word))


def _chunks(text):
    out, cur = [], []
    for word in text.split():
        cur.append(word)
        if _chunk_full(cur, " ".join(cur), word):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _timed_chunks(scene):
    """(text, start, end) caption chunks for one scene. Word timings from edge-tts when we have
    them; otherwise each chunk gets a share of the scene's audio length by character count."""
    words = scene.get("words")
    if words:
        groups, cur = [], []
        for wd in words:
            cur.append(wd)
            text = " ".join(x["w"] for x in cur)
            if _chunk_full(cur, text, wd["w"]):
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        timed = []
        for i, group in enumerate(groups):
            start = scene["start"] + group[0]["s"]
            # hold each caption until the next one starts, so the screen never blinks empty
            nxt = groups[i + 1][0]["s"] if i + 1 < len(groups) else group[-1]["e"] + 0.15
            timed.append((" ".join(x["w"] for x in group), start, scene["start"] + nxt))
        return timed
    parts = _chunks(scene["text"])
    weights = [len(p) + 2 for p in parts]
    total = sum(weights)
    t, timed = scene["start"], []
    for p, wgt in zip(parts, weights):
        end = t + scene["speech"] * wgt / total
        timed.append((p, t, end))
        t = end
    return timed


CAPTION_PLACES = {"lower third": (2, 640), "bottom": (2, 260), "middle": (5, 0), "top": (8, 420)}


def write_subtitles(scenes, path):
    """Word-synced captions, styled by the Captions settings."""
    align, margin = CAPTION_PLACES.get(env("CAPTION_POSITION", "lower third"), CAPTION_PLACES["lower third"])
    box = flag("CAPTION_BOX")
    outline_colour = "&H60000000" if box else _hex_ass(env("CAPTION_OUTLINE_COLOR", "#000000"))
    style = ",".join(str(x) for x in [
        "Main", env("CAPTION_FONT", "DejaVu Sans"), int(_num("CAPTION_SIZE", 84)),
        _hex_ass(env("CAPTION_COLOR", "#FFFFFF")), "&H000000FF", outline_colour,
        "&H60000000" if box else "&H78000000", -1, 0, 0, 0, 100, 100, 0, 0,
        3 if box else 1, 14 if box else int(_num("CAPTION_OUTLINE", 7)), 0 if box else int(_num("CAPTION_SHADOW", 3)),
        align, 70, 70, margin, 1])
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: " + style,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    highlight = r"{\c&H" + _hex_ass(env("CAPTION_HIGHLIGHT", "#FFF200"))[4:] + "&}"  # inline colour has no alpha
    pop = r"{\fscx85\fscy85\t(0,90,\fscx100\fscy100)}" if flag("CAPTION_POP", "true") else ""
    upper = flag("CAPTION_UPPERCASE", "true")
    events = []
    for s in scenes:
        for i, (p, start, end) in enumerate(_timed_chunks(s)):
            text = (p.upper() if upper else p).replace("{", "(").replace("}", ")").replace("\\", "/")
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Main,,0,0,0,,"
                          + pop + (highlight if i % 2 else "") + text)
    path.write_text("\n".join(head + events) + "\n", encoding="utf-8")


def make_thumb(video, out):
    _ffmpeg(["-ss", "1", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", str(out)])


# ------------------------------------------------------------------------ cover

MONEY_RE = re.compile(r"\$|\d")


def _cover_lines(d):
    """The plan's cover lines, or - for older plans - the first money figure plus the business name."""
    plan = _read_json(d / "plan.json") or {}
    if plan.get("cover_lines"):
        return plan["cover_lines"]
    scenes = (_read_json(d / "scenes.json") or {}).get("scenes") or []
    figures = [s.get("on_screen", "") for s in scenes if "$" in s.get("on_screen", "")]
    money = re.search(r"\$\s?[\d,.]+\s*[KkMm]?(\s*/\s*\w+)?", figures[0]).group(0) if figures else ""
    name = re.sub(r"\(.*?\)|business|route", "", plan.get("topic") or "", flags=re.I).split()[:4]
    return [x for x in [money.replace(" ", "")] + [" ".join(name[:2]), " ".join(name[2:4])] if x]


def _cover_picture(d, choice):
    """(scene index, picture path) for the cover; (None, None) = a plain gradient."""
    scenes = (_read_json(d / "scenes.json") or {}).get("scenes") or []
    pics = [(i, d / s["visual"]) for i, s in enumerate(scenes)
            if s.get("visual") and not s.get("is_video") and not s.get("card") and not s.get("loop")
            and (d / s["visual"]).exists()]
    if not pics:
        return None, None
    if str(choice).isdigit():
        return next((p for p in pics if p[0] == int(choice)), pics[0])
    if choice == "money scene":
        return next((p for p in pics if "$" in scenes[p[0]].get("on_screen", "")), pics[0])
    if choice == "best photo":  # the most detailed picture compresses worst: a decent proxy for 'busiest'
        return max(pics, key=lambda p: p[1].stat().st_size)
    return pics[0]


def make_cover(d, lines=None, scene=None):
    """cover.jpg, 1080x1920: the hook picture (or the chosen scene), the money figure huge in yellow,
    the business name in white on a red box, the series badge on top. Text sits in the upper-middle,
    where the Shorts / Reels / TikTok buttons and captions do not cover it. Returns the file name."""
    lines = [" ".join(x.split()).upper()[:18] for x in (lines or _cover_lines(d)) if x and x.strip()][:3]
    if not lines:
        raise PipelineError("no text for the cover")
    idx, pic = _cover_picture(d, env("COVER_SCENE", "hook") if scene is None else scene)
    label = (_read_json(d / "scenes.json") or {}).get("label") or ""
    dark = max(0.0, min(0.8, _num("COVER_DARKEN", 0.3)))
    if pic:
        src = ["-i", str(pic)]
        vf = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}", "eq=saturation=1.3:contrast=1.08"]
    else:
        src = ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0={CARD_PALETTES[0][0]}:c1={CARD_PALETTES[0][1]}:d=1"]
        vf = []
    if dark:
        vf.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{dark}:t=fill")
    money_col = _hex_ff(env("COVER_MONEY_COLOR", "#FFE200"), "FFE200")
    box_col = _hex_ff(env("COVER_BOX_COLOR", "#E50914"), "E50914")
    blocks = []
    for k, line in enumerate(lines):
        money = k == 0 and bool(MONEY_RE.search(line))
        # DejaVu Sans Bold capitals and digits are ~0.75 of the font size wide; keep the outline inside the frame
        size = int(min(240 if money else 140, (920 if money else 860) / (0.75 * max(len(line), 1))))
        blocks.append((line, size, money))
    gap = 56
    total = sum(s for _, s, _ in blocks) + gap * (len(blocks) - 1)
    y = int(820 - total / 2)
    for k, (line, size, money) in enumerate(blocks):
        f = d / f"cover_{k}.txt"
        f.write_text(line, encoding="utf-8")
        style = (f"fontcolor={money_col}:borderw=16:bordercolor=black:shadowx=8:shadowy=8:shadowcolor=black@0.6" if money
                 else f"fontcolor=white:box=1:boxcolor={box_col}@0.95:boxborderw=26")
        vf.append(f"drawtext=fontfile={FONT_BOLD}:textfile={f.name}:fontsize={size}:{style}:x=(w-text_w)/2:y={y}")
        y += size + gap
    if label:
        (d / "cover_label.txt").write_text(label, encoding="utf-8")
        vf.append(f"drawtext=fontfile={FONT_BOLD}:textfile=cover_label.txt:fontsize=58:fontcolor=white:box=1"
                  f":boxcolor=black@0.75:boxborderw=20:x=(w-text_w)/2:y=230")
    _ffmpeg([*src, "-frames:v", "1", "-vf", ",".join(vf), "-q:v", "2", "cover.jpg"], cwd=d)
    for f in d.glob("cover_*.txt"):
        f.unlink(missing_ok=True)
    (d / "cover_label.txt").unlink(missing_ok=True)
    _write_json(d / "cover.json", {"lines": lines, "scene": idx, "at": _now()})
    return "cover.jpg"


def cover(job_id, lines=None, scene=None):
    """Re-make a finished video's cover with the creator's own text or another picture."""
    d = job_dir(job_id)
    if not (d / "scenes.json").exists():
        raise PipelineError("this video has no pictures to make a cover from")
    clean = [x for x in (lines or []) if str(x).strip()] or None
    name = make_cover(d, clean, scene)
    result = _read_json(d / "result.json")
    if result is not None:
        result["cover"] = name
        _write_json(d / "result.json", result)
    return _read_json(d / "cover.json")


def save_scenes(job, scenes, scenes_in, label, credits):
    """What each scene is made of, so one picture can be replaced and the video re-rendered."""
    _write_json(job / "scenes.json", {"label": label, "credits": credits, "scenes": [
        {"text": s["text"], "audio": s["audio"].name, "words": s["words"],
         "visual": s["visual"].name if s["visual"] else None, "is_video": s["is_video"],
         "card": list(s["card"]) if s["card"] else None, "loop": bool(src.get("loop")),
         "on_screen": src["on_screen"], "prompt": src["prompt"], "query": src["query"]}
        for s, src in zip(scenes, scenes_in)]})


def load_scenes(job):
    data = _read_json(job / "scenes.json")
    if not data:
        raise PipelineError("this video was made before re-rendering existed - create a new one")
    return data


def replace_scene_image(job_id, idx, stream, filename):
    """Put the user's own picture (e.g. made in the Gemini app) into scene idx."""
    d = job_dir(job_id)
    data = load_scenes(d)
    if not 0 <= idx < len(data["scenes"]):
        raise PipelineError("no such scene")
    if data["scenes"][idx].get("loop"):
        raise PipelineError("the loop ending always shows the hook picture - replace the hook's picture instead")
    ext = Path(filename or "x.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise PipelineError("upload a .png, .jpg or .webp picture")
    raw = d / f"upload_{idx:02}{ext}"
    with open(raw, "wb") as f:
        shutil.copyfileobj(stream, f, length=1024 * 1024)
    try:
        _to_jpg(raw, d / f"img_{idx:02}.jpg")
    finally:
        raw.unlink(missing_ok=True)
    sc = data["scenes"][idx]
    sc.update(visual=f"img_{idx:02}.jpg", is_video=False, card=None, user_image=True)
    _write_json(d / "scenes.json", data)
    return sc


REDO_QUEUE = DATA / "redo_pictures.json"   # [{channel, job, not_before, tries}] - survives restarts


def redo_pictures(job_id):
    """Swap the number cards of a video that ran out of FLUX for AI pictures, then re-render it and its
    cover. Raises while FLUX is still at its limit; pictures already made are kept for the next try."""
    d = job_dir(job_id)
    data = load_scenes(d)
    made = 0
    for i, sc in enumerate(data["scenes"]):
        if sc.get("loop") or not sc.get("card") or not sc.get("prompt"):
            continue
        photo = d / f"img_{i:02}.jpg"
        flux_image(sc["prompt"], photo)
        sc.update(visual=photo.name, card=None, is_video=False)
        made += 1
        _write_json(d / "scenes.json", data)
    rerender(job_id)
    if flag("COVERS", "true"):
        plan = _read_json(d / "plan.json") or {}
        make_cover(d, lines=plan.get("cover_lines") or None)
    return made


def queue_redo(job_id, not_before=None):
    """Redo the pictures later (e.g. after FLUX frees up); app.py's loop works through the queue."""
    q = [x for x in (_read_json(REDO_QUEUE, []) or []) if x["job"] != job_id]
    q.append({"channel": space.current(), "job": job_id, "not_before": not_before or _now(), "tries": 0})
    _write_json(REDO_QUEUE, q)
    return q


def rerender(job_id):
    """Rebuild final.mp4 from the saved scenes (voice, words and pictures are reused as they are)."""
    d = job_dir(job_id)
    data = load_scenes(d)
    job = Job(job_id)
    result = _read_json(d / "result.json") or {}
    token = _job_options.set(job.status.get("options") or {})  # the same look as when it was made
    try:
        job.stage("render", "running", "re-rendering")
        scenes = []
        first = data["scenes"][0]
        for i, sc in enumerate(data["scenes"]):
            if sc.get("loop"):  # follow the hook, whose picture may have been replaced
                sc = {**sc, **{k: first.get(k) for k in ("visual", "is_video", "card")}}
            visual = d / sc["visual"] if sc.get("visual") else None
            card = tuple(sc["card"]) if sc.get("card") else None
            overlay = "" if card else text_overlay(d, i, sc["on_screen"], data["label"], shade=True)
            scenes.append({"text": sc["text"], "audio": d / sc["audio"], "words": sc["words"],
                           "visual": visual, "is_video": sc["is_video"], "card": card, "overlay": overlay,
                           "loop": bool(sc.get("loop"))})
        seconds, _ = render(d, scenes, music_name=result.get("music"))
        job.stage("render", "done", f"{seconds:.1f} s, re-rendered")
        if result:
            result["seconds"] = round(seconds, 1)
            _write_json(d / "result.json", result)
        job.finish()
    except Exception as e:
        job.fail(str(e))
        raise
    finally:
        _job_options.reset(token)


def render(job, scenes, music_name=None):
    """scenes: list of dicts {text, audio, words, visual, is_video, card, overlay}.
    Writes job/final.mp4 and returns (seconds, music file name or None)."""
    seg_list, pcm, timeline = [], bytearray(), []
    t, rate = 0.0, 24000
    for i, s in enumerate(scenes):
        speech = _wav_seconds(s["audio"])
        frames = math.ceil((speech + SCENE_PAD) * FPS)
        length = frames / FPS
        with wave.open(str(s["audio"]), "rb") as w:
            if (w.getnchannels(), w.getsampwidth()) != (1, 2):
                raise PipelineError(f"unexpected audio format in {s['audio']}")
            rate = w.getframerate()
            chunk = w.readframes(w.getnframes())
        # pad every scene's audio to exactly its video length so sound and picture never drift
        pcm += chunk + b"\x00\x00" * max(0, int(round(length * rate)) - len(chunk) // 2)
        out = job / f"seg_{i:02}.mp4"
        if s.get("card"):
            segment_card(job, *s["card"], frames, i, out, loop=s.get("loop", False))
        elif s["is_video"]:
            segment_from_video(s["visual"], frames, out, s.get("overlay", ""))
        else:
            segment_from_image(s["visual"], frames, i, out, s.get("overlay", ""), loop=s.get("loop", False))
        seg_list.append(out.name)
        timeline.append({"text": s["text"], "start": t, "speech": speech, "words": s.get("words")})
        t += length

    pcm = _mix_sfx(pcm, rate, [x["start"] for x in timeline])
    _write_wav(job / "voice.wav", bytes(pcm), rate)
    (job / "segments.txt").write_text("".join(f"file '{n}'\n" for n in seg_list), encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", "segments.txt", "-c", "copy", "video.mp4"], cwd=job)
    write_subtitles(timeline, job / "subs.ass")

    music = music_tracks()
    args = ["-i", "video.mp4", "-i", "voice.wav"]
    pick = None
    choice = music_name or env("MUSIC_TRACK", "auto")
    if music and choice != "none":
        same = [p for p in music if p.name == choice]
        pick = same[0] if same else music[len(_history()) % len(music)]
    vf = "ass=subs.ass" if flag("CAPTIONS", "true") else "null"
    if pick:
        vol = env("MUSIC_VOLUME", "0.12")
        args += ["-stream_loop", "-1", "-i", str(pick), "-filter_complex",
                 f"[0:v]{vf}[v];[2:a]volume={vol}[m];"
                 "[1:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-vf", vf, "-map", "0:v", "-map", "1:a"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-shortest", "-movflags", "+faststart",
             "final.mp4"]
    _ffmpeg(args, cwd=job)
    # the per-scene pieces are only needed to build final.mp4
    for name in seg_list + ["video.mp4"]:
        (job / name).unlink(missing_ok=True)
    make_thumb(job / "final.mp4", job / "thumb.jpg")
    return t, (pick.name if pick else None)


def music_tracks():
    return sorted(p for p in (DATA / "music").glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a"))


def music_credit(name):
    credits = _read_json(DATA / "music" / "credits.json", {}) or {}
    return credits.get(name) if name else None


# ------------------------------------------------------------------- schedule

def youtube_status(mode):
    """Body for videos.update?part=status. The API wipes mutable fields that are left out, so
    all of them are set. mode: private | schedule | public | auto (= AUTO_PUBLISH setting)."""
    if mode == "auto":
        mode = "schedule" if flag("AUTO_PUBLISH") else "private"
    status = {
        "privacyStatus": "public" if mode == "public" else "private",
        "embeddable": True,
        "publicStatsViewable": True,
        "license": "youtube",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": flag("AI_DISCLOSURE", "true"),
    }
    if mode == "schedule":
        tz = ZoneInfo(env("PUBLISH_TIMEZONE", "America/New_York"))
        hh, mm = (int(x) for x in env("PUBLISH_TIME", "18:00").split(":"))
        now = dt.datetime.now(tz)
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now + dt.timedelta(minutes=10) and now - when < dt.timedelta(hours=3):
            # the daily run made the video at publish time: go public now, not tomorrow
            status["privacyStatus"] = "public"
            return status
        if when < now + dt.timedelta(minutes=10):
            when += dt.timedelta(days=1)
        status["publishAt"] = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return status


def publish_data(job_id, mode="auto"):
    """What n8n needs to upload one video: metadata, file URL and the status body."""
    if mode not in ("auto", "private", "schedule", "public"):
        raise PipelineError("mode must be auto, private, schedule or public")
    data = load_job(job_id)
    if data["status"]["state"] != "done" or not data["result"]:
        raise PipelineError("this video is not finished yet")
    job = Job(job_id)
    job.status["youtube"] = {"state": "uploading", "mode": mode, "at": _now()}
    job.stage("youtube", "running", "uploading via n8n")
    return {**data["result"], "job_id": job_id, "mode": mode, "youtube_status": youtube_status(mode),
            "file_url": f"http://worker:8000/files/{job_id}/final.mp4"}


def mark_uploaded(job_id, video_id, mode=None):
    job = Job(job_id)
    job.status["youtube"] = {
        "state": "uploaded", "video_id": video_id, "mode": mode, "at": _now(),
        "url": f"https://youtube.com/shorts/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }
    job.stage("youtube", "done", f"uploaded ({mode or 'private'})")
    return job.status["youtube"]


def mark_youtube_queued(job_id, mode):
    job = Job(job_id)
    job.status["youtube"] = {"state": "queued", "mode": mode, "at": _now()}
    job.stage("youtube", "pending", "sent to n8n")


# ---------------------------------------------------------------------- produce

def produce(topic=None, mock=None, use_video_hook=None, job_id=None, options=None, script=None):
    """One video. options: settings for this video only (the New video dialog), on top of the
    Studio settings; script: the creator's own narration - first line the hook, then one line per scene."""
    mock = flag("MOCK") if mock is None else mock
    use_video = flag("USE_VIDEO_HOOK") if use_video_hook is None else use_video_hook
    options = clean_options(options)
    lines = [x.strip() for x in (script or "").splitlines() if x.strip()]
    if script and len(lines) < 3:
        raise PipelineError("your script needs at least 3 lines: the hook, then one line per scene")
    if lines:
        options["BODY_SCENES"] = str(len(lines) - 1)
    token = _job_options.set(options)
    queued = None
    try:
        # no topic of its own: the next business from the Topics queue (a split story's next part wins)
        if not topic and not lines and not mock and not parts_queue() and flag("TOPIC_QUEUE", "true"):
            import topics
            queued = topics.next_item()
            topic = queued["topic"] if queued else None
        job = Job(job_id or new_job_id(), mock=mock, topic=topic)
        job.status["options"] = options
        if queued:
            job.status["queue_id"] = queued["id"]
        job.save()
        try:
            result = _produce(job, topic, mock, use_video, lines)
        except Exception as e:
            job.fail(str(e))
            import notify
            notify.send("error", f"❌ Video failed: {job.status.get('topic') or 'new video'}\n{str(e)[:400]}\n{notify.link(job.id)}")
            raise
        if queued:
            topics.mark_used(queued["id"], job.id)
        return result
    finally:
        _job_options.reset(token)


def _series_title(title, prefix):
    """Prefix the title once: the model sometimes adds the episode or part tag itself, anywhere."""
    if not prefix:
        return title[:100]
    tag = re.escape(prefix.split("#")[0].split("(")[0].strip())
    title = re.sub(rf"\(?\s*{tag}\s*#\s*\d+\s*\)?\s*:?\s*", " ", title, flags=re.I) if tag else title
    title = re.sub(r"\(?\s*part\s*\d+\s*(/|of)\s*\d+\s*\)?\s*:?\s*", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    return (prefix + " " + title)[:100]


def _guess_on_screen(text):
    """Big on-screen text for a line the creator wrote: its first money figure or number."""
    m = re.search(r"\$\s?\d[\d,.]*\s*(k|K|million|thousand)?(\s*(a|per|/)\s*(month|day|year|week))?|\d[\d,.]*\s*%", text)
    return m.group(0).strip() if m else " ".join(text.split()[:3])


def _apply_script(plan, lines):
    """Put the creator's own lines into the plan word for word. The model only added pictures,
    on-screen text, loop lines and packaging around them."""
    hook, body = lines[0], lines[1:]
    first = plan.hooks[0] if plan.hooks else Hook(formula_id="your-own", text=hook, loop_line="")
    plan.hooks = [Hook(formula_id="your-own", text=hook, loop_line=first.loop_line)] + \
                 [h for h in plan.hooks if h.text.strip() != hook][:4]
    scenes = []
    for i, line in enumerate(body):
        if i < len(plan.scenes):
            s = plan.scenes[i]
            scenes.append(Scene(narration=line, image_prompt=s.image_prompt, stock_query=s.stock_query,
                                on_screen=s.on_screen))
        else:
            scenes.append(Scene(narration=line, image_prompt=line, stock_query=" ".join(line.split()[:3]),
                                on_screen=_guess_on_screen(line)))
    plan.scenes, plan.parts = scenes, []
    return plan


def local_plan(topic, lines):
    """When every Gemini model is down, a creator's own script still becomes a video."""
    hook, body = lines[0], lines[1:]
    return Plan(topic=topic or hook, hooks=[Hook(formula_id="your-own", text=hook, loop_line="")],
                hook_visual=hook, hook_stock_query=" ".join(hook.split()[:3]),
                hook_on_screen=_guess_on_screen(hook),
                scenes=[Scene(narration=x, image_prompt=x, stock_query=" ".join(x.split()[:3]),
                              on_screen=_guess_on_screen(x)) for x in body],
                parts=[], titles=[topic or hook[:60]], description=topic or hook, tags=[])


def _labels(episode, k, total):
    label = env("SERIES_LABEL").format(n=episode) if env("SERIES_LABEL") else ""
    prefix = env("SERIES_TITLE_PREFIX").format(n=episode) if env("SERIES_TITLE_PREFIX") else ""
    if total > 1:
        label = f"{label} · PART {k}/{total}".strip(" ·")
        prefix = f"{prefix.rstrip(':')} (Part {k}/{total}):" if prefix else f"Part {k}/{total}:"
    return label, prefix


def _produce(job, topic, mock, use_video, lines):
    d = job.dir
    history = _history()
    client = None if mock else _client()
    # an unfinished split story goes first, unless the user asked for a specific topic or wrote a script
    part = None if topic or mock or lines else parts_queue()
    episode = part["episode"] if part else episode_number()
    if part:
        topic = part["topic"]

    job.stage("script", "running", f"writing part {part['next']}/{part['total']}" if part
              else "adding pictures to your script" if lines else "writing the script")
    if lines:
        try:
            plan = mock_plan(topic) if mock else make_plan(client, topic, history, episode, script=lines)
        except PipelineError as e:  # the creator's words are enough to make the video
            print(f"[warn] no model for the packaging ({e}); using your lines as they are", flush=True)
            plan = local_plan(topic, lines)
        plan = _apply_script(plan, lines)
    else:
        plan = mock_plan(topic) if mock else make_plan(client, topic, history, episode, part)
    (d / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    if not plan.hooks or not plan.scenes or not plan.titles:
        raise PipelineError("the model returned an empty plan - see plan.json")
    if not mock and not lines:
        plan = improve_hooks(job, client, plan)
    job.stage("script", "done", f"{len(plan.hooks)} hooks, {len(plan.scenes)} scenes")
    if mock:
        job.stage("check", "skipped", "test mode")
    else:
        plan = fact_check(job, client, plan, own_script=bool(lines))
        plan = prompt_check(job, client, plan)
    hook, hook_score, hook_scores = pick_hook(d, plan.hooks, plan.topic)
    if lines:
        hook = lines[0]  # the creator's own hook, scored only for information
        hook_score = next((h for h in hook_scores if h.get("hook", "").strip() == hook), None)
    title, title_score, title_scores = pick_title(d, plan.titles)
    _write_json(d / "scores.json", {"hooks": hook_scores, "titles": title_scores})
    outline = [x.strip() for x in plan.parts if x.strip()][:max_parts()]
    if part:
        k, total = part["next"], part["total"]
    elif len(outline) > 1:
        k, total = 1, len(outline)
    else:
        k, total = 1, 1
    label, prefix = _labels(episode, k, total)
    job.status["topic"] = plan.topic if not part else f"{part['topic']} (part {k}/{total})"
    chosen = next((h for h in plan.hooks if h.text.replace("\n", " ").strip() == hook.strip()), plan.hooks[0])
    ctx = {"plan": plan, "client": client, "mock": mock, "use_video": use_video, "episode": episode,
           "k": k, "total": total, "part": part, "outline": outline, "label": label,
           "hook_score": hook_score, "title_score": title_score}
    return _build(job, ctx, hook, chosen.loop_line, _series_title(title, prefix))


def _build(job, ctx, hook, loop_line, title, variant=None, variant_of=None, music=None, image_dir=None):
    """Voice, pictures and edit for one version of a plan. Variants call this again with another
    hook, title, voice, music and pictures."""
    d, plan, client, mock = job.dir, ctx["plan"], ctx["client"], ctx["mock"]
    episode, k, total, part, label = ctx["episode"], ctx["k"], ctx["total"], ctx["part"], ctx["label"]
    loop_line = (loop_line or "").strip() if flag("LOOP_ENDING", "true") else ""
    job.stage("script", "done", f"{len(plan.hooks)} hooks, {len(plan.scenes) + 1 + bool(loop_line)} scenes"
              + (f", part {k}/{total}" if total > 1 else "") + (f", variant {variant}" if variant else ""))

    scenes_in = [{"text": hook, "prompt": plan.hook_visual, "query": plan.hook_stock_query,
                  "on_screen": plan.hook_on_screen}] + \
                [{"text": s.narration, "prompt": s.image_prompt, "query": s.stock_query,
                  "on_screen": s.on_screen} for s in plan.scenes]
    if loop_line:
        # only the loop line may stay open; a payoff that also ends on "because" would read
        # "..., because and you can skip...", so close it with a full stop
        scenes_in[-1]["text"] = close_sentence(scenes_in[-1]["text"])
        # the closing line runs over the hook's own picture and leads back into the hook on replay
        scenes_in.append({"text": loop_line, "prompt": plan.hook_visual, "query": plan.hook_stock_query,
                          "on_screen": plan.hook_on_screen, "loop": True})
    n = len(scenes_in)

    job.stage("voice", "running", f"0/{n}")
    words = []
    for i, s in enumerate(scenes_in):
        words.append(voice_over(client, s["text"], d / f"voice_{i:02}.wav", mock))
        job.stage("voice", "running", f"{i + 1}/{n}")
    job.stage("voice", "done", f"{n} clips, {env('TTS_PROVIDER', 'edge')}"
              + (f" · {env('EDGE_VOICE').split('-')[-1].replace('Neural', '')}" if env("TTS_PROVIDER", "edge") == "edge" else ""))

    provider = visual_provider()
    job.stage("visuals", "running", f"0/{n} · {provider}")
    scenes, used, credits = [], set(), []
    count = {"images": 0, "stock": 0, "cards": 0, "test": 0}
    video_s, source_down, flux_down = 0.0, False, False
    for i, s in enumerate(scenes_in):
        entry = {"text": s["text"], "audio": d / f"voice_{i:02}.wav", "words": words[i],
                 "visual": None, "is_video": False, "card": None, "overlay": "", "loop": bool(s.get("loop"))}
        clip = d / "hook.mp4"
        reuse = image_dir / f"img_{i:02}.jpg" if image_dir else None
        if s.get("loop"):
            first = scenes[0]
            entry.update(visual=first["visual"], is_video=first["is_video"], card=first["card"],
                         overlay="" if first["card"] else text_overlay(d, i, s["on_screen"], label, shade=True))
        elif reuse and reuse.exists():
            shutil.copy(reuse, d / reuse.name)
            count["reused pictures"] = count.get("reused pictures", 0) + 1
            entry.update(visual=d / reuse.name, overlay=text_overlay(d, i, s["on_screen"], label, shade=True))
        elif i == 0 and ctx["use_video"] and not mock and hook_video(client, s["prompt"], clip):
            entry.update(visual=clip, is_video=True)
            video_s = _probe_seconds(clip) or 8.0
        elif provider == "pexels":
            clip = d / f"clip_{i:02}.mp4"
            try:
                credit = pexels_clip(s["query"], clip, used)
            except Exception as e:  # network or API trouble: this scene becomes a card
                print(f"[warn] pexels failed for '{s['query']}': {e}", flush=True)
                credit = None
            if credit:
                credits.append(credit)
                count["stock"] += 1
                entry.update(visual=clip, is_video=True,
                             overlay=text_overlay(d, i, s["on_screen"], label, shade=True))
            else:
                count["cards"] += 1
                entry["card"] = (s["on_screen"], label)
        elif provider in ("flux", "openverse"):
            # flux -> openverse photo -> number card: each step only when the previous one fails
            photo = d / f"img_{i:02}.jpg"
            got = False
            if provider == "flux" and not flux_down:
                try:
                    flux_image(s["prompt"], photo)
                    count["ai images"] = count.get("ai images", 0) + 1
                    got = True
                except Exception as e:  # daily free limit or outage: stop asking for this video
                    print(f"[warn] flux unavailable ({e}); trying photos instead", flush=True)
                    flux_down = True
                    import notify
                    notify.once("flux", "limit", "⚠ FLUX pictures stopped working (free limit of 10,000 neurons "
                                "a day, about 130 pictures, or a Cloudflare outage). Videos use photos or number "
                                "cards until it resets at 00:00 UTC - in practice up to about an hour later."
                                f"\nDetails: {str(e)[:200]}")
            if not got and not source_down:
                try:
                    credit = openverse_image(s["query"], photo, used)
                except Exception as e:  # throttled or down: stop asking for the rest of this video
                    print(f"[warn] openverse unavailable ({e}); remaining scenes use cards", flush=True)
                    source_down, credit = True, None
                if credit:
                    credits.append(credit)
                    count["photos"] = count.get("photos", 0) + 1
                    got = True
            if got:
                entry.update(visual=photo, overlay=text_overlay(d, i, s["on_screen"], label, shade=True))
            else:
                count["cards"] += 1
                entry["card"] = (s["on_screen"], label)
        elif provider == "gemini" and mock:
            entry["visual"] = d / f"img_{i:02}.png"
            mock_image(i, entry["visual"])
            count["test"] += 1
        elif provider == "gemini":
            entry["visual"] = d / f"img_{i:02}.png"
            try:
                image(client, s["prompt"], entry["visual"])
                count["images"] += 1
            except PipelineError:
                # one blocked picture should not cost the whole video: show the key fact instead
                print(f"[warn] image {i} failed, using a card instead", flush=True)
                entry.update(visual=None, card=(s["on_screen"], label))
                count["cards"] += 1
        else:
            entry["card"] = (s["on_screen"], label)
            count["cards"] += 1
        scenes.append(entry)
        job.stage("visuals", "running", f"{i + 1}/{n} · {provider}")
    summary = ", ".join(f"{v} {k}" for k, v in count.items() if v) + (", 1 AI video" if video_s else "")
    job.stage("visuals", "done", summary)

    save_scenes(d, scenes, scenes_in, label, credits)
    job.stage("render", "running", "editing")
    seconds, music = render(d, scenes, music_name=music)
    job.stage("render", "done", f"{seconds:.1f} s, 1080x1920")
    cover = None
    if flag("COVERS", "true"):
        try:
            cover = make_cover(d, lines=plan.cover_lines or None)
        except Exception as e:  # a missing cover never costs the video
            print(f"[warn] cover failed: {e}", flush=True)

    extra = [env("DESCRIPTION_CTA"), env("DESCRIPTION_FOOTER"), music_credit(music), *credits]
    description = "\n\n".join([plan.description.strip(), "\n".join(x for x in extra if x)]).strip()
    tags = [t.replace(",", " ").strip() for t in plan.tags if t.strip()]
    result = {
        "job_id": job.id,
        "mock": mock,
        "topic": plan.topic,
        "episode": episode,
        "part": k,
        "parts": total,
        "variant": variant,
        "variant_of": variant_of,
        "voice": env("EDGE_VOICE") if env("TTS_PROVIDER", "edge") == "edge" else env("TTS_VOICE", "Puck"),
        "loop_line": loop_line or None,
        "title": title,
        "description": description[:4900],
        "music": music,
        "visuals": provider,
        "tags": ",".join(tags)[:450],
        "category_id": env("YOUTUBE_CATEGORY_ID", "27"),
        "region_code": env("YOUTUBE_REGION", "US"),
        "language": env("CHANNEL_LANGUAGE", "en"),
        "hook": hook,
        "hook_formula": next((h.formula_id for h in plan.hooks
                              if " ".join(h.text.split()) == " ".join(hook.split())), "your-own"),
        "cover": cover,
        "hook_score": ctx["hook_score"] if not variant else None,
        "title_score": ctx["title_score"] if not variant else None,
        "seconds": round(seconds, 1),
        "estimated_cost_usd": 0.0 if mock else round(
            count["images"] * IMAGE_PRICE + video_s * VIDEO_PRICE_PER_S + (0 if variant else SCRIPT_PRICE), 2),
        "file_url": f"http://worker:8000/files/{job.id}/final.mp4",
    }
    if not mock:
        history = _history()
        history.append({"date": dt.date.today().isoformat(), "topic": part["topic"] if part else plan.topic,
                        "title": title, "job": job.id, "episode": episode, "part": k, "parts": total,
                        **({"variant": variant} if variant else {})})
        _write_json(HISTORY.resolve(), history)
        if not variant:
            # remember what is left of a split story: the next run (dashboard or n8n) makes the next part
            said = " ".join([hook] + [s.narration for s in plan.scenes])
            if k < total:
                _write_json(PARTS_FILE, {
                    "episode": episode, "topic": part["topic"] if part else plan.topic,
                    "outline": part["outline"] if part else ctx["outline"], "total": total, "next": k + 1,
                    "said": (part["said"] if part else []) + [said]})
            elif part:
                PARTS_FILE.unlink(missing_ok=True)
    result["next_part"] = k < total and not mock and not variant
    _write_json(d / "result.json", result)
    job.finish()
    if not variant and not mock:
        import notify
        check = _read_json(d / "factcheck.json") or {}
        doubts = len(check.get("issues") or []) - len(check.get("applied") or [])
        notify.send("done", f"✅ Ready: {title} ({seconds:.0f} s)"
                            + (f"\n⚠ {doubts} doubtful figure(s) — check the Script tab before posting" if doubts > 0 else "")
                            + f"\n{notify.link(job.id)}")
    return result


# ---------------------------------------------------------------------- variants

VARIANT_COLOURS = ["#00E5FF", "#7CFF4F", "#FF9E2C", "#FF5CA8", "#FFF200"]


def make_variants(base_id, count, on_start=None):
    """count more versions of a finished video for A/B testing: same topic and body, but another
    hook (next best by hookscore, with its own loop line), another title, voice, music, caption
    colour and - unless VARIANT_NEW_IMAGES is off - freshly generated pictures. These are real
    differences a viewer notices, not a disguised copy of the same file."""
    base = job_dir(base_id)
    plan = Plan.model_validate_json((base / "plan.json").read_text(encoding="utf-8"))
    result = _read_json(base / "result.json") or {}
    status = _read_json(base / "status.json") or {}
    scores = _read_json(base / "scores.json") or {}
    base_options = status.get("options") or {}
    count = max(0, min(4, int(count)))

    by_text = {h.text.replace("\n", " ").strip(): h for h in plan.hooks}
    ranked = [by_text[r["hook"].strip()] for r in sorted(scores.get("hooks") or [], key=lambda r: -r["verdict"])
              if r.get("hook", "").strip() in by_text] or list(plan.hooks)
    others = [h for h in ranked if h.text.strip() != (result.get("hook") or "").strip()] or ranked
    titles = [r["title"] for r in sorted(scores.get("titles") or [], key=lambda r: -r["score"])] or plan.titles
    titles = [t for t in titles if t.strip()]

    token = _job_options.set(base_options)
    try:
        voices = [v.strip() for v in env("VARIANT_VOICES").split(",") if v.strip() and v.strip() != result.get("voice")]
        tracks = [p.name for p in music_tracks()]
        mock = bool(result.get("mock"))
        client = None if mock else _client()
        k, total, episode = result.get("part") or 1, result.get("parts") or 1, result.get("episode") or episode_number()
        label, prefix = _labels(episode, k, total)
    finally:
        _job_options.reset(token)

    # pressing "Make versions" again continues after the versions that already exist (C, D, ...)
    siblings = sum(1 for p in JOBS.iterdir() if (_read_json(p / "status.json") or {}).get("variant_of") == base_id)
    made = []
    for v in range(siblings + 1, siblings + count + 1):
        letter = "BCDEFGHIJKLMNOPQRSTUVWXYZ"[min(v - 1, 24)]
        hook = others[(v - 1) % len(others)]
        opts = dict(base_options)
        if voices:
            opts["EDGE_VOICE"] = voices[(v - 1) % len(voices)]
        opts["CAPTION_HIGHLIGHT"] = VARIANT_COLOURS[(v - 1) % len(VARIANT_COLOURS)]
        music = None
        if tracks and result.get("music") in tracks:
            music = tracks[(tracks.index(result["music"]) + v) % len(tracks)]
        job = Job(new_job_id(), mock=mock, topic=f"{result.get('topic') or plan.topic} (variant {letter})")
        job.status["options"] = opts
        job.status["variant_of"] = base_id
        job.save()
        job.stage("check", "skipped", "same facts as version A")
        if on_start:
            on_start(job.id)
        (job.dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        _write_json(job.dir / "scores.json", scores)
        token = _job_options.set(opts)
        try:
            ctx = {"plan": plan, "client": client, "mock": mock, "use_video": False, "episode": episode,
                   "k": k, "total": total, "part": None, "outline": [], "label": label,
                   "hook_score": None, "title_score": None}
            made.append(_build(job, ctx, hook.text.replace("\n", " ").strip(), hook.loop_line,
                               _series_title(titles[v % len(titles)] if titles else plan.topic, prefix),
                               variant=letter, variant_of=base_id, music=music,
                               image_dir=None if flag("VARIANT_NEW_IMAGES", "true") else base))
        except Exception as e:
            job.fail(str(e))
            print(f"[warn] variant {letter} failed: {e}", flush=True)
            import notify
            notify.send("error", f"❌ Version {letter} of “{result.get('title')}” failed: {str(e)[:300]}\n{notify.link(job.id)}")
        finally:
            _job_options.reset(token)
    # the original becomes variant A once it has siblings
    if made and not result.get("variant"):
        result["variant"] = "A"
        _write_json(base / "result.json", result)
    if made and not mock:
        import notify
        notify.send("done", f"✅ {len(made)} more version(s) of “{result.get('title')}” ready: "
                            + ", ".join(m["variant"] for m in made) + f"\n{notify.link(base_id)}")
    return made


# ---------------------------------------------------------------------- upload

def create_upload(stream, filename, title, description="", tags=""):
    """A finished video the user made elsewhere, so it can go through the same YouTube step."""
    ext = Path(filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise PipelineError("upload an .mp4, .mov, .webm, .mkv or .m4v file")
    job = Job(new_job_id(), kind="uploaded", topic=title)
    d = job.dir
    try:
        for key in ("script", "voice", "visuals"):
            job.stage(key, "skipped", "uploaded video")
        job.stage("render", "running", "saving the file")
        src = d / f"upload{ext}"
        with open(src, "wb") as f:
            shutil.copyfileobj(stream, f, length=1024 * 1024)
        if ext == ".mp4":
            src.replace(d / "final.mp4")
        else:
            _ffmpeg(["-i", src.name, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "final.mp4"], cwd=d)
            src.unlink()
        seconds = _probe_seconds(d / "final.mp4")
        if seconds <= 0:
            raise PipelineError("this file does not look like a video")
        make_thumb(d / "final.mp4", d / "thumb.jpg")
        job.stage("render", "done", f"{seconds:.1f} s, uploaded")
        result = {
            "job_id": job.id, "mock": False, "topic": title, "title": (title or "Untitled").strip()[:100],
            "description": (description or "").strip(), "tags": tags or "",
            "category_id": env("YOUTUBE_CATEGORY_ID", "27"), "region_code": env("YOUTUBE_REGION", "US"),
            "language": env("CHANNEL_LANGUAGE", "en"), "hook": None, "seconds": round(seconds, 1),
            "estimated_cost_usd": 0.0, "file_url": f"http://worker:8000/files/{job.id}/final.mp4",
        }
        _write_json(d / "result.json", result)
        job.finish()
        return result
    except Exception as e:
        job.fail(str(e))
        raise


# --------------------------------------------------------------- studio settings

def settings_view():
    """The Settings form: every field with its current value and where that value comes from."""
    studio = studio_settings()
    out = []
    for s in SETTINGS_SCHEMA:
        item = dict(s)
        k = s["key"]
        env_value = os.environ.get(k, "").strip()
        if k in studio:
            item.update(value=studio[k], source="studio")
        elif env_value:
            item.update(value=env_value, source=".env")
        else:
            item.update(value=s.get("default", ""), source="default")
        item["env_value"] = env_value or s.get("default", "")
        if k == "MUSIC_TRACK":
            item["options"] = ["auto", "none"] + [p.name for p in music_tracks()]
        out.append(item)
    return out


_voices_cache = []


def list_voices():
    """All edge-tts voices (name, gender, locale). Fetched once from Microsoft, then cached."""
    if not _voices_cache:
        import edge_tts
        voices = asyncio.run(edge_tts.list_voices())
        _voices_cache.extend({"name": v["ShortName"], "gender": v.get("Gender"), "locale": v.get("Locale"),
                              "label": v.get("FriendlyName", v["ShortName"])} for v in voices)
    return _voices_cache


def voice_preview(text, options):
    """A few seconds of a voice with unsaved settings, for the Settings page."""
    out = DATA / "previews"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "voice.wav"
    token = _job_options.set(clean_options(options))
    try:
        tts_edge((text or "Here's the math. A used van costs about eight thousand dollars.")[:300], path)
    finally:
        _job_options.reset(token)
    return path


def caption_preview(options):
    """One 1080x1920 frame with the caption and on-screen text style, for the Settings page."""
    out = DATA / "previews"
    out.mkdir(parents=True, exist_ok=True)
    token = _job_options.set(clean_options(options))
    try:
        sample = [{"text": "Here's the math, about $8K to start.", "start": 0.0, "speech": 2.0,
                   "words": [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.3} for i, w in
                             enumerate("Here's the math, about $8K to start.".split())]}]
        write_subtitles(sample, out / "subs.ass")
        bg = next((p for p in sorted(JOBS.glob("*/img_01.jpg"), reverse=True)), None)
        src = ["-loop", "1", "-i", str(bg)] if bg else ["-f", "lavfi", "-i", f"color=c=0x203040:s={W}x{H}"]
        overlay = text_overlay(out, 0, "Startup: $8K", env("SERIES_LABEL", "").format(n=6), shade=True)
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
              + (f",{overlay}" if overlay else "") + (",ass=subs.ass" if flag("CAPTIONS", "true") else ""))
        _ffmpeg([*src, "-ss", "0.95", "-t", "1", "-vf", vf, "-frames:v", "1", "-update", "1", "preview.jpg"], cwd=out)
    finally:
        _job_options.reset(token)
    return out / "preview.jpg"


def health():
    try:
        ff = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        ffmpeg = ff.stdout.split("\n", 1)[0] if ff.returncode == 0 else None
    except FileNotFoundError:
        ffmpeg = None
    provider = env("TTS_PROVIDER", "edge")
    return {
        "ok": ffmpeg is not None,
        "ffmpeg": ffmpeg,
        "gemini_key": bool(env("GEMINI_API_KEY")),
        "mock": flag("MOCK"),
        "tts_provider": provider,
        "voice": env("EDGE_VOICE", "en-US-AndrewMultilingualNeural") if provider == "edge"
        else env("TTS_VOICE", "Puck"),
        "language": env("CHANNEL_LANGUAGE", "en"),
        "niche": env("CHANNEL_NICHE", "surprising science facts"),
        "niche_label": niche()["label"],
        "visuals": visual_provider(),
        "series": env("SERIES_NAME"),
        "next_episode": episode_number(),
        "next_part": (lambda q: q and f"Part {q['next']}/{q['total']} of “{q['topic']}”")(parts_queue()),
        "next_topic": (lambda t: t and t["topic"])(
            __import__("topics").next_item() if flag("TOPIC_QUEUE", "true") else None),
        "loop_ending": flag("LOOP_ENDING", "true"),
        "max_parts": max_parts(),
        "use_video_hook": flag("USE_VIDEO_HOOK"),
        "auto_publish": flag("AUTO_PUBLISH"),
        "publish": f"{env('PUBLISH_TIME', '18:00')} {env('PUBLISH_TIMEZONE', 'America/New_York')}",
        "skills_found": (SKILLS / "yt-script" / "hooks.json").exists(),
        "voice_profile_filled": bool(_voice_profile()),
        "videos_made": len(_history()),
    }
