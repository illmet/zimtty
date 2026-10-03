"""Turn an Article into screen-sized pages.

Rules (agreed design):
  * paragraphs that fit a fresh page move there whole, unless keeping a
    heading with its following text requires a split
  * paragraphs taller than a whole page are split at a sentence boundary,
    falling back to a line boundary only if one sentence is itself too tall
  * headings are kept with the start of what follows them
  * every h2 (and any heading the user jumped to via the TOC) starts a page
  * the infobox sits in its own box on the right; if it is taller than a
    page it continues on the next page; once it runs out the text goes full width
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from rich.cells import cell_len, chop_cells
from rich.console import Console
from rich.style import Style
from rich.text import Text

from .zimdoc import Article, Block, InfoRow, Palette, TableData

_console = Console(width=400, color_system=None, legacy_windows=False, emoji=False)

SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"“(\[]?[A-Z0-9])")
TIGHT = {"item", "ref"}  # consecutive list items get no blank line between them
HEADINGS = {"title", "h2", "h3", "h4"}
NUMBER_TOKEN = re.compile(r"(?<![\w.])[+−-]?\d+(?:[,.]\d+)*(?:[%‰])?(?![\w.])")


def wrap(text: Text, width: int) -> list[Text]:
    if width < 4:
        width = 4
    t = text.copy()
    t.end = ""
    out = []
    for ln in t.wrap(_console, width, overflow="fold"):
        ln.rstrip()  # rich can leave a trailing space that pushes wide (CJK) lines over
        if ln.cell_len > width:
            ln.truncate(width)
        out.append(ln)
    return out


def fold_preserving(text: Text, width: int) -> list[Text]:
    """Fold one logical line without stripping spaces or losing styled spans."""
    parts = chop_cells(text.plain, max(width, 4))
    if not parts:  # empty lines and standalone zero-width marks are still content
        return [text.copy()]
    out = []
    offset = 0
    for part in parts:
        out.append(text[offset:offset + len(part)])
        offset += len(part)
    return out


class _PreFlow:
    """Consume preformatted logical lines, retaining exact source offsets."""

    def __init__(self, text: Text):
        expanded = text.copy()
        expanded.expand_tabs(8)
        self.lines = expanded.split("\n", allow_blank=True)
        self.line = 0
        self.offset = 0
        self.width: int | None = None
        self.pending: deque[Text] = deque()

    @property
    def done(self) -> bool:
        return self.line >= len(self.lines)

    def take(self, width: int, count: int) -> list[Text]:
        out = []
        while not self.done and len(out) < count:
            if self.width != width or not self.pending:
                self.pending = deque(fold_preserving(self.lines[self.line][self.offset:], width))
                self.width = width
            part = self.pending.popleft()
            out.append(part)
            self.offset += len(part.plain)
            if not self.pending:
                self.line += 1
                self.offset = 0
        return out


class _TableMeasure:
    """Bounded layout comparisons used only when changing column allocation."""

    def __init__(self, table: TableData, width: int):
        self.rows = [row for i, row in enumerate(table.rows) if i not in table.full_width_rows]
        self.width = width
        self.header = self.rows[0] if self.rows and all(c.header for c in self.rows[0]) else []
        self.available = (len(self.rows) <= 128 and sum(map(len, self.rows)) <= 512
                          and sum(len(c.text.plain) for row in self.rows for c in row) <= 40_000)
        self.calls = 2048
        self.characters = 250_000
        self.cache: dict[tuple[int, int, bool], int] = {}

    def _height(self, text: Text, width: int, pre: bool = False, cache: bool = True) -> int | None:
        key = (id(text), width, pre)
        if cache and key in self.cache:
            return self.cache[key]
        if not self.available or self.calls <= 0 or len(text.plain) > self.characters:
            return None
        self.calls -= 1
        self.characters -= len(text.plain)
        expanded = text.copy()
        expanded.expand_tabs(8)
        height = (sum(len(fold_preserving(line, width)) for line in expanded.split("\n", allow_blank=True))
                  if pre else len(wrap(expanded, width)))
        if cache:
            self.cache[key] = height
        return height

    def grid(self, columns: list[int]) -> int | None:
        height = int(bool(self.header))  # header rule
        for row in self.rows:
            heights = [self._height(cell.text, width, cell.preformatted)
                       for cell, width in zip(row, columns)]
            if any(value is None for value in heights):
                return None
            height += max(heights, default=1)
        return height

    def records(self) -> int | None:
        height = max(0, 2 * len(self.rows) - 1)  # Row labels and inter-row gaps
        for ri, row in enumerate(self.rows):
            for ci, cell in enumerate(row):
                label = self.header[ci].text if self.header and ri else Text(f"Cell {ci + 1}")
                if cell.preformatted:
                    label_height = self._height(label + Text(":"), self.width, cache=False)
                    value_height = self._height(cell.text, self.width, True)
                    if label_height is None or value_height is None:
                        return None
                    height += label_height + value_height
                else:
                    value_height = self._height(label + Text(": ") + cell.text, self.width, cache=False)
                    if value_height is None:
                        return None
                    height += value_height
        return height


@dataclass
class Page:
    lines: list[Text]
    info: list[Text]  # infobox lines shown on this page (empty => no box)
    info_more: bool  # infobox continues on the next page
    first_block: int
    width: int
    blocks: set[int] = field(default_factory=set)


@dataclass
class Layout:
    height: int  # text rows per page
    full_width: int  # text width when no infobox
    narrow_width: int  # text width beside the infobox
    info_width: int  # inner width of the infobox (0 = no side box)


class Paginator:
    def __init__(self, pal: Palette):
        self.pal = pal
        self._cache: dict[tuple[int, int], list[Text]] = {}
        self._table_widths: dict[tuple[int, int], list[int] | None] = {}
        self._table_headers: dict[int, int | None] = {}

    # ---------------------------------------------------------- rendering

    def render_block(self, b: Block, width: int, text: Text | None = None, cont: bool = False) -> list[Text]:
        """cont=True: this is a continuation piece of a block split across pages."""
        if text is None:
            key = (id(b), width)
            hit = self._cache.get(key)
            if hit is None:
                hit = self._cache[key] = self._render_block(b, width, b.text, False)
            return hit
        return self._render_block(b, width, text, cont)

    def _render_block(self, b: Block, width: int, t: Text, cont: bool) -> list[Text]:
        pal = self.pal
        if b.kind == "pre":
            flow = _PreFlow(t)
            pad = min(b.indent, max(width // 4, 0), max(width - 4, 0))
            return [Text(" " * pad) + line for line in flow.take(
                width - pad, sum(len(line.plain) + 1 for line in flow.lines)
            )]
        if b.kind == "table" and b.table is not None:
            out = wrap(b.table.caption, width) if b.table.caption.plain else []
            for row in range(len(b.table.rows)):
                out.extend(self._table_row(b.table, row, width))
            return out
        if b.kind == "title":
            head = t.copy()
            head.stylize(Style(bold=True, color=pal.heading))
            return wrap(head, width) + [Text("━" * min(width, max(len(t.plain), 12)), Style(color=pal.accent))]
        if b.kind == "h2":
            head = t.copy()
            head.stylize(Style(bold=True, color=pal.heading))
            return wrap(head, width) + [Text("─" * width, Style(color=pal.muted))]
        if b.kind == "h3":
            head = t.copy()
            head.stylize(Style(bold=True, color=pal.sub))
            return wrap(head, width)
        if b.kind == "h4":
            head = t.copy()
            head.stylize(Style(bold=True, italic=True, color=pal.fg))
            return wrap(head, width)
        if b.kind in ("item", "ref"):
            # hanging indent: wrapped lines align under the text, not the bullet.
            # A continuation piece (split across pages) has no bullet: indent it all.
            # Deep nesting is clamped so tiny windows keep >= ~half the width for text.
            hang = min(b.hang, max(width // 4, 1))
            pad = " " * min(b.indent, max(width // 4, 0))
            if cont:
                head, body = Text(" " * hang), t
            else:
                head, body = t[:b.hang], t[b.hang:]
                if head.cell_len > hang:
                    head = head[:hang]
            inner = wrap(body, width - len(pad) - hang)
            return [Text(pad) + (head if k == 0 else Text(" " * hang)) + x for k, x in enumerate(inner)]
        if b.indent:
            pad = min(b.indent, max(width // 4, 0))
            return [Text(" " * pad) + x for x in wrap(t, width - pad)]
        return wrap(t, width)

    def _following_height(self, b: Block, width: int, limit: int) -> int:
        """Measure enough of the next block to decide heading placement."""
        if b.kind == "pre":
            pad = min(b.indent, max(width // 4, 0), max(width - 4, 0))
            return len(_PreFlow(b.text).take(width - pad, limit))
        if b.kind == "table" and b.table is not None:
            height = len(wrap(b.table.caption, width)) + 1 if b.table.caption.plain else 0
            for row in range(len(b.table.rows)):
                height += len(self._table_row(b.table, row, width))
                if height >= limit:
                    return limit
                if self._columns(b.table, width) is None:
                    height += 1
            return height
        return min(limit, len(self.render_block(b, width)))

    def _columns(self, table: TableData, width: int) -> list[int] | None:
        """Bounded column measurement; wide/irregular tables use record layout."""
        key = (id(table), width)
        if key in self._table_widths:
            return self._table_widths[key]
        rows = [row for index, row in enumerate(table.rows) if index not in table.full_width_rows]
        count = len(rows[0]) if rows else 0
        result = None
        if (table.simple and width >= 32 and 0 < count <= 16
                and all(len(row) == count for row in rows)
                and count * 4 + (count - 1) * 3 <= width):
            preferred = [4] * count
            minimum = [4] * count
            for row in rows:
                for i, cell in enumerate(row):
                    preferred[i] = max(preferred[i], min(width, max(
                        (line.cell_len for line in cell.text.split("\n")), default=0
                    )))
                    if not cell.preformatted:
                        minimum[i] = max(minimum[i], max(
                            (min(width, cell_len(match.group())) for match in NUMBER_TOKEN.finditer(cell.text.plain)),
                            default=4,
                        ))
            result = [min(8, size) for size in preferred]
            budget = width - (count - 1) * 3
            recovering = sum(result) > budget
            measure = None
            if sum(minimum) > budget:
                result = None
            else:
                # Preserve established layouts. Only recover a rejected grid
                # or borrow column space when a numeric token would be folded.
                if recovering:
                    result = minimum.copy()
                remaining = budget - sum(result)
                active = [i for i in range(count) if result[i] < preferred[i]]
                while remaining and active:
                    share = max(1, remaining // len(active))
                    for i in active:
                        extra = min(share, preferred[i] - result[i], remaining)
                        result[i] += extra
                        remaining -= extra
                    active = [i for i in active if result[i] < preferred[i]]
                for i in range(count):
                    while result[i] < minimum[i]:
                        if measure is None:
                            measure = _TableMeasure(table, width)
                        donors = [j for j in range(count) if result[j] > minimum[j]]
                        donor = max(donors, key=lambda j: result[j] - minimum[j])
                        measured = []
                        for j in donors:
                            candidate = result.copy()
                            transfer = min(minimum[i] - result[i], result[j] - minimum[j])
                            candidate[i] += transfer
                            candidate[j] -= transfer
                            cost = measure.grid(candidate)
                            if cost is not None:
                                measured.append((cost, -(result[j] - minimum[j]), j))
                        if measured:
                            donor = min(measured)[2]
                        take = min(minimum[i] - result[i], result[donor] - minimum[donor])
                        if take <= 0:
                            break  # minima fit the budget; defensive for malformed models
                        result[donor] -= take
                        result[i] += take
                if recovering:
                    if measure is None:
                        measure = _TableMeasure(table, width)
                    grid_height, record_height = measure.grid(result), measure.records()
                    if grid_height is None or record_height is None or grid_height > record_height:
                        result = None
        self._table_widths[key] = result
        return result

    def _table_header_index(self, table: TableData) -> int | None:
        key = id(table)
        if key not in self._table_headers:
            index = next((i for i, row in enumerate(table.rows)
                          if i not in table.full_width_rows), None) if table.simple else None
            self._table_headers[key] = (index if index is not None and table.rows[index]
                                       and all(cell.header for cell in table.rows[index]) else None)
        return self._table_headers[key]

    def _table_row(self, table: TableData, index: int, width: int) -> list[Text]:
        row = table.rows[index]
        columns = self._columns(table, width)
        cells = []
        for cell in row:
            text = cell.text.copy()
            text.expand_tabs(8)
            if cell.header:
                text.stylize(Style(bold=True, color=self.pal.heading))
            cells.append(text)
        def cell_lines(text: Text, column_width: int, preformatted: bool) -> list[Text]:
            if preformatted:
                return [part for line in text.split("\n", allow_blank=True)
                        for part in fold_preserving(line, column_width)]
            return wrap(text, column_width)

        if index in table.full_width_rows:
            return [line for cell, text in zip(row, cells)
                    for line in cell_lines(text, width, cell.preformatted)]

        header_index = self._table_header_index(table)
        if columns is not None:
            wrapped = [cell_lines(text, col, cell.preformatted)
                       for cell, text, col in zip(row, cells, columns)]
            out = []
            separator = Text(" │ ", Style(color=self.pal.muted))
            for line in range(max((len(parts) for parts in wrapped), default=1)):
                pieces = []
                for parts, col in zip(wrapped, columns):
                    part = parts[line].copy() if line < len(parts) else Text("")
                    part.pad_right(max(0, col - part.cell_len))
                    pieces.append(part)
                out.append(separator.join(pieces))
            if index == header_index:
                out.append(Text("─" * (sum(columns) + 3 * (len(columns) - 1)),
                                Style(color=self.pal.muted)))
            return out
        # Complex spans have no inferred column labels: preserve source order.
        out = [Text(f"Row {index + 1}", Style(color=self.pal.muted))]
        headers = table.rows[header_index] if header_index is not None and index > header_index else []
        for i, text in enumerate(cells):
            label = (headers[i].text.copy() if i < len(headers)
                     else Text(f"Cell {i + 1}"))
            label.stylize(Style(bold=True, color=self.pal.muted))
            if row[i].preformatted:
                out.extend(wrap(label + Text(":"), width))
                out.extend(cell_lines(text, width, True))
            else:
                out.extend(wrap(label + Text(": ") + text, width))
        return out

    def render_info(self, rows: list[InfoRow], width: int) -> list[Text]:
        pal = self.pal
        out: list[Text] = []
        # label column: fits the longest label *word* so nothing is split mid-word
        # ("Presiden/t"), capped at 45% of the box; longer words overflow onto
        # their own full-width line instead. Short labels like "Preceded by"
        # get the room to stay on one line when the box allows it.
        words = [len(w) + (2 if r.label.plain.startswith("  ") else 0)
                 for r in rows if r.kind == "pair" and r.value.plain for w in r.label.plain.split()]
        whole = [len(r.label.plain) for r in rows if r.kind == "pair" and r.value.plain]
        cap = int(width * 0.45)
        fitting = [w for w in words if w <= cap]
        lw = max(8, max(fitting, default=8))
        short_whole = [w for w in whole if w <= min(cap, 13)]
        lw = max(lw, max(short_whole, default=0))
        vw = width - lw - 1

        def centred(t: Text, style: Style):
            for part in t.split("\n"):
                if not part.plain.strip():
                    continue
                part.stylize(style)
                for ln in wrap(part, width):
                    ln.align("center", width)
                    if ln.cell_len > width:  # align() pads by len, not cells (e.g. Burmese)
                        ln = wrap(Text(ln.plain.strip(), style=style), width)[0]
                    out.append(ln)

        for r in rows:
            if r.kind == "title":
                centred(r.value.copy(), Style(bold=True, color=pal.heading))
            elif r.kind == "subtitle":
                centred(r.value.copy(), Style(italic=True, color=pal.muted))
            elif r.kind == "header":
                if out and out[-1].plain:
                    out.append(Text(""))
                t = r.value.copy()
                t.stylize(Style(bold=True, color=pal.accent))
                out.extend(wrap(t, width))
            elif r.kind == "pair" and not r.value.plain.strip():
                # a label whose details follow on the next rows (e.g. "Legislature")
                lab = r.label.copy()
                lab.stylize(Style(bold=True, color=pal.fg))
                out.extend(wrap(lab, width))
            elif r.kind == "pair":
                lab = r.label.copy()
                lab.stylize(Style(bold=True, color=pal.muted))
                vl = []
                for part in r.value.split("\n"):
                    if part.plain.strip():
                        vl.extend(wrap(part, vw))
                if any(len(w) + (len(lab.plain) - len(lab.plain.lstrip())) > lw for w in lab.plain.split()):
                    # label word too long for the column: label on its own line
                    out.extend(wrap(lab, width))
                    out.extend(Text(" " * (lw + 1)) + v for v in vl)
                    continue
                ind = len(lab.plain) - len(lab.plain.lstrip())
                if ind:  # sub-row: keep wrapped label lines indented too
                    ll = [Text(" " * ind) + x for x in wrap(lab[ind:], lw - ind)]
                else:
                    ll = wrap(lab, lw)
                for i in range(max(len(ll), len(vl))):
                    a = ll[i].copy() if i < len(ll) else Text("")
                    a.pad_right(lw - a.cell_len)
                    out.append(a + Text(" ") + (vl[i] if i < len(vl) else Text("")))
            else:  # full-width row; an empty one is a deliberate gap
                if not r.value.plain.strip():
                    if out and out[-1].plain:
                        out.append(Text(""))
                    continue
                for part in r.value.split("\n"):
                    if part.plain.strip():
                        out.extend(wrap(part, width))
        while out and not out[-1].plain.strip():
            out.pop()
        return out

    # ---------------------------------------------------------- pagination

    def _split_sentences(self, t: Text, width: int, avail: int, b: Block, cont: bool) -> tuple[Text | None, Text]:
        """Largest prefix of whole sentences whose rendering fits in avail lines."""
        cuts = [m.end() for m in SENTENCE_END.finditer(t.plain)]
        best = None
        lo, hi = 0, len(cuts) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if len(self.render_block(b, width, t[: cuts[mid]], cont)) <= avail:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            return None, t
        head = t[: cuts[best]]
        head.rstrip()
        return head, t[cuts[best]:]

    def paginate(self, art: Article, lay: Layout, breaks: set[int]) -> list[Page]:
        self._cache = {}
        self._table_widths = {}
        self._table_headers = {}
        H = max(lay.height, 4)
        info_lines = self.render_info(art.infobox, lay.info_width) if (art.infobox and lay.info_width) else []
        info_per_page = max(H - 2, 1)  # box border takes 2 rows

        pages: list[Page] = []

        def new_page(first_block: int) -> Page:
            pi = len(pages)
            start = pi * info_per_page
            chunk = info_lines[start: start + info_per_page]
            more = start + info_per_page < len(info_lines)
            width = lay.narrow_width if chunk else lay.full_width
            p = Page(lines=[], info=chunk, info_more=more, first_block=first_block, width=width)
            pages.append(p)
            return p

        page = new_page(0)
        prev_kind: str | None = None
        blocks = art.blocks

        def place_lines(lines: list[Text], block: int, before: int = 0,
                        repeat: list[Text] | None = None) -> None:
            """Place an already rendered row without treating decoration as text."""
            nonlocal page
            offset = 0
            while offset < len(lines):
                available = H - len(page.lines) - before
                if available <= 0:
                    page = new_page(block)
                    before = 0
                    if repeat and len(repeat) <= H // 3:
                        self._put(page, repeat, 0, block)
                    available = H - len(page.lines)
                chunk = lines[offset:offset + available]
                self._put(page, chunk, before, block)
                offset += len(chunk)
                before = 0

        for i, b in enumerate(blocks):
            is_heading = b.kind in HEADINGS
            forced = (b.kind == "h2" or i in breaks) and i != 0
            if forced and page.lines:
                page = new_page(i)
                prev_kind = None

            def gap() -> int:
                if not page.lines:
                    return 0
                return 0 if (b.kind in TIGHT and prev_kind == b.kind) else 1

            if b.kind == "infobox":
                if not lay.info_width:
                    place_lines(self.render_info(art.infobox, page.width), i, gap())
                    prev_kind = b.kind
                continue

            if b.kind == "pre":
                flow = _PreFlow(b.text)
                before = gap()
                while not flow.done:
                    available = H - len(page.lines) - before
                    if available <= 0:
                        page = new_page(i)
                        before = 0
                        available = H
                    pad = min(b.indent, max(page.width // 4, 0), max(page.width - 4, 0))
                    lines = [Text(" " * pad) + line
                             for line in flow.take(page.width - pad, available)]
                    self._put(page, lines, before, i)
                    before = 0
                prev_kind = b.kind
                continue

            if b.kind == "table" and b.table is not None:
                table = b.table
                header_index = self._table_header_index(table)
                first_data_index = next((row for row in range((header_index or 0) + 1, len(table.rows))
                                         if row not in table.full_width_rows), None)
                header_cache: dict[int, list[Text]] = {}

                def header_for(width: int) -> list[Text]:
                    if width not in header_cache:
                        header = (self._table_row(table, header_index, width)
                                  if header_index is not None
                                  and self._columns(table, width) is not None else [])
                        header_cache[width] = header if len(header) <= H // 3 else []
                    return header_cache[width]

                before = gap()
                initial_header_page: Page | None = None
                if table.caption.plain:
                    caption = table.caption.copy()
                    caption.stylize(Style(bold=True, color=self.pal.heading))
                    place_lines(wrap(caption, page.width), i, before)
                    before = 1
                previous_width = page.width
                for row in range(len(table.rows)):
                    # Finish a split row at its original column widths; a wider
                    # subsequent row starts on a fresh page with its own header.
                    if previous_width != page.width and page.lines:
                        page = new_page(i)
                        before = 0
                    width = page.width
                    lines = self._table_row(table, row, width)
                    repeat_needed = (header_index is not None and row > header_index
                                     and row not in table.full_width_rows)
                    repeat = header_for(width) if repeat_needed else []
                    available = H - len(page.lines) - before
                    if (row == header_index and first_data_index is not None
                            and page.lines
                            and not (prev_kind in HEADINGS and row == 0 and not table.caption.plain)):
                        first_data = self._table_row(table, first_data_index, width)
                        keep = len(first_data) if len(lines) + len(first_data) <= H else 1
                        if len(lines) + keep > available:
                            page = new_page(i)
                            before = 0
                            width = page.width
                            lines = self._table_row(table, row, width)
                            available = H
                    if page.lines and len(lines) > available:
                        fresh_width = (lay.full_width if len(pages) * info_per_page >= len(info_lines)
                                       else lay.narrow_width)
                        fresh = self._table_row(table, row, fresh_width)
                        fresh_header = header_for(fresh_width) if repeat_needed else []
                        # Ordinary rows remain intact. Oversized rows use any
                        # remaining space; a first row may split to stay with
                        # its preceding heading or caption.
                        keep_start = ((row == 0 and (prev_kind in HEADINGS or bool(table.caption.plain)))
                                      or (row == first_data_index and page is initial_header_page))
                        if (len(fresh) + len(fresh_header) <= H and not keep_start) or available <= 0:
                            page = new_page(i)
                            before = 0
                            width, lines, repeat = fresh_width, fresh, fresh_header
                    if not page.lines and repeat:
                        self._put(page, repeat, 0, i)
                    place_lines(lines, i, before, repeat)
                    if row == header_index:
                        initial_header_page = page
                    previous_width = width
                    before = 0 if self._columns(table, width) is not None else 1
                prev_kind = b.kind
                continue

            lines = self.render_block(b, page.width)
            if is_heading:
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                next_height = self._following_height(nxt, page.width, H + 1) if nxt else 0
                keep = min(2, next_height) + 1 if nxt else 0
                if len(lines) + 1 + next_height <= H and nxt:
                    keep = next_height + 1
                if page.lines and len(page.lines) + gap() + len(lines) + keep > H:
                    page = new_page(i)
                    lines = self.render_block(b, page.width)
                # Keep the final heading line (and its rule) with the start of
                # the following block, even when the heading itself is huge.
                tail = 2 if b.kind in ("title", "h2") else 1
                while len(lines) + keep > H and len(lines) > tail:
                    count = min(H, len(lines) - tail)
                    self._put(page, lines[:count], gap(), i)
                    lines = lines[count:]
                    page = new_page(i)
                self._put(page, lines, gap(), i)
                prev_kind = b.kind
                continue

            if len(page.lines) + gap() + len(lines) <= H:
                self._put(page, lines, gap(), i)
                prev_kind = b.kind
                continue

            # does it fit on a fresh page?
            if page.lines:
                follows_heading = prev_kind in HEADINGS and H - len(page.lines) - gap() > 0
                fresh_w = lay.full_width if len(pages) * info_per_page >= len(info_lines) else lay.narrow_width
                fresh = self.render_block(b, fresh_w)
                if len(fresh) <= H and not follows_heading:
                    page = new_page(i)
                    self._put(page, self.render_block(b, page.width), 0, i)
                    prev_kind = b.kind
                    continue
                # oversized: start it here if there is real room, else on a new page
                if H - len(page.lines) - gap() < 4 and not follows_heading:
                    page = new_page(i)

            # split an oversized block across pages
            rest = b.text
            cont = False
            while True:
                avail = H - len(page.lines) - gap()
                lines = self.render_block(b, page.width, rest, cont)
                if len(lines) <= avail:
                    self._put(page, lines, gap(), i)
                    break
                head, tail = self._split_sentences(rest, page.width, avail, b, cont)
                if head is not None and head.plain.strip():
                    self._put(page, self.render_block(b, page.width, head, cont), gap(), i)
                    rest = tail
                else:
                    # a single sentence taller than the space: cut on a line
                    self._put(page, lines[:avail], gap(), i)
                    # non-space chars shown (bullet included when not cont, and rest has it too)
                    consumed = sum(len(re.sub(r"\s+", "", x.plain)) for x in lines[:avail])
                    rest = self._drop_chars(rest, consumed)
                cont = True
                page = new_page(i)
            prev_kind = b.kind

        # infobox longer than the whole article: give it extra pages
        while len(pages) * info_per_page < len(info_lines):
            new_page(len(blocks) - 1)
        if info_lines:
            info_blocks = {i for i, block in enumerate(blocks) if block.kind == "infobox"}
            for p in pages:
                if p.info:
                    p.blocks.update(info_blocks)
        return pages

    @staticmethod
    def _drop_chars(t: Text, target: int) -> Text:
        """Drop the first `target` non-space characters (plus following spaces)."""
        plain = t.plain
        seen = i = 0
        while i < len(plain) and seen < target:
            if not plain[i].isspace():
                seen += 1
            i += 1
        while i < len(plain) and plain[i].isspace():
            i += 1
        return t[i:]

    @staticmethod
    def _put(page: Page, lines: list[Text], gap: int, block: int):
        if gap:
            page.lines.append(Text(""))
        page.lines.extend(lines)
        page.blocks.add(block)
