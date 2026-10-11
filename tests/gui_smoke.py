"""Smoke test: build the real Tk window, render it, and save a screenshot.

Run:  python tests/gui_smoke.py [output.png]
The window is created off-screen-safe (normal position), updated a few times so
that every widget is realised, captured with Pillow's ImageGrab when available,
and then destroyed.  Exit code 0 means the GUI built without raising.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filesorter import core, gui  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("gui-screenshot.png")
    # Keep the scratch settings file out of the real user profile.
    os.environ[gui.SETTINGS_ENV] = str(out.resolve().parent)
    root = tk.Tk()
    root.title(f"{core.APP_NAME} {core.APP_VERSION}")
    root.geometry("1040x760+10+10")
    app = gui.SorterApp(root)

    # Fill in some plausible content so the screenshot shows a real workflow.
    app.folder_var.set(str(Path.home() / "Downloads"))
    app.group_var.set(core.GROUP_CATEGORY)
    app._on_group_change()
    app.rename_tpl_var.set("{year}{month}{day}_{stem}{ext}")
    app.dedupe_var.set(True)
    app.log.delete("1.0", "end")
    for line in (
        "---- " + "-" * 68,
        "Safe File Sorter 1.0.0",
        "---- " + "-" * 68,
        "10:24:07  INFO Folder : C:\\Users\\me\\Downloads",
        "10:24:07  INFO Mode   : category",
        "10:24:07  INFO DRY RUN - files will be listed but not touched.",
        "---- " + "-" * 68,
        "10:24:07  INFO Found 240 candidate file(s).",
        "10:24:07  INFO Planned: 214 action(s): 214 move(s), 0 rename(s)",
        "10:24:07  INFO [dry-run] [move] holiday.jpg  ->  Images\\holiday.jpg",
        "10:24:07  INFO [dry-run] [move] budget.xlsx  ->  Spreadsheets\\budget.xlsx",
        "10:24:07  WARN notes.txt: destination exists - using notes (2).txt  ",
        "10:24:07  ERR  locked.db: move failed (the file is in use by another program)",
        "---- " + "-" * 68,
        "10:24:07  OK   Dry run complete - no file was touched.",
    ):
        app._emit_line(line)
    app.status_var.set("Preview complete - nothing was changed.")
    app.progress.configure(value=100, maximum=100)

    for _ in range(6):
        root.lift()
        root.attributes("-topmost", True)
        root.update_idletasks()
        root.update()

    # Screenshot the whole window (title bar included) with .NET.
    saved = False
    left = max(0, root.winfo_rootx() - 8)
    top = max(0, root.winfo_rooty() - 32)
    width = root.winfo_width() + 26
    height = root.winfo_height() + 48
    capture = (
        "Add-Type -AssemblyName System.Windows.Forms, System.Drawing;"
        f"$r=New-Object System.Drawing.Rectangle({left},{top},{width},{height});"
        "$bmp=New-Object System.Drawing.Bitmap $r.Width,$r.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($r.Location,[System.Drawing.Point]::Empty,$r.Size);"
        f"$bmp.Save('{out.resolve()}',[System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose();$bmp.Dispose()"
    )
    for shell in ("pwsh", "powershell"):
        try:
            done = subprocess.run(
                [shell, "-NoProfile", "-Command", capture],
                capture_output=True,
                timeout=60,
            )
            saved = done.returncode == 0 and out.exists()
            if saved:
                break
        except (OSError, subprocess.SubprocessError):
            continue

    # Also exercise the settings persistence and the help window logic.
    app._remember()
    print("window size:", root.winfo_width(), "x", root.winfo_height())
    print("settings file:", gui.settings_path().exists())
    print("widgets realised:", len(root.winfo_children()) > 0)
    print("log lines:", app.log.index("end-1c"))
    print("screenshot:", out if saved else "not saved")

    app.on_close()
    settings = Path(os.environ[gui.SETTINGS_ENV]) / gui.SETTINGS_NAME
    if settings.exists():
        try:
            settings.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
