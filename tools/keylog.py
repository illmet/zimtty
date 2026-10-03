"""Inspect terminal input structure without saving or displaying input content.

    uv run tools/keylog.py [kitty]

Run in a real terminal; Ctrl-C exits and restores its previous settings. Each
read reports only its byte count and whether it contained an escape character.
The probe stops after 256 reads or two minutes. Compare runs with and without
`kitty` to investigate keyboard-protocol behavior. No file is created; the old
OUTFILE argument is rejected. The historical filename is retained for compatibility.
"""
import os
import re
import select
import sys
import termios
import time
import tty


USAGE = "usage: keylog.py [kitty]"
_KITTY_CTRL_C = re.compile(rb"\x1b\[99;5(?::[123])?(?:;[0-9:]+)?u")


def probe(kitty: bool) -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1049h")
        if kitty:
            sys.stdout.write("\x1b[>25u")  # 1|8|16 = what Textual 8 requests
        sys.stdout.write(f"Input structure probe; kitty={'on' if kitty else 'off'}; Ctrl-C exits.\r\n")
        sys.stdout.flush()
        end = time.monotonic() + 120
        events = 0
        tail = b""
        while events < 256 and time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            data = os.read(fd, 1024)
            if not data:
                break
            # Terminal reads may combine keys or divide an escape sequence.
            pending = tail + data
            if b"\x03" in data or _KITTY_CTRL_C.search(pending):
                break
            tail = pending[-64:]
            events += 1
            escaped = "yes" if b"\x1b" in data else "no"
            sys.stdout.write(f"event={events} bytes={len(data)} escape={escaped}\r\n")
            sys.stdout.flush()
    finally:
        # Restore terminal attributes even if writing the protocol cleanup fails.
        try:
            if kitty:
                sys.stdout.write("\x1b[<u")
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if argv not in ([], ["kitty"]):
        print(f"{USAGE}\nkeylog.py: invalid arguments; use --help", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("keylog.py: an interactive terminal is required", file=sys.stderr)
        return 2
    try:
        probe(bool(argv))
    except KeyboardInterrupt:
        pass
    except (OSError, termios.error, ValueError):
        print("keylog.py: terminal probe failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
