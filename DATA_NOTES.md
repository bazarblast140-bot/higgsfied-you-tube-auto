# Muli Didi Kahaniya — why the views stopped, and what the data says

Channel: `UCpX7NO1owC4-2oLD9KNGpWA` (@mulididikahaniya). **Shorts only.**
All figures pulled from vidIQ on 18 Aug 2026. Re-check before trusting them later.

## The state

| | value |
| --- | ---: |
| Videos | 166 |
| Lifetime views | 115,468 |
| **Views per video** | **696** |
| Last 30 days | 54 uploads, 12,865 views |
| **Views per video, last 30d** | **238** |

## Root cause: the used-topic list was never persisted

`pick_topic()` picks a random unused premise from `topic_bank.json`, marks it
used, and writes the file back **to the runner's disk**. Nothing committed that
file to the repo, and `actions/checkout@v4` starts every run from a clean
checkout — where `used_topics` is `[]`. So every scheduled run drew at random
from the *entire* bank with no memory of what had already aired.

With a 30-topic bank and 54 runs, uniform draws with replacement predict:

    30 x (1 - (29/30)^54) = 25.2 distinct topics
    => ~29 of 54 uploads are repeats

That matches what shipped. Repeated titles, with their view counts:

| Title | Times | Views |
| --- | ---: | --- |
| घमंडी बैंगन ने गरीब आलू का मज़ाक उड़ाया | 5 | 6, 2, 51, 50, 83 |
| भूखे बच्चों के लिए मूली ने… | 4 | 1, 2, 17, 20 |
| टनल में हर रात गाड़ी रोकती है वो | 3 | 4, 8, 4 |
| अमीर कद्दू सेठ और गरीब मटर नौकरानी | 3 | 1, 1, 4 |
| धोखा देकर भागा अमीर बैंगन | 2 | 5, 22 |
| बीमार माँ की दवाई के लिए चूड़ियाँ | 2 | 8, 27 |

The fix is in `.github/workflows/main.yml`: `permissions: contents: write` plus
a step that commits `topic_bank.json` back after each run.

That duplicates were shipped is measured. That YouTube *suppressed* the channel
for it is **likely, not proven** — the code's own comment already worried about
the "repetitive / mass-produced" flag, and views collapsed as the repeats piled
up, but nothing here confirms a penalty was applied.

## What actually works on this channel

The four best Shorts, and their like rates against a ~0.3% niche norm:

| Title | Views | Likes | Like rate |
| --- | ---: | ---: | ---: |
| घमंडी सेठ को भी बचाया! आलू काका की हिम्मत | 6,055 | 302 | **5.0%** |
| बर्फ पहलवान vs मूली दीदी की टीम — महा-दंगल | 2,132 | 70 | 3.3% |
| खोया मंदिर… बर्फ का राक्षस | 1,025 | 20 | 2.0% |
| बीमार माँ की दवाई के लिए कुर्बानी | 714 | 14 | 2.0% |

Three of the four are **action / adventure / rescue**. The daily quiet
emotional-sacrifice shorts sit at 1-30 views. When this channel does land, the
audience is real — a 5% like rate is 15x the niche norm — so the reach problem
is which stories get made, not how they are made.

Four data points is thin evidence. Treat "action beats quiet drama here" as a
working hypothesis to test, not a settled fact.

## What does NOT transfer

The `[sabzi A] ne diya [sabzi B] ko dhoka` pattern clears 250k-1.5M views on
**long-form 7-8 minute** vegetable channels. It does not carry to 40-second
Shorts: this channel's own two dhoka shorts got **5 and 22 views**. That
finding belongs to the long-form channel, not here.

## Dormant

`slideshow.yml` (long-form chudail horror, `workflow_dispatch` only) produced
~20 videos at 0-9 views each. It never fires on a schedule; leave it off.

## The learning loop

The pipeline used to pick a premise at random and never look back — it had no
way of knowing that `muli_action` earns views and `muli_drama` does not. It now
keeps `topic_performance.json`: every upload records its topic, category and
video id, and the next run fetches those videos' view counts through the public
YouTube Data API (`cfg["youtube_api_key"]` — public statistics, so no OAuth
scope beyond the upload token already in use).

Categories are then scored and the topic pick is weighted by that score. Four
rules keep the loop honest:

- **Median, not mean.** One 6,000-view outlier among a dozen 20-view uploads
  must not sell the whole category.
- **72-hour grace.** A video only votes once it has had time to earn views, so
  this morning's upload does not drag its category down.
- **Three samples minimum.** Below that a category is scored at the median of
  the ones that qualify, so it keeps getting tried instead of being written off.
- **30% stays uniform.** Without a floor, one lucky video would lock the channel
  into a single category forever.

If the API key is missing or the call fails, the pick falls back to uniform
random and the run continues.
