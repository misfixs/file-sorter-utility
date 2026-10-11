"""End-to-end test of the GUI wiring: buttons -> worker -> log -> undo.

This drives the real :class:`filesorter.gui.SorterApp` on a throw-away folder
and stubs out only the modal dialogs (which would otherwise block).  It proves
the whole path works: config building, worker thread, queue pump, log output,
file moves and undo.

    python tests/gui_workflow.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filesorter import core, gui  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent


def scratch_root() -> Path:
    for candidate in (Path(tempfile.gettempdir()), PROJECT / ".tmp"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe-{os.getpid()}.gui"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return PROJECT


def make_folder(root: Path) -> Path:
    folder = root / f"gui-workflow-{os.urandom(4).hex()}"
    folder.mkdir()
    for index, name in enumerate(
        ("holiday.jpg", "notes.txt", "budget.xlsx", "invoice-2024.pdf", "song.mp3")
    ):
        (folder / name).write_text(f"content {index}", encoding="utf-8")
    (folder / "nested").mkdir()
    (folder / "nested" / "deep.png").write_text("nested", encoding="utf-8")
    return folder


def wait_for_idle(app: gui.SorterApp, root: tk.Tk, timeout: float = 120.0) -> None:
    """Wait until the worker is gone, the queue is drained and the job settled.

    The machine may be busy with other work, so this waits on observable state
    (worker finished + queue empty + controls re-enabled) rather than on a
    fixed number of event-loop turns.
    """
    deadline = time.time() + timeout
    settled = False
    while time.time() < deadline:
        root.update()
        worker_done = app.worker is None or not app.worker.is_alive()
        if worker_done and app.msg_queue.empty() and str(app.stop_button["state"]) == "disabled":
            settled = True
            break
        time.sleep(0.05)
    # A couple of extra turns so any final dialog/log work lands.
    for _ in range(6):
        root.update()
        time.sleep(0.02)
    if not settled:
        raise TimeoutError("the GUI worker did not finish in time")


def main() -> int:
    root_dir = scratch_root()
    folder = make_folder(root_dir)
    checks: list[tuple[str, bool]] = []

    def check(label: str, condition: bool, detail: object = "") -> None:
        checks.append((label, bool(condition)))
        suffix = f"   ({detail})" if detail != "" else ""
        print(("PASS  " if condition else "FAIL  ") + label + suffix)

    root = tk.Tk()
    root.geometry("1000x700+30+30")
    os.environ[gui.SETTINGS_ENV] = str(root_dir / "gui-workflow-settings")
    app = gui.SorterApp(root)

    # Stub the modal dialogs so the test can run unattended.
    dialogs: list[str] = []
    gui.messagebox.showinfo = lambda *a, **k: dialogs.append(a[0] if a else "?")
    gui.messagebox.showwarning = lambda *a, **k: dialogs.append("warn")
    gui.messagebox.showerror = lambda *a, **k: dialogs.append("error")
    gui.messagebox.askyesno = lambda *a, **k: True

    app.folder_var.set(str(folder))
    app.group_var.set(core.GROUP_CATEGORY)
    app.recurse_var.set(True)
    app.dedupe_var.set(False)
    app._on_group_change()
    root.update()

    # -- 1. preview (dry run) ---------------------------------------------
    app.start_sort(preview=True)
    wait_for_idle(app, root)
    log_text = app.log.get("1.0", "end")
    check("preview ran and reported actions", "[would]" in log_text)
    check("preview left the folder untouched", (folder / "holiday.jpg").is_file())
    check("preview dialog shown", "Preview complete" in dialogs)

    # -- 2. real sort ------------------------------------------------------
    dialogs.clear()
    app.confirm_and_sort()
    wait_for_idle(app, root)
    check("sort moved files into categories", (folder / "Images" / "holiday.jpg").is_file())
    check("sort kept every file", len(list(folder.rglob("*.png"))) == 1)
    check("sort dialog shown", "Sort finished" in dialogs)
    check("status bar updated", "moved" in app.status_var.get())
    check("undo button re-enabled", str(app.undo_button["state"]) == "normal")

    # -- 3. a second run must be a no-op -----------------------------------
    dialogs.clear()
    app.confirm_and_sort()
    wait_for_idle(app, root)
    check("second run changed nothing", (folder / "Images" / "holiday.jpg").is_file())

    # -- 4. undo -----------------------------------------------------------
    dialogs.clear()
    app.undo_last()
    wait_for_idle(app, root)
    restored = sorted(p.name for p in folder.iterdir() if p.is_file())
    check(
        "undo restored the original layout",
        restored == ["budget.xlsx", "holiday.jpg", "invoice-2024.pdf", "notes.txt", "song.mp3"],
    )
    check("undo dialog shown", "Undo finished" in dialogs)
    check("undo handler caught an error", "error" not in dialogs)

    # -- 5. the "dry run only" switch must govern SORT FILES too -----------
    def snapshot() -> list[str]:
        return sorted(
            str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
        )

    before_dry = snapshot()
    dialogs.clear()
    app.dry_run_var.set(True)
    root.update()
    app.confirm_and_sort()  # ticked => this must simulate, not move
    wait_for_idle(app, root)
    check("dry-run switch honoured by SORT FILES (disk untouched)", snapshot() == before_dry)
    check("dry-run switch reported a preview", "Preview complete" in dialogs)
    app.dry_run_var.set(False)
    root.update()

    # -- 6. invalid input is refused, not crashed --------------------------
    app.folder_var.set(str(folder / "does-not-exist"))
    dialogs.clear()
    app.start_sort(preview=False)
    check("missing folder refused with a warning", dialogs == ["warn"])

    # -- 7. numeric and path validation ------------------------------------
    app.folder_var.set(str(folder))
    for bad in ("nan", "inf", "1e400"):
        app.min_size_var.set(bad)
        dialogs.clear()
        app.start_sort(preview=True)
        check(f"size {bad!r} refused", dialogs == ["warn"])
    app.min_size_var.set("100")
    app.max_size_var.set("10")
    dialogs.clear()
    app.start_sort(preview=True)
    check("min > max refused", dialogs == ["warn"])
    app.min_size_var.set("0")
    app.max_size_var.set("0")
    app.folder_var.set(f'"{folder}"')  # quoted path must still work
    config = app._build_config()
    check("quoted folder path accepted", config.root == folder)

    # -- 8. log colouring uses the engine's level token --------------------
    app.clear_log()
    for line in ("10:00:00 ERROR-ish", "10:00:00 ERR boom", "10:00:00 WARN careful",
                 "10:00:00 OK good", "-" * 20):
        app._emit_line(line)
    tags = []
    for index in range(1, 6):
        names = app.log.tag_names(f"{index}.0")
        tags.append(names[0] if names else None)
    check(
        "log levels are coloured",
        tags == ["INFO", "ERR", "WARN", "OK", "----"],
        tags,
    )
    app.clear_log()

    app.on_close()
    shutil.rmtree(folder, ignore_errors=True)

    failed = [label for label, ok in checks if not ok]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("failed: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
