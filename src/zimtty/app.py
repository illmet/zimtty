"""zimtty: a keyboard-first, paginated terminal reader for ZIM files (offline
Wikipedia and other wikis), themed from the active Omarchy theme."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .diagnostics import DiagnosticTrace, Event, disable_framework_logging

disable_framework_logging()

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key, Resize
from textual.widgets import Input, OptionList, Static, Tree
from textual.widgets.option_list import Option

from . import theme as omatheme
from .paginate import Layout, Page, Paginator
from .safety import safe_text
from .zimdoc import Article, ArticleParser, Zim, local_href

# Textual abandons an escape sequence after 32 chars and "reissues" it as typed
# keys. With the kitty keyboard protocol, foot delivers text produced by the
# Compose key (Omarchy: Caps Lock, e.g. the name/email macros in ~/.XCompose)
# as ONE CSI-u sequence carrying every codepoint: "ilyaamet@gmail.com" is ~75
# chars, so it used to land in the search box as "^[101;;105:108:…u" garbage.
# Real sequences this long are always one of these, so a higher cap is safe.
import textual._xterm_parser as _xp  # noqa: E402

_xp._MAX_SEQUENCE_SEARCH_THRESHOLD = max(_xp._MAX_SEQUENCE_SEARCH_THRESHOLD, 1024)

HINT_KEYS = "asdfghjklqwertyuiopzxcvbnm"
INFO_MIN_TOTAL = 96  # below this terminal width the infobox gets no side column
INFO_W = 38  # inner width of the infobox
TEXT_MAX = 88  # comfortable reading measure


@dataclass
class Place:
    path: str
    block: int = 0
    page_offset: int = 0  # continuation page within a code/table/prose block
    breaks: frozenset[int] = frozenset()


# ------------------------------------------------------------------ widgets


class VimTree(Tree):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "scroll_home", show=False),
        Binding("G", "scroll_end", show=False),
    ]


class TocPanel(Vertical):
    DEFAULT_CSS = """
    TocPanel {
        dock: left; layer: overlay; width: 42; height: 100%;
        display: none; background: $background;
        border: solid $accent; border-title-color: $accent; padding: 0 1;
    }
    TocPanel Tree { background: $background; scrollbar-size-vertical: 1; }
    """

    def compose(self) -> ComposeResult:
        t: Tree[int] = VimTree("contents", id="toc-tree")
        t.show_root = False
        t.auto_expand = False
        t.guide_depth = 2
        yield t


class SearchBar(Vertical):
    DEFAULT_CSS = """
    SearchBar {
        dock: top; layer: overlay; height: auto; width: 100%;
        display: none; background: $background;
        border: solid $accent; border-title-color: $accent; padding: 0 1;
    }
    /* Textual's default Input has a 'tall' border (and a focused variant with
       higher specificity). With height: 1 those two border rows ate the only
       row, leaving 0 rows for the text: typed characters were invisible. */
    SearchBar Input, SearchBar Input:focus {
        border: none; height: 1; padding: 0; background: $background; color: $foreground;
    }
    SearchBar Input > .input--placeholder { color: $text-muted; }
    SearchBar Input > .input--cursor { background: $accent; color: $background; }
    SearchBar OptionList, SearchBar OptionList:focus { border: none; height: auto; max-height: 5; background: $background; padding: 0; }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search titles…", id="search-input")
        yield OptionList(id="search-results", markup=False)


class HelpPanel(Static):
    """Key reference. Toggled by ?, closed by ? / esc, auto-closes after a while."""
    DEFAULT_CSS = """
    HelpPanel {
        layer: overlay; dock: right; width: 58; height: auto; margin: 1 2;
        display: none; background: $background; color: $foreground;
        border: solid $accent; border-title-color: $accent; padding: 0 1;
    }
    """


class PageView(Horizontal):
    DEFAULT_CSS = """
    PageView { height: 1fr; width: 100%; align-horizontal: center; padding: 1 2 0 2; }
    #text { width: auto; height: 100%; }
    #gutter { width: 3; }
    #info {
        width: auto; height: auto; max-height: 100%;
        border: solid $accent 60%; border-title-color: $text-muted;
        border-subtitle-color: $text-muted; padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="text", markup=False)
        yield Static(id="gutter")
        yield Static(id="info", markup=False)

    def on_resize(self, event: Resize) -> None:
        # App resize events arrive before this container has its new geometry.
        # Widget Resize events do not bubble, so forward the completed layout.
        self.app._queue_reader_resize()


class StatusBar(Horizontal):
    DEFAULT_CSS = """
    StatusBar { dock: bottom; height: 1; padding: 0 2; background: $background; }
    #st-left { width: 1fr; color: $text-muted; }
    #st-right { width: auto; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="st-left", markup=False)
        yield Static(id="st-right", markup=False)


# ------------------------------------------------------------------ app


class ZimTTY(App):
    TITLE = "zimtty"
    AUTO_FOCUS = None  # the reader itself has focus; panels take it only while open
    CSS = """
    Screen { layers: base overlay; background: $background; }
    #splash { width: 100%; height: 100%; content-align: center middle; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("slash", "search", "search"),
        Binding("c", "toc", "contents"),
        Binding("j,down,pagedown,space", "page(1)", "next page", show=False),
        Binding("k,up,pageup", "page(-1)", "prev page", show=False),
        Binding("g,home", "first", show=False),
        Binding("G,end", "last", show=False),
        Binding("left,backspace", "back", "back", show=False),
        Binding("right", "forward", "forward", show=False),
        Binding("f", "hints", "links", show=False),
        Binding("l", "toggle_links", "plain links", show=False),
        Binding("n", "link_step(1)", "next link", show=False),
        Binding("N", "link_step(-1)", "previous link", show=False),
        Binding("enter", "link_open", "open link", show=False),
        Binding("a", "notes", "additional notes", show=False),
        Binding("s", "toggle_refs", "citations", show=False),
        Binding("R", "random", "random", show=False),
        Binding("question_mark", "help", "help", show=False),
    ]

    def __init__(self, zim: Zim, start: str | None, trace: DiagnosticTrace | None = None):
        disable_framework_logging()
        super().__init__()
        self.diagnostics = trace if trace is not None else DiagnosticTrace()
        self.zim = zim
        self.start = start
        try:
            self.pal, self._theme = omatheme.build(omatheme.read_colors(strict=True))
        except omatheme.ThemeError as error:
            self.diagnostics.record(Event.THEME_INVALID, error)
            self.pal, self._theme = omatheme.build(omatheme.FALLBACK)
        self._theme_mtime = omatheme.colors_mtime()
        self.show_refs = False
        self.article: Article | None = None
        self.pages: list[Page] = []
        self.page_i = 0
        self.breaks: set[int] = set()
        self.history: list[Place] = []
        self.hist_i = -1
        self.hints: dict[str, str] | None = None
        self.hint_buf = ""
        self._search_timer = None
        self._results_for: str | None = None
        self._sugg_proc = None
        self._query_id = 0
        self._open_on_results: str | None = None
        self.plain_links = False  # 'l': hide link styling and disable following
        self.link_sel: int | None = None  # n/N: index into this page's link targets
        self._help_timer = None

    def _handle_exception(self, error: Exception) -> None:
        # Textual's default traceback shows locals, which can contain queries
        # and whole articles. Keep its failure/test signaling but not rendering.
        self._return_code = 1
        if self._exception is None:
            self._exception = error
            self._exception_event.set()
        self.diagnostics.record(Event.INTERNAL_ERROR, error)
        self._exit_renderables.append(Text(
            "zimtty: internal error; use --diagnostics for a short trace."
        ))
        self._close_messages_no_wait()

    # -------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield SearchBar()
        yield TocPanel()
        yield HelpPanel()
        yield PageView()
        yield StatusBar()

    def on_mount(self) -> None:
        self.register_theme(self._theme)
        self.theme = "omarchy"
        self.query_one(SearchBar).border_title = "/ search"
        self.query_one(TocPanel).border_title = "contents"
        self.set_interval(2.0, self._check_theme)
        self.run_worker(self._start_suggestd(), group="suggestd")
        if not self.start or not self.open(self.start):
            self._splash()

    def _splash(self):
        n = self.zim.z.article_count
        t = Text.assemble(
            (safe_text(self.zim.name) + "\n", Style(bold=True, color=self.pal.heading)),
            (f"{n:,} articles, fully offline\n\n", Style(color=self.pal.muted)),
            ("/", Style(bold=True, color=self.pal.accent)), (" search    ", Style(color=self.pal.muted)),
            ("R", Style(bold=True, color=self.pal.accent)), (" random    ", Style(color=self.pal.muted)),
            ("?", Style(bold=True, color=self.pal.accent)), (" keys    ", Style(color=self.pal.muted)),
            ("q", Style(bold=True, color=self.pal.accent)), (" quit", Style(color=self.pal.muted)),
            justify="center",
        )
        self.query_one("#text", Static).update(t)
        self.query_one("#info", Static).display = False
        self.query_one("#gutter", Static).display = False
        self._status()
        self.action_search()  # open immediately so the very first keystroke lands in it

    def _layout(self) -> Layout:
        pv = self.query_one(PageView)
        # Textual's content size already excludes the container's padding.
        W = max(pv.content_size.width, 20)
        H = max(pv.content_size.height, 6)
        if W >= INFO_MIN_TOTAL:
            info = INFO_W
            narrow = min(TEXT_MAX, W - (info + 4) - 3)
        else:
            info, narrow = 0, min(TEXT_MAX, W)
        return Layout(height=H, full_width=min(TEXT_MAX, W), narrow_width=narrow, info_width=info)

    def _queue_reader_resize(self) -> None:
        if getattr(self, "_reader_resize_pending", False):
            return
        self._reader_resize_pending = True
        self.call_after_refresh(self._repaginate_after_resize)

    def _repaginate_after_resize(self) -> None:
        self._reader_resize_pending = False
        if not self.article:
            return
        layout = self._layout()
        if layout == getattr(self, "_reader_resize_layout", None):
            return
        self._reader_resize_layout = layout
        self._repaginate()

    # -------------------------------------------------------------- navigation

    def open(self, target: str, push: bool = True, block: int | None = None,
             *, relative: bool = False, page_offset: int = 0,
             page_breaks: frozenset[int] | None = None) -> bool:
        # Prepare the entire document before replacing the current reading state.
        # Archive read/parse errors must not leave new content with old pages.
        try:
            if relative and self.article is not None:
                res = self.zim.resolve(target, base_path=self.article.path,
                                       base_href=self.article.base_href)
            else:
                res = self.zim.resolve(target)
            if not res:
                self.notify(safe_text(f"not in this ZIM: {target}")[:300],
                            severity="warning", timeout=3, markup=False)
                return False
            path, frag = res
            article = self._read_article(path)
            if block is None:
                block = self._anchor_block(frag, article) if frag else 0
            block = max(0, min(block, len(article.blocks) - 1))
            if page_breaks is not None:
                breaks = set(page_breaks)
            else:
                breaks = {block} if block and article.blocks[block].kind != "infobox" else set()
            pages = Paginator(self.pal).paginate(article, self._layout(), breaks)
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            # Includes bounded-parser ArticleError and defensive RecursionError.
            self._article_error(error)
            return False
        if push:
            self._remember()  # must happen before the new article replaces self.pages
        self.article = article
        self.breaks = breaks
        self._fill_toc()
        self._set_pages(pages, block, page_offset)
        if push:
            del self.history[self.hist_i + 1:]
            self.history.append(self._current_place())
            self.hist_i = len(self.history) - 1
        return True

    def _article_error(self, error: Exception) -> None:
        self.diagnostics.record(Event.ARTICLE_READ_FAILED, error)
        self.notify(safe_text(f"could not open article: {error}")[:300],
                    severity="error", timeout=5, markup=False)

    def _read_article(self, path: str) -> Article:
        html = self.zim.html(path)
        return ArticleParser(self.pal, self.show_refs, links=not self.plain_links).parse(path, html)

    def _anchor_block(self, frag: str | None, article: Article | None = None) -> int:
        article = article if article is not None else self.article
        if not frag or not article:
            return 0
        for candidate in self._anchor_candidates(frag):
            if candidate in article.anchors:
                return article.anchors[candidate]
            # Retain support for programmatically constructed Articles.
            for i, b in enumerate(article.blocks):
                if b.anchor == candidate:
                    return i
        return 0

    @staticmethod
    def _anchor_candidates(frag: str) -> list[str]:
        decoded = unquote(frag)
        # Older MediaWiki URLs encode UTF-8 fragment bytes as .XX instead of %XX.
        legacy = unquote(re.sub(r"\.([0-9A-Fa-f]{2})", r"%\1", decoded))
        return list(dict.fromkeys((frag, decoded, decoded.replace(" ", "_"),
                                   legacy, legacy.replace(" ", "_"))))

    def _remember(self):
        """Store the current block in the current history entry before leaving it."""
        if 0 <= self.hist_i < len(self.history) and self.pages:
            self.history[self.hist_i] = self._current_place()

    @staticmethod
    def _block_pages(pages: list[Page], block: int) -> list[int]:
        return [i for i, page in enumerate(pages)
                if block in page.blocks or page.first_block == block]

    def _current_place(self) -> Place:
        block = self.pages[self.page_i].first_block if self.pages else 0
        candidates = self._block_pages(self.pages, block)
        offset = candidates.index(self.page_i) if self.page_i in candidates else 0
        return Place(self.article.path, block, offset, frozenset(self.breaks))

    def _repaginate(self, anchor_block: int | None = None):
        if not self.article:
            return
        page_offset = 0
        if anchor_block is None:
            place = self._current_place()
            anchor_block, page_offset = place.block, place.page_offset
        try:
            pages = Paginator(self.pal).paginate(self.article, self._layout(), self.breaks)
        except (RuntimeError, ValueError) as error:
            self._article_error(error)
            return
        self._set_pages(pages, anchor_block, page_offset)

    def _set_pages(self, pages: list[Page], anchor_block: int, page_offset: int = 0) -> None:
        self.pages = pages
        # Line/column positions change whenever pages are rebuilt.
        self.link_sel = None
        self.hints = None
        self.hint_buf = ""
        # A code block/table can occupy many pages. An anchor names its start,
        # not its last continuation page.
        candidates = self._block_pages(pages, anchor_block)
        self.page_i = candidates[min(max(page_offset, 0), len(candidates) - 1)] if candidates else 0
        self._show()

    def _show(self):
        if not self.pages:
            return
        p = self.pages[self.page_i]
        lines = p.lines
        if self.hints is not None:
            lines = self._hinted(lines)
        elif self.link_sel is not None:
            lines = self._selected(lines)
        body = Text("\n").join(lines) if lines else Text("")
        text = self.query_one("#text", Static)
        text.styles.width = p.width
        text.update(body)
        info = self.query_one("#info", Static)
        gutter = self.query_one("#gutter", Static)
        lay = self._layout()
        has_box = bool(lay.info_width and self.article.infobox)
        gutter.display = has_box
        info.display = bool(p.info)
        if p.info:
            info.styles.width = lay.info_width + 4
            info.update(Text("\n").join(p.info))
            first = self.pages.index(p) == 0 or not self.pages[self.page_i - 1].info
            info.border_title = None if first else "↑ continued"
            info.border_subtitle = "continues ↓" if p.info_more else None
        if has_box and not p.info:
            # keep the text column where it was; full width after the box ends
            gutter.display = False
        self._status()

    def _status(self):
        left = self.query_one("#st-left", Static)
        right = self.query_one("#st-right", Static)
        if not self.article:
            left.update(Text(safe_text(self.zim.name)))
            right.update("")
            return
        a = self.article
        sec = ""
        fb = self.pages[self.page_i].first_block if self.pages else 0
        for e in a.toc:
            if e.block <= fb and e.level == 2:
                sec = e.title
        l = Text(safe_text(a.title), Style(color=self.pal.fg))
        if sec:
            l.append(f"  ›  {safe_text(sec)}", Style(color=self.pal.muted))
        left.update(l)
        r = Text()
        if self.hints is not None:
            r.append(f"link: {self.hint_buf}_  (esc cancels)   ", Style(color=self.pal.accent, bold=True))
        elif self.link_sel is not None:
            targets = self._link_targets()
            if 0 <= self.link_sel < len(targets):
                name = self._href_title(targets[self.link_sel][1])
                r.append(f"{self.link_sel + 1}/{len(targets)} → {name}", Style(color=self.pal.accent, bold=True))
                r.append("  enter   ", Style(color=self.pal.muted))
        if self.plain_links:
            r.append("links off ", Style(color=self.pal.muted))
            r.append("l   ", Style(color=self.pal.muted))
        if a.notes:
            r.append(f"{len(a.notes)} note{'s' if len(a.notes) > 1 else ''} ", Style(color=self.pal.accent))
            r.append("a   ", Style(color=self.pal.muted))
        r.append(f"{self.page_i + 1}/{len(self.pages)}", Style(color=self.pal.fg))
        right.update(r)

    # -------------------------------------------------------------- actions

    def action_page(self, delta: int):
        if self.pages:
            self.page_i = max(0, min(len(self.pages) - 1, self.page_i + delta))
            self.link_sel = None
            self._show()

    def action_first(self):
        self.page_i = 0
        self.link_sel = None
        self._show()

    def action_last(self):
        self.page_i = max(len(self.pages) - 1, 0)
        self.link_sel = None
        self._show()

    def action_back(self):
        self._visit_history(self.hist_i - 1)

    def action_forward(self):
        self._visit_history(self.hist_i + 1)

    def _visit_history(self, index: int) -> None:
        if not (0 <= index < len(self.history)):
            return
        previous = self._current_place()
        p = self.history[index]
        if self.open(p.path, push=False, block=p.block, page_offset=p.page_offset, page_breaks=p.breaks):
            self.history[self.hist_i] = previous
            self.hist_i = index

    def action_follow(self, href: str):
        if self.plain_links or not local_href(href):
            return
        if href.startswith("#"):
            if self.article is not None:
                frag = href[1:]
                b = self._anchor_block(frag)
                known = not frag or b != 0 or any(
                    key in self.article.anchors for key in self._anchor_candidates(frag)
                )
                if known:
                    self.open(self.article.path, block=b)
                else:
                    self.notify("section not found in this article", timeout=2)
            return
        self.open(href, relative=True)

    def action_random(self):
        try:
            path = self.zim.random_path()
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            self._article_error(error)
            return
        self.open(path)

    def action_notes(self):
        if not self.article:
            return
        for i, b in enumerate(self.article.blocks):
            if b.anchor == "__notes__":
                self.jump_block(i)
                return
        self.notify("no additional notes on this article", timeout=2)

    def _reload_keep_place(self) -> bool:
        """Re-parse the current article (after a display toggle), same page."""
        if self.article:
            place = self._current_place()
            try:
                article = self._read_article(self.article.path)
                pages = Paginator(self.pal).paginate(article, self._layout(), self.breaks)
            except (OSError, RuntimeError, ValueError, KeyError) as error:
                self._article_error(error)
                return False
            self.article = article
            self._fill_toc()
            self._set_pages(pages, place.block, place.page_offset)
        return True

    def action_toggle_refs(self):
        self.show_refs = not self.show_refs
        if not self._reload_keep_place():
            self.show_refs = not self.show_refs
            return
        self.notify(f"citation markers {'on' if self.show_refs else 'off'}", timeout=1.5)

    def action_toggle_links(self):
        if self.query_one(TocPanel).display:
            self._toc_key("l")  # in the contents panel, l means "open subsection"
            return
        self.plain_links = not self.plain_links
        if not self._reload_keep_place():
            self.plain_links = not self.plain_links
            return
        self._status()
        self.notify("links off: plain text, not followable" if self.plain_links else "links on",
                    timeout=1.5)

    HELP = (
        "j/k ↓/↑ PgDn/PgUp space   page\n"
        "g / G                     first / last page\n"
        "c                         contents (j/k, enter, h/l)\n"
        "/                         search titles\n"
        "n / N                     next / previous link, enter opens\n"
        "f                         follow a link by label\n"
        "← / →  backspace          back / forward\n"
        "a                         additional notes\n"
        "l                         links off / on\n"
        "s                         citation markers\n"
        "R                         random article\n"
        "?                         this help (? or esc closes)\n"
        "q                         quit"
    )

    def action_help(self):
        panel = self.query_one(HelpPanel)
        if self._help_timer is not None:
            self._help_timer.stop()
            self._help_timer = None
        if panel.display:
            panel.display = False
            return
        panel.border_title = "keys"
        panel.update(self.HELP)
        panel.display = True
        self._help_timer = self.set_timer(12, self._close_help)

    def _close_help(self):
        self.query_one(HelpPanel).display = False
        if self._help_timer is not None:
            self._help_timer.stop()
            self._help_timer = None

    # -------------------------------------------------------------- n / N link cursor

    def _link_targets(self) -> list[tuple[list[tuple[int, int, int]], str]]:
        """Links on the current page, in reading order. A link wrapped over two
        lines is ONE target: ([(line, start, end), ...], href)."""
        if not self.pages or self.plain_links:
            return []
        runs = self._link_runs(self.pages[self.page_i].lines)
        out: list[tuple[list[tuple[int, int, int]], str]] = []
        identities: dict[int, int] = {}
        for li, start, end, href, identity in runs:
            if identity is not None:
                if identity in identities:
                    out[identities[identity]][0].append((li, start, end))
                else:
                    identities[identity] = len(out)
                    out.append(([(li, start, end)], href))
                continue
            prev = out[-1] if out else None
            if prev and prev[1] == href and prev[0][-1][0] == li - 1 and start == 0:
                prev[0].append((li, start, end))  # continuation of a wrapped link
            else:
                out.append(([(li, start, end)], href))
        return out

    def _href_title(self, href: str) -> str:
        if href.startswith("#"):
            return "§ " + safe_text(unquote(href[1:])).replace("_", " ")
        return safe_text(unquote(href.split("#", 1)[0].removeprefix("./"))).replace("_", " ")

    def action_link_step(self, delta: int):
        if self.plain_links:
            self.notify("links are off, press l to turn them back on", timeout=2)
            return
        targets = self._link_targets()
        if not targets:
            # nothing here: move to the next/previous page that has links
            start = self.page_i
            while 0 <= self.page_i + delta < len(self.pages):
                self.page_i += delta
                targets = self._link_targets()
                if targets:
                    break
            if not targets:
                self.page_i = start
                self.notify("no links", timeout=1.5)
                return
            self.link_sel = 0 if delta > 0 else len(targets) - 1
            self._show()
            return
        if self.link_sel is None:
            self.link_sel = 0 if delta > 0 else len(targets) - 1
        else:
            nxt = self.link_sel + delta
            if 0 <= nxt < len(targets):
                self.link_sel = nxt
            elif 0 <= self.page_i + delta < len(self.pages):
                # like vim's search wrapping onto the next screen: go to the
                # neighbouring page and keep walking its links
                self.page_i += delta
                t2 = self._link_targets()
                self.link_sel = (0 if delta > 0 else len(t2) - 1) if t2 else None
            else:
                self.notify("last link" if delta > 0 else "first link", timeout=1.2)
        self._show()

    def action_link_open(self):
        targets = self._link_targets()
        if self.link_sel is None or not (0 <= self.link_sel < len(targets)):
            return
        href = targets[self.link_sel][1]
        self.link_sel = None
        self.action_follow(href)

    def _selected(self, lines: list[Text]) -> list[Text]:
        targets = self._link_targets()
        if not (0 <= self.link_sel < len(targets)):
            return lines
        out = list(lines)
        sel = Style(bold=True, color=self.pal.fg, bgcolor=self.pal.accent, underline=False)
        for li, start, end in targets[self.link_sel][0]:
            ln = out[li].copy()
            ln.stylize(sel, start, end)
            out[li] = ln
        return out

    def jump_block(self, block: int):
        if block and self.article.blocks[block].kind != "infobox":
            self.breaks.add(block)
        self._repaginate(anchor_block=block)

    # -------------------------------------------------------------- TOC

    def _fill_toc(self):
        tree = self.query_one("#toc-tree", Tree)
        tree.clear()
        stack = [(1, tree.root)]
        for e in self.article.toc:
            while stack[-1][0] >= e.level:
                stack.pop()
            node = stack[-1][1].add(Text(safe_text(e.title)), data=e.block, expand=False)
            stack.append((e.level, node))
        for n in list(tree.root.children):
            if not n.children:
                n.allow_expand = False
        self._leafify(tree.root)

    def _leafify(self, node):
        for n in node.children:
            if not n.children:
                n.allow_expand = False
            else:
                self._leafify(n)

    def action_toc(self):
        panel = self.query_one(TocPanel)
        if panel.display:
            self._close_toc()
            return
        if not self.article or not self.article.toc:
            self.notify("no sections", timeout=1.5)
            return
        panel.display = True
        tree = self.query_one("#toc-tree", Tree)
        # put the cursor on the section we're reading and open its parent
        fb = self.pages[self.page_i].first_block if self.pages else 0
        target = None
        for node in self._walk_nodes(tree.root):
            if node.data is not None and node.data <= fb:
                target = node
        if target is None and tree.root.children:
            target = tree.root.children[0]
        if target is not None:
            p = target.parent
            while p is not None and p is not tree.root:
                p.expand()
                p = p.parent
            self.call_after_refresh(lambda: tree.move_cursor(target))
        tree.focus()

    def _toc_key(self, key: str):
        tree = self.query_one("#toc-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        if key == "l" and node.children:
            node.expand()
        elif key == "h":
            if node.is_expanded:
                node.collapse()
            elif node.parent is not None and node.parent is not tree.root:
                tree.move_cursor(node.parent)
                node.parent.collapse()

    def _walk_nodes(self, node):
        for n in node.children:
            yield n
            yield from self._walk_nodes(n)

    def _close_toc(self):
        self.query_one(TocPanel).display = False
        self.set_focus(None)

    @on(Tree.NodeSelected, "#toc-tree")
    def _toc_select(self, ev: Tree.NodeSelected):
        node = ev.node
        if node.children and not node.is_expanded:
            node.expand()  # first enter opens subsections, second enter jumps
            return
        self._close_toc()
        self.jump_block(node.data)

    # -------------------------------------------------------------- search

    def action_search(self):
        bar = self.query_one(SearchBar)
        bar.display = True
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self._results_for = None
        self._open_on_results = None
        self.query_one("#search-results", OptionList).clear_options()
        inp.focus()

    def _close_search(self):
        self.query_one(SearchBar).display = False
        self._open_on_results = None
        self.set_focus(None)

    @on(Input.Changed, "#search-input")
    def _search_changed(self, ev: Input.Changed):
        # debounce so a fast-typed word sends one query, not one per keystroke
        if self._search_timer is not None:
            self._search_timer.stop()
        q = ev.value
        self._search_timer = self.set_timer(0.08, lambda: self._send_query(q))

    # Queries run in a separate process (suggestd.py): libzim holds the GIL, so a
    # thread would freeze the UI for 1-2 s on short queries against the full ZIM.
    async def _start_suggestd(self):
        try:
            self._sugg_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "zimtty.suggestd", self.zim.path,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1 << 20,
            )
        except OSError as error:
            self.diagnostics.record(Event.SEARCH_START_FAILED, error)
            self._sugg_proc = None
            return
        self.run_worker(self._read_suggestd(), exclusive=False, group="suggestd")

    async def _read_suggestd(self):
        proc = self._sugg_proc
        if proc is None:
            return
        try:
            while proc.stdout:
                try:
                    line = await proc.stdout.readline()
                except (OSError, ValueError) as error:
                    # Oversized archive titles can exceed the JSON line limit.
                    # A broken suggestion pipe must not terminate the reader UI.
                    self.diagnostics.record(Event.SEARCH_PIPE_FAILED, error)
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.diagnostics.record(Event.SEARCH_PIPE_FAILED)
                    continue
                if not isinstance(msg, dict):
                    self.diagnostics.record(Event.SEARCH_PIPE_FAILED)
                    continue
                if msg.get("error") == "search_failed":
                    self.diagnostics.record(Event.SEARCH_QUERY_FAILED)
                if "results" in msg:
                    results = msg["results"]
                    if (not isinstance(msg.get("q"), str) or not isinstance(results, list)
                            or not all(isinstance(r, list) and len(r) == 2
                                       and all(isinstance(s, str) for s in r) for r in results)):
                        self.diagnostics.record(Event.SEARCH_PIPE_FAILED)
                        continue
                    self._show_results(msg["q"], [tuple(r) for r in results])
        finally:
            if self._sugg_proc is proc:
                self._sugg_proc = None  # next query falls back to in-process search
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # exited between the returncode check and kill
            await proc.wait()

    def _send_query(self, q: str):
        proc = self._sugg_proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            self._suggest_inprocess(q)
            return
        self._query_id += 1
        try:
            proc.stdin.write((json.dumps({"id": self._query_id, "q": q}) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            self.diagnostics.record(Event.SEARCH_PIPE_FAILED, error)
            self._sugg_proc = None
            self._suggest_inprocess(q)

    @work(thread=True, exclusive=True)
    def _suggest_inprocess(self, q: str):
        try:
            res = self.zim.suggest(q, 5)
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            self.diagnostics.record(Event.SEARCH_QUERY_FAILED, error)
            res = []
        self.call_from_thread(self._show_results, q, res)

    async def on_unmount(self) -> None:
        proc = getattr(self, "_sugg_proc", None)
        if proc is not None:
            self._sugg_proc = None
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()

    def _show_results(self, q: str, res: list[tuple[str, str]]):
        if self.query_one("#search-input", Input).value != q:
            return
        if self._open_on_results == q:
            # Enter was pressed before this query's results arrived
            self._open_on_results = None
            if res:
                self._close_search()
                self.open(res[0][0])
            else:
                self.notify("no matches", timeout=1.5)
            return
        ol = self.query_one("#search-results", OptionList)
        ol.clear_options()
        for path, title in res:
            ol.add_option(Option(Text(safe_text(title)), id=path))
        self._results_for = q
        if res:
            ol.highlighted = 0

    @on(Input.Submitted, "#search-input")
    def _search_submit(self, ev: Input.Submitted):
        ol = self.query_one("#search-results", OptionList)
        if self._results_for == ev.value and ol.option_count and ol.highlighted is not None:
            self._close_search()
            self.open(ol.get_option_at_index(ol.highlighted).id)
            return
        if not ev.value.strip():
            return
        # results for this exact text aren't in yet: open the top hit when they
        # arrive (never block the UI thread on a query)
        self._open_on_results = ev.value
        if self._search_timer is not None:
            self._search_timer.stop()
        self._send_query(ev.value)

    @on(OptionList.OptionSelected, "#search-results")
    def _search_pick(self, ev: OptionList.OptionSelected):
        self._close_search()
        self.open(ev.option.id)

    # -------------------------------------------------------------- link hints

    def _page_links(self, lines: list[Text]) -> list[tuple[int, int, int, str]]:
        """(line, start, end, href) for each link run on the page."""
        return [run[:4] for run in self._link_runs(lines)]

    @staticmethod
    def _link_runs(lines: list[Text]) -> list[tuple[int, int, int, str, int | None]]:
        """Retain each source link's identity through wrapping and table columns."""
        out = []
        for li, line in enumerate(lines):
            runs: list[list] = []
            for sp in sorted(line.spans, key=lambda s: s.start):
                st = sp.style
                href = st.meta.get("href") if isinstance(st, Style) and st.meta else None
                if not href:
                    continue
                identity = st.meta.get("link_id")
                if (runs and runs[-1][3:] == [href, identity]
                        and sp.start <= runs[-1][2]):
                    runs[-1][2] = max(runs[-1][2], sp.end)
                else:
                    runs.append([li, sp.start, sp.end, href, identity])
            out.extend(tuple(r) for r in runs)
        return out

    @staticmethod
    def _labels(n: int) -> list[str]:
        keys = HINT_KEYS
        if n <= len(keys):
            return list(keys[:n])
        return [a + b for a in keys for b in keys][:n]

    def action_hints(self):
        if not self.pages:
            return
        if self.plain_links:
            self.notify("links are off, press l to turn them back on", timeout=2)
            return
        links = [(runs[0][0], runs[0][1], runs[0][2], href)
                 for runs, href in self._link_targets()]
        if not links:
            self.notify("no links on this page", timeout=1.5)
            return
        labels = self._labels(len(links))
        self.hints = {lab: link for lab, link in zip(labels, links)}
        self.hint_buf = ""
        self._show()

    def _hinted(self, lines: list[Text]) -> list[Text]:
        by_line: dict[int, list[tuple[int, str]]] = {}
        for lab, (li, start, _end, _h) in self.hints.items():
            if lab.startswith(self.hint_buf):
                by_line.setdefault(li, []).append((start, lab))
        out = []
        tag = Style(bold=True, color=self.pal.fg, bgcolor=self.pal.accent)
        for li, line in enumerate(lines):
            if li not in by_line:
                out.append(line)
                continue
            new = line.copy()
            # Work right-to-left because a wide character may require padding
            # after replacing it with an ASCII hint, changing string offsets.
            for start, lab in sorted(by_line[li], reverse=True):
                end = min(start + len(lab), len(new.plain))
                cells = new[start:end].cell_len
                label = lab[:min(end - start, cells)]
                replacement = Text(label + " " * max(0, cells - len(label)), tag)
                new = new[:start] + replacement + new[end:]
            out.append(new)
        return out

    def on_key(self, event: Key) -> None:
        if self.hints is None:
            if event.key == "escape":
                if self.query_one(SearchBar).display:
                    self._close_search()
                    event.stop()
                elif self.query_one(TocPanel).display:
                    self._close_toc()
                    event.stop()
                elif self.query_one(HelpPanel).display:
                    self._close_help()
                    event.stop()
                elif self.link_sel is not None:
                    self.link_sel = None
                    self._show()
                    event.stop()
            elif self.query_one(SearchBar).display and event.key in ("up", "down"):
                ol = self.query_one("#search-results", OptionList)
                if ol.option_count:
                    cur = ol.highlighted or 0
                    ol.highlighted = (cur + (1 if event.key == "down" else -1)) % ol.option_count
                event.stop()
                event.prevent_default()
            elif self.query_one(TocPanel).display and event.key in ("c", "q"):
                self._close_toc()
                event.stop()
            elif self.query_one(TocPanel).display and event.key in ("l", "h"):
                self._toc_key(event.key)
                event.stop()
                event.prevent_default()
            return
        # link-hint mode swallows keys
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.hints = None
            self._show()
            return
        ch = event.character or ""
        if not ch or ch not in HINT_KEYS:
            return
        self.hint_buf += ch
        matches = [lab for lab in self.hints if lab.startswith(self.hint_buf)]
        if not matches:
            self.hint_buf = ""
        elif len(matches) == 1 and matches[0] == self.hint_buf:
            href = self.hints[matches[0]][3]
            self.hints = None
            self.action_follow(href)
            return
        self._show()

    # -------------------------------------------------------------- theme

    def _check_theme(self):
        m = omatheme.colors_mtime()
        if m == self._theme_mtime:
            return
        self._theme_mtime = m
        try:
            pal, theme = omatheme.build(omatheme.read_colors(strict=True))
        except omatheme.ThemeError as error:
            self.diagnostics.record(Event.THEME_INVALID, error)
            self.notify("Theme update ignored: invalid colors.", severity="warning", timeout=3)
            return
        self.pal, self._theme = pal, theme
        self.register_theme(self._theme)
        self.theme = "omarchy"
        self.refresh_css()
        self._reload_keep_place()


# ------------------------------------------------------------------ entry


def find_zim(arg: str | None) -> str:
    cands: list[Path] = []
    if arg:
        cands.append(Path(arg).expanduser())
    if os.environ.get("ZIMTTY_ZIM"):
        cands.append(Path(os.environ["ZIMTTY_ZIM"]).expanduser())
    for d in (Path.home() / "Downloads/zim", Path.home() / ".local/share/zimtty"):
        if d.is_dir():
            cands.extend(sorted(d.glob("*.zim"), key=lambda p: p.stat().st_size, reverse=True))
    for p in cands:
        try:
            Zim(str(p))
            return str(p)
        except Exception:
            continue  # e.g. a ZIM that is still downloading
    sys.exit("zimtty: no readable .zim found (pass a path, set ZIMTTY_ZIM, or put one in ~/Downloads/zim)")


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes unknown options, which may be private input.
        super().error("invalid arguments; use --help")


def main():
    parser = _Arguments(prog="zimtty", description="Offline terminal reader for ZIM files.")
    parser.add_argument("--diagnostics", action="store_true",
                        help="print up to 32 private event codes on exit; no log file")
    parser.add_argument("targets", nargs="*", metavar="ZIM_OR_TITLE",
                        help="optional .zim path and article title (use -- before titles starting with -)")
    args = parser.parse_intermixed_args()
    zim_arg = next((a for a in args.targets if a.endswith(".zim")), None)
    rest = list(args.targets)
    if zim_arg is not None:
        rest.remove(zim_arg)
    start = " ".join(rest) or None
    trace = DiagnosticTrace()
    trace.record(Event.SESSION_STARTED)
    app = None
    try:
        zim = Zim(find_zim(zim_arg))
        app = ZimTTY(zim, start, trace=trace)
        app.run()
        if app.return_code:
            raise SystemExit(app.return_code)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        trace.record(Event.STARTUP_FAILED if app is None else Event.INTERNAL_ERROR, error)
        print("zimtty: could not run the reader; use --diagnostics for a short trace.", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        trace.record(Event.SESSION_STOPPED)
        if args.diagnostics:
            print(trace.render(), file=sys.stderr)
        trace.clear()
