"""Make one Muli video the approved way, end to end.

    python make_muli_video.py "premise in hindi"        # build, do not upload
    python make_muli_video.py "premise" --upload        # build and publish

This is the recipe the channel signed off on: every character drawn from the
canonical cast sheet, every scene held for exactly its own narration line, real
motion rather than a pan over a still, the channel's own instrumental bed, and a
quality gate before anything is published.

Run it locally only. It spends Higgsfield Pro credits through the CLI, which the
cloud bot cannot do (consumer Pro is CLI-only, never headless) — the daily bot
stays on free stills.
"""
import argparse
import json
import os
import re
import subprocess
import time
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.chdir(BASE)

import imageio_ffmpeg                       # noqa: E402
import ultimate_shorts_agent as u           # noqa: E402

HF = r"C:\Users\91872\AppData\Local\higgsfield\bin\higgsfield.exe"
# EVERY sheet goes in as a reference, not just the main one. With only the main
# sheet attached, a story that called for a supporting villager drew whoever on
# that sheet looked closest — a scene written for Karela Dada came back with the
# garlic elder instead.
CAST_SHEETS = [Path(BASE) / "assets" / n for n in
               ("cast_sheet.jpg", "cast_sheet_2.jpg", "cast_sheet_3.jpg")]
FF = imageio_ffmpeg.get_ffmpeg_exe()

# kling2_6 is the cheapest motion that still holds the cast: 10 credits per 10s,
# where kling3_0_turbo wants 15 and veo returns "nsfw" on clothed cartoons.
VIDEO_MODEL = "kling2_6"
MAX_CLIP = 10.0            # the model's ceiling; lines regularly run longer
STRETCH_LIMIT = 1.45       # beyond this, slow motion stops reading as a choice
CREDITS_PER_CLIP = 10
CREDITS_PER_STILL = 2      # one nano_banana_pro draw, measured 2026-07-21


def credits():
    r = subprocess.run([HF, "account", "status"], capture_output=True,
                       text=True, encoding="utf-8")
    m = re.search(r"([\d.]+)\s+credits", (r.stdout or "") + (r.stderr or ""))
    return float(m.group(1)) if m else None


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"ffmpeg failed:\n{r.stderr[-600:]}")


def hf_generate(model, out, **params):
    """Submit one job, wait, and save the result. Raises with the reason."""
    cmd = [HF, "generate", "create", model, "--wait", "--wait-timeout", "12m",
           "--json"]
    for k, v in params.items():
        flag = f"--{k.replace('_', '-')}"
        # A list means the CLI wants the flag repeated, as it does for the
        # several cast sheets that make up the character library.
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            cmd += [flag, str(item)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    blob = (r.stdout or "") + (r.stderr or "")
    m = re.search(r'"result_url":\s*"(https[^"]+)"', blob)
    if not m:
        why = "nsfw" if '"status": "nsfw"' in blob else blob[-200:].strip()
        raise RuntimeError(f"{model} failed: {why}")
    Path(out).write_bytes(requests.get(m.group(1), timeout=300).content)
    return out


def _wholesome(prompt):
    """Reword a prompt that tripped the safety filter.

    The filter reads innocent things wrongly — a crying child, an embrace, a
    beggar's bare feet — and rejects frames of clothed cartoon vegetables. The
    wording is softened and the modesty stated outright rather than the beat
    being abandoned.
    """
    swaps = {"crying": "sad", "cries": "looks sad", "tears": "a sad face",
             "weeping": "sad", "sobbing": "quietly sad", "bare": "covered",
             "barefoot": "wearing simple sandals", "naked": "fully dressed",
             "dying": "very weak", "sick": "unwell", "beggar": "poor villager",
             "hugging": "standing close to", "embrace": "gentle namaste"}
    out = prompt
    for bad, good in swaps.items():
        out = re.sub(rf"\b{bad}\b", good, out, flags=re.I)
    return ("Wholesome family-friendly children's cartoon illustration. "
            "All characters are fully clothed in modest Indian village dress. "
            + out)


def draw_scene(scene, out):
    """Draw one scene FROM THE CAST SHEETS — the only thing that holds the cast.

    A safety-filter rejection is retried with softened wording instead of
    ending the run: these are clothed cartoon vegetables, so a refusal is a
    false positive, and giving up part-way wastes every frame already paid for.
    """
    lead = ("Use the EXACT characters from the reference cast sheets, same "
            "designs, same clothes. Match each named character to the one "
            "that actually bears that name on the sheets. ")
    refs = [str(p) for p in CAST_SHEETS]
    plain = scene["image_prompt"]
    # The reworded version is only worth trying against a safety refusal; a
    # dropped connection just needs the same request again, after a pause.
    attempts = [plain, _wholesome(plain), plain, _wholesome(plain)]
    last = None
    for n, body in enumerate(attempts, 1):
        try:
            return hf_generate("nano_banana_pro", out, prompt=lead + body,
                               image_references=refs, aspect_ratio="9:16")
        except RuntimeError as exc:
            last = exc
            if n < len(attempts):
                print(f"       draw attempt {n} failed ({str(exc)[-60:]}); "
                      f"retrying in {5 * n}s")
                time.sleep(5 * n)
    raise last


def animate_scene(scene, still, need, out, work):
    """Animate one scene so it covers `need` seconds without ever freezing."""
    motion = (scene["video_prompt"] + " Cartoon 3D animation, smooth natural "
              "motion, characters exactly as in the start image.")
    raw = work / f"raw_{out.stem}.mp4"
    hf_generate(VIDEO_MODEL, raw, prompt=motion, start_image=still,
                aspect_ratio="9:16", duration=10, sound="false")

    if need <= MAX_CLIP + 0.2:
        run([FF, "-y", "-i", str(raw), "-an", "-vf",
             "tpad=stop_mode=clone:stop_duration=1,fps=30",
             "-t", f"{need:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", str(out)])
    elif need <= MAX_CLIP * STRETCH_LIMIT:
        # A modest overrun becomes deliberate slow motion — cheaper than a clip.
        run([FF, "-y", "-i", str(raw), "-an", "-vf",
             f"setpts={need / MAX_CLIP:.4f}*PTS,fps=30", "-t", f"{need:.3f}",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             str(out)])
    else:
        # Too long for one clip: continue from its last frame so the motion
        # carries on instead of cutting or freezing.
        tail = work / f"tail_{out.stem}.jpg"
        run([FF, "-y", "-sseof", "-0.1", "-i", str(raw), "-frames:v", "1",
             "-q:v", "2", str(tail)])
        raw_b = work / f"raw_{out.stem}_b.mp4"
        hf_generate(VIDEO_MODEL, raw_b,
                    prompt="The scene continues from this exact moment, same "
                           "characters, same place. " + motion,
                    start_image=tail, aspect_ratio="9:16", duration=10,
                    sound="false")
        lst = work / f"cat_{out.stem}.txt"
        lst.write_text(f"file '{raw.resolve()}'\nfile '{raw_b.resolve()}'\n")
        run([FF, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-an",
             "-vf", "fps=30,tpad=stop_mode=clone:stop_duration=1",
             "-t", f"{need:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", str(out)])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("premise", help="the story premise, in Hindi")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fresh", action="store_true",
                    help="write a new story even when one is already saved in "
                         "the output folder")
    ap.add_argument("--stills", action="store_true",
                    help="skip the paid motion clips and pan over the stills "
                         "instead — the cast is still drawn from the sheet, so "
                         "it stays locked, at roughly a sixth of the credits")
    args = ap.parse_args()

    missing = [str(p) for p in CAST_SHEETS if not p.exists()]
    if missing:
        raise SystemExit("cast sheet(s) missing: " + ", ".join(missing))

    work = Path(args.out or (Path.home() / "Desktop" / "muli-run"))
    work.mkdir(parents=True, exist_ok=True)
    u.ensure_dirs()
    cfg = u.load_config()

    # A finished story is kept and reused. Re-running after a failure part-way
    # used to write a FRESH story, which invalidated every drawing already paid
    # for and billed the whole video again; --fresh asks for a new one on purpose.
    story_file = work / "pkg.json"
    if story_file.exists() and not args.fresh:
        pkg = json.loads(story_file.read_text(encoding="utf-8"))
        print(f"1/5  reusing the story already written ({story_file.name})")
    else:
        print("1/5  writing the story")
        pkg = u.generate_content_package(cfg, args.premise)
        hook = u.generate_hook(cfg, args.premise)
        if hook:
            pkg["Narration"] = f"{hook} {pkg['Narration']}"
            pkg["Scenes"][0]["narration_hi"] = (
                f"{hook} {pkg['Scenes'][0].get('narration_hi', '')}".strip())
        story_file.write_text(json.dumps(pkg, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"     {pkg['Title']}")

    print("2/5  voicing each scene")
    voice = work / "voice.mp3"
    _, total, durs = u.synthesize_scene_voices(cfg, pkg["Scenes"], str(voice))

    have = credits()
    if args.stills:
        # Only the scene drawings are billed; the movement is a ffmpeg pan.
        need = len(pkg["Scenes"]) * CREDITS_PER_STILL
        print(f"     {total:.0f}s across {len(durs)} scenes; stills only "
              f"≈ {need} credits (have {have})")
    else:
        need_clips = sum(2 if d > MAX_CLIP * STRETCH_LIMIT else 1 for d in durs)
        need = (need_clips * CREDITS_PER_CLIP
                + len(pkg["Scenes"]) * CREDITS_PER_STILL)
        print(f"     {total:.0f}s across {len(durs)} scenes; ~{need_clips} clips "
              f"≈ {need} credits (have {have})")
    if have is not None and have < need:
        raise SystemExit(
            f"not enough credits ({have} < {need}) — top up, or run with "
            "--stills, or the video would be abandoned half-paid-for")

    print("3/5  drawing scenes from the cast sheet")
    stills = []
    for i, sc in enumerate(pkg["Scenes"], 1):
        p = work / f"s{i}.jpg"
        # A drawing is only reusable if it was made for THIS beat. Re-running
        # writes a fresh story, so reusing whatever happened to sit on disk
        # pairs new narration with pictures of the old one.
        stamp = work / f"s{i}.prompt.txt"
        want = sc.get("image_prompt", "")
        fresh = (p.exists() and stamp.exists()
                 and stamp.read_text(encoding="utf-8") == want)
        if not fresh:
            draw_scene(sc, p)
            stamp.write_text(want, encoding="utf-8")
        stills.append(p)
        print(f"     s{i}{'' if not fresh else ' (reused)'}")

    final = str(work / "final.mp4")
    if args.stills:
        print("4/5  no motion clips (stills mode)")
        print("5/5  assembling with a slow pan over each still")
        assets = [str(p) for p in stills]
        u.assemble_video(assets, str(voice), total, final,
                         music_dir=u.SHORTS_MUSIC_DIR, music_volume=0.08,
                         width=1080, height=1920, durations=durs)
    else:
        print("4/5  animating")
        clips = []
        for i, (sc, still, need) in enumerate(
                zip(pkg["Scenes"], stills, durs), 1):
            c = work / f"clip{i}.mp4"
            if not c.exists():
                animate_scene(sc, still, need, c, work)
            clips.append(str(c))
            print(f"     clip{i} ({need:.1f}s)")

        print("5/5  assembling")
        assets = clips
        u.assemble_animated_video(clips, str(voice), total, final,
                                  music_dir=u.SHORTS_MUSIC_DIR,
                                  music_volume=0.08, durations=durs)

    report = u.audit_video(final, pkg, assets, durs, "muli")
    print("\n" + report)
    print(f"\n{final}\ncredits left: {credits()}")

    if args.upload:
        url = u.upload_to_youtube(cfg, final, pkg)
        print(url)
        # The daily bot reports to Telegram and this tool did not, so a video
        # made by hand went up with no word of it anywhere.
        u.send_telegram(
            cfg,
            "✅ Muli video uploaded (locked cast)\n\n"
            f"🎬 {pkg['Title']}\n"
            f"🎨 {'stills' if args.stills else 'motion'} · "
            f"{len(durs)} scenes · {total:.0f}s\n"
            f"💳 credits left: {credits()}\n"
            f"🔗 {url}\n\n"
            f"{report}")
    else:
        print("\nnot uploaded — watch it, then re-run with --upload")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:                                  # noqa: BLE001
        # A build can die part-way with credits already spent; say so rather
        # than leaving the failure sitting unread in a terminal.
        import traceback
        traceback.print_exc()
        try:
            u.alert_failure(u.load_config(),
                            f"❌ Muli video build FAILED\nError: {exc}")
        except Exception:                                     # noqa: BLE001
            pass
        raise
