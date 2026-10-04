"""Archive text and failed articles remain ordinary, recoverable UI input."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Input, OptionList, Tree

import zimtty.theme as omatheme
from zimtty.app import SearchBar, ZimTTY
from zimtty.paginate import Paginator


GOOD = '<h1>Good</h1><p>Lead <a href="other">other</a>.</p><h2 id="Details">Details</h2><p>Body.</p>'
OTHER = '<h1>Other</h1><p>Another article.</p>'


class MemoryZim:
    path = "memory.zim"

    def __init__(self, documents=None, name="Test archive", titles=None):
        self.documents = documents if documents is not None else {"good": GOOD, "other": OTHER}
        self.name = name
        self.titles = titles or {}
        self.z = SimpleNamespace(article_count=len(self.documents))

    def resolve(self, target):
        path, _, fragment = target.partition("#")
        return (path, fragment or None) if path in self.documents else None

    def html(self, path):
        value = self.documents[path]
        if isinstance(value, Exception):
            raise value
        return value

    def suggest(self, query, n=5):
        return [(path, self.titles.get(path, path)) for path in self.documents
                if query and query in path][:n]


class MemoryApp(ZimTTY):
    async def _start_suggestd(self):
        pass

    def _send_query(self, query):
        self._show_results(query, self.zim.suggest(query))


@pytest.fixture(autouse=True)
def fixed_theme(monkeypatch):
    monkeypatch.setattr(omatheme, "read_colors", lambda **_: dict(omatheme.FALLBACK))
    monkeypatch.setattr(omatheme, "colors_mtime", lambda: 0.0)


def drawn(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


@pytest.mark.parametrize("heading", [
    "Use [/bold] literally",
    "[bold]Ordinary brackets[/bold]",
    "[@click=app.bell]Ordinary brackets[/]",
])
async def test_headings_are_literal_and_clicks_do_not_invoke_text_actions(heading, monkeypatch):
    html = f'<h1>Title</h1><p>Lead.</p><h2 id="section">{heading}</h2><p>Body.</p>'
    app = MemoryApp(MemoryZim({"article": html}), "article")
    bells = []
    monkeypatch.setattr(app, "action_bell", lambda: bells.append(True))
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause()
        node = app.query_one("#toc-tree", Tree).root.children[0]
        assert node.label.plain == heading
        assert not any(isinstance(span.style, Style) and span.style.meta.get("@click")
                       for span in node.label.spans)
        await pilot.press("c")
        await pilot.click("#toc-tree", offset=(3, 0))
        await pilot.pause()
        assert not bells
        assert app.article.path == "article"


async def test_archive_search_and_notification_text_is_literal():
    name = "Archive [/bold]"
    title = "Result [bold]literal[/bold]"
    zim = MemoryZim(name=name, titles={"good": title})
    app = MemoryApp(zim, None)
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause()
        assert name in drawn(app)
        await pilot.press(*"good")
        await pilot.pause(0.25)
        option = app.query_one("#search-results", OptionList).get_option_at_index(0)
        assert isinstance(option.prompt, Text)
        assert option.prompt.plain == title
        assert title in drawn(app)
        missing = "missing [/bold]"
        assert not app.open(missing)
        await pilot.pause()
        notification = list(app._notifications)[-1]
        assert notification.message == f"not in this ZIM: {missing}"
        assert notification.markup is False
        await pilot.press("enter")
        await pilot.pause()
        assert app.article.path == "good"


async def test_terminal_controls_never_reach_rendered_document_or_link_preview():
    href = "other%1B%07%C2%9B"
    html = ('<h1>Title&#27;[31m</h1><p>Before &#27;]0;ARTICLE_MARKER&#27;\\ after'
            f' <a href="{href}">target</a></p>')
    app = MemoryApp(MemoryZim({"article": html}), "article")
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        # Display sanitization must not change the link's archive identity.
        assert app._link_targets()[0][1] == href
        assert app._href_title(href) == "other"
        assert app._href_title("#part%1B%07%C2%9B") == "§ part"
        strips = app.screen._compositor.render_strips()
        text = "\n".join(strip.text for strip in strips)
        assert "ARTICLE_MARKER" in text
        assert not any(control in text for control in ("\x1b", "\x07", "\x9b"))
        output = app.screen._compositor.render_full_update().render_segments(app.console)
        assert "\x1b]0;ARTICLE_MARKER" not in output


async def test_metadata_and_search_controls_are_sanitized_without_changing_paths():
    path = "good\x1bpath"
    name = "Archive\x1b]0;NAME_MARKER\x1b\\\x07"
    title = "Result\x1b[31m\x9bTITLE_MARKER"
    app = MemoryApp(MemoryZim({path: GOOD}, name=name, titles={path: title}), None)
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause()
        await pilot.press(*"good")
        await pilot.pause(0.25)
        option = app.query_one("#search-results", OptionList).get_option_at_index(0)
        assert option.id == path
        assert option.prompt.plain == "Result[31mTITLE_MARKER"
        assert "NAME_MARKER" in drawn(app) and "TITLE_MARKER" in drawn(app)
        assert not any(c in drawn(app) for c in ("\x1b", "\x07", "\x9b"))
        await pilot.press("enter")
        await pilot.pause()
        assert app.article.path == path


@pytest.mark.parametrize("bad", [
    "<div>" * 1100 + "Deep" + "</div>" * 1100,
    OSError("Unreadable [/bold]\x1b[31m"),
    RuntimeError("Corrupt archive"),
])
async def test_failed_article_preserves_reading_state_and_can_recover(bad):
    zim = MemoryZim({"good": GOOD, "bad": bad, "other": OTHER})
    app = MemoryApp(zim, "good")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("j", "f")
        article, pages, page_i = app.article, app.pages, app.page_i
        history, hist_i = deepcopy(app.history), app.hist_i
        hints, breaks = deepcopy(app.hints), set(app.breaks)
        assert not app.open("bad")
        await pilot.pause()
        assert app.article is article and app.pages is pages
        assert app.page_i == page_i
        assert app.history == history and app.hist_i == hist_i
        assert app.hints == hints and app.breaks == breaks
        notification = list(app._notifications)[-1]
        assert notification.severity == "error" and not notification.markup
        assert "\x1b" not in notification.message
        assert app.open("other")
        assert app.article.path == "other"
        app.action_back()
        assert app.article.path == "good"


async def test_pagination_failure_does_not_commit_a_new_article(monkeypatch):
    app = MemoryApp(MemoryZim(), "good")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        article, pages = app.article, app.pages
        history = deepcopy(app.history)

        def fail(*args, **kwargs):
            raise ValueError("Cannot paginate this article")

        with monkeypatch.context() as patch:
            patch.setattr(Paginator, "paginate", fail)
            assert not app.open("other")
        assert app.article is article and app.pages is pages
        assert app.history == history
        assert app.open("other")


async def test_failed_history_navigation_and_reload_preserve_state():
    zim = MemoryZim()
    app = MemoryApp(zim, "good")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.open("other")
        history = deepcopy(app.history)
        zim.documents["good"] = OSError("no longer readable")
        app.action_back()
        assert app.hist_i == 1 and app.article.path == "other"
        assert app.history == history
        zim.documents["good"] = GOOD
        app.action_back()
        zim.documents["other"] = OSError("no longer readable")
        app.action_forward()
        assert app.hist_i == 0 and app.article.path == "good"
        article, pages = app.article, app.pages
        zim.documents["good"] = OSError("no longer readable")
        app.action_toggle_refs()
        app.action_toggle_links()
        assert not app.show_refs and not app.plain_links
        assert app.article is article and app.pages is pages


async def test_failed_initial_article_leaves_search_available():
    app = MemoryApp(MemoryZim({"bad": OSError("unreadable"), "good": GOOD}), "bad")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.article is None and app.hist_i == -1
        assert app.query_one(SearchBar).display
        assert app.focused is app.query_one("#search-input", Input)
        await pilot.press(*"good", "enter")
        await pilot.pause()
        assert app.article.path == "good"
        assert not app.query_one(SearchBar).display
