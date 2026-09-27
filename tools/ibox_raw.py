"""Show the raw HTML skeleton (tags + classes) of infobox rows matching a pattern.

    uv run tools/ibox_raw.py FILE.zim "Article title" "regex on row text"

Use it when an infobox renders oddly: it shows which classes/nesting the row uses.
"""
import re
import sys

from selectolax.parser import HTMLParser

from zimtty.zimdoc import Zim

zim = Zim(sys.argv[1])
title, pat = sys.argv[2], re.compile(sys.argv[3])
p, _ = zim.resolve(title)
t = HTMLParser(zim.html(p))
ib = t.css_first("table.infobox")


def skel(n, d=0, maxd=6):
    if d > maxd:
        return
    for c in n.iter(include_text=False):
        if c.tag in ("style",):
            continue
        txt = re.sub(r"\s+", " ", c.text(deep=False) or "").strip()[:40]
        print("  " * d + f"<{c.tag} class={c.attributes.get('class')!r} style={(c.attributes.get('style') or '')[:40]!r}> {txt}")
        skel(c, d + 1, maxd)


rows = [tr for tr in ib.css("tr") if pat.search(tr.text(strip=True))]
# innermost matching row
rows = [r for r in rows if not any(pat.search(x.text(strip=True)) for x in r.css("tr") if x is not r and x.mem_id != r.mem_id)]
print("matching rows:", len(rows))
for tr in rows[:1]:
    print("=== ROW")
    skel(tr, 0)
