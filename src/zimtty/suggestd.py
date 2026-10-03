"""Title-suggestion server, run as a separate process.

Why a process: libzim's SuggestionSearcher does not release Python's GIL, so a
"background" thread still freezes the Textual UI while it runs. On the full
English ZIM a one-letter query takes ~1-2 s, so typing into the search box
stalled the screen (keystrokes and results appeared seconds late). A separate
process has its own GIL.

Protocol (JSON lines):  stdin  {"id": 3, "q": "mart"}
                        stdout {"id": 3, "q": "mart", "results": [[path, title], ...]}
Only the newest pending query is answered; stale ones are skipped.
"""
from __future__ import annotations

import json
import sys
import threading


def serve(zim_path: str) -> None:
    from .zimdoc import Zim

    zim = Zim(zim_path)
    latest: dict | None = None
    cond = threading.Condition()
    eof = False

    def reader():
        nonlocal latest, eof
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with cond:
                latest = msg  # overwrite: older unanswered queries are dropped
                cond.notify()
        with cond:
            eof = True
            cond.notify()

    threading.Thread(target=reader, daemon=True).start()
    out = sys.stdout
    print(json.dumps({"ready": True}), file=out, flush=True)
    while True:
        with cond:
            while latest is None and not eof:
                cond.wait()
            if latest is None and eof:
                return
            msg, latest = latest, None
        response = {"id": msg.get("id"), "q": msg.get("q"), "results": []}
        try:
            res = zim.suggest(msg.get("q", ""), int(msg.get("n", 5)))
        except Exception:  # never serialize error text (it can include the query)
            res = []
            response["error"] = "search_failed"
        response["results"] = res
        print(json.dumps(response), file=out, flush=True)


if __name__ == "__main__":
    try:
        serve(sys.argv[1])
    except Exception:
        # The parent handles process failure; never write raw traceback content.
        raise SystemExit(1) from None
