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
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.chdir(BASE)

import imageio_ffmpeg                       # noqa: E402
import ultimate_shorts_agent as u           # noqa: E402

HF = r"C:\Users\91872\AppData\Local\higgsfield\bin\higgsfield.exe"
CAST_SHEET = Path(r"C:\Users\91872\Desktop\muli-cast\cast_sheet.jpg")
FF = imageio_ffmpeg.get_ffmpeg_exe()

# kling2_6 is the cheapest motion that still holds the cast: 10 credits per 10s,
# where kling3_0_turbo wants 15 and veo returns "nsfw" on clothed cartoons.
VIDEO_MODEL = "kling2_6"
MAX_CLIP = 10.0            # the model's ceiling; lines regularly run longer
STRETCH_LIMIT = 1.45       # beyond this, slow motion stops reading as a choice
CREDITS_PER_CLIP = 10


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
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    blob = (r.stdout or "") + (r.stderr or "")
    m = re.search(r'"result_url":\s*"(https[^"]+)"', blob)
    if not m:
        why = "nsfw" if '"status": "nsfw"' in blob else blob[-200:].strip()
        raise RuntimeError(f"{model} failed: {why}")
    Path(out).write_bytes(requests.get(m.group(1), timeout=300).content)
    return out


def draw_scene(scene, out):
    """Draw one scene FROM THE CAST SHEET — the only thing that holds the cast."""
    return hf_generate(
        "nano_banana_pro", out,
        prompt="Use the EXACT characters from the reference cast sheet, same "
               "designs, same clothes. " + scene["image_prompt"],
        image_references=CAST_SHEET, aspect_ratio="9:16")


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
    args = ap.parse_args()

    if not CAST_SHEET.exists():
        raise SystemExit(f"cast sheet missing: {CAST_SHEET}")

    work = Path(args.out or (Path.home() / "Desktop" / "muli-run"))
    work.mkdir(parents=True, exist_ok=True)
    u.ensure_dirs()
    cfg = u.load_config()

    print("1/5  writing the story")
    pkg = u.generate_content_package(cfg, args.premise)
    hook = u.generate_hook(cfg, args.premise)
    if hook:
        pkg["Narration"] = f"{hook} {pkg['Narration']}"
        pkg["Scenes"][0]["narration_hi"] = (
            f"{hook} {pkg['Scenes'][0].get('narration_hi', '')}".strip())
    (work / "pkg.json").write_text(json.dumps(pkg, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
    print(f"     {pkg['Title']}")

    print("2/5  voicing each scene")
    voice = work / "voice.mp3"
    _, total, durs = u.synthesize_scene_voices(cfg, pkg["Scenes"], str(voice))

    need_clips = sum(2 if d > MAX_CLIP * STRETCH_LIMIT else 1 for d in durs)
    have = credits()
    print(f"     {total:.0f}s across {len(durs)} scenes; ~{need_clips} clips "
          f"≈ {need_clips * CREDITS_PER_CLIP} credits (have {have})")
    if have is not None and have < need_clips * CREDITS_PER_CLIP:
        raise SystemExit("not enough credits — top up, or the video would be "
                         "abandoned half-paid-for")

    print("3/5  drawing scenes from the cast sheet")
    stills = []
    for i, sc in enumerate(pkg["Scenes"], 1):
        p = work / f"s{i}.jpg"
        if not p.exists():
            draw_scene(sc, p)
        stills.append(p)
        print(f"     s{i}")

    print("4/5  animating")
    clips = []
    for i, (sc, still, need) in enumerate(zip(pkg["Scenes"], stills, durs), 1):
        c = work / f"clip{i}.mp4"
        if not c.exists():
            animate_scene(sc, still, need, c, work)
        clips.append(str(c))
        print(f"     clip{i} ({need:.1f}s)")

    print("5/5  assembling")
    final = str(work / "final.mp4")
    u.assemble_animated_video(clips, str(voice), total, final,
                              music_dir=u.SHORTS_MUSIC_DIR, music_volume=0.08,
                              durations=durs)

    report = u.audit_video(final, pkg, clips, durs, "muli")
    print("\n" + report)
    print(f"\n{final}\ncredits left: {credits()}")

    if args.upload:
        print(u.upload_to_youtube(cfg, final, pkg))
    else:
        print("\nnot uploaded — watch it, then re-run with --upload")


if __name__ == "__main__":
    main()
