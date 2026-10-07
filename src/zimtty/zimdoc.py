"""ZIM access and HTML -> Article model.

Walk common HTML containers in document order, retaining structured code and
tables alongside styled prose, infobox data, and anchor/TOC information. Both
modern section-based and older flat MediaWiki exports use this shared model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import accumulate, count
from urllib.parse import quote, unquote, urljoin, urlsplit

from libzim.reader import Archive
from libzim.suggestion import SuggestionSearcher
from rich.style import Style
from rich.text import Text
from selectolax.parser import HTMLParser, Node

from .safety import safe_text

# ---------------------------------------------------------------- model


@dataclass
class TableCell:
    text: Text
    header: bool = False
    preformatted: bool = False
    colspan: int = 1  # grid columns covered, after empty columns are removed
    rowspan: int = 1  # rows covered, never past the end of the table


@dataclass
class TableData:
    rows: list[list[TableCell]]  # source cells in document order, each once
    caption: Text = field(default_factory=Text)
    simple: bool = True  # may be shown as a grid of columns
    # Single-cell rows spanning every column (titles, section bands, notes).
    full_width_rows: frozenset[int] = frozenset()
    # Spans were validated, so cells line up in columns even when the grid is
    # not allowed (nested tables). Simple tables are always aligned; others
    # keep source order without inferring header labels.
    aligned: bool = False


@dataclass
class Block:
    kind: str  # title | h2 | h3 | h4 | para | item | quote | hatnote | ref | pre | table | infobox
    text: Text
    anchor: str | None = None  # heading id, for TOC / #fragment jumps
    indent: int = 0
    hang: int = 0  # width of the bullet/number prefix, for hanging indents
    anchors: tuple[str, ...] = ()  # IDs/name aliases, carried through rearrangement
    table: TableData | None = None


@dataclass
class InfoRow:
    kind: str  # title | subtitle | header | pair | full
    label: Text | None
    value: Text | None


@dataclass
class TocEntry:
    block: int  # index into Article.blocks
    level: int  # 2, 3, 4
    title: str
    anchor: str | None


@dataclass
class Article:
    path: str
    title: str
    blocks: list[Block]
    infobox: list[InfoRow]
    notes: list[Text]  # lead hatnotes + maintenance boxes, moved out of the lead
    toc: list[TocEntry] = field(default_factory=list)
    anchors: dict[str, int] = field(default_factory=dict)
    base_href: str | None = None


@dataclass
class Palette:
    fg: str = "#d0d0d0"
    bright: str = "#ffffff"
    muted: str = "#808080"
    accent: str = "#7aa2f7"
    link: str = "#7aa2f7"
    heading: str = "#ffffff"
    sub: str = "#bb9af7"
    frame: str = "#536ca3"  # box borders: the accent at 60% over the background


# ---------------------------------------------------------------- constants

# Bounds apply to uncompressed HTML before content loading, and to the parsed
# element tree before any recursive walker. TeX exceeding its nesting bound is
# displayed as raw source. These limits allow normal articles while rejecting
# input that would exhaust memory or the Python recursion stack.
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_DOM_DEPTH = 128
MAX_TEX_DEPTH = 64
# Tables follow the HTML table model only within these bounds; wider or
# overlapping layouts keep their cells in source order instead.
MAX_TABLE_COLUMNS = 128
MAX_TABLE_SLOTS = 250_000
MAX_HEADER_ROWS = 3


class ArticleError(ValueError):
    """An article exceeds the supported resource limits."""


def _check_article_size(size: int) -> None:
    if size > MAX_ARTICLE_BYTES:
        raise ArticleError("Article HTML exceeds the maximum size of 16 MiB.")


def _check_dom_depth(tree: HTMLParser) -> None:
    """Validate element nesting iteratively, using memory proportional to depth."""
    if tree.root is None:
        return
    stack = [iter((tree.root,))]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        if len(stack) > MAX_DOM_DEPTH:
            raise ArticleError(
                f"Article HTML exceeds the maximum nesting depth of {MAX_DOM_DEPTH}."
            )
        stack.append(iter(node.iter()))


def place_cells(rows: list[list[TableCell]],
                bands: frozenset[int] = frozenset()) -> tuple[list[list[int]], int] | None:
    """Start column of every cell under the HTML table model, and the width.

    Bands occupy no columns. Overlapping cells, or grids beyond the column and
    slot bounds, return None so callers keep source order instead of guessing.
    """
    busy: list[int] = []  # per column: the first row no longer covered from above
    starts: list[list[int]] = []
    for r, row in enumerate(rows):
        if r in bands:
            starts.append([0] * len(row))
            continue
        column = 0
        placed = []
        for cell in row:
            while column < len(busy) and busy[column] > r:
                column += 1
            end = column + cell.colspan
            if end > MAX_TABLE_COLUMNS:
                return None
            busy.extend([0] * (end - len(busy)))
            if any(busy[c] > r for c in range(column, end)):
                return None
            busy[column:end] = [r + cell.rowspan] * cell.colspan
            placed.append(column)
            column = end
        starts.append(placed)
    if len(busy) * len(rows) > MAX_TABLE_SLOTS:
        return None
    return starts, len(busy)


def header_rows(rows: list[list[TableCell]], bands: frozenset[int] = frozenset()) -> range:
    """Leading rows made only of header cells, after any title bands.

    A run longer than MAX_HEADER_ROWS keeps only its first row. Without data
    rows below it there is nothing to label, so there is no header.
    """
    start = 0
    while start in bands:
        start += 1
    end = start
    while (end < len(rows) and end not in bands and rows[end]
           and all(cell.header for cell in rows[end])):
        end += 1
    if not any(r not in bands for r in range(end, len(rows))):
        return range(start, start)
    return range(start, start + 1 if end - start > MAX_HEADER_ROWS else end)


def rule_breaks(text: Text) -> set[int]:
    """Offsets of the line breaks in text that came from <hr>."""
    return {span.start for span in text.spans if span.style == RULE_BREAK}


def _span(cell: Node, name: str) -> int | None:
    """A positive span of at most four digits; None marks malformed markup."""
    raw = (cell.attributes.get(name, "1") or "").strip()
    if len(raw) <= 4 and raw.isascii() and raw.isdecimal() and int(raw) > 0:
        return int(raw)
    return None


def _align(rows: list[list[TableCell]]) -> frozenset[int] | None:
    """Fit spans to the HTML table model, dropping columns that are empty in
    every data row (stripped portraits, colour swatches, spacer columns).

    Rewrites spans and removes emptied cells and rows in place. Returns the
    full-width bands, or None when the cells cannot be placed in a grid.
    """
    for r, row in enumerate(rows):
        for cell in row:
            cell.rowspan = min(cell.rowspan, len(rows) - r)
    placed = place_cells(rows)
    if placed is None:
        return None
    starts, width = placed
    bands = frozenset(r for r, row in enumerate(rows) if len(row) == 1 and row[0].colspan == width)
    head = header_rows(rows, bands)
    used = [False] * width
    spanning = []
    for r, row in enumerate(rows):
        if r in head:
            continue
        for cell, start in zip(row, starts[r]):
            if not cell.text.plain.strip():
                continue
            if cell.colspan == 1:
                used[start] = True
            else:
                spanning.append((start, start + cell.colspan))
    for start, end in spanning:  # a merged value keeps at least one column
        if not any(used[start:end]):
            used[start] = True
    if any(used) and not all(used):
        before = list(accumulate(used, initial=0))  # used columns left of each
        for row, row_starts in zip(rows, starts):
            for cell, start in zip(row, row_starts):
                cell.colspan = before[start + cell.colspan] - before[start]
    for row in rows:
        row[:] = [cell for cell in row if cell.colspan]
        if all(cell.rowspan == 1 and not cell.text.plain.strip() for cell in row):
            row.clear()  # spacer rows, or rows that only held an image
    kept = list(accumulate((bool(row) for row in rows), initial=0))
    for r, row in enumerate(rows):
        for cell in row:
            cell.rowspan = kept[r + cell.rowspan] - kept[r]
    rows[:] = [row for row in rows if row]
    placed = place_cells(rows)
    if placed is None:  # defensive: compaction preserves the layout
        return None
    starts, width = placed
    return frozenset(r for r, row in enumerate(rows)
                     if width > 1 and len(row) == 1 and row[0].colspan == width
                     and row[0].rowspan == 1)


# Blocks we never show in the reading view.
SKIP_BLOCK_CLASSES = {
    "navbox", "navbox-styles", "thumb", "sidebar", "metadata", "noprint",
    "side-box", "gallery", "mw-empty-elt", "toclimit-2", "toclimit-3",
    "toclimit-4", "paragraphbreak", "OWIDSlider", "portalbox", "portal",
    "sistersitebox", "mbox-small", "navigation-not-searchable-box",
    "infobox-journal-search", "shortdescription", "mw-authority-control",
}
NOTE_CLASSES = {"ambox", "ombox", "tmbox", "cmbox", "fmbox", "dmbox"}
# Inline things that are hidden by Wikipedia's CSS or pure chrome.
SKIP_INLINE_CLASSES = {
    "noprint", "mw-editsection", "geo-nondefault", "geo-multi-punct",
    "sortkey", "mw-cite-backlink", "navbar", "metadata", "mw-empty-elt",
    "Z3988", "reflink", "ext-phonos", "infobox-image",
    # image captions / galleries / maps: meaningless in a no-pictures ZIM
    "thumb", "tmulti", "thumbcaption", "infobox-caption", "gallery",
    "switcher-container", "locmap", "ib-settlement-caption", "maptable",
    "mapframe", "mw-kartographer-container", "notpageimage", "ib-settlement-cols",
    "ib-settlement-caption-link",
}
SKIP_TAGS = {"style", "script", "link", "meta", "_comment", "img", "audio",
             "video", "figure", "noscript", "input", "button", "svg"}

# Top-level sections that are "back matter": they go after Additional notes.
BACK_MATTER = {
    "references", "citations", "notes", "footnotes", "sources",
    "general and cited sources", "works cited", "cited sources",
    "further reading", "external links", "notes and references",
    "references and notes", "bibliography and references", "explanatory notes",
}
NOTES_TITLE = "Additional notes"

WS = re.compile(r"[ \t\r\n\f\u00a0]+")
INVISIBLE = str.maketrans("", "", "\u200b\u2060\ufeff\u00ad")
# Marks a line break from <hr>: stacked separate values, not one wrapped value.
RULE_BREAK = Style(meta={"break": "rule"})


def classes(node: Node) -> set[str]:
    return set((node.attributes.get("class") or "").split())


# ---------------------------------------------------------------- TeX -> text
# nopic ZIMs keep maths only as the TeX source (the rendered SVG is an image).
# A small best-effort converter makes common formulas readable in a terminal.

_TEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "cup": "∪",
    "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬",
    "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "iff": "⇔", "implies": "⇒", "mapsto": "↦", "infty": "∞",
    "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "Π", "int": "∫", "oint": "∮",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "sim": "∼", "simeq": "≃", "propto": "∝", "pm": "±", "mp": "∓",
    "times": "×", "cdot": "·", "div": "÷", "circ": "∘", "ldots": "…", "cdots": "⋯",
    "dots": "…", "prime": "′", "langle": "⟨", "rangle": "⟩", "hbar": "ħ", "ell": "ℓ",
    "sqrt": "√", "quad": " ", "qquad": "  ", "log": "log", "ln": "ln", "exp": "exp",
    "sin": "sin", "cos": "cos", "tan": "tan", "lim": "lim", "max": "max", "min": "min",
    "det": "det", "Pr": "Pr", "operatorname": "", "left": "", "right": "", "mathrm": "",
    "mathit": "", "mathbf": "", "textstyle": "", "displaystyle": "", "text": "",
    "boldsymbol": "", "mathcal": "", "mathfrak": "", "bigl": "", "bigr": "", "Big": "",
    "big": "", "Bigl": "", "Bigr": "",
}
_BB = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "E": "𝔼"}
_SUP = str.maketrans("0123456789+-=()niTk", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵀᵏ")
_SUB = str.maketrans("0123456789+-=()aeijkmnoprstx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢⱼₖₘₙₒₚᵣₛₜₓ")


def _paren(x: str) -> str:
    x = x.strip()
    return x if len(x) <= 1 or re.fullmatch(r"[\w.′]+", x) else f"({x})"


def _script(body: str, table: dict, mark: str) -> str:
    body = body.strip()
    if body and all(ord(c) in table for c in body):
        return body.translate(table)  # every char has a Unicode super/subscript
    return mark + _paren(body)


class _Tex:
    """Tiny recursive-descent TeX reader: enough for Wikipedia's inline maths."""

    def __init__(self, s: str):
        self.s, self.i = s, 0
        self.depth = 0

    def group(self) -> str:
        """One argument: {...} or a single token."""
        if self.depth >= MAX_TEX_DEPTH:
            raise ValueError("TeX nesting limit exceeded")
        self.depth += 1
        try:
            self.ws()
            if self.i >= len(self.s):
                return ""
            if self.s[self.i] == "{":
                self.i += 1
                out = self.seq("}")
                self.i += 1  # skip }
                return out
            return self.token()
        finally:
            self.depth -= 1

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def token(self) -> str:
        s = self.s
        c = s[self.i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[self.i:])
            self.i += m.end()
            name = m.group(1)
            if name in ("frac", "dfrac", "tfrac"):
                num, den = self.group(), self.group()
                return f"{_paren(num)}/{_paren(den)}"
            if name == "sqrt":
                self.ws()
                if self.i < len(s) and s[self.i] == "[":  # \sqrt[n]{x}
                    j = s.index("]", self.i)
                    n = s[self.i + 1:j]
                    self.i = j + 1
                    return f"{n}√{_paren(self.group())}"
                return "√" + _paren(self.group())
            if name == "mathbb":
                g = self.group()
                return _BB.get(g, g)
            if name in ("text", "mathrm", "operatorname", "mathit", "mathbf", "mathcal",
                        "mathfrak", "boldsymbol", "textbf", "textit", "mathsf", "mbox"):
                return self.group()
            if name in (",", ";", ":", "!", " "):
                return " "
            if name in ("{", "}", "%", "$", "#", "&", "_"):
                return name
            if name == "\\":
                return "; "
            return _TEX_SYMBOLS.get(name, name)
        if c == "^" or c == "_":
            self.i += 1
            body = self.group()
            return _script(body, _SUP, "^") if c == "^" else _script(body, _SUB, "_")
        if c == "{":
            return self.group()
        self.i += 1
        return c

    def seq(self, stop: str | None = None) -> str:
        out = []
        while self.i < len(self.s) and (stop is None or self.s[self.i] != stop):
            out.append(self.token())
        return "".join(out)


def tex_to_text(s: str) -> str:
    s = safe_text(s)
    if "\\" not in s and "^" not in s and "_" not in s and "{" not in s:
        return s
    try:
        t = _Tex(re.sub(r"\s+([\^_])", r"\1", s)).seq()
    except (ValueError, AttributeError, IndexError, RecursionError):
        return s  # malformed or too deeply nested TeX: show the safe raw source
    t = re.sub(r"\s*([=<>≤≥≠≈∈∉⊂⊆→⇒⇔±×])\s*", r" \1 ", t)
    t = re.sub(r"\s+([,)\]])", r"\1", t)
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def hidden(node: Node) -> bool:
    style = (node.attributes.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


# ---------------------------------------------------------------- inline text

_LINK_IDS = count()


def local_href(href: str) -> bool:
    """Only archive-relative URLs are actionable; never launch external schemes."""
    try:
        parsed = urlsplit(href)
    except ValueError:
        return False
    return bool(href) and not parsed.scheme and not parsed.netloc and not href.startswith("//")


def archive_href(href: str, base_path: str, base_href: str | None = None) -> tuple[str, str | None] | None:
    """Resolve a local HTML URL inside an archive, without any filesystem/network IO.

    Split query/fragment before percent-decoding so encoded '#' and '?' remain
    part of the entry name. An external HTML base is ignored: exported wikis
    often retain their original website's base while their links are local.
    """
    if not local_href(href):
        return None
    base = "/" + quote(base_path.lstrip("/"), safe="/")
    if base_href and local_href(base_href):
        base = urljoin(base, base_href)
    parsed = urlsplit(urljoin(base, href))
    fragment = parsed.fragment if parsed.fragment or "#" in href else None
    return unquote(parsed.path).lstrip("/"), fragment


class InlineBuilder:
    """Turns an element's inline content into a rich Text with links/refs styled."""

    def __init__(self, pal: Palette, show_refs: bool, boxy: bool = False, links: bool = True):
        self.pal = pal
        self.show_refs = show_refs
        self.links = links  # False: links render as plain text and aren't followable
        self.boxy = boxy  # infobox mode: <li>/<div>/<br> become line breaks
        self.segs: list[tuple[str, Style | None]] = []

    def build(self, node: Node) -> Text:
        self._walk(node, None)
        return self._finish()

    def build_nodes(self, nodes: list[Node]) -> Text:
        """Build an inline run including each supplied node's own tag/style."""
        for node in nodes:
            self._visit(node, None)
        return self._finish()

    def _brk(self, style: Style | None = None):
        self.segs.append(("\n", style))

    def _walk(self, node: Node, style: Style | None):
        for ch in node.iter(include_text=True):
            self._visit(ch, style)

    def _visit(self, ch: Node, style: Style | None):
        tag = ch.tag
        if tag == "-text":
            s = (ch.text_content or "").translate(INVISIBLE)
            if s:
                self.segs.append((s, style))
            return
        if tag in SKIP_TAGS or hidden(ch):
            return
        cls = classes(ch)
        if cls & SKIP_INLINE_CLASSES:
            return
        if tag == "sup" and ("mw-ref" in cls or "reference" in cls):
            if self.show_refs:
                self.segs.append((ch.text(strip=True), Style(color=self.pal.muted, dim=True)))
            return
        if tag in ("br", "hr"):
            self._brk(RULE_BREAK if tag == "hr" else None)
            return
        if tag == "a":
            href = ch.attributes.get("href") or ""
            if ch.attributes.get("role") == "button":
                return
            if self.links and local_href(href):
                link = Style(color=self.pal.link, underline=True,
                             meta={"href": href, "link_id": next(_LINK_IDS),
                                   "@click": f"app.follow({href!r})"})
                self._walk(ch, (style + link) if style else link)
            else:
                self._walk(ch, style)
            return
        if tag in ("b", "strong", "i", "em", "cite", "var", "code", "kbd", "samp"):
            if tag in ("b", "strong"):
                added = Style(bold=True)
            elif tag in ("code", "kbd", "samp"):
                added = Style(color=self.pal.sub)
            else:
                added = Style(italic=True)
            self._walk(ch, (style + added) if style else added)
            return
        if "mwe-math-element" in cls:
            ann = ch.css_first("annotation")
            tex = ann.text() if ann else ""
            img = ch.css_first("img")
            alt = (img.attributes.get("alt") if img else "") or tex
            alt = alt.strip()
            if alt.startswith("{\\displaystyle") and alt.endswith("}"):
                alt = alt[len("{\\displaystyle"):-1].strip()
            self.segs.append((tex_to_text(alt), Style(italic=True, color=self.pal.sub)))
            return
        if tag in ("ul", "ol", "dl", "table", "pre") and not self.boxy:
            return  # nested blocks are handled by the block walker
        if self.boxy and tag in ("li", "div", "p", "tr", "dd", "dt", "pre"):
            self._brk()
            self._walk(ch, style)
            self._brk()
            return
        self._walk(ch, style)

    def _finish(self) -> Text:
        out = Text()
        pending_space = False
        at_line_start = True
        for s, st in self.segs:
            # All segments have already passed through HTML entity decoding,
            # including reference labels and TeX extracted from attributes.
            s = safe_text(s)
            if s == "\n":
                if not at_line_start:
                    out.append("\n", st)
                elif st is not None and out.plain.endswith("\n"):
                    out.stylize(st, len(out) - 1, len(out))  # <br><hr>: keep the rule
                at_line_start, pending_space = True, False
                continue
            parts = WS.split(s)
            for i, p in enumerate(parts):
                if i > 0:
                    pending_space = True
                if not p:
                    continue
                if pending_space and not at_line_start:
                    out.append(" ")
                out.append(p, st)
                pending_space, at_line_start = False, False
            if s and WS.fullmatch(s[-1]):
                pending_space = True
        out.rstrip()
        # tidy spaces before punctuation left by removed refs, e.g. "word ,"
        return out


# ---------------------------------------------------------------- the walker


class ArticleParser:
    def __init__(self, pal: Palette, show_refs: bool = False, links: bool = True):
        self.pal = pal
        self.show_refs = show_refs
        self.links = links

    def inline(self, node: Node, boxy=False) -> Text:
        return InlineBuilder(self.pal, self.show_refs, boxy, self.links).build(node)

    def parse(self, path: str, html: str) -> Article:
        # Reject obviously oversized input before allocating its UTF-8 copy.
        _check_article_size(len(html))
        _check_article_size(len(html.encode("utf-8")))
        tree = HTMLParser(html)
        _check_dom_depth(tree)
        root = (tree.css_first(".mw-parser-output") or tree.css_first("#mw-content-text")
                or tree.css_first("main") or tree.css_first("article") or tree.body)
        h1 = (root.css_first("h1") if root is not None else None) or tree.css_first("h1")
        title = self.inline(h1).plain if h1 else (tree.css_first("title").text(strip=True) if tree.css_first("title") else path)
        title = safe_text(title)
        self.infobox: list[InfoRow] = []
        self.notes: list[Text] = []
        self._note_blocks: list[Block] = []
        self._title_node_id = h1.mem_id if h1 else None
        self._title_anchors = list(self._node_anchors(h1, deep=True)) if h1 else []
        self._seen_heading = False
        self._article_title = title
        mediawiki = bool(tree.css_first(".mw-parser-output, #mw-content-text, [data-mw-section-id]"))
        flat: list[Block] = []
        trailing = self._walk(root, flat, in_lead=True) if root is not None else ()
        if trailing and flat:
            self._add_anchors(flat[-1], trailing)
        lead: list[Block] = []
        top: list[tuple[Block, list[Block]]] = []  # (h2 block, body)
        for b in flat:
            if b.kind == "h2":
                top.append((b, []))
            elif top:
                top[-1][1].append(b)
            else:
                lead.append(b)
        self._lead_first(lead)
        if mediawiki or self.notes:
            main = [(h, b) for h, b in top if h.text.plain.strip().lower() not in BACK_MATTER]
            back = [(h, b) for h, b in top if h.text.plain.strip().lower() in BACK_MATTER]
        else:
            main, back = top, []
        if self.notes:
            nh = Block("h2", Text(NOTES_TITLE), anchor="__notes__")
            main.append((nh, self._note_blocks))

        title_aliases = tuple(dict.fromkeys(self._title_anchors))
        blocks = [Block("title", Text(title), anchor="__top__", anchors=title_aliases)] + lead
        if trailing and not flat:
            self._add_anchors(blocks[0], trailing)
        for h, b in main + back:
            blocks.append(h)
            blocks.extend(b)
        blocks = self._drop_empty_headings(blocks)
        base = tree.css_first("base[href]")
        art = Article(path=path, title=title, blocks=blocks, infobox=self.infobox,
                      notes=self.notes, base_href=base.attributes["href"] if base else None)
        for i, b in enumerate(blocks):
            for anchor in ((b.anchor,) if b.anchor else ()) + b.anchors:
                art.anchors.setdefault(anchor, i)
            if b.kind in ("h2", "h3", "h4"):
                art.toc.append(TocEntry(i, int(b.kind[1]), b.text.plain, b.anchor))
        return art

    @staticmethod
    def _lead_first(lead: list[Block]) -> None:
        """Open with the first lead paragraph when the infobox precedes it, as
        Wikipedia's mobile site does, so an infobox in the text no longer
        delays the article by pages. The paragraph crosses only tables
        (companion boxes such as ratings or standings, which then follow it at
        any width), never a heading or the list it would continue."""
        box = next((i for i, b in enumerate(lead) if b.kind == "infobox"), None)
        if box is None or any(b.kind == "para" for b in lead[:box]):
            return
        for i in range(box + 1, len(lead)):
            if lead[i].kind == "para" and not lead[i].indent:
                lead.insert(box, lead.pop(i))
                return
            if lead[i].kind != "table":
                return

    @staticmethod
    def _drop_empty_headings(blocks: list[Block]) -> list[Block]:
        rank = {"h2": 2, "h3": 3, "h4": 4}
        out: list[Block] = []
        pending: list[str] = []
        for i, b in enumerate(blocks):
            if b.kind in rank:
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                if (not b.text.plain.strip() or nxt is None
                        or (nxt.kind in rank and rank[nxt.kind] <= rank[b.kind])):
                    pending.extend(((b.anchor,) if b.anchor else ()) + b.anchors)
                    continue
            ArticleParser._add_anchors(b, pending)
            pending.clear()
            out.append(b)
        if pending and out:
            ArticleParser._add_anchors(out[-1], pending)
        return out

    @staticmethod
    def _add_anchors(block: Block, anchors) -> None:
        block.anchors = tuple(dict.fromkeys((*block.anchors, *anchors)))

    @staticmethod
    def _skip(node: Node) -> bool:
        return (node.tag in (SKIP_TAGS - {"figure"}) or node.tag in ("nav", "footer", "form")
                or hidden(node) or bool(classes(node) & (SKIP_BLOCK_CLASSES | SKIP_INLINE_CLASSES))
                or node.attributes.get("role") in ("navigation", "banner", "contentinfo", "search")
                or node.attributes.get("id") in ("toc", "mw-navigation", "mw-panel", "catlinks", "jump-to-nav")
                or bool(classes(node) & {"toc", "mw-jump-link", "printfooter", "catlinks"}))

    def _node_anchors(self, node: Node, deep: bool = False) -> tuple[str, ...]:
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if self._skip(current):
                continue
            ident = current.attributes.get("id")
            name = current.attributes.get("name") if current.tag == "a" else None
            found.extend(value for value in (ident, name) if value)
            if deep:
                stack.extend(reversed(list(current.iter())))
        return tuple(dict.fromkeys(found))

    def _heading_text(self, h: Node) -> Text:
        # headings can contain <style> (navbox templates); use the inline walker
        t = self.inline(h)
        return Text(t.plain)

    # -- block level
    def _walk(self, node: Node, out: list[Block], in_lead: bool,
              indent: int = 0, list_depth: int = 0, content_only: bool = False) -> tuple[str, ...]:
        """Walk one container once, preserving runs of inline text between blocks."""
        inline_nodes: list[Node] = []
        pending = list(self._node_anchors(node))
        structural = {"div", "section", "main", "article", "header", "aside", "address",
                      "p", "pre", "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
                      "table", "details", "summary", "center", "figure", "figcaption",
                      "h1", "h2", "h3", "h4", "h5", "h6"}

        def flush():
            if not inline_nodes:
                return
            text = InlineBuilder(self.pal, self.show_refs, links=self.links).build_nodes(inline_nodes)
            for child in inline_nodes:
                if child.tag != "-text":
                    pending.extend(self._node_anchors(child, deep=True))
            if text.plain.strip():
                out.append(Block("para", text, indent=indent, anchors=tuple(dict.fromkeys(pending))))
                pending.clear()
            inline_nodes.clear()

        for ch in node.iter(include_text=True):
            if ch.tag == "-text":
                inline_nodes.append(ch)
                continue
            if self._skip(ch):
                continue
            cls = classes(ch)
            if ch.tag not in structural and not (cls & NOTE_CLASSES or cls & {"hatnote", "dablink"}):
                inline_nodes.append(ch)
                continue
            flush()
            before = len(out)
            remaining = self._block(ch, out, in_lead, indent, list_depth, content_only)
            if len(out) > before:
                self._add_anchors(out[before], pending)
                pending.clear()
            pending.extend(remaining)
        flush()
        return tuple(dict.fromkeys(pending))

    def _block(self, node: Node, out: list[Block], in_lead: bool,
               indent: int, list_depth: int, content_only: bool) -> tuple[str, ...]:
        tag, cls = node.tag, classes(node)
        aliases = self._node_anchors(node)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self._heading_text(node)
            anchors = self._node_anchors(node, deep=True)
            if tag == "h1" and (node.mem_id == self._title_node_id
                                or text.plain.strip() == self._article_title.strip()):
                self._title_anchors.extend(anchors)
                return ()
            if not content_only:
                self._seen_heading = True
            level = max(2, min(int(tag[1]), 4))
            out.append(Block(f"h{level}", text, anchor=anchors[0] if anchors else None,
                             anchors=anchors, indent=indent))
        elif tag == "pre":
            text = Text(self._pre_text(node))
            if text.plain:
                out.append(Block("pre", text, indent=indent, anchors=self._node_anchors(node, deep=True)))
            else:
                return aliases
        elif tag in ("ul", "ol"):
            return self._list(node, out, list_depth, indent, content_only)
        elif tag == "table" and "infobox" in cls and not self.infobox and not content_only:
            self.infobox = self._infobox(node)
            text = Text("\n").join(
                Text(" ").join(value for value in (row.label, row.value) if value is not None)
                for row in self.infobox
            )
            if text.plain.strip():
                out.append(Block("infobox", text, indent=indent,
                                 anchors=self._node_anchors(node, deep=True)))
            else:
                return self._node_anchors(node, deep=True)
        elif (tag == "table"
              and not (not content_only and not self._technical_note(cls)
                       and cls & (NOTE_CLASSES | {"hatnote", "dablink"}))):
            table = self._table(node)
            parts = [table.caption] if table.caption.plain else []
            parts.extend(Text(" | ").join(cell.text for cell in row) for row in table.rows)
            text = Text("\n").join(parts)
            if text.plain.strip():
                out.append(Block("table", text, indent=indent, table=table,
                                 anchors=self._node_anchors(node, deep=True)))
            else:
                return aliases
        elif not content_only and not self._technical_note(cls) and cls & NOTE_CLASSES:
            text = self._note_text(node)
            if text.plain:
                self.notes.append(text)
                self._note_blocks.append(Block("hatnote", text, anchors=self._node_anchors(node, deep=True)))
        elif not content_only and cls & {"hatnote", "dablink"}:
            text = self.inline(node, boxy=True)
            if text.plain:
                text.stylize(Style(italic=True, color=self.pal.muted))
                block = Block("hatnote", text, anchors=self._node_anchors(node, deep=True), indent=indent)
                if in_lead and not self._seen_heading:
                    self.notes.append(text)
                    self._note_blocks.append(block)
                else:
                    out.append(block)
        else:
            start = len(out)
            quote = tag == "blockquote" or bool(cls & {"quotebox", "templatequote"})
            extra_indent = 4 if quote else (2 if tag == "dd" else 0)
            remaining = self._walk(node, out, in_lead, indent + extra_indent, list_depth, content_only)
            for block in out[start:]:
                if tag == "dt":
                    block.text.stylize("bold")
                elif quote and block.kind == "para":
                    block.kind = "quote"
                    block.text.stylize("italic")
                elif tag == "dd" and block.kind == "para":
                    block.kind = "quote"
            return remaining
        return ()

    @staticmethod
    def _technical_note(cls: set[str]) -> bool:
        return bool(cls & {"note", "warning", "tip", "important", "caution", "admonition"}
                    or any(c.startswith("archwiki-template-box") for c in cls))

    def _pre_text(self, node: Node) -> str:
        parts: list[str] = []
        for child in node.iter(include_text=True):
            if child.tag == "-text":
                parts.append(safe_text(child.text_content or ""))
            elif not self._skip(child):
                parts.append("\n" if child.tag == "br" else self._pre_text(child))
        return "".join(parts)

    def _table(self, node: Node) -> TableData:
        cap = next((child for child in node.iter() if child.tag == "caption"), None)
        caption = self.inline(cap, boxy=True) if cap is not None else Text()
        rows: list[list[TableCell]] = []
        well_formed = True
        nested = False
        for row in self._direct_rows(node):
            if self._skip(row):
                continue
            cells: list[TableCell] = []
            for cell in row.iter():
                if cell.tag not in ("td", "th") or self._skip(cell):
                    continue
                # Bound parsing as well as rendering; large/malformed spans
                # stay in the source-order fallback without allocating a grid.
                colspan, rowspan = _span(cell, "colspan"), _span(cell, "rowspan")
                well_formed = well_formed and colspan is not None and rowspan is not None
                nested = nested or cell.css_first("table") is not None
                parts: list[Block] = []
                self._walk(cell, parts, in_lead=False, content_only=True)
                preformatted = any(block.kind == "pre" or
                                   (block.table is not None and any(c.preformatted for r in block.table.rows for c in r))
                                   for block in parts)
                cells.append(TableCell(Text("\n").join(block.text for block in parts),
                                       cell.tag == "th", preformatted=preformatted,
                                       colspan=colspan or 1, rowspan=rowspan or 1))
            if cells:
                rows.append(cells)
        bands = _align(rows) if well_formed else None
        if bands is None:
            for row in rows:
                for cell in row:
                    cell.colspan = cell.rowspan = 1
        aligned = bands is not None
        return TableData(rows, caption, simple=aligned and not nested,
                         full_width_rows=bands or frozenset(), aligned=aligned)

    def _list(self, lst: Node, out: list[Block], depth: int, indent: int = 0,
              content_only: bool = False) -> tuple[str, ...]:
        ordered = lst.tag == "ol"
        refs = "references" in classes(lst)
        start_value = lst.attributes.get("start", "1")
        try:
            n = int(start_value) - 1 if len(start_value) < 10 else 0
        except ValueError:
            n = 0
        pending = list(self._node_anchors(lst))
        for li in lst.iter():
            if li.tag != "li" or self._skip(li):
                continue
            n += 1
            value = li.attributes.get("value")
            if ordered and value and len(value) < 10:
                try:
                    n = int(value)
                except ValueError:
                    pass
            marker = f"{n}. " if ordered or refs else ("• " if depth == 0 else "◦ ")
            parts: list[Block] = []
            remaining = self._walk(li, parts, in_lead=False, indent=indent + 2,
                                   list_depth=depth + 1, content_only=content_only)
            if not parts:
                pending.extend(remaining)
                continue
            if parts[0].kind != "para":
                parts.insert(0, Block("para", Text(), indent=indent + 2))
            first = parts[0]
            first.kind = "ref" if refs else "item"
            first.hang = len(marker)
            first.text = Text(marker, Style(color=self.pal.muted)) + first.text
            self._add_anchors(first, pending)
            pending = list(remaining)
            for block in parts[1:]:
                if block.kind not in ("item", "ref"):
                    block.indent += len(marker)
            out.extend(parts)
        return tuple(dict.fromkeys(pending))

    def _note_text(self, box: Node) -> Text:
        body = box.css_first(".mbox-text") or box
        t = self.inline(body)
        t.stylize(Style(color=self.pal.muted))
        return t

    def _infobox(self, table: Node) -> list[InfoRow]:
        rows: list[InfoRow] = []
        cap = table.css_first("caption")
        if cap and cap.text(strip=True):
            rows.append(InfoRow("title", None, self.inline(cap, boxy=True).split("\n")[0]))
        self._info_rows(table, rows, depth=0)
        if rows and rows[0].kind != "title" and self._article_title:
            # some infoboxes (sports people, buildings) have no title row
            rows.insert(0, InfoRow("title", None, Text(self._article_title)))
        # drop section headers left with nothing under them (e.g. "Signature",
        # whose image is stripped in nopic ZIMs)
        out: list[InfoRow] = []
        for i, r in enumerate(rows):
            if r.kind == "header":
                nxt = rows[i + 1] if i + 1 < len(rows) else None
                if nxt is None or nxt.kind in ("header", "title"):
                    continue
            out.append(r)
        return out

    def _label(self, cell: Node) -> Text:
        """Infobox label: divs inside (e.g. 'Capital<div>and largest city') become
        spaces; a leading '•' marks a sub-row and is turned into an indent."""
        t = self.inline(cell, boxy=True)
        parts = [p for p in t.split("\n") if p.plain.strip()]
        out = Text(" ").join(parts) if parts else Text("")
        plain = out.plain
        if plain.startswith("•"):
            n = 1 + (len(plain) - 1 - len(plain[1:].lstrip()))
            out = Text("  ") + out[n:]
        return out

    CAPTION_START = re.compile(
        r"^\s*(clockwise|from (top|left)|top(\s|-)?(left|right|to)|left to right|"
        r"(top|upper) row|pictured|shown)", re.I)

    def _image_caption(self, cell: Node) -> bool:
        """A cell that captioned a collage whose images were stripped by nopic."""
        return bool(self.CAPTION_START.match(cell.text(strip=True)[:60]))

    @staticmethod
    def _is_grid(t: Text) -> bool:
        """Mini-diagrams (periodic-table neighbours, route maps) flatten into
        lots of 1-2 character lines; they are unreadable as text."""
        lines = [ln.strip() for ln in t.plain.split("\n") if ln.strip()]
        return len(lines) >= 4 and sum(len(ln) <= 2 for ln in lines) / len(lines) > 0.5

    @staticmethod
    def _direct_rows(table: Node) -> list[Node]:
        trs = []
        for part in table.iter():
            if part.tag in ("tbody", "thead", "tfoot"):
                trs.extend(r for r in part.iter() if r.tag == "tr")
            elif part.tag == "tr":
                trs.append(part)
        return trs

    def _info_rows(self, table: Node, rows: list[InfoRow], depth: int):
        side_names: list[Text] = []  # e.g. ["Allies", "Axis"] from a two-column row
        for tr in self._direct_rows(table):
            if hidden(tr):
                continue
            cells = [c for c in tr.iter() if c.tag in ("th", "td")]
            if not cells:
                continue
            c0 = cells[0]
            k0 = classes(c0)
            if "infobox-image" in k0 or "noresize" in k0 or (k0 & SKIP_INLINE_CLASSES):
                continue
            if "infobox-above" in k0 or "infobox-title" in k0 or ("summary" in k0 and not rows):
                t = self.inline(c0, boxy=True)  # name + native name on separate lines
                if t.plain:
                    rows.append(InfoRow("title", None, t))
            elif "infobox-subheader" in k0:
                t = self.inline(c0, boxy=True)
                if t.plain:
                    rows.append(InfoRow("subtitle", None, t))
            elif "infobox-header" in k0:
                t = self.inline(c0)
                if t.plain:
                    rows.append(InfoRow("header", None, t))
            elif len(cells) >= 2 and c0.tag == "th":
                lab, val = self._label(c0), self.inline(cells[1], boxy=True)
                if val.plain:
                    rows.append(InfoRow("pair", lab, val))
                elif lab.plain:  # e.g. "Legislature" with its houses on the next rows
                    rows.append(InfoRow("pair", lab, Text("")))
            elif len(cells) == 1 and c0.tag == "th":
                t = self.inline(c0)
                if t.plain:
                    rows.append(InfoRow("header" if rows else "title", None, t))
            else:
                # full-width cell(s); a nested table inside (dates/results in
                # military infoboxes, sub-boxes) is unpacked into proper rows.
                # Side-by-side cells (Allies | Axis) are stacked; the names from
                # the first such row are kept as labels for the rows below it.
                texts = []
                for c in cells:
                    if c.css_first(".tmulti, .thumb, .multiimageinner") or self._image_caption(c):
                        continue  # photo montage: what's left is only its captions
                    nested = [n for n in c.css("table") if self._direct_rows(n)]
                    if nested and depth < 2:
                        for n in nested:
                            if len(n.css("td")) > 24:
                                continue  # a diagram (periodic table, route map), not data
                            if n.parent is not None and n.parent.tag in ("td", "th", "div"):
                                self._info_rows(n, rows, depth + 1)
                        continue
                    t = self.inline(c, boxy=True)
                    if t.plain and len(t.plain) < 600 and not self._is_grid(t):
                        texts.append(t)
                if len(texts) > 1 and all(len(t.plain) < 40 and "\n" not in t.plain for t in texts):
                    side_names = texts  # a row of column names
                    rows.append(InfoRow("full", None, Text("  ·  ").join(texts)))
                    continue
                for k, t in enumerate(texts):
                    if len(texts) > 1:
                        if rows and rows[-1].kind == "full" and rows[-1].value.plain:
                            rows.append(InfoRow("full", None, Text("")))  # gap between sides
                        if len(side_names) == len(texts):
                            name = side_names[k].copy()
                            name.stylize(Style(bold=True, italic=True, color=self.pal.muted))
                            rows.append(InfoRow("full", None, name))
                    rows.append(InfoRow("full", None, t))


# ---------------------------------------------------------------- ZIM wrapper


class Zim:
    def __init__(self, path: str):
        self.path = path
        self.z = Archive(path)
        self._sugg = SuggestionSearcher(self.z)

    @property
    def name(self) -> str:
        for k in ("Title", "Name"):
            try:
                return safe_text(self.z.get_metadata(k).decode())
            except Exception:
                pass
        return safe_text(self.path)

    def resolve(self, target: str, *, base_path: str | None = None,
                base_href: str | None = None) -> tuple[str, str | None] | None:
        """Resolve titles/canonical paths, or a local href relative to its article.

        URL navigation uses archive paths only. Title search is deliberately a
        separate mode so punctuation and titles matching other folders cannot
        change the meaning of a relative link.
        """
        target = target.strip()
        if not target:
            return None
        if base_path is not None:
            resolved = archive_href(target, base_path, base_href)
            if resolved is None:
                return None
            target, frag = resolved
            title_lookup = False
        else:
            frag = None
            # Canonical entry names can themselves contain '#', '?' or '%'.
            if not self.z.has_entry_by_path(target):
                if self.z.has_entry_by_title(target):
                    target = self.z.get_entry_by_title(target).path
                else:
                    target, separator, fragment = target.partition("#")
                    frag = fragment if separator else None
                    target = unquote(target.removeprefix("./"))
            title_lookup = True
        seen: set[str] = set()
        for _ in range(10):
            if not target:
                return None
            if not self.z.has_entry_by_path(target):
                alt = target.replace(" ", "_")
                if self.z.has_entry_by_path(alt):
                    target = alt
                elif title_lookup and self.z.has_entry_by_title(target):
                    target = self.z.get_entry_by_title(target).path
                else:
                    return None
            if target in seen:
                return None
            seen.add(target)
            e = self.z.get_entry_by_path(target)
            if e.is_redirect:
                e = e.get_redirect_entry()
                target = e.path
                title_lookup = False
                continue
            item = e.get_item()
            if item.mimetype.split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
                return None
            if item.size < 1024:  # soft redirect page: <meta http-equiv=refresh>
                html = bytes(item.content).decode("utf-8", "replace")
                tree = HTMLParser(html)
                refresh = next((node for node in tree.css("meta[http-equiv]")
                                if node.attributes.get("http-equiv", "").lower() == "refresh"), None)
                content = refresh.attributes.get("content", "") if refresh is not None else ""
                match = re.search(r"(?:^|;)\s*url\s*=\s*(.*?)\s*$", content, re.I)
                if match:
                    href = match.group(1).strip("\"'")
                    base = tree.css_first("base[href]")
                    resolved = archive_href(href, target, base.attributes["href"] if base else None)
                    if resolved is None:
                        return None
                    target, redirect_frag = resolved
                    if redirect_frag is not None:
                        frag = redirect_frag
                    title_lookup = False
                    continue
            return target, frag
        return None

    def html(self, path: str) -> str:
        item = self.z.get_entry_by_path(path).get_item()
        _check_article_size(item.size)
        return bytes(item.content).decode("utf-8", "replace")

    def title_of(self, path: str) -> str:
        try:
            return safe_text(self.z.get_entry_by_path(path).title)
        except KeyError:
            return safe_text(path)

    def suggest(self, q: str, n: int = 5) -> list[tuple[str, str]]:
        q = q.strip()
        if not q:
            return []
        out: list[tuple[str, str]] = []
        # libzim ranks "Germany to germany" above "Germany": put the exact
        # article (following redirects, e.g. "USA" -> "United States") first
        for cand in (q, q[:1].upper() + q[1:], q.title()):
            r = self.resolve(cand)
            if r:
                out.append((r[0], self.title_of(r[0])))
                break
        for p in self._sugg.suggest(q).getResults(0, n + 1):
            if all(p != o[0] for o in out):
                out.append((p, self.title_of(p)))
        return out[:n]

    def random_path(self) -> str:
        for _ in range(50):
            e = self.z.get_random_entry()
            r = self.resolve(e.path)
            if r:
                return r[0]
        return self.z.main_entry.get_redirect_entry().path

    def main_path(self) -> str | None:
        try:
            m = self.z.main_entry
            return m.get_redirect_entry().path if m.is_redirect else m.path
        except Exception:
            return None
