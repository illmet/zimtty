"""Pure unit tests: no ZIM files needed."""
from rich.text import Text
from selectolax.parser import HTMLParser

from zimtty.paginate import Layout, Paginator, wrap
from zimtty.zimdoc import ArticleParser, Block, InlineBuilder, Palette, tex_to_text


# ---------------------------------------------------------------- TeX

def test_tex_basic():
    assert tex_to_text(r"E=mc^{2}") == "E = mc²"
    assert tex_to_text(r"x^{2}+y_{i}") == "x²+yᵢ"
    assert tex_to_text(r"\mu \in \mathbb {R}") == "μ ∈ ℝ"


def test_tex_nested_frac_sqrt():
    assert tex_to_text(r"{\mathcal {N}}(\mu ,\sigma ^{2})") == "N(μ,σ²)"
    assert tex_to_text(r"{\frac {1}{\sqrt {2\pi \sigma ^{2}}}}") == "1/(√(2π σ²))"
    assert tex_to_text(r"\sqrt[3]{x}") == "3√x"


def test_tex_passthrough_and_malformed():
    assert tex_to_text("plain words") == "plain words"
    assert isinstance(tex_to_text(r"\frac{1"), str)  # never raises


# ---------------------------------------------------------------- inline HTML

def _inline(html: str, **kw) -> Text:
    node = HTMLParser(f"<div>{html}</div>").css_first("div")
    return InlineBuilder(Palette(), **kw).build(node)


def test_inline_links_refs_and_whitespace():
    t = _inline('A <a href="Being_and_Time">book</a><sup class="reference">[1]</sup> by  <b>him</b>.',
                show_refs=False)
    assert t.plain == "A book by him."
    hrefs = {sp.style.meta.get("href") for sp in t.spans if getattr(sp.style, "meta", None)}
    assert "Being_and_Time" in hrefs


def test_inline_links_off_is_plain():
    t = _inline('A <a href="X">book</a>.', show_refs=False, links=False)
    assert t.plain == "A book."
    assert not any(getattr(sp.style, "meta", None) for sp in t.spans)


def test_inline_external_links_not_followable():
    t = _inline('<a href="https://example.com">site</a>', show_refs=False)
    assert not any(getattr(sp.style, "meta", None) for sp in t.spans)


def test_inline_strips_zero_width():
    assert _inline("a\u200bb\u00adc", show_refs=False).plain == "abc"


# ---------------------------------------------------------------- article structure

HTML = """<html><body><h1>Thing</h1>
<section data-mw-section-id="0">
  <div class="hatnote">For other uses, see Thing (disambiguation).</div>
  <table class="infobox"><tbody>
    <tr><th class="infobox-above">Thing</th></tr>
    <tr><th class="infobox-label">Born</th><td class="infobox-data">1900</td></tr>
    <tr><th class="infobox-header">Signature</th></tr>
  </tbody></table>
  <p>Lead paragraph.</p>
</section>
<section data-mw-section-id="1"><div class="mw-heading mw-heading2"><h2 id="History">History</h2></div>
  <p>History text.</p>
  <section data-mw-section-id="2"><div class="mw-heading mw-heading3"><h3 id="Early">Early</h3></div><p>Early text.</p></section>
</section>
<section data-mw-section-id="3"><div class="mw-heading mw-heading2"><h2 id="References">References</h2></div><p>Ref.</p></section>
<section data-mw-section-id="4"><div class="mw-heading mw-heading2"><h2 id="Empty">Empty</h2></div></section>
</body></html>"""


def test_parse_structure():
    a = ArticleParser(Palette()).parse("Thing", HTML)
    kinds = [(b.kind, b.text.plain) for b in a.blocks]
    assert kinds[0] == ("title", "Thing")
    assert ("para", "Lead paragraph.") in kinds
    # lead hatnote moved out of the lead into "Additional notes", before back matter
    titles = [e.title for e in a.toc]
    assert titles == ["History", "Early", "Additional notes", "References"]
    assert "Empty" not in titles  # empty sections dropped
    assert a.notes and "disambiguation" in a.notes[0].plain


def test_infobox_rows_and_empty_header_dropped():
    a = ArticleParser(Palette()).parse("Thing", HTML)
    kinds = [(r.kind, (r.label.plain if r.label else r.value.plain)) for r in a.infobox]
    assert ("pair", "Born") in kinds
    assert not any(k == "header" for k, _ in kinds)  # "Signature" had nothing under it


def test_flat_old_style_html():
    """mwoffliner <=1.16 has no <section>: split at h2 instead."""
    flat = """<html><body><h1>Old</h1><div class="mw-parser-output">
      <p>Lead.</p><div class="mw-heading"><h2 id="A">A</h2></div><p>A text.</p>
      <div class="hatnote">Main article: X</div><p>More.</p></div></body></html>"""
    a = ArticleParser(Palette()).parse("Old", flat)
    assert [e.title for e in a.toc] == ["A"]
    assert not a.notes  # hatnote after a heading stays inline, not a lead note


# ---------------------------------------------------------------- pagination

def _article(n_paras=30, words=40):
    a = ArticleParser(Palette()).parse("Thing", HTML)
    para = " ".join(["word"] * words) + "."
    a.blocks[2:2] = [Block("para", Text(f"P{i} " + para)) for i in range(n_paras)]
    return a


def test_wrap_respects_cell_width_for_cjk():
    for ln in wrap(Text("漢字" * 30 + " end"), 20):
        assert ln.cell_len <= 20


def test_pagination_invariants():
    a = _article()
    for W, H, info in ((20, 10, 0), (60, 18, 0), (140, 45, 38)):
        lay = Layout(H, min(88, W), min(88, W - info - 7) if info else min(88, W), info)
        pages = Paginator(Palette()).paginate(a, lay, set())
        for pg in pages:
            assert len(pg.lines) <= H
            assert all(ln.cell_len <= pg.width for ln in pg.lines)


def test_paragraphs_not_split_when_they_fit():
    a = _article(n_paras=12, words=30)
    pages = Paginator(Palette()).paginate(a, Layout(20, 60, 60, 0), set())
    for pg in pages:
        text = "\n".join(ln.plain for ln in pg.lines)
        # each page starts on a paragraph/heading boundary: never mid-sentence
        first = pg.lines[0].plain if pg.lines else ""
        assert not first.startswith("word"), text[:80]


def test_h2_starts_a_page():
    a = _article()
    pages = Paginator(Palette()).paginate(a, Layout(30, 80, 80, 0), set())
    for pg in pages[1:]:
        kinds = [a.blocks[i].kind for i in sorted(pg.blocks)]
        if "h2" in kinds:
            assert a.blocks[pg.first_block].kind == "h2"


def test_no_text_lost_on_tiny_window():
    a = _article(n_paras=5, words=200)  # paragraphs bigger than a page: must be split
    pages = Paginator(Palette()).paginate(a, Layout(8, 20, 20, 0), set())
    got = " ".join(ln.plain for pg in pages for ln in pg.lines).split()
    for b in a.blocks:
        if b.kind == "para" and b.text.plain.startswith("P"):  # the big generated ones
            words = b.text.plain.split()
            start = got.index(words[0])  # "P0", "P1", ... are unique markers
            assert got[start:start + len(words)] == words, b.text.plain[:30]


def test_infobox_continues_and_text_goes_full_width():
    a = _article(n_paras=40)
    lay = Layout(12, 88, 45, 38)
    pages = Paginator(Palette()).paginate(a, lay, set())
    assert pages[0].info and pages[0].width == 45
    assert not pages[-1].info and pages[-1].width == 88
