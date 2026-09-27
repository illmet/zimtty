"""Measure how long libzim's title search blocks the Python main thread.

    uv run tools/gil_probe.py FILE.zim [query ...]

libzim holds the GIL, so a query in a thread still freezes the UI for its whole
duration. That's why search runs in a subprocess (suggestd.py). Re-run this if
libzim is upgraded: if 'longest UI stall' drops to ~5ms, threads would do.
"""
import sys
import threading
import time

from libzim.reader import Archive
from libzim.suggestion import SuggestionSearcher

z = Archive(sys.argv[1])
queries = sys.argv[2:] or ["G", "Ge", "Ger", "Germ", "Germany", "m", "ma", "mar", "mart"]
s = SuggestionSearcher(z)
for q in queries:
    done = threading.Event()
    beats: list[float] = []
    res = {}

    def work():
        t = time.perf_counter()
        r = s.suggest(q)
        res["n"] = len(list(r.getResults(0, 5)))
        res["dt"] = time.perf_counter() - t
        done.set()

    th = threading.Thread(target=work)
    start = time.perf_counter()
    beats.append(start)
    th.start()
    while not done.is_set():
        time.sleep(0.005)
        beats.append(time.perf_counter())
    th.join()
    beats.append(time.perf_counter())
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    print(f"{q!r:10} query {res['dt']*1000:6.0f}ms  heartbeats {len(beats):4}  "
          f"longest UI stall {max(gaps)*1000:6.0f}ms")
