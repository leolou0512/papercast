#!/usr/bin/env python3
"""Check every theme's colours for WCAG AA contrast, from the stylesheets themselves.

    python3 stacks/papercast-group/tools/theme_contrast.py [--static DIR] [--all]

Reads the tokens of Light and Dark (app.css, tour.css, map.css, social.css, auth.css: the
:root and :root[data-theme="dark"] blocks) and of each theme in themes.css, resolves var(), and
checks the pairs the pages draw: text, secondary text and the accent on every background they
sit on (4.5:1), the messages' green, amber and red on the page's backgrounds (4.5:1), the text on
the accent and on the red of the swipe's Delete, the toast, the board's paper titles, the
search and transcript highlights, the map's faint numbers (4.5:1); field borders and the map's
papers against the page (3:1). Also that each theme in themes.css sets every colour token Light
sets (a token added to a page's stylesheet later needs a value in each theme). Prints one line per
failure (all pairs with --all) and exits 1 when a theme in themes.css fails either check. Light
and Dark are Leo's own and only reported.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"
TEXT, UI = 4.5, 3.0

# (foreground, [backgrounds], minimum)
BGS = ["bg", "side", "surface", "hover", "sel"]
PAIRS = [
    ("text", BGS + ["tour-card", "now", "mark", "hl@bg"], TEXT),
    ("text-2", BGS + ["tour-card", "mark"], TEXT),        # a search match in a row's second line
    ("accent", BGS, TEXT),
    ("on-accent", ["accent"], TEXT),
    ("alive", ["bg", "side", "surface"], TEXT),
    ("warn", ["bg", "side", "surface"], TEXT),
    ("danger", ["bg", "side", "surface"], TEXT),
    ("on-danger", ["danger"], TEXT),
    ("toast-fg", ["toast-bg"], TEXT),
    ("toast-link", ["toast-bg"], TEXT),
    ("bd-title", ["side", "sel"], TEXT),
    ("pm-faint", ["bg", "surface"], TEXT),
    ("input-line", ["bg", "side"], UI),
    ("pm-node", ["bg"], UI),
    ("alive", ["bg"], UI),                   # a listened paper on the map
]


def lum(rgb) -> float:
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b) -> float:
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def parse_color(v: str):
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4) or 1))
    return None


def over(fg, bg):
    """fg (maybe translucent) composited over an opaque bg."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def blocks(css: str):
    """(selector, {token: value}) for every innermost rule, @media wrappers dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        sel = " ".join(m.group(1).split("{")[-1].split())
        decls = dict((k, v.strip()) for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+)", m.group(2)))
        if decls:
            yield sel, decls


def themes(static: Path, own: dict | None = None) -> dict:
    """{theme: {token: value}}: Light and Dark from the pages' own stylesheets, the rest from
    themes.css (over Light's). `own`, when given, gets {theme: the tokens themes.css sets}."""
    # a token a rule reads with a fallback, var(--on-danger, #fff), where Light and Dark leave it out
    out = {"light": {"--on-danger": "#FFFFFF"}, "dark": {}}
    for name in ("auth.css", "app.css", "social.css", "tour.css", "map.css"):
        p = static / name
        if not p.exists():
            continue
        for sel, d in blocks(p.read_text()):
            if sel in (":root", ".pmap"):
                for k, v in d.items():
                    out["light"].setdefault(k, v) if name == "auth.css" else out["light"].__setitem__(k, v)
            elif sel in (':root[data-theme="dark"]', ':root[data-theme="dark"] .pmap'):
                out["dark"].update(d)
    # Dark keeps Light's tokens it does not redefine (the map's --pm-* that follow the page)
    out["dark"] = {**out["light"], **out["dark"]}
    p = static / "themes.css"
    if p.exists():
        for sel, d in blocks(p.read_text()):
            m = re.fullmatch(r':root\[data-theme="([\w-]+)"\]( \.pmap)?', sel)
            if m:
                out.setdefault(m.group(1), dict(out["light"])).update(d)
                if own is not None:
                    own.setdefault(m.group(1), set()).update(d)
    return out


def needed(light: dict) -> set:
    """The colour tokens a theme must set: Light's, but for the fonts and the map's --pm-* that
    follow a page token (var(--bg, ...))."""
    return {k for k, v in light.items() if k not in ("--sans", "--mono") and not (k.startswith("--pm-") and v.strip().startswith("var("))}


def resolve(tokens: dict, name: str, depth: int = 0):
    v = tokens.get("--" + name)
    if v is None or depth > 10:
        return None
    for _ in range(10):
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)", v.strip())
        if not m:
            break
        got = tokens.get(m.group(1))
        v = got if got is not None else (m.group(2) or "")
    return parse_color(v)


def check(tokens: dict):
    """[(fg, bg, ratio, minimum)] for every pair this theme has both colours of."""
    rows = []
    for fg, bgs, need in PAIRS:
        f = resolve(tokens, fg)
        if f is None:
            continue
        for bgn in bgs:
            if "@" in bgn:                      # a translucent colour over a background
                top, base = bgn.split("@")
                t, b = resolve(tokens, top), resolve(tokens, base)
                b = over(t, b) if t and b else None
            else:
                b = resolve(tokens, bgn)
            if b is None:
                continue
            if b[3] < 1:
                b = over(b, resolve(tokens, "bg"))
            rows.append((fg, bgn, ratio(over(f, b) if f[3] < 1 else f, b), need))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--static", default=str(STATIC), help="the hub's static dir")
    ap.add_argument("--all", action="store_true", help="print every pair, not only the failures")
    a = ap.parse_args(argv)
    fails = 0
    sets: dict = {}
    got = themes(Path(a.static), sets)
    must = needed(got["light"])
    for theme, tokens in got.items():
        own = theme not in ("light", "dark")
        rows = check(tokens)
        bad = [r for r in rows if r[2] < r[3]]
        print(f"{theme}: {len(rows)} pairs, {len(bad)} under AA" + ("" if own else " (Leo's own: reported only)"))
        for fg, bg, r, need in (rows if a.all else bad):
            print(f"  {'FAIL' if r < need else 'ok  '} {fg:11s} on {bg:9s} {r:5.2f} (needs {need})")
        if own:
            missing = sorted(must - sets.get(theme, set()))
            if missing:
                print(f"  MISSING {' '.join(missing)} (Light sets them; this theme would show Light's)")
            fails += len(bad) + len(missing)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
