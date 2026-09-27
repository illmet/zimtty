"""UI tests: drive the Textual app headlessly (replaces scratch/drive*, keys_check,
search_links_check, searchbar_*, compose_probe, typing_lag)."""
import asyncio
import time
from pathlib import Path

import pytest
from rich.style import Style
from textual._xterm_parser import XTermParser

import zimtty.theme as omatheme
from zimtty.app import HelpPanel, ZimTTY


def drawn(app) -> str:
    """What is actually on screen (not just widget state)."""
    return "\n".join("".join(s.text for s in st) for st in app.screen._compositor.render_strips())


def link_spans(app) -> int:
    return sum(1 for p in app.pages for ln in p.lines for sp in ln.spans
               if isinstance(sp.style, Style) and sp.style.meta and "href" in sp.style.meta)


async def wait_for(cond, timeout=10.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise TimeoutError
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- compose key

def test_compose_macro_parses_as_text():
    """foot sends Compose (Caps) macros as ONE long kitty CSI-u sequence; Textual
    used to give up after 32 chars and type the raw bytes into the input."""
    s = "ilyaamet@gmail.com"
    seq = "\x1b[101;;" + ":".join(str(ord(c)) for c in s) + "u"
    assert "".join(e.character or "" for e in XTermParser().feed(seq)) == s


# ---------------------------------------------------------------- search

async def test_search_text_visible_and_opens(small_zim):
    app = ZimTTY(small_zim, None)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        inp = app.query_one("#search-input")
        assert app.focused is inp and inp.content_region.height == 1
        await pilot.press(*"max weber")
        await wait_for(lambda: app._results_for == "max weber")
        assert "max weber" in drawn(app)  # the invisible-text bug
        await pilot.press("enter")
        await pilot.pause()
        assert app.article.title == "Max Weber"


@pytest.mark.parametrize("theme", ["catppuccin-latte", "nord", "flexoki-light"])
async def test_search_visible_across_themes(small_zim, theme, monkeypatch):
    colors = Path(f"/usr/share/omarchy/themes/{theme}/colors.toml")
    if not colors.exists():
        pytest.skip("omarchy theme not installed")
    monkeypatch.setattr(omatheme, "COLORS", colors)
    app = ZimTTY(small_zim, None)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"weber")
        await pilot.pause(0.5)
        assert "weber" in drawn(app)


async def test_typing_not_frozen_by_slow_query(big_zim):
    """A 1-letter query takes ~1.5 s in libzim (which holds the GIL); it runs in
    a subprocess, so keys typed meanwhile must appear immediately."""
    app = ZimTTY(big_zim, None)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(lambda: app._sugg_proc is not None)
        pid = app._sugg_proc.pid
        inp = app.query_one("#search-input")
        await pilot.press("g")
        await asyncio.sleep(0.15)
        t = time.perf_counter()
        await pilot.press("e")
        await wait_for(lambda: inp.value.endswith("e"))
        assert time.perf_counter() - t < 0.3
        # Enter before results arrive opens the top hit once they do
        await pilot.press("backspace", "backspace", *"heidegger", "enter")
        await wait_for(lambda: app.article is not None and not app.query_one("SearchBar").display)
        assert app.article.title == "Martin Heidegger"
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(pid, 0)  # suggestion subprocess cleaned up


# ---------------------------------------------------------------- reading

async def test_toc_jump_puts_heading_at_top_and_history(small_zim):
    app = ZimTTY(small_zim, "Max Weber")
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        assert app.page_i == 1
        await pilot.press("c", "j", "enter", "j", "enter")  # Methodology > Verstehen
        await pilot.pause()
        assert app.pages[app.page_i].lines[0].plain.startswith("Verstehen")
        page = app.page_i
        await pilot.press("f")
        await pilot.pause()
        await pilot.press(*sorted(app.hints)[0])
        await pilot.pause()
        assert app.article.title != "Max Weber"
        await pilot.press("left")
        await pilot.pause()
        assert app.article.title == "Max Weber" and app.page_i == page


async def test_help_toggles_single_panel(small_zim):
    app = ZimTTY(small_zim, "Max Weber")
    async with app.run_test(size=(150, 42)) as pilot:
        h = app.query_one(HelpPanel)
        await pilot.press("question_mark"); await pilot.pause()
        assert h.display
        await pilot.press("question_mark"); await pilot.pause()
        assert not h.display
        await pilot.press("question_mark", "escape"); await pilot.pause()
        assert not h.display and not app._notifications


async def test_link_cursor_n_N_enter(small_zim):
    app = ZimTTY(small_zim, "Max Weber")
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        await pilot.press("n", "n", "n", "N"); await pilot.pause()
        assert app.link_sel == 1
        p0 = app.page_i
        while app.page_i == p0:
            await pilot.press("n")
        assert app.link_sel == 0                       # walked onto the next page
        await pilot.press("N"); await pilot.pause()
        assert app.page_i == p0 and app.link_sel == len(app._link_targets()) - 1
        await pilot.press("g", "n"); await pilot.pause()
        want = app._href_title(app._link_targets()[0][1])
        await pilot.press("enter"); await pilot.pause()
        assert app.article.title != "Max Weber" and want  # followed the selected link
        await pilot.press("left", "n", "escape"); await pilot.pause()
        assert app.link_sel is None


async def test_links_toggle(small_zim):
    app = ZimTTY(small_zim, "Max Weber")
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        on, page = link_spans(app), app.page_i
        await pilot.press("l"); await pilot.pause()
        assert link_spans(app) == 0 and app.page_i == page
        await pilot.press("n", "f"); await pilot.pause()
        assert app.link_sel is None and app.hints is None
        await pilot.press("l"); await pilot.pause()
        assert link_spans(app) == on > 0
        await pilot.press("c", "l"); await pilot.pause()  # l in contents = expand
        assert not app.plain_links


async def test_notes_key(small_zim):
    app = ZimTTY(small_zim, "Hunger in the United Kingdom")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("a"); await pilot.pause()
        assert app.pages[app.page_i].lines[0].plain.startswith("Additional notes")
