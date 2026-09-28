#!/usr/bin/env python3
"""A stand-in for the `claude` binary, for tests/e2e_test.py (SPEC.md section 11: tests use a fake
`claude` on PATH, never the real one). Never calls anything.

The CLI's pipeline (A8) runs `claude -p ...` in a job directory and expects files there. This
fake guesses what each call wants from the prompt, the way a well-behaved agent would:
  - `--model` naming haiku (the link grading, no tools): answers "[]" (no links);
  - a prompt that names paper.json: writes paper.json (title, authors, year, arxiv_id, doi);
  - a prompt that names script.md / explainer.json / claims.md: writes each one named that is
    not there yet (a script that passes common/checks.py for 15-25 minutes at 150 words a
    minute, an explainer with points and no figures, a claims list);
  - anything else (the cut pass, a repair turn): leaves the files as they are.
It prints what `claude -p` prints for --output-format text, json or stream-json.

If A8 ships its own fake claude (packages/papercast-cli/tests/fake_claude*), the e2e test uses
that one instead: it knows the pipeline's exact protocol.
"""
from __future__ import annotations

import json
import os
import re
import sys

TITLE = "Straight Paths for Generative Models, a Test Paper"

_SENTENCES = [
    "The method trains a network to move noise toward data along straight paths.",
    "Each training example pairs a point of noise with a point of data and asks for the velocity between them.",
    "Sampling then follows that velocity field from the noise to the data in a handful of steps.",
    "A straight path is cheap to follow, because a coarse step lands close to where a fine step would.",
    "The older approach simulated a slow diffusion process and needed hundreds of small steps.",
    "Here the loss is a plain squared error between the predicted and the true velocity.",
    "Nothing in the objective asks the network to estimate a density or a score.",
    "The paths cross each other when noise and data are paired at random.",
    "Crossing paths blur the velocity field, and the samples come out soft.",
    "Pairing each noise point with a nearby data point keeps the paths apart.",
    "With the paths apart, the field is smoother and a few steps are enough.",
    "The authors measure image quality on standard benchmarks and match strong diffusion baselines.",
    "They also count the network evaluations each sample needs, and the count falls sharply.",
    "The price is an extra pairing step during training, which costs a little time per batch.",
    "A second idea is to straighten the paths again by training on the model's own samples.",
    "Each round of straightening makes the next sampler faster, with a small loss in variety.",
    "The theory says the learned field transports the noise distribution onto the data distribution.",
    "In words, the flow carries the whole cloud of noise onto the whole cloud of data.",
    "The proof rests on the continuity equation, which keeps track of where probability mass goes.",
    "A reader can picture it as water flowing through pipes, never created and never lost.",
    "The experiments cover images, audio and small molecules, with the same recipe each time.",
    "For molecules the straight path runs through the positions of the atoms.",
    "The limits are clear: very sharp data, such as text, does not suit a continuous path.",
    "Guidance from a label works as it does for diffusion, by mixing two velocity fields.",
    "The strength of the mix trades variety for fidelity, and a middle setting works best.",
    "The key idea is that a straight path is the simplest thing a network can learn to follow.",
]
_HEADINGS = ["# The idea", "# Why straight paths", "# Pairing noise with data", "# What the theory says",
             "# The experiments", "# Where it stops working", "# The idea again, with its cost"]


def script_text(words_min: int = 2400) -> str:
    """A script that passes common/checks.check at 150 words a minute for 15-25 minutes."""
    paras, n, i = [], 0, 0
    while n < words_min:
        head = _HEADINGS[(len(paras) // 3) % len(_HEADINGS)] if len(paras) % 3 == 0 else None
        body = " ".join(_SENTENCES[(i + k) % len(_SENTENCES)] for k in range(5))
        i += 5
        if head:
            paras.append(head)
            n += len(head.split()) - 1
        paras.append(body)
        n += len(body.split())
    return "\n\n".join(paras) + "\n"


def explainer() -> dict:
    return {"title": TITLE,
            "points": ["A network learns the velocity that moves noise onto data along straight paths.",
                       "Pairing each noise point with a nearby data point keeps the paths from crossing.",
                       "Straight paths need few sampling steps."],
            "figures": []}


def claims() -> str:
    return ("- The learned velocity field transports noise onto data (the continuity equation).\n"
            "- Pairing noise with nearby data reduces the number of sampling steps.\n")


def paper() -> dict:
    return {"title": TITLE, "authors": ["Ada Lovelace", "Alan Turing"], "year": 2022,
            "arxiv_id": None, "doi": None}


def _prompt(argv: list[str]) -> str:
    for flag in ("-p", "--print"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
    rest = [a for a in argv if not a.startswith("-")]
    text = " ".join(rest)
    if not sys.stdin.isatty():
        try:
            text += sys.stdin.read()
        except OSError:
            pass
    return text


def _write(name: str, content) -> None:
    if os.path.exists(name):
        return
    with open(name + ".tmp", "w", encoding="utf-8") as fh:
        if isinstance(content, str):
            fh.write(content)
        else:
            json.dump(content, fh, indent=1)
    os.replace(name + ".tmp", name)


def main(argv: list[str]) -> int:
    if argv[:1] in (["--version"], ["-v"]):
        print("2.1.999 (Claude Code, fake for tests)")
        return 0
    if argv[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "fake", "subscriptionType": "max"}))
        return 0
    model = argv[argv.index("--model") + 1] if "--model" in argv and argv.index("--model") + 1 < len(argv) else ""
    fmt = argv[argv.index("--output-format") + 1] if "--output-format" in argv else "text"
    prompt = _prompt(argv)
    result = "done"
    if "haiku" in model:
        result = "[]"
    else:
        if re.search(r"paper\.json", prompt):
            _write("paper.json", paper())
        if re.search(r"script\.md", prompt):
            _write("script.md", script_text())
        if re.search(r"explainer\.json", prompt):
            _write("explainer.json", explainer())
        if re.search(r"claims\.md", prompt):
            _write("claims.md", claims())
    final = {"type": "result", "subtype": "success", "is_error": False, "result": result,
             "session_id": "00000000-0000-4000-8000-000000000000", "num_turns": 1,
             "duration_ms": 10, "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}}
    if fmt == "json":
        print(json.dumps(final))
    elif fmt == "stream-json":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": final["session_id"],
                          "model": model or "claude-opus-5-5", "tools": []}))
        print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": result}]}, "session_id": final["session_id"]}))
        print(json.dumps(final))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
