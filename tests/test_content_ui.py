"""Reader navigation connects parsed anchors, relative URLs, code and tables."""
import pytest

from zimtty import theme
from zimtty.app import ZimTTY

from .test_links import Entry, MemoryZim


class Reader(ZimTTY):
    async def _start_suggestd(self):
        pass


@pytest.fixture(autouse=True)
def fixed_theme(monkeypatch, tmp_path):
    monkeypatch.setattr(theme, "COLORS", tmp_path / "missing-theme")


def page_text(app):
    return "\n".join(line.plain for line in app.pages[app.page_i].lines)


async def test_heading_aliases_relative_links_and_code_anchor_start():
    start = Entry("docs/Start", '<h1 id="top">Start</h1><p>Introduction.</p>'
                  '<h2 id="Current"><span id="Legacy">Section</span></h2><p>Details.</p>')
    code = "\n".join(f"    line_{i:03d}()" for i in range(80))
    other = Entry("other/Page", '<h1>Other</h1><p>Before.</p>'
                  f'<h2><span id="Commands">Commands</span></h2><pre id="code">{code}</pre>')
    app = Reader(MemoryZim([start, other]), "docs/Start")
    async with app.run_test(size=(70, 18)) as pilot:
        await pilot.pause()
        assert app._anchor_block("Current") == app._anchor_block("Legacy") > 0
        app.action_follow("#Legacy")
        await pilot.pause()
        assert app.pages[app.page_i].first_block == app.article.anchors["Legacy"]
        assert "Details." in page_text(app)
        app.action_back()
        assert app.page_i == 0
        app.action_forward()
        assert "Details." in page_text(app)
        app.action_follow("../other/Page?print=1#code")
        await pilot.pause()
        assert app.article.path == "other/Page"
        assert "line_000()" in page_text(app)
        assert "line_079()" not in page_text(app)
        assert app.pages[app.page_i].first_block == app.article.anchors["code"]
        app.action_follow("#")
        assert app.page_i == 0
        app.action_back()
        assert "line_000()" in page_text(app)


async def test_local_html_base_is_used_for_links_and_missing_fragment_keeps_place():
    start = Entry("docs/Start", '<base href="../manual/"><h1>Start</h1><p>Body.</p>')
    target = Entry("manual/Next", '<h1>Next</h1><h2 id="Café">Café</h2><p>Content.</p>')
    app = Reader(MemoryZim([start, target]), "docs/Start")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_follow("Next#Caf.C3.A9")
        await pilot.pause()
        assert app.article.path == "manual/Next"
        assert app.pages[app.page_i].first_block == app.article.anchors["Café"]
        position = app.page_i
        history = list(app.history)
        app.action_follow("#missing")
        app.action_follow("https://example.com")
        assert app.page_i == position and app.history == history


async def test_table_links_are_clickable_after_wrapping_and_resize():
    source = Entry("docs/Start", '<h1>Table links</h1><table><tr><th>Command</th><th>Purpose</th></tr>'
                   '<tr><td><a href="Next#target">very long command documentation link</a></td>'
                   '<td>Details about this command.</td></tr></table>')
    target = Entry("docs/Next", '<h1>Next</h1><h2><span id="target">Target</span></h2><p>Reached.</p>')
    app = Reader(MemoryZim([source, target]), "docs/Start")
    async with app.run_test(size=(70, 22)) as pilot:
        await pilot.pause()
        for size in ((70, 22), (30, 12), (100, 30)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            page_with_link = next(i for i, page in enumerate(app.pages)
                                  if app._page_links(page.lines))
            app.page_i = page_with_link
            targets = app._link_targets()
            assert len(targets) == 1
            app.action_hints()
            assert len(app.hints) == 1
            app.hints = None
        app.link_sel = 0
        app.action_link_open()
        await pilot.pause()
        assert app.article.path == "docs/Next"
        assert "Reached." in page_text(app)


async def test_long_code_continuation_survives_history_and_display_toggles():
    code = "\n".join(f"    line_{i:03d}()" for i in range(100))
    source = Entry("docs/Code", f'<h1>Code</h1><pre id="code">{code}</pre>')
    target = Entry("docs/Other", '<h1>Other</h1><p>Other content.</p>')
    app = Reader(MemoryZim([source, target]), "docs/Code")
    async with app.run_test(size=(70, 18)) as pilot:
        await pilot.pause()
        app.action_page(3)
        place = app._current_place()
        current = page_text(app)
        assert place.page_offset > 0
        app.action_toggle_links()
        assert page_text(app) == current
        app.action_toggle_refs()
        assert page_text(app) == current
        app.open("docs/Other")
        app.action_back()
        assert page_text(app) == current
        assert app._current_place() == place
        app.action_forward()
        assert app.article.path == "docs/Other"


def infobox_entry(path, title, fields=30, paragraphs=30):
    rows = "".join(f"<tr><th>Field {k}</th><td>Value {k}</td></tr>" for k in range(fields))
    text = "".join(f"<p>Paragraph {k}. " + "Reading text. " * 20 + "</p>" for k in range(paragraphs))
    return Entry(path, f'<h1>{title}</h1><table class="infobox">{rows}</table>{text}')


def all_text(app):
    return "\n".join(line.plain for page in app.pages for line in page.lines)


async def test_infobox_in_the_text_follows_the_lead_and_i_expands_or_collapses_it():
    app = Reader(MemoryZim([infobox_entry("docs/Start", "Start"),
                            infobox_entry("docs/Other", "Other")]), "docs/Start")
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        first = page_text(app)
        assert first.index("Paragraph 0.") < first.index("┌") < first.index("more lines · i")
        assert "Field 29" not in all_text(app)
        box = next(i for i, block in enumerate(app.article.blocks) if block.kind == "infobox")
        # Expanding from anywhere shows the box from its start.
        app.action_page(4)
        await pilot.press("i")
        assert app.info_expanded and "Field 29" in all_text(app)
        assert app.page_i == app._block_pages(app.pages, box)[0]
        assert list(app._notifications)[-1].message == "infobox expanded"
        # Collapsing keeps the reading place.
        app.action_page(6)
        reading = app.pages[app.page_i].first_block
        await pilot.press("i")
        assert not app.info_expanded and "Field 29" not in all_text(app)
        assert reading in app.pages[app.page_i].blocks
        # The expansion belongs to its article.
        await pilot.press("i")
        app.action_follow("Other")
        assert app.article.path == "docs/Other" and not app.info_expanded
        app.action_back()
        assert app.article.path == "docs/Start" and not app.info_expanded
        # Beside the text the box is already whole.
        await pilot.resize_terminal(150, 40)
        await pilot.pause()
        await pilot.press("i")
        assert not app.info_expanded
        assert list(app._notifications)[-1].message == "the infobox is shown beside the text"


@pytest.mark.parametrize("entry,message", [
    (infobox_entry("docs/Small", "Small", fields=2, paragraphs=2), "the whole infobox is shown"),
    (Entry("docs/Plain", "<h1>Plain</h1><p>Text.</p>"), "no infobox on this article"),
])
async def test_i_explains_when_there_is_nothing_to_expand(entry, message):
    app = Reader(MemoryZim([entry]), entry.path)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        pages = [page.lines for page in app.pages]
        await pilot.press("i")
        assert not app.info_expanded and [page.lines for page in app.pages] == pages
        assert list(app._notifications)[-1].message == message
