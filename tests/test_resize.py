"""Repagination follows completed reader geometry when the terminal resizes."""
from types import SimpleNamespace

import pytest
from textual.widgets import Static

import zimtty.theme as omatheme
from zimtty.app import INFO_MIN_TOTAL, INFO_W, TEXT_MAX, PageView, ZimTTY


def article_html(with_infobox):
    info = ""
    if with_infobox:
        rows = "".join(
            f"<tr><th>Field {i}</th><td>Detailed value for field {i}.</td></tr>"
            for i in range(24)
        )
        info = f'<table class="infobox">{rows}</table>'
    paragraphs = "".join(
        f'<p><a href="article">Paragraph {i}. Follow this article. '
        + "Useful reading content. " * 12 + "</a></p>"
        for i in range(30)
    )
    return "<h1>Resize example</h1>" + info + paragraphs


class MemoryZim:
    path = "memory.zim"
    name = "Resize archive"
    z = SimpleNamespace(article_count=1)

    def __init__(self, with_infobox):
        self.document = article_html(with_infobox)

    def resolve(self, target):
        return ("article", None) if target == "article" else None

    def html(self, path):
        return self.document


class ResizeApp(ZimTTY):
    async def _start_suggestd(self):
        pass

    def _repaginate(self, anchor_block=None):
        self.repagination_count = getattr(self, "repagination_count", 0) + 1
        super()._repaginate(anchor_block)


@pytest.fixture(autouse=True)
def fixed_theme(monkeypatch):
    monkeypatch.setattr(omatheme, "read_colors", lambda **_: dict(omatheme.FALLBACK))
    monkeypatch.setattr(omatheme, "colors_mtime", lambda: 0.0)


def assert_reader_geometry(app):
    view = app.query_one(PageView)
    width, height = view.content_size
    layout = app._layout()
    assert layout.full_width == min(TEXT_MAX, width)
    assert layout.height == height
    assert layout.info_width == (INFO_W if width >= INFO_MIN_TOTAL else 0)
    for page in app.pages:
        assert page.width == (layout.narrow_width if page.info else layout.full_width)
        assert len(page.lines) <= height
        assert all(line.cell_len <= page.width for line in page.lines)
        assert len(page.info) <= max(height - 2, 1)
    text = app.query_one("#text", Static)
    assert text.content_size.width == app.pages[app.page_i].width
    assert text.region.right <= view.content_region.right
    assert text.region.bottom <= view.content_region.bottom


@pytest.mark.parametrize("with_infobox", [False, True])
async def test_repeated_shrink_and_grow_uses_current_reader_geometry(with_infobox):
    app = ResizeApp(MemoryZim(with_infobox), "article")
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        assert_reader_geometry(app)
        app.action_page(2)
        for width, height in [(80, 24), (50, 12), (30, 10), (150, 42),
                              (100, 30), (99, 30), (24, 8), (150, 42)]:
            anchor = app.pages[app.page_i].first_block
            app.action_hints()
            assert app.hints
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert_reader_geometry(app)
            assert app.hints is None and app.hint_buf == "" and app.link_sel is None
            assert anchor in app.pages[app.page_i].blocks
            assert 0 <= app.page_i < len(app.pages)
            # Navigation remains usable after every change of terminal geometry.
            old_page = app.page_i
            app.action_page(1)
            assert app.page_i == min(old_page + 1, len(app.pages) - 1)
            app.action_page(-1)
            assert 0 <= app.page_i < len(app.pages)


async def test_duplicate_resize_events_do_not_repaginate_unchanged_layout():
    app = ResizeApp(MemoryZim(True), "article")
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        before = app.repagination_count
        for _ in range(8):
            app._queue_reader_resize()
        await pilot.pause()
        assert app.repagination_count == before
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.repagination_count == before + 1
        assert_reader_geometry(app)
        before = app.repagination_count
        for _ in range(8):
            app._queue_reader_resize()
        await pilot.pause()
        assert app.repagination_count == before


async def test_resize_after_padding_change_uses_content_area_once():
    app = ResizeApp(MemoryZim(False), "article")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.query_one(PageView).styles.padding = (2, 3, 1, 4)
        await pilot.resize_terminal(81, 25)
        await pilot.pause()
        assert app.query_one(PageView).content_size.width == 81 - 7
        assert app.query_one(PageView).content_size.height == 25 - 1 - 3
        assert_reader_geometry(app)
