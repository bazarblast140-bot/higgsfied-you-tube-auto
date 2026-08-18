#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ultimate_shorts_agent.py
========================
A single-file, production-grade pipeline that automatically:

  1. Picks an unused kids-story topic from topic_bank.json -> Topic bank
  2. Writes a 10-scene HINDI cartoon story (Pixar/Disney)  -> LLM
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
# The Muli shorts are emotional village dramas — the upbeat/hip-hop beds in the
# music root fought the story, so the shorts draw from the emotional beds only.
SHORTS_MUSIC_DIR = os.path.join(MUSIC_DIR, "emotional")
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
    # Soul accepts only these exact sizes; 1152x2048 is 9:16 vertical (Shorts),
    # 2048x1152 the 16:9 landscape the slideshow videos need.
    "image_size":           "1152x2048",
    "image_size_landscape": "2048x1152",
    # DoP model: one of 'dop-lite', 'dop-preview', 'dop-turbo'.
    "video_model":      "dop-turbo",
    "poll_interval_s":  6,                              # how often to poll
    "poll_timeout_s":   20 * 60,                        # give up after 20 min (DoP is slow)
}

# Kids cartoon-story settings (NITIN CARTOON ZONE style).
NUM_SCENES   = 10                      # more images so supporting cast shows (~55-70s)
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

# Flux is trained around one megapixel. Asking Pollinations for the final
# 1080x1920 is 2.07 MP — nearly DOUBLE — and generating that far above a
# diffusion model's training resolution is what produces the broken frames on
# this channel: duplicated limbs, two heads, melted faces. It bought nothing
# either, because assemble_video rescales every still to its 1.2x pan canvas
# regardless. So generate inside the model's comfortable budget and let ffmpeg
# do the enlarging: a clean small image upscaled beats a broken big one.
# Lower this to ~0.9 if artefacts ever come back; raise it for more detail.
IMG_GEN_MEGAPIXELS   = 1.3

# Hugging Face Inference Providers — the image source after Pollinations' free
# tier closed. schnell is the fast, cheap, 4-step distilled FLUX; its prompt
# adherence is the weaker part of the pipeline (it is what still lets a face
# drift between scenes). "black-forest-labs/FLUX.1-dev" follows the cast sheet
# noticeably more closely and costs more per image — switch via hf_image_model.
HF_IMAGE_MODEL       = "black-forest-labs/FLUX.1-schnell"

# How many of the cast may share one frame. Diffusion models bind attributes to
# subjects poorly, and the error grows fast with the head count — at four to six
# characters the vegetables swap heads and clothes outright. Two holds. Raise
# this only on a model with stronger multi-subject binding (FLUX.1-dev).
MAX_CHARS_PER_SCENE  = 3              # muli + 1-2 others (animal/veg) per frame

# Reach / SEO — niche hashtags appended to every description, and base keywords
# merged into tags so YouTube surfaces the video in search + suggested.
SEO_HASHTAGS = [
    "#Shorts", "#sabjikahani", "#aalukahani", "#mulididi", "#kahani",
    "#emotionalstory", "#sabzicartoon", "#hindikahani", "#vegetablecartoon",
    "#gaonkikahani", "#moralstory", "#dilchhukahani",
    "#BarfPahalwan", "#MuliDidi", "#AaluKaka", "#PyajHulk",
]
SEO_BASE_TAGS = [
    "sabji kahani", "aalu muli ki kahani", "muli didi", "vegetable cartoon hindi",
    "emotional story hindi", "sabzi cartoon", "gaon ki kahani", "hindi kahani",
    "dil chhu lene wali kahani", "moral story hindi",
    # This channel's own best Shorts are action/rescue, so the tags follow the
    # recurring villains and set-pieces that actually pulled views.
    "barf pahalwan", "muli didi ki team", "sabzi action", "rakshas kahani",
    "aalu kaka", "pyaj hulk", "hindi cartoon shorts", "veggie squad",
]

# The channel's SIGNATURE RECURRING CAST — starred (Muli Didi) in EVERY short so
# the channel has one familiar face, with fixed looks so they stay identical
# across scenes AND across videos.
# Full coverage is spelled out per character on purpose. Left vaguer ("wearing a
# patched kurta", "wearing a saree") the image models rendered a bare potato body
# and a drape that left the radish woman's pale body bare at the shoulders and
# arms — off-model, and enough to trip downstream NSFW filters on innocent beats.
# Keyed per character so the exact same words can be pasted into every scene
# prompt. See build_scene_prompt for why this must not be left to the writer.
#
# Two things are deliberate in the wording, both learned from watching frames
# come back wrong:
#   * "a potato man" got read as "a poor MAN" and rendered an ordinary human
#     boy with no vegetable anywhere. Every entry now says outright that the
#     head IS the vegetable and the character is NOT human.
#   * Anything left unstated drifts between scenes — skin tone, eye colour and
#     hair changed shot to shot while the clothes (which WERE stated) stayed
#     put. So every fixed detail a viewer would notice is pinned here.
MULI_CAST_SHEET = {
    # ===== MULI DIDI CORE FAMILY (use often) =====
    "muli_didi": (
        "MULI DIDI: a female RADISH character, NOT a human — her head IS a "
        "smooth white radish, and she alone has a big fresh green leafy top "
        "growing from the crown of her head. Pure white radish skin, large "
        "round dark brown eyes, long black eyelashes, a small red bindi, and a "
        "bare smooth white scalp under the leaves. She wears a bright pink "
        "Indian saree over a full-sleeved blouse covering her shoulders, arms "
        "and whole body, barefoot."
    ),
    "garib_aalu": (
        "GARIB AALU (Muli Didi's husband): a male POTATO character, NOT a human "
        "— his head and body ARE a big round light-brown potato, entirely "
        "covered in rough brown potato skin with small potato eye-dimples, and "
        "the top of his potato head is completely smooth and bare. Small sad "
        "black eyes, thick black eyebrows, a drooping black moustache. He wears "
        "a long-sleeved patched beige kurta buttoned to the neck that covers "
        "his whole torso and arms, and a matching beige dhoti, barefoot."
    ),
    "beta_chunnu": (
        "CHUNNU (Muli's son): a small boy RADISH character, NOT a human — his "
        "head IS a little white radish with a tiny green leafy top, white "
        "radish skin, big round dark brown eyes. Wears a simple blue kurta and "
        "beige shorts, barefoot. About 6 years old energy."
    ),
    "beti_munni": (
        "MUNNI (Muli's daughter): a small girl RADISH character, NOT a human — "
        "her head IS a little white radish with a small green leafy top, white "
        "radish skin, big round eyes, tiny red bindi. Wears a simple yellow "
        "frock, barefoot. About 5 years old energy."
    ),
    "muli_maa": (
        "MULI MAA (Muli Didi's mother): an elderly female RADISH character, NOT "
        "a human — her head IS a white radish with a softer smaller green leafy "
        "top, gentle wrinkled eyes, greyish hair-line under leaves. Wears a "
        "faded maroon saree over full-sleeved blouse, barefoot."
    ),
    "muli_papa": (
        "MULI PAPA (Muli Didi's father): an elderly male RADISH character, NOT "
        "a human — his head IS a slightly larger white radish with a thin green "
        "leafy top, kind wrinkled eyes, thin white moustache. Wears a cream "
        "kurta and dhoti, barefoot."
    ),
    "bhai_moolchand": (
        "MOOLCHAND (Muli Didi's younger brother): a young male RADISH character, "
        "NOT a human — his head IS a white radish with a green leafy top, "
        "energetic eyes. Wears a green kurta, barefoot."
    ),
    "saas_aloo_maa": (
        "ALOO MAA (Muli's mother-in-law, Aalu's mother): an elderly female "
        "POTATO character, NOT a human — her head IS a round brown potato, "
        "kind or strict eyes depending on the scene, smooth bare potato scalp. "
        "Wears a brownish-orange saree over full sleeves, barefoot."
    ),
    # ===== RECURRING VILLAGE (not core family) =====
    "ameer_baingan": (
        "AMEER BAINGAN (the rich villain): a male BRINJAL character, NOT a "
        "human — his head and body ARE a shiny deep-purple eggplant with a "
        "small green stalk cap on top. Narrow eyes, a thin black moustache. "
        "FULLY CLOTHED in a black suit, with black sunglasses and a thick gold "
        "chain."
    ),
    "tamatar": (
        "TAMATAR: a male TOMATO character, NOT a human — his head and body ARE "
        "a round bright red tomato with a small green stalk on top, cheerful "
        "round black eyes. Fully clothed in a green kurta."
    ),
    "lahsun": (
        "LAHSUN: an old male GARLIC character, NOT a human — his head IS a "
        "white garlic bulb, with a long flowing white beard and kind wrinkled "
        "eyes. Fully clothed in a white kurta."
    ),
    "mirch": (
        "MIRCH: a female GREEN CHILLI character, NOT a human — her head and "
        "body ARE a long curved bright green chilli with a small stalk on top, "
        "fiery angular eyes. Fully clothed in a yellow saree."
    ),
    "pyaaz": (
        "PYAAZ: a male ONION character, NOT a human — his head and body ARE a "
        "round purple onion with papery purple skin and thin green shoots on "
        "top. Fully clothed in an orange kurta."
    ),
}

# The supporting villagers, drawn on assets/cast_sheet_2.jpg and _3.jpg. They
# are named here so the writer can actually cast them: without these lines a
# story asked for Karela Dada silently substituted Lahsun, while the title still
# announced Karela — the video promised a character it never showed.
MULI_SUPPORTING = {
    # Attractive animals (viewer magnets) — side roles; MULI DIDI stays hero
    "billi_minni": (
        "MINNI THE CAT: a cute small orange tabby cat, big sparkling eyes, "
        "soft fur, NOT a vegetable — cartoon cat companion of Muli Didi."
    ),
    "kutta_sheru": (
        "SHERU THE DOG: a loyal medium-size brown Indian village dog, floppy "
        "ears, warm eyes, wagging tail — cartoon dog friend of Muli Didi."
    ),
    "tota_ramu": (
        "RAMU THE PARROT: a bright green Indian parrot with a red beak, "
        "often on Muli Didi's shoulder or a branch."
    ),
    "gaay_lakshmi": (
        "LAKSHMI THE COW: a gentle white Indian cow with kind eyes and small "
        "horns, soft cartoon style."
    ),
    "hirni": (
        "HIRNI THE DEER: a graceful young spotted deer fawn with large soft eyes."
    ),
    "ullu": (
        "ULU THE OWL: a small brown owl with round eyes on a night tree branch."
    ),
    "bandar_muniya": (
        "MUNIYA THE MONKEY: a playful small grey langur monkey, curious eyes."
    ),
    "chidiya": (
        "CHIDIYA: a tiny brown sparrow near Muli Didi's hand or windowsill."
    ),
    "gobhi": "GOBHI: a female CAULIFLOWER character — her head IS a white "
             "cauliflower with green leaves at the base. Green saree.",
    "bhindi": "BHINDI: a male OKRA character — his head and body ARE a long "
              "green okra pod. White kurta.",
    "gajar": "GAJAR: a young female CARROT character — her head IS an orange "
             "carrot with a green frond top. Yellow frock.",
    "matar": "MATAR: a small male PEAS character — his head IS a round green "
             "pea. Blue kurta.",
    "shimla_mirch": "SHIMLA MIRCH: a male CAPSICUM character — his head IS a "
                    "green bell pepper with a curved stalk. Brown kurta.",
    "karela_dada": "KARELA DADA: an elderly male BITTER-GOURD character — his "
                   "head IS a bumpy green bitter gourd, with a white "
                   "moustache and white eyebrows. Grey kurta.",
    "kaddu": "KADDU: a female PUMPKIN character — her head IS a round orange "
             "pumpkin with a short stalk. Orange saree.",
    "lauki": "LAUKI: a male BOTTLE-GOURD character — his head and body ARE a "
             "long pale-green bottle gourd. Cream kurta.",
    "adrak_dada": "ADRAK DADA: an elderly male GINGER character — his head IS "
                  "a knobbly brown ginger root, with a long white beard. "
                  "Saffron kurta.",
    "nimbu": "NIMBU: a young male LEMON character — his head IS a bright "
             "yellow lemon. White shirt.",
    "nariyal": "NARIYAL: a male COCONUT character — his head IS a hairy brown "
               "coconut with green shoots on top. Green lungi.",
    "aam": "AAM: a young female MANGO character — her head IS a golden-orange "
           "mango with one green leaf. Red frock.",
    "kela": "KELA: a male BANANA character — his head and body ARE a curved "
            "yellow banana. Blue kurta.",
    "seb": "SEB: a female APPLE character — her head IS a round red apple with "
           "a green leaf. Maroon saree.",
    "anar": "ANAR: a young female POMEGRANATE character — her head IS a deep "
            "red pomegranate with a small crown. Pink frock.",
    "makka": "MAKKA: a male CORN character — his head IS a yellow corn cob "
             "wrapped in green husk leaves. Beige kurta.",
}

# The whole sheet as one block — what the WRITER reads to plot the story.
MULI_CAST = (
    " ".join(MULI_CAST_SHEET.values())
    + " OTHER VILLAGERS available for supporting roles: "
    + " ".join(MULI_SUPPORTING.values())
    + " Use ONLY these characters, and call each by the exact name given here "
      "in BOTH the narration and the title — a title naming someone the story "
      "never mentions promises a viewer something the video does not show. "
      "Everyone is fully clothed in Indian village clothes."
)

# Pasted byte-identical in front of / behind every Shorts scene prompt.
SHORTS_STYLE_LOCK = (
    "Cinematic 3D animated short film still, Pixar/DreamWorks quality, "
    "looks like a paused movie frame not a flat illustration, "
    "shallow depth of field, soft volumetric god-rays, warm golden-hour or "
    "moonlit village lighting, subtle motion-ready cloth and leaves, "
    "cute-but-expressive vegetable characters with big emotional eyes, "
    "Indian village set (mud walls, neem trees, clay pots), "
    "ultra-detailed, light film grain, rich color grade. "
)
SHORTS_TAIL_LOCK = (
    " Vertical 9:16 composition. One single clean frame: exactly one of each "
    "character, correct anatomy, one head and two arms each, no duplicated or "
    "merged figures, no extra limbs, no distorted faces. Everyone fully "
    "clothed. No text, no letters, no words, no numbers, no logos, no watermark."
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
    " WIDE 16:9 landscape composition, dark Indian village at night, moonlight, "
    "fog, deep shadows. Masterpiece quality, hyper-detailed, consistent "
    "character design across the whole story. One single clean frame: exactly "
    "one of each person, correct human anatomy, one head, two arms and two "
    "hands each, five fingers per hand, no duplicated or merged figures, no "
    "extra limbs, no melted or distorted faces. Spooky and eerie but NOT "
    "graphic: absolutely no blood, no gore, no wounds, no zombie — a ghostly, "
    "atmospheric scare suitable for a family audience. Absolutely no text, no "
    "letters, no watermark, no logo."
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

    # DeepSeek V4 Pro via env (GitHub secret DEEPSEEK_API_KEY) — FORCE override
    # so stale CONFIG_JSON model names (kimi-k3 etc.) cannot hit DeepSeek API.
    ds_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if ds_key:
        cfg["text_provider"] = "openai"
        cfg["openai_api_key"] = ds_key
        cfg["openai_base_url"] = (
            os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        ).rstrip("/")
        # ALWAYS deepseek model ids when using DeepSeek endpoint
        cfg["openai_model"] = "deepseek-v4-flash"  # ultra-low cost; switch to -pro if quality drops
        cfg["openai_max_output_tokens"] = int(cfg.get("openai_max_output_tokens") or 8192)
        cfg["openai_min_output_tokens"] = int(cfg.get("openai_min_output_tokens") or 2048)
        # Clear any leftover kimi/anthropic model hints that call sites might read
        cfg["anthropic_model"] = cfg.get("anthropic_model")  # leave alone if unused
        log.info(
            "Text provider: DeepSeek (%s) via %s",
            cfg["openai_model"], cfg["openai_base_url"],
        )
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
def llm_text(cfg, system_prompt, user_prompt, max_tokens, want_json=False):
    """Ask the configured writing model for one block of text.

    `want_json` puts the OpenAI-compatible back end into JSON mode. Kimi K3 is a
    reasoning model and, left free, sometimes returns its thinking as prose with
    the JSON missing or malformed — which surfaced as "No JSON object found" and
    sank the whole video. JSON mode forces a single valid object, so the story
    call stops failing intermittently.

    `text_provider` picks the back end:
      "anthropic" (default) — Claude, via the Anthropic SDK.
      "openai"              — ANY OpenAI-compatible endpoint, which covers
                              OpenRouter and the free gateways, set through
                              `openai_base_url`, `openai_api_key`, `openai_model`.

    Keeping both behind one call means the scripts can move to a free model
    when Claude credit runs low without touching the six places that write
    something, and means only one place has to know that Anthropic wants a
    separate `system` argument while OpenAI-compatible APIs want a system
    message inside the list.
    """
    provider = (cfg.get("text_provider") or "anthropic").strip().lower()

    if provider in ("openai", "openrouter", "openai-compatible"):
        import requests
        base = (cfg.get("openai_base_url")
                or "https://openrouter.ai/api/v1").rstrip("/")
        key = (cfg.get("openai_api_key") or "").strip()
        if not key:
            raise RuntimeError("openai_api_key missing in config.json.")
        model = cfg.get("openai_model", "deepseek/deepseek-chat")
        # Two clamps, both learned from Kimi K3:
        #  * cap — Claude allows 32k output and the slideshow asks for it, but
        #    OpenAI-compatible models cap far lower and reject an over-ask. The
        #    longest real answer (a ~750-word Hindi story) is ~3k tokens, so
        #    8192 never truncates.
        #  * floor — K3 is a REASONING model: it spends hidden "thinking" tokens
        #    out of the same max_tokens budget before writing any content. The
        #    hook call asks for only 200, and K3 burns all of it thinking and
        #    returns an EMPTY string. Giving every call room for the reasoning
        #    plus its answer is what makes the short calls come back at all.
        # K3's hidden reasoning can run several thousand tokens BEFORE it writes
        # a word, out of the same budget. At 6000 the story came back empty —
        # reasoning had eaten all of it. The floor gives every call enough room
        # for the thinking plus its answer; the cap keeps the slideshow's 32k
        # ask within what the model accepts.
        is_deepseek = "deepseek" in str(model).lower() or "deepseek.com" in base
        # DeepSeek V4 defaults to thinking mode — burns tokens and often returns
        # prose without a JSON object. Disable thinking for structured calls.
        if is_deepseek:
            cap = int(cfg.get("openai_max_output_tokens", 8192))
            floor = int(cfg.get("openai_min_output_tokens", 2048))
        else:
            cap = int(cfg.get("openai_max_output_tokens", 16384))
            floor = int(cfg.get("openai_min_output_tokens", 10000))
        want = min(max(max_tokens, floor), cap)

        sys_prompt = system_prompt
        if want_json:
            sys_prompt = (
                system_prompt.rstrip()
                + "\n\nCRITICAL: Reply with ONE valid JSON object only. "
                "No markdown fences, no explanation, no thinking out loud."
            )
        payload = {"model": model, "max_tokens": want,
                   "messages": [{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user_prompt}]}
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        if is_deepseek:
            # Official DeepSeek V4: disable thinking for reliable JSON
            payload["thinking"] = {"type": "disabled"}

        import time as _t
        r = None
        for attempt in range(1, 5):
            r = requests.post(f"{base}/chat/completions",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=600)
            if r.status_code < 400 or r.status_code not in (429, 500, 502, 503, 529):
                break
            wait = 5 * attempt
            log.warning("%s transient [%s] (try %d/4); retrying in %ds...",
                        model, r.status_code, attempt, wait)
            _t.sleep(wait)
        if r.status_code >= 400:
            raise RuntimeError(f"{model} failed [{r.status_code}]: {r.text[:400]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            # Some reasoning models put useful text in reasoning_content only
            if not text:
                text = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"{model} returned no text: {str(data)[:400]}")
        if not text:
            raise RuntimeError(
                f"{model} returned empty content (tokens may have gone to "
                f"hidden reasoning). Response keys: {list(data.get('choices',[{}])[0].get('message',{}).keys())}"
            )
        return text

    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("anthropic_model", "claude-opus-4-8")

    # The SDK insists on streaming once max_tokens is this large.
    if max_tokens > 8000:
        with client.messages.stream(
            model=model, max_tokens=max_tokens, system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            return "".join(
                event.delta.text for event in stream
                if event.type == "content_block_delta"
                and getattr(event.delta, "type", "") == "text_delta")
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}])
    return "".join(b.text for b in resp.content if b.type == "text")


def _extract_json(text):
    """Return the first {...} JSON object found in `text`, ignoring ``` fences."""
    import re
    text = (text or "").strip()
    if not text:
        raise ValueError("No JSON object found in model response.")
    # strip markdown fences anywhere
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        preview = text[:240].replace("\n", " ")
        raise ValueError(
            f"No JSON object found in model response. Preview: {preview!r}"
        )
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
    import random

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

RECURRING CAST — use these EXACT characters (ids in brackets):
{_cast_id_list()}

Full looks (for your understanding only — do NOT rewrite them anywhere):
{MULI_CAST}

Rules:
- MULI DIDI must be central to the story. Use only the fixed cast above.
- Style: cinematic 3D Pixar-style cartoon, vertical 9:16, Indian village, warm
  emotional lighting. Visual mood for THIS video: {flavor}.
- Story arc across the {num_scenes} scenes: an immediate THREAT (a villain, a
  monster, a disaster, someone in danger) -> the stakes get worse -> Muli Didi's
  team acts with courage -> a physical turn where the danger is beaten -> short
  moral. This channel's three best Shorts are all action/rescue (Barf Pahalwan
  duel, the rakshas temple, saving the arrogant seth) at 2-5% like rates; its
  quiet emotional-sacrifice shorts sit at 1-30 views. Danger first, feelings
  inside the danger — never a slow domestic scene.
- MULI DIDI is in almost every scene as the emotional centre. Often include family: husband Garib Aalu, kids Chunnu & Munni, Muli Maa / Muli Papa, brother Moolchand, or saas Aloo Maa — so the home feels full, not only Muli alone. Other vegetables
  and animals (cat, dog, parrot, cow, deer, owl, monkey, bird) may appear as
  helpers or side characters — use at most 1 animal per scene when the topic
  calls for it. Animals attract viewers; keep them cute and family-safe.
- Rotate the THREAT so no two shorts feel alike: Barf Pahalwan, a rakshas, a
  flood, a fire, an earthquake, thieves, a collapsing bridge, a sea storm.
  Emotional beats belong inside the rescue, not instead of it.
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
    - "characters": an array of the cast IDS visible in this beat, e.g.
      ["muli_didi", "garib_aalu"]. Use the exact ids listed above, nothing else.
      AT MOST {MAX_CHARS_PER_SCENE} ids per scene. Aim for 2 characters in most scenes (Muli Didi + one friend/animal/villager) so the supporting cast is visible across the short. Only use 1 character for pure close-up emotion. Never exceed {MAX_CHARS_PER_SCENE} — more bodies confuse the image model.
    - "action": ONE English sentence describing ONLY what is happening in this
      frame — the pose, the emotion on their faces, the village location, the
      props, the camera framing. Do NOT describe what the characters look like,
      their colours, their bodies or their clothes, and do NOT mention art style,
      render quality or aspect ratio: all of that is added automatically and
      must stay identical in every scene. Keep exactly the cast listed in
      "characters" in frame — no extra background people.
    - "video_prompt": detailed ENGLISH motion for image-to-video: slow camera push-in or gentle parallax, cloth/leaves moving in breeze, blinking eyes, subtle body sway — make it feel like a living animated film frame, not a still.
    - "narration_hi": this scene's portion of the Hindi narration. Joined in
      order, all narration_hi together = the full Narration.

Constraints: EXACTLY {num_scenes} scenes. Muli Didi ALWAYS central. Family-safe. Vary the plot type. Output valid JSON only.
"""

    raw = llm_text(cfg, system_prompt, user_prompt, 6000, want_json=True)
    try:
        data = json.loads(_extract_json(raw))
    except (ValueError, json.JSONDecodeError) as first_err:
        log.warning("Story JSON parse failed (%s); retrying once...", first_err)
        retry_user = (
            user_prompt
            + "\n\nPREVIOUS REPLY WAS INVALID. Output ONLY a single JSON object "
            "with keys Title, Description, Tags, Narration, Scenes. No other text."
        )
        raw = llm_text(cfg, system_prompt, retry_user, 6000, want_json=True)
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
    # The image prompt is ASSEMBLED HERE from the locked blocks, not taken from
    # the model — that is what keeps the cast identical scene to scene. A model
    # that ignored the schema and sent a whole prompt anyway is still honoured
    # (better an off-style frame than a dropped beat), just logged as a miss.
    scenes = []
    for sc in data["Scenes"]:
        if not isinstance(sc, dict):
            continue
        action = str(sc.get("action") or "").strip()
        if action:
            img = build_scene_prompt(sc.get("characters"), action, flavor)
        else:
            img = str(sc.get("image_prompt") or sc.get("visual_prompt") or "").strip()
            if img:
                log.warning("Scene came back as a freehand prompt, not "
                            "characters+action — cast may drift on this frame.")
        vid = str(sc.get("video_prompt") or "").strip() or action or img
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
        hook = llm_text(cfg, system, user, 200).strip()
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
    import random

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

    raw = llm_text(cfg, system_prompt, user_prompt, 4000, want_json=True)
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
  that moment. Each object has:
    - "characters": an array of the "name" values (from Characters above) who
      are visible in this beat. Show whoever the narration is following right
      now — the farmer while the story follows him, the chudail once she
      appears. Use the names exactly as spelled in Characters.
    - "action": ONE English sentence describing ONLY what happens in this frame
      — the pose, the expression, where they are in the dark Indian village at
      night, the props, the camera framing. Do NOT describe what anyone looks
      like (age, face, hair, skin, clothes) and do NOT mention art style, render
      quality or aspect ratio: their "look" sentences and the style are pasted
      in automatically, word for word, so the same person stays the same person
      in every scene. Rewriting them is what makes the face change.

Constraints: Narration in Hindi about {words} words. EXACTLY {n} scenes, in
narration order. Output valid JSON only.
"""

    # A multi-scene story is still a sizable generation; the SDK requires
    # streaming once max_tokens is this high, so collect the stream.
    raw = llm_text(cfg, system_prompt, user_prompt, 32000, want_json=True)
    data = json.loads(_extract_json(raw))

    for key in ("TitleHindi", "TitleEnglish", "Description", "Tags",
                "Characters", "Narration", "Scenes"):
        if key not in data or not data[key]:
            raise ValueError(f"Claude slideshow response missing key: {key}")

    # {lowercased name: look} — the single source of truth for how each person
    # is worded, reused verbatim in every beat they appear in.
    cast_map = {}
    for c in data["Characters"]:
        if isinstance(c, dict) and str(c.get("name", "")).strip() \
                and str(c.get("look", "")).strip():
            cast_map[str(c["name"]).strip().lower()] = str(c["look"]).strip()
    if not cast_map:
        raise ValueError("Slideshow response produced no usable characters.")
    log.info("Cast: %s", ", ".join(cast_map)[:400])

    # Assemble the channel's title convention, trimming the hook if the whole
    # thing would blow past YouTube's 100-character limit.
    hook = f"{str(data['TitleHindi']).strip()} | {str(data['TitleEnglish']).strip()}"
    suffix = f" | {SLIDESHOW_TITLE_SUFFIX}"
    data["Title"] = (hook[:100 - len(suffix)] + suffix)[:100]
    data["Tags"] = _normalize_tags(data["Tags"], SLIDESHOW_TAGS)
    data["Description"] = _add_hashtags_to_description(data["Description"], SLIDESHOW_HASHTAGS)

    # Prompts are ASSEMBLED HERE from the cast sheet, not taken from the writer.
    # A scene that came back as a freehand visual_prompt is still used so the
    # story keeps its beat, but it is the one frame that can drift.
    prompts, freehand = [], 0
    for sc in data["Scenes"]:
        if not isinstance(sc, dict):
            if str(sc).strip():
                prompts.append(str(sc).strip())
                freehand += 1
            continue
        action = str(sc.get("action") or "").strip()
        if action:
            prompts.append(build_slide_prompt(cast_map, sc.get("characters"),
                                              action))
        elif str(sc.get("visual_prompt") or "").strip():
            prompts.append(str(sc["visual_prompt"]).strip())
            freehand += 1
    if not prompts:
        raise ValueError("Slideshow response produced no scene prompts.")
    if freehand:
        log.warning("%d/%d slides came back as freehand prompts instead of "
                    "characters+action — those faces may drift.",
                    freehand, len(prompts))
    data["ScenePrompts"] = prompts

    log.info("Slideshow story ready. Title: %s | %d scenes | narration %d chars",
             data["Title"], len(prompts), len(str(data["Narration"])))
    return data


def _cast_id_list():
    """The cast as `id — NAME` lines, so the writer picks ids it cannot misspell
    into a character that then gets silently dropped from the frame."""
    return "\n".join(
        f"  {cid} — {look.split(':', 1)[0]}"
        for cid, look in MULI_CAST_SHEET.items()
    )


def build_scene_prompt(characters, action, flavor):
    """Assemble one Shorts scene prompt out of LOCKED blocks.

    Consistency does not come from the seed — Flux rebuilds the whole image from
    the prompt, so when the writer paraphrases a character (the full description
    in scene 1, then just "radish woman in a pink saree" by scene 4) the model
    draws a different person. Telling the writer to copy the look "verbatim"
    never held: paraphrasing is what language models do. So the writer no longer
    authors the prompt at all. It names which cast members are in the beat and
    what happens; the style and the character wording are pasted in here,
    identical to the character, for every scene of every video.

    `characters` are MULI_CAST_SHEET keys; unknown names are dropped, and an
    empty result falls back to the heroine so a frame is never castless.

    The list is also capped at MAX_CHARS_PER_SCENE. Every extra body in one
    prompt makes the model blend their traits: the first uploaded video put
    four to six of the cast in a frame and came back with Muli's radish head
    growing on Aalu's shoulders and Muli herself drawn as an ordinary woman,
    while the two-character test frames held together. Locking the wording
    stops the writer drifting; it cannot stop the model mixing up a crowd, so
    the crowd has to go.
    """
    ids = [c for c in (characters or []) if c in MULI_CAST_SHEET]
    if not ids:
        ids = ["muli_didi"]
    if len(ids) > MAX_CHARS_PER_SCENE:
        log.warning("Scene asked for %d characters; keeping the first %d to "
                    "stop the model blending their looks.",
                    len(ids), MAX_CHARS_PER_SCENE)
        ids = ids[:MAX_CHARS_PER_SCENE]
    seen, looks = set(), []
    for c in ids:                              # de-dupe, keep the writer's order
        if c not in seen:
            seen.add(c)
            looks.append(MULI_CAST_SHEET[c])
    return (SHORTS_STYLE_LOCK + f"Colour mood: {flavor}. " + " ".join(looks)
            + " SCENE: " + str(action).strip().rstrip(".") + "." + SHORTS_TAIL_LOCK)


def build_slide_prompt(cast_map, characters, action):
    """The slideshow's equivalent of build_scene_prompt — same reasoning.

    `cast_map` is {name: look} built from the story's own Characters list, so
    each character's one look sentence is reused word for word in every beat
    they appear in instead of being re-described by the writer each time.
    """
    seen, looks = set(), []
    for n in (characters or []):
        key = str(n).strip().lower()
        if key in cast_map and key not in seen:
            seen.add(key)
            looks.append(cast_map[key])
    body = " ".join(looks)
    if body:
        body += " "
    return (SLIDESHOW_IMG_STYLE + body + "SCENE: "
            + str(action).strip().rstrip(".") + "." + SLIDESHOW_IMG_QUALITY)


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


def _gen_dims(width, height):
    """Shrink a requested frame size to Flux's comfortable pixel budget, keeping
    the aspect ratio and snapping both sides to a multiple of 16.

    Diffusion models work on a 16-pixel latent grid and are trained near one
    megapixel; an off-grid or far-oversized request is what smears edges and
    duplicates anatomy. 1080x1920 comes back as 848x1520 (1.29 MP).
    """
    import math
    budget = IMG_GEN_MEGAPIXELS * 1_000_000
    pixels = max(1, width * height)
    f = 1.0 if pixels <= budget else math.sqrt(budget / pixels)
    w = max(256, int(round(width * f / 16)) * 16)
    h = max(256, int(round(height * f / 16)) * 16)
    return w, h


def _free_image_url(prompt, seed=None, width=1080, height=1920, token=""):
    """Image URL from Pollinations.ai.

    Measured 2026-07-19, reproducibly and after a cooldown so it is not a rate
    limit: the ANONYMOUS tier serves a plain request (200, 768x768 jpeg) but
    answers ANY width/height/seed/model request with
        502 "Gen Sana request failed with 402: API key budget too low"
    i.e. those parameters route to a paid model this app has no key for. The
    old URL always sent all four, so every image request now fails outright.

    So without a token we ask for nothing but the prompt and take the 768x768
    square the free tier gives. With `pollinations_token` in config.json the
    full sized + seeded request is used, which is the only way to get a real
    1080x1920 frame — and the only way the shared seed does anything at all.

    width/height are the FINAL frame size wanted; the request is made at the
    model's native budget via _gen_dims and enlarged later by ffmpeg.
    """
    from urllib.parse import quote
    import random
    url = "https://image.pollinations.ai/prompt/" + quote(prompt)
    if not token:
        return url + "?nologo=true"          # nologo is free; the rest are not
    if seed is None:
        seed = random.randint(1, 9_000_000)
    gw, gh = _gen_dims(width, height)
    return (url + f"?width={gw}&height={gh}&nologo=true&model=flux&seed={seed}"
            + f"&token={quote(token)}")


def _hugging_face_image(cfg, prompt, out_path, width, height, seed=None):
    """Render one image through Hugging Face Inference Providers, straight to
    out_path. Returns True on success.

    This is the replacement for Pollinations, whose free tier stopped serving
    new generations on 2026-07-19 (it now answers anything that is not already
    in its cache with "402 API key budget too low" — a single extra space in a
    known-good prompt is enough to fail it, so no prompt wording brings it
    back). HF still honours width/height/seed, which Pollinations' free path
    never did, so this is also the first provider on which the shared seed does
    anything at all.

    Note this is metered against the account's inference credits, so a run that
    starts failing here is far more likely to be an exhausted allowance than a
    bug — the error text says so and is logged in full.
    """
    from huggingface_hub import InferenceClient
    token = (cfg.get("hf_token") or "").strip()
    if not token:
        raise RuntimeError("hf_token missing in config.json.")
    client = InferenceClient(
        provider=cfg.get("hf_provider", "nscale"), api_key=token)
    gw, gh = _gen_dims(width, height)
    kwargs = {"width": gw, "height": gh}
    if seed is not None:
        kwargs["seed"] = seed
    img = client.text_to_image(
        prompt, model=cfg.get("hf_image_model", HF_IMAGE_MODEL), **kwargs)
    img.save(out_path)
    return True


# Cloudflare Workers AI. Free allocation is 10,000 neurons PER DAY, reset at
# 00:00 UTC — a daily budget rather than a one-off pot, which is why this
# outlives the sources that quietly turned paid. Measured from Cloudflare's own
# pricing on 2026-07-21: flux-1-schnell costs 4.8 neurons per 512x512 tile plus
# 9.6 per step, so one 1080x1920 frame at 8 steps is ~134 neurons. A day's work
# here (4 short scenes + 26 slideshow stills) is ~4,000 — about 40% of the free
# allowance, leaving room for retries.
CF_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_STEPS = 8                      # flux-1-schnell's maximum, and its best look


def _cloudflare_image(cfg, prompt, out_path, width, height, seed=None):
    """Render via Cloudflare Workers AI flux-1-schnell, then cover-crop to size.

    API schema (2026) accepts only prompt + steps — width/height/seed are
    rejected. We resize/crop to the caller's 9:16 or 16:9 with Pillow.
    """
    import base64
    import requests
    from io import BytesIO
    from PIL import Image

    account = (cfg.get("cloudflare_account_id") or "").strip()
    token = (cfg.get("cloudflare_api_token") or "").strip()
    if not account or not token:
        raise RuntimeError(
            "cloudflare_account_id / cloudflare_api_token missing in config.json.")

    model = cfg.get("cloudflare_image_model", CF_IMAGE_MODEL)
    payload = {
        "prompt": prompt,
        "steps": int(cfg.get("cloudflare_steps", CF_STEPS)),
    }

    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
        headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=240)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Cloudflare image failed [{r.status_code}]: {r.text[:300]}")

    if "json" in r.headers.get("content-type", ""):
        data = r.json()
        b64 = (data.get("result") or {}).get("image")
        if not b64:
            raise RuntimeError(f"Cloudflare returned no image: {str(data)[:300]}")
        payload_bytes = base64.b64decode(b64)
    else:
        payload_bytes = r.content

    tw, th = int(width), int(height)
    img = Image.open(BytesIO(payload_bytes)).convert("RGB")
    # cover-crop to target aspect, then resize
    sw, sh = img.size
    target_aspect = tw / max(th, 1)
    src_aspect = sw / max(sh, 1)
    if src_aspect > target_aspect:
        # too wide — crop sides
        new_w = int(sh * target_aspect)
        left = max(0, (sw - new_w) // 2)
        img = img.crop((left, 0, left + new_w, sh))
    else:
        # too tall — crop top/bottom
        new_h = int(sw / target_aspect)
        top = max(0, (sh - new_h) // 2)
        img = img.crop((0, top, sw, top + new_h))
    img = img.resize((tw, th), Image.Resampling.LANCZOS)
    img.save(out_path, quality=95)
    return True



def _hf_image_url(cfg, prompt, width, height):
    """One Higgsfield Soul text2image render -> the finished image URL.

    Soul only accepts a fixed set of sizes, so the orientation of the frame the
    caller wants picks between the vertical and the landscape one rather than
    the exact pixels being requested.
    """
    A = HIGGSFIELD_API
    api_key = (cfg.get("higgsfield_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("higgsfield_api_key missing in config.json.")
    size = (cfg.get("higgsfield_image_size") or A["image_size"]) \
        if height >= width else A["image_size_landscape"]
    return _hf_submit_and_wait(
        A,
        {"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
        A["text2image_path"],
        {"prompt": prompt, "width_and_height": size},
        kind="image",
    )


def _looks_like_image(path, min_side=256):
    """True only if the file decodes as a COMPLETE image of a sane size.

    A byte-count check is not enough: Pollinations answers a rate-limit or a
    content block with a small stub or a truncated body, and those sailed past
    the old `> 5000 bytes` test straight into the finished video.
    """
    try:
        if os.path.getsize(path) <= 5000:
            return False
    except OSError:
        return False
    try:
        from PIL import Image
    except ImportError:                       # Pillow missing -> size check only
        return True
    try:
        with Image.open(path) as im:
            im.verify()                       # catches truncated / corrupt data
        with Image.open(path) as im:          # verify() leaves the handle spent
            im.load()
            w, h = im.size
        return w >= min_side and h >= min_side
    except Exception:                                         # noqa: BLE001
        return False


def _warm_free_image(url):
    """Fetch the free image once so it is generated + cached before Higgsfield
    pulls it. Returns True if it looks like a valid image."""
    import requests
    r = requests.get(url, timeout=120)
    return r.status_code == 200 and len(r.content) > 5000


# --------------------------------------------------------------------------- #
#  STEP 2b — Hindi voiceover (free edge-tts)
# --------------------------------------------------------------------------- #
# Microsoft's free endpoint intermittently answers a long request with no audio
# at all ("NoAudioReceived") — most often on a big block of text from a cloud
# IP, which is exactly the slideshow's ~3000-character narration on the runner.
# Splitting the narration into smaller pieces and retrying each makes the step
# reliable, because a piece is far less likely to come back empty and a stray
# failure costs one retry instead of the whole run.
TTS_CHUNK_CHARS = 550
TTS_TRIES = 4


def _split_for_tts(text, limit=TTS_CHUNK_CHARS):
    """Break narration into <=limit-char pieces on sentence-ish boundaries."""
    import re
    text = " ".join(text.split())
    # Hindi danda and western sentence enders, keeping the delimiter attached.
    sentences = re.split(r"(?<=[।?!.])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > limit:                 # a single very long sentence
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 <= limit:
            cur = f"{cur} {s}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()] or [text]


def _tts_one(narration, out_path, voice, rate, pitch):
    """One edge-tts attempt. Raises on an empty/failed result."""
    import asyncio
    import edge_tts

    async def _run():
        await edge_tts.Communicate(
            narration, voice, rate=rate, pitch=pitch).save(out_path)

    asyncio.run(_run())
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("TTS produced an empty audio file.")


def _elevenlabs_key(cfg):
    """Prefer env (GitHub Actions secret), then config.json."""
    return (
        (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        or (cfg.get("elevenlabs_api_key") or "").strip()
    )


def _elevenlabs_voice_id(cfg):
    return (
        (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
        or (cfg.get("elevenlabs_voice_id") or "").strip()
        or "RABOvaPec1ymXz02oDQi"
    )


def _tts_elevenlabs(narration, out_path, cfg):
    """One ElevenLabs request -> mp3. Raises on failure."""
    import requests
    key = _elevenlabs_key(cfg)
    vid = _elevenlabs_voice_id(cfg)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}"
    headers = {
        "xi-api-key": key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    model = (cfg.get("elevenlabs_model") or "eleven_multilingual_v2").strip()
    payload = {
        "text": narration,
        "model_id": model,
        "voice_settings": {
            "stability": float(cfg.get("elevenlabs_stability", 0.5)),
            "similarity_boost": float(cfg.get("elevenlabs_similarity", 0.75)),
            "style": float(cfg.get("elevenlabs_style", 0.2)),
        },
    }
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs HTTP {r.status_code}: {(r.text or '')[:240]}"
        )
    if not r.content:
        raise RuntimeError("ElevenLabs returned empty audio body.")
    with open(out_path, "wb") as f:
        f.write(r.content)
    if os.path.getsize(out_path) < 500:
        raise RuntimeError("ElevenLabs audio file too small.")


def synthesize_hindi_voice(cfg, narration, out_path, _tts_voice_override=None,
                           _tts_rate_override=None, _tts_pitch_override=None):
    """Generate Hindi voiceover mp3. ElevenLabs first (if key), else edge-tts.

    Returns (out_path, duration_seconds).
    """
    import time
    import imageio_ffmpeg
    from moviepy import AudioFileClip

    narration = (narration or "").strip()
    if not narration:
        narration = "..."

    # ---- Prefer ElevenLabs when key is available ----
    if _elevenlabs_key(cfg) and not _tts_voice_override:
        last = None
        for attempt in range(3):
            try:
                log.info(
                    "TTS: ElevenLabs voice=%s model=%s (try %s/3)...",
                    _elevenlabs_voice_id(cfg),
                    cfg.get("elevenlabs_model") or "eleven_multilingual_v2",
                    attempt + 1,
                )
                _tts_elevenlabs(narration, out_path, cfg)
                a = AudioFileClip(out_path)
                dur = float(a.duration)
                a.close()
                log.info(
                    "TTS: ElevenLabs ready -> %s (%.1fs)",
                    os.path.basename(out_path), dur,
                )
                return out_path, dur
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("ElevenLabs try %s failed: %s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        log.warning(
            "ElevenLabs failed after retries (%s); falling back to edge-tts.",
            last,
        )

    # ---- Free Microsoft edge-tts fallback ----
    voice = _tts_voice_override or cfg.get("tts_voice", TTS_VOICE)
    rate = _tts_rate_override if _tts_rate_override is not None else cfg.get("tts_rate", TTS_RATE)
    pitch = _tts_pitch_override if _tts_pitch_override is not None else cfg.get("tts_pitch", TTS_PITCH)

    chunks = _split_for_tts(narration)
    log.info(
        "TTS: generating Hindi voiceover (%s, rate %s, pitch %s, %d piece%s)...",
        voice, rate, pitch, len(chunks), "s" if len(chunks) != 1 else "",
    )

    wd = os.path.dirname(out_path) or "."
    stem = os.path.splitext(os.path.basename(out_path))[0]
    parts, tmp = [], []

    for i, chunk in enumerate(chunks):
        part = out_path if len(chunks) == 1 else os.path.join(wd, f"_{stem}_p{i}.mp3")
        last = None
        for attempt in range(TTS_TRIES):
            try:
                _tts_one(chunk, part, voice, rate, pitch)
                last = None
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.5 * (attempt + 1))
        if last is not None:
            raise RuntimeError(
                f"edge-tts failed for chunk {i + 1}/{len(chunks)} after "
                f"{TTS_TRIES} tries: {last}"
            )
        parts.append(part)
        if len(chunks) > 1:
            tmp.append(part)

    if len(chunks) > 1:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        listfile = os.path.join(wd, f"_tts_{stem}_list.txt")
        tmp.append(listfile)
        with open(listfile, "w", encoding="utf-8") as f:
            for pth in parts:
                f.write(f"file '{os.path.abspath(pth)}'\n")
        _run_ffmpeg([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i",
            listfile, "-c:a", "libmp3lame", "-b:a", "192k", out_path,
        ])
        for pth in tmp:
            try:
                os.remove(pth)
            except OSError:
                pass

    a = AudioFileClip(out_path)
    dur = float(a.duration)
    a.close()
    log.info("TTS: voiceover ready -> %s (%.1fs)", os.path.basename(out_path), dur)
    return out_path, dur


# Breath of silence left after each scene's line, so the cut lands on the pause
# rather than on top of the next word.
SCENE_VOICE_GAP_S = 0.35


def synthesize_scene_voices(cfg, scenes, out_path):
    """Voice each scene's OWN line, then join them into one track.

    Returns (out_path, total_duration, [per_scene_duration, ...]).

    The per-scene durations are what keep picture and voice together: each
    scene is held on screen for exactly as long as its own narration takes.
    Splitting one long voiceover into equal slices instead is what made the
    picture drift out of step with the story.
    """
    import imageio_ffmpeg
    from moviepy import AudioFileClip

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    wd = os.path.dirname(out_path) or "."
    parts, durs, tmp = [], [], []

    for i, sc in enumerate(scenes, 1):
        line = (sc.get("narration_hi") or "").strip()
        if not line:
            line = "..."
        raw = os.path.join(wd, f"_vo_raw_{i}.mp3")
        pad = os.path.join(wd, f"_vo_{i}.mp3")
        tmp += [raw, pad]
        synthesize_hindi_voice(cfg, line, raw)
        _run_ffmpeg([ffmpeg, "-y", "-i", raw, "-af",
                     f"apad=pad_dur={SCENE_VOICE_GAP_S}",
                     "-c:a", "libmp3lame", "-b:a", "192k", pad])
        a = AudioFileClip(pad)
        durs.append(float(a.duration))
        a.close()
        parts.append(pad)

    listfile = os.path.join(wd, "_vo_list.txt")
    tmp.append(listfile)
    with open(listfile, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    _run_ffmpeg([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                 "-c:a", "libmp3lame", "-b:a", "192k", out_path])

    for p in tmp:
        try:
            os.remove(p)
        except OSError:
            pass

    total = sum(durs)
    log.info("TTS: %d scene lines voiced (%.1fs total; %s)", len(durs), total,
             ", ".join(f"{d:.1f}s" for d in durs))
    return out_path, total, durs


# --------------------------------------------------------------------------- #
#  STEP 2c — Scene images (Higgsfield Soul, 3D Pixar style)
# --------------------------------------------------------------------------- #
FREE_IMAGE_TRIES = 3


def generate_scene_image(cfg, visual_prompt, out_path, width=1080, height=1920,
                         base_seed=None):
    """Render one image (default 9:16; pass width/height for 16:9 etc.) and
    download it to out_path.

    Provider comes from cfg["image_provider"]:
      "higgsfield"  — Soul text2image. Costs credits, but it is the only source
                      that returns a real 1152x2048 vertical frame.
      "pollinations"/"free" — the free tier. Measured 2026-07-19: it serves a
                      short prompt as a 768x768 square and answers anything
                      longer, sized or seeded with "402 API key budget too low".
                      Every prompt this agent builds is far past that, so this
                      path now fails outright unless pollinations_token is set.

    `base_seed`, when given, is shared by every scene in one video. On its own a
    shared seed does NOT hold a character's face steady — Flux re-derives the
    whole image from the prompt, so a reworded description draws a different
    person no matter what the seed is. It only pays off once the prompt itself
    is locked (see build_scene_prompt): near-identical prompt + identical seed
    keeps the denoising path close, and the cast stays recognisable. Retries
    step the seed by one rather than randomising it, so a rerun is reproducible
    and a stuck generation still gets a way out.
    """
    import random
    import time as _time

    # The preferred provider first, then every other one that could still draw.
    # A single provider used to be the whole story, so when the Higgsfield API
    # wallet emptied ("403 Not enough credits") every frame failed and BOTH
    # pipelines died having published nothing. Falling through to a lesser
    # source keeps the channel running instead of going dark.
    preferred = (cfg.get("image_provider") or "cloudflare").strip().lower()
    if preferred == "hf":
        preferred = "huggingface"
    if preferred in ("cf", "workers-ai"):
        preferred = "cloudflare"
    # Cloudflare leads the fallbacks: its free allowance renews daily and it is
    # the only remaining free source that returns a full-size, correctly shaped
    # frame. Pollinations stays last as the square-only source of final resort.
    chain = [preferred] + [p for p in ("cloudflare", "huggingface",
                                       "higgsfield", "pollinations")
                           if p != preferred]

    # A missing key or an empty wallet will not come good on a retry, so those
    # move straight to the next provider rather than sleeping between attempts.
    fatal = ("not enough credits", "missing in config.json", "budget too low",
             "unauthorized", "invalid token", "401", "403")

    last = None
    for provider in chain:
        for attempt in range(1, FREE_IMAGE_TRIES + 1):
            try:
                seed = ((base_seed + attempt - 1) if base_seed is not None
                        else random.randint(1, 9_000_000))
                if provider == "cloudflare":
                    _cloudflare_image(cfg, visual_prompt, out_path,
                                      width, height, seed=seed)
                elif provider == "huggingface":
                    _hugging_face_image(cfg, visual_prompt, out_path,
                                        width, height, seed=seed)
                elif provider == "higgsfield":
                    _download_url(
                        _hf_image_url(cfg, visual_prompt, width, height),
                        out_path)
                else:
                    _download_url(_free_image_url(
                        visual_prompt, seed=seed, width=width, height=height,
                        token=cfg.get("pollinations_token", "").strip()),
                        out_path)
                if _looks_like_image(out_path):
                    if provider != preferred:
                        log.warning("Image drawn by FALLBACK provider '%s' — "
                                    "'%s' is unavailable.", provider, preferred)
                    return out_path
                last = "the download was not a complete image"
            except Exception as exc:                          # noqa: BLE001
                last = str(exc)[:160]

            if any(s in str(last).lower() for s in fatal):
                log.warning("Provider '%s' unusable (%s); next provider.",
                            provider, last)
                break
            if attempt < FREE_IMAGE_TRIES:
                log.warning("Image try %d/%d on '%s' failed (%s); retrying...",
                            attempt, FREE_IMAGE_TRIES, provider, last)
                _time.sleep(3 * attempt)   # back off; free tiers rate-limit

    raise RuntimeError(
        f"Image failed on every provider {chain}. Last error: {last}")


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
    video_prompt.

    Always returns EXACTLY one asset per scene, in order. A scene is retried
    with a wholesome reworded prompt (for NSFW false-positives), and if it still
    fails its neighbour's picture is reused rather than dropping the beat:
    dropping one left the remaining pictures to absorb the missing scene's
    narration, which broke both the story order and the voice sync for the whole
    rest of the video. Raises only if NO scene renders.
    """
    import random
    import shutil
    paths = [None] * len(scenes)
    total = len(scenes)
    kind = "clip" if animate else "image"
    ext = "mp4" if animate else "png"
    # One seed for every still in a short. This only holds the cast together
    # because build_scene_prompt already locked the wording, so consecutive
    # prompts differ by one clause; a shared seed on freely-worded prompts did
    # nothing at all.
    base_seed = random.randint(1, 9_000_000)
    for i, sc in enumerate(scenes, 1):
        img = sc.get("image_prompt", "")
        vid = sc.get("video_prompt", "") or img
        out = os.path.join(WORK_DIR, f"scene_{run_id}_{i}.{ext}")
        for attempt, ip in enumerate((img, _safe_prompt(img), img), 1):
            try:
                log.info("Higgsfield: scene %s %d/%d (try %d) ...",
                         kind, i, total, attempt)
                if animate:
                    generate_scene_clip(cfg, ip, vid, out)
                else:
                    generate_scene_image(cfg, ip, out, base_seed=base_seed)
                paths[i - 1] = out
                break
            except Exception as exc:                          # noqa: BLE001
                log.warning("Scene %d try %d failed (%s).", i, attempt,
                            str(exc)[:140])

    if not any(paths):
        raise RuntimeError("All scene assets failed to render.")

    # Fill any hole from the nearest rendered neighbour, so every scene still
    # has a picture and the beats stay lined up with the narration.
    for i, p in enumerate(paths):
        if p:
            continue
        donor = next((q for q in reversed(paths[:i]) if q), None) or \
                next((q for q in paths[i + 1:] if q), None)
        stand_in = os.path.join(WORK_DIR, f"scene_{run_id}_{i + 1}_fill.{ext}")
        shutil.copyfile(donor, stand_in)
        paths[i] = stand_in
        log.warning("Scene %d unrendered -> reusing %s to keep the beats "
                    "aligned.", i + 1, os.path.basename(donor))

    log.info("Rendered %d/%d scene %ss.", sum(
        1 for p in paths if not p.endswith(f"_fill.{ext}")), total, kind)
    return paths


def _safe_prompt(prompt):
    """
    Reword a scene image prompt to dodge over-eager NSFW filters WITHOUT losing
    the cute kids (they are the whole point of the channel) — add a wholesome
    qualifier so the filter reads it as harmless cartoon art.

    The qualifier goes INSIDE the locked style block rather than in front of it.
    Prepended, it introduced a competing art direction ("stylised
    non-realistic", "no realism") that overrode the channel's Pixar look, so a
    retried scene came back visibly off-model next to its neighbours — a second
    way for the same video to look inconsistent.
    """
    safe = ("Wholesome, family-friendly, innocent and heartwarming, all "
            "characters fully clothed. ")
    if prompt.startswith(SHORTS_STYLE_LOCK):
        return SHORTS_STYLE_LOCK + safe + prompt[len(SHORTS_STYLE_LOCK):]
    return safe + prompt


def render_slideshow_images(cfg, prompts, run_id):
    """
    Generate one FREE (Pollinations) image per slideshow scene prompt.

    Prompts arrive already assembled by build_slide_prompt (style + verbatim
    character looks + beat), so nothing is wrapped here — wrapping again would
    stack the style block twice. ONE seed is shared across the whole video,
    which now pays off because the prompts differ only in their action clause.
    `generate_scene_image`
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
        styled = str(prompt).strip()
        if SLIDESHOW_IMG_STYLE.strip() not in styled:   # freehand fallback path
            styled = SLIDESHOW_IMG_STYLE + styled + SLIDESHOW_IMG_QUALITY
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
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        system = (
            "You turn scene descriptions into SHORT English stock-video search "
            "queries. For each numbered scene output 2-4 plain keywords naming "
            "the concrete VISIBLE subject/action only (no style words, no "
            "punctuation, no camera terms). Return ONLY a JSON object of the "
            'form {"queries": ["kw kw", ...]} with one string per scene, in the '
            "same order and count."
        )
        raw = llm_text(cfg, system, numbered, 1200, want_json=True)
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


# Camera moves for the still frames. A daily channel cannot afford real AI
# video, and rigged 3D animation is out of scope, so the frames stay still —
# but a still that only slides sideways reads as a dead slideshow. Every scene
# instead gets a smooth ZOOM plus a diagonal drift (a proper Ken Burns move via
# zoompan), and the direction alternates and rotates per scene so consecutive
# frames feel distinct and the short has energy. Free: it is one ffmpeg filter,
# no API, no per-clip cost.
_PAN_CORNERS = [(0.12, 0.12), (0.88, 0.12), (0.88, 0.88), (0.12, 0.88),
                (0.50, 0.50)]


def _motion_vf(i, per, W, H, fps):
    """Return the -vf string that turns still image #i into a moving `per`-second
    9:16 frame: cover-fill to a 1.5x canvas, then zoompan a 12% zoom + a
    corner-to-corner drift. `crop` cannot animate its size (only x/y), which is
    why zoompan does the zoom. The 1.5x intermediate keeps zoompan's sub-pixel
    jitter invisible while staying light enough for a 3-4GB runner.
    """
    sw, sh = int(W * 1.5), int(H * 1.5)
    nf = max(1, round(per * fps))
    zoom = 0.12
    if i % 2 == 0:                                   # alternate in / out
        z = f"min(1+{zoom}*on/{nf},{1 + zoom:.3f})"
    else:
        z = f"max({1 + zoom:.3f}-{zoom}*on/{nf},1)"
    sx, sy = _PAN_CORNERS[i % len(_PAN_CORNERS)]
    ex, ey = _PAN_CORNERS[(i + 2) % len(_PAN_CORNERS)]
    fx = f"({sx}+({ex - sx})*on/{nf})"
    fy = f"({sy}+({ey - sy})*on/{nf})"
    x = f"(iw-iw/zoom)*{fx}"
    y = f"(ih-ih/zoom)*{fy}"
    return (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={nf}:s={W}x{H}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )


def _still_to_clip(img_path, out_path, dur, width, height, scene_index=0):
    """Turn a still image into a short moving mp4 clip so a still fallback can be
    stitched by the same clip assembler as real footage. Uses the shared
    zoom+pan camera move (low-RAM, threads=1)."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    fps = 30
    nf = max(1, round(dur * fps))
    # Cap the output with -frames:v, NOT -t. zoompan with d=NF on a -loop 1
    # input ignores -t and runs away emitting NF frames per looped input frame
    # (thousands of frames, effectively hanging); -frames:v stops it dead at NF.
    _run_ffmpeg([
        ffmpeg, "-y", "-loop", "1", "-i", img_path, "-an",
        "-vf", _motion_vf(scene_index, dur, width, height, fps),
        "-frames:v", str(nf),
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
                _still_to_clip(still, out, 5.0, width, height, scene_index=i)
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
# Tracks with a singer fight the narration — a listener hears a second voice
# under the story. Anything credited "feat." is a vocal track.
VOCAL_MARKERS = ("feat.", "feat ", "(vocal", "ft.")


def pick_music(mdir):
    """Choose one instrumental bed from `mdir`, or None if it holds nothing usable."""
    import glob
    import random

    tracks = []
    for ext in ("*.mp3", "*.wav", "*.m4a"):
        tracks += glob.glob(os.path.join(mdir, ext))
    playable = [t for t in tracks
                if not any(m in os.path.basename(t).lower()
                           for m in VOCAL_MARKERS)]
    if len(playable) < len(tracks):
        log.info("Skipped %d vocal track(s) in %s.",
                 len(tracks) - len(playable), os.path.basename(mdir) or mdir)
    if not playable:
        log.warning("No instrumental music in %s; voiceover only.", mdir)
        return None
    return random.choice(playable)


def _music_mix_filter(mvol):
    """Music bed that ducks itself under the narration.

    sidechaincompress drives the bed down whenever the voice is speaking and
    lets it swell back in the gaps, so the story stays intelligible without the
    music having to sit so low it may as well not be there.
    """
    return (
        f"[2:a]volume={mvol},aloop=loop=-1:size=2000000000[mus];"
        "[1:a]asplit=2[voc][key];"
        "[mus][key]sidechaincompress=threshold=0.02:ratio=8:attack=5:"
        "release=400[duck];"
        "[voc][duck]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )


def assemble_video(image_paths, voice_path, voice_dur, out_path,
                   music_dir=None, music_volume=None, width=1080, height=1920,
                   durations=None):
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
    fps = 30   # the per-scene zoom+pan headroom now lives in _motion_vf
    n = max(1, len(image_paths))
    # Each scene holds for its OWN narration line when the caller measured them;
    # equal slices are only a fallback for callers that pass one long voiceover.
    if durations and len(durations) == len(image_paths):
        pers = list(durations)
        pers[-1] += 1.0               # let the last picture breathe past the VO
    else:
        pers = [(voice_dur + 1.0) / n] * n
    wd = os.path.dirname(out_path) or "."
    tmp = []

    log.info("Assembling slideshow (%d scenes, %s, %dx%d)...",
             n, " / ".join(f"{p:.1f}s" for p in pers), W, H)
    segs = []
    for i, img in enumerate(image_paths):
        per = pers[i]
        seg = os.path.join(wd, f"_kb_{i}.mp4")
        tmp.append(seg)
        nf = max(1, round(per * fps))
        _run_ffmpeg([
            ffmpeg, "-y", "-loop", "1", "-i", img, "-an",
            # Shared zoom+pan camera move: a smooth zoom plus a diagonal drift,
            # direction rotating per scene, so the short reads as alive instead
            # of a static slide. The move's cover-fill also protects an
            # off-aspect still (e.g. a 768x768 square) from being stretched.
            "-vf", _motion_vf(i, per, W, H, fps),
            # -frames:v (not -t): zoompan on a looped input ignores -t and runs
            # away emitting thousands of frames; -frames:v stops it at exactly nf.
            "-frames:v", str(nf),
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

    music = pick_music(mdir) if mvol > 0 else None

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))
        cmd += ["-filter_complex", _music_mix_filter(mvol),
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
                            music_dir=None, music_volume=None,
                            durations=None):
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
    # Each clip runs for its OWN narration line when the caller measured them;
    # equal slices are only a fallback for one undivided voiceover.
    if durations and len(durations) == len(clip_paths):
        pers = list(durations)
        pers[-1] += 1.0
    else:
        pers = [(voice_dur + 1.0) / n] * n
    wd = os.path.dirname(out_path) or "."
    tmp = []

    log.info("Stitching %d animated clips (%s)...", n,
             " / ".join(f"{p:.1f}s" for p in pers))
    segs = []
    for i, clip in enumerate(clip_paths):
        per = pers[i]
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

    music = pick_music(mdir) if mvol > 0 else None

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
           "-i", voice_path]
    if music:
        cmd += ["-i", music]
        log.info("Background music: %s", os.path.basename(music))
        cmd += ["-filter_complex", _music_mix_filter(mvol),
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


def alert_failure(cfg, message):
    """Send a failure alert even when the run never got a usable config.

    A crash before load_config() left `cfg` as None, and the old code only
    notified `if cfg is not None` — so the failures that mattered most (a blank
    or malformed secret in the cloud) were the ones that stayed silent. This
    recovers the Telegram credentials from config.json or the environment when
    `cfg` is missing them, so a failed run always speaks up.
    """
    creds = dict(cfg) if isinstance(cfg, dict) else {}
    if not creds.get("telegram_bot_token") or not creds.get("telegram_chat_id"):
        try:
            with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
                creds = json.load(f)
        except Exception:                                     # noqa: BLE001
            pass
    creds.setdefault("telegram_bot_token",
                     os.environ.get("TELEGRAM_BOT_TOKEN"))
    creds.setdefault("telegram_chat_id",
                     os.environ.get("TELEGRAM_CHAT_ID"))
    send_telegram(creds, message)


# --------------------------------------------------------------------------- #
#  Orchestration — one full pipeline run
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  QUALITY GATE — audits every finished video before it is published
# --------------------------------------------------------------------------- #
def audit_video(video_path, pkg, scene_paths, scene_durs, run_id):
    """Grade the finished video against the channel's standing quality bar.

    Checks the things that have actually gone wrong on this channel — a story
    too short to follow, beats reused because a scene failed, a cast member
    missing, weak SEO, no music — and reports every category to Telegram, so a
    regression is visible on the notification instead of being found by watching
    the upload.

    Returns a printable report. It never blocks the upload: a flawed video that
    ships still teaches more than one that silently never appears.
    """
    import glob
    report, problems = [], []

    def check(label, ok, detail):
        report.append(f"{'✅' if ok else '⚠️'} {label}: {detail}")
        if not ok:
            problems.append(label)

    # 1. LENGTH — a story needs room to land; ~15s reads as nonsense.
    total = sum(scene_durs) if scene_durs else 0
    check("Length", 35 <= total <= 70, f"{total:.0f}s (target 40-60s)")

    # 2. SEQUENCE — every beat must have its own picture.
    fills = sum(1 for p in scene_paths if "_fill." in os.path.basename(p))
    check("Sequence", fills == 0,
          "all scenes rendered" if not fills else f"{fills} scene(s) reused")

    # 3. SYNC — each picture is cut to its own line.
    aligned = len(scene_paths) == len(scene_durs) == len(pkg.get("Scenes", []))
    check("Voice sync", aligned,
          f"{len(scene_paths)} scenes / {len(scene_durs)} voiced lines")

    # 4. CAST — Muli Didi leads every story.
    prompts = " ".join(s.get("image_prompt", "") for s in pkg.get("Scenes", []))
    muli = sum(1 for s in pkg.get("Scenes", [])
               if "radish" in s.get("image_prompt", "").lower())
    check("Muli Didi", muli >= 1, f"in {muli}/{len(pkg.get('Scenes', []))} scenes")

    # 5. HOOK — scene 1 must move, or the scroll never stops.
    hook_vid = (pkg.get("Scenes") or [{}])[0].get("video_prompt", "")
    check("Hook", len(hook_vid) > 40, f"{len(hook_vid)} chars of motion")

    # 6. SEO.
    title, tags = pkg.get("Title", ""), pkg.get("Tags", [])
    desc = pkg.get("Description", "")
    check("Title", 20 <= len(title) <= 100, f"{len(title)} chars")
    check("Tags", len(tags) >= 10, f"{len(tags)} tags")
    check("Description", len(desc) >= 60, f"{len(desc)} chars")

    # 7. MUSIC — present, and instrumental.
    beds = [b for ext in ("*.mp3", "*.wav", "*.m4a")
            for b in glob.glob(os.path.join(SHORTS_MUSIC_DIR, ext))]
    clean = [b for b in beds if not any(m in os.path.basename(b).lower()
                                        for m in VOCAL_MARKERS)]
    check("Music", bool(clean), f"{len(clean)} instrumental bed(s)")

    # 8. FILE.
    size = os.path.getsize(video_path) / 1e6 if os.path.exists(video_path) else 0
    check("File", size > 1, f"{size:.1f} MB")

    verdict = ("🟢 QUALITY: all checks passed" if not problems
               else f"🔴 QUALITY: {len(problems)} issue(s) — "
                    + ", ".join(problems))
    text = verdict + "\n" + "\n".join(report)
    for line in text.splitlines():
        (log.info if "✅" in line or "🟢" in line else log.warning)(line)
    return text


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
        # Voiced line-by-line so every scene can be held for exactly its own
        # narration -> picture and story stay in step.
        _, voice_dur, scene_durs = synthesize_scene_voices(
            cfg, pkg["Scenes"], voice_path)

        # 3. Scene visuals + assemble the 40-50s video ------------------- #
        smv = cfg.get("shorts_music_volume", 0.08)
        mdir = SHORTS_MUSIC_DIR
        if visual_mode == "stock_video":
            # FREE real stock footage (Pexels/Pixabay) -> motion clips.
            # Higgsfield NEVER hit -> 0 credits. Motion boosts retention.
            scene_paths = render_stock_clips(cfg, pkg["Scenes"], run_id,
                                             width=1080, height=1920)
            assemble_animated_video(scene_paths, voice_path, voice_dur,
                                    final_video, music_dir=mdir,
                                    music_volume=smv, durations=scene_durs)
        elif animate:
            # Higgsfield DoP animation (free image -> DoP motion). Main path.
            scene_paths = render_scene_assets(cfg, pkg["Scenes"], run_id, True)
            assemble_animated_video(scene_paths, voice_path, voice_dur,
                                    final_video, music_dir=mdir,
                                    music_volume=smv, durations=scene_durs)
        else:
            # Legacy FREE Pollinations stills + Ken Burns slideshow.
            scene_paths = render_scene_assets(cfg, pkg["Scenes"], run_id, False)
            assemble_video(scene_paths, voice_path, voice_dur, final_video,
                           music_dir=mdir, music_volume=smv,
                           durations=scene_durs)

        # 4. Quality gate + upload --------------------------------------- #
        audit = audit_video(final_video, pkg, scene_paths, scene_durs, run_id)
        url = upload_to_youtube(cfg, final_video, pkg)

        # 5. Notify ------------------------------------------------------ #
        send_telegram(
            cfg,
            "✅ Shorts uploaded successfully!\n\n"
            f"🎬 Title: {pkg['Title']}\n"
            f"📈 Topic: {trend['topic']} ({trend['source']})\n"
            f"🔗 {url}\n\n"
            f"{audit}",
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
        title = pkg["Title"] if pkg else "(content not generated)"
        alert_failure(
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
        title = pkg["Title"] if pkg else "(content not generated)"
        alert_failure(cfg, f"❌ News Short FAILED (id={run_id})\n"
                           f"Content: {title}\nError: {exc}")


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

        # The channel is shorts-only (one niche: Muli Didi). The chudail
        # slideshow stays off unless the config opts back in — set in the cloud
        # rather than the workflow file, which this token cannot edit. Exits
        # quietly so a cron-job.org trigger is a no-op, not a failure.
        if not cfg.get("enable_slideshow", False):
            log.info("Slideshow disabled (enable_slideshow not set); skipping.")
            return

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
        alert_failure(cfg, f"❌ Slideshow FAILED (id={run_id})\nError: {exc}")


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
