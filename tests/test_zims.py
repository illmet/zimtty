"""Tests against full English Wikipedia (skipped when not installed)."""
import re

import pytest

from zimtty.paginate import Layout, Paginator
from zimtty.zimdoc import ArticleParser, Zim

from .conftest import sampled_article_paths

# Literal values such as "<vacant>" are valid article text. Recognize leaked
# structural HTML here rather than treating every angle-bracket word as a tag.
JUNK = re.compile(
    r"mw-parser-output|/\*|display:|\.mw-|class=|"
    r"</?(?:div|span|table|tbody|thead|tr|td|th|style|script)\b[^>]*>"
)


def parse(zim: Zim, title: str, pal):
    r = zim.resolve(title)
    assert r, f"{title} not in ZIM"
    return ArticleParser(pal).parse(r[0], zim.html(r[0]))


# ---------------------------------------------------------------- robustness sweep

def test_sampled_articles_paginate_cleanly(big_zim, pal):
    """Seeded articles at tiny..wide sizes: pages never overflow, no text lost."""
    for p in sampled_article_paths(big_zim, 60, seed=7):
        a = ArticleParser(pal).parse(p, big_zim.html(p))
        for W, H, info in ((20, 10, 0), (88, 30, 0), (140, 45, 38)):
            lay = Layout(H, min(88, W), min(88, W - info - 7) if info else min(88, W), info)
            pages = Paginator(pal).paginate(a, lay, set())
            for pg in pages:
                assert len(pg.lines) <= H, (a.title, W, H)
                assert all(ln.cell_len <= pg.width for ln in pg.lines), (a.title, W, H)
            if W == 20:
                got = re.sub(r"\W", "", "".join(ln.plain for pg in pages for ln in pg.lines))
                want = re.sub(r"\W", "", "".join(b.text.plain for b in a.blocks if b.kind == "para"))
                assert len(got) >= len(want), a.title


# ---------------------------------------------------------------- known articles

def test_max_weber_structure(big_zim, pal):
    a = parse(big_zim, "Max Weber", pal)
    titles = [e.title for e in a.toc]
    assert titles[0] == "Biography" and "Verstehen" in titles
    assert a.infobox[0].kind == "title"
    assert {"Born", "Died"} <= {r.label.plain for r in a.infobox if r.kind == "pair"}


def test_lead_hatnote_goes_to_notes(big_zim, pal):
    a = parse(big_zim, "Hunger in the United Kingdom", pal)
    assert a.notes and "redirects here" in a.notes[0].plain
    assert any(b.anchor == "__notes__" for b in a.blocks)


@pytest.mark.parametrize("title,must,mustnot", [
    ("Germany", ["Legislature", "  President", "Capital and largest city"], []),
    ("World War II", ["Date", "Location", "Result"], ["Clockwise", "From top to bottom"]),
    ("Martin Heidegger", ["Born", "Education"], []),
    ("Normal distribution", ["Notation", "Parameters"], ["\\mathcal", "\\sigma"]),
])
def test_infoboxes_big(big_zim, pal, title, must, mustnot):
    a = parse(big_zim, title, pal)
    labels = [r.label.plain for r in a.infobox if r.kind == "pair"]
    lines = Paginator(pal).render_info(a.infobox, 38)
    text = "\n".join(ln.plain for ln in lines)
    for m in must:
        assert m in labels, (title, m, labels[:12])
    for m in mustnot:
        assert m not in text, (title, m)
    assert not JUNK.search(text)
    assert max(ln.cell_len for ln in lines) <= 38


def test_infobox_sweep_big(big_zim, pal):
    """Seeded infoboxes: no CSS/HTML leaking, overflow, or missing titles."""
    seen = 0
    for p in sampled_article_paths(big_zim, 200, seed=11):
        a = ArticleParser(pal).parse(p, big_zim.html(p))
        if not a.infobox:
            continue
        seen += 1
        lines = Paginator(pal).render_info(a.infobox, 38)
        assert a.infobox[0].kind == "title", a.title
        assert all(ln.cell_len <= 38 for ln in lines), a.title
        bad = [ln.plain for ln in lines if JUNK.search(ln.plain)]
        assert not bad, (a.title, bad[:2])
    assert seen > 50


# ---------------------------------------------------------------- search / redirects

@pytest.mark.parametrize("q", ["Germany", "Oxygen", "World War II"])
def test_exact_title_first(big_zim, q):
    assert big_zim.suggest(q, 5)[0][1] == q


def test_search_case_insensitive(big_zim):
    assert big_zim.suggest("max weber", 1)[0][1] == "Max Weber"


def test_soft_redirect(big_zim):
    assert big_zim.resolve("Cockney accent")[0] == "Cockney"
