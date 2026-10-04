"""Title-suggestion server, run as a separate process.

Why a process: libzim's SuggestionSearcher does not release Python's GIL, so a
"background" thread still freezes the Textual UI while it runs. On the full
English ZIM a one-letter query takes ~1-2 s, so typing into the search box
stalled the screen (keystrokes and results appeared seconds late). A separate
process has its own GIL.

Protocol (JSON lines):  stdin  {"id": 3, "q": "mart"}
                        stdout {"id": 3, "q": "mart", "results": [[path, title], ...]}
The parent sends one active request and retains only its newest pending query.
This helper executes each submitted request serially.
"""
from __future__ import annotations

import json
import sys


def serve(zim_path: str) -> None:
    from .zimdoc import Zim

    zim = Zim(zim_path)
    out = sys.stdout
    print(json.dumps({"ready": True}), file=out, flush=True)
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if (not isinstance(msg, dict) or type(msg.get("id")) is not int
                or not isinstance(msg.get("q"), str)):
            continue
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
