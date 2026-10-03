"""Archive URLs are relative to the current document; navigation stays local."""
from types import SimpleNamespace

import pytest
from rich.style import Style
from rich.text import Text
from selectolax.parser import HTMLParser

from zimtty.app import ZimTTY
from zimtty.paginate import Page
from zimtty.zimdoc import InlineBuilder, Palette, Zim, archive_href


class Entry:
    def __init__(self, path, html="<p>Body.</p>", title=None, redirect=None, mimetype="text/html"):
        self.path = path
        self.title = title or path
        self.is_redirect = redirect is not None
        self.redirect = redirect
        self.content = html.encode()
        self.mimetype = mimetype

    def get_item(self):
        return SimpleNamespace(content=self.content, size=len(self.content), mimetype=self.mimetype)

    def get_redirect_entry(self):
        return self.redirect


class MemoryArchive:
    def __init__(self, entries):
        self.entries = {entry.path: entry for entry in entries}
        self.article_count = len(entries)

    def has_entry_by_path(self, path):
        return path in self.entries

    def get_entry_by_path(self, path):
        return self.entries[path]

    def has_entry_by_title(self, title):
        return any(entry.title == title for entry in self.entries.values())

    def get_entry_by_title(self, title):
        return next(entry for entry in self.entries.values() if entry.title == title)

    def get_metadata(self, key):
        return b"Test archive"


class MemoryZim(Zim):
    def __init__(self, entries):
        self.path = "memory.zim"
        self.z = MemoryArchive(entries)

    def suggest(self, query, n=5):
        return []


@pytest.mark.parametrize("base,href,html_base,expected", [
    ("docs/Start", "../Other#Section", None, ("Other", "Section")),
    ("docs/Start", "./Sub?view=print#A%20B", None, ("docs/Sub", "A%20B")),
    ("docs/Start", "/root/Page", None, ("root/Page", None)),
    ("docs/Start", "Name%23Hash%3FQuestion", None, ("docs/Name#Hash?Question", None)),
    ("docs/Start", "../%E6%BC%A2%E5%AD%97", None, ("漢字", None)),
    ("docs/Start", "#Part", None, ("docs/Start", "Part")),
    ("docs/Start", "#", None, ("docs/Start", "")),
    ("docs/Start", "?view=print", None, ("docs/Start", None)),
    ("docs/Start", "Page", "../manual/", ("manual/Page", None)),
    ("docs/Start", "Page", "https://example.com/wiki/", ("docs/Page", None)),
    ("docs/Name#Hash", "#Part", None, ("docs/Name#Hash", "Part")),
])
def test_archive_url_resolution(base, href, html_base, expected):
    assert archive_href(href, base, html_base) == expected


@pytest.mark.parametrize("href", ["https://example.com", "HTTP://example.com", "//example.com",
                                 "mailto:name@example.com", "file:///tmp/file", "javascript:alert(1)",
                                 "data:text/html,hello", "tel:123", "https://[invalid"])
def test_external_schemes_are_never_actionable(href):
    root = HTMLParser(f'<p><a href="{href}">Label</a></p>').css_first("p")
    text = InlineBuilder(Palette(), False).build(root)
    assert text.plain == "Label"
    assert not any(isinstance(span.style, Style) and span.style.meta for span in text.spans)
    assert archive_href(href, "docs/Page") is None


def test_fragment_links_and_inline_runs_preserve_styles_and_distinct_identities():
    root = HTMLParser('<p>A <a href="#part"><code>first</code> continuation</a> '
                      '<a href="#part">second</a></p>').css_first("p")
    text = InlineBuilder(Palette(), False).build_nodes(list(root.iter(include_text=True)))
    assert text.plain == "A first continuation second"
    links = [span.style.meta for span in text.spans if isinstance(span.style, Style) and span.style.meta]
    assert {meta["href"] for meta in links} == {"#part"}
    assert len({meta["link_id"] for meta in links}) == 2


def test_relative_links_do_not_fall_back_to_unrelated_titles():
    zim = MemoryZim([Entry("different", title="Missing"), Entry("docs/Page"), Entry("Page")])
    assert zim.resolve("Missing") == ("different", None)
    assert zim.resolve("Missing", base_path="docs/Start") is None
    assert zim.resolve("Page", base_path="docs/Start") == ("docs/Page", None)
    assert zim.resolve("Page") == ("Page", None)


def test_canonical_names_with_url_punctuation_and_encoded_href():
    zim = MemoryZim([Entry("docs/Name#Hash?Query%20")])
    assert zim.resolve("docs/Name#Hash?Query%20") == ("docs/Name#Hash?Query%20", None)
    assert zim.resolve("Name%23Hash%3FQuery%2520#Part", base_path="docs/Start") == (
        "docs/Name#Hash?Query%20", "Part"
    )


def test_complete_titles_take_precedence_over_fragment_splitting():
    zim = MemoryZim([Entry("C"), Entry("csharp", title="C#")])
    assert zim.resolve("C#") == ("csharp", None)
    assert zim.resolve("C#History") == ("C", "History")


def test_redirects_are_relative_and_keep_fragments():
    target = Entry("target/Actual", mimetype="text/html; charset=UTF-8")
    soft = Entry("docs/Soft", '<meta http-equiv="REFRESH" content="0; url=\'../target/Actual#Details\'">')
    hard = Entry("docs/Alias", redirect=soft)
    zim = MemoryZim([target, soft, hard])
    assert zim.resolve("Alias#Original", base_path="docs/Start") == ("target/Actual", "Details")
    soft.content = b'<meta http-equiv="refresh" content="0; url=../target/Actual">'
    assert zim.resolve("docs/Alias#Original") == ("target/Actual", "Original")
    soft.content = b'<meta http-equiv="refresh" content="0; url=../target/Actual#">'
    assert zim.resolve("docs/Alias#Original") == ("target/Actual", "")


def test_redirect_cycles_and_external_redirects_stop():
    a = Entry("A", '<meta http-equiv="refresh" content="0; url=B">')
    b = Entry("B", '<meta http-equiv="refresh" content="0; url=A">')
    external = Entry("External", '<meta http-equiv="refresh" content="0; url=https://example.com">')
    zim = MemoryZim([a, b, external])
    assert zim.resolve("A") is None
    assert zim.resolve("External") is None


def test_table_links_group_by_identity_across_nonzero_columns():
    first = Style(meta={"href": "Target", "link_id": 10})
    second = Style(meta={"href": "Target", "link_id": 11})
    lines = [Text("  ") + Text("first", first) + Text(" | ") + Text("other", second),
             Text("  ") + Text("continues", first) + Text(" | ") + Text("again", second)]
    app = ZimTTY(MemoryZim([]), None)
    app.pages = [Page(lines, [], False, 0, 60)]
    targets = app._link_targets()
    assert len(targets) == 2
    assert [len(runs) for runs, href in targets] == [2, 2]
    assert [href for runs, href in targets] == ["Target", "Target"]


def test_link_hints_preserve_table_column_width_for_wide_text():
    app = ZimTTY(MemoryZim([]), None)
    line = Text("漢字 | 本文")
    app.hints = {"a": (0, 0, 2, "first"), "s": (0, 5, 7, "second")}
    result = app._hinted([line])[0]
    assert result.plain == "a 字 | s 文"
    assert result.cell_len == line.cell_len
