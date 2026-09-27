"""ZIM access and HTML -> Article model.

The mwoffliner HTML (Parsoid output) is very regular:
  <section data-mw-section-id="0">  lead: infobox, hatnotes, lead paragraphs
  <section data-mw-section-id="N">  <div class="mw-heading mw-heading2"><h2 id=..>
      nested <section> for h3/h4 ...
We walk that tree once and produce a flat list of Blocks, which is what the
paginator works on. Styling is baked into rich Text using the current palette.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from libzim.reader import Archive
from libzim.suggestion import SuggestionSearcher
from rich.style import Style
from rich.text import Text
from selectolax.parser import HTMLParser, Node

# ---------------------------------------------------------------- model


@dataclass
class Block:
    kind: str  # title | h2 | h3 | h4 | para | item | quote | hatnote | ref | placeholder
    text: Text
    anchor: str | None = None  # heading id, for TOC / #fragment jumps
    indent: int = 0
    hang: int = 0  # width of the bullet/number prefix, for hanging indents


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


@dataclass
class Palette:
    fg: str = "#d0d0d0"
    bright: str = "#ffffff"
    muted: str = "#808080"
    accent: str = "#7aa2f7"
    link: str = "#7aa2f7"
    heading: str = "#ffffff"
    sub: str = "#bb9af7"


# ---------------------------------------------------------------- constants

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
INVISIBLE = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff\u00ad")


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

    def group(self) -> str:
        """One argument: {...} or a single token."""
        self.ws()
        if self.i >= len(self.s):
            return ""
        if self.s[self.i] == "{":
            self.i += 1
            out = self.seq("}")
            self.i += 1  # skip }
            return out
        return self.token()

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
    if "\\" not in s and "^" not in s and "_" not in s and "{" not in s:
        return s
    try:
        t = _Tex(re.sub(r"\s+([\^_])", r"\1", s)).seq()
    except (ValueError, AttributeError, IndexError):
        return s  # malformed TeX: show it raw rather than crash
    t = re.sub(r"\s*([=<>≤≥≠≈∈∉⊂⊆→⇒⇔±×])\s*", r" \1 ", t)
    t = re.sub(r"\s+([,)\]])", r"\1", t)
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def hidden(node: Node) -> bool:
    style = (node.attributes.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


# ---------------------------------------------------------------- inline text


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

    def _brk(self):
        self.segs.append(("\n", None))

    def _walk(self, node: Node, style: Style | None):
        for ch in node.iter(include_text=True):
            tag = ch.tag
            if tag == "-text":
                s = (ch.text_content or "").translate(INVISIBLE)
                if s:
                    self.segs.append((s, style))
                continue
            if tag in SKIP_TAGS or hidden(ch):
                continue
            cls = classes(ch)
            if cls & SKIP_INLINE_CLASSES:
                continue
            if tag == "sup" and ("mw-ref" in cls or "reference" in cls):
                if self.show_refs:
                    self.segs.append((ch.text(strip=True), Style(color=self.pal.muted, dim=True)))
                continue
            if tag == "br":
                self._brk()
                continue
            if tag == "a":
                href = ch.attributes.get("href") or ""
                if ch.attributes.get("role") == "button":
                    continue
                if self.links and href and not href.startswith(("#", "http:", "https:", "//", "mailto:")):
                    # "@click" meta makes Textual run the action on mouse click
                    link = Style(color=self.pal.link, underline=True,
                                 meta={"href": href, "@click": f"app.follow({href!r})"})
                    self._walk(ch, (style + link) if style else link)
                else:
                    self._walk(ch, style)
                continue
            if tag in ("b", "strong"):
                s2 = Style(bold=True)
                self._walk(ch, (style + s2) if style else s2)
                continue
            if tag in ("i", "em", "cite", "var"):
                s2 = Style(italic=True)
                self._walk(ch, (style + s2) if style else s2)
                continue
            if "mwe-math-element" in cls:
                ann = ch.css_first("annotation")
                tex = ann.text() if ann else ""
                img = ch.css_first("img")
                alt = (img.attributes.get("alt") if img else "") or tex
                alt = alt.strip()
                if alt.startswith("{\\displaystyle") and alt.endswith("}"):
                    alt = alt[len("{\\displaystyle"):-1].strip()
                self.segs.append((tex_to_text(alt), Style(italic=True, color=self.pal.sub)))
                continue
            if tag in ("ul", "ol", "dl", "table") and not self.boxy:
                continue  # nested block lists are handled by the block walker
            if self.boxy and tag in ("li", "div", "p", "tr", "dd", "dt"):
                self._brk()
                self._walk(ch, style)
                self._brk()
                continue
            self._walk(ch, style)

    def _finish(self) -> Text:
        out = Text()
        pending_space = False
        at_line_start = True
        for s, st in self.segs:
            if s == "\n":
                if not at_line_start:
                    out.append("\n")
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
        tree = HTMLParser(html)
        h1 = tree.css_first("h1")
        title = h1.text(strip=True) if h1 else (tree.css_first("title").text(strip=True) if tree.css_first("title") else path)
        self.infobox: list[InfoRow] = []
        self.notes: list[Text] = []
        self._seen_heading = False
        self._article_title = title
        sections = [s for s in tree.css("section") if s.parent is None or s.parent.tag != "section"]

        lead: list[Block] = []
        top: list[tuple[Block, list[Block]]] = []  # (h2 block, body)
        if not sections:
            # Older mwoffliner (<=1.16): flat .mw-parser-output, no <section> wrappers.
            # Walk it once, then cut at h2 headings.
            root = tree.css_first(".mw-parser-output") or tree.css_first("#mw-content-text") or tree.body
            flat: list[Block] = []
            if root is not None:
                self._walk(root, flat, in_lead=True)
            for b in flat:
                if b.kind == "h2":
                    top.append((b, []))
                elif top:
                    top[-1][1].append(b)
                else:
                    lead.append(b)
        for sec in sections:
            sid = sec.attributes.get("data-mw-section-id")
            if sid == "0":
                self._walk(sec, lead, in_lead=True)
            else:
                body: list[Block] = []
                self._walk(sec, body, in_lead=False)
                if body and body[0].kind == "h2":
                    top.append((body[0], body[1:]))
                elif top:
                    top[-1][1].extend(body)
                else:
                    lead.extend(body)

        main = [(h, b) for h, b in top if h.text.plain.strip().lower() not in BACK_MATTER]
        back = [(h, b) for h, b in top if h.text.plain.strip().lower() in BACK_MATTER]
        if self.notes:
            nh = Block("h2", Text(NOTES_TITLE), anchor="__notes__")
            main.append((nh, [Block("hatnote", n) for n in self.notes]))

        blocks = [Block("title", Text(title), anchor="__top__")] + lead
        for h, b in main + back:
            b = [x for x in b if x.kind not in ("h2",)]
            if not b:
                continue
            blocks.append(h)
            blocks.extend(b)
        blocks = self._drop_empty_headings(blocks)

        art = Article(path=path, title=title, blocks=blocks, infobox=self.infobox, notes=self.notes)
        for i, b in enumerate(blocks):
            if b.kind in ("h2", "h3", "h4"):
                art.toc.append(TocEntry(i, int(b.kind[1]), b.text.plain, b.anchor))
        return art

    @staticmethod
    def _drop_empty_headings(blocks: list[Block]) -> list[Block]:
        rank = {"h2": 2, "h3": 3, "h4": 4}
        out: list[Block] = []
        for i, b in enumerate(blocks):
            if b.kind in rank:
                if not b.text.plain.strip():
                    continue  # heading made only of template junk
                nxt = next((x for x in blocks[i + 1:]), None)
                if nxt is None or (nxt.kind in rank and rank[nxt.kind] <= rank[b.kind]):
                    continue
            out.append(b)
        return out

    def _heading_text(self, h: Node) -> Text:
        # headings can contain <style> (navbox templates); use the inline walker
        t = self.inline(h)
        return Text(t.plain)

    # -- block level
    def _walk(self, node: Node, out: list[Block], in_lead: bool):
        for ch in node.iter():
            tag = ch.tag
            if tag in SKIP_TAGS or hidden(ch):
                continue
            cls = classes(ch)
            if tag == "section":
                self._walk(ch, out, in_lead=False)
            elif "mw-heading" in cls:
                h = ch.css_first("h2, h3, h4, h5, h6")
                if h:
                    self._seen_heading = True
                    lvl = min(int(h.tag[1]), 4)
                    out.append(Block(f"h{lvl}", self._heading_text(h), anchor=h.attributes.get("id")))
            elif tag in ("h2", "h3", "h4", "h5", "h6"):
                self._seen_heading = True
                lvl = min(int(tag[1]), 4)
                out.append(Block(f"h{lvl}", self._heading_text(ch), anchor=ch.attributes.get("id")))
            elif cls & NOTE_CLASSES:
                t = self._note_text(ch)
                if t.plain:
                    self.notes.append(t)
            elif "hatnote" in cls or "dablink" in cls:
                t = self.inline(ch)
                if not t.plain:
                    continue
                if in_lead and not self._seen_heading:
                    self.notes.append(t)
                else:
                    t.stylize(Style(italic=True, color=self.pal.muted))
                    out.append(Block("hatnote", t))
            elif tag == "table" and "infobox" in cls:
                if not self.infobox:
                    self.infobox = self._infobox(ch)
            elif cls & SKIP_BLOCK_CLASSES:
                continue
            elif tag == "p":
                t = self.inline(ch)
                if t.plain:
                    out.append(Block("para", t))
            elif tag in ("ul", "ol"):
                self._list(ch, out, 0)
            elif tag == "dl":
                for d in ch.iter():
                    t = self.inline(d)
                    if not t.plain:
                        continue
                    if d.tag == "dt":
                        t.stylize("bold")
                        out.append(Block("para", t))
                    else:
                        out.append(Block("quote" if d.tag == "dd" else "para", t, indent=2))
            elif tag == "blockquote" or "quotebox" in cls or "templatequote" in cls:
                t = self.inline(ch, boxy=True)
                if t.plain:
                    t.stylize(Style(italic=True))
                    out.append(Block("quote", t, indent=4))
            elif tag == "table":
                cap = ch.css_first("caption")
                name = cap.text(strip=True) if cap else ""
                rows = len(ch.css("tr"))
                out.append(Block("placeholder", Text(f"[ table{': ' + name if name else ''} · {rows} rows · not shown yet ]", Style(color=self.pal.muted, italic=True))))
            elif "mw-references-wrap" in cls or "reflist" in cls or "refbegin" in cls or tag == "div":
                # generic containers (div-col, reflist, stack, ...): descend
                self._walk(ch, out, in_lead)

    def _list(self, lst: Node, out: list[Block], depth: int):
        ordered = lst.tag == "ol"
        refs = "references" in classes(lst)
        n = 0
        for li in lst.iter():
            if li.tag != "li":
                continue
            n += 1
            t = self.inline(li)
            if t.plain:
                if refs:
                    marker = f"{n}. "
                else:
                    marker = f"{n}. " if ordered else ("• " if depth == 0 else "◦ ")
                bullet = Text(marker, Style(color=self.pal.muted))
                out.append(Block("ref" if refs else "item", bullet + t, indent=2 + depth * 2, hang=len(marker)))
            for sub in li.iter():
                if sub.tag in ("ul", "ol"):
                    self._list(sub, out, depth + 1)

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
                return self.z.get_metadata(k).decode()
            except Exception:
                pass
        return self.path

    def resolve(self, target: str) -> tuple[str, str | None] | None:
        """Turn an href / path / title into (path, fragment), following redirects."""
        target = target.strip()
        frag = None
        if "#" in target:
            target, frag = target.split("#", 1)
        target = target.removeprefix("./")
        from urllib.parse import unquote
        target = unquote(target)
        for _ in range(5):
            if not target:
                return None
            if not self.z.has_entry_by_path(target):
                alt = target.replace(" ", "_")
                if self.z.has_entry_by_path(alt):
                    target = alt
                elif self.z.has_entry_by_title(target):
                    target = self.z.get_entry_by_title(target).path
                else:
                    return None
            e = self.z.get_entry_by_path(target)
            if e.is_redirect:
                e = e.get_redirect_entry()
                target = e.path
                continue
            item = e.get_item()
            if item.mimetype != "text/html":
                return None
            if item.size < 1024:  # soft redirect page: <meta http-equiv=refresh>
                html = bytes(item.content).decode("utf-8", "replace")
                m = re.search(r"URL='?([^'\"]+)'?", html) if "refresh" in html else None
                if m:
                    nt = m.group(1)
                    if "#" in nt:
                        nt, frag = nt.split("#", 1)
                    target = unquote(nt.removeprefix("./"))
                    continue
            return target, frag
        return None

    def html(self, path: str) -> str:
        return bytes(self.z.get_entry_by_path(path).get_item().content).decode("utf-8", "replace")

    def title_of(self, path: str) -> str:
        try:
            return self.z.get_entry_by_path(path).title
        except KeyError:
            return path

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
