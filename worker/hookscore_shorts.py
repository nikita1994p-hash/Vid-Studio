"""Hook scorer tuned for this channel: money-business YouTube Shorts with a synthetic voice.

Same idea as the yt-script skill's hookscore.py (five properties 0-100, a verdict that also weighs
the weakest one), with five changes for this format:
- numbers count when spelled out ('four thousand dollars'), since the voice script spells them;
- STAKES counts money won as well as money lost: 'makes $4K a month' is the stake in this niche;
- ADDRESS ('you' in the first words) becomes FRONT: the first five words must carry the hook -
  a number, the business, or 'you' all work, so 'This van makes $4K a month' is not punished;
- the length band is 8-18 words (a Shorts hook is spoken in ~4 s, not 10);
- the weakest property weighs 30%, not 40%, so one structurally absent property (a statistic hook
  has no 'you') caps a hook at WORKABLE instead of sinking it.
Like the original it is a lint, not a prediction: a low score is a reason to look again.

kind='health' (NICHE_KIND) swaps money for the body: a figure with a unit (minutes, reps, hours of
sleep, steps) counts as specific, and the stake is what the viewer gains or loses - pain, strength,
sleep, energy, years.
"""
import json
import os
import re
from pathlib import Path

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}


def digits(text):
    """'four thousand dollars a month' -> '$4,000 a month', 'seventy percent' -> '70%'."""
    tokens = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    out, i = [], 0
    while i < len(tokens):
        low = tokens[i].lower()
        if low not in _ONES and low not in _TENS:
            out.append(tokens[i])
            i += 1
            continue
        total = cur = 0
        j = last = i
        while j < len(tokens):
            w = tokens[j].lower()
            if w in _ONES or w in _TENS:
                cur += _ONES.get(w, 0) + _TENS.get(w, 0)
                last = j
            elif w == "hundred":
                cur = max(cur, 1) * 100
                last = j
            elif w in _SCALES:
                total += max(cur, 1) * _SCALES[w]
                cur = 0
                last = j
            elif not (w == "and" or re.fullmatch(r"[ -]+", tokens[j])):
                break
            j += 1
        j = last + 1   # the separators / 'and' after the last number word stay as they were
        value = f"{total + cur:,}"
        rest = "".join(tokens[j:j + 2]).lower()
        if re.match(r"\s+(dollars?|bucks)\b", rest):
            value, j = "$" + value, j + 2
        elif re.match(r"\s+percent\b", rest):
            value, j = value + "%", j + 2
        out.append(value)
        i = j
    return "".join(out)


FILLER = {"basically", "actually", "literally", "just", "really", "very", "kind", "sort", "guys", "hey",
          "welcome", "today", "video", "subscribe", "channel"}
VAGUE = {"amazing", "incredible", "insane", "crazy", "huge", "massive", "secret", "powerful", "ultimate",
         "revolutionary", "unbelievable", "awesome", "passive", "easy"}
STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "with", "your", "my", "business", "service",
        "services", "rental", "rentals", "company", "shop"}
NUMBER = re.compile(r"\$?\d[\d,.]*\s?(%|k\b|m\b|x\b)?", re.I)
MONEY = re.compile(r"\$\d|\b\d[\d,.]*\s?(k|m)?\s?(dollars?|bucks)\b", re.I)
RATE = re.compile(r"(\$\d[\d,.]*\s?k?|\d[\d,.]*\s?k?\s?dollars?)\s*(a|per|an|each|every)\s+"
                  r"(month|day|week|year|hour|car|job|house|lawn|load|visit|client|customer|weekend)\b", re.I)
WIN = re.compile(r"\b(makes?|making|made|earns?|earning|profit|profits|pays?|paid|clears?|nets?|charges?|"
                 r"brings? in|revenue|income|cash|money)\b", re.I)
LOSS = re.compile(r"\b(lose|loses|lost|losing|waste|wasting|wasted|costs?|risk|risking|broke|quit|stop|never|"
                  r"fail|fails|failing|mistake|debt|rent|before|dead|dies|scam)\b", re.I)
GAP = re.compile(r"\b(why|how|what|which|but|until|nobody|except|instead|without|only|catch|most people|"
                 r"nobody tells|here's|here is)\b", re.I)
CLOSED = re.compile(r"\b(because|so that|which means)\b", re.I)
UNIT = re.compile(r"\b\d[\d,.]*\s?(%|minutes?|mins?|seconds?|hours?|days?|weeks?|reps?|sets?|steps?|"
                  r"grams?|calories|pounds?|kg|glasses|times|years?)\b", re.I)
BODY_RATE = re.compile(r"\b\d[\d,.]*\s?(minutes?|reps?|sets?|steps?|hours?|glasses|times|grams?)\s*(a|per|each|every)\s+"
                       r"(day|week|night|morning|session|meal|hour)\b", re.I)
BODY_WIN = re.compile(r"\b(stronger|faster|burns?|build|builds|lose|loses|fat|muscle|strength|energy|sleep|"
                      r"younger|longer|fixes|relieves?|heals?|flexib\w*|posture|heart|lungs|focus|mood)\b", re.I)
BODY_LOSS = re.compile(r"\b(pain|hurts?|injur\w*|ruin\w*|damage\w*|mistake|wrong|stop|never|risk|worse|aging|"
                       r"belly|stiff|weak|tired|exhausted|wasting|before|dangerous)\b", re.I)
YOU = re.compile(r"\b(you|your|you're|yourself)\b", re.I)
WEAK_OPEN = re.compile(r"^(so|hey|hi|today|in this video|welcome|let's talk|let me)\b", re.I)


def _words(t):
    return re.findall(r"[a-z0-9'%$.,]+", t.lower())


def _topic_words(topic):
    return {w for w in re.findall(r"[a-z]+", (topic or "").lower()) if w not in STOP and len(w) > 2}


def specificity(t, topic_words, kind="business"):
    w = _words(t)
    if not w:
        return 0
    nums = len(NUMBER.findall(t))
    s = 30 + 25 * min(nums, 2) + (10 if (UNIT if kind == "health" else MONEY).search(t) else 0)
    s += 10 if topic_words & set(re.findall(r"[a-z]+", t.lower())) else 0
    s -= 15 * sum(x in VAGUE for x in w) + 5 * sum(x in FILLER for x in w)
    return max(0, min(100, s))


def stakes(t, topic_words=None, kind="business"):
    if kind == "health":
        hits = len(BODY_WIN.findall(t)) + len(BODY_LOSS.findall(t))
        return max(0, min(100, 25 + 25 * min(hits, 3) + (15 if BODY_RATE.search(t) else 0)
                          + (10 if UNIT.search(t) else 0)))
    hits = len(WIN.findall(t)) + len(LOSS.findall(t))
    return max(0, min(100, 25 + 25 * min(hits, 3) + (15 if RATE.search(t) else 0) + (10 if MONEY.search(t) else 0)))


def curiosity(t, topic_words=None, kind=None):
    n = len(GAP.findall(t))
    s = 25 + 18 * min(n, 3) + (15 if t.strip().endswith("?") else 0) - (25 if CLOSED.search(t) else 0)
    return max(0, min(100, s))


def front(t, topic_words, kind=None):
    first = " ".join(t.split()[:5])
    if WEAK_OPEN.search(t.strip()):
        return 10
    s = 20
    s += 35 if NUMBER.search(first) else 0
    s += 30 if YOU.search(first) else 0
    s += 30 if topic_words & set(re.findall(r"[a-z]+", first.lower())) else 0
    s += 20 if re.match(r"^(why|how|what|this|your|stop|never|nobody|most)\b", t.strip(), re.I) else 0
    return max(0, min(100, s))


def brevity(t, topic_words=None, kind=None):
    n = len(t.split())
    if n == 0:
        return 0
    if 8 <= n <= 18:
        return 100
    return max(30, 100 - (8 - n) * 10) if n < 8 else max(10, 100 - (n - 18) * 8)


PROPS = [("SPECIFICITY", specificity), ("STAKES", stakes), ("CURIOSITY", curiosity),
         ("FRONT", front), ("BREVITY", brevity)]

FIX = {
    "SPECIFICITY": "put a concrete figure from the body in it (startup cost, price per job, profit a month) and name the business",
    "STAKES": "say what is won or lost: what it makes a month, what it costs, what the viewer wastes or risks",
    "CURIOSITY": "open a gap the hook does not close: why / how / but / until / the catch, or end on a question",
    "FRONT": "make the first five words carry it: the number, the business or 'you' - never a warm-up",
    "BREVITY": "8 to 18 words",
}

DESCRIBE = {
    "SPECIFICITY": "a concrete figure (spelled-out numbers count) and the business named",
    "STAKES": "money won or lost: makes, earns, profit, per month, costs, waste, risk, lose, mistake",
    "CURIOSITY": "an open gap: why, how, what, but, until, nobody, the catch, or a question mark; never 'because'",
    "FRONT": "the first five words hold a number, the business or 'you'; no 'So / Hey / Today' opener",
    "BREVITY": "8-18 words",
}


FIX_HEALTH = {**FIX,
              "SPECIFICITY": "put a concrete figure from the body in it (minutes, reps, hours, steps) and name the "
                             "exercise or habit",
              "STAKES": "say what the viewer gains or loses: pain, strength, sleep, energy - and how fast",
              "FRONT": "make the first five words carry it: the number, the exercise or habit, or 'you'"}
DESCRIBE_HEALTH = {**DESCRIBE,
                   "SPECIFICITY": "a concrete figure with a unit (minutes, reps, hours, steps) and the topic named",
                   "STAKES": "what the body gains or loses: pain, strength, sleep, energy, injury, mistake, never",
                   "FRONT": "the first five words hold a number, the exercise or habit, or 'you'; no 'So / Hey / "
                            "Today' opener"}


def fix(kind="business"):
    return FIX_HEALTH if kind == "health" else FIX


def describe(kind="business"):
    return DESCRIBE_HEALTH if kind == "health" else DESCRIBE


def _formulas():
    path = Path(os.environ.get("SKILLS_MOUNT", "/skills")) / "yt-script" / "hooks.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["hooks"]
    except Exception:
        return []


def band(v):
    return "STRONG" if v >= 70 else "WORKABLE" if v >= 55 else "WEAK"


def score(text, topic="", kind="business"):
    t = digits(text.replace("\n", " ").strip())
    tw = _topic_words(topic)
    parts = {name: fn(t, tw, kind) for name, fn in PROPS}
    vals = list(parts.values())
    verdict = round(0.7 * sum(vals) / len(vals) + 0.3 * min(vals))
    best, hits = "Unclassified", 0
    for f in _formulas():
        n = sum(1 for p in f.get("match", []) if re.search(p, t, re.I))
        if n > hits:
            best, hits = f["name"], n
    return {"hook": text.replace("\n", " ").strip(), "scored_as": t, "properties": parts, "verdict": verdict,
            "band": band(verdict), "formula": best, "weakest": min(parts, key=parts.get)}
