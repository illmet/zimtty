"""Table layout regressions against the optional local full Wikipedia archive."""

import pytest

from zimtty.paginate import Layout, Paginator
from zimtty.zimdoc import Article, ArticleParser


@pytest.fixture
def berlin(big_zim, pal):
    resolved = big_zim.resolve("Berlin")
    assert resolved is not None
    return ArticleParser(pal).parse(resolved[0], big_zim.html(resolved[0]))


def table_pages(block, pal, width):
    document = Article("Berlin", "Berlin", [block], [], [])
    pages = Paginator(pal).paginate(document, Layout(24, width, width, 0), set())
    assert all(len(page.lines) <= 24 for page in pages)
    assert all(line.cell_len <= width for page in pages for line in page.lines)
    return pages


@pytest.mark.parametrize("width", [45, 88])
def test_berlin_population_keeps_grid_and_full_width_footer(berlin, pal, width):
    block = next(block for block in berlin.blocks if block.table is not None
                 and block.table.caption.plain.casefold() == "historical population")
    table = block.table
    assert table.simple
    footer_index = len(table.rows) - 1
    assert footer_index in table.full_width_rows
    pages = table_pages(block, pal, width)
    rendered = [line.plain for page in pages for line in page.lines]
    assert any("Year" in line and "Pop." in line and " │ " in line for line in rendered)
    assert not any(line.startswith(("Row ", "Cell ")) for line in rendered)
    renderer = Paginator(pal)
    footer = [line.plain for line in renderer._table_row(table, footer_index, width)]
    assert rendered[-len(footer):] == footer
    assert all(" │ " not in line for line in footer)
    assert len(pages) <= 3  # Previously six pages of one-cell-per-line records.


def test_berlin_transport_keeps_annual_ridership_number_whole(berlin, pal):
    block = next(block for block in berlin.blocks if block.table is not None
                 and any("Annual ridership" in cell.text.plain
                         for row in block.table.rows for cell in row))
    pages = table_pages(block, pal, 45)
    number = "431,000,000"
    assert any(number in cell.text.plain for row in block.table.rows for cell in row)
    assert any(number in line.plain and " │ " in line.plain
               for page in pages for line in page.lines)
    assert len(pages) == 2


def test_horned_grebe_wide_descriptions_keep_compact_record_layout(big_zim, pal):
    resolved = big_zim.resolve("Horned grebe")
    assert resolved is not None
    document = ArticleParser(pal).parse(resolved[0], big_zim.html(resolved[0]))
    block = next(block for block in document.blocks if block.table is not None
                 and any("Summer distribution" in cell.text.plain
                         for row in block.table.rows for cell in row))
    assert Paginator(pal)._columns(block.table, 45) is None
    assert len(table_pages(block, pal, 45)) == 2


def test_juliet_simms_recorded_covers_keep_compact_record_layout(big_zim, pal):
    resolved = big_zim.resolve("Juliet Simms")
    assert resolved is not None
    document = ArticleParser(pal).parse(resolved[0], big_zim.html(resolved[0]))
    block = next(block for block in document.blocks if block.table is not None
                 and block.table.caption.plain.casefold() == "recorded covers")
    assert Paginator(pal)._columns(block.table, 45) is None
    assert len(table_pages(block, pal, 45)) == 2
