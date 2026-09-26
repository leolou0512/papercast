#!/usr/bin/env python3
"""Build Leo's choice page from ~/lab/review/_template.html and the measured clips.

    build_page.py --dir <out dir> --text-file paragraph.txt \
                  --template ~/lab/review/_template.html \
                  --page ~/lab/review/2026-09-26_papercast-voice.html

Replaces only the title, the meta line and the QUESTIONS section of the template (the
<script> block is untouched, per ~/lab/review/README.md). Clips are embedded as
base64 MP3; every number on the page is read from the metrics files in --dir.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
from pathlib import Path


def load(d: Path, name: str) -> dict:
    return json.loads((d / name).read_text())


def episode_minutes(rtf: float) -> int:
    return round(rtf * 20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--text-file", type=Path, required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--page", type=Path, required=True)
    ap.add_argument("--date", default="2026-09-26")
    a = ap.parse_args()
    d = a.dir
    clips = load(d, "clips.json")

    br, brm = load(d, "breeze.json"), load(d, "breeze.gpumem.json")
    vx, vxm = load(d, "voxtral.json"), load(d, "voxtral.gpumem.json")
    ko = load(d, "kokoro.json")
    gib = lambda mib: f"{mib / 1024:.1f}"

    # Breeze: the clip and the numbers come from the same run. Default is the run with two
    # CUDA-graph stages (backbone decode, depth decoder); the all-eager run is the fallback
    # and its slower-but-smaller figures are quoted alongside.
    br_clip, br_eager_min = "breeze", episode_minutes(br["runs"][-1]["rtf"])
    br_min, br_peak = br_eager_min, brm["peak_process_tree_mib"]
    extra = ""
    fast = d / "breeze_fast.json"
    if fast.exists() and (d / "breeze_fast.gpumem.json").exists():
        bf, bfm = load(d, "breeze_fast.json"), load(d, "breeze_fast.gpumem.json")
        if bfm.get("returncode") == 0:
            br_clip = "breeze_fast"
            br_min, br_peak = episode_minutes(bf["runs"][-1]["rtf"]), bfm["peak_process_tree_mib"]
            extra = (f" A slower mode fits in {gib(brm['peak_process_tree_mib'])} GB but takes "
                     f"about {br_eager_min} minutes.")
    br_line = (f"Breeze TTS 2 (3.5 billion parameters), on the GPU. It has no preset voices, so "
               f"this narrator is described in words. A 20-minute episode takes about "
               f"{br_min} minutes; peak GPU memory {gib(br_peak)} of the card's 16 GB.{extra}")
    vx_line = (f"Voxtral TTS (Mistral, 4 billion parameters), on the GPU, preset voice "
               f"&ldquo;neutral female&rdquo;. A 20-minute episode takes about "
               f"{episode_minutes(vx['runs'][-1]['rtf'])} minutes; peak GPU memory "
               f"{gib(vxm['peak_process_tree_mib'])} of the card's 16 GB.")
    ko_line = (f"Kokoro (82 million parameters), on the processors, no GPU, preset voice "
               f"&ldquo;heart&rdquo;. A 20-minute episode takes about "
               f"{episode_minutes(ko['runs'][-1]['rtf'])} minutes on {ko['torch_threads']} of "
               f"stibnite's 64 threads. This is the <b>Use CPU voice</b> fallback whichever you pick.")

    def audio(name: str) -> str:
        b64 = base64.b64encode((d / f"{name}.mp3").read_bytes()).decode()
        return (f'<audio controls preload="auto" style="width:100%" '
                f'src="data:audio/mpeg;base64,{b64}"></audio>')

    text = html.escape(a.text_file.read_text().strip())
    cards = [("A", br_clip, br_line), ("B", "voxtral", vx_line), ("C", "kokoro", ko_line)]
    body = [f"""
  <section class="q">
    <h2>The paragraph</h2>
    <p class="why">Same text in all three, from MatterGen (Zeni et&nbsp;al., <i>Nature</i> 2025).
    All three clips are matched in loudness, so turn the volume up once and leave it.</p>
    <details><summary>Show the text</summary><p>{text}</p></details>
  </section>
"""]
    for letter, name, line in cards:
        body.append(f"""
  <section class="q">
    <h2>Voice {letter}</h2>
    {audio(name)}
    <p class="why" style="margin-top:.6rem">{line}</p>
  </section>
""")
    body.append("""
  <section class="q">
    <h2>1. Which voice narrates the episodes?</h2>
    <label class="opt"><input type="radio" name="voice" value="breeze"> A &mdash; Breeze TTS 2</label>
    <label class="opt"><input type="radio" name="voice" value="voxtral"> B &mdash; Voxtral TTS</label>
    <label class="opt"><input type="radio" name="voice" value="kokoro"> C &mdash; Kokoro for everything (never waits for the GPU)</label>
    <label class="opt"><input type="radio" name="voice" value="none"> None of these &mdash; say why below</label>
  </section>

  <section class="q">
    <h2>2. Notes</h2>
    <textarea name="notes" placeholder="Anything about the voice: pace, accent, a male narrator instead, ..."></textarea>
  </section>
""")
    t = a.template.read_text()
    t = t.replace("<title>TITLE — GEODE review</title>", "<title>Papercast narrator voice</title>")
    t = t.replace("<h1>TITLE</h1>", "<h1>Papercast narrator voice</h1>")
    t = t.replace("Written by Claude on DATE. Pick your answers, then hit <b>Save choices</b> at the bottom.",
                  f"Written by Claude on {a.date}. Listen to the three clips, pick one, then hit "
                  f"<b>Save choices</b> at the bottom.")
    t, n = re.subn(r"(<!-- =+ QUESTIONS START.*?-->)(.*?)(\s*<!-- =+ QUESTIONS END =+ -->)",
                   lambda m: m.group(1) + "".join(body) + m.group(3), t, flags=re.S)
    assert n == 1, "template QUESTIONS markers not found"
    assert "TITLE" not in t and "DATE." not in t
    a.page.write_text(t)
    print(a.page, a.page.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
