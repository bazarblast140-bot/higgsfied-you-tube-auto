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
                google-auth-oauthlib google-auth-httplib2 schedule requests pytz yt-dlp

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
NEWS_TOPIC_BANK = os.path.join(BASE_DIR, "news_topic_bank.json") # news topics
SLIDESHOW_BANK = os.path.join(BASE_DIR, "slideshow_topics.json")  # sabzi drama/horror

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
MUSIC_VOLUME = 0.08                     # background music level for SHORTS (0.08 = subtle)

# Slideshow long-video settings (sabzi/veggie emotional + horror stories).
NUM_SLIDESHOW_SCENES = 24               # stills across a ~5 min horror video
SLIDESHOW_VOICE      = "hi-IN-MadhurNeural"   # mature male storyteller voice
SLIDESHOW_RATE       = "+0%"            # natural storytelling pace
SLIDESHOW_PITCH      = "+0Hz"
SLIDESHOW_MUSIC_VOLUME = 0.09           # under the voiceover; 0.18 drowned the narration
FREE_IMAGE_DELAY_S   = 3                # gap between free-image calls (avoid rate-limit)

# Reach / SEO — niche hashtags appended to every description, and base keywords
# merged into tags so YouTube surfaces the video in search + suggested.
SEO_HASHTAGS = [
    "#Shorts", "#sabjikahani", "#aalukahani", "#mulididi", "#kahani",
    "#emotionalstory", "#sabzicartoon", "#hindikahani", "#vegetablecartoon",
    "#gaonkikahani", "#moralstory", "#dilchhukahani",
]
SEO_BASE_TAGS = [
    "sabji kahani", "aalu muli ki kahani", "muli didi", "vegetable cartoon hindi",
    "emotional story hindi", "sabzi cartoon", "gaon ki kahani", "hindi kahani",
    "dil chhu lene wali kahani", "moral story hindi",
]

# The channel's SIGNATURE RECURRING CAST — starred (Muli Didi) in EVERY short so
# the channel has one familiar face, with fixed looks so they stay identical
# across scenes AND across videos.
# Full coverage is spelled out per character on purpose. Left vaguer ("wearing a
# patched kurta", "wearing a saree") the image models rendered a bare potato body
# and a drape that left the radish woman's pale body bare at the shoulders and
# arms — off-model, and enough to trip downstream NSFW filters on innocent beats.
MULI_CAST = (
    "MULI DIDI (the heroine — she MUST appear in every story): a kind, graceful "
    "female radish woman with a smooth white radish body, a big fresh green "
    "leafy top on her head, large expressive emotional eyes, FULLY CLOTHED in a "
    "pink Indian saree over a full-sleeved blouse covering her shoulders, arms "
    "and whole body. "
    "GARIB AALU (her husband): a poor, humble potato man with a round "
    "light-brown potato body, sad kind eyes, FULLY CLOTHED in a patched beige "
    "kurta and dhoti, barefoot. "
    "AMEER BAINGAN (the rich villain): a shiny purple brinjal man FULLY CLOTHED "
    "in a black suit, with black sunglasses and a thick gold chain. "
    "MULI'S CHILDREN: two small cute radish kids with little green leafy tops, "
    "fully clothed in simple beige village clothes. "
    "TAMATAR: a cheerful red tomato man in a green kurta. "
    "LAHSUN: a wise old white garlic man with a long white beard, in a white "
    "kurta. "
    "MIRCH: a fiery green chilli woman in a yellow saree. "
    "PYAAZ: a purple onion man in an orange kurta. "
    "Everyone is fully clothed in Indian village clothes."
)

# SEO for the faceless news shorts.
NEWS_HASHTAGS = [
    "#NewsShorts", "#HindiNews", "#BreakingNews", "#CurrentAffairs", "#DailyNews",
    "#TechNews", "#ScienceNews", "#HealthUpdate", "#IndiaNews", "#GlobalNews",
    "#Samachar", "#LatestNews",
]
NEWS_BASE_TAGS = [
    "hindi news", "breaking news", "current affairs", "daily news", "samachar",
    "latest news", "tech news hindi", "science news hindi", "health news hindi",
    "india news", "global news", "shorts news", "aaj ki khabar",
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

# Different HOOK openings so every Short starts differently (NOT always "रुको").
# The hook agent picks one at random each video for variety.
HOOK_STYLES = [
    "a surprising question that makes the viewer curious",
    "a shocking or unbelievable one-line fact/claim",
    "a 'क्या तुम जानते हो...?' style teaser",
    "a mini cliffhanger about what happens next",
    "a playful challenge or dare to keep watching",
    "an emotional/heart-tugging opening line",
    "a funny or silly attention-grabbing line",
    "a 'सोचो ज़रा...' style thought-provoker",
    "directly addressing the viewer ('तुम्हें यकीन नहीं होगा...')",
    "a countdown/teaser hinting at a twist coming",
]

# SEO for the sabzi/veggie slideshow long videos.
# The suffix appended to every slideshow title, matching the horror-channel
# convention: "<Hindi hook> | <English> | Chudail Ki Kahaniya | Bhoot Kahaniya".
SLIDESHOW_TITLE_SUFFIX = "Chudail Ki Kahaniya | Bhoot Kahaniya | Horror Story"

# Wrapped around every slideshow image prompt to push Flux toward a sharp,
# photoreal, consistent horror frame instead of a soft cartoon one.
SLIDESHOW_IMG_STYLE = (
    "Ultra-realistic cinematic horror film still, photorealistic, shot on a "
    "cinema camera, highly detailed faces, dramatic moody lighting, sharp "
    "focus, depth of field, 8k, atmospheric and terrifying. "
)
SLIDESHOW_IMG_QUALITY = (
    " Masterpiece quality, hyper-detailed, consistent character design across "
    "the whole story. Spooky and eerie but NOT graphic: absolutely no blood, no "
    "gore, no wounds, no zombie — a ghostly, atmospheric scare suitable for a "
    "family audience. Absolutely no text, no letters, no watermark, no logo."
)
SLIDESHOW_HASHTAGS = [
    "#horrorstory", "#chudail", "#bhootkikahani", "#hindihorror", "#kahani",
    "#horror", "#chudailkikahaniya", "#bhootkahaniya", "#darrkahani",
    "#hindikahani", "#gaonkikahani", "#scarystories",
]
SLIDESHOW_TAGS = [
    "chudail ki kahani", "bhoot ki kahani", "horror story hindi", "hindi horror",
    "chudail", "bhoot kahaniya", "darr ki kahani", "gaon ki kahani",
    "hindi kahani", "scary stories hindi", "horror stories", "dayan ki kahani",
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


def pick_news_topic(cfg):
    """
    Pick an UNUSED news topic from news_topic_bank.json, mark it used, and
    return {"topic","source","category"}. Resets the used list once every topic
    has been used. Falls back to a live trend / evergreen topic if the bank is
    missing or unreadable.
    """
    import random
    try:
        with open(NEWS_TOPIC_BANK, "r", encoding="utf-8") as f:
            bank = json.load(f)
        cats = bank.get("categories", {})
        used = set(bank.get("used_topics", []))
        all_topics = [(c, t) for c, lst in cats.items() for t in lst]
        pool = [(c, t) for c, t in all_topics if t not in used]
        if not pool:                                   # everything used -> reset
            log.info("News topic bank exhausted; resetting used_topics.")
            used = set()
            pool = all_topics
        if not pool:
            raise ValueError("news_topic_bank.json has no topics")
        cat, topic = random.choice(pool)
        used.add(topic)
        bank["used_topics"] = sorted(used)
        with open(NEWS_TOPIC_BANK, "w", encoding="utf-8") as f:
            json.dump(bank, f, ensure_ascii=False, indent=2)
        log.info("News topic (%s): %s", cat, topic)
        return {"topic": topic, "source": "news-topic-bank", "category": cat}
    except Exception as exc:                            # noqa: BLE001
        log.warning("News topic bank read failed (%s); using trend/fallback.", exc)
        return fetch_trending_topic(cfg) # Fallback to general trending if news bank fails


def pick_slideshow_topic(cfg):
    """
    Pick an UNUSED sabzi/veggie slideshow premise from slideshow_topics.json and
    its mood ("emotional" or "scary"), mark it used, reset when all used.
    Returns {"topic","category","mood"}.
    """
    import random
    with open(SLIDESHOW_BANK, "r", encoding="utf-8") as f:
        bank = json.load(f)
    cats = bank.get("categories", {})
    used = set(bank.get("used_topics", []))
    all_topics = [(c, t) for c, lst in cats.items() for t in lst]
    pool = [(c, t) for c, t in all_topics if t not in used]
    if not pool:
        log.info("Slideshow topics exhausted; resetting used list.")
        used = set()
        pool = all_topics
    cat, topic = random.choice(pool)
    used.add(topic)
    bank["used_topics"] = sorted(used)
    with open(SLIDESHOW_BANK, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)
    mood = "scary" if cat == "horror_suspense" else "emotional"
    log.info("Slideshow topic (%s / %s): %s", cat, mood, topic)
    return {"topic": topic, "category": cat, "mood": mood}


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
        "You are the head writer for a viral HINDI 'sabzi/veggie cartoon' "
        "emotional-drama YouTube channel (like the hugely popular Aalu-Muli / "
        "Sabji Cartoon channels). Every story stars the SAME recurring cast of "
        "vegetable characters living as people in an Indian village, led by MULI "
        "DIDI. The stories are emotional family dramas — poverty, sacrifice, "
        "betrayal, love, respect — always ending with a clear moral, and always "
        "family-friendly. You always respond with a single valid JSON object and "
        "nothing else."
    )
    user_prompt = f"""
Write a {num_scenes}-scene HINDI emotional 'sabzi cartoon' SHORT STORY based on
this premise:

PREMISE: "{topic}"

RECURRING CAST — use these EXACT characters and looks (do not invent new looks):
{MULI_CAST}

Rules:
- MULI DIDI must be central to the story. Use the fixed cast above; every scene's
  image_prompt must copy the FULL look (verbatim) of whichever characters appear,
  so they stay identical across scenes and across videos.
- Style: cinematic 3D Pixar-style cartoon, vertical 9:16, Indian village, warm
  emotional lighting. Visual mood for THIS video: {flavor}.
- Story arc across the {num_scenes} scenes: setup -> an emotional turn (sacrifice,
  betrayal, hardship or a test) -> a moving twist -> a heartfelt ending with a
  clear moral (seekh). Emotional and gripping, but family-safe.
- SCENE 1 is the HOOK: the most dramatic/emotional moment, to stop the scroll.
- Narration: fluent, natural, emotional Hindi storytelling. Total ~100-120 words
  (about 40-45 seconds) so the story is clear, not rushed.

Return ONLY a JSON object with EXACTLY these keys:

- "Title": a HIGH-SEO, dramatic Hindi/Hinglish YouTube title (under ~90 chars,
  1-2 emojis), in the viral sabzi-cartoon style — front-load a curiosity hook
  and searchable keywords (e.g. "मूली", "गरीब आलू", "sabji kahani", "emotional
  story"). Example shape: "😢 गरीब आलू की पत्नी मूली ने दिया धोखा | Sabji Kahani".
- "Description": 2-3 lines of emotional Hindi. First line keyword-rich, then a
  subscribe request. Do NOT add hashtags yourself (added automatically).
- "Tags": array of 10-15 relevant hindi + english SEO keywords (no '#').
- "Narration": the FULL story narration as ONE flowing Hindi paragraph (the
  spoken voiceover — no scene labels, no English, no stage directions).
- "Scenes": an array of EXACTLY {num_scenes} objects, in order, each with:
    - "image_prompt": a detailed ENGLISH image prompt. It MUST start with
      "3D Pixar-style cartoon animation," then COPY the full verbatim look of the
      character(s) in this beat from the cast above, describe this scene in an
      Indian village, and end with "vertical 9:16 composition". Make it premium:
      cute-but-expressive vegetable characters with big emotional eyes, wearing
      Indian clothes, volumetric cinematic lighting, ultra-detailed, film-quality
      Pixar/DreamWorks 3D render, 8K. NO text/letters/words/numbers/logos.
    - "video_prompt": a detailed ENGLISH motion prompt describing what the
      characters DO in this scene (gestures, emotional expressions, movement).
    - "narration_hi": this scene's portion of the Hindi narration. Joined in
      order, all narration_hi together = the full Narration.

Constraints: EXACTLY {num_scenes} scenes. Muli Didi central. Family-safe
emotional drama. Output valid JSON only.
"""

    resp = client.messages.create(
        model=model,
        max_tokens=6000,
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
        import random
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        model = cfg.get("anthropic_model", "claude-opus-4-8")
        style = random.choice(HOOK_STYLES)
        system = (
            "You write viral YouTube Shorts HOOKS in Hindi for a children's "
            "cartoon story channel. Reply with ONLY the hook line, nothing else."
        )
        user = (
            'Write ONE super-catchy HINDI (Devanagari) opening HOOK line for a '
            f'kids cartoon story about: "{topic}".\n'
            f"HOOK STYLE for THIS video: {style}.\n"
            "Rules: max ~12 words, spoken in about 3 seconds, instant curiosity "
            "or surprise so viewers don't scroll away. Simple Hindi a child "
            "enjoys. IMPORTANT: be ORIGINAL and VARY the opening every time — "
            'do NOT start with "रुको" and avoid repeating common opener words. '
            "Reply with ONLY the hook."
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


def generate_news_content_package(cfg, topic):
    """
    Use Claude to write a HINGLISH/HINDI news script for a faceless YouTube Short:
    a {num_scenes}-scene factual news report with concise narration,
    per-scene visual prompts for stock footage, plus YouTube title/description/tags.

    Returns a dict with keys:
        Title, Description, Tags, Narration (full Hinglish/Hindi voiceover),
        ScenePrompts = [visual_prompt_string, ...]
    Raises on unrecoverable failure.
    """
    import anthropic
    import random

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("anthropic_model", "claude-opus-4-8")
    num_scenes = int(cfg.get("num_scenes", NUM_SCENES)) # Reuse NUM_SCENES for shorts

    system_prompt = (
        "You are a Hinglish/Hindi news scriptwriter for a YouTube Shorts channel. "
        "Your goal is to create concise, factual, and engaging news reports "
        "suitable for a faceless video format (no presenter). "
        "You always respond with a single valid JSON object and nothing else."
    )
    user_prompt = f"""
Create a {num_scenes}-scene HINGLISH/HINDI news report SHORT on this topic:

TOPIC: "{topic}"

Rules:
- Format: Faceless news short. Visuals will be stock footage or AI-generated images, NOT animated characters.
- Narration: Concise, factual, engaging Hinglish/Hindi. Total spoken narration ~60-90 words (about 30-45 seconds).
- Story Arc: Start with a hook, present key facts, and a brief conclusion/impact.

Return ONLY a JSON object with EXACTLY these keys:

- "Title": a HIGH-SEO, catchy Hinglish/Hindi YouTube title (under ~70 chars,
  1-2 emojis). Front-load a searchable keyword people actually type (e.g.
  "आज की खबर", "Breaking News", "Hindi News") AND a curiosity hook.
- "Description": 2-3 lines of natural Hinglish/Hindi. First line keyword-rich (repeat the
  main topic + words like "आज की खबर", "Hindi News", "Breaking News"), then a short
  subscribe request. Do NOT add hashtags yourself (they are added automatically).
- "Tags": array of 10-15 relevant Hinglish + English SEO keywords (no '#'), mixing
  the specific topic with broad terms viewers search.
- "Narration": the FULL news narration as ONE Hinglish/Hindi paragraph (this is the
  spoken voiceover — no scene labels, no English, no stage directions).
- "ScenePrompts": an array of EXACTLY {num_scenes} strings, in order, each being a
  detailed ENGLISH visual prompt for stock footage or AI image generation. Describe
  the visual content for this part of the news. Examples: "busy city street at night",
  "scientist working in a lab", "medical professionals discussing charts",
  "protestors holding signs", "satellite orbiting Earth".
  NO text/letters/words/numbers/logos anywhere in the image.

Constraints: EXACTLY {num_scenes} scene prompts. Everything factual and appropriate for news.
Output valid JSON only.
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
    for key in ("Title", "Description", "Tags", "Narration", "ScenePrompts"):
        if key not in data or not data[key]:
            raise ValueError(f"Claude response missing required key: {key}")

    data["Title"] = str(data["Title"]).strip()[:100]
    data["Tags"] = _normalize_tags(data["Tags"], NEWS_BASE_TAGS)
    data["Description"] = _add_hashtags_to_description(data["Description"], NEWS_HASHTAGS)

    log.info("News content ready. Title: %s | %d scene prompts | narration %d chars",
             data["Title"], len(data["ScenePrompts"]), len(str(data["Narration"])))
    return data


def generate_slideshow_content(cfg, topic, mood):
    """
    Claude writes a ~5 minute HINDI chudail/bhoot HORROR story in the
    style of Dream Stories TV: an original, family-safe Indian village ghost
    story — eerie and suspenseful, never gore or adult. Returns:
        Title, Description, Tags, Narration (full Hindi voiceover),
        ScenePrompts = [English image prompt, ...]  (~NUM_SLIDESHOW_SCENES)
    """
    import anthropic

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("anthropic_model", "claude-opus-4-8")
    n = int(cfg.get("num_slideshow_scenes", NUM_SLIDESHOW_SCENES))
    words = int(cfg.get("slideshow_words", 750))  # ~5 min at storytelling pace

    system_prompt = (
        "You are the lead writer for a hugely popular HINDI horror-storytelling "
        "YouTube channel (in the mould of Dream Stories TV / Chudail Ki "
        "Kahaniya). You write long, gripping Indian village ghost stories — "
        "chudail, dayan, bhoot, aatma — narrated by a single mature male "
        "storyteller. The stories are genuinely scary and atmospheric but always "
        "FAMILY-SAFE: suspense and dread, never gore, blood or adult content. "
        "You always respond with a single valid JSON object and nothing else."
    )
    user_prompt = f"""
Write a gripping ~5 minute HINDI horror story for this premise:

PREMISE: "{topic}"

Return ONLY a JSON object with EXACTLY these keys:

- "TitleHindi": a short, dramatic, high-CTR HINDI hook title (like "मंदिर के
  बाहर जाना मना है"). No emojis, keep it under 45 characters.
- "TitleEnglish": a 2-4 word English version of the hook (like "Don't Go
  Outside" or "Highway Witch").
- "Description": 4-6 lines of gripping Hindi — a teaser that sets the scene,
  2-3 curiosity questions ("क्या ...?", "कौन थी वो ...?"), a spine-chilling line,
  and a subscribe request. Do NOT add hashtags (added automatically).
- "Tags": array of 10-15 relevant hindi/english horror SEO keywords (no '#').
- "Characters": an array of the 2-4 main characters in the story. Each is an
  object with:
    - "name": the character's name (English).
    - "look": ONE English sentence fixing their exact, unchanging appearance —
      age, gender, face, hair, skin tone, body, and specific clothing with
      colours. Human characters must look like ordinary Indian villagers. For
      the ghost, describe a classic Indian chudail: a woman in a red saree with
      very long messy black hair covering part of a pale, eerie face and dark
      sunken eyes — creepy and ghostly, but NOT bloody, NOT a zombie, no wounds.
      Make each character visually distinct so they are never confused.
- "Narration": a SINGLE flowing HINDI (Devanagari) voiceover of about {words}
  words (spoken for ~5 minutes) — a masterful storyteller telling the whole
  story start to finish: a chilling hook in the first lines, slow-building
  dread, vivid detail, a terrifying climax, an unexpected twist, and a haunting
  closing line + a request to like and subscribe. Only spoken words, no scene
  labels, no headings.
- "Scenes": an array of EXACTLY {n} objects, IN THE SAME ORDER as the story is
  narrated, so each image shows exactly what the narration is talking about at
  that moment. Each object has ONE key "visual_prompt": a detailed ENGLISH image
  prompt for that beat. CRITICAL RULES for every visual_prompt:
    * Whichever character(s) appear in this beat, describe them by COPYING their
      full "look" sentence VERBATIM into the prompt, so the same person looks
      identical in every scene they appear in. Never invent a new appearance.
    * Show the character the narration is currently about — if the story is
      following the farmer here, the farmer is in this frame; when the chudail
      appears in the story, she appears here.
    * Set it in a dark Indian village/rural night: moonlight, fog, deep shadows,
      WIDE 16:9 landscape composition. NO text, letters, numbers or logos.

Constraints: Narration in Hindi about {words} words. EXACTLY {n} scenes, in
narration order. Output valid JSON only.
"""

    # A multi-scene story is still a sizable generation; the SDK requires
    # streaming once max_tokens is this high, so collect the stream.
    with client.messages.stream(
        model=model, max_tokens=32000, system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        raw = "".join(
            event.delta.text
            for event in stream
            if event.type == "content_block_delta"
            and getattr(event.delta, "type", "") == "text_delta"
        )
    data = json.loads(_extract_json(raw))

    for key in ("TitleHindi", "TitleEnglish", "Description", "Tags",
                "Characters", "Narration", "Scenes"):
        if key not in data or not data[key]:
            raise ValueError(f"Claude slideshow response missing key: {key}")

    cast = ", ".join(
        f"{c.get('name', '?')} ({c.get('look', '')})"
        for c in data["Characters"] if isinstance(c, dict)
    )
    log.info("Cast: %s", cast[:400])

    # Assemble the channel's title convention, trimming the hook if the whole
    # thing would blow past YouTube's 100-character limit.
    hook = f"{str(data['TitleHindi']).strip()} | {str(data['TitleEnglish']).strip()}"
    suffix = f" | {SLIDESHOW_TITLE_SUFFIX}"
    data["Title"] = (hook[:100 - len(suffix)] + suffix)[:100]
    data["Tags"] = _normalize_tags(data["Tags"], SLIDESHOW_TAGS)
    data["Description"] = _add_hashtags_to_description(data["Description"], SLIDESHOW_HASHTAGS)

    prompts = []
    for sc in data["Scenes"]:
        vp = sc.get("visual_prompt") if isinstance(sc, dict) else sc
        if vp and str(vp).strip():
            prompts.append(str(vp).strip())
    if not prompts:
        raise ValueError("Slideshow response produced no scene prompts.")
    data["ScenePrompts"] = prompts

    log.info("Slideshow story ready. Title: %s | %d scenes | narration %d chars",
             data["Title"], len(prompts), len(str(data["Narration"])))
    return data


def _normalize_tags(tags, base_tags):
    if isinstance(tags, str): tags = [t.strip() for t in tags.split(",")]
    tags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()]
    seen, merged = set(), []
    for t in tags + base_tags:
        if t.lower() not in seen: seen.add(t.lower()); merged.append(t)
    return merged[:15]


def _add_hashtags_to_description(desc, hashtags):
    desc = str(desc).strip()
    have = {w.lower() for w in desc.split() if w.startswith("#")}
    add = [h for h in hashtags if h.lower() not in have][:max(0, 13 - len(have))]
    if add:
        return desc + "\n\n" + " ".join(add)
    return desc


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


def _free_image_url(prompt, seed=None, width=1080, height=1920):
    """Public image URL from the FREE Pollinations.ai (Flux) generator — no key,
    no cost. Deterministic per (prompt, seed). Default 9:16; pass width/height
    for other aspect ratios (e.g. 1920x1080 for 16:9)."""
    from urllib.parse import quote
    import random
    if seed is None:
        seed = random.randint(1, 9_000_000)
    return ("https://image.pollinations.ai/prompt/" + quote(prompt)
            + f"?width={width}&height={height}&nologo=true&model=flux&seed={seed}")


def _warm_free_image(url):
    """Fetch the free image once so it is generated + cached before Higgsfield
    pulls it. Returns True if it looks like a valid image."""
    import requests
    r = requests.get(url, timeout=120)
    return r.status_code == 200 and len(r.content) > 5000


# --------------------------------------------------------------------------- #
#  STEP 2b — Hindi voiceover (free edge-tts)
# --------------------------------------------------------------------------- #
def synthesize_hindi_voice(cfg, narration, out_path, _tts_voice_override=None,
                           _tts_rate_override=None, _tts_pitch_override=None):
    """Generate a Hindi voiceover mp3 from `narration` (free Microsoft TTS).
    Returns (out_path, duration_seconds)."""
    import asyncio
    import edge_tts

    voice = _tts_voice_override or cfg.get("tts_voice", TTS_VOICE)
    rate = _tts_rate_override if _tts_rate_override is not None else cfg.get("tts_rate", TTS_RATE)
    pitch = _tts_pitch_override if _tts_pitch_override is not None else cfg.get("tts_pitch", TTS_PITCH)
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
FREE_IMAGE_TRIES = 3


def generate_scene_image(cfg, visual_prompt, out_path, width=1080, height=1920,
                         base_seed=None):
    """Render one image (default 9:16; pass width/height for 16:9 etc.) and
    download it to out_path.

    FREE Pollinations only. A failure here is nearly always transient — the free
    tier rate-limits or returns a stub — so retry with a fresh seed rather than
    falling back to Higgsfield: those credits are spent, so the fallback only
    ever added a slow 403 to every miss (six slides were lost that way in the
    last slideshow) and could silently start charging again if topped up.

    `base_seed`, when given, is used on the first try so that every scene in one
    video shares a seed — with Flux that keeps the characters' faces and the
    overall look consistent from scene to scene. Retries fall back to a random
    seed, since the same seed tends to fail the same way.
    """
    import random
    import time as _time

    last = None
    for attempt in range(1, FREE_IMAGE_TRIES + 1):
        try:
            seed = (base_seed if attempt == 1 and base_seed is not None
                    else random.randint(1, 9_000_000))
            url = _free_image_url(visual_prompt, seed=seed,
                                  width=width, height=height)
            _download_url(url, out_path)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
                return out_path
            last = "the image came back empty"
        except Exception as exc:                              # noqa: BLE001
            last = str(exc)[:120]

        if attempt < FREE_IMAGE_TRIES:
            log.warning("Free image try %d/%d failed (%s); retrying...",
                        attempt, FREE_IMAGE_TRIES, last)
            _time.sleep(3 * attempt)   # back off; the free tier rate-limits

    raise RuntimeError(f"Free image failed after {FREE_IMAGE_TRIES} tries: {last}")


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
    import random
    paths = []
    total = len(scenes)
    kind = "clip" if animate else "image"
    ext = "mp4" if animate else "png"
    # One seed for every still in a short: with Flux this keeps the recurring
    # Muli-Didi cast looking consistent from scene to scene.
    base_seed = random.randint(1, 9_000_000)
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
                    generate_scene_image(cfg, ip, out, base_seed=base_seed)
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


def render_slideshow_images(cfg, prompts, run_id):
    """
    Generate one FREE (Pollinations) image per slideshow scene prompt.

    Every prompt is wrapped in the photoreal horror style so the frames look
    cinematic rather than soft, and ONE seed is shared across the whole video so
    the characters stay recognisable from scene to scene. `generate_scene_image`
    retries each frame internally; a frame that still fails is skipped. A small
    delay between calls keeps the free tier from rate-limiting. Returns the list
    of image paths.
    """
    import random
    import time as _time

    paths = []
    total = len(prompts)
    base_seed = random.randint(1, 9_000_000)  # one look for the whole story
    for i, prompt in enumerate(prompts, 1):
        out = os.path.join(WORK_DIR, f"slide_{run_id}_{i}.png")
        styled = SLIDESHOW_IMG_STYLE + str(prompt).strip() + SLIDESHOW_IMG_QUALITY
        try:
            log.info("Slideshow image %d/%d...", i, total)
            generate_scene_image(cfg, styled, out, width=1920, height=1080,
                                 base_seed=base_seed)
            paths.append(out)
        except Exception as exc:                              # noqa: BLE001
            log.warning("Slide %d skipped (%s).", i, str(exc)[:120])
        if i < total:
            _time.sleep(FREE_IMAGE_DELAY_S)
    if not paths:
        raise RuntimeError("All slideshow images failed to render.")
    log.info("Rendered %d/%d slideshow images.", len(paths), total)
    return paths


# --------------------------------------------------------------------------- #
#  STEP 2e — Real FREE stock footage (Pexels / Pixabay) -> scene clips
#  (OpenMontage-style: real motion clips instead of still AI images; keeps
#   viewers watching = better retention. NO paid service, NO Higgsfield.)
# --------------------------------------------------------------------------- #
def _stock_prompt_text(item):
    """A scene item is either a dict (Shorts: image_prompt) or a plain prompt
    string (slideshow). Return its descriptive text."""
    if isinstance(item, dict):
        return item.get("image_prompt", "") or item.get("video_prompt", "")
    return str(item)


def _naive_stock_query(text):
    """Cheap keyword fallback when the LLM query step is unavailable: keep the
    first few concrete words, drop style boilerplate."""
    skip = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "to",
            "3d", "pixar", "disney", "dreamworks", "cartoon", "style",
            "cinematic", "highly", "detailed", "ultra", "scene", "illustration",
            "render", "rendered", "4k", "8k", "hd", "photo", "photorealistic",
            "wholesome", "family", "friendly", "cute", "stylised", "stylized"}
    words = re.findall(r"[A-Za-z]+", text)
    keep = [w for w in words if len(w) > 1 and w.lower() not in skip][:3]
    return " ".join(keep) or "nature background"


def generate_stock_queries(cfg, prompts):
    """Convert each scene's long visual prompt into a SHORT (2-4 word) English
    stock-footage search query, via one cheap batched Claude call. Falls back to
    naive keywords if the LLM is unavailable. Returns a list the SAME length as
    `prompts` (order preserved)."""
    texts = [_stock_prompt_text(p) for p in prompts]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        model = cfg.get("anthropic_model", "claude-opus-4-8")
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        system = (
            "You turn scene descriptions into SHORT English stock-video search "
            "queries. For each numbered scene output 2-4 plain keywords naming "
            "the concrete VISIBLE subject/action only (no style words, no "
            "punctuation, no camera terms). Return ONLY a JSON object of the "
            'form {"queries": ["kw kw", ...]} with one string per scene, in the '
            "same order and count."
        )
        msg = client.messages.create(
            model=model, max_tokens=1200, system=system,
            messages=[{"role": "user", "content": numbered}],
        )
        raw = next((b.text for b in msg.content if b.type == "text"), "")
        arr = json.loads(_extract_json(raw)).get("queries", [])
        if isinstance(arr, list) and len(arr) == len(texts):
            return [str(q).strip() or _naive_stock_query(t)
                    for q, t in zip(arr, texts)]
        log.warning("Stock-query JSON malformed (%d vs %d); naive keywords.",
                    len(arr) if isinstance(arr, list) else -1, len(texts))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Stock-query LLM step failed (%s); naive keywords.",
                    str(exc)[:120])
    return [_naive_stock_query(t) for t in texts]


def _pick_pexels_file(videos, orientation):
    """Choose one Pexels video-file URL: prefer the requested orientation and a
    short-edge <=1920 (keeps files small + low-RAM). Falls back to any capped
    file if orientation has no match."""
    best, best_score = None, -1
    for v in videos:
        for f in v.get("video_files", []):
            w, h, link = f.get("width") or 0, f.get("height") or 0, f.get("link")
            if not (link and w and h) or max(w, h) > 1920:
                continue
            is_portrait = h >= w
            if (orientation == "portrait") != is_portrait:
                continue
            score = min(w, h)                     # sharper short-edge wins
            if score > best_score:
                best, best_score = link, score
    if best is None:                              # orientation had no match
        for v in videos:
            for f in v.get("video_files", []):
                w, h, link = f.get("width") or 0, f.get("height") or 0, f.get("link")
                if link and w and h and max(w, h) <= 1920:
                    return link
    return best


def _pick_pixabay_file(hits):
    """Choose one Pixabay video URL (prefer medium quality)."""
    for h in hits:
        vids = h.get("videos", {})
        for q in ("medium", "large", "small", "tiny"):
            f = vids.get(q) or {}
            if f.get("url"):
                return f["url"]
    return None


def fetch_stock_clip(cfg, query, out_path, orientation="portrait"):
    """Download ONE free real stock video clip matching `query`. Tries Pexels
    (needs a FREE pexels_api_key), then Pixabay (FREE pixabay_api_key). Returns
    out_path on success, else None. No paid service, no card required."""
    import requests

    pk = cfg.get("pexels_api_key", "").strip()
    if pk:
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": pk},
                params={"query": query, "orientation": orientation,
                        "per_page": 8, "size": "medium"},
                timeout=60,
            )
            if r.status_code == 200:
                url = _pick_pexels_file(r.json().get("videos", []), orientation)
                if url:
                    _download_url(url, out_path)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 20000:
                        return out_path
            else:
                log.warning("Pexels HTTP %s for '%s'.", r.status_code, query)
        except Exception as exc:                              # noqa: BLE001
            log.warning("Pexels failed for '%s' (%s).", query, str(exc)[:120])

    xk = cfg.get("pixabay_api_key", "").strip()
    if xk:
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": xk, "q": query, "per_page": 8,
                        "safesearch": "true"},
                timeout=60,
            )
            if r.status_code == 200:
                url = _pick_pixabay_file(r.json().get("hits", []))
                if url:
                    _download_url(url, out_path)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 20000:
                        return out_path
            else:
                log.warning("Pixabay HTTP %s for '%s'.", r.status_code, query)
        except Exception as exc:                              # noqa: BLE001
            log.warning("Pixabay failed for '%s' (%s).", query, str(exc)[:120])

    return None


def _still_to_clip(img_path, out_path, dur, width, height):
    """Turn a still image into a short Ken-Burns mp4 clip so a Pollinations
    still fallback can be stitched by the same clip assembler as real footage.
    Reuses the exact crop-pan approach from assemble_video (low-RAM, threads=1)."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    W, H = width, height
    SW, SH = int(W * 1.2), int(H * 1.2)          # 1.2x headroom for the pan
    fps = 30
    prog = f"min(1\\,t/{dur:.3f})"
    _run_ffmpeg([
        ffmpeg, "-y", "-loop", "1", "-t", f"{dur:.3f}", "-i", img_path, "-an",
        "-vf",
        f"scale={SW}:{SH},crop={W}:{H}:(in_w-out_w)*{prog}:"
        f"(in_h-out_h)*{prog},setsar=1,fps={fps},format=yuv420p",
        "-threads", "1", "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-r", str(fps), out_path,
    ])
    return out_path


def render_stock_clips(cfg, prompts, run_id, width=1080, height=1920):
    """For each scene: download a FREE real stock video clip (Pexels/Pixabay);
    if none is found, fall back to a FREE Pollinations still rendered as a short
    Ken-Burns clip. Returns a list of mp4 paths (ALL clips) so
    assemble_animated_video can stitch them uniformly. Higgsfield is NEVER hit."""
    import time as _time

    orientation = "portrait" if height >= width else "landscape"
    queries = generate_stock_queries(cfg, prompts)
    paths, total = [], len(prompts)

    for i, (item, query) in enumerate(zip(prompts, queries), 1):
        out = os.path.join(WORK_DIR, f"stock_{run_id}_{i}.mp4")
        log.info("Stock clip %d/%d: '%s' ...", i, total, query)
        got = None
        try:
            got = fetch_stock_clip(cfg, query, out, orientation)
        except Exception as exc:                              # noqa: BLE001
            log.warning("Stock fetch %d failed (%s).", i, str(exc)[:120])

        if got:
            paths.append(got)
        else:
            # Fallback: FREE Pollinations still -> Ken Burns clip (no paid gen).
            # Use a REALISTIC photographic prompt (built from the search query)
            # so the fallback matches the real-footage look, not a cartoon.
            still = os.path.join(WORK_DIR, f"stockimg_{run_id}_{i}.png")
            base = query or _stock_prompt_text(item)
            prompt = (f"realistic cinematic photograph, {base}, natural "
                      "lighting, high detail, lifelike, no cartoon, no text")
            try:
                log.info("  no stock hit; Pollinations still fallback %d.", i)
                generate_scene_image(cfg, prompt, still,
                                     width=width, height=height)
                _still_to_clip(still, out, 5.0, width, height)
                paths.append(out)
            except Exception as exc:                          # noqa: BLE001
                log.warning("Fallback still %d failed (%s); skipping scene.",
                            i, str(exc)[:120])
            finally:
                if os.path.exists(still):
                    try:
                        os.remove(still)
                    except OSError:
                        pass

        if i < total:
            _time.sleep(FREE_IMAGE_DELAY_S)       # be gentle on free services

    if not paths:
        raise RuntimeError("All stock clips failed (no footage, no fallback).")
    log.info("Stock footage ready: %d/%d clips.", len(paths), total)
    return paths


# --------------------------------------------------------------------------- #
#  STEP 2d — Assemble the video (images + Ken Burns + voiceover + music)
# --------------------------------------------------------------------------- #
def assemble_video(image_paths, voice_path, voice_dur, out_path,
                   music_dir=None, music_volume=None, width=1080, height=1920):
    """
    Build a 9:16 slideshow with ffmpeg (fast, native): each image gets a slow
    Ken Burns PAN (via the crop filter on a pre-upscaled still) for an equal
    slice of the voiceover; the Hindi voiceover plays on top with optional
    royalty-free background music ducked underneath. `music_dir` / `music_volume`
    override the defaults (e.g. a mood folder + louder music for slideshows).

    Each image is encoded ONE AT A TIME to a temp segment (Ken Burns pan via an
    animated crop) and the segments are joined with the concat DEMUXER — this is
    robust, whereas the single-command concat FILTER can crash libx264 on some
    resolutions. width/height set the aspect (default 9:16; 1920x1080 for 16:9).
    """
    import glob
    import random
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mdir = music_dir or MUSIC_DIR
    mvol = MUSIC_VOLUME if music_volume is None else music_volume
    W, H = width, height
    SW, SH = int(W * 1.2), int(H * 1.2)   # 1.2x upscale -> headroom for the pan
    fps = 30
    n = max(1, len(image_paths))
    per = (voice_dur + 1.0) / n    # hold each image a touch past the VO end
    wd = os.path.dirname(out_path) or "."
    tmp = []

    log.info("Assembling slideshow (%d scenes, %.1fs each, %dx%d)...",
             n, per, W, H)
    segs = []
    for i, img in enumerate(image_paths):
        seg = os.path.join(wd, f"_kb_{i}.mp4")
        tmp.append(seg)
        prog = (f"min(1\\,t/{per:.3f})" if i % 2 == 0
                else f"max(0\\,1-t/{per:.3f})")
        _run_ffmpeg([
            ffmpeg, "-y", "-loop", "1", "-t", f"{per:.3f}", "-i", img, "-an",
            "-vf",
            f"scale={SW}:{SH},crop={W}:{H}:(in_w-out_w)*{prog}:"
            f"(in_h-out_h)*{prog},setsar=1,fps={fps},format=yuv420p",
            # -threads 1 keeps libx264's per-thread frame buffers from
            # multiplying RAM use -> reliable on low-memory (3-4GB) runners.
            "-threads", "1",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-r", str(fps), seg,
        ])
        segs.append(seg)

    listfile = os.path.join(wd, "_kb_list.txt")
    tmp.append(listfile)
    with open(listfile, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"file '{os.path.abspath(s)}'\n")

    tracks = (glob.glob(os.path.join(mdir, "*.mp3"))
              + glob.glob(os.path.join(mdir, "*.wav"))
              + glob.glob(os.path.join(mdir, "*.m4a")))
    music = random.choice(tracks) if (tracks and mvol > 0) else None

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))
        cmd += ["-filter_complex",
                f"[2:a]volume={mvol},aloop=loop=-1:size=2000000000[mus];"
                "[1:a][mus]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map", "0:v", "-map", "[aout]"]
    else:
        log.warning("No music in %s; voiceover only.", mdir)
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path]

    try:
        _run_ffmpeg(cmd)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("ffmpeg produced an empty video file.")
        log.info("Final video rendered -> %s", out_path)
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
    return out_path


def _run_ffmpeg(cmd):
    """Run an ffmpeg command, raising with the stderr tail on failure."""
    import subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-700:]}")


def assemble_animated_video(clip_paths, voice_path, voice_dur, out_path,
                            width=1080, height=1920,
                            music_dir=None, music_volume=None):
    """
    Stitch scene VIDEO clips (Higgsfield DoP animation OR free stock footage)
    into the final video. Each clip is processed ONE AT A TIME (memory-safe):
    normalized to width x height and PLAYED AT REAL SPEED once, then it holds
    its last frame to fill its equal slice of the voiceover (no slow-motion, no
    reverse, no loop-jump), so the motion looks natural and smooth. The segments
    are concatenated
    and the Hindi voiceover is laid on top with optional background music ducked
    underneath. width/height set the aspect (default 9:16; 1920x1080 for 16:9);
    music_dir/music_volume override the defaults (e.g. a mood folder for
    slideshows).
    """
    import glob
    import random
    import imageio_ffmpeg
    from moviepy import VideoFileClip

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    mdir = music_dir or MUSIC_DIR
    mvol = MUSIC_VOLUME if music_volume is None else music_volume
    W, H, fps = width, height, 30
    n = max(1, len(clip_paths))
    per = (voice_dur + 1.0) / n
    wd = os.path.dirname(out_path) or "."
    tmp = []

    log.info("Stitching %d animated clips (%.1fs each)...", n, per)
    segs = []
    for i, clip in enumerate(clip_paths):
        seg = os.path.join(wd, f"_seg_{i}.mp4")
        tmp.append(seg)
        # Play the clip once at REAL speed, then FREEZE the last frame to fill
        # `per` seconds (no setpts slow-motion, no reverse, no loop-jump).
        # tpad clones the final frame for a long time; -t trims the segment to
        # exactly `per`, so the motion runs real-time and then holds still. A
        # clip longer than `per` is simply trimmed by -t (no freeze visible).
        # setpts=N/FRAME_RATE/TB rebuilds continuous timestamps for a clean join.
        _run_ffmpeg([
            ffmpeg, "-y", "-i", clip, "-an",
            "-t", f"{per:.3f}",
            "-filter_complex",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=3600,"
            f"setpts=N/FRAME_RATE/TB[v]",
            "-map", "[v]",
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

    tracks = (glob.glob(os.path.join(mdir, "*.mp3"))
              + glob.glob(os.path.join(mdir, "*.wav"))
              + glob.glob(os.path.join(mdir, "*.m4a")))
    music = random.choice(tracks) if (tracks and mvol > 0) else None

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))
        cmd += ["-filter_complex",
                f"[2:a]volume={mvol},aloop=loop=-1:size=2000000000[mus];"
                "[1:a][mus]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map", "0:v", "-map", "[aout]"]
    else:
        log.warning("No music in %s; voiceover only.", mdir)
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
            # A browser consent flow can NEVER work on a headless CI runner —
            # fail with an actionable message instead of a confusing
            # "client_secret.json not found".
            if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
                raise RuntimeError(
                    "YouTube token expired/revoked and cannot be refreshed on a "
                    "headless runner. FIX: (1) in Google Cloud Console set the "
                    "OAuth consent screen publishing status to 'In production' "
                    "(in 'Testing' mode refresh tokens die after 7 days), "
                    "(2) run `python ultimate_shorts_agent.py reauth` locally, "
                    "(3) paste the new token.json into the TOKEN_JSON secret."
                )
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


def reauth_youtube():
    """Re-run the Google consent flow LOCALLY (a browser opens) and rewrite
    token.json. Use this when the cloud run fails with 'token expired/revoked'.
    Afterwards paste the whole token.json into the TOKEN_JSON GitHub secret.

    TIP: first set the OAuth consent screen to 'In production' in Google Cloud
    Console — while it is in 'Testing', Google expires refresh tokens after
    7 days, so the bot breaks every week.
    """
    ensure_dirs()
    if os.path.exists(TOKEN_PATH):
        backup = TOKEN_PATH + ".old"
        try:
            shutil.copyfile(TOKEN_PATH, backup)
            os.remove(TOKEN_PATH)
            log.info("Stale token backed up -> %s", backup)
        except OSError as exc:                                # noqa: BLE001
            log.warning("Could not back up the stale token (%s).", exc)

    get_youtube_service()        # opens the browser, writes a fresh token.json
    log.info("=" * 70)
    log.info("NEW TOKEN WRITTEN -> %s", TOKEN_PATH)
    log.info("Now copy the ENTIRE contents of token.json into the GitHub "
             "secret named TOKEN_JSON (Settings -> Secrets -> Actions).")
    log.info("=" * 70)


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
#  (NEW) YouTube Downloader
# --------------------------------------------------------------------------- #
def download_from_youtube(url, out_dir=WORK_DIR):
    """
    Download a video from a YouTube URL into the specified directory.
    Uses yt-dlp to fetch the best quality mp4.
    Returns the full path to the downloaded file, or None on failure.

    NOTE: Downloading and re-using copyrighted content without permission may
    violate YouTube's Terms of Service and copyright law. Use responsibly.
    """
    try:
        import yt_dlp
        log.info("Downloading YouTube video from: %s", url)

        # yt-dlp options: download best mp4 format into the work directory
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(out_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            filename = ydl.prepare_filename(info)
            ydl.download([url])

        log.info("YouTube video downloaded successfully -> %s", filename)
        return filename
    except Exception as exc:
        log.error("Failed to download YouTube video: %s", exc)
        return None


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

    # Initialize variables to None to ensure they are defined even if early steps fail
    cfg = None
    pkg = None
    voice_path = os.path.join(WORK_DIR, f"voice_{run_id}.mp3")
    final_video = os.path.join(WORK_DIR, f"final_{run_id}.mp4")
    scene_paths = []

    try:
        ensure_dirs()
        cfg = load_config()
        # visual_mode: "stock_video" (FREE real footage, 0 credits) |
        #              "ai_image" (legacy: Higgsfield DoP if animate, else stills)
        visual_mode = cfg.get("visual_mode", "ai_image")
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

        # 2. Hindi voiceover --------------------------------------------- #
        _, voice_dur = synthesize_hindi_voice(cfg, pkg["Narration"], voice_path)

        # 3. Scene visuals + assemble the 40-50s video ------------------- #
        # Subtle background music (auto-activates once no-copyright tracks are
        # added to assets/music; voiceover-only until then).
        smv = cfg.get("shorts_music_volume", 0.08)
        if visual_mode == "stock_video":
            # FREE real stock footage (Pexels/Pixabay) -> motion clips.
            # Higgsfield NEVER hit -> 0 credits. Motion boosts retention.
            scene_paths = render_stock_clips(cfg, pkg["Scenes"], run_id,
                                             width=1080, height=1920)
            assemble_animated_video(scene_paths, voice_path, voice_dur,
                                    final_video, music_volume=smv)
        elif animate:
            # Higgsfield DoP animation (free image -> DoP motion). Main path.
            scene_paths = render_scene_assets(cfg, pkg["Scenes"], run_id, True)
            assemble_animated_video(scene_paths, voice_path, voice_dur,
                                    final_video, music_volume=smv)
        else:
            # Legacy FREE Pollinations stills + Ken Burns slideshow.
            scene_paths = render_scene_assets(cfg, pkg["Scenes"], run_id, False)
            assemble_video(scene_paths, voice_path, voice_dur, final_video,
                           music_volume=smv)

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


def run_news_short_pipeline():
    """
    One NEWS SHORT run: news topic -> Hinglish/Hindi news script ->
    free edge-tts voiceover -> free stock footage/images ->
    9:16 animated video + music -> YouTube. Near-free (only Claude text costs).
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log.info("=" * 70)
    log.info("NEWS SHORT RUN START (id=%s)", run_id)
    log.info("=" * 70)

    cfg = None
    pkg = None
    voice_path = os.path.join(WORK_DIR, f"news_voice_{run_id}.mp3")
    final_video = os.path.join(WORK_DIR, f"news_final_{run_id}.mp4")
    scene_paths = []

    try:
        ensure_dirs()
        cfg = load_config()

        # 1. News Topic + Script ----------------------------------------- #
        news_info = pick_news_topic(cfg)
        pkg = generate_news_content_package(cfg, news_info["topic"])
        # No hook for news shorts, typically.

        # 2. Hinglish/Hindi voiceover ------------------------------------ #
        # Use a slightly more mature voice for news if available, or default.
        _, voice_dur = synthesize_hindi_voice(
            cfg, pkg["Narration"], voice_path,
            _tts_voice_override=cfg.get("news_tts_voice", "hi-IN-MadhurNeural"),
            _tts_rate_override=cfg.get("news_tts_rate", "+5%"),
            _tts_pitch_override=cfg.get("news_tts_pitch", "+0Hz"),
        )

        # 3. Scene visuals (FREE stock footage) + assemble the video ----- #
        # Use stock_video mode for faceless news shorts.
        smv = cfg.get("shorts_music_volume", 0.08) # Reuse shorts music volume
        
        # For news, we explicitly use stock footage for visuals.
        scene_paths = render_stock_clips(cfg, pkg["ScenePrompts"], run_id,
                                         width=1080, height=1920)
        assemble_animated_video(scene_paths, voice_path, voice_dur,
                                final_video, music_volume=smv)

        # 4. Upload ------------------------------------------------------ #
        url = upload_to_youtube(cfg, final_video, pkg)

        # 5. Notify ------------------------------------------------------ #
        send_telegram(
            cfg,
            "✅ News Short uploaded successfully!\n\n"
            f"🎬 Title: {pkg['Title']}\n"
            f"📈 Topic: {news_info['topic']} ({news_info['source']})\n"
            f"🔗 {url}",
        )
        # Tidy the work dir (keep nothing transient)
        for p in scene_paths + [voice_path, final_video]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

        log.info("NEWS SHORT RUN SUCCESS (id=%s)", run_id)

    except Exception as exc:                                  # noqa: BLE001
        log.error("NEWS SHORT RUN FAILED (id=%s): %s", run_id, exc)
        log.error(traceback.format_exc())
        if cfg is not None:
            title = pkg["Title"] if pkg else "(content not generated)"
            send_telegram(cfg, f"❌ News Short FAILED (id={run_id})\nError: {exc}")


def run_slideshow_pipeline():
    """One SLIDESHOW long-video run: sabzi/veggie premise -> ~3 min Hindi story
    -> free images -> storyteller voiceover -> 9:16 Ken Burns slideshow + mood
    music -> YouTube. Near-free (only Claude text costs)."""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log.info("=" * 70)
    log.info("SLIDESHOW RUN START (id=%s)", run_id)
    log.info("=" * 70)

    cfg = None
    pkg = None
    voice_path = os.path.join(WORK_DIR, f"sv_{run_id}.mp3")
    final_video = os.path.join(WORK_DIR, f"slide_{run_id}.mp4")
    img_paths = []

    try:
        ensure_dirs()
        cfg = load_config()
        visual_mode = cfg.get("visual_mode", "ai_image")

        # 1. sabzi topic + ~3 min story ---------------------------------- #
        topic = pick_slideshow_topic(cfg)
        pkg = generate_slideshow_content(cfg, topic["topic"], topic["mood"])

        # 2. storyteller voiceover + FREE scene images ------------------- #
        _, vdur = synthesize_hindi_voice(
            cfg, pkg["Narration"], voice_path,
            _tts_voice_override=cfg.get("slideshow_voice", SLIDESHOW_VOICE),
            _tts_rate_override=cfg.get("slideshow_rate", SLIDESHOW_RATE),
            _tts_pitch_override=cfg.get("slideshow_pitch", SLIDESHOW_PITCH),
        )
        # 3. 16:9 video + mood music ------------------------------------- #
        mood_dir = os.path.join(MUSIC_DIR, topic["mood"])
        if visual_mode == "stock_video":
            # FREE real stock footage (Pexels/Pixabay) -> 16:9 motion clips.
            img_paths = render_stock_clips(cfg, pkg["ScenePrompts"], run_id,
                                           width=1920, height=1080)
            assemble_animated_video(img_paths, voice_path, vdur, final_video,
                                    width=1920, height=1080, music_dir=mood_dir,
                                    music_volume=SLIDESHOW_MUSIC_VOLUME)
        else:
            # Legacy FREE Pollinations stills + Ken Burns slideshow.
            img_paths = render_slideshow_images(cfg, pkg["ScenePrompts"], run_id)
            assemble_video(img_paths, voice_path, vdur, final_video,
                           music_dir=mood_dir,
                           music_volume=SLIDESHOW_MUSIC_VOLUME,
                           width=1920, height=1080)

        # 4. upload + notify --------------------------------------------- #
        url = upload_to_youtube(cfg, final_video, pkg)
        send_telegram(
            cfg,
            "✅ Slideshow uploaded!\n\n"
            f"🎬 {pkg['Title']}\n"
            f"📖 {topic['topic']} ({topic['mood']})\n"
            f"🔗 {url}",
        )
        for p in img_paths + [voice_path, final_video]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        log.info("SLIDESHOW RUN SUCCESS (id=%s)", run_id)

    except Exception as exc:                                  # noqa: BLE001
        log.error("SLIDESHOW RUN FAILED (id=%s): %s", run_id, exc)
        log.error(traceback.format_exc())
        if cfg is not None:
            send_telegram(cfg, f"❌ Slideshow FAILED (id={run_id})\nError: {exc}")


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
    # CLI helpers:
    #   python ultimate_shorts_agent.py run        -> make one SHORT now
    #   python ultimate_shorts_agent.py slideshow  -> make one long SLIDESHOW now
    #   python ultimate_shorts_agent.py reauth     -> refresh the YouTube token
    #   python ultimate_shorts_agent.py            -> start the local scheduler
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "run":
        run_pipeline()
    elif arg == "news_short":
        run_news_short_pipeline()
    elif arg == "slideshow":
        run_slideshow_pipeline()
    elif arg == "reauth":
        reauth_youtube()
    else:
        main()
