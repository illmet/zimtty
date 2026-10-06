# zimtty

zimtty is a keyboard-first terminal reader for local ZIM archives, including
offline Wikipedia and ArchWiki. Search article titles, browse a contents panel,
follow internal links and section anchors, and move through reading history
without leaving the terminal. Articles appear one screen-sized page at a time,
with headings kept beside their text and infoboxes shown alongside the article
or inline on narrower screens. 

The reader preserves code indentation, lists, captions, and technical warnings.
Tables keep their columns, including merged cells and multi-row headers, with
headers repeated on each page and full-width titles or notes. Columns left
empty by the missing images (portraits, party colours) are dropped. Tables too
wide for the screen, or whose sentences would be squeezed into narrow columns,
become records labelled by their headers. Long code lines fold to fit, and
article text and tables use up to 88 columns. Wikipedia `nopic` and English ArchWiki archives have integration
coverage; other text-based wikis may need adjustments for their particular HTML.
The minimum supported terminal size is 24 columns by 8 rows.

Reading stays offline, and the app writes no usage history.
Optional `--diagnostics` prints a short trace on exit containing only the last
32 event codes, timings, and limited code locations, excluding typed input,
article text, paths, and exception messages. Verbose framework logging and
automatic screenshots are disabled. Redirecting diagnostics to a file or using
terminal recording can still leave a record outside the app.

    uv run zimtty                       # largest readable .zim in ~/Downloads/zim
    uv run zimtty "Max Weber"           # open an article directly
    uv run zimtty path/to/file.zim      # choose an archive, or set ZIMTTY_ZIM
    uv run zimtty --diagnostics         # print the short trace after exiting

    # optional alias to quickly jump to the application
    alias wiki='uv run --offline --no-sync --project /path-to/zimtty zimtty'

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

When searching, title suggestions update after a 200 ms pause in typing. Enter submits immediately.

## Code

    src/zimtty/zimdoc.py      ZIM access + HTML -> blocks / infobox / TOC (+ TeX -> text)
    src/zimtty/paginate.py    blocks -> screen pages
    src/zimtty/theme.py       Omarchy colors.toml -> palette + Textual theme
    src/zimtty/safety.py      terminal-control filtering for archive display text
    src/zimtty/diagnostics.py bounded event codes without input or document text
    src/zimtty/suggestd.py    title-search subprocess (libzim holds the GIL)
    src/zimtty/app.py         the Textual app
    tests/                   unit, real-ZIM, and headless UI tests
    tools/                   render, probe, ibox_raw, keylog, gil_probe

Developer tools may display archive contents. `tools/keylog.py [kitty]` reports
input event sizes and escape-sequence presence without recording raw keys.

## Tests

    uv run pytest                     # everything, including optional local archives
    uv run pytest tests/test_unit.py   # no ZIM files needed, instant
    uv run pytest tests/test_security_*.py tests/test_suggestd_recovery.py  # no ZIM files needed

Tests that need real ZIMs look in `~/Downloads/zim` (or `$ZIMTTY_TEST_ZIM_DIR`) and
are skipped if the files are missing: `wikipedia_en_all_nopic_*.zim` and
`archlinux_en_all_maxi_*.zim`. Archive samples use seeded entry selection for
repeatability within a particular ZIM. Synthetic fixtures cover flat and
section-based HTML, code, tables, links, and malformed input without downloads.

## Theme sync

By default, zimtty uses a built-in dark palette inspired by Tokyo Night, with
a dark background, pale text, and blue accents.

When a compatible theme file is available, the reader follows its colours and
picks up changes every two seconds. For example, Omarchy supplies this file at
`~/.local/state/omarchy/current/theme/colors.toml` (under `$XDG_STATE_HOME` when
set). Other theme tools can sync colours by maintaining a compatible file at
the same location.
