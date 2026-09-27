# zimtty

A keyboard-first terminal reader for ZIM files: offline Wikipedia and other wikis.
It shows articles one screen-sized page at a time rather than as a scrolling web page,
with the infobox in its own box on the right, and takes its colours from the active
Omarchy theme.

    uv run zimtty                       # picks the largest readable .zim in ~/Downloads/zim
    uv run zimtty "Max Weber"           # open an article directly
    uv run zimtty path/to/file.zim      # or set ZIMTTY_ZIM

Works best with Wikipedia `nopic` ZIMs (both the current `<section>` HTML and the
older flat HTML of e.g. the July 2025 English build). Other MediaWiki-based ZIMs
(Wiktionary, distro wikis) open and read fine. Non-MediaWiki ZIMs such as
Gutenberg (EPUB-based) or Stack Exchange aren't supported yet.

## Keys

    j/k  ↓/↑  PgDn/PgUp  space   next / previous page
    g / G                        first / last page
    c                            contents panel (j/k move, l/h open/close, enter: open subsections, then jump)
    /                            title search (↑/↓ + enter)
    n / N                        select next / previous link (walks across pages), enter opens it
    f                            link hints: type the label to follow a link
    l                            links off/on (plain text, not followable)
    ← / →  backspace             back / forward in history
    a                            jump to "Additional notes" (lead hatnotes/warnings moved there)
    s                            toggle citation markers
    R                            random article
    ?                            key help (press ? again or esc to close)
    q / esc                      quit / close panel

## Layout rules

Whole paragraphs per page (oversized ones split at sentence boundaries), headings
kept with their text, every top-level section starts a new page, infobox in its own
box on the right (continues across pages; text goes full width once it ends).
Colours come from `~/.local/state/omarchy/current/theme/colors.toml` and live-reload
when the theme changes.

## Code

    src/zimtty/zimdoc.py     ZIM access + HTML -> blocks / infobox / TOC (+ TeX -> text)
    src/zimtty/paginate.py   blocks -> screen pages
    src/zimtty/theme.py      Omarchy colors.toml -> palette + Textual theme
    src/zimtty/suggestd.py   title-search subprocess (libzim holds the GIL)
    src/zimtty/app.py        the Textual app
    tests/                   pytest: unit, real-ZIM, and headless UI tests
    tools/                   diagnostics: render, probe, ibox_raw, keylog, gil_probe

## Tests

    uv run pytest                 # everything (~1.5 min)
    uv run pytest tests/test_unit.py   # no ZIM files needed, instant

Tests that need real ZIMs look in `~/Downloads/zim` (or `$ZIMTTY_TEST_ZIM_DIR`) and
are skipped if the files are missing: `wikipedia_en_sociology_nopic_*.zim` (small,
new-style HTML), `wikipedia_en_all_nopic_*.zim` (full, old-style HTML), and any
ZIMs in `other/`.
