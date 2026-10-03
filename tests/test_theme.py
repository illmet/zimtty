"""Theme validation uses temporary files and never changes the desktop theme."""
from dataclasses import fields

import pytest
from rich.style import Style

from zimtty import theme


@pytest.fixture
def colors_file(tmp_path, monkeypatch):
    path = tmp_path / "colors.toml"
    monkeypatch.setattr(theme, "COLORS", path)
    return path


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_valid_theme_builds_palette_and_textual_variables(colors_file, mode):
    colors_file.write_text(f'mode = "{mode}"\nforeground = "#123456"\nbackground = "#abcdef"\n')
    colors = theme.read_colors(strict=True)
    palette, textual_theme = theme.build(colors)
    assert palette.fg == "#123456"
    assert textual_theme.background == "#abcdef"
    assert textual_theme.dark == (mode == "dark")
    assert textual_theme.variables["block-cursor-foreground"] == "#123456"
    assert textual_theme.to_color_system().generate()["background"] == "#ABCDEF"


def test_partial_legacy_theme_and_blank_accent_use_defaults(colors_file):
    colors_file.write_text('color4 = "#123456"\ncolor5 = "#abcdef"\naccent = " "\n')
    colors = theme.read_colors(strict=True)
    palette, textual_theme = theme.build(colors)
    assert colors["blue"] == colors["accent"] == "#123456"
    assert colors["magenta"] == "#abcdef"
    assert colors["foreground"] == theme.FALLBACK["foreground"]
    assert palette.accent == "#123456"
    assert textual_theme.secondary == "#abcdef"


def test_build_accepts_partial_values_without_mutating_input():
    colors = {"blue": "#123456"}
    palette, textual_theme = theme.build(colors)
    assert colors == {"blue": "#123456"}
    assert palette.accent == textual_theme.accent == "#123456"


@pytest.mark.parametrize("color", ["#fff", "rgba(255, 255, 255, 0.5)", "ansi_bright_white"])
def test_textual_color_formats_also_make_valid_rich_styles(color):
    palette, textual_theme = theme.build({"foreground": color, "accent": color})
    assert palette.fg.startswith("#") and len(palette.fg) == 7
    for field in fields(palette):
        Style(color=getattr(palette, field.name))
    textual_theme.to_color_system().generate()


@pytest.mark.parametrize("field,value", [
    ("foreground", 42), ("background", True), ("accent", None),
    ("muted", ["#ffffff"]), ("selection", {"color": "#ffffff"}),
    ("lighter_background", "not-a-color"), ("bright_foreground", ""),
    ("blue", "#gggggg"), ("magenta", "#12"),
])
def test_build_rejects_bad_colors_before_theme_registration(field, value):
    with pytest.raises(theme.ThemeError, match=field):
        theme.build({field: value})


@pytest.mark.parametrize("mode", ["automatic", "LIGHT", "", 0, None, []])
def test_build_rejects_invalid_mode(mode):
    with pytest.raises(theme.ThemeError, match="mode"):
        theme.build({"mode": mode})


@pytest.mark.parametrize("content", [
    b'foreground = "not-a-color"\n',
    b'foreground = 42\n',
    b'mode = "automatic"\n',
    b'foreground = [\n',
    b'foreground = "\xff"\n',
])
def test_bad_file_falls_back_by_default_and_raises_in_strict_mode(colors_file, content):
    colors_file.write_bytes(content)
    assert theme.read_colors() == theme.FALLBACK
    with pytest.raises(theme.ThemeError):
        theme.read_colors(strict=True)


def test_missing_file_falls_back_in_both_modes(colors_file):
    assert theme.read_colors() == theme.FALLBACK
    assert theme.read_colors(strict=True) == theme.FALLBACK


def test_unreadable_file_falls_back_or_raises(colors_file):
    # A directory produces a deterministic I/O failure even when run as root.
    colors_file.mkdir()
    assert theme.read_colors() == theme.FALLBACK
    with pytest.raises(theme.ThemeError):
        theme.read_colors(strict=True)


def test_fallback_result_is_independent(colors_file):
    colors = theme.read_colors()
    colors["foreground"] = "modified"
    assert theme.read_colors()["foreground"] == theme.FALLBACK["foreground"]
