#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ultimate_shorts_agent.py
========================
A single-file, production-grade pipeline that automatically:

  1. Picks an unused kids-story topic from topic_bank.json -> Topic bank
  2. Writes a 6-scene HINDI cartoon story (Pixar/Disney)   -> Claude (opus)
  3. Generates a cute Hindi voiceover                      -> edge-tts (free)
  4. Renders + ANIMATES each scene (image -> video)        -> Higgsfield Soul+DoP
  5. Stitches the animated clips + voiceover + music       -> ffmpeg
  6. Uploads the video to YouTube + Telegram report        -> google-api / requests
  7. Runs twice daily at 08:00 & 18:00 IST                 -> Task Scheduler

Every step is wrapped in defensive try/except logic. If the live trend fetch
fails, an evergreen GK/Facts fallback topic is used so the pipeline never stalls.

--------------------------------------------------------------------------------
INSTALL
--------------------------------------------------------------------------------
    pip install anthropic moviepy edge-tts google-api-python-client \
                google-auth-oauthlib google-auth-httplib2 schedule requests pytz

--------------------------------------------------------------------------------
FOLDER LAYOUT (auto-created on first run)
--------------------------------------------------------------------------------
    youtube-shorts-agent/
      ├── ultimate_shorts_agent.py
      ├── client_secret.json     (Google Cloud OAuth desktop client)
      ├── token.json             (auto-generated after first YouTube login)
      ├── config.json            (API keys / tokens - see SAMPLE_CONFIG below)
      ├── assets/music/          (drop 4-5 no-copyright .mp3 tracks here)
      ├── archive/               (uploaded raw files land here)
      └── logs/                  (rotating run logs)

--------------------------------------------------------------------------------
config.json SAMPLE
--------------------------------------------------------------------------------
    {
      "anthropic_api_key": "sk-ant-...",
      "anthropic_model": "claude-opus-4-8",
      "telegram_bot_token": "123456:ABC-...",
      "telegram_chat_id": "987654321",
      "higgsfield_api_key": "KEY_ID:KEY_SECRET",  // from cloud.higgsfield.ai
      "higgsfield_model": "dop-turbo",            // dop-lite/dop-preview/dop-turbo
      "youtube_privacy_status": "public",        // or "unlisted" / "private"
      "run_immediately_on_start": false
    }

NOTE: Video rendering uses the official Higgsfield Cloud REST API (stable),
not browser automation. Endpoint paths / model / aspect ratio are isolated in
the HIGGSFIELD_API block below. Get an API key at https://cloud.higgsfield.ai/.
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

# --------------------------------------------------------------------------- #
#  Paths & constants
# --------------------------------------------------------------------------- #
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "config.json")
CLIENT_SECRET = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_PATH    = os.path.join(BASE_DIR, "token.json")
MUSIC_DIR     = os.path.join(BASE_DIR, "assets", "music")
ARCHIVE_DIR   = os.path.join(BASE_DIR, "archive")
WORK_DIR      = os.path.join(BASE_DIR, "work")          # transient render space
LOG_DIR       = os.path.join(BASE_DIR, "logs")
TOPIC_BANK    = os.path.join(BASE_DIR, "topic_bank.json")   # kids-story topics

YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]

# Higgsfield Cloud REST API — the stable, documented way to render videos.
# (Replaces the old, fragile browser-automation selectors.)
#   Docs / key:  https://cloud.higgsfield.ai/  ->  API keys
#   Auth header: "Authorization: Key KEY_ID:KEY_SECRET"
# Higgsfield has no single "text->video" call; the real flow is two steps:
#   1) text -> image  (Soul model)   POST /v1/text2image/soul
#   2) image -> video (DoP model)    POST /v1/image2video/dop
# Both return a "job set"; we poll GET /v1/job-sets/{id} until completed.
# Schema below was verified live against the API; tweak in one place if needed.
HIGGSFIELD_API = {
    "base_url":         "https://platform.higgsfield.ai",
    "text2image_path":  "/v1/text2image/soul",         # step 1: prompt -> image
    "image2video_path": "/v1/image2video/dop",         # step 2: image -> video
    "status_path":      "/v1/job-sets/{request_id}",   # poll job set here
    # Soul accepts only these exact sizes; 1152x2048 is 9:16 vertical (Shorts).
    "image_size":       "1152x2048",
    # DoP model: one of 'dop-lite', 'dop-preview', 'dop-turbo'.
    "video_model":      "dop-turbo",
    "poll_interval_s":  6,                              # how often to poll
    "poll_timeout_s":   20 * 60,                        # give up after 20 min (DoP is slow)
}

# Kids cartoon-story settings (NITIN CARTOON ZONE style).
NUM_SCENES   = 6                       # scenes per story (~40-50s total)
TTS_VOICE    = "hi-IN-SwaraNeural"     # cute girl (or hi-IN-MadhurNeural for boy)
TTS_RATE     = "+18%"                   # faster + energetic (grabs attention)
TTS_PITCH    = "+22Hz"                  # higher = cuter, child-like
MUSIC_VOLUME = 0.0                      # background music level (0.0 = OFF; e.g. 0.08 = quiet)

# Reach / SEO — niche hashtags appended to every description, and base keywords
# merged into tags so YouTube surfaces the video in search + suggested.
SEO_HASHTAGS = [
    "#Shorts", "#hindikahani", "#kahani", "#bachonkikahani", "#moralstory",
    "#hindicartoon", "#kidsstory", "#storytime", "#viralshorts", "#trending",
    "#hindistories", "#cartoonstory",
]
SEO_BASE_TAGS = [
    "hindi kahani", "bachon ki kahani", "moral story hindi", "hindi cartoon",
    "kids story hindi", "cartoon kahani", "hindi moral stories",
    "bedtime stories hindi", "hindi stories for kids", "shorts",
]

# Visual freshness — a random mood/palette per video keeps the 3D-cartoon brand
# consistent while making each upload look distinct (helps avoid YouTube's
# "repetitive / mass-produced" flag that can block monetization).
STYLE_FLAVORS = [
    "warm golden sunset tones", "bright cheerful daytime colours",
    "cozy cool evening blue tones", "vibrant rainbow pastel palette",
    "soft dreamy morning light", "festive colourful decorations",
    "lush green nature setting", "magical sparkly glow",
    "rainy-day cozy mood", "snowy winter wonderland tones",
]
TARGET_SECONDS_MIN = 40                # acceptable video length window
TARGET_SECONDS_MAX = 55

# How often to use a LIVE trend instead of a topic-bank story (0.0-1.0).
TREND_RATIO = 0.0

# Evergreen general-knowledge fact topics (the channel's core content).
FALLBACK_TOPICS = [
    {"topic": "Amazing facts about the human brain"},
    {"topic": "Mysteries of the Bermuda Triangle"},
    {"topic": "How black holes actually work"},
    {"topic": "Unknown facts about Ancient India"},
    {"topic": "Why the Great Wall of China was built"},
    {"topic": "Surprising facts about the deep ocean"},
    {"topic": "Secrets of the Egyptian pyramids"},
    {"topic": "Incredible facts about outer space"},
    {"topic": "Why do cats purr and what it means"},
    {"topic": "How honey bees make honey"},
    {"topic": "Amazing facts about the planet Mars"},
    {"topic": "Why the sky is blue"},
    {"topic": "Fascinating facts about octopuses"},
    {"topic": "How the human heart works"},
    {"topic": "Mysteries of the lost city of Atlantis"},
    {"topic": "Why dinosaurs went extinct"},
    {"topic": "Amazing facts about Mount Everest"},
    {"topic": "How rainbows are formed"},
    {"topic": "Surprising facts about the Sun"},
    {"topic": "Why elephants never forget"},
    {"topic": "Fascinating facts about Ancient Egypt"},
    {"topic": "How airplanes stay in the air"},
    {"topic": "Amazing facts about the human body"},
    {"topic": "Why the Moon affects ocean tides"},
    {"topic": "Incredible facts about the Amazon rainforest"},
    {"topic": "How volcanoes erupt"},
]

# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #
def _build_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    # Windows consoles default to cp1252, which crashes on emoji / Hindi text.
    # Force UTF-8 on stdout/stderr so logging never raises UnicodeEncodeError.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                       # noqa: BLE001
            pass
    logger = logging.getLogger("shorts_agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:                        # avoid duplicate handlers on reload
        return logger
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "agent.log"),
        maxBytes=2_000_000, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # A console may not exist under Windows Task Scheduler (sys.stdout is None);
    # only add the stream handler when there is a real stream to write to.
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = _build_logger()


# --------------------------------------------------------------------------- #
#  Config & filesystem bootstrap
# --------------------------------------------------------------------------- #
def ensure_dirs():
    for d in (MUSIC_DIR, ARCHIVE_DIR, WORK_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def load_config():
    """Load config.json; fail loudly with a helpful message if missing."""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"config.json not found at {CONFIG_PATH}. "
            "Create it (see the sample in this file's docstring)."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # sensible defaults
    cfg.setdefault("youtube_privacy_status", "public")
    cfg.setdefault("headless", True)
    cfg.setdefault("run_immediately_on_start", False)
    return cfg


# --------------------------------------------------------------------------- #
#  STEP 1a — Trending engine (Google Trends RSS)
# --------------------------------------------------------------------------- #
def pick_topic(cfg):
    """
    Pick an UNUSED kids-story topic from topic_bank.json, mark it used, and
    return {"topic","source","category"}. Resets the used list once every topic
    has been used. Falls back to a live trend / evergreen topic if the bank is
    missing or unreadable.
    """
    import random
    try:
        with open(TOPIC_BANK, "r", encoding="utf-8") as f:
            bank = json.load(f)
        cats = bank.get("categories", {})
        used = set(bank.get("used_topics", []))
        all_topics = [(c, t) for c, lst in cats.items() for t in lst]
        pool = [(c, t) for c, t in all_topics if t not in used]
        if not pool:                                   # everything used -> reset
            log.info("Topic bank exhausted; resetting used_topics.")
            used = set()
            pool = all_topics
        if not pool:
            raise ValueError("topic_bank.json has no topics")
        cat, topic = random.choice(pool)
        used.add(topic)
        bank["used_topics"] = sorted(used)
        with open(TOPIC_BANK, "w", encoding="utf-8") as f:
            json.dump(bank, f, ensure_ascii=False, indent=2)
        log.info("Story topic (%s): %s", cat, topic)
        return {"topic": topic, "source": "topic-bank", "category": cat}
    except Exception as exc:                            # noqa: BLE001
        log.warning("Topic bank read failed (%s); using trend/fallback.", exc)
        return fetch_trending_topic(cfg)


def fetch_trending_topic(cfg):
    """
    Pick the day's topic for a HINDI general-knowledge facts channel.

    Most of the time we use an evergreen GK fact topic (the channel's core),
    and occasionally (TREND_RATIO) a live trend for freshness. A live trend is
    sourced from YouTube trending India (needs cfg["youtube_api_key"]) or, failing
    that, Google Trends RSS. Never raises.
    """
    import random

    trend_ratio = float(cfg.get("trend_ratio", TREND_RATIO))
    if random.random() < trend_ratio:
        # ---- Occasionally: a live trend ------------------------------- #
        try:
            topic = _fetch_youtube_trend(cfg)
            if topic:
                log.info("Live trending topic (YouTube): %s", topic)
                return {"topic": topic, "source": "youtube-trending"}
        except Exception as exc:                           # noqa: BLE001
            log.warning("YouTube trending fetch failed (%s).", exc)
        try:
            topic = _fetch_google_trend()
            if topic:
                log.info("Live trending topic (Google Trends): %s", topic)
                return {"topic": topic, "source": "google-trends-rss"}
        except Exception as exc:                           # noqa: BLE001
            log.warning("Google Trends fetch failed (%s).", exc)

    # ---- Default: evergreen GK fact topic ----------------------------- #
    fb = random.choice(FALLBACK_TOPICS).copy()
    fb["source"] = "gk-evergreen"
    log.info("Evergreen GK topic: %s", fb["topic"])
    return fb


# Low-value queries/categories that make poor educational Shorts.
_TREND_SKIP = ("weather", "मौसम", "हवामान", "હવામાન", "forecast", "rainfall",
               "monsoon", "result", "exam", "admit card", "login", "price",
               "petrol", "diesel", "gold rate", "score", "live score", "vs")


def _fetch_youtube_trend(cfg):
    """Top India YouTube trending video title (skips music). None if no key."""
    import requests
    import random

    key = (cfg.get("youtube_api_key") or "").strip()
    if not key:
        return None
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet", "chart": "mostPopular", "regionCode": "IN",
            "maxResults": 30, "key": key,
        },
        timeout=20,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    titles = []
    for it in items:
        snip = it.get("snippet", {})
        cat = snip.get("categoryId")
        title = (snip.get("title") or "").strip()
        # Skip Music (10) and obvious low-value titles.
        if cat == "10" or not title:
            continue
        if any(s in title.lower() for s in _TREND_SKIP):
            continue
        titles.append(title)
    if not titles:
        return None
    return random.choice(titles[:15])


def _fetch_google_trend():
    """Top India Google daily search trend (filtered). None if unavailable."""
    import requests
    import random
    import xml.etree.ElementTree as ET

    r = requests.get("https://trends.google.com/trending/rss?geo=IN",
                     timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    topics = [(item.findtext("title") or "").strip()
              for item in root.iter("item")]
    topics = [t for t in topics if t]
    if not topics:
        return None
    good = [t for t in topics if not any(s in t.lower() for s in _TREND_SKIP)]
    pool = good or topics
    return random.choice(pool[:8])


# --------------------------------------------------------------------------- #
#  STEP 1b — SEO/content generation (Claude, claude-opus-4-8, JSON output)
# --------------------------------------------------------------------------- #
def _extract_json(text):
    """Return the first {...} JSON object found in `text`, ignoring ``` fences."""
    text = text.strip()
    if text.startswith("```"):
        # strip a leading ```json / ``` fence and the trailing ```
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model response.")
    return text[start:end + 1]


def generate_content_package(cfg, topic):
    """
    Use Claude to write a HINDI kids' cartoon STORY (NITIN CARTOON ZONE style):
    a {num_scenes}-scene Pixar/Disney story with consistent characters, a moral,
    per-scene image + motion (video) prompts, plus YouTube title/description/tags.

    Returns a dict with keys:
        Title, Description, Tags, Narration (full Hindi voiceover),
        Scenes = [{image_prompt, video_prompt, narration_hi}, ...]
    Raises on unrecoverable failure.
    """
    import anthropic

    import random

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("anthropic_model", "claude-opus-4-8")
    num_scenes = int(cfg.get("num_scenes", NUM_SCENES))
    flavor = random.choice(STYLE_FLAVORS)

    system_prompt = (
        "You are a Hindi children's cartoon story writer for a YouTube Shorts "
        "channel in the style of 'NITIN CARTOON ZONE' — 3D Pixar/Disney "
        "animation, cute little Indian kids with big expressive eyes, wholesome "
        "moral or funny stories. You always respond with a single valid JSON "
        "object and nothing else."
    )
    user_prompt = f"""
Create a {num_scenes}-scene HINDI children's cartoon SHORT STORY on this topic:

TOPIC: "{topic}"

Rules:
- Style: 3D Pixar/Disney animation, vertical 9:16, bright colourful, cinematic.
- Visual mood for THIS video (use it in every scene's image_prompt): {flavor}.
- Characters: pick a FRESH, DIFFERENT and fun main character for THIS story —
  vary it every time (e.g. a cute animal like a fox/panda/elephant/monkey, a
  quirky bird, a friendly robot, a talking fruit/vegetable, or an Indian kid) —
  so every video looks new and distinct. Give it big expressive eyes and lots of
  personality. Define each main character ONCE with a detailed look (species/age,
  clothes, colours, features) and REUSE that exact description in EVERY scene's
  image_prompt so the character stays consistent across scenes.
- Story arc across the {num_scenes} scenes: setup -> action -> a funny or
  emotional twist -> happy ending with a clear moral (seekh).
- Narration: simple, fluent, natural Hindi as a child narrator would tell it.
- Total spoken narration ~70-80 words (about 30 seconds) — keep it tight.

Return ONLY a JSON object with EXACTLY these keys:

- "Title": a HIGH-SEO, catchy Hindi/Hinglish YouTube title (under ~70 chars,
  1-2 emojis). Front-load a searchable keyword people actually type (e.g.
  "हिंदी कहानी", "moral story", "bachon ki kahani") AND a curiosity hook.
- "Description": 2-3 lines of natural Hindi. First line keyword-rich (repeat the
  main topic + words like "हिंदी कहानी", "moral story", "cartoon"), then a short
  subscribe request. Do NOT add hashtags yourself (they are added automatically).
- "Tags": array of 10-15 relevant hindi + english SEO keywords (no '#'), mixing
  the specific topic with broad terms viewers search.
- "Narration": the FULL story narration as ONE Hindi paragraph (this is the
  spoken voiceover — no scene labels, no English, no stage directions).
- "Scenes": an array of EXACTLY {num_scenes} objects, in order, each with:
    - "image_prompt": a detailed ENGLISH image prompt. It MUST start with
      "3D Pixar-style animation," include the FULL consistent character
      description(s), describe this scene, and end with
      "vertical 9:16 composition, wholesome". Make it PREMIUM and characterful:
      award-winning high-end Pixar/DreamWorks-quality 3D render, ultra-detailed
      hyper-realistic fur/skin/fabric textures, expressive charismatic faces,
      cinematic studio lighting, soft depth of field, rich vivid colours, 8K,
      like a polished animated movie poster. NO text/letters/words/numbers/logos
      anywhere in the image.
    - "video_prompt": a detailed ENGLISH motion prompt describing what the
      characters DO in this scene (gestures, actions, natural blinking, gentle
      cute movements, expressions).
    - "narration_hi": this scene's portion of the Hindi narration (a sentence
      or two). Joined in order, all narration_hi together = the full Narration.

Constraints: EXACTLY {num_scenes} scenes. Everything family-friendly and
wholesome. Output valid JSON only.
"""

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(_extract_json(raw))

    # ---- Validation / normalization -------------------------------------- #
    for key in ("Title", "Description", "Tags", "Narration", "Scenes"):
        if key not in data or not data[key]:
            raise ValueError(f"Claude response missing required key: {key}")

    data["Title"] = str(data["Title"]).strip()[:100]

    tags = data["Tags"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()]
    # SEO: merge in evergreen base tags, de-dupe (case-insensitive), cap at 15.
    seen, merged = set(), []
    for t in tags + SEO_BASE_TAGS:
        if t.lower() not in seen:
            seen.add(t.lower())
            merged.append(t)
    data["Tags"] = merged[:15]

    # SEO: append niche hashtags to the description (YouTube ignores >15 total,
    # so cap them). Skip hashtags already present.
    desc = str(data["Description"]).strip()
    have = {w.lower() for w in desc.split() if w.startswith("#")}
    add = [h for h in SEO_HASHTAGS if h.lower() not in have][:max(0, 13 - len(have))]
    if add:
        data["Description"] = desc + "\n\n" + " ".join(add)

    # Scenes -> list of {image_prompt, video_prompt, narration_hi}.
    scenes = []
    for sc in data["Scenes"]:
        if not isinstance(sc, dict):
            continue
        img = str(sc.get("image_prompt") or sc.get("visual_prompt") or "").strip()
        vid = str(sc.get("video_prompt") or "").strip() or img
        nar = str(sc.get("narration_hi") or "").strip()
        if img:
            scenes.append({"image_prompt": img, "video_prompt": vid,
                           "narration_hi": nar})
    if not scenes:
        raise ValueError("Claude response produced no usable scenes.")
    data["Scenes"] = scenes

    # If the model didn't give a combined narration, build it from the scenes.
    if not str(data.get("Narration") or "").strip():
        data["Narration"] = " ".join(s["narration_hi"] for s in scenes
                                      if s["narration_hi"]).strip()

    log.info("Story ready. Title: %s | %d scenes | narration %d chars",
             data["Title"], len(scenes), len(str(data["Narration"])))
    return data


def generate_hook(cfg, topic):
    """
    Dedicated HOOK agent — a separate, focused Claude call that writes ONE punchy
    ~3-second HINDI opening line to stop the scroll (curiosity / surprise). This
    is spoken first, before the story. Returns a string ("" on any failure, so
    the pipeline never breaks over a missing hook).
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        model = cfg.get("anthropic_model", "claude-opus-4-8")
        system = (
            "You write viral YouTube Shorts HOOKS in Hindi for a children's "
            "cartoon story channel. Reply with ONLY the hook line, nothing else."
        )
        user = (
            'Write ONE super-catchy HINDI (Devanagari) opening HOOK line for a '
            f'kids cartoon story about: "{topic}".\n'
            "Rules: max ~12 words, spoken in about 3 seconds, instant curiosity "
            "or surprise so viewers don't scroll away (a question, a \"रुको!\", "
            "a teaser). Simple Hindi a child enjoys. Reply with ONLY the hook."
        )
        resp = client.messages.create(
            model=model, max_tokens=200, system=system,
            messages=[{"role": "user", "content": user}],
        )
        hook = "".join(b.text for b in resp.content if b.type == "text").strip()
        hook = hook.splitlines()[0].strip().strip('"').strip("'") if hook else ""
        log.info("Hook agent: %s", hook)
        return hook
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Hook agent failed (%s); continuing without hook.", exc)
        return ""


# --------------------------------------------------------------------------- #
#  STEP 2a — Higgsfield render via Cloud REST API
# --------------------------------------------------------------------------- #
def render_with_higgsfield(cfg, visual_prompt, out_path):
    """
    Render a 9:16 video from `visual_prompt` via the Higgsfield Cloud REST API,
    then download the finished .mp4 to `out_path`.

    Two-step flow (Higgsfield has no direct text->video):
      1. text -> image   (Soul)   POST /v1/text2image/soul
      2. image -> video  (DoP)    POST /v1/image2video/dop
    Each step submits a job set and polls GET /v1/job-sets/{id} until done.
    Returns out_path on success; raises on failure.

    Endpoint paths / sizes / model live in HIGGSFIELD_API (top of file).
    Auth uses "KEY_ID:KEY_SECRET" from config ("higgsfield_api_key");
    get it at https://cloud.higgsfield.ai/ -> API keys.
    """
    A = HIGGSFIELD_API
    api_key = cfg.get("higgsfield_api_key", "").strip()
    if not api_key:
        raise RuntimeError(
            "higgsfield_api_key missing in config.json. Get a key at "
            "https://cloud.higgsfield.ai/ (format: KEY_ID:KEY_SECRET)."
        )
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }

    # ---- Step 1: text -> image (Soul) ----------------------------------- #
    log.info("Higgsfield: step 1/2 — generating 9:16 image from prompt...")
    image_url = _hf_submit_and_wait(
        A, headers, A["text2image_path"],
        {
            "prompt": visual_prompt,
            "width_and_height": cfg.get("higgsfield_image_size", A["image_size"]),
        },
        kind="image",
    )
    log.info("Higgsfield: image ready -> %s", image_url)

    # ---- Step 2: image -> video (DoP) ----------------------------------- #
    log.info("Higgsfield: step 2/2 — animating image into video...")
    video_url = _hf_submit_and_wait(
        A, headers, A["image2video_path"],
        {
            "prompt": visual_prompt,
            "model": cfg.get("higgsfield_model", A["video_model"]),
            "input_images": [{"type": "image_url", "image_url": image_url}],
        },
        kind="video",
    )

    # ---- Download ------------------------------------------------------- #
    log.info("Higgsfield: downloading rendered video...")
    _download_url(video_url, out_path)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("Higgsfield download produced an empty file.")

    log.info("Higgsfield: render saved -> %s", out_path)
    return out_path


def _hf_submit_and_wait(A, headers, path, params, kind):
    """
    Submit one Higgsfield job ({"params": params}) to `path`, poll the job set
    until it completes, and return the resulting media URL. `kind` is "image"
    or "video" (used only for logging + preferring the right file extension).
    """
    import requests

    submit_url = A["base_url"] + path
    resp = requests.post(submit_url, headers=headers,
                         json={"params": params}, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Higgsfield {kind} submit failed [{resp.status_code}]: "
            f"{resp.text[:300]}"
        )
    job_id = _hf_extract_request_id(resp.json())
    if not job_id:
        raise RuntimeError(
            f"Higgsfield {kind} submit returned no job id: {resp.text[:300]}"
        )
    log.info("Higgsfield: %s job accepted (id=%s); polling...", kind, job_id)

    status_url = A["base_url"] + A["status_path"].format(request_id=job_id)
    deadline = time.time() + A["poll_timeout_s"]
    while time.time() < deadline:
        time.sleep(A["poll_interval_s"])
        s = requests.get(status_url, headers=headers, timeout=60)
        if s.status_code >= 400:
            log.warning("Higgsfield: %s poll [%s]: %s",
                        kind, s.status_code, s.text[:200])
            continue
        data = s.json()
        status = _hf_overall_status(data)
        log.info("Higgsfield: %s status=%s", kind, status or "(unknown)")
        if status in ("completed", "succeeded", "success", "done"):
            url = _hf_extract_media_url(data, kind)
            if not url:
                raise RuntimeError(
                    f"Higgsfield {kind} completed but no media URL found: "
                    f"{s.text[:300]}"
                )
            return url
        if status in ("failed", "error", "nsfw", "canceled", "cancelled"):
            raise RuntimeError(f"Higgsfield {kind} {status}: {s.text[:300]}")
    raise RuntimeError(
        f"Higgsfield {kind} render timed out after {A['poll_timeout_s']}s."
    )


# --- Small, defensive helpers for the Higgsfield JSON (schema-tolerant) ---- #
def _hf_dig(obj, *keys):
    """Return the first present key from a dict (case-insensitive), else None."""
    if not isinstance(obj, dict):
        return None
    lowered = {k.lower(): v for k, v in obj.items()}
    for k in keys:
        if k.lower() in lowered and lowered[k.lower()] is not None:
            return lowered[k.lower()]
    return None


def _hf_extract_request_id(data):
    """Pull the job/request id out of a submit response, however it's nested."""
    if isinstance(data, dict):
        rid = _hf_dig(data, "request_id", "id", "job_id", "requestId", "jobId")
        if rid:
            return rid
        for nest in ("data", "request", "job", "result"):
            inner = data.get(nest) if nest in data else None
            rid = _hf_extract_request_id(inner)
            if rid:
                return rid
    return None


def _hf_overall_status(data):
    """
    Determine the job set's overall status. Higgsfield may report a single
    top-level "status" or per-job statuses under "jobs"[]. Returns a lowercase
    string ("completed", "in_progress", "failed", ...) or "".
    """
    if not isinstance(data, dict):
        return ""
    top = _hf_dig(data, "status", "state")
    if isinstance(top, str):
        return top.lower()
    jobs = _hf_dig(data, "jobs")
    if isinstance(jobs, list) and jobs:
        bad = ("failed", "error", "nsfw", "canceled", "cancelled")
        good = ("completed", "succeeded", "success", "done")
        statuses = [str(_hf_dig(j, "status", "state") or "").lower()
                    for j in jobs if isinstance(j, dict)]
        statuses = [s for s in statuses if s]
        if statuses:
            for s in statuses:
                if s in bad:
                    return s
            if all(s in good for s in statuses):
                return "completed"
            return statuses[0]
    return ""


def _hf_extract_media_url(data, kind):
    """Find the finished media URL in a completed payload (image or video)."""
    # Common shapes: jobs[].results.raw.url, results[0].url, output.url, url
    candidates = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and k.lower() in (
                    "url", "video_url", "videourl", "image_url", "imageurl",
                    "download_url", "downloadurl",
                ) and v.startswith("http"):
                    candidates.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(data)
    if not candidates:
        return None
    exts = ((".mp4", ".mov", ".webm") if kind == "video"
            else (".jpg", ".jpeg", ".png", ".webp"))
    typed = [c for c in candidates
             if c.lower().split("?")[0].endswith(exts)]
    pool = typed or candidates
    # Higgsfield returns both a full-quality "raw" url and a minified "_min"
    # preview; prefer the full-resolution one.
    full = [c for c in pool if "_min" not in c.lower()]
    return (full or pool)[0]


def _download_url(url, out_path):
    """Stream a remote URL to disk."""
    import requests
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


def _free_image_url(prompt, seed=None):
    """Public 9:16 image URL from the FREE Pollinations.ai (Flux) generator —
    no key, no cost. Deterministic per (prompt, seed)."""
    from urllib.parse import quote
    import random
    if seed is None:
        seed = random.randint(1, 9_000_000)
    return ("https://image.pollinations.ai/prompt/" + quote(prompt)
            + f"?width=1080&height=1920&nologo=true&model=flux&seed={seed}")


def _warm_free_image(url):
    """Fetch the free image once so it is generated + cached before Higgsfield
    pulls it. Returns True if it looks like a valid image."""
    import requests
    r = requests.get(url, timeout=120)
    return r.status_code == 200 and len(r.content) > 5000


# --------------------------------------------------------------------------- #
#  STEP 2b — Hindi voiceover (free edge-tts)
# --------------------------------------------------------------------------- #
def synthesize_hindi_voice(cfg, narration, out_path):
    """Generate a Hindi voiceover mp3 from `narration` (free Microsoft TTS).
    Returns (out_path, duration_seconds)."""
    import asyncio
    import edge_tts

    voice = cfg.get("tts_voice", TTS_VOICE)
    rate = cfg.get("tts_rate", TTS_RATE)
    pitch = cfg.get("tts_pitch", TTS_PITCH)
    log.info("TTS: generating Hindi voiceover (%s, rate %s, pitch %s)...",
             voice, rate, pitch)

    async def _run():
        await edge_tts.Communicate(
            narration, voice, rate=rate, pitch=pitch).save(out_path)

    asyncio.run(_run())
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("TTS produced an empty audio file.")

    from moviepy import AudioFileClip
    a = AudioFileClip(out_path)
    dur = float(a.duration)
    a.close()
    log.info("TTS: voiceover ready -> %s (%.1fs)", os.path.basename(out_path), dur)
    return out_path, dur


# --------------------------------------------------------------------------- #
#  STEP 2c — Scene images (Higgsfield Soul, 3D Pixar style)
# --------------------------------------------------------------------------- #
def generate_scene_image(cfg, visual_prompt, out_path):
    """Render one 9:16 image and download it to out_path. Uses the FREE
    Pollinations generator when image_provider='pollinations' (default), falling
    back to Higgsfield Soul on failure."""
    provider = cfg.get("image_provider", "pollinations")
    if provider in ("pollinations", "free"):
        try:
            _download_url(_free_image_url(visual_prompt), out_path)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
                return out_path
            log.warning("Free image looked empty; falling back to Higgsfield.")
        except Exception as exc:                              # noqa: BLE001
            log.warning("Free image failed (%s); falling back to Higgsfield.",
                        str(exc)[:120])

    A = HIGGSFIELD_API
    api_key = cfg.get("higgsfield_api_key", "").strip()
    if not api_key:
        raise RuntimeError(
            "higgsfield_api_key missing in config.json (cloud.higgsfield.ai)."
        )
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }
    url = _hf_submit_and_wait(
        A, headers, A["text2image_path"],
        {
            "prompt": visual_prompt,
            "width_and_height": cfg.get("higgsfield_image_size", A["image_size"]),
        },
        kind="image",
    )
    _download_url(url, out_path)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("Scene image download produced an empty file.")
    return out_path


def generate_scene_clip(cfg, image_prompt, video_prompt, out_path):
    """
    Render one ANIMATED 9:16 clip for a scene: Higgsfield Soul image (from
    image_prompt) -> DoP image2video (motion from video_prompt). The characters
    actually move. Downloads the mp4 to out_path.
    """
    A = HIGGSFIELD_API
    api_key = cfg.get("higgsfield_api_key", "").strip()
    if not api_key:
        raise RuntimeError("higgsfield_api_key missing in config.json.")
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }
    # 1) get the scene image. FREE Pollinations by default (saves Higgsfield
    #    image credits); Higgsfield DoP then animates that image URL directly.
    image_url = None
    if cfg.get("image_provider", "pollinations") in ("pollinations", "free"):
        try:
            u = _free_image_url(image_prompt)
            if _warm_free_image(u):          # generate + cache before DoP pulls it
                image_url = u
        except Exception as exc:                              # noqa: BLE001
            log.warning("Free image failed (%s); using Higgsfield Soul.",
                        str(exc)[:120])
    if not image_url:                        # fallback: paid Higgsfield Soul
        image_url = _hf_submit_and_wait(
            A, headers, A["text2image_path"],
            {
                "prompt": image_prompt,
                "width_and_height": cfg.get("higgsfield_image_size",
                                            A["image_size"]),
            },
            kind="image",
        )
    # 2) image -> video (DoP), motion from the scene's video_prompt
    video_url = _hf_submit_and_wait(
        A, headers, A["image2video_path"],
        {
            "prompt": video_prompt or image_prompt,
            "model": cfg.get("higgsfield_model", A["video_model"]),
            "input_images": [{"type": "image_url", "image_url": image_url}],
        },
        kind="video",
    )
    _download_url(video_url, out_path)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("Scene clip download produced an empty file.")
    return out_path


def render_scene_assets(cfg, scenes, run_id, animate):
    """
    Generate one asset per scene — an animated clip (animate=True) or a still
    image (animate=False). `scenes` is a list of dicts with image_prompt /
    video_prompt. Resilient: each scene is retried once with a wholesome
    reworded image prompt (for NSFW false-positives) then skipped if it still
    fails. Returns the list of file paths (raises only if NONE succeed).
    """
    paths = []
    total = len(scenes)
    kind = "clip" if animate else "image"
    ext = "mp4" if animate else "png"
    for i, sc in enumerate(scenes, 1):
        img = sc.get("image_prompt", "")
        vid = sc.get("video_prompt", "") or img
        out = os.path.join(WORK_DIR, f"scene_{run_id}_{i}.{ext}")
        for attempt, ip in enumerate((img, _safe_prompt(img)), 1):
            try:
                log.info("Higgsfield: scene %s %d/%d (try %d) ...",
                         kind, i, total, attempt)
                if animate:
                    generate_scene_clip(cfg, ip, vid, out)
                else:
                    generate_scene_image(cfg, ip, out)
                paths.append(out)
                break
            except Exception as exc:                          # noqa: BLE001
                log.warning("Scene %d try %d failed (%s).", i, attempt,
                            str(exc)[:140])
        else:
            log.warning("Scene %d skipped after retries.", i)

    if not paths:
        raise RuntimeError("All scene assets failed to render.")
    log.info("Rendered %d/%d scene %ss.", len(paths), total, kind)
    return paths


def _safe_prompt(prompt):
    """
    Reword a scene image prompt to dodge over-eager NSFW filters WITHOUT losing
    the cute kids (they are the whole point of the channel) — just prepend strong
    wholesome/stylised framing so the filter reads it as harmless cartoon art.
    """
    return ("Wholesome family-friendly 3D Pixar/Disney CARTOON illustration, "
            "highly stylised non-realistic cute characters, fully clothed, "
            "innocent and heartwarming, no realism. " + prompt)


# --------------------------------------------------------------------------- #
#  STEP 2d — Assemble the video (images + Ken Burns + voiceover + music)
# --------------------------------------------------------------------------- #
def assemble_video(image_paths, voice_path, voice_dur, out_path):
    """
    Build a 9:16 slideshow with ffmpeg (fast, native): each image gets a slow
    Ken Burns PAN (via the crop filter on a pre-upscaled still) for an equal
    slice of the voiceover; the Hindi voiceover plays on top with optional
    royalty-free background music ducked underneath.

    Uses ffmpeg directly because per-frame compositing in MoviePy is far too
    slow for an automated pipeline (~minutes vs ~an hour).
    """
    import subprocess
    import glob
    import random
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    W, H = 1080, 1920
    SW, SH = 1296, 2304            # 1.2x upscale -> headroom for the pan
    fps = 30
    n = max(1, len(image_paths))
    per = (voice_dur + 1.0) / n    # hold each image a touch past the VO end

    tracks = (glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))
              + glob.glob(os.path.join(MUSIC_DIR, "*.wav"))
              + glob.glob(os.path.join(MUSIC_DIR, "*.m4a")))
    music = random.choice(tracks) if (tracks and MUSIC_VOLUME > 0) else None

    cmd = [ffmpeg, "-y"]
    for img in image_paths:
        cmd += ["-loop", "1", "-t", f"{per:.3f}", "-i", img]
    cmd += ["-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))

    # Per-scene Ken Burns pan (alternating diagonal) via animated crop.
    fc = []
    for i in range(n):
        if i % 2 == 0:
            xex, yex = f"(in_w-out_w)*t/{per:.3f}", f"(in_h-out_h)*t/{per:.3f}"
        else:
            xex = f"(in_w-out_w)*(1-t/{per:.3f})"
            yex = f"(in_h-out_h)*(1-t/{per:.3f})"
        fc.append(
            f"[{i}:v]scale={SW}:{SH},crop={W}:{H}:{xex}:{yex},"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
    fc.append("".join(f"[v{i}]" for i in range(n)) +
              f"concat=n={n}:v=1:a=0[vout]")

    voice_idx = n
    if music:
        fc.append(f"[{n + 1}:a]volume={MUSIC_VOLUME},aloop=loop=-1:size=2000000000[mus]")
        fc.append(f"[{voice_idx}:a][mus]amix=inputs=2:duration=first:"
                  f"dropout_transition=0[aout]")
        amap = "[aout]"
    else:
        log.warning("No music in %s; exporting with voiceover only.", MUSIC_DIR)
        amap = f"{voice_idx}:a"

    cmd += [
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", amap,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-r", str(fps), "-c:a", "aac", "-b:a", "192k", out_path,
    ]

    log.info("Assembling video with ffmpeg (%d scenes, %.1fs each)...", n, per)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-800:]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("ffmpeg produced an empty video file.")
    log.info("Final video rendered -> %s", out_path)
    return out_path


def _run_ffmpeg(cmd):
    """Run an ffmpeg command, raising with the stderr tail on failure."""
    import subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-700:]}")


def assemble_animated_video(clip_paths, voice_path, voice_dur, out_path):
    """
    Stitch ANIMATED scene clips into the final 9:16 video. Each clip is processed
    ONE AT A TIME (memory-safe): normalized to 1080x1920 and slowed down (FORWARD
    only — no reverse/boomerang) to fill its equal slice of the voiceover, so the
    motion always plays forward and never runs backwards. The segments are
    concatenated and the Hindi voiceover is laid on top with optional background
    music ducked underneath.
    """
    import glob
    import random
    import imageio_ffmpeg
    from moviepy import VideoFileClip

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    W, H, fps = 1080, 1920, 30
    n = max(1, len(clip_paths))
    per = (voice_dur + 1.0) / n
    wd = os.path.dirname(out_path) or "."
    tmp = []

    log.info("Stitching %d animated clips (%.1fs each)...", n, per)
    segs = []
    for i, clip in enumerate(clip_paths):
        seg = os.path.join(wd, f"_seg_{i}.mp4")
        tmp.append(seg)
        # Slow the clip (forward only) so it lasts `per` seconds — never reverse.
        try:
            _v = VideoFileClip(clip)
            dur = float(_v.duration)
            _v.close()
        except Exception:                                     # noqa: BLE001
            dur = 5.0
        factor = max(1.0, per / max(0.1, dur))                # >=1 -> slow, never speed up
        _run_ffmpeg([
            ffmpeg, "-y", "-i", clip, "-an", "-filter_complex",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p,"
            f"setpts={factor:.4f}*PTS[v]",
            "-map", "[v]", "-t", f"{per:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-r", str(fps), seg,
        ])
        segs.append(seg)

    # 3) concat segments + Hindi voiceover (+ ducked music) -> final
    listfile = os.path.join(wd, "_concat_list.txt")
    tmp.append(listfile)
    with open(listfile, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"file '{os.path.abspath(s)}'\n")

    tracks = (glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))
              + glob.glob(os.path.join(MUSIC_DIR, "*.wav"))
              + glob.glob(os.path.join(MUSIC_DIR, "*.m4a")))
    music = random.choice(tracks) if (tracks and MUSIC_VOLUME > 0) else None

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))
        cmd += ["-filter_complex",
                f"[2:a]volume={MUSIC_VOLUME},aloop=loop=-1:size=2000000000[mus];"
                "[1:a][mus]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map", "0:v", "-map", "[aout]"]
    else:
        log.warning("No music in %s; voiceover only.", MUSIC_DIR)
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path]

    try:
        _run_ffmpeg(cmd)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("ffmpeg produced an empty video file.")
        log.info("Final animated video rendered -> %s", out_path)
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
    return out_path


# --------------------------------------------------------------------------- #
#  STEP 3 — YouTube upload (persistent token.json)
# --------------------------------------------------------------------------- #
def get_youtube_service():
    """Build an authorized YouTube API client using a persistent token.json."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(
            TOKEN_PATH, YOUTUBE_UPLOAD_SCOPE)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                log.info("YouTube: refreshing expired token...")
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:                          # noqa: BLE001
                # Token revoked (e.g. after enabling 2-Step Verification) —
                # fall back to a fresh consent flow instead of crashing.
                log.warning("YouTube: token refresh failed (%s); "
                            "re-authenticating...", exc)
                creds = None

        if not refreshed:
            if not os.path.exists(CLIENT_SECRET):
                raise FileNotFoundError(
                    f"client_secret.json not found at {CLIENT_SECRET}")
            log.info("YouTube: starting OAuth consent flow (browser)...")
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET, YOUTUBE_UPLOAD_SCOPE)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(cfg, video_path, seo):
    """Upload `video_path` and return the public watch URL."""
    from googleapiclient.http import MediaFileUpload

    youtube = get_youtube_service()
    privacy = cfg.get("youtube_privacy_status", "public")

    body = {
        "snippet": {
            "title": seo["Title"],
            "description": seo["Description"],
            "tags": seo["Tags"],
            "categoryId": "27",          # 27 = Education (safe default)
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True,
                            mimetype="video/mp4")
    log.info("YouTube: uploading (%s)...", privacy)
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("YouTube: upload progress %d%%",
                     int(status.progress() * 100))

    video_id = response["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("YouTube: upload complete -> %s", url)

    # SEO: set a custom thumbnail from a frame of the video (best-effort).
    _set_thumbnail(youtube, video_id, video_path)
    return url


def _set_thumbnail(youtube, video_id, video_path):
    """Grab a frame from the video and set it as the custom thumbnail.
    Best-effort: needs a phone-verified channel; never breaks the pipeline."""
    try:
        import imageio_ffmpeg
        from googleapiclient.http import MediaFileUpload
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        thumb = os.path.join(WORK_DIR, f"thumb_{video_id}.jpg")
        _run_ffmpeg([ffmpeg, "-y", "-ss", "1.5", "-i", video_path,
                     "-frames:v", "1", "-q:v", "2", thumb])
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumb, mimetype="image/jpeg"),
        ).execute()
        log.info("YouTube: custom thumbnail set.")
        try:
            os.remove(thumb)
        except OSError:
            pass
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Thumbnail skipped (%s). Verify the channel (phone) to "
                    "enable custom thumbnails.", exc)


# --------------------------------------------------------------------------- #
#  STEP 4 — Archive + Telegram notification
# --------------------------------------------------------------------------- #
def archive_raw_file(raw_path):
    """Move the raw Higgsfield file into the archive dir, timestamped."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(ARCHIVE_DIR, f"raw_{ts}.mp4")
        shutil.move(raw_path, dest)
        log.info("Archived raw file -> %s", dest)
        return dest
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Archiving failed: %s", exc)
        return None


def send_telegram(cfg, message):
    """Send a status report to Telegram. Never raises."""
    try:
        import requests
        token = cfg.get("telegram_bot_token")
        chat_id = cfg.get("telegram_chat_id")
        if not token or not chat_id:
            log.warning("Telegram not configured; skipping notification.")
            return
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            api,
            data={"chat_id": chat_id, "text": message,
                  "disable_web_page_preview": False},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Telegram notification sent.")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Telegram notification failed: %s", exc)


# --------------------------------------------------------------------------- #
#  Orchestration — one full pipeline run
# --------------------------------------------------------------------------- #
def run_pipeline():
    """Execute the full pipeline once. Each step is independently guarded."""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log.info("=" * 70)
    log.info("PIPELINE RUN START  (id=%s)", run_id)
    log.info("=" * 70)

    cfg = None
    pkg = None
    voice_path = os.path.join(WORK_DIR, f"voice_{run_id}.mp3")
    final_video = os.path.join(WORK_DIR, f"final_{run_id}.mp4")
    scene_paths = []

    try:
        ensure_dirs()
        cfg = load_config()
        animate = bool(cfg.get("animate_scenes", True))

        # 1. Topic + story + hook ---------------------------------------- #
        trend = pick_topic(cfg)
        pkg = generate_content_package(cfg, trend["topic"])
        hook = generate_hook(cfg, trend["topic"])
        if hook:
            pkg["Narration"] = f"{hook} {pkg['Narration']}"
            if pkg["Scenes"]:
                first = pkg["Scenes"][0].get("narration_hi", "")
                pkg["Scenes"][0]["narration_hi"] = f"{hook} {first}".strip()

        # 2. Hindi voiceover + scene assets (animated clips or stills) --- #
        _, voice_dur = synthesize_hindi_voice(cfg, pkg["Narration"], voice_path)
        scene_paths = render_scene_assets(cfg, pkg["Scenes"], run_id, animate)

        # 3. Assemble the 40-50s video ----------------------------------- #
        if animate:
            assemble_animated_video(scene_paths, voice_path, voice_dur,
                                    final_video)
        else:
            assemble_video(scene_paths, voice_path, voice_dur, final_video)

        # 4. Upload ------------------------------------------------------ #
        url = upload_to_youtube(cfg, final_video, pkg)

        # 5. Notify ------------------------------------------------------ #
        send_telegram(
            cfg,
            "✅ Shorts uploaded successfully!\n\n"
            f"🎬 Title: {pkg['Title']}\n"
            f"📈 Topic: {trend['topic']} ({trend['source']})\n"
            f"🔗 {url}",
        )
        # tidy the work dir (keep nothing transient)
        for p in scene_paths + [voice_path, final_video]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

        log.info("PIPELINE RUN SUCCESS (id=%s)", run_id)

    except Exception as exc:                                  # noqa: BLE001
        log.error("PIPELINE RUN FAILED (id=%s): %s", run_id, exc)
        log.error(traceback.format_exc())
        if cfg is not None:
            title = pkg["Title"] if pkg else "(content not generated)"
            send_telegram(
                cfg,
                f"❌ Shorts pipeline FAILED (id={run_id})\n"
                f"Step content: {title}\n"
                f"Error: {exc}",
            )


# --------------------------------------------------------------------------- #
#  Scheduler — twice daily at 08:00 & 18:00 IST
# --------------------------------------------------------------------------- #
def main():
    import schedule

    ensure_dirs()
    log.info("Ultimate Shorts Agent starting up.")

    # `schedule` fires in local time. We convert 08:00/18:00 IST to the
    # machine's local clock so it is correct regardless of server timezone.
    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        local_tz = datetime.now().astimezone().tzinfo

        def ist_to_local(hh, mm):
            today = datetime.now(ist).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            return today.astimezone(local_tz).strftime("%H:%M")

        slot_morning = ist_to_local(8, 0)
        slot_evening = ist_to_local(18, 0)
    except Exception:                                        # noqa: BLE001
        # If pytz is unavailable assume the host is already on IST.
        slot_morning, slot_evening = "08:00", "18:00"

    schedule.every().day.at(slot_morning).do(run_pipeline)
    schedule.every().day.at(slot_evening).do(run_pipeline)
    log.info("Scheduled daily runs at local %s and %s (= 08:00 & 18:00 IST).",
             slot_morning, slot_evening)

    # Optional immediate kickoff for testing.
    try:
        cfg = load_config()
        if cfg.get("run_immediately_on_start"):
            log.info("run_immediately_on_start=true -> running now.")
            run_pipeline()
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Startup config check skipped: %s", exc)

    log.info("Entering scheduler loop. Press Ctrl+C to stop.")
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("Shutdown requested. Goodbye.")
            break
        except Exception as exc:                             # noqa: BLE001
            log.error("Scheduler loop error: %s", exc)
            time.sleep(30)


if __name__ == "__main__":
    # CLI helpers:  `python ultimate_shorts_agent.py run`  -> one-shot run now
    #               `python ultimate_shorts_agent.py`      -> start scheduler
    if len(sys.argv) > 1 and sys.argv[1].lower() == "run":
        run_pipeline()
    else:
        main()
