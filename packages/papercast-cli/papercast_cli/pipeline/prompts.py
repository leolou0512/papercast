"""The instructions the pipeline gives the episode session. Adapted from Leo's runner
(prompts.py): the same binding rules, the same explainer.json spec, the same repair and
cut-only turns; no vault, no library notes, no proposals and no live chat (the group version has
none). The craft (how to write a good script) is the base guideline's job: the hub's
`GET /api/cli/prompt`, with this listener's preferences after it, copied into the work
directory as `guideline.md`.

The session has two instructions: first `identify` (write paper.json, so the hub can say
whether the paper is already there before the long run starts), then `episode`.
"""
from __future__ import annotations

from . import explainer as _ex
from . import tags as _tags

# The episode run writes files, not messages. Every prompt of the run ends so.
END = "When every file is written, stop. Write no message."


def rules() -> str:
    """The binding rules (Leo's, 2026-09-27, made general), at the very top of the episode
    instruction; the base guideline opens with the same."""
    return f"""These rules are binding and override everything else, including `guideline.md`:
- Make only two things: the script and the explainer page. Nothing else.
- The script narrates the paper. It never addresses the listener and never talks about itself: not its topic, its plan, its length or what the narrator will do.
- Build it around the paper's key idea: state it once, early, as a plain claim about the method, without announcing that it is the key idea.
- Explainer page: one laptop screen, at most {_ex.PAGE_MAX_WORDS} words and {_ex.PAGE_MAX_FIGURES} figures, and only the paper's key points and figures: you write `explainer.json`, the pipeline builds the page from it. Nothing on it repeats the script, and nothing on it is about the page, the audio or these instructions.
- When unsure whether to add something, leave it out."""


def no_caveats() -> str:
    return ("Do exactly what is asked. No caveats, notes, disclaimers, warnings, asides or "
            "commentary about the task, the paper's status, duplicates, the instructions or "
            "yourself: not in a message, not in the script, not in the explainer. The person who "
            "asked for this episode has already decided what to make.")


def source_text(has_pdf: bool, url: str | None) -> str:
    """How the paper is named in both instructions."""
    if has_pdf:
        return "the paper in this directory, `paper.pdf`" + (f" (from {url})" if url else "")
    return f"the paper at {url}"


def read_text(has_pdf: bool, url: str | None, pages_dir: bool) -> str:
    if has_pdf:
        t = ("It is `paper.pdf`. The Read tool refuses more than 20 pages of a PDF at once, so "
             "read a longer paper in page ranges of at most 20 pages with Read's `pages` "
             "parameter (`\"1-20\"`, then `\"21-40\"`, and so on, up to its last page).")
        if pages_dir:
            t += (" `pages/p-NNN.png` are images of each page (NNN = page number, three digits) "
                  "if you need to look closely at a figure.")
        return t
    return (f"Read it with WebFetch from {url}; only research sites are reachable (arXiv, "
            "Semantic Scholar, OpenReview, doi.org and the major publishers). For an arXiv "
            "paper, `https://arxiv.org/html/<id>` has the full text when arXiv made an HTML "
            "version. Fetch every part of the paper, not only its abstract.")


def identify(has_pdf: bool, url: str | None) -> str:
    """The session's first instruction: paper.json only."""
    return f"""[papercast] You will make one podcast episode about {source_text(has_pdf, url)}. First, identify the paper. You work only in this directory. You cannot run commands; you read and write files.

Read only what you need for this ({"its first page is usually enough" if has_pdf else "its abstract page is usually enough"}) and write `paper.json` in this directory, one JSON object and nothing else:
{{"title": "<the title exactly as printed>", "authors": ["<first author>", "<second author>"], "year": <the year it was published, or null>, "arxiv_id": "<the arXiv id without version, like 2210.02747>" or null, "doi": "<the DOI, like 10.1038/s41586-023-06735-9>" or null, "url": "<the paper's own page, like https://arxiv.org/abs/2210.02747>" or null}}
Take the ids from the paper itself (arXiv's margin stamp, a printed DOI) or the page it came from. If it prints neither, one WebSearch for the exact title may find its arXiv page; write null rather than guess. Write nothing else yet: the episode instructions follow in the next message. {END}"""


def paper_fix(problems: list[str]) -> str:
    return ("[papercast] `paper.json` is not usable yet: " + "; ".join(problems) +
            ". Write it again, one JSON object with title, authors, year, arxiv_id, doi and url "
            "as instructed. " + END)


def explainer_spec(crops: bool) -> str:
    """The explainer.json part of the episode instruction: schema, limits, no-meta rules and one
    good and one bad example (the bad one is what the GEODE episode of 2026-09-27 wrote).
    Without crops (no poppler on this machine, or no PDF) only drawn figures are offered."""
    f = min(_ex.PAGE_MAX_FIGURES, _ex.MAX_FIGURES)
    if crops:
        shape = ('{"points": ["<key point>", ...], "figures": [{"crop": "page=3;box=0.08,0.12,0.92,0.55", '
                 '"caption": "<caption>"}, {"svg": "<svg viewBox=\\"0 0 400 200\\">...</svg>", "caption": "<caption>"}]}')
        kinds = """ Each has `caption` (one or two sentences, at most {cw} words, saying what the figure shows as a fact about the paper) and exactly one of:
    - `crop`: a region of a page of the paper: page number, then the box as fractions of the page width and height (left, top, right, bottom). Look at `pages/` to get the box right.
    - `svg`: an abstract diagram you draw (structure, flow, geometry), one `<svg>` element with a `viewBox`. Shapes, lines, paths and text only: no script, style, images, links or foreignObject (they are removed). Draw with `currentColor` so it reads in light and dark; put `class="hl"` on the one thing to highlight."""
        fig_bad = '{"crop": "page=7;box=0.08,0.10,0.92,0.48", "caption": "Paper crops, each with what to look for. Compare starred rows with starred rows only; check the RMSD column."}'
        fig_good = '{"crop": "page=7;box=0.08,0.10,0.92,0.48", "caption": "Benchmark results; asterisks mark sets relaxed before scoring. Among those, the filtered sets move further under relaxation yet score higher."}'
        bad_why = "The point is a heading about the audio, and the caption talks about the page and tells the reader what to do."
    else:
        shape = ('{"points": ["<key point>", ...], "figures": [{"svg": "<svg viewBox=\\"0 0 400 200\\">...</svg>", '
                 '"caption": "<caption>"}]}')
        kinds = """ Each has `caption` (one or two sentences, at most {cw} words, saying what the figure shows as a fact about the paper) and `svg`: an abstract diagram you draw (structure, flow, geometry), one `<svg>` element with a `viewBox`. Shapes, lines, paths and text only: no script, style, images, links or foreignObject (they are removed). Draw with `currentColor` so it reads in light and dark; put `class="hl"` on the one thing to highlight. The paper's own figures cannot be used here."""
        fig_bad = '{"svg": "<svg viewBox=\\"0 0 400 200\\">...</svg>", "caption": "A diagram to look at while listening; compare the two paths."}'
        fig_good = '{"svg": "<svg viewBox=\\"0 0 400 200\\">...</svg>", "caption": "Denoising moves each atom toward the nearest reachable periodic image, not the nearest image overall."}'
        bad_why = "The point is a heading about the audio, and the caption talks about listening and tells the reader what to do."
    kinds = kinds.format(cw=_ex.CAPTION_WORDS)
    return f"""- `explainer.json`: the content of the explainer page, as one JSON object and nothing else. The pipeline builds the page from it: the paper's title as the heading, your points as a list, then each figure with its caption. There is no other text on the page, so write no heading, intro, summary or closing line anywhere.
  {shape}
  - `points`: 1 to {_ex.MAX_POINTS} strings, the paper's key points, one sentence each, at most {_ex.POINT_WORDS} words each.
  - `figures`: 0 to {f} items. A figure only for a structure, a flow, a geometry or a plot that is clearer as a picture; no figure is fine.{kinds}
  - Every point and caption is a plain statement about the paper. Never: mention the audio, podcast, episode, listener, this page, the explainer, the figures or crops as a set, or these instructions ("can't show", "cannot be said", "words alone"); address the reader or tell them what to do ("you", "what to look for", or starting with Look, Check, Compare, Note, Notice, See, Consider, Focus). Numbers are fine.
  - Bad (rejected): {{"points": ["GEODE: three things the audio can't show"], "figures": [{fig_bad}]}}. {bad_why}
  - Good: {{"points": ["Denoising toward the nearest periodic image can aim at an arrangement the process cannot reach, so GEODE keeps only the reachable images."], "figures": [{fig_good}]}}"""


def _tags_in_use(vocab: list[str] | None) -> str:
    if not vocab:
        return "No tags are in use yet."
    return (f"Tags already in use: {_tags.in_use_text(vocab)}. Reuse one of these, spelled "
            "exactly the same, whenever it fits; add a new tag only when none of them fits.")


def episode(has_pdf: bool, url: str | None, pages_dir: bool, crops: bool, words_lo: int,
            words_hi: int, min_lo: float, min_hi: float, tags_in_use: list[str] | None = None) -> str:
    """The episode instruction. Self-contained: it also starts a new session when the old one
    cannot be resumed."""
    ex = ", ".join(f'"{t}"' for t in _tags.EXAMPLES[:4])
    return f"""{rules()}

{no_caveats()}

You are making one podcast episode about {source_text(has_pdf, url)}. The script narrates the paper: it never addresses the listener and never talks about itself.
You work only in this directory. You cannot run commands; you read and write files.

Before anything else, read `guideline.md` in this directory: the rules for the episode, then this listener's preferences. The preferences decide what gets more time; the rules decide how the episode sounds and what never goes in.

Then read the paper itself, all of it. {read_text(has_pdf, url, pages_dir)}

Write these files in this directory. The first two are the episode; the third is internal bookkeeping for the pipeline, required but never shown:

- `script.md`: the spoken episode, one narrator, {words_lo} to {words_hi} words ({min_lo:g} to {min_hi:g} minutes read aloud). Only paragraphs separated by blank lines, and `#` heading lines. No lists, emphasis, links or tables. No digits anywhere: write every number, unit and symbol as words. No LaTeX, no maths symbols, no Greek letters: say the words. Follow the guideline for everything else.
{explainer_spec(crops)}
- `claims.md` (internal): front matter with the paper's `title` exactly as printed on the paper, `authors` (a YAML list), `year` (the year the paper was published, from the paper or its arXiv stamp; `unknown` if it gives none) and `tags` (two or three short lowercase topic tags, one to four words each, naming its field and topic, e.g. {ex}), then two or three lines starting `- `, each one main claim of the paper in one sentence. {_tags_in_use(tags_in_use)}
  ---
  title: <paper title>
  authors: [<first author>, <second author>]
  year: <year>
  tags: [<tag>, <tag>]
  ---
  - <claim>

The pipeline checks every file. Anything wrong (a missing file, digits in the script, a script sentence that names the listener, narrates what the listener knows, talks about the episode or states the paper's publication status, stock AI phrasing, a point or caption that breaks the rules above) is sent back to you once, naming each item and why, and fails the episode if it is still wrong.

{END}"""


def repair(problems: list[str], missing: list[str], over: list[str] | None = None,
           explainer: list[str] | None = None) -> str:
    """The one repair turn: names exactly what is missing, wrong in script.md or
    explainer.json, or over a limit, and why."""
    lines = []
    if missing:
        lines.append("These files are missing: " + ", ".join(f"`{m}`" for m in missing) + ".")
    for p in problems:
        lines.append(f"`script.md`: {p}.")
    for p in explainer or []:
        lines.append(f"`explainer.json`, {p}.")
    lines += list(over or [])
    return ("[papercast] The episode is not finished yet. " + " ".join(lines) +
            " Fix this now, in this directory, following the episode instructions you were given "
            "earlier in this session (words only in script.md: no digits, symbols, LaTeX or Markdown other than "
            "paragraphs and # headings, about the paper and never to the listener; in "
            "explainer.json only plain statements about the paper). " + END)


def cut(words_lo: int, words_hi: int) -> str:
    """The cut-only turn after every check passed: deletions only, enforced by cut.py; the
    length check stays the only other guard. Leo's wording."""
    return ("[papercast] One more pass, cuts only. Reread `script.md`. Only three kinds of "
            "sentence may go: a caveat or aside, a comparison with or link to other work or "
            "something already known, and a sentence that only restates its neighbour. Delete one "
            "of these unless leaving it out would make the listener misread the method or the "
            "result. Never delete a sentence that says what the method does, why it works, or what "
            "the paper found: results and findings always stay. Delete whole "
            "sentences only: change no word, add nothing, move nothing. The script must stay "
            f"within {words_lo} to {words_hi} words. Then apply the same test to `explainer.json`: "
            "delete whole points or figures only, and change nothing else. Touch no other file. "
            "If nothing fails the test, change nothing. Any change other than a deletion is "
            "undone. " + END)


def retry(failed: dict | None) -> str:
    why = ""
    if failed:
        why = f" The last attempt failed: {failed.get('code')}: {failed.get('message')}."
        if failed.get("detail"):
            why += f" ({failed['detail']})"
    return ("[papercast] This is a retry of the episode." + why +
            " Look at the files already in this directory and finish every output exactly as "
            "instructed earlier in this session. " + END)


def interrupted() -> str:
    return ("[papercast] Your previous run was interrupted (the machine slept or restarted, or "
            "the pipeline stopped). Look at the files already in this directory and finish every "
            "output exactly as instructed earlier in this session. " + END)
