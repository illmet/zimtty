"""Invalid theme edits leave the reader usable until valid colors arrive."""
from copy import deepcopy
import os

import pytest

from zimtty import theme
from zimtty.app import TocPanel

from .test_security_ui import MemoryApp, MemoryZim


@pytest.fixture
def write_colors(tmp_path, monkeypatch):
    path = tmp_path / "colors.toml"
    monkeypatch.setattr(theme, "COLORS", path)
    version = 0

    def write(content):
        nonlocal version
        version += 1
        path.write_text(content)
        # Each write has a distinct timestamp even on coarse filesystems.
        stamp = 1_700_000_000 + version
        os.utime(path, (stamp, stamp))

    return write


def theme_events(app):
    return [line for line in app.diagnostics.render().splitlines()
            if "theme_invalid" in line]


@pytest.mark.parametrize("content", [
    'foreground = 42\n',
    'foreground = "PRIVATE_INVALID_COLOR"\n',
    'foreground = [\n',
])
async def test_invalid_startup_theme_uses_fallback_and_reader_stays_usable(write_colors, content):
    write_colors(content)
    fallback_palette, fallback_theme = theme.build(theme.FALLBACK)
    app = MemoryApp(MemoryZim(), "good")
    assert app.pal == fallback_palette
    assert app._theme == fallback_theme
    assert len(theme_events(app)) == 1
    assert "PRIVATE_INVALID_COLOR" not in app.diagnostics.render()

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.article.path == "good"
        app._check_theme()
        assert len(theme_events(app)) == 1  # unchanged invalid file is not retried
        await pilot.press("j", "c")
        await pilot.pause()
        assert app.query_one(TocPanel).display
        await pilot.press("escape", "/", *"other", "enter")
        await pilot.pause()
        assert app.article.path == "other"


@pytest.mark.parametrize("bad_update", [
    'selection = "PRIVATE_INVALID_COLOR"\n',
    'foreground = { unexpected = "value" }\n',
    'foreground = "partially written',
])
async def test_bad_live_theme_retains_state_and_later_valid_update_recovers(write_colors, bad_update):
    write_colors('foreground = "#123456"\nbackground = "#102030"\n')
    app = MemoryApp(MemoryZim(), "good")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        assert app.page_i > 0
        article, pages, page_i = app.article, app.pages, app.page_i
        palette, active_theme = app.pal, app._theme
        history = deepcopy(app.history)

        write_colors(bad_update)
        app._check_theme()
        await pilot.pause()

        assert app.article is article and app.pages is pages
        assert app.page_i == page_i and app.history == history
        assert app.pal is palette and app._theme is active_theme
        assert len(theme_events(app)) == 1
        notifications = list(app._notifications)
        assert len(notifications) == 1
        assert notifications[0].message == "Theme update ignored: invalid colors."
        assert notifications[0].severity == "warning"
        assert "PRIVATE_INVALID_COLOR" not in app.diagnostics.render()
        assert bad_update not in app.diagnostics.render()

        # The periodic check should not repeatedly warn for the same file mtime.
        app._check_theme()
        app._check_theme()
        await pilot.pause()
        assert len(theme_events(app)) == 1
        assert list(app._notifications) == notifications

        await pilot.press("g", "j", "c")
        await pilot.pause()
        assert app.page_i == page_i and app.query_one(TocPanel).display
        await pilot.press("escape")
        anchor = app.pages[app.page_i].first_block

        write_colors('mode = "light"\nforeground = "#334455"\nbackground = "#ffffff"\n')
        app._check_theme()
        await pilot.pause()

        assert app.pal.fg == "#334455" and app.pal is not palette
        assert app._theme.dark is False and app._theme.background == "#ffffff"
        assert app.pages is not pages
        assert app.article.path == "good" and app.history == history
        assert app.pages[app.page_i].first_block == anchor
        assert len(theme_events(app)) == 1
        await pilot.press("/", *"other", "enter")
        await pilot.pause()
        assert app.article.path == "other"
