"""Render an article to plain text, page by page, without the UI.

    uv run tools/render.py FILE.zim "Article title" [WIDTH] [HEIGHT] [PAGES]

Shows the TOC, moved notes, timings, and each page with the infobox column
beside it. Handy for eyeballing layout changes on real articles.
"""
import sys
import time

from zimtty.paginate import Layout, Paginator
from zimtty.zimdoc import ArticleParser, Palette, Zim

zim = Zim(sys.argv[1])
q = sys.argv[2]
W = int(sys.argv[3]) if len(sys.argv) > 3 else 140
H = int(sys.argv[4]) if len(sys.argv) > 4 else 40
maxpages = int(sys.argv[5]) if len(sys.argv) > 5 else 3

r = zim.resolve(q) or zim.resolve(zim.suggest(q, 1)[0][0])
t0 = time.perf_counter()
art = ArticleParser(Palette()).parse(r[0], zim.html(r[0]))
t1 = time.perf_counter()
info_w = 38 if W >= 96 else 0
lay = Layout(height=H, full_width=min(W, 88),
             narrow_width=min(88, W - info_w - 7) if info_w else min(W, 88), info_width=info_w)
pages = Paginator(Palette()).paginate(art, lay, set())
t2 = time.perf_counter()
print(f"{art.title!r}: {len(art.blocks)} blocks, {len(art.infobox)} infobox rows, {len(art.notes)} notes, "
      f"{len(art.toc)} toc, {len(pages)} pages | parse {1000*(t1-t0):.0f}ms paginate {1000*(t2-t1):.0f}ms")
print("TOC:", ["  " * (e.level - 2) + e.title for e in art.toc][:40])
print("NOTES:", [n.plain[:120] for n in art.notes])
for pi, p in enumerate(pages[:maxpages]):
    more = " +more" if p.info_more else ""
    print(f"\n======== page {pi+1}/{len(pages)} (text width {p.width}, {len(p.lines)} lines, info {len(p.info)}{more})")
    for k in range(max(len(p.lines), len(p.info))):
        left = p.lines[k].plain if k < len(p.lines) else ""
        right = p.info[k].plain if k < len(p.info) else ""
        print(f"{left:<{p.width}} │ {right}" if p.info else left)
