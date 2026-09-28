"""The upload bundle's manifest (SPEC.md section 5). A9 owns this file."""
from __future__ import annotations

MANIFEST_VERSION = 1
MAX_BYTES = 50 * 1024 * 1024
REQUIRED_FILES = ("script", "explainer_json", "explainer_html")


def validate(manifest, names) -> list[str]:
    """Problems with a manifest, given the set of file names in the bundle: [] when fine."""
    out = []
    if not isinstance(manifest, dict):
        return ["manifest.json is not a JSON object"]
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        out.append(f"manifest_version must be {MANIFEST_VERSION}")
    paper = manifest.get("paper")
    if not isinstance(paper, dict) or not isinstance(paper.get("title"), str) or not paper["title"].strip():
        out.append("paper.title is required")
    if not manifest.get("paper_id") and not manifest.get("claim_id"):
        out.append("paper_id (a new version) or claim_id (a new paper) is required")
    files = manifest.get("files")
    if not isinstance(files, dict):
        out.append("files is required")
    else:
        for k in REQUIRED_FILES:
            if not files.get(k):
                out.append(f"files.{k} is required")
            elif files[k] not in names:
                out.append(f"files.{k} names {files[k]!r}, which is not in the bundle")
    links = manifest.get("links", [])
    if not isinstance(links, list):
        out.append("links must be a list")
    else:
        for i, l in enumerate(links):
            if not isinstance(l, dict) or l.get("direction") not in ("builds_on", "built_on_by") or l.get("grade") not in ("e", "s", "w") or not isinstance(l.get("other"), dict):
                out.append(f"links[{i}]: other, direction (builds_on|built_on_by), grade (e|s|w)")
    return out
