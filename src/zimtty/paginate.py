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
  * without room for that box, the infobox is framed in the text, folded to
    a card of its first section; expanded, it continues across pages like
    the side box
  * table rows stay whole unless taller than a page; header rows repeat on
    each page of a grid, and a merged cell continuing onto a new page is
    repeated (muted) on its first row
  * tables too wide for their columns become records labelled by the headers
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from rich.cells import cell_len, chop_cells
from rich.console import Console
from rich.style import Style
from rich.text import Text

from .zimdoc import (Article, Block, InfoRow, Palette, TableCell, TableData, header_rows,
                     place_cells, rule_breaks)

_console = Console(width=400, color_system=None, legacy_windows=False, emoji=False)

SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"“(\[]?[A-Z0-9])")
TIGHT = {"item", "ref"}  # consecutive list items get no blank line between them
HEADINGS = {"title", "h2", "h3", "h4"}
NUMBER_TOKEN = re.compile(r"(?<![\w.])[+−-]?\d+(?:[,.]\d+)*(?:[%‰])?(?![\w.])")
LIST_ITEM = re.compile(r"[•◦]|\d+\. ")  # list markers added by the parser
RUNNING_TEXT = 40  # average longest line of the values in a column of sentences
TEXT_COLUMN = 14  # narrowest grid column for running text
INFO_CARD = 12  # most lines of an infobox card in the text (and at most a third of a page)


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


def joined(separator: Text, parts: list[Text]) -> Text:
    """Text.join, except the separator's style stays on the separators
    instead of becoming the base style of every part."""
    out = Text()
    for k, part in enumerate(parts):
        if k:
            out.append_text(separator)
        out.append_text(part)
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


class _Grid:
    """Cell positions of a table that follows the HTML table model.

    Every slot of a non-band row refers to the cell covering it, so a merged
    cell appears once per covered slot and starts in exactly one row.
    """

    def __init__(self, table: TableData, starts: list[list[int]], width: int):
        bands = table.full_width_rows
        self.width = width
        self.header = header_rows(table.rows, bands)
        self.slots: list[list[TableCell | None]] = [
            [] if r in bands else [None] * width for r in range(len(table.rows))]
        self.origin: dict[int, int] = {}  # id(cell) -> the row it starts in
        self.cells: list[tuple[int, TableCell, int, int]] = []  # (row, cell, start, end)
        self.carried: set[int] = set()  # rows covered by a cell from an earlier row
        self._labels: dict[tuple[int, int], Text | None] = {}
        for r, (row, row_starts) in enumerate(zip(table.rows, starts)):
            if r in bands:
                continue
            for cell, start in zip(row, row_starts):
                self.origin[id(cell)] = r
                self.cells.append((r, cell, start, start + cell.colspan))
                for covered in range(r, min(r + cell.rowspan, len(table.rows))):
                    if covered not in bands:
                        self.slots[covered][start:start + cell.colspan] = [cell] * cell.colspan
                        if covered != r:
                            self.carried.add(covered)

    def segments(self, r: int) -> list[tuple[TableCell | None, int, int, bool]]:
        """(cell, start, end, starts_here) for each run of slots sharing a cell."""
        row = self.slots[r]
        out = []
        start = 0
        while start < self.width:
            cell = row[start]
            end = start + 1
            while cell is not None and end < self.width and row[end] is cell:
                end += 1
            out.append((cell, start, end, cell is not None and self.origin[id(cell)] == r))
            start = end
        return out

    def continued(self, r: int) -> bool:
        """The first column continues a data cell from an earlier row (a sub-row)."""
        row = self.slots[r]
        if not row or row[0] is None:
            return False
        origin = self.origin[id(row[0])]
        return origin != r and origin not in self.header

    def title(self, cell: TableCell) -> bool:
        """A header cell spanning the whole table names no particular column."""
        return self.width > 1 and cell.colspan == self.width

    def label(self, start: int, end: int) -> Text | None:
        """Header text covering these columns, outermost first. Without a
        header over the whole span, each column's own label is listed."""
        key = (start, end)
        if key not in self._labels:
            found: list[TableCell] = []
            for r in self.header:
                row = self.slots[r]
                cell = row[start]
                if (cell is None or self.title(cell) or any(cell is seen for seen in found)
                        or any(row[c] is not cell for c in range(start, end))):
                    continue
                found.append(cell)
            parts = [line for cell in found for line in cell.text.split("\n") if line.plain.strip()]
            if parts:
                self._labels[key] = Text(" ").join(parts)
            elif end - start > 1:
                own = {label.plain: label for c in range(start, end)
                       if (label := self.label(c, c + 1)) is not None}
                self._labels[key] = Text(" / ").join(own.values()) if own else None
            else:
                self._labels[key] = None
        return self._labels[key]


class _TableMeasure:
    """Bounded layout comparisons used only when changing column allocation."""

    def __init__(self, paginator: Paginator, table: TableData, grid: _Grid, width: int):
        self.paginator = paginator
        self.table = table
        self.model = grid
        self.rows = [r for r in range(len(table.rows)) if r not in table.full_width_rows]
        self.width = width
        self.available = (len(self.rows) <= 128 and len(grid.cells) <= 512
                          and sum(len(cell.text.plain) for _, cell, _, _ in grid.cells) <= 40_000)
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
        height = int(bool(self.model.header))  # header rule
        for r in self.rows:
            tallest = 0
            for cell, start, end, starts_here in self.model.segments(r):
                if cell is None or not starts_here:
                    continue  # a continued cell is shown only where it starts
                value = self._height(cell.text, sum(columns[start:end]) + 3 * (end - start - 1),
                                     cell.preformatted)
                if value is None:
                    return None
                tallest = max(tallest, value)
            height += tallest
        return height

    def records(self, limit: int | None = None) -> int | None:
        """Height of the record layout, plus one line per record: columns that
        line up across rows are worth that much when choosing a layout.
        Measuring stops once the height reaches limit."""
        if not self.available:
            return None
        height = 0
        for r in self.rows:
            lines = self.paginator._record(self.table, self.model, r, self.width)
            if not lines:
                continue
            if not self.paginator._attached(self.table, self.model, r):
                height += 1 + int(height > 0)  # a blank line between records
            height += len(lines)  # sub-rows attach to their record
            if limit is not None and height >= limit:
                break
        return height


@dataclass
class Page:
    lines: list[Text]
    info: list[Text]  # infobox lines shown on this page (empty => no box)
    info_more: bool  # infobox continues on the next page
    first_block: int
    width: int
    blocks: set[int] = field(default_factory=set)
    folded: int = 0  # infobox lines left out of the card on this page


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
        self._grids: dict[int, _Grid | None] = {}
        self._rows: dict[tuple[int, int, int, bool], list[Text]] = {}
        self._box_height: int | None = None  # the infobox framed in the text

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
        if b.kind == "infobox" and self._box_height is not None:
            return min(limit, self._box_height)
        if b.kind == "pre":
            pad = min(b.indent, max(width // 4, 0), max(width - 4, 0))
            return len(_PreFlow(b.text).take(width - pad, limit))
        if b.kind == "table" and b.table is not None:
            height = len(wrap(b.table.caption, width)) + 1 if b.table.caption.plain else 0
            for row in range(len(b.table.rows)):
                lines = self._table_row(b.table, row, width)
                if not lines:
                    continue  # header text shown as record labels
                height += len(lines)
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
        grid = self._grid(table)
        count = grid.width if grid is not None else 0
        result = None
        if (table.simple and grid is not None and width >= 32 and 0 < count <= 16
                and count * 4 + (count - 1) * 3 <= width):
            preferred = [4] * count
            minimum = [4] * count
            line_lengths = [0] * count  # each value's longest line, summed per column
            values = [0] * count
            merged = []
            for row, cell, i, end in grid.cells:
                longest = max((line.cell_len for line in cell.text.split("\n")), default=0)
                if end - i != 1:
                    merged.append((i, end, min(width, longest)))
                    continue
                preferred[i] = max(preferred[i], min(width, longest))
                if not cell.preformatted:
                    minimum[i] = max(minimum[i], max(
                        (min(width, cell_len(match.group())) for match in NUMBER_TOKEN.finditer(cell.text.plain)),
                        default=4,
                    ))
                if row not in grid.header and cell.text.plain.strip():
                    line_lengths[i] += longest
                    values[i] += 1
            # A merged cell wraps across the columns it spans; ask them for
            # enough room between them to fit its longest line.
            for start, end, longest in merged:
                short = longest - sum(preferred[start:end]) - 3 * (end - start - 1)
                for k in range(max(short, 0)):
                    preferred[start + k % (end - start)] += 1
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
                            measure = _TableMeasure(self, table, grid, width)
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
                # Sentences squeezed into a narrow column break words and wrap
                # every word or two; records give running text the full width.
                if any(values[i] and line_lengths[i] >= RUNNING_TEXT * values[i]
                       and result[i] < TEXT_COLUMN for i in range(count)):
                    result = None
                elif recovering:
                    if measure is None:
                        measure = _TableMeasure(self, table, grid, width)
                    grid_height = measure.grid(result)
                    record_height = (measure.records(limit=grid_height)
                                     if grid_height is not None else None)
                    if grid_height is None or record_height is None or grid_height > record_height:
                        result = None
        self._table_widths[key] = result
        return result

    def _grid(self, table: TableData) -> _Grid | None:
        """Cell positions, or None for spans kept in source order."""
        key = id(table)
        if key not in self._grids:
            placed = (place_cells(table.rows, table.full_width_rows)
                      if table.simple or table.aligned else None)
            self._grids[key] = _Grid(table, *placed) if placed is not None else None
        return self._grids[key]

    def _header(self, table: TableData) -> range:
        grid = self._grid(table)
        return grid.header if grid is not None else range(0)

    def _header_lines(self, table: TableData, width: int) -> list[Text]:
        """Every header row, ending with its rule; empty for records."""
        return [line for row in self._header(table) for line in self._table_row(table, row, width)]

    @staticmethod
    def _attached(table: TableData, grid: _Grid, index: int) -> bool:
        """A record sub-row, listed under the row whose first cell it continues."""
        return grid.continued(index) and not any(cell.preformatted for cell in table.rows[index])

    def _cell_text(self, cell: TableCell) -> Text:
        text = cell.text.copy()
        text.expand_tabs(8)
        if cell.header:
            text.stylize(Style(bold=True, color=self.pal.heading))
        return text

    @staticmethod
    def _cell_lines(text: Text, width: int, preformatted: bool) -> list[Text]:
        if preformatted:
            return [part for line in text.split("\n", allow_blank=True)
                    for part in fold_preserving(line, width)]
        return wrap(text, width)

    def _join_lines(self, text: Text, lists: bool = True) -> Text:
        """A cell value flowed as running text for records.

        Line breaks inside a value (a date, a name and its lifespan) become
        spaces and rejoin hyphenated words; values stacked with <hr> are
        separated by ' · '. List items keep their own lines unless lists=False.
        """
        plain = text.plain
        if "\n" not in plain:
            return text
        rules = rule_breaks(text)
        out = Text()
        start = 0
        for k, character in enumerate(plain):
            if character != "\n":
                continue
            out.append_text(text[start:k])
            following = plain[k + 1:]
            if lists and LIST_ITEM.match(following):
                out.append("\n")
            elif k in rules:
                out.append(" · ", Style(color=self.pal.muted))
            elif not (plain[k - 1:k] == "-" and following[:1].isalpha()):
                out.append(" ")
            start = k + 1
        out.append_text(text[start:])
        return out

    def _field(self, label: Text, cell: TableCell, text: Text, width: int, hang: int = 0) -> list[Text]:
        """'Label: value', with wrapped lines indented by hang."""
        label = label.copy()
        label.stylize(Style(bold=True, color=self.pal.muted))
        if cell.preformatted:
            return wrap(label + Text(":"), width) + self._cell_lines(text, width, True)
        lines = wrap(label + Text(": ") + text, width - hang)
        return lines[:1] + [Text(" " * hang) + line for line in lines[1:]]

    def _table_row(self, table: TableData, index: int, width: int, top: bool = False) -> list[Text]:
        """Screen lines for one table row. top=True places it first on a page,
        where values continued from rows above are repeated for context."""
        grid = self._grid(table)
        top = top and grid is not None and index in grid.carried
        key = (id(table), index, width, top)
        if key not in self._rows:
            self._rows[key] = self._render_row(table, index, width, top)
        return self._rows[key]

    def _render_row(self, table: TableData, index: int, width: int, top: bool) -> list[Text]:
        row = table.rows[index]
        if index in table.full_width_rows:
            return [line for cell in row
                    for line in self._cell_lines(self._cell_text(cell), width, cell.preformatted)]
        columns = self._columns(table, width)
        grid = self._grid(table)
        if columns is not None:
            return self._grid_row(grid, index, columns, top)
        if grid is not None:
            return self._record(table, grid, index, width, top)
        # Unvalidated spans have no column positions: preserve source order.
        out = [Text(f"Row {index + 1}", Style(color=self.pal.muted))]
        for i, cell in enumerate(row):
            out.extend(self._field(Text(f"Cell {i + 1}"), cell, self._cell_text(cell), width))
        return out

    def _grid_row(self, grid: _Grid, index: int, columns: list[int], top: bool = False) -> list[Text]:
        wrapped: list[list[Text]] = []
        widths: list[int] = []
        for cell, start, end, starts_here in grid.segments(index):
            width = sum(columns[start:end]) + 3 * (end - start - 1)
            widths.append(width)
            if cell is None or not (starts_here or top):
                wrapped.append([])  # a merged cell shows once, where it starts
                continue
            text = self._cell_text(cell)
            if not starts_here:
                text.stylize(Style(color=self.pal.muted))
            wrapped.append(self._cell_lines(text, width, cell.preformatted))
        out = []
        separator = Text(" │ ", Style(color=self.pal.muted))
        for line in range(max((len(parts) for parts in wrapped), default=1)):
            pieces = []
            for parts, width in zip(wrapped, widths):
                part = parts[line].copy() if line < len(parts) else Text("")
                part.pad_right(max(0, width - part.cell_len))
                pieces.append(part)
            out.append(joined(separator, pieces))
        if grid.header and index == grid.header.stop - 1:
            out.append(Text("─" * (sum(columns) + 3 * (len(columns) - 1)),
                            Style(color=self.pal.muted)))
        return out

    def _record(self, table: TableData, grid: _Grid, index: int, width: int,
                top: bool = False) -> list[Text]:
        """A row as 'Label: value' lines named by the header rows above it.

        Values merged down from earlier rows repeat in each record they cover.
        A row continuing the first cell of the one above is a sub-row: its own
        values share one indented line beneath that record, unless it starts a
        page, where it shows the continued values too (muted) for context.
        """
        if index in grid.header:
            # Header text labels every record; only a title has lines of its own.
            return [line for cell, _, _, starts_here in grid.segments(index)
                    if cell is not None and starts_here and grid.title(cell)
                    for line in wrap(self._cell_text(cell), width)]
        labelled = bool(grid.header)
        attached = self._attached(table, grid, index) and not top
        fields = []
        for cell, start, end, starts_here in grid.segments(index):
            if cell is None or (attached and not starts_here) or not cell.text.plain.strip():
                continue
            text = self._cell_text(cell)
            if not cell.preformatted:
                text = self._join_lines(text, lists=not attached)
            if not starts_here and grid.continued(index):
                text.stylize(Style(color=self.pal.muted))
            label = (grid.label(start, end) if labelled else None) or Text(f"Cell {start + 1}")
            fields.append((label, cell, text))
        if attached:
            pieces = []
            for label, _, text in fields:
                label = label.copy()
                label.stylize(Style(bold=True, color=self.pal.muted))
                pieces.append(label + Text(": ") + text)
            if not pieces:
                return []
            lines = wrap(joined(Text(" · ", Style(color=self.pal.muted)), pieces), width - 4)
            return [Text("  ") + lines[0]] + [Text("    ") + line for line in lines[1:]]
        out = [] if labelled else [Text(f"Row {index + 1}", Style(color=self.pal.muted))]
        for label, cell, text in fields:
            out.extend(self._field(label, cell, text, width, hang=2 if labelled else 0))
        return out

    def render_info(self, rows: list[InfoRow], width: int) -> list[Text]:
        return self._info_lines(rows, width)[0]

    def _info_lines(self, rows: list[InfoRow], width: int) -> tuple[list[Text], list[int]]:
        """The infobox's lines, and the number of lines through each row."""
        pal = self.pal
        out: list[Text] = []
        ends: list[int] = []
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
                else:
                    ind = len(lab.plain) - len(lab.plain.lstrip())
                    if ind:  # sub-row: keep wrapped label lines indented too
                        ll = [Text(" " * ind) + x for x in wrap(lab[ind:], lw - ind)]
                    else:
                        ll = wrap(lab, lw)
                    for i in range(max(len(ll), len(vl))):
                        a = ll[i].copy() if i < len(ll) else Text("")
                        a.pad_right(lw - a.cell_len)
                        out.append(a + Text(" ") + (vl[i] if i < len(vl) else Text("")))
            elif not r.value.plain.strip():  # an empty full-width row is a deliberate gap
                if out and out[-1].plain:
                    out.append(Text(""))
            else:  # full-width row
                for part in r.value.split("\n"):
                    if part.plain.strip():
                        out.extend(wrap(part, width))
            ends.append(len(out))
        while out and not out[-1].plain.strip():
            out.pop()
        return out, ends

    def _info_box(self, rows: list[InfoRow], width: int, budget: int) -> tuple[list[Text], int]:
        """The infobox's lines, and how many of them make its card: whole rows
        of the first section (up to a header after some labelled fields; an
        image caption is not one), within budget."""
        lines, ends = self._info_lines(rows, width)
        card = 0
        fields = False
        for row, end in zip(rows, ends):
            end = min(end, len(lines))
            if (row.kind == "header" and fields) or (end > budget and card):
                break
            card = end
            fields = fields or row.kind == "pair"
        card = min(card, budget)
        while card and not lines[card - 1].plain.strip():
            card -= 1
        return lines, card

    def _frame(self, lines: list[Text], width: int, top: str | None = None,
               bottom: str | tuple[str, ...] | None = None) -> list[Text]:
        """Lines boxed like the side box: a label sits in the top edge at the
        left and in the bottom edge at the right. Of several labels, the
        first that fits is used."""
        border = Style(color=self.pal.frame)

        def edge(left: str, right: str, labels: str | tuple[str, ...] | None, start: bool) -> Text:
            out = Text()
            out.append(left, border)
            fill = width - 2
            labels = (labels,) if isinstance(labels, str) else labels or ()
            label = next((label for label in labels if cell_len(label) + 4 <= fill), None)
            if label:
                rest = fill - cell_len(label) - 3
                out.append("─" * (1 if start else rest), border)
                out.append(f" {label} ", Style(color=self.pal.muted))
                out.append("─" * (rest if start else 1), border)
            else:
                out.append("─" * fill, border)
            out.append(right, border)
            return out

        inner = width - 4
        out = [edge("┌", "┐", top, True)]
        for line in lines:
            if line.cell_len > inner:
                line = line.copy()
                line.truncate(inner)
            row = Text()
            row.append("│ ", border)
            row.append_text(line)
            row.append(" " * (inner - line.cell_len))
            row.append(" │", border)
            out.append(row)
        out.append(edge("└", "┘", bottom, False))
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

    def paginate(self, art: Article, lay: Layout, breaks: set[int],
                 info_expanded: bool = False) -> list[Page]:
        """info_expanded: an infobox framed in the text is shown in full
        instead of folded to its card."""
        self._cache = {}
        self._table_widths = {}
        self._grids = {}
        self._rows = {}
        H = max(lay.height, 4)
        info_lines = self.render_info(art.infobox, lay.info_width) if (art.infobox and lay.info_width) else []
        info_per_page = max(H - 2, 1)  # box border takes 2 rows
        box: list[Text] = []  # without a side box, the infobox is framed in the text
        card = 0
        if art.infobox and not lay.info_width:
            box, card = self._info_box(art.infobox, lay.full_width - 4, max(1, min(INFO_CARD, H // 3)))
        shown = box if info_expanded else box[:card]
        self._box_height = None if lay.info_width else len(shown) + 2 * bool(shown)

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

        def place_box(block: int, before: int) -> None:
            """Frame the infobox in the text. A card stays whole; the expanded
            box continues across pages with edges labelled like the side box."""
            nonlocal page
            folded = len(box) - len(shown)
            last = ((f"{folded} more lines · i", f"+{folded} · i") if folded
                    else "collapse · i" if card < len(box) else None)
            available = H - len(page.lines) - before
            if (page.lines and len(shown) + 2 > available
                    and (len(shown) + 2 <= H or available < 6)):
                page = new_page(block)
                before = 0
            offset = 0
            while offset < len(shown):
                if offset:
                    page = new_page(block)
                    before = 0
                part = shown[offset:offset + max(H - len(page.lines) - before - 2, 1)]
                offset += len(part)
                self._put(page, self._frame(part, page.width,
                                            "↑ continued" if offset > len(part) else None,
                                            "continues ↓" if offset < len(shown) else last),
                          before, block)
            page.folded = folded

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
                if shown:
                    place_box(i, gap())
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
                grid = self._grid(table)
                header = self._header(table)
                header_index = header.start if header else None
                first_data_index = next((row for row in range(header.stop if header else 1, len(table.rows))
                                         if row not in table.full_width_rows), None)
                header_cache: dict[int, list[Text]] = {}

                def header_for(width: int) -> list[Text]:
                    if width not in header_cache:
                        lines = (self._header_lines(table, width)
                                 if self._columns(table, width) is not None else [])
                        header_cache[width] = lines if len(lines) <= H // 3 else []
                    return header_cache[width]

                def render(row: int, width: int, top: bool = False) -> list[Text]:
                    """A grid places its header rows together, as one row."""
                    if row == header_index and self._columns(table, width) is not None:
                        return self._header_lines(table, width)
                    return self._table_row(table, row, width, top)

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
                    grid_view = self._columns(table, width) is not None
                    if row in header and row != header_index and grid_view:
                        continue  # placed with the first header row
                    lines = render(row, width)
                    if not lines:
                        continue  # header text that only labels records
                    if (before and not grid_view and grid is not None
                            and self._attached(table, grid, row)):
                        before = 0  # a sub-row stays under its record
                    repeat_needed = (header_index is not None and row >= header.stop
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
                            lines = render(row, width)
                            available = H
                    if page.lines and len(lines) > available:
                        fresh_width = (lay.full_width if len(pages) * info_per_page >= len(info_lines)
                                       else lay.narrow_width)
                        fresh = render(row, fresh_width, top=True)
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
                    if not page.lines:
                        lines = render(row, width, top=True)
                        if repeat:
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
