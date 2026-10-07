"""Read the active Omarchy theme (colors.toml) and map it to our palette + a Textual theme."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from textual.color import Color, ColorParseError
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


class ThemeError(ValueError):
    """The configured theme cannot safely supply application colors."""


def _validated_colors(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ThemeError("Theme colors must be a table.")
    c = dict(values)
    # Older/third-party themes may define only color0..15.
    aliases = {"blue": "color4", "magenta": "color5"}
    for key, default in FALLBACK.items():
        if key != "accent":
            c.setdefault(key, c.get(aliases[key], default) if key in aliases else default)
    if "accent" not in c or (isinstance(c["accent"], str) and not c["accent"].strip()):
        c["accent"] = c["blue"]
    if not isinstance(c["mode"], str) or c["mode"] not in ("dark", "light"):
        raise ThemeError("Theme mode must be 'dark' or 'light'.")
    # Validate blue before the accent that may have inherited its value.
    color_keys = [key for key in FALLBACK if key not in ("mode", "accent")] + ["accent"]
    for key in color_keys:
        value = c[key]
        if not isinstance(value, str) or not value.strip():
            raise ThemeError(f"Theme color '{key}' must be a non-empty color string.")
        value = value.strip()
        try:
            Color.parse(value)
        except (ColorParseError, ValueError, OverflowError) as error:
            raise ThemeError(f"Invalid color for theme field '{key}'.") from error
        c[key] = value
    return c


def read_colors(strict: bool = False) -> dict:
    """Load validated colors; strict reloads can retain the previous theme on error.

    A missing file means no Omarchy theme is active and always uses the default.
    Other read or validation errors raise ThemeError only when strict is enabled.
    """
    try:
        with open(COLORS, "rb") as f:
            c = tomllib.load(f)
        return _validated_colors(c)
    except FileNotFoundError:
        pass
    except ThemeError:
        if strict:
            raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        if strict:
            raise ThemeError("Could not read valid theme colors.") from error
    return _validated_colors(FALLBACK)


def colors_mtime() -> float:
    try:
        return COLORS.stat().st_mtime
    except OSError:
        return 0.0


def build(c: dict) -> tuple[Palette, Theme]:
    c = _validated_colors(c)
    fg = c["foreground"]
    accent = c["accent"]
    # Rich styles need opaque RGB colors; Textual also accepts short hex, alpha,
    # and ANSI color names which Rich cannot parse directly.
    rgb = {
        key: Color.parse(c[key]).hex6.lower()
        for key in FALLBACK if key != "mode"
    }
    pal = Palette(
        fg=rgb["foreground"],
        bright=rgb["bright_foreground"],
        muted=rgb["muted"],
        accent=rgb["accent"],
        link=rgb["blue"],
        heading=rgb["bright_foreground"],
        sub=rgb["accent"],
        # The side box's border is "$accent 60%"; inline boxes match it.
        frame=Color.parse(c["background"]).blend(Color.parse(accent), 0.6).hex6.lower(),
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
