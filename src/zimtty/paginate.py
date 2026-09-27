"""Turn an Article into screen-sized pages.

Rules (agreed design):
  * a paragraph is never split across pages if it fits on a fresh page;
    otherwise it goes to the next page whole
  * paragraphs taller than a whole page are split at a sentence boundary,
    falling back to a line boundary only if one sentence is itself too tall
  * headings are kept with the start of what follows them
  * every h2 (and any heading the user jumped to via the TOC) starts a page
  * the infobox sits in its own box on the right; if it is taller than a
    page it continues on the next page; once it runs out the text goes full width
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rich.console import Console
from rich.style import Style
from rich.text import Text

from .zimdoc import Article, Block, InfoRow, Palette

_console = Console(width=400, color_system=None, legacy_windows=False, emoji=False)

SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"“(\[]?[A-Z0-9])")
TIGHT = {"item", "ref"}  # consecutive list items get no blank line between them


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
        for i, b in enumerate(blocks):
            is_heading = b.kind in ("h2", "h3", "h4")
            forced = (b.kind == "h2" or i in breaks) and i != 0
            if forced and page.lines:
                page = new_page(i)
                prev_kind = None

            def gap() -> int:
                if not page.lines:
                    return 0
                return 0 if (b.kind in TIGHT and prev_kind == b.kind) else 1

            lines = self.render_block(b, page.width)
            if is_heading:
                cap = max(1, min(3, H - 2))  # a runaway heading must not blow up the page
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                keep = min(2, len(self.render_block(nxt, page.width))) + 1 if nxt else 0
                if page.lines and len(page.lines) + gap() + min(len(lines), cap) + keep > H:
                    page = new_page(i)
                    lines = self.render_block(b, page.width)
                if len(lines) > cap:
                    lines = lines[:cap - 1] + [lines[-1]] if b.kind in ("title", "h2") and cap > 1 else lines[:cap]
                self._put(page, lines, gap(), i)
                prev_kind = b.kind
                continue

            if len(page.lines) + gap() + len(lines) <= H:
                self._put(page, lines, gap(), i)
                prev_kind = b.kind
                continue

            # does it fit on a fresh page?
            if page.lines:
                fresh_w = lay.full_width if len(pages) * info_per_page >= len(info_lines) else lay.narrow_width
                fresh = self.render_block(b, fresh_w)
                if len(fresh) <= H:
                    page = new_page(i)
                    self._put(page, self.render_block(b, page.width), 0, i)
                    prev_kind = b.kind
                    continue
                # oversized: start it here if there is real room, else on a new page
                if H - len(page.lines) - gap() < 4:
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
