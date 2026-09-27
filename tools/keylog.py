"""Log raw terminal input bytes, optionally with the kitty keyboard flags Textual uses.

    uv run tools/keylog.py OUTFILE [kitty]

Run it in a real terminal (it takes over the screen; ctrl-c exits). This is how to
see what the terminal actually sends for odd keys: Compose macros, dead keys,
IME input. Compare with and without `kitty`.
"""
import os
import select
import sys
import termios
import time
import tty

out = open(sys.argv[1], "w", buffering=1)
kitty = len(sys.argv) > 2 and sys.argv[2] == "kitty"
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
try:
    sys.stdout.write("\x1b[?1049h")
    if kitty:
        sys.stdout.write("\x1b[>25u")  # 1|8|16 = what Textual 8 requests
    sys.stdout.write(f"keylog kitty={kitty}; ctrl-c exits\r\n")
    sys.stdout.flush()
    out.write(f"START kitty={kitty}\n")
    end = time.time() + 120
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        data = os.read(fd, 1024)
        out.write(f"{time.time():.3f} {data!r}\n")
        sys.stdout.write(repr(data) + "\r\n")
        sys.stdout.flush()
        if data in (b"\x03", b"\x1b[99;5u"):
            break
finally:
    if kitty:
        sys.stdout.write("\x1b[<u")
    sys.stdout.write("\x1b[?1049l")
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
