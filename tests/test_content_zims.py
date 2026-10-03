"""Content and navigation checks against the optional official ArchWiki ZIM."""
import pytest
from rich.text import Text
from selectolax.parser import HTMLParser

from zimtty.paginate import Layout, Paginator
from zimtty.safety import safe_text
from zimtty.zimdoc import Article, ArticleParser, Block, Palette

from .conftest import sampled_article_paths


def parse(zim, title):
    resolved = zim.resolve(title)
    assert resolved is not None, title
    path, _ = resolved
    html = zim.html(path)
    return ArticleParser(Palette()).parse(path, html), HTMLParser(html)


@pytest.mark.parametrize("title", ["Installation guide", "Network configuration", "Pacman"])
def test_technical_code_blocks_and_heading_anchors_are_retained(archwiki_zim, title):
    article, tree = parse(archwiki_zim, title)
    expected = [safe_text(node.text()) for node in tree.css("pre")]
    actual = [block.text.plain for block in article.blocks if block.kind == "pre"]
    assert actual == expected and actual
    assert article.toc and all(entry.anchor in article.anchors for entry in article.toc)
    assert all(article.anchors[entry.anchor] == entry.block for entry in article.toc)
    # Check actual commands independently of paragraph/table decoration.
    for source in actual:
        code = Article(article.path, title, [Block("pre", Text(source))], [], [])
        pages = Paginator(Palette()).paginate(code, Layout(6, 88, 88, 0), set())
        rendered = "\n".join(line.plain for page in pages for line in page.lines)
        assert "".join(rendered.split()) == "".join(source.split())
        assert all(len(page.lines) <= 6 and all(line.cell_len <= 88 for line in page.lines)
                   for page in pages)


@pytest.mark.parametrize("title,command", [
    ("Pacman/Rosetta", "pacman -S"),
    ("Systemd", "systemctl status"),
])
def test_real_command_tables_are_retained_and_paginate(archwiki_zim, title, command):
    article, _ = parse(archwiki_zim, title)
    tables = [block for block in article.blocks if block.table is not None]
    assert tables and any(block.table.simple for block in tables)
    assert any(command in cell.text.plain for block in tables
               for row in block.table.rows for cell in row)
    if title == "Systemd":
        table = next(block.table for block in tables if block.table.full_width_rows)
        assert "Analyzing the system state" in {
            table.rows[index][0].text.plain for index in table.full_width_rows
        }
    for width in (20, 45, 88):
        pages = Paginator(Palette()).paginate(article, Layout(10, width, width, 0), set())
        assert all(len(page.lines) <= 10 and all(line.cell_len <= width for line in page.lines)
                   for page in pages)
        shown_blocks = set().union(*(page.blocks for page in pages))
        assert {i for i, block in enumerate(article.blocks) if block.table is not None} <= shown_blocks


def test_warning_stays_before_its_command_and_file_examples_keep_their_names(archwiki_zim):
    installation, _ = parse(archwiki_zim, "Installation guide")
    warning = next(i for i, block in enumerate(installation.blocks)
                   if block.text.plain.startswith("Warning Only format the EFI system partition"))
    assert installation.blocks[warning + 1].kind == "pre"
    assert "mkfs.fat" in installation.blocks[warning + 1].text.plain

    network, _ = parse(archwiki_zim, "Network configuration")
    filename = next(i for i, block in enumerate(network.blocks)
                    if block.kind == "pre" and block.text.plain == "/etc/hostname")
    assert network.blocks[filename + 1].kind == "pre"
    assert network.blocks[filename + 1].text.plain == "yourhostname\n"


def test_relative_links_redirects_and_fragments_resolve_from_real_article(archwiki_zim):
    article, tree = parse(archwiki_zim, "Pacman/Rosetta")
    source_hrefs = {node.attributes["href"] for node in tree.css("a[href]")}
    # Includes a parent-directory href, a fragment on another article, and a
    # redirect whose destination has a different canonical archive path.
    for href, expected in (
        ("../Pacman", ("Pacman", None)),
        ("../System_maintenance#Partial_upgrades_are_unsupported",
         ("System_maintenance", "Partial_upgrades_are_unsupported")),
        ("../ABS", ("Arch_build_system", None)),
    ):
        assert href in source_hrefs
        assert archwiki_zim.resolve(href, base_path=article.path,
                                   base_href=article.base_href) == expected
        if expected[1]:
            destination, _ = parse(archwiki_zim, expected[0])
            assert expected[1] in destination.anchors

    table_hrefs = {span.style.meta["href"] for block in article.blocks if block.table
                   for row in block.table.rows for cell in row for span in cell.text.spans
                   if getattr(span.style, "meta", None) and "href" in span.style.meta}
    assert "../System_maintenance#Partial_upgrades_are_unsupported" in table_hrefs
    anchor = article.toc[0].anchor
    assert archwiki_zim.resolve(f"#{anchor}", base_path=article.path) == (article.path, anchor)


def test_archwiki_search_finds_exact_title(archwiki_zim):
    assert archwiki_zim.suggest("installation guide", 3)[0] == (
        "Installation_guide", "Installation guide")


def test_sampled_archwiki_articles_paginate(archwiki_zim, pal):
    for path in sampled_article_paths(archwiki_zim, 12, seed=23):
        article = ArticleParser(pal).parse(path, archwiki_zim.html(path))
        for width in (20, 45, 88):
            pages = Paginator(pal).paginate(article, Layout(10, width, width, 0), set())
            assert pages, path
            assert all(len(page.lines) <= 10 and all(line.cell_len <= width for line in page.lines)
                       for page in pages), path
