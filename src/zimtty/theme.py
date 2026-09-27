"""Read the active Omarchy theme (colors.toml) and map it to our palette + a Textual theme."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from textual.theme import Theme

from .zimdoc import Palette

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
COLORS = STATE / "omarchy/current/theme/colors.toml"

FALLBACK = {  # tokyo-night-ish, used when not on Omarchy
    "mode": "dark", "background": "#1a1b26", "foreground": "#c0caf5",
    "accent": "#7aa2f7", "muted": "#565f89", "selection": "#283457",
    "lighter_background": "#24283b", "dark_background": "#16161e",
    "blue": "#7aa2f7", "magenta": "#bb9af7", "bright_foreground": "#ffffff",
}


def read_colors() -> dict:
    try:
        with open(COLORS, "rb") as f:
            c = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return dict(FALLBACK)
    # older/third-party themes may only define color0..15; fill gaps
    for k, v in FALLBACK.items():
        c.setdefault(k, c.get("color" + {"blue": "4", "magenta": "5"}.get(k, "x"), v))
    return c


def colors_mtime() -> float:
    try:
        return COLORS.stat().st_mtime
    except OSError:
        return 0.0


def build(c: dict) -> tuple[Palette, Theme]:
    fg = c["foreground"]
    accent = c.get("accent") or c["blue"]
    pal = Palette(
        fg=fg,
        bright=c.get("bright_foreground", fg),
        muted=c.get("muted", "#808080"),
        accent=accent,
        link=c.get("blue", accent),
        heading=c.get("bright_foreground", fg),
        sub=accent,
    )
    theme = Theme(
        name="omarchy",
        primary=accent,
        secondary=c.get("magenta", accent),
        accent=accent,
        foreground=fg,
        background=c["background"],
        surface=c["background"],
        panel=c.get("lighter_background", c["background"]),
        boost=c.get("lighter_background"),
        dark=c.get("mode", "dark") != "light",
        variables={
            "text-muted": c.get("muted", "#808080"),
            "block-cursor-background": c.get("selection", accent),
            "block-cursor-foreground": fg,
            "block-cursor-text-style": "bold",
            "input-selection-background": c.get("selection", accent),
            "footer-background": c["background"],
        },
    )
    return pal, theme
