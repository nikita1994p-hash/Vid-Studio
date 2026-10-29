# VidAuto — project state (for a fresh chat / after /compact)

Last updated: 2026-10-28. Owner talks Russian; all viewer-facing content is English (US audience).

## What it is
Faceless YouTube Shorts autopilot, series **"365 Businesses That Make Money"** (one real, boring,
startable business per Short: startup cost, monthly profit range, the catch, step one).
Runs in Docker on Windows 11 from this folder.

- `docker-compose.yml` → `n8n` (2.45.4, http://localhost:5678) + `worker` (FastAPI + FFmpeg, dashboard http://localhost:8000)
- `Запустить VidAuto.bat` / `Остановить VidAuto.bat` — one-click start/stop (ASCII-only content: cmd breaks on UTF-8 labels)
- `worker/pipeline.py` — whole production; `worker/app.py` — HTTP API; `worker/static/` — Studio-style dashboard
- `n8n/yt-autopilot-workflow.json` — imported into n8n as "VidAuto: YouTube Shorts" (NOT published yet)
- `data/jobs/<id>/` — per-video files (`status.json`, `result.json`, `plan.json`, `scores.json`, `scenes.json`, `final.mp4`)
- `data/music/` — 8 Kevin MacLeod tracks (CC BY 4.0) + `credits.json` (credit auto-added to description)
- `data/sfx/transition.wav` — FFmpeg-generated whoosh on scene cuts
- `.env` — all settings (Gemini key is set); `.env.example` — same without keys
- `VidAuto — инструкция от А до Я.docx` — the user guide (Russian, 20 sections; section 13 = topics/fact check/covers/Telegram/A/B). Generator script lives only in a temp scratchpad; to update, rebuild with docx-js or edit the docx.
- `АНАЛИЗ.md` — original feasibility/cost analysis (prices verified 27.10.2026)
- Voice profile: `C:\Users\nikit\.claude\youtube\voice.md` (filled, English); template copy `voice.template.md`
- YT skills used: `C:\Users\nikit\.claude\skills\yt-script\hookscore.py`, `hooks.json`, `yt-package\title.py`

## Pipeline (all free right now)
1. Script: Gemini Interactions API, chain `gemini-3.8-flash` → `gemini-3.5-flash-lite` → `gemini-3.5-flash`
   (90 s hard deadline each; free-tier models often 503/429). 5 hooks over 21 formulas, best by hookscore.py,
   title by title.py, title prefixed "Business #N:" (N = len(history)+1), duplicate tags stripped.
2. Voice: edge-tts `en-US-AndrewMultilingualNeural`, word timings → word-synced captions.
3. Visuals (`VISUAL_PROVIDER=auto`): **FLUX.1 schnell on Cloudflare Workers AI** first (free 10k neurons/day,
   `CF_ACCOUNT_ID` + `CF_API_TOKEN` in .env, ~2 s per 1024x1024 image, verified on Business #5), then
   Openverse photos (keyless, licenses cc0/pdm/by only, relevance filter,
   15 s timeout + circuit breaker) → number cards fallback. Pexels supported but Pexels paused new API keys.
   Gemini images = paid only (free tier limit 0, verified). Manual Gemini Pro images via dashboard
   "Copy prompt for Gemini" → "Replace picture" → "Re-render video" (~40 s).
   Script structure rules live in `data/script_rules.md` (hook → setup → middle → payoff → loop),
   editable in dashboard Settings; injected into the prompt every run (`#` lines are user notes).
   Loop ending (`LOOP_ENDING=true`): each hook has a `loop_line`; it becomes a last scene over the hook
   picture that zooms out to exactly the hook's first frame (seamless replay).
   Parts (`MAX_PARTS=3`): the model may return `parts` outlines → part 1 now, the rest queued in
   `data/parts_queue.json`; dashboard runs make all parts in a row, n8n makes one per run. Titles
   "Business #N (Part k/T):", label "BUSINESS #N · PART k/T"; parts share the episode number.
4. Render: FFmpeg 1080x1920, zoompan at 1.4x, overlay (shade + "BUSINESS #N" tag + gold on_screen text),
   ASS captions, music, whoosh. Description gets footer + music credit + photo credits.

## Studio settings, versions, accounts (added 28.10.2026)
- Settings: `pipeline.SETTINGS_SCHEMA` (54 fields, 8 groups) → Settings page; saved to `data/settings.json`.
  `env()` order: this video's options (ContextVar, from ＋CREATE → Options, stored in status.options and reused
  by re-render) → Studio settings → .env → schema default. API keys stay in .env only.
  Voice: locale/gender/voice/speed/pitch/volume + Listen preview; captions: font/size/colours/outline/shadow/
  box/position/words/uppercase/pop + Preview; picture: key-number/badge colour, shade, zoom, image style.
- Own script: ＋CREATE → "Write my own script" (line 1 hook, 1 line per scene) → kept word for word, Gemini
  only adds pictures/on-screen/loop line/titles; if all models fail → `local_plan`.
- Versions (A/B, 1-5): `make_variants` = next-best hook (+ its loop line), next title, voice from
  VARIANT_VOICES, next music track, caption colour, fresh FLUX pictures. Deliberately NOT micro-trimmed /
  pitch-shifted copies to dodge YouTube duplicate detection (spam policy risk) - told the user so.
- Accounts: `worker/social.py` — OAuth login/logout/switch for YouTube, TikTok, Instagram (Instagram Login),
  Facebook Page, Threads, X; app keys + tokens in `data/accounts/`. Redirect URI
  `http://localhost:8000/oauth/<platform>/callback`. Upload code follows the 2026 docs but is UNTESTED until
  the user connects real apps. Threads needs PUBLIC_BASE_URL (public https link); TikTok unaudited = private only.
- n8n workflow replaced (same id TwAPdIU6eqh6KQN1, now "VidAuto: Make & Publish", still unpublished):
  daily → /produce → /social/defaults → per platform POST /social/<p>/post; Studio Publish → webhook
  `vidauto-publish`. "Post directly" in the Studio skips n8n. The old n8n YouTube-node path is gone.

## Earnings (added 28.10.2026)
- `worker/earnings.py` + Studio page **Earnings**: month income/costs/profit, 6-month table + bar chart,
  YouTube subs/views/revenue/RPM (Analytics API), YPP progress (500 subs + 3M Shorts views/90d; 1000 + 10M),
  views per video per platform, manual income entries (`data/income.json`), stats cache `data/stats.json`
  (auto refresh when >6 h old). Costs = per-video AI estimate + MONTHLY_COSTS setting.
- YouTube scopes now include yt-analytics(.monetary).readonly; TikTok video.list; IG/Threads insights.
  Accounts connected before this need Switch account to grant them. Fetchers UNTESTED without real accounts
  (verified only with fake stats.json, then deleted).
- DESCRIPTION_CTA setting = a link line added to every description. Chart colours #2f8fe0 / #2ba640 validated.

## Topics, fact check, covers, Telegram, A/B results (added 28.10.2026)
- `worker/topics.py` + Studio **Topics**: `data/topics.json` [{id, topic, state todo|used|skipped, job_id}].
  produce() with no topic/script/part takes the first todo (TOPIC_QUEUE on), marks it used only on success.
  `worker/business_ideas.txt` = 397 starter businesses (already loaded, Laundromat first); Gemini "30 fresh ideas".
  Drag and drop reorder, ⤒ to top, ✓/⤼/↺/✕. Verified in the browser.
- Fact check: new stage `check` between script and voice. `pipeline.fact_check` = 2nd Gemini call with
  FactCheck schema -> `factcheck.json`. FACT_CHECK flag (default) | fix (high-severity body lines only, swaps just
  the wrong sentence; never hooks or own script) | off. Verified on a copy of Business #6 with a planted $200K/mo.
- Covers: `Plan.cover_lines` (optional) -> `make_cover` -> `cover.jpg` 1080x1920 (hook picture, huge yellow money
  figure, name on red box, badge). Settings group Cover; Studio card "Cover" (edit lines/picture, Remake).
  Posting: YouTube thumbnails.set (needs phone-verified channel), Facebook /thumbnails, Instagram cover_url (needs
  PUBLIC_BASE_URL); TikTok/X have no API for it. Business #6 has its cover. Posting part UNTESTED.
- `worker/notify.py`: Telegram bot (token+chat in `data/accounts/telegram.json`, set up in Settings ->
  Notifications: Save token / Find my chat / Send test). Events NOTIFY_DONE/ERRORS/LIMITS/POSTED/MONEY/AB.
  Not connected yet. App runs a background stats refresh every 6 h (for money/A/B alerts).
- `worker/abtest.py` + Studio **A/B tests**: groups = original + variant_of; winner after AB_DAYS (7) with
  AB_MIN_VIEWS (100) by YouTube averageViewPercentage (earnings now fetches per-video retention). Leaderboard
  per hook formula and voice; AB_LEARN feeds winning formulas into the script prompt. `result.hook_formula`
  stored from now on. Verified only with fake data.

## Topic check, picture prompt check (added 28.10.2026)
- Studio renamed "Vid Studio" (UI only; folder/bat/n8n names stay VidAuto).
- `topics.check()` (POST /api/topics/check, 80 per call, UI loops): Gemini TopicReview -> ok | fix (typo/wording,
  renamed in place, old name in note) | doubt (not real, illegal, scam, brand, vague, duplicate - only marked).
  Licenses/big budgets are deliberately NOT a doubt reason (first version flagged 78 real businesses). suggest()
  reviews fresh ideas before adding and drops doubtful ones (TOPIC_CHECK). All 397 starter topics checked: 0 doubtful.
  Fixed bug in add(): it stripped leading digits/trailing ")" ("3D printing" -> "D printing").
- `pipeline.prompt_check` after fact_check (PROMPT_CHECK, Picture group): rewrites prompts with hands, text, crowds,
  screens etc. -> `promptcheck.json`, Studio card "Picture prompts". Script prompt now steers image_prompt the same
  way; FLUX suffix adds "sharp focus, natural proportions, anatomically correct". Verified on a copy of Business #6.

## Hook scoring and rewrite loop (added 28.10.2026)
- yt-script's hookscore.py is a general lint (its own docstring: barely separates hits from misses), digit-only
  numbers, loss-only stakes, demands 'you', 9-24 words, 40% weakest link -> our spelled-number hooks got ~42.
  Gaming it produced nonsense ("How can you never risk your savings before...") scoring 74.
- `worker/hookscore_shorts.py` (default, HOOK_SCORER=shorts; 'skill' = old one): SPECIFICITY (spelled numbers
  count), STAKES (money won or lost), CURIOSITY, FRONT (first 5 words: number / business / you), BREVITY 8-18,
  verdict 0.7*mean + 0.3*min. Calibrated: junk 24-37, ordinary 41-56, well-built 80.
- `pipeline.improve_hooks` before the script is done: if best < HOOK_MIN_SCORE (55) rewrite up to HOOK_ROUNDS (3);
  every rewrite passes a strict Gemini editor (on topic, figures from body, natural, loop_line flows, no
  stuffing). hookcheck.json + note on the Script tab. Test (target 70): 56 -> 69 honestly in 3 rounds, ~10 min.
  Business #6's best is 56 under the new scorer, so at the default 55 no rewrite is needed.

## Verified facts (do not re-research)
- Gemini API free tier: text + TTS work; ALL image models and Omni video = "limit 0 on Free Tier". Imagen 3/4 gone (Imagen 4 shut down 17.08.2026).
- New Gemini accounts are Prepay ($5 min); AI Pro $10/mo credit only spends while prepaid balance > 0.
- YouTube videos.insert: unverified projects no longer forced private; publishAt needs privacyStatus=private; containsSyntheticMedia set via videos.update.
- OAuth consent screen must be "In production" or refresh tokens die in 7 days.
- Docker env_file truncates unquoted values at " #" → quote SERIES_LABEL / SERIES_TITLE_PREFIX.
- No NVIDIA GPU (AMD Radeon iGPU, 15 GB RAM) → local SD/FLUX impractical.
- openhiggsfield.ai = UI that needs a paid provider key (not free). HyperFrames (HeyGen, HTML→MP4) optional for motion graphics later.

## Status
- Done: 4 real videos (Business #1–#4), dashboard, playback-cut bug fixed (no re-render while playing / no idle polling),
  Cache-Control no-cache on dashboard files, interrupted-job recovery on restart.
- Not done: YouTube OAuth in n8n (user will do later, guide section 9), n8n owner account, Publish workflow,
  Pexels key (API paused). Openverse was throttled at last test → recent videos are cards only.
- Ideas offered: Wikimedia Commons as second photo source; topic queue of 365 businesses in dashboard;
  n8n error alerts to Telegram; Kokoro TTS as license-clean voice; HyperFrames for animated infographics.

## Gotchas
- Windows `python3` alias (WindowsApps) can hang in background shells — use PowerShell `Invoke-RestMethod` for API checks.
- Long scratch paths break Win32 cwd; project path is short now.
- Rebuild after code changes: `docker compose up -d --build worker`.

## Publish timing (added 29.10.2026)
- PUBLISH_TIME 09:00, PUBLISH_TIMEZONE Europe/Kyiv (saved in data/settings.json), same as the n8n daily run.
- `youtube_status("schedule")`: if the video is ready within 3 h after the publish time (or up to 10 min before), it goes public at once; ready earlier the same day -> publishAt today; otherwise tomorrow.
- AUTO_PUBLISH is still false: the daily run uploads private for the manual test week.
- `data/tools/redo_pictures.py <job_id>`: redo FLUX pictures for a video that fell back to cards, re-render, new cover (run in the worker with `-w /app`, MSYS_NO_PATHCONV=1 from Git Bash).

## Accounts and channels — Stage 1 (added 29.10.2026)
Built so the SaaS move later swaps storage, not logic.
- `worker/space.py` — channels. `data/channels/<id>/` holds channel.json {id,name,owner,template,daily,created}, jobs/,
  topics.json, history.json, settings.json, script_rules.md, parts_queue.json, stats.json, income.json,
  ab_notified.json, notify_state.json, accounts/ (youtube.json, telegram.json…). Shared in /data: music, sfx,
  previews, accounts/apps.json + pending.json (OAuth app keys, logins in progress), users.json, sessions.json,
  telegram_offsets.json. A ContextVar holds the current channel; `space.Here("x")` paths resolve per call;
  `space.thread()` / `copy_context()` carry it into threads (_hard_timeout pool, notify.send). `space.migrate()`
  moved the old single-channel files into the first channel on first start (owner claimed by the first account).
- `worker/auth.py` — e-mail + password (scrypt), sessions (cookie `vs_session`, SHA-256 stored, 30 days), roles
  owner/user, marketing consent (separate unticked box, `marketing_at`), CSV export of subscribers only.
  Login slowdown after 5 wrong passwords. No e-mail verification / reset yet (needs SMTP: SaaS stage).
- app.py middleware: /login, /static, /api/auth, /health are open; /produce, /social/*, /jobs/* are for n8n
  (channel from the video id; `INTERNAL_TOKEN` env enables a header check — set it on a server);
  everything else needs a session and runs in the user's open channel (or the video's channel if theirs).
  A user with no channel gets 409 "create a channel first" — never another user's data.
- Channel API: GET /api/me, POST /api/channels {name, template}, POST /api/channels/{id}/open, PATCH (name, daily),
  DELETE ?confirm=<name>; users (owner): GET/PATCH/DELETE /api/users, GET /api/users/marketing.csv.
- `/produce` (n8n 9:00) makes one video per channel with daily=true and returns {videos:[{job_id, channel, platforms}]};
  the n8n code node posts each (n8n/yt-autopilot-workflow.json patched; timeout 60 min).
- `worker/niches.py` — NICHE_KIND (business | health) wording for script, fact check, hook rewrite/editor, topic
  check and suggestions; templates `business` and `sport_health` (settings, SPORT_RULES, sport_health_ideas.txt).
  hookscore_shorts.score(text, topic, kind): health counts units (minutes, reps, steps…) and body stakes.
- Telegram buttons: a private YouTube upload's "posted" alert has 🌍 Publish now / 🗑 Delete from YouTube;
  `notify.poll_loop` reads presses for every channel's bot (only the alert's chat may press). Needs the new
  scope youtube.force-ssl → reconnect YouTube once (Accounts → Switch account); privacy.html lists it.
- PUBLISH_TIME 09:00 / PUBLISH_TIMEZONE Europe/Kyiv now also in .env (default for new channels).

## FLUX settings (29.10.2026)
- FLUX_STEPS = 4 everywhere (channel settings, .env, .env.example, schema default): flux-1-schnell is distilled for 1-4
  steps; measured ~220 neurons/picture at 6 steps, 4 steps ≈ 165 → ≈ 60 pictures ≈ 10 videos per day on the free 10k.
- Workers AI flux-1-schnell accepts ONLY `prompt` and `steps` (verified: width, height, num_inference_steps, seed → HTTP 400
  "Additional properties not allowed"). Output is always 1024x1024; the renderer crops to 9:16.
- Limit behaviour seen 29.10: refused at 00:05/00:22 UTC, worked 00:54 (≈1.75k used), refused again ≈01:25 while the
  dashboard said ≈2k/10k today — enforcement and the dashboard disagree; treat it as unknown until observed again.
- `data/redo_pictures.json` + app.py `_redo_loop`: card-only videos get FLUX pictures later (pipeline.queue_redo).

## Design rules
See DESIGN.md (tokens, spacing, controls, safety). Check every page with `(await import("/static/ui-audit.js")).auditAll()` at 1440 px and 375 px.

## Studio stats and marketing data
Sign-up source (?ref= / utm_*), browser time zone + language, unsubscribe reason, plan (free / trial 7 days / pro, set by hand on Users) are stored per account. Subscribers CSV: 28 columns incl. stage and last activity. Studio stats page (#/stats, super admin / owner): funnel, active 7/30 days, sources, reasons, plans, regions, videos / uploads / sign-ups per day. Never uses YouTube API data (Limited Use).

## Plans, invites, plans page
worker/plans.py: none (new accounts: menu + plans page only) / trial 3 days / standard / pro; videos per rolling 24 h 0/1/1/5 (A/B versions count), channels 0/1/1/3; admins unlimited. Admins set plans on Users (admins: plain users only); invite links (Users -> Invite links, super admin / owner) give a plan at sign-up; Telegram alert on every sign-up with a "Give trial" button (admin channels only). Public plans page /pricing: Trial -> sign-up, Standard / Pro -> "payments soon" dialog -> sign-up with wanted_plan. Prices: plans.py PLANS[...]["price"].

## Public address (Tailscale Funnel)
docker compose --profile public up -d starts the tailscale container (needs TS_AUTHKEY in .env); it publishes only the worker at https://vidstudio.<tailnet>.ts.net (tailscale/serve.json). Requests through it never reach /produce, /social/, /jobs/, /n8n/ (PUBLIC_PROXY_HOST=tailscale). Instagram / Threads fetch files through signed 24 h links (auth.sign_file, key in data/secret.key). After it runs: set PUBLIC_BASE_URL to the ts.net address.
