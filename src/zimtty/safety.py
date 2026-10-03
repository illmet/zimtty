"""Sanitize untrusted display text without changing document identities."""
from __future__ import annotations

import re


# Keep ordinary layout whitespace and all meaningful Unicode, including joiners.
# Escape sequences cannot reach the terminal once their C0/C1 controls are gone.
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def safe_text(value: str) -> str:
    """Remove C0, DEL and C1 controls, except tab, newline and carriage return.

    Call this after HTML entity decoding and only for display strings. Archive
    paths, anchor IDs and link targets must retain their original identities.
    """
    return _TERMINAL_CONTROLS.sub("", value)
