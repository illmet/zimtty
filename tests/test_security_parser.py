"""Untrusted archive text cannot emit terminal controls or exhaust recursion."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selectolax.parser import HTMLParser

from zimtty.safety import safe_text
from zimtty.zimdoc import (
    MAX_ARTICLE_BYTES,
    MAX_DOM_DEPTH,
    MAX_TEX_DEPTH,
    ArticleError,
    ArticleParser,
    InlineBuilder,
    Palette,
    Zim,
    tex_to_text,
)


def test_safe_text_removes_terminal_controls_and_preserves_unicode():
    controls = "".join(chr(c) for c in (*range(32), *range(127, 160)))
    ordinary = "café 漢字 می‌خواهم 👨‍👩‍👧‍👦 \t\n\r"
    assert safe_text(ordinary + controls) == ordinary + "\t\n\r"
    assert safe_text("\x1b]52;c;Zm9v\x07\x9b31m") == "]52;c;Zm9v31m"


@pytest.mark.parametrize("payload", ["\x1b]52;c;Zm9v\x07", "&#27;]52;c;Zm9v&#7;"])
def test_all_article_display_fields_strip_decoded_controls(payload):
    html = f"""<html><body><h1>{payload}</h1>
    <div class="hatnote">{payload}</div>
    <table class="infobox"><caption>{payload}</caption><tr>
      <th>{payload}</th><td>{payload}</td></tr></table>
    <p>{payload}<sup class="reference">{payload}</sup>
      <span class="mwe-math-element"><img alt="{payload}"></span></p>
    <h2 id="section">{payload}</h2><p>Body.</p>
    <table><caption>{payload}</caption><tr><td>Cell</td></tr></table>
    </body></html>"""
    article = ArticleParser(Palette(), show_refs=True).parse("article", html)
    displays = [article.title]
    displays.extend(block.text.plain for block in article.blocks)
    displays.extend(note.plain for note in article.notes)
    displays.extend(entry.title for entry in article.toc)
    displays.extend(
        value.plain for row in article.infobox for value in (row.label, row.value)
        if value is not None
    )
    assert article.title == "]52;c;Zm9v"
    assert article.infobox and article.notes and article.toc
    tables = [block.table for block in article.blocks if block.table is not None]
    assert tables
    displays.extend(cell.text.plain for table in tables for row in table.rows for cell in row)
    displays.extend(table.caption.plain for table in tables)
    assert all(value == safe_text(value) for value in displays)
    assert not any("&#27;" in value for value in displays)


def test_title_fallbacks_are_safe_but_article_path_is_unchanged():
    path = "path\x1bwith\x9bcontrols"
    parser = ArticleParser(Palette())
    article = parser.parse(path, "<p>Body.</p>")
    assert article.path == path
    assert article.title == "pathwithcontrols"
    assert article.blocks[0].text.plain == "pathwithcontrols"
    article = parser.parse(path, "<title>Title&#27;with&#7;controls</title><p>Body.</p>")
    assert article.title == "Titlewithcontrols"


def test_inline_preserves_joiners_and_link_identity():
    href = "folder/Target\x1b#section"
    label = "می‌خواهم 👨‍👩‍👧‍👦"
    root = HTMLParser(f'<p><a href="{href}">{label}&#27;</a></p>').css_first("p")
    text = InlineBuilder(Palette(), show_refs=False).build(root)
    assert text.plain == label
    assert {span.style.meta["href"] for span in text.spans} == {href}
    article = ArticleParser(Palette()).parse(
        "test", '<h2 id="part\x1b">Heading</h2><p>Body.</p>'
    )
    assert article.toc[0].anchor == "part\x1b"


def test_parser_rejects_oversized_direct_input_before_html_parsing(monkeypatch):
    import zimtty.zimdoc as zimdoc

    parse_html = Mock(side_effect=AssertionError("oversized HTML must not be parsed"))
    monkeypatch.setattr(zimdoc, "HTMLParser", parse_html)
    parser = ArticleParser(Palette())
    with pytest.raises(ArticleError, match="maximum size"):
        parser.parse("large", "x" * (MAX_ARTICLE_BYTES + 1))
    # Direct strings are bounded by UTF-8 bytes, not merely character count.
    monkeypatch.setattr(zimdoc, "MAX_ARTICLE_BYTES", 64)
    with pytest.raises(ArticleError, match="maximum size"):
        parser.parse("multibyte", "é" * 33)
    parse_html.assert_not_called()


def test_archive_size_is_checked_before_accessing_content():
    class OversizedItem:
        size = MAX_ARTICLE_BYTES + 1

        @property
        def content(self):
            raise AssertionError("oversized article content must not be loaded")

    zim = Zim.__new__(Zim)
    zim.z = SimpleNamespace(
        get_entry_by_path=lambda path: SimpleNamespace(get_item=OversizedItem)
    )
    with pytest.raises(ArticleError, match="maximum size"):
        zim.html("large")


def test_archive_html_keeps_content_and_lookup_identity():
    path = "raw\x1bpath"
    html = b"<p>safe after parsing: &#27;</p>"
    lookup = Mock(return_value=SimpleNamespace(
        get_item=lambda: SimpleNamespace(size=len(html), content=html)
    ))
    zim = Zim.__new__(Zim)
    zim.z = SimpleNamespace(get_entry_by_path=lookup)
    assert zim.html(path) == html.decode()
    lookup.assert_called_once_with(path)


@pytest.mark.parametrize("tag", ["div", "b"])
def test_dom_depth_limit_is_enforced_before_recursive_walks(tag):
    parser = ArticleParser(Palette())
    # html, body and p occupy three levels; the remaining depth is usable.
    allowed = MAX_DOM_DEPTH - 3
    html = "<html><body><p>" + f"<{tag}>" * allowed + "Text"
    html += f"</{tag}>" * allowed + "</p></body></html>"
    # Divs cannot occur within p in HTML, so use valid block nesting separately.
    if tag == "div":
        html = "<html><body>" + "<div>" * allowed + "<p>Text</p>"
        html += "</div>" * allowed + "</body></html>"
    assert any(block.text.plain == "Text" for block in parser.parse("ok", html).blocks)
    nested = f"<{tag}>" * (MAX_DOM_DEPTH + 1) + "Text" + f"</{tag}>" * (MAX_DOM_DEPTH + 1)
    with pytest.raises(ArticleError, match="maximum nesting depth"):
        parser.parse("deep", nested)


@pytest.mark.parametrize("formula", [
    "{" * (MAX_TEX_DEPTH + 1) + "x" + "}" * (MAX_TEX_DEPTH + 1),
    r"\sqrt" * 1000 + "x",
    "{" * 1000 + "x" + "}" * 1000,
])
def test_deep_tex_falls_back_to_safe_raw_text(formula):
    assert tex_to_text(formula + "\x1b") == formula
    html = f'<p><span class="mwe-math-element"><img alt="{formula}&#27;"></span></p>'
    article = ArticleParser(Palette()).parse("formula", html)
    assert article.blocks[-1].text.plain == formula


def test_tex_at_depth_limit_still_converts():
    assert tex_to_text("{" * MAX_TEX_DEPTH + "x" + "}" * MAX_TEX_DEPTH) == "x"
    assert tex_to_text(r"\frac{1}{\sqrt{x^2}}") == "1/(√x²)"


def test_archive_metadata_titles_and_fallbacks_are_display_safe():
    path = "archive\x1b.zim"
    lookup = Mock(return_value=SimpleNamespace(title="Entry\x1btitle\x9b"))
    zim = Zim.__new__(Zim)
    zim.path = path
    zim.z = SimpleNamespace(
        get_entry_by_path=lookup,
        get_metadata=lambda key: b"Archive\x1btitle\x07",
    )
    assert zim.name == "Archivetitle"
    assert zim.path == path
    assert zim.title_of(path) == "Entrytitle"
    lookup.assert_called_once_with(path)
    lookup.side_effect = KeyError("missing")
    zim.z.get_metadata = Mock(side_effect=KeyError("missing"))
    assert zim.name == "archive.zim"
    assert zim.title_of(path) == "archive.zim"
