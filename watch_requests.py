"""Watch GitHub for video requests and build them on this laptop.

    python watch_requests.py

Locked-cast videos cannot run in the cloud: they need
`nano_banana_pro --image-references`, which exists only in the Higgsfield CLI,
and the headless API answers 404 for it. So the request travels instead of the
work — a line typed on a phone lands in the repo, and this picks it up.

From a phone: open video_requests.txt on github.com, tap the pencil, add a line
with the story you want, commit. That is the whole interface.

    मूली दीदी और कद्दू काकी की कहानी

Lines already handled are rewritten with their video's link, so the file doubles
as a record of what was made. A line starting with # is ignored.

The laptop has to be awake and online — that is the price of the locked cast.
"""
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import ultimate_shorts_agent as u           # noqa: E402

REQUESTS = BASE / "video_requests.txt"
POLL_SECONDS = 120
HF = r"C:\Users\91872\AppData\Local\higgsfield\bin\higgsfield.exe"
CREDITS_NEEDED = 8          # a 4-scene --stills video, at 2 per drawing


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-300:]}")
    return (r.stdout or "") + (r.stderr or "")


def credits():
    r = subprocess.run([HF, "account", "status"], capture_output=True,
                       text=True, encoding="utf-8")
    m = re.search(r"([\d.]+)\s+credits", (r.stdout or "") + (r.stderr or ""))
    return float(m.group(1)) if m else None


def pending(lines):
    """The first line asking for a video that has not been made yet."""
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("[done]") \
                or line.startswith("[failed]"):
            continue
        return i, line
    return None, None


def build(premise, cfg):
    """Build and publish one request. Returns the video URL."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", premise)[:40] or "video"
    out = Path.home() / "Desktop" / "muli-runs" / \
        f"{datetime.now():%Y%m%d_%H%M%S}_{slug}"
    out.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(
        [sys.executable, str(BASE / "make_muli_video.py"), premise,
         "--stills", "--upload", "--out", str(out)],
        cwd=BASE, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    blob = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(https://www\.youtube\.com/watch\?v=[\w-]+)", blob)
    if r.returncode or not m:
        raise RuntimeError(blob[-400:].strip() or "build failed")
    return m.group(1)


def main():
    cfg = u.load_config()
    print(f"watching {REQUESTS.name} every {POLL_SECONDS}s — Ctrl+C to stop")
    u.send_telegram(cfg, "👀 Video watcher started — add a line to "
                         "video_requests.txt on GitHub and it will be built.")

    while True:
        try:
            git("pull", "--quiet", "--rebase")
            lines = (REQUESTS.read_text(encoding="utf-8").splitlines()
                     if REQUESTS.exists() else [])
            idx, premise = pending(lines)

            if premise:
                have = credits()
                if have is not None and have < CREDITS_NEEDED:
                    # Say so once, then wait rather than failing every request.
                    u.send_telegram(
                        cfg, f"⚠️ Not enough Higgsfield credits ({have}); a "
                             f"video needs about {CREDITS_NEEDED}. Waiting — "
                             "top up and it will build on its own.")
                    time.sleep(3600)
                    continue

                print(f"building: {premise}")
                u.send_telegram(cfg, f"🎬 Building: {premise}")
                try:
                    url = build(premise, cfg)
                    lines[idx] = f"[done] {url}  <- {premise}"
                    print(f"  -> {url}")
                except Exception as exc:                      # noqa: BLE001
                    lines[idx] = f"[failed] {premise}"
                    print(f"  FAILED: {exc}")
                    u.alert_failure(cfg, f"❌ Video build FAILED\n"
                                         f"Request: {premise}\nError: {exc}")

                # Record the outcome so the same line is not built twice.
                REQUESTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
                git("add", str(REQUESTS))
                git("-c", "user.name=shorts-bot",
                    "-c", "user.email=bazarblast140@gmail.com",
                    "commit", "-q", "-m", "Record a video request outcome",
                    check=False)
                git("push", check=False)

        except KeyboardInterrupt:
            raise
        except Exception as exc:                              # noqa: BLE001
            # A dropped network or a git hiccup must not end the watch.
            print(f"  (poll error: {str(exc)[:150]})")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
