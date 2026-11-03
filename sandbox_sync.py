"""Fill data-sandbox/ (the test Studio's data) from data/ (the real one).

Copies settings, rules, music and the newest videos of every channel - but never platform logins,
app keys, the Telegram bot, sessions or the signing key, so the test Studio cannot post or message.
The old data-sandbox/ is wiped first.

    python sandbox_sync.py            # 2 newest videos per channel
    python sandbox_sync.py --jobs 5
"""
import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL, TEST = HERE / "data", HERE / "data-sandbox"
SHARED = ["users.json", "music", "sfx", "previews"]       # data/<name>
CHANNEL = ["channel.json", "settings.json", "script_rules.md", "topics.json", "history.json",
           "parts_queue.json", "stats.json", "income.json"]  # data/channels/<id>/<name>; never accounts/


def copy(src, dst):
    if src.is_dir():
        shutil.copytree(src, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=2, help="newest videos to copy per channel")
    n = ap.parse_args().jobs
    if TEST.exists():
        shutil.rmtree(TEST)
    TEST.mkdir()
    for name in SHARED:
        copy(REAL / name, TEST / name)
    for ch in sorted((REAL / "channels").iterdir()):
        if not ch.is_dir():
            continue
        for name in CHANNEL:
            copy(ch / name, TEST / "channels" / ch.name / name)
        jobs = sorted((ch / "jobs").iterdir())[-n:] if (ch / "jobs").is_dir() else []
        for job in jobs:
            copy(job, TEST / "channels" / ch.name / "jobs" / job.name)
        print(f"{ch.name}: {len(jobs)} video(s)")
    print(f"done: {TEST}")


if __name__ == "__main__":
    main()
