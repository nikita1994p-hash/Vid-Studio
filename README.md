# Vid Studio

A faceless YouTube Shorts autopilot for the series **365 Businesses That Make Money**: script (Gemini) → fact check →
voice (edge-tts) → pictures (FLUX on Cloudflare Workers AI) → FFmpeg render → cover → posting (YouTube, TikTok,
Instagram, Facebook, Threads, X), with a local dashboard on http://localhost:8000.

Runs in Docker on Windows: `Запустить VidAuto.bat` / `Остановить VidAuto.bat`.

- `worker/` — FastAPI app, pipeline and the dashboard (`worker/static`)
- `n8n/` — the daily workflow for n8n
- `docs/` — public pages for the Google OAuth consent screen (GitHub Pages)
- `.env.example` — copy to `.env` and add your own keys; `.env` and `data/` are never committed

Project notes: `PROJECT_STATE.md`. Full guide (Russian): `VidAuto — инструкция от А до Я.docx`.
