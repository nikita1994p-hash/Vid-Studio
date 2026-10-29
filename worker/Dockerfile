FROM python:3.12-slim-trixie

# ffmpeg renders the video; DejaVu has Cyrillic glyphs for the burned-in subtitles
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py pipeline.py social.py earnings.py topics.py notify.py abtest.py hookscore_shorts.py space.py auth.py niches.py business_ideas.txt sport_health_ideas.txt ./
COPY static ./static

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 DATA_DIR=/data SKILLS_MOUNT=/skills VOICE_FILE=/youtube/voice.md
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
