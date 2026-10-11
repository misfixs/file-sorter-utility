#!/usr/bin/env python3
"""
Safe File Sorter - launcher.
============================

    python run.py                      # start the Tkinter GUI
    python run.py "D:\\Downloads"       # start the GUI with a folder pre-filled
    python run.py --cli --help         # command line mode (scripting / automation)

The GUI is the default; pass ``--cli`` to get the head-less interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--cli", "cli"):
        from filesorter.cli import main as cli_main

        return cli_main(argv[1:])
    try:
        from filesorter.gui import main as gui_main
    except ImportError as exc:  # pragma: no cover - broken install
        print(f"Cannot start the GUI: {exc}", file=sys.stderr)
        print("Tkinter is part of the standard Python distribution for Windows/macOS.", file=sys.stderr)
        print("On Debian/Ubuntu install it with:  sudo apt install python3-tk", file=sys.stderr)
        return 1
    return gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
