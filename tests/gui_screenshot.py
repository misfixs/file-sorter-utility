"""Launch the real app, screenshot it, and shut it down.

The screenshot uses .NET's System.Drawing (no third-party Python packages),
which keeps this check dependency-free on Windows.

    python tests/gui_screenshot.py <output.png> [seconds-to-wait]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

CAPTURE = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$shell = New-Object -ComObject WScript.Shell
[void]$shell.AppActivate('Safe File Sorter')
Start-Sleep -Milliseconds 900
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save($env:SHOT_PATH, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose(); $bmp.Dispose()
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "gui-screenshot.png").resolve()
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    settings = out.parent / "gui-screenshot.settings.json"

    # A scratch folder to show in the window.
    scratch = out.parent / "ScreenshotDownloads"
    scratch.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(
        ("holiday.jpg", "notes.txt", "budget.xlsx", "Invoice-2024.pdf")
    ):
        (scratch / name).write_text(f"sample {index}", encoding="utf-8")

    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT / "run.py"),
            str(scratch),
            "--settings",
            str(settings),
        ],
        cwd=str(PROJECT),
    )
    try:
        time.sleep(wait)
        env = dict(os.environ, SHOT_PATH=str(out))
        captured = False
        for shell in ("pwsh", "powershell"):
            try:
                done = subprocess.run(
                    [shell, "-NoProfile", "-Command", CAPTURE],
                    env=env,
                    capture_output=True,
                    timeout=90,
                )
                if done.returncode == 0 and out.exists() and out.stat().st_size > 0:
                    captured = True
                    break
            except (OSError, subprocess.SubprocessError):
                continue
        if not captured:
            print("screenshot failed:", out)
    finally:
        # The Tk main loop ignores terminate() on Windows, so close the tree.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
    return 0 if out.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
