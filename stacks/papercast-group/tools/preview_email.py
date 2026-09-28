#!/usr/bin/env python3
"""Preview the welcome email (hub/email/welcome.html and welcome.txt) before it goes to anyone.

    python3 stacks/papercast-group/tools/preview_email.py [--out DIR] [--name Maria] [--username mg1234]
        [--signin-url URL] [--to ADDR] [--from ADDR] [--by-url] [--shots PREFIX]

Fills the placeholders with sample values (as the sender does: plain replacement, the values
HTML-escaped in the HTML) and writes

  DIR/welcome-preview.html  the email as a page. The banner is inlined, so it shows before the site
                            serves it; --by-url keeps its public URL instead.
  DIR/welcome-preview.eml   multipart/alternative, the plain text then the HTML, the banner by its
                            public URL: open it in a mail client, or send it for a test.

and prints the checks mail clients care about: the HTML's size (Gmail clips a message whose HTML
passes 102 KB), no script, form or external CSS, no placeholder left, every image with alt text,
the banner's bytes and pixels. Exits 1 when a check fails.

With --shots PREFIX it also renders the email in headless Chrome (hub/tests/web_cdp.py) as
PREFIX-<variant>.png: 600 (a 680 px window, the 600 px email at full width) and 375 px, light and
dark (prefers-color-scheme), images off, and a rough Outlook desktop: the style blocks stripped,
Outlook's conditional comments taken, its VML button drawn as a box, then the same with Outlook's
two dark modes imitated (light backgrounds and dark text flipped; every colour flipped).
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import html
import re
import sys
import tempfile
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

GROUP = Path(__file__).resolve().parent.parent              # stacks/papercast-group
EMAIL_DIR = GROUP / "hub" / "email"
STATIC_EMAIL = GROUP / "hub" / "static" / "email"
PUBLIC_EMAIL = "https://papercast.virtualatoms.org/email/"
SUBJECT = "Welcome to Virtual Atoms Lab Papercast"
KEYS = ("name", "username", "signin_url", "site_url", "github_url")
GMAIL_CLIP = 102 * 1024


def fill(template: str, values: dict, escape: bool) -> str:
    """Each {key} replaced by its value (HTML-escaped when `escape`); other braces left alone."""
    def one(m):
        v = str(values[m.group(1)])
        return html.escape(v, quote=True) if escape else v
    return re.sub(r"\{(" + "|".join(KEYS) + r")\}", one, template)


def render(values: dict) -> tuple[str, str]:
    h = (EMAIL_DIR / "welcome.html").read_text(encoding="utf-8")
    t = (EMAIL_DIR / "welcome.txt").read_text(encoding="utf-8")
    return fill(h, values, True), fill(t, values, False)


def banner_files(page: str) -> list[Path]:
    return [STATIC_EMAIL / f for f in dict.fromkeys(re.findall(re.escape(PUBLIC_EMAIL) + r"([\w.-]+)", page))]


def inline_images(page: str) -> str:
    def one(m):
        f = STATIC_EMAIL / m.group(1)
        mime = "image/jpeg" if f.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return f"data:{mime};base64," + base64.b64encode(f.read_bytes()).decode()
    return re.sub(re.escape(PUBLIC_EMAIL) + r"([\w.-]+)", one, page)


def local_images(page: str) -> str:
    return re.sub(re.escape(PUBLIC_EMAIL) + r"([\w.-]+)", lambda m: (STATIC_EMAIL / m.group(1)).as_uri(), page)


def checks(page: str, text: str) -> list[tuple[bool, str]]:
    out = []
    n = len(page.encode("utf-8"))
    out.append((n < GMAIL_CLIP, f"HTML {n / 1024:.1f} KB (Gmail clips above 102 KB)"))
    low = page.lower()
    bad = [w for w in ("<script", "<form", "<input", "<link", "@import", "<iframe", "<video", "javascript:") if w in low]
    out.append((not bad, "no script, form, link, import or iframe" + (f": found {bad}" if bad else "")))
    body = re.sub(r"<style.*?</style>", "", page, flags=re.S | re.I)
    left = sorted(set(re.findall(r"\{[a-z_]+\}", body) + re.findall(r"\{[a-z_]+\}", text)))
    out.append((not left, "no placeholder left" + (f": {left}" if left else "")))
    imgs = re.findall(r"<img\b[^>]*>", page, flags=re.I)
    noalt = [i for i in imgs if not re.search(r'\balt="[^"]+"', i)]
    out.append((not noalt, f"{len(imgs)} image(s), each with alt text" if not noalt else f"images without alt: {noalt}"))
    out.append(("background-image" not in low and "background:url" not in low.replace(" ", ""),
                "no CSS background images"))
    for f in banner_files(page):
        if not f.exists():
            out.append((False, f"{f.name} missing from {STATIC_EMAIL}"))
            continue
        kb = f.stat().st_size / 1024
        size = ""
        try:
            from PIL import Image
            with Image.open(f) as im:
                size = f", {im.size[0]}x{im.size[1]} px"
        except Exception:
            pass
        out.append((kb < 150, f"{f.name} {kb:.0f} KB{size} (aim: under 150 KB)"))
    return out


def eml(page: str, text: str, to: str, sender: str) -> bytes:
    m = EmailMessage()
    m["Subject"] = SUBJECT
    m["From"] = sender
    m["To"] = to
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="papercast.virtualatoms.org")
    m.set_content(text)
    m.add_alternative(page, subtype="html")
    return m.as_bytes()


# ---------- the screenshots ----------

def _flip(hexcol: str) -> str:
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, 1 - l, s)
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _light(hexcol: str) -> bool:
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)[1] > 0.5


def _hex6(c: str) -> str:
    c = c.upper()
    return "#" + "".join(ch * 2 for ch in c[1:]) if len(c) == 4 else c


def outlook(page: str, dark: str | None = None) -> str:
    """Roughly what Outlook's Word engine keeps: no style blocks, its conditional comments taken,
    the other branch dropped, no max-width or rounded corners, the VML button as a plain box.
    dark="partial": light backgrounds made dark and dark text made light (Outlook's usual dark
    mode); dark="full": every colour's lightness flipped. Images are never changed."""
    p = re.sub(r"<style.*?</style>", "", page, flags=re.S | re.I)
    p = re.sub(r"<!--\[if !mso\]><!-- -->.*?<!--<!\[endif\]-->", "", p, flags=re.S)
    p = re.sub(r"<!--\[if mso\]>(.*?)<!\[endif\]-->", r"\1", p, flags=re.S)

    def rr(m):
        a = m.group(1)
        st = re.search(r'style="([^"]*)"', a).group(1)
        w = re.search(r"width:\s*(\d+)px", st).group(1)
        hh = re.search(r"height:\s*(\d+)px", st).group(1)
        fill_ = re.search(r'fillcolor="([^"]+)"', a).group(1)
        return (f'<table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr><td bgcolor="{fill_}" '
                f'style="background-color:{fill_}; width:{w}px; height:{hh}px; border-radius:6px; text-align:center; vertical-align:middle;">')
    p = re.sub(r"<v:roundrect\b([^>]*)>", rr, p)
    p = p.replace("</v:roundrect>", "</td></tr></table>").replace("<w:anchorlock/>", "")
    p = re.sub(r"<center\b", "<div", p).replace("</center>", "</div>")
    p = re.sub(r"max-width:\s*\d+px;?", "", p)
    p = re.sub(r"border-radius:\s*[^;\"]+;?", "", p)
    if dark:
        def col(m):
            prop, c = m.group(1), _hex6(m.group(2))
            is_bg = prop.lower() in ("background-color", "bgcolor", "fillcolor")
            if dark == "full" or (is_bg and _light(c)) or (not is_bg and not _light(c)):
                c = _flip(c)
            return m.group(0).replace(m.group(2), c)
        p = re.sub(r"(background-color|(?<![-\w])color)\s*:\s*(#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3})\b", col, p)
        p = re.sub(r'(bgcolor|fillcolor)="(#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3})"', col, p)
        # the client's own background around the message, as a dark theme draws it
        p = p.replace("<body ", '<body bgcolor="#1F1F1F" ', 1)
    return p


def shots(page: str, prefix: str) -> list[Path]:
    sys.path.insert(0, str(GROUP / "hub" / "tests"))
    from web_cdp import Browser  # noqa: E402
    local = local_images(page)
    broken = re.sub(re.escape(PUBLIC_EMAIL) + r"[\w.-]+", "https://images-off.invalid/banner.jpg", page)
    variants = [
        ("600-light", local, 680, "light"),
        ("600-dark", local, 680, "dark"),
        ("375-light", local, 375, "light"),
        ("375-dark", local, 375, "dark"),
        ("600-images-off", broken, 680, "light"),
        ("375-images-off-dark", broken, 375, "dark"),
        ("outlook-light", outlook(local), 680, "light"),
        ("outlook-dark-partial", outlook(local, "partial"), 680, "light"),
        ("outlook-dark-full", outlook(local, "full"), 680, "light"),
    ]
    tmp = Path(tempfile.mkdtemp(prefix="welcome-shots-"))
    made = []
    b = Browser()
    try:
        for name, src, w, scheme in variants:
            f = tmp / f"{name}.html"
            f.write_text(src, encoding="utf-8")
            b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
            b.call("Emulation.setDeviceMetricsOverride", width=w, height=800, deviceScaleFactor=2, mobile=w < 600)
            b.goto(f.as_uri())
            b.pump(0.3)
            h = int(b.js("Math.ceil(document.documentElement.scrollHeight)"))
            b.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=2, mobile=w < 600)
            b.pump(0.2)
            out = Path(f"{prefix}-{name}.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            b.screenshot(out)
            made.append(out)
    finally:
        b.close()
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=".", help="where the preview .html and .eml go (default: here)")
    ap.add_argument("--name", default="Maria")
    ap.add_argument("--username", default="mg1234")
    ap.add_argument("--signin-url", default="https://papercast.virtualatoms.org/signin")
    ap.add_argument("--site-url", default="https://papercast.virtualatoms.org/")
    ap.add_argument("--github-url", default="https://github.com/leolou0512/papercast")
    ap.add_argument("--to", help="the .eml's To (default: NAME <USERNAME@ic.ac.uk>)")
    ap.add_argument("--from", dest="sender", default="Virtual Atoms Lab Papercast <papercast@virtualatoms.org>")
    ap.add_argument("--by-url", action="store_true", help="keep the banner's public URL in the preview page")
    ap.add_argument("--shots", metavar="PREFIX", help="also render PREFIX-<variant>.png in headless Chrome")
    a = ap.parse_args(argv)

    values = {"name": a.name, "username": a.username, "signin_url": a.signin_url,
              "site_url": a.site_url, "github_url": a.github_url}
    page, text = render(values)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "welcome-preview.html").write_text(page if a.by_url else inline_images(page), encoding="utf-8")
    (out / "welcome-preview.eml").write_bytes(eml(page, text, a.to or f"{a.name} <{a.username}@ic.ac.uk>", a.sender))
    print(f"wrote {out / 'welcome-preview.html'} and {out / 'welcome-preview.eml'}")
    ok = True
    for good, what in checks(page, text):
        ok &= good
        print(("  ok    " if good else "  FAIL  ") + what)
    if a.shots:
        for p in shots(page, a.shots):
            print(f"  shot  {p}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
