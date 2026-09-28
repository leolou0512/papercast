"""poppler wrappers, copied from Leo's runner (pdf.py): page count, text (links fallback), page
images for the agent, figure crops for the explainer. Every call is niced and time-bounded;
the PDF is hostile.

poppler is optional on a contributor's laptop: without `pdftoppm` and `pdfinfo` there are no
page images and no crops (the explainer then has SVG figures and text only), without
`pdftotext` no text fallback for links. `available()` says which.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

TIMEOUT = 120
ARXIV_NEW = r"\d{4}\.\d{4,5}"
# Where Homebrew and MacPorts put poppler, besides the PATH.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin")
DISABLED: set[str] = set()      # tests: names treated as not installed


class PdfError(Exception):
    pass


def tool(name: str) -> str | None:
    """Absolute path of a poppler tool, or None when it is not installed."""
    if name in DISABLED:
        return None
    path = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_PATH])
    return shutil.which(name, path=path)


def available() -> dict:
    """{"crops": pdftoppm and pdfinfo, "text": pdftotext}."""
    return {"crops": bool(tool("pdftoppm") and tool("pdfinfo")), "text": bool(tool("pdftotext"))}


def _run(argv: list[str], timeout: int = TIMEOUT, nice: int = 10) -> subprocess.CompletedProcess:
    exe = tool(argv[0])
    if not exe:
        raise PdfError(f"{argv[0]} is not installed")
    nice_bin = shutil.which("nice")
    full = ([nice_bin, "-n", str(nice)] if nice_bin else []) + [exe, *argv[1:]]
    return subprocess.run(full, capture_output=True, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(1024).lstrip().startswith(b"%PDF-")
    except OSError:
        return False


def info(path: str) -> dict:
    """pdfinfo as a dict (`_pages` = the page count). Raises PdfError for a non-PDF, damaged or
    encrypted file. Without pdfinfo: only the header is checked, `_pages` is 0."""
    if not is_pdf(path):
        raise PdfError("not a PDF file")
    if not tool("pdfinfo"):
        return {"_pages": 0, "_encrypted": False}
    try:
        r = _run(["pdfinfo", path], timeout=60)
    except subprocess.TimeoutExpired:
        raise PdfError("pdfinfo timed out: the PDF may be damaged")
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = err[-1] if err else f"pdfinfo exit {r.returncode}"
        if "Incorrect password" in msg or "encrypted" in msg.lower():
            raise PdfError("the PDF is encrypted")
        raise PdfError(f"damaged PDF: {msg[:200]}")
    d = {}
    for line in out.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            d[k.strip()] = v.strip()
    d["_encrypted"] = d.get("Encrypted", "no").startswith("yes")
    try:
        d["_pages"] = int(d.get("Pages", "0"))
    except ValueError:
        d["_pages"] = 0
    if d["_pages"] <= 0:
        raise PdfError("the PDF has no pages")
    return d


def text(path: str, out_path: str, first: int | None = None, last: int | None = None) -> str:
    argv = ["pdftotext", "-enc", "UTF-8"]
    if first:
        argv += ["-f", str(first)]
    if last:
        argv += ["-l", str(last)]
    argv += [path, out_path]
    try:
        r = _run(argv)
    except subprocess.TimeoutExpired:
        raise PdfError("text extraction timed out")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PdfError(f"text extraction failed: {(msg[-1] if msg else r.returncode)}")
    with open(out_path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def page_sizes(path: str, pages: int) -> dict[int, tuple[float, float]]:
    r = _run(["pdfinfo", "-f", "1", "-l", str(pages), path], timeout=60)
    sizes = {}
    for m in re.finditer(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)",
                         r.stdout.decode("utf-8", "replace")):
        sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    return sizes


def render_pages(path: str, out_dir: str, pages: int, dpi: int = 100, max_pages: int = 80) -> int:
    """pages/p-NNN.png for the agent to look at (it has no shell to make them itself)."""
    os.makedirs(out_dir, exist_ok=True)
    n = min(pages, max_pages)
    r = _run(["pdftoppm", "-r", str(dpi), "-png", "-f", "1", "-l", str(n), path,
              os.path.join(out_dir, "p")], timeout=600)
    if r.returncode != 0:
        raise PdfError("page rendering failed")
    # pdftoppm pads to the width of the page count (p-1.png or p-01.png): normalise to 3.
    for name in os.listdir(out_dir):
        m = re.match(r"^p-(\d+)\.png$", name)
        if m:
            want = f"p-{int(m.group(1)):03d}.png"
            if want != name:
                os.replace(os.path.join(out_dir, name), os.path.join(out_dir, want))
    return n


def crop_png(path: str, page: int, box: tuple[float, float, float, float],
             size_pt: tuple[float, float], dpi: int = 150) -> bytes:
    """PNG bytes of `box` (page fractions x0,y0,x1,y1) of `page`, via pdftoppm's crop area.
    With no output root pdftoppm writes the image to stdout (measured, poppler 22.02)."""
    x0, y0, x1, y1 = box
    w_px = size_pt[0] * dpi / 72.0
    h_px = size_pt[1] * dpi / 72.0
    x, y = int(x0 * w_px), int(y0 * h_px)
    w, h = max(1, int((x1 - x0) * w_px)), max(1, int((y1 - y0) * h_px))
    r = _run(["pdftoppm", "-r", str(dpi), "-png", "-f", str(page), "-l", str(page),
              "-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h), "-singlefile", path],
             timeout=120)
    if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
        raise PdfError(f"could not crop page {page}")
    return r.stdout


# --- identifiers printed in the paper ------------------------------------------

ARXIV_STAMP = re.compile(rf"arXiv:\s?({ARXIV_NEW})(v\d+)?\s*\[[a-zA-Z\-.]+\]")
DOI_MARKED = re.compile(r"(?:doi\.org/|doi:\s?|DOI:?\s?)(10\.\d{4,9}/[^\s\"'<>]+)", re.I)


def clean_doi(d: str) -> str:
    return d.rstrip(".,;)]}").lower()


def identifiers_from_first_page(page1: str) -> dict:
    """arXiv id from arXiv's own margin stamp, DOI only where page one marks it as a DOI.
    Deliberately narrow: a published paper's first page can mention another paper's arXiv id,
    but not with the stamp's `[cs.LG]` category bracket."""
    out = {"arxiv_id": None, "doi": None}
    m = ARXIV_STAMP.search(page1)
    if m:
        out["arxiv_id"] = m.group(1)
    m = DOI_MARKED.search(page1)
    if m:
        out["doi"] = clean_doi(m.group(1))
    return out
