"""Common textual HTML retains content, order, anchors and literal formatting."""
import pytest
from rich.style import Style

from zimtty.zimdoc import ArticleParser, Palette, header_rows, place_cells, rule_breaks


def parse(html):
    return ArticleParser(Palette()).parse("guide/page", html)


def texts(article):
    return [block.text.plain for block in article.blocks]


@pytest.mark.parametrize("container", ["main", "article", 'div class="mw-parser-output"', 'div id="mw-content-text"'])
def test_content_root_excludes_surrounding_chrome_and_does_not_duplicate_title(container):
    tag = container.split()[0]
    article = parse(f'<html><body><nav>Navigation</nav><{container}>'
                    '<h1 id="title">Guide</h1>Direct <b>bold</b> text.'
                    f'<p>Paragraph.</p></{tag}><footer>Footer</footer></body></html>')
    assert texts(article) == ["Guide", "Direct bold text.", "Paragraph."]
    assert article.anchors["title"] == 0


def test_sections_inside_wrappers_are_walked_once_with_surrounding_content():
    article = parse('''<h1>Guide</h1><p>Before.</p>
      <section><h2 id="Part">Part</h2><p>First.</p>
        <div><section><h3 id="Nested">Nested</h3><p>Nested text.</p></section></div>
        <p>After nested.</p></section><p>After section.</p>''')
    assert texts(article) == ["Guide", "Before.", "Part", "First.", "Nested", "Nested text.",
                              "After nested.", "After section."]
    assert [entry.title for entry in article.toc] == ["Part", "Nested"]


def test_plain_body_text_and_unfamiliar_inline_tags_are_retained():
    article = parse('Before <custom>middle</custom> after.<p>Paragraph.</p>Tail.')
    assert texts(article) == ["guide/page", "Before middle after.", "Paragraph.", "Tail."]


def test_title_inline_words_keep_spaces_and_title_occurs_once():
    article = parse('<main><h1>A <em>technical</em> title</h1><p>Body.</p></main>')
    assert article.title == "A technical title"
    assert texts(article) == ["A technical title", "Body."]


def test_figures_preserve_code_and_captions_while_images_remain_omitted():
    article = parse('<figure><img src="diagram.png" alt="Picture">'
                    '<pre>  first\n\tsecond</pre><figcaption>Example command.</figcaption></figure>')
    assert texts(article) == ["guide/page", "  first\n\tsecond", "Example command."]
    assert article.blocks[1].kind == "pre"


def test_preformatted_text_preserves_whitespace_and_sanitizes_controls():
    article = parse('<h1>Code</h1><pre id="commands"><code>  echo &lt;one&gt;\n'
                    '\t<span>printf two</span>\n\ntrailing  \n&#27;[31m</code></pre>')
    block = article.blocks[1]
    assert block.kind == "pre"
    assert block.text.plain == "  echo <one>\n\tprintf two\n\ntrailing  \n[31m"
    assert article.anchors["commands"] == 1


def test_list_paragraphs_commands_and_nested_items_keep_document_order():
    article = parse('''<ol start="3"><li id="step"><p>Do this.</p><pre>one\n  two</pre>
      <p>Then this.</p><ul><li>Child.</li></ul>After child.</li><li value="8">Last.</li></ol>''')
    assert texts(article) == ["guide/page", "3. Do this.", "one\n  two", "Then this.",
                              "◦ Child.", "After child.", "8. Last."]
    assert article.blocks[2].kind == "pre"
    assert article.blocks[2].indent > article.blocks[1].indent
    assert article.anchors["step"] == 1


def test_definition_lists_and_quotes_preserve_nested_blocks():
    article = parse('''<dl><dt>Term</dt><dd><p>Definition.</p><pre>  configure\n\trun</pre>
      <p>Explanation.</p></dd></dl><blockquote><p>Quote one.</p><pre>  code</pre>
      <p>Quote two.</p></blockquote>''')
    assert texts(article) == ["guide/page", "Term", "Definition.", "  configure\n\trun",
                              "Explanation.", "Quote one.", "  code", "Quote two."]
    assert [b.kind for b in article.blocks if b.kind == "pre"] == ["pre", "pre"]
    assert all(article.blocks[i].indent > 0 for i in (2, 3, 4, 5, 6, 7))


def test_technical_warning_stays_before_commands_and_keeps_direct_text():
    article = parse('''<main><h1>Guide</h1><p>Intro.</p>
      <div class="archwiki-template-box archwiki-template-box-warning ambox" id="warning">
        <strong>Warning:</strong> back up first.<p>Do not interrupt.</p></div>
      <pre>dangerous-example</pre><h2>References</h2><p>Reference.</p></main>''')
    assert texts(article) == ["Guide", "Intro.", "Warning: back up first.", "Do not interrupt.",
                              "dangerous-example", "References", "Reference."]
    assert article.notes == []
    assert article.anchors["warning"] == 2


def test_wikipedia_lead_notes_and_backmatter_keep_anchors_after_reordering():
    article = parse('''<div class="mw-parser-output"><h1>Guide</h1>
      <div class="hatnote" id="disambig">Other uses.</div><p>Lead.</p>
      <h2 id="References">References</h2><p>Sources.</p>
      <h2 id="Body">Body</h2><p>Text.</p>
      <table class="ambox" id="maintenance"><tr><td class="mbox-text">Needs work.</td></tr></table>
      </div>''')
    assert [entry.title for entry in article.toc] == ["Body", "Additional notes", "References"]
    assert texts(article)[article.anchors["disambig"]] == "Other uses."
    assert texts(article)[article.anchors["maintenance"]] == "Needs work."
    assert texts(article)[article.anchors["References"]] == "References"


def test_heading_span_ids_empty_anchors_and_container_aliases_are_mapped():
    article = parse('''<main id="root"><h1 id="title">Guide</h1>
      <a name="legacy"></a><h2 id="outer"><span class="mw-headline" id="inner">Part</span></h2>
      <p id="paragraph">Text <span id="inline">within.</span></p>
      <span id="next"></span><pre id="code">command</pre><a name="tail"></a></main>''')
    assert article.anchors["title"] == 0
    assert article.anchors["legacy"] == article.anchors["outer"] == article.anchors["inner"] == 1
    assert article.anchors["paragraph"] == article.anchors["inline"] == 2
    assert article.anchors["next"] == article.anchors["code"] == article.anchors["tail"] == 3
    assert "root" in article.anchors
    assert article.toc[0].anchor == "outer"


def test_duplicate_anchors_target_first_occurrence_and_empty_heading_aliases_survive():
    article = parse('<h2 id="empty">Empty</h2><h2 id="same">Part</h2>'
                    '<p id="same">Body.</p><h2 id="last">Last empty</h2>')
    assert [entry.title for entry in article.toc] == ["Part"]
    assert article.anchors["same"] == article.anchors["empty"] == 1
    assert article.anchors["last"] == 2


def test_base_href_retains_archive_identity_for_resolver():
    article = parse('<head><base href="../assets/"></head><p>Text.</p>')
    assert article.base_href == "../assets/"


def test_plain_table_retains_caption_cells_headers_links_and_anchors():
    article = parse('''<table id="options"><caption id="caption">Options</caption>
      <tr><th>Flag</th><th>Meaning</th></tr>
      <tr><td id="flag"><code>--help</code></td><td><a href="Help">Open help</a></td></tr></table>''')
    block = article.blocks[1]
    assert block.kind == "table" and block.table.simple
    assert block.table.caption.plain == "Options"
    assert [[cell.text.plain for cell in row] for row in block.table.rows] == [
        ["Flag", "Meaning"], ["--help", "Open help"]]
    assert [[cell.header for cell in row] for row in block.table.rows] == [[True, True], [False, False]]
    assert all(word in block.text.plain for word in ("Options", "Flag", "Meaning", "--help", "Open help"))
    assert {article.anchors[a] for a in ("options", "caption", "flag")} == {1}
    assert any(span.style.meta.get("href") == "Help" for span in block.table.rows[1][1].text.spans)


@pytest.mark.parametrize("body,simple,aligned", [
    ('<tr><td rowspan="999999999999999999999999">Once</td><td>Second</td></tr>', False, False),
    ('<tr><td colspan="2">Once</td><td>Second</td></tr><tr><td>A</td><td>B</td><td>C</td></tr>',
     True, True),
    ('<tr><td>Once</td><td>Second</td></tr><tr><td>Third</td></tr>', True, True),
    ('<tr><td>Once<table><tr><td>Nested</td></tr></table>After</td><td>Second</td></tr>', False, True),
])
def test_tables_keep_each_source_cell_once(body, simple, aligned):
    article = parse(f'<table><caption>Caption</caption>{body}</table>')
    block = article.blocks[1]
    assert block.kind == "table"
    assert (block.table.simple, block.table.aligned) == (simple, aligned)
    assert block.text.plain.count("Once") == 1
    assert block.text.plain.count("Second") == 1
    assert block.text.plain.count("Caption") == 1
    if "Nested" in body:
        assert block.text.plain.count("Nested") == block.text.plain.count("After") == 1
        assert "Once\nNested\nAfter" in block.table.rows[0][0].text.plain
    assert sum(len(row) for row in block.table.rows) <= 5


def positions(table):
    starts, width = place_cells(table.rows, table.full_width_rows)
    return width, [[(cell.text.plain, start, cell.colspan, cell.rowspan)
                    for cell, start in zip(row, row_starts)]
                   for row, row_starts in zip(table.rows, starts)]


def test_merged_cells_follow_the_html_table_model():
    table = parse('''<table>
      <tr><th rowspan="2">Name</th><th colspan="2">Term</th><th rowspan="2">Party</th></tr>
      <tr><th>Start</th><th>End</th></tr>
      <tr><td>Walpole</td><td>1721</td><td>1742</td><td rowspan="3">Whig</td></tr>
      <tr><td rowspan="2">Pelham</td><td>1743</td><td>1754</td></tr>
      <tr><td>1754</td><td>1756</td></tr></table>''').blocks[1].table
    assert table.simple and table.aligned and not table.full_width_rows
    assert positions(table) == (4, [
        [("Name", 0, 1, 2), ("Term", 1, 2, 1), ("Party", 3, 1, 2)],
        [("Start", 1, 1, 1), ("End", 2, 1, 1)],
        [("Walpole", 0, 1, 1), ("1721", 1, 1, 1), ("1742", 2, 1, 1), ("Whig", 3, 1, 3)],
        [("Pelham", 0, 1, 2), ("1743", 1, 1, 1), ("1754", 2, 1, 1)],
        [("1754", 1, 1, 1), ("1756", 2, 1, 1)],
    ])
    assert header_rows(table.rows, table.full_width_rows) == range(0, 2)


def test_columns_and_rows_empty_of_data_are_dropped_with_their_anchors():
    # A nopic export: portraits are empty cells, and a party colour swatch
    # shares a colspan="2" header with the party name.
    article = parse('''<table>
      <tr><th>No.</th><th>Portrait</th><th>Name</th><th colspan="2">Party</th></tr>
      <tr><td></td><td></td><td></td><td></td><td></td></tr>
      <tr><th>1</th><td id="first-portrait"></td><td>Washington</td>
          <td style="background-color:#DCDCDC"></td><td>Unaffiliated</td></tr>
      <tr><th>2</th><td></td><td>Adams</td><td style="background-color:#EA9978"></td>
          <td>Federalist</td></tr></table>''')
    table = article.blocks[1].table
    assert table.simple and table.aligned
    assert positions(table) == (3, [
        [("No.", 0, 1, 1), ("Name", 1, 1, 1), ("Party", 2, 1, 1)],
        [("1", 0, 1, 1), ("Washington", 1, 1, 1), ("Unaffiliated", 2, 1, 1)],
        [("2", 0, 1, 1), ("Adams", 1, 1, 1), ("Federalist", 2, 1, 1)],
    ])
    assert "Portrait" not in article.blocks[1].text.plain
    assert article.anchors["first-portrait"] == 1


def test_rowspans_shrink_when_an_empty_row_is_dropped():
    table = parse('''<table><tr><th>Party</th><th>Name</th></tr>
      <tr><td rowspan="3">Whig</td><td>Walpole</td></tr>
      <tr><td></td></tr>
      <tr><td>Pelham</td></tr></table>''').blocks[1].table
    assert positions(table) == (2, [
        [("Party", 0, 1, 1), ("Name", 1, 1, 1)],
        [("Whig", 0, 1, 2), ("Walpole", 1, 1, 1)],
        [("Pelham", 1, 1, 1)],
    ])


def test_header_only_tables_have_no_header_to_label_records():
    table = parse('<table><tr><th>Only a caption-like header</th></tr>'
                  '<tr><td></td></tr></table>').blocks[1].table
    assert [[cell.text.plain for cell in row] for row in table.rows] == [["Only a caption-like header"]]
    assert header_rows(table.rows, table.full_width_rows) == range(0)


@pytest.mark.parametrize("body", [
    '<tr><td>A</td><td rowspan="2">B</td></tr><tr><td colspan="2">C</td></tr>',  # C overlaps B
    '<tr><td colspan="9999">Wide</td></tr><tr><td>A</td><td>B</td></tr>',
])
def test_overlapping_or_oversized_spans_keep_source_order(body):
    table = parse(f'<table>{body}</table>').blocks[1].table
    assert not table.simple and not table.aligned and not table.full_width_rows
    assert all(cell.colspan == cell.rowspan == 1 for row in table.rows for cell in row)


def test_rules_between_stacked_cell_values_are_line_breaks_marked_apart_from_br():
    article = parse('<table><tr><th>Election</th><th>Name</th></tr>'
                    '<tr><td>1800<br><hr>1804</td><td>Thomas Jefferson<br>(1743–1826)</td></tr></table>')
    election, name = article.blocks[1].table.rows[1]
    assert election.text.plain == "1800\n1804" and rule_breaks(election.text) == {4}
    assert name.text.plain == "Thomas Jefferson\n(1743–1826)" and not rule_breaks(name.text)
    assert parse("<div>One<hr>Two</div>").blocks[1].text.plain == "One\nTwo"


def test_full_width_table_rows_keep_position_styles_links_and_anchors():
    article = parse('''<table><caption>Caption</caption>
      <tr><th colspan="2" id="title">Title</th></tr>
      <tr><th>Year</th><th>Value</th></tr><tr><td>2025</td><td>100</td></tr>
      <tr><td colspan="2"><b>Section</b></td></tr><tr><td>2026</td><td>200</td></tr>
      <tr><td colspan="2" id="note"><a href="Source">Source note</a></td></tr></table>''')
    block = article.blocks[1]
    assert block.table.simple
    assert block.table.full_width_rows == frozenset({0, 3, 5})
    assert [len(row) for row in block.table.rows] == [1, 2, 2, 1, 2, 1]
    assert block.text.plain == "Caption\nTitle\nYear | Value\n2025 | 100\nSection\n2026 | 200\nSource note"
    assert article.anchors["title"] == article.anchors["note"] == 1
    assert any(span.style.meta.get("href") == "Source"
               for span in block.table.rows[-1][0].text.spans)


@pytest.mark.parametrize("span", ["", "0", "-1", "2.0", "invalid", "9" * 1000])
def test_invalid_or_huge_colspan_stays_bounded_and_in_source_order(span):
    article = parse(f'<table><tr><th colspan="{span}">Title</th></tr>'
                    '<tr><td>One</td><td>Two</td></tr></table>')
    table = article.blocks[1].table
    assert not table.simple and not table.full_width_rows
    assert [[cell.text.plain for cell in row] for row in table.rows] == [["Title"], ["One", "Two"]]


def test_bare_colspan_attribute_uses_fallback_without_raising():
    article = parse('<table><tr><th colspan>Title</th></tr>'
                    '<tr><td>One</td><td>Two</td></tr></table>')
    assert not article.blocks[1].table.simple
    assert article.blocks[1].table.rows[0][0].text.plain == "Title"


def test_table_cells_keep_preformatted_and_paragraph_content():
    article = parse('<table><tr><td><p>Before.</p><pre>  one\n\ttwo</pre><p>After.</p></td></tr></table>')
    assert article.blocks[1].table.rows[0][0].text.plain == "Before.\n  one\n\ttwo\nAfter."
    assert article.blocks[1].table.rows[0][0].preformatted


def test_infobox_has_inline_fallback_and_later_infoboxes_are_not_dropped():
    article = parse('''<h1>Guide</h1><table class="infobox" id="first-info">
      <tr><th>Name</th><td id="first-value">First</td></tr></table><p>Between.</p>
      <table class="infobox" id="second-info"><tr><th>Name</th><td>Second</td></tr></table>''')
    first, second = article.blocks[2], article.blocks[3]
    assert first.kind == "infobox" and "First" in first.text.plain
    assert second.kind == "table" and "Second" in second.text.plain
    assert article.anchors["first-info"] == article.anchors["first-value"] == 2
    assert article.anchors["second-info"] == 3


def test_first_lead_paragraph_moves_before_the_infobox_and_its_companion_tables():
    article = parse('''<h1>Guide</h1><table class="infobox" id="box"><tr><th>Born</th><td>1900</td></tr></table>
      <table id="names"><tr><th>Native name</th><td>Name</td></tr></table>
      <p id="intro">Introduction.</p><p>Second.</p><h2 id="Part">Part</h2><p>Body.</p>''')
    assert [(block.kind, block.text.plain.split("\n")[0]) for block in article.blocks[:5]] == [
        ("title", "Guide"), ("para", "Introduction."), ("infobox", "Guide"),
        ("table", "Native name | Name"), ("para", "Second.")]
    assert (article.anchors["intro"], article.anchors["box"], article.anchors["names"]) == (1, 2, 3)


@pytest.mark.parametrize("between", [
    "<ul><li>Item.</li></ul>", "<h3>Sub</h3>", "<blockquote><p>Quote.</p></blockquote>",
    "<dl><dd>Indented.</dd></dl>"])
def test_lead_paragraph_stays_after_lists_quotes_and_headings_below_the_infobox(between):
    article = parse('<h1>Guide</h1><table class="infobox"><tr><th>Born</th><td>1900</td></tr></table>'
                    f'{between}<p>Later.</p>')
    assert [block.kind for block in article.blocks][:2] == ["title", "infobox"]
    assert article.blocks[-1].text.plain == "Later."


def test_infobox_after_the_lead_paragraph_or_without_one_keeps_its_place():
    after = parse('<h1>Guide</h1><p>First.</p><table class="infobox"><tr><th>A</th><td>B</td></tr></table>'
                  '<p>Second.</p>')
    assert [block.kind for block in after.blocks] == ["title", "para", "infobox", "para"]
    alone = parse('<h1>Guide</h1><table class="infobox"><tr><th>A</th><td>B</td></tr></table>'
                  '<h2>Part</h2><p>Body.</p>')
    assert [block.kind for block in alone.blocks] == ["title", "infobox", "h2", "para"]


def test_archive_markup_is_literal_in_new_content_types():
    payload = '[@click=app.bell]literal[/]'
    article = parse(f'<pre>{payload}&#27;</pre><table><caption>{payload}</caption>'
                    f'<tr><td>{payload}&#27;</td></tr></table>')
    values = [b.text for b in article.blocks]
    values.extend(cell.text for b in article.blocks if b.table for row in b.table.rows for cell in row)
    assert any(payload in value.plain for value in values)
    assert all("\x1b" not in value.plain for value in values)
    assert not any(isinstance(span.style, Style) and span.style.meta.get("@click")
                   for value in values for span in value.spans)


def test_hidden_navigation_and_scripts_do_not_leak_into_content():
    article = parse('''<main><h1>Guide</h1><nav id="nav">Navigation</nav>
      <div id="toc">Old contents</div><p>Visible<script>secret()</script> text.</p>
      <div style="display:none">Hidden</div><pre>safe<script>secret()</script></pre></main>''')
    assert texts(article) == ["Guide", "Visible text.", "safe"]
    assert "nav" not in article.anchors
