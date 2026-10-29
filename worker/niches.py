"""Niche kinds and channel templates.

A kind (NICHE_KIND setting) is the wording the prompts and checks use: what one episode is about,
which figures matter, what the fact checker and the topic check look for.
A template is what a new channel starts with: its settings, script rules and starter topics.
"""
from pathlib import Path

HERE = Path(__file__).parent

KINDS = {
    "business": {
        "label": "Business / money",
        "subject": "business",
        "figures": "money figures (startup cost, price, profit)",
        "on_screen": "'$300/day', 'Startup: $12K', 'Profit: $4K/mo'",
        "on_screen_rule": "writes numbers as digits with $, K and /",
        "cover": "the money figure from the script as digits (e.g. '$4K/MONTH', '$300/DAY'), then the business",
        "fact_focus": [
            "Check every money figure (startup cost, prices, profit, income), every time span, quantity and",
            "factual claim against what is typical for this business in that market. Also check that the same",
            "thing never gets two different numbers (hook vs scenes vs on-screen text vs cover), and that profit",
            "is not confused with revenue.",
        ],
        "fact_fix": "a realistic figure",
        "hook_example": "'Why do you pay shop rent when an eight thousand dollar van detailing cars clears four "
                        "thousand a month?'",
        "hook_stakes": "(per month, per job, what it costs)",
        "viewer_framing": "paying rent, a day job, free weekends, savings at risk",
        "topic_item": "one small business",
        "topic_ok": [
            "- ok: a real business people actually run in that market, legal, honestly able to make money,",
            "  spelled correctly. Needing a license, permits, insurance, a lease or a big budget is FINE -",
            "  the video covers the startup cost and the catch;",
        ],
        "topic_doubt": "not a real business, illegal, a scam or get-rich-quick scheme, a franchise/brand, too vague "
                       "to film (e.g. 'Online business')",
    },
    "health": {
        "label": "Sport & health",
        "subject": "topic",
        "figures": "the key numbers (minutes, reps, sets, days, hours of sleep, grams, steps)",
        "on_screen": "'10 min/day', '3 x 12 reps', '7 hours sleep', '8,000 steps'",
        "on_screen_rule": "writes numbers as digits with units and /",
        "cover": "the key number from the script as digits with its unit (e.g. '10 MIN/DAY', '3 X 12'), then the "
                 "topic",
        "fact_focus": [
            "Check every health and fitness number and claim (durations, reps, doses, calories, sleep hours,",
            "study results) against current scientific consensus (WHO, NIH, CDC, major reviews). Flag as HIGH:",
            "any promise to cure, treat or prevent a disease; a diagnosis; medical or supplement dosing advice;",
            "extreme diets or fasting; exercise advice that is unsafe without a warning; invented studies or",
            "percentages; 'doctors hate this' style claims. Also check that the same thing never gets two",
            "different numbers (hook vs scenes vs on-screen text vs cover).",
        ],
        "fact_fix": "a figure and wording that match the scientific consensus",
        "hook_example": "'You walk eight thousand steps a day, so why does your back still hurt by three o'clock?'",
        "hook_stakes": "(what the viewer gains or loses: pain, strength, sleep, energy, years)",
        "viewer_framing": "sitting all day, little free time, back pain, poor sleep, low energy",
        "topic_item": "one sport, exercise, habit or health topic",
        "topic_ok": [
            "- ok: a real, safe exercise, habit, sport or health topic backed by mainstream science, spelled",
            "  correctly, specific enough to film in under a minute;",
        ],
        "topic_doubt": "unsafe, pseudo-scientific or medical (diagnosis, treatment, cure, drugs, supplements as "
                       "medicine), a brand or product, or too vague to film (e.g. 'Health')",
    },
}


def kind(name):
    return KINDS.get(name or "business", KINDS["business"])


# ------------------------------------------------------------------ channel templates

SPORT_RULES = """\
# Script rules for 'Sport & Health'. Lines starting with # are notes and are not sent to the AI.
Write like a calm, friendly coach talking to one person: full spoken sentences that connect, never a list.

1. HOOK (first 3-5 seconds): one concrete claim or question about this topic, built on a real number
   (minutes, reps, hours, steps) that the body backs up.
2. SETUP (scene 1): what it is and who it is for, in plain words.
3. WHY IT WORKS (1-2 scenes): the mechanism in simple words, with the number from mainstream science.
4. HOW TO DO IT (1-2 scenes): one clear, safe routine the viewer can try today - sets, reps or minutes.
5. THE MISTAKE (1 scene): the common mistake or the safety warning, said kindly.
6. PAYOFF (last body scene): what changes and when, honestly ("in about two weeks most people notice...").

Never:
- promise to cure, treat or prevent a disease, or give medical, drug or supplement dosing advice;
- invent studies, percentages or "doctors hate this" claims;
- shame bodies or use before/after weight promises;
- skip the warning when an exercise can hurt (back, knees, heart conditions, pregnancy).
"""

SPORT_TOPICS = HERE / "sport_health_ideas.txt"

TEMPLATES = {
    "business": {
        "label": "Business: 365 Businesses That Make Money",
        "settings": {},   # the settings in .env were written for this niche
        "rules": None,
        "topics": HERE / "business_ideas.txt",
    },
    "sport_health": {
        "label": "Sport & Health",
        "settings": {
            "NICHE_KIND": "health",
            "CHANNEL_NICHE": "simple, science-backed exercise, sleep and nutrition habits that ordinary people "
                             "can do at home in minutes a day - what to do, why it works and the common mistake",
            "CHANNEL_AUDIENCE": "American adults 25-55 with a desk job and little free time, who want more energy, "
                                "less back pain and better sleep, and distrust fitness influencers",
            "CHANNEL_LANGUAGE": "en",
            "SERIES_NAME": "1 Habit a Day",
            "SERIES_RULES": "Each episode covers ONE specific exercise, habit or health fact an ordinary adult can "
                            "try today. Cover: what it is, why it works (one number from mainstream science), how "
                            "to do it safely, the common mistake, and what changes. No medical advice, no cures, "
                            "no supplements.",
            "SERIES_LABEL": "HABIT #{n}",
            "SERIES_TITLE_PREFIX": "Habit #{n}:",
            "DESCRIPTION_FOOTER": "General information, not medical advice. Talk to a doctor before starting a new "
                                  "exercise routine, especially with an injury or a health condition.",
            "DESCRIPTION_CTA": "",
            "EDGE_VOICE": "en-US-EmmaMultilingualNeural",
            "FACT_CHECK": "fix",
            "COVER_BOX_COLOR": "#0A84FF",
            "COVER_MONEY_COLOR": "#FFE200",
        },
        "rules": SPORT_RULES,
        "topics": SPORT_TOPICS,
    },
}


def template(name):
    return TEMPLATES.get(name) or TEMPLATES["business"]


def starter_topics(name):
    path = template(name)["topics"]
    if not path or not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
