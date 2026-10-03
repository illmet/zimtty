"""Lossless preformatted blocks, complete headings, and readable table pages."""
from collections import defaultdict

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from zimtty.paginate import Layout, Paginator
from zimtty.zimdoc import Article, Block, InfoRow, Palette, TableCell, TableData


def article(*blocks, infobox=()):
    return Article("test", "Test", list(blocks), list(infobox), [])


def paginate(document, width=40, height=8, narrow=None, info_width=0):
    pages = Paginator(Palette()).paginate(
        document, Layout(height, width, narrow or width, info_width), set()
    )
    for page in pages:
        assert len(page.lines) <= height
        assert all(line.cell_len <= page.width for line in page.lines)
    return pages


def lines(pages):
    return [line for page in pages for line in page.lines]


def nonspace(text):
    return "".join(text.split())


def tagged_cells(pages):
    """Read source-cell identities from the final styled screen characters."""
    found = defaultdict(str)
    locations = defaultdict(set)
    console = Console()
    for page_index, page in enumerate(pages):
        for line in page.lines:
            for offset, character in enumerate(line.plain):
                style = line.get_style_at_offset(console, offset)
                key = style.meta.get("cell")
                if key is not None:
                    found[key] += character
                    locations[key].add(page_index)
                    assert style.meta["href"] == f"cell/{key}"
                    assert style.meta["link_id"] == f"link/{key}"
    return found, locations


def table_block(values, *, simple=True, caption="", header=True, full_width_rows=frozenset()):
    rows = []
    expected = {}
    header_row = next((i for i in range(len(values)) if i not in full_width_rows), None)
    for row, values_in_row in enumerate(values):
        cells = []
        for column, value in enumerate(values_in_row):
            key = f"{row}/{column}"
            text = Text(value, Style(meta={
                "cell": key, "href": f"cell/{key}", "link_id": f"link/{key}",
            }))
            cells.append(TableCell(text, header=header and row == header_row))
            expected[key] = value
        rows.append(cells)
    data = TableData(rows, Text(caption), simple=simple, full_width_rows=full_width_rows)
    flat = Text("\n").join(cell.text for row in rows for cell in row)
    return Block("table", flat, table=data), expected


def test_pre_preserves_blank_lines_indentation_trailing_spaces_and_tabs():
    source = "    first  \n\n\tsecond  \n漢\tend  \nlast\n\n"
    pages = paginate(article(Block("pre", Text(source))), width=32, height=4)
    assert "\n".join(line.plain for line in lines(pages)) == (
        "    first  \n\n        second  \n漢      end  \nlast\n\n"
    )


@pytest.mark.parametrize("width,height", [(8, 4), (20, 6), (40, 8)])
def test_long_pre_line_preserves_every_character_and_link_span(width, height):
    source = "  " + "漢字👨‍👩‍👧‍👦abc  " * 35 + "  END  "
    text = Text(source, Style(meta={"href": "Code", "link_id": "code"}))
    pages = paginate(article(Block("pre", text)), width=width, height=height)
    assert "".join(line.plain for line in lines(pages)) == source
    console = Console()
    for line in lines(pages):
        for offset in range(len(line.plain)):
            assert line.get_style_at_offset(console, offset).meta["href"] == "Code"


def test_pre_reflows_remaining_source_when_infobox_ends():
    source = "0123456789" * 18
    info = [InfoRow("full", None, Text("Info"))]
    pages = paginate(article(Block("pre", Text(source)), infobox=info),
                     width=30, height=4, narrow=8, info_width=10)
    assert pages[0].width == 8 and pages[1].width == 30
    assert [line.plain for line in pages[0].lines] == [source[i:i + 8] for i in range(0, 32, 8)]
    assert pages[1].lines[0].plain == source[32:62]
    assert "".join(line.plain for line in lines(pages)) == source


def test_pre_context_indentation_survives_page_width_changes():
    source = "  " + "0123456789" * 16 + "  "
    block = Block("pre", Text(source), indent=6)
    info = [InfoRow("full", None, Text("Info"))]
    pages = paginate(article(block, infobox=info), width=30, height=4,
                     narrow=12, info_width=10)
    restored = []
    for page in pages:
        pad = min(6, page.width // 4, page.width - 4)
        for line in page.lines:
            assert line.plain.startswith(" " * pad)
            restored.append(line.plain[pad:])
    assert "".join(restored) == source
    rendered = Paginator(Palette()).render_block(block, 12)
    assert all(line.plain.startswith("   ") and line.cell_len <= 12 for line in rendered)
    assert "".join(line.plain[3:] for line in rendered) == source


@pytest.mark.parametrize("kind", ["title", "h2", "h3", "h4"])
def test_long_headings_keep_every_word_across_pages(kind):
    heading = " ".join(f"word{i}" for i in range(80))
    pages = paginate(article(Block(kind, Text(heading))), width=20, height=4)
    rendered = "".join(line.plain for line in lines(pages)
                       if set(line.plain) not in ({"─"}, {"━"}))
    assert nonspace(rendered) == nonspace(heading)
    assert len(pages) > 1


def test_heading_keeps_paragraph_start_when_whole_paragraph_cannot_fit():
    document = article(Block("h2", Text("Heading")), Block("para", Text("word " * 30)))
    pages = paginate(document, width=20, height=10)
    assert pages[0].blocks == {0, 1}
    assert sum(line.plain.count("word") for line in lines(pages)) == 30


def test_heading_and_short_following_block_move_together():
    document = article(Block("para", Text("Intro. " * 8)),
                       Block("h3", Text("Heading")),
                       Block("para", Text("Body. " * 8)))
    pages = paginate(document, width=20, height=6)
    heading_page = next(page for page in pages if 1 in page.blocks)
    assert 2 in heading_page.blocks
    assert sum(2 in page.blocks for page in pages) == 1


def test_heading_keeps_start_of_large_first_table_row():
    block, expected = table_block([["Value " * 15]], header=False)
    pages = paginate(article(Block("h2", Text("Heading")), block), width=20, height=8)
    assert pages[0].blocks == {0, 1}
    found, _ = tagged_cells(pages)
    assert nonspace(found["0/0"]) == nonspace(expected["0/0"])


@pytest.mark.parametrize("width", [20, 40, 88])
def test_simple_tables_preserve_cells_links_and_use_narrow_record_fallback(width):
    block, expected = table_block([
        ["Name", "Description"],
        ["Alpha", "First value " * 8],
        ["漢字", "Joined 👨‍👩‍👧‍👦 example " * 5],
        ["Omega", "Final value"],
    ], caption="Table caption")
    pages = paginate(article(block), width=width, height=8)
    found, _ = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("0/"):
            assert nonspace(found[key]) == nonspace(value)
    rendered = "\n".join(line.plain for line in lines(pages))
    assert "Table caption" in rendered
    if width == 20:
        assert " │ " not in rendered and "Name: Alpha" in rendered
    else:
        assert " │ " in rendered


def test_table_rows_stay_together_and_headers_repeat():
    block, expected = table_block([["Name", "Value"]] + [
        [f"Entry{i}", f"Detail{i} " * 5] for i in range(12)
    ])
    pages = paginate(article(block), width=40, height=8)
    found, locations = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("0/"):
            assert nonspace(found[key]) == nonspace(value)
            assert len(locations[key]) == 1
    assert len(locations["0/0"]) == len(pages)


def test_table_header_stays_with_first_data_row():
    block, _ = table_block([
        ["Flag", "Meaning"], ["--help", "Show available commands."],
        ["--offline", "Use local files."],
    ])
    document = article(Block("h2", Text("Commands")), Block("para", Text("Run:")),
                       Block("pre", Text("if ready:\n    start()\n\n    verify()")), block)
    pages = paginate(document, width=50, height=12)
    found, locations = tagged_cells(pages)
    assert locations["0/0"] == locations["1/0"]
    assert nonspace(found["1/0"]) == "--help"


def test_oversized_table_row_survives_page_and_infobox_width_changes():
    block, expected = table_block([
        ["Key", "Value"], ["Oversized", "meaningful-value " * 100],
        ["Last", "Final sentinel"],
    ])
    info = [InfoRow("full", None, Text("Info"))]
    pages = paginate(article(block, infobox=info), width=80, narrow=40,
                     height=6, info_width=10)
    found, _ = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("0/"):
            assert nonspace(found[key]) == nonspace(value)
    assert pages[0].width == 40 and pages[-1].width == 80
    assert len(pages) > 2


def test_complex_table_fallback_preserves_source_order_without_header_inference():
    block, expected = table_block([
        ["Merged heading"], ["First", "Second", "Third"], ["Last"],
    ], simple=False)
    pages = paginate(article(block), width=40, height=5)
    found, _ = tagged_cells(pages)
    for key, value in expected.items():
        assert nonspace(found[key]) == nonspace(value)
    rendered = "\n".join(line.plain for line in lines(pages))
    assert "Merged heading:" not in rendered
    offsets = [rendered.index(value) for value in expected.values()]
    assert offsets == sorted(offsets)


def test_many_columns_use_bounded_source_order_fallback():
    block, expected = table_block([[f"value-{i}" for i in range(50)]], header=False)
    pages = paginate(article(block), width=88, height=8)
    found, _ = tagged_cells(pages)
    assert all(nonspace(found[key]) == nonspace(value) for key, value in expected.items())
    assert all(" │ " not in line.plain for line in lines(pages))


@pytest.mark.parametrize("width,simple", [(20, True), (40, True), (40, False)])
def test_preformatted_table_cells_preserve_spaces_tabs_and_blank_lines(width, simple):
    source = "  first  \n\n\tsecond  \n  " + "code " * 12 + " END  \n"
    text = Text(source)
    text.stylize(Style(meta={"cell": "code", "href": "cell/code", "link_id": "link/code"}))
    table = TableData([[TableCell(text, preformatted=True)]], simple=simple)
    pages = paginate(article(Block("table", text, table=table)), width=width, height=5)
    found, _ = tagged_cells(pages)
    expanded = text.copy()
    expanded.expand_tabs(8)
    assert found["code"] == expanded.plain.replace("\n", "")
    # Explicit empty logical lines remain visible, including the final newline.
    rendered = [line.plain for line in lines(pages)]
    assert sum(not line.strip() for line in rendered) >= 2


@pytest.mark.parametrize("sidebar", [False, True])
def test_infobox_remains_readable_with_or_without_sidebar(sidebar):
    value = Text("meaningful " * 20)
    value.stylize(Style(meta={"cell": "info", "href": "cell/info", "link_id": "link/info"}))
    info = [InfoRow("title", None, Text("Details")), InfoRow("pair", Text("Label"), value)]
    block = Block("infobox", Text("Details Label ") + value)
    pages = paginate(article(Block("para", Text("Before")), block,
                             Block("para", Text("After")), infobox=info),
                     width=40 if sidebar else 20, narrow=20, height=6,
                     info_width=15 if sidebar else 0)
    if sidebar:
        assert all("meaningful" not in line.plain for line in lines(pages))
        info_pages = [type(page)(page.info, [], False, 0, 15) for page in pages]
        found, _ = tagged_cells(info_pages)
    else:
        assert all(not page.info for page in pages)
        found, _ = tagged_cells(pages)
        assert 1 in set.union(*(page.blocks for page in pages))
    assert nonspace(found["info"]) == nonspace(value.plain)


def test_late_infobox_anchor_marks_actual_sidebar_pages():
    info = [InfoRow("full", None, Text("Sidebar content"))]
    intro = [Block("para", Text("Introductory prose. " * 30)) for _ in range(8)]
    block = Block("infobox", Text("Sidebar content"), anchors=("box",))
    pages = paginate(article(*intro, block, infobox=info), width=88, narrow=45,
                     height=10, info_width=38)
    assert len(pages) > 2
    assert [i for i, page in enumerate(pages) if len(intro) in page.blocks] == [
        i for i, page in enumerate(pages) if page.info
    ] == [0]


def test_column_allocation_keeps_existing_comfortable_layouts():
    block, _ = table_block([["Name", "Description"], ["Alpha", "First value " * 8]])
    renderer = Paginator(Palette())
    assert renderer._columns(block.table, 40) == [5, 32]
    assert renderer._columns(block.table, 88) == [5, 80]


def test_column_allocation_recovers_narrow_grid_without_truncating_values():
    block, expected = table_block([
        ["Club(s)", "Sport(s)", "Founded", "League(s)", "Venue(s)"],
        ["Union Berlin", "Football", "1966", "Bundesliga", "Alte Försterei"],
    ])
    pages = paginate(article(block), width=45, height=8)
    assert any(" │ " in line.plain for line in lines(pages))
    found, _ = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("0/"):
            assert nonspace(found[key]) == nonspace(value)


def test_recovered_grid_stays_stacked_when_narrow_columns_add_height():
    block, expected = table_block([
        [f"Category {column}" for column in range(6)],
        ["Long descriptive information " * 8 for _ in range(6)],
    ])
    assert Paginator(Palette())._columns(block.table, 45) is None
    pages = paginate(article(block), width=45, height=8)
    found, _ = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("0/"):
            assert nonspace(found[key]) == nonspace(value)


def test_large_table_recovery_keeps_prior_fallback_when_measurement_is_bounded():
    block, _ = table_block([["Descriptive value" for _ in range(6)] for _ in range(140)])
    renderer = Paginator(Palette())
    assert renderer._columns(block.table, 45) is None
    assert renderer._columns(block.table, 88) is not None


def test_numeric_tokens_borrow_room_from_other_columns():
    block, _ = table_block([
        ["System", "Stations / Lines / Net length", "Annual ridership", "Operator / Notes"],
        ["S-Bahn", "166 / 16 / 331 km (206 mi)", "431,000,000 (2016)",
         "DB / Mainly overground rapid transit"],
    ])
    renderer = Paginator(Palette())
    widths = renderer._columns(block.table, 45)
    assert widths is not None and widths[2] >= len("431,000,000")
    assert sum(widths) + 9 <= 45
    rendered = renderer._table_row(block.table, 1, 45)
    assert any("431,000,000" in line.plain for line in rendered)


@pytest.mark.parametrize("width", [20, 45, 88])
def test_full_width_bands_keep_order_styles_and_actual_grid_headers(width):
    values = [["Population overview"], ["Year", "Population", "Change"],
              *[[str(1900 + i), f"{1000 + i:,}", "+1%"] for i in range(12)],
              ["Boundary changes affect these population totals."]]
    block, expected = table_block(values, full_width_rows=frozenset({0, len(values) - 1}))
    pages = paginate(article(block), width=width, height=8)
    found, locations = tagged_cells(pages)
    for key, value in expected.items():
        if not key.startswith("1/"):
            assert nonspace(found[key]) == nonspace(value)
    rendered = "\n".join(line.plain for line in lines(pages))
    assert rendered.startswith("Population overview")
    assert "Cell 1: Population overview" not in rendered
    assert "Cell 1: Boundary" not in rendered
    assert max(locations["0/0"]) <= min(locations["1/0"])
    assert min(locations[f"{len(values) - 1}/0"]) >= max(locations["13/0"])
    if width >= 32:
        assert " │ " in rendered
        assert len(locations["1/0"]) > 1


def test_footer_on_new_page_does_not_repeat_grid_header():
    block, _ = table_block([
        ["Name", "Value"], ["First", "One"], ["Second", "Two"],
        ["A footer that belongs after the grid and uses its own full width."],
    ], full_width_rows=frozenset({3}))
    pages = paginate(article(block), width=40, height=4)
    assert "footer" in pages[-1].lines[0].plain
    assert all("Name" not in line.plain and " │ " not in line.plain for line in pages[-1].lines)


@pytest.mark.parametrize("height", [4, 5, 6])
def test_banded_table_after_heading_keeps_grid_header_with_data(height):
    block, _ = table_block([
        ["Table title"], ["Key", "Value"], ["A", "B"], ["End note"],
    ], full_width_rows=frozenset({0, 3}))
    pages = paginate(article(Block("h2", Text("Heading")), block), width=40, height=height)
    _, locations = tagged_cells(pages)
    assert locations["1/0"] == locations["2/0"]
    assert max(locations["0/0"]) <= min(locations["1/0"])


def test_full_width_preformatted_divider_preserves_whitespace_and_following_cells():
    block, expected = table_block([
        ["Name", "Value"], ["Before", "First"], ["  code  \n\n\tcommand  \n"],
        ["After", "Last"],
    ], full_width_rows=frozenset({2}))
    cell = block.table.rows[2][0]
    cell.preformatted = True
    cell.text = Text(expected["2/0"])
    cell.text.stylize(Style(meta={"cell": "2/0", "href": "cell/2/0", "link_id": "link/2/0"}))
    pages = paginate(article(block), width=40, height=5)
    found, locations = tagged_cells(pages)
    expanded = cell.text.copy()
    expanded.expand_tabs(8)
    assert found["2/0"] == expanded.plain.replace("\n", "")
    assert nonspace(found["3/0"]) == "After"
    assert max(locations["2/0"]) <= min(locations["3/0"])
