"""Shared by the CLI and the hub: preferences, wording checks, bundle schema (SPEC.md sections 5, 6, 10). Owner: A9."""
from __future__ import annotations

import os

BASE_GUIDELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base_guideline.md")


def base_guideline() -> str:
    """Base prompt v1 (the same text as stacks/papercast-group/prompts/base-guideline.md): what
    the hub stores as version 1, with wording.read() as its wording."""
    with open(BASE_GUIDELINE, encoding="utf-8") as fh:
        return fh.read()
