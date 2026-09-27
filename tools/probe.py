"""Poke at a ZIM file: metadata, title search, and the HTML structure of one article.

    uv run tools/probe.py FILE.zim ["Article title"]

Writes the raw article HTML to /tmp/probe.html for closer inspection
(tools/ibox_raw.py shows infobox rows specifically).
"""
import sys
from collections import Counter

from libzim.reader import Archive
from libzim.suggestion import SuggestionSearcher
from selectolax.parser import HTMLParser

path = sys.argv[1]
title = sys.argv[2] if len(sys.argv) > 2 else None
z = Archive(path)
print("entries:", z.entry_count, "articles:", z.article_count)
print("has fulltext:", z.has_fulltext_index, "has title idx:", z.has_title_index)
for k in ("Title", "Date", "Scraper", "Name", "Flavour"):
    try:
        print(f"  {k}: {z.get_metadata(k).decode()}")
    except Exception:
        pass
print("main:", z.main_entry.get_item().path if z.main_entry.is_redirect is False else z.main_entry.get_redirect_entry().path)

if not title:
    sys.exit()

for q in (title, title.lower()):
    s = SuggestionSearcher(z).suggest(q)
    print(f"suggest {q!r}: {s.getEstimatedMatches()} ->", list(s.getResults(0, 5)))

entry = z.get_entry_by_path(list(SuggestionSearcher(z).suggest(title).getResults(0, 1))[0])
if entry.is_redirect:
    entry = entry.get_redirect_entry()
item = entry.get_item()
html = bytes(item.content).decode()
print("path:", entry.path, "mime:", item.mimetype, "bytes:", len(html))
open("/tmp/probe.html", "w").write(html)

t = HTMLParser(html)
body = t.body
classes = Counter()
for n in body.traverse():
    for c in (n.attributes.get("class") or "").split():
        classes[c] += 1
print("top classes:", classes.most_common(45))
print("sections:", len(t.css("section")), "details:", len(t.css("details")))
print("headings:", [(h.tag, h.text(strip=True)) for h in t.css("h1,h2,h3")][:25])
for sel in (".infobox", "table.infobox", ".hatnote", ".ambox", ".mw-ref", "sup.reference", "a[href]", ".mw-references", "ol.references", "p"):
    print(f"{sel:18} {len(t.css(sel))}")
