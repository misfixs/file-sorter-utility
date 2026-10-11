"""
filesorter.gui
==============

Tkinter front-end for the Safe File Sorter.

Run it with::

    python -m filesorter.gui
    python run.py

Layout
------
    +------------------------------------------------------------------+
    | Safe File Sorter - tidy a folder without losing anything         |
    +------------------------------------------------------------------+
    | Folder to tidy: [ C:\\Users\\me\\Downloads        ] [Browse] [Open] |
    | [x] include sub-folders   [ ] include hidden/system files        |
    +---------------------+--------------------------------------------+
    | Sorting rules       | Templates & filters                        |
    | ( ) by extension    | folder template  [ {category}/{year}     ] |
    | ( ) by category     | rename template  [ photo_{year}{month}...] |
    | ( ) by date         | date format      [ %Y/%m-%B             ]  |
    | ( ) by keyword      | keywords         [ invoice, receipt     ]  |
    | ( ) combined        | only extensions  [ pdf, jpg             ]  |
    | ( ) no subfolders   | size between     [ 0 ] and [ 0 ] KB        |
    |                     | conflicts: ( ) rename  ( ) skip            |
    |                     | [x] dry run preview                        |
    |                     | [x] move exact duplicates to _Duplicates   |
    +---------------------+--------------------------------------------+
    | [ Preview (dry run) ] [ SORT FILES ] [ Undo last run ] [ STOP ]  |
    +------------------------------------------------------------------+
    | log output (scrollable, timestamped)                             |
    | ...                                                              |
    +------------------------------------------------------------------+
    | Ready.  |  progress bar                                       |
    +------------------------------------------------------------------+

All file work happens on a worker thread; the UI only ever receives small
messages through a queue, so the window keeps repainting during a long run.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # allow both "python -m filesorter.gui" and "python filesorter/gui.py"
    from . import core
except ImportError:  # pragma: no cover - direct execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from filesorter import core

APP_TITLE = f"{core.APP_NAME} {core.APP_VERSION}"
SETTINGS_NAME = "filesorter-gui-settings.json"
#: Set this environment variable to keep the settings file somewhere else
#: (used by the test scripts so they never touch the real user profile).
SETTINGS_ENV = "FILESORTER_SETTINGS"
MAX_LOG_LINES = 6000

PLACEHOLDER_HELP = """Folder template placeholders (leave empty to use the chosen mode):
  {ext} {EXT}            file extension, with or without the dot (.pdf / PDF)
  {category}             Images, Documents, Video, Audio, Archives, Code, ...
  {keyword}              first matching keyword from your list
  {year} {month} {monthname} {day} {weekday} {hour} {minute} {second}
  {date}                 the date format below, e.g. %Y/%m-%B
  {name}                 original file name with extension
  {stem}                 original file name without extension
  Examples:   {category}          {year}/{monthname}       Archive/{ext}

Rename template placeholders (leave empty to keep original names):
  {stem}{ext}            original name, unchanged (add a prefix/suffix instead)
  IMG_{year}{month}{day}_{stem}{ext}
  {keyword}-{stem}{ext}
  Note: the original extension is appended automatically when the template does
  not already contain {ext}.

Reference date keys: %Y year, %m month, %B month name, %d day, %A weekday."""


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def settings_dir() -> Path:
    """Per-user folder for the settings file (never the program folder)."""
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "SafeFileSorter"


def settings_path() -> Path:
    """Full path of the settings file (``--settings`` may override it)."""
    override = Path(SETTINGS_NAME).expanduser()
    if override.is_absolute():
        return override
    return settings_dir() / override


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except (OSError, TypeError):
        pass


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class SorterApp(ttk.Frame):
    """The whole GUI, as a Frame so it can be embedded or tested."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=8)
        self.master = master
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.recent_journal: Path | None = None
        self.last_plan: core.PlanResult | None = None

        self.folder_var = tk.StringVar()
        self.group_var = tk.StringVar(value=core.GROUP_EXTENSION)
        self.folder_tpl_var = tk.StringVar()
        self.rename_tpl_var = tk.StringVar()
        self.date_fmt_var = tk.StringVar(value="%Y/%m-%B")
        self.keywords_var = tk.StringVar()
        self.ext_filter_var = tk.StringVar()
        self.min_size_var = tk.StringVar(value="0")
        self.max_size_var = tk.StringVar(value="0")
        self.conflict_var = tk.StringVar(value=core.CONFLICT_RENAME)
        self.recurse_var = tk.BooleanVar(value=False)
        self.hidden_var = tk.BooleanVar(value=False)
        self.dedupe_var = tk.BooleanVar(value=False)
        # Unticked by default: pressing SORT FILES should do what it says.
        # The dedicated "Preview (dry run)" button is the safe way to look first.
        self.dry_run_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")

        self._build_widgets()
        self._load_settings()
        self._on_group_change()
        self.after(80, self._pump_queue)

    # -- construction ------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        row = 0

        header = ttk.Label(
            self,
            text=f"{core.APP_NAME} \u2014 tidy a folder without losing anything",
            font=("Segoe UI", 13, "bold"),
        )
        header.grid(row=row, column=0, sticky="w", pady=(0, 6))
        row += 1

        # ---- source folder ------------------------------------------------
        source = ttk.LabelFrame(self, text=" 1. Folder to tidy ", padding=8)
        source.grid(row=row, column=0, sticky="ew", pady=4)
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="Folder:").grid(row=0, column=0, sticky="w")
        self.folder_entry = ttk.Entry(source, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(source, text="Browse\u2026", command=self.browse_folder).grid(row=0, column=2)
        ttk.Button(source, text="Open in Explorer", command=self.open_folder).grid(
            row=0, column=3, padx=(6, 0)
        )
        options = ttk.Frame(source)
        options.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(options, text="include sub-folders", variable=self.recurse_var).pack(
            side="left"
        )
        ttk.Checkbutton(
            options, text="include hidden / system files", variable=self.hidden_var
        ).pack(side="left", padx=12)
        ttk.Label(
            options,
            text="(quotes and %VARIABLES% are fine)",
            foreground="#666666",
        ).pack(side="left", padx=12)
        row += 1

        # ---- rules + templates -------------------------------------------
        middle = ttk.Frame(self)
        middle.grid(row=row, column=0, sticky="ew", pady=4)
        middle.columnconfigure(0, weight=0)
        middle.columnconfigure(1, weight=1)

        rules = ttk.LabelFrame(middle, text=" 2. Sorting rules ", padding=8)
        rules.grid(row=0, column=0, sticky="nsew")
        for mode in core.GROUPING_MODES:
            ttk.Radiobutton(
                rules,
                text=core.GROUPING_LABELS[mode],
                value=mode,
                variable=self.group_var,
                command=self._on_group_change,
            ).pack(anchor="w")

        right = ttk.Frame(middle)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)

        tpl = ttk.LabelFrame(right, text=" 3. Templates, keyword & filters ", padding=8)
        tpl.grid(row=0, column=0, sticky="ew")
        tpl.columnconfigure(0, weight=0)
        tpl.columnconfigure(1, weight=1)
        tpl.columnconfigure(2, weight=0)
        tpl.columnconfigure(3, weight=1)
        line = 0

        ttk.Label(tpl, text="Folder template:").grid(row=line, column=0, sticky="w")
        ttk.Entry(tpl, textvariable=self.folder_tpl_var).grid(
            row=line, column=1, columnspan=3, sticky="ew", padx=6, pady=2
        )
        line += 1
        ttk.Label(tpl, text="Rename template:").grid(row=line, column=0, sticky="w")
        ttk.Entry(tpl, textvariable=self.rename_tpl_var).grid(
            row=line, column=1, columnspan=3, sticky="ew", padx=6, pady=2
        )
        line += 1
        ttk.Label(tpl, text="Date format:").grid(row=line, column=0, sticky="w")
        self.date_entry = ttk.Entry(tpl, textvariable=self.date_fmt_var, width=18)
        self.date_entry.grid(row=line, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(tpl, text="Only these extensions:").grid(row=line, column=2, sticky="w")
        self.ext_entry = ttk.Entry(tpl, textvariable=self.ext_filter_var)
        self.ext_entry.grid(row=line, column=3, sticky="ew", padx=6, pady=2)
        line += 1
        ttk.Label(tpl, text="Keywords (comma separated):").grid(row=line, column=0, sticky="w")
        self.keywords_entry = ttk.Entry(tpl, textvariable=self.keywords_var)
        self.keywords_entry.grid(row=line, column=1, columnspan=3, sticky="ew", padx=6, pady=2)
        line += 1
        size_box = ttk.Frame(tpl)
        size_box.grid(row=line, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(size_box, text="Ignore files smaller than").pack(side="left")
        ttk.Entry(size_box, textvariable=self.min_size_var, width=8).pack(side="left", padx=4)
        ttk.Label(size_box, text="KB and larger than").pack(side="left")
        ttk.Entry(size_box, textvariable=self.max_size_var, width=8).pack(side="left", padx=4)
        ttk.Label(size_box, text="KB   (0 = no limit)").pack(side="left")
        line += 1
        ttk.Label(tpl, text="Leave a template empty to use the chosen sorting rule.", foreground="#666666").grid(
            row=line, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )
        line += 1

        safety = ttk.LabelFrame(right, text=" 4. Safety ", padding=8)
        safety.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        safety.columnconfigure(0, weight=1)
        conflicts = ttk.Frame(safety)
        conflicts.grid(row=0, column=0, sticky="w")
        ttk.Label(conflicts, text="When a name already exists:").pack(side="left")
        ttk.Radiobutton(
            conflicts, text="keep both (rename)", value=core.CONFLICT_RENAME, variable=self.conflict_var
        ).pack(side="left", padx=6)
        ttk.Radiobutton(
            conflicts, text="skip the file", value=core.CONFLICT_SKIP, variable=self.conflict_var
        ).pack(side="left")
        ttk.Checkbutton(
            safety,
            text="move exact duplicates into a _Duplicates folder",
            variable=self.dedupe_var,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(
            safety,
            text="dry run only \u2014 show what would happen, change nothing",
            variable=self.dry_run_var,
        ).grid(row=2, column=0, sticky="w")
        self.help_button = ttk.Button(safety, text="Show placeholder help", command=self.show_help)
        self.help_button.grid(row=3, column=0, sticky="w", pady=(6, 0))
        row += 1

        # ---- actions ------------------------------------------------------
        actions = ttk.Frame(self)
        actions.grid(row=row, column=0, sticky="ew", pady=6)
        self.preview_button = ttk.Button(
            actions, text="Preview (dry run)", command=lambda: self.start_sort(preview=True)
        )
        self.preview_button.pack(side="left")
        self.sort_button = ttk.Button(
            actions, text="\u25b6  SORT FILES", command=self.confirm_and_sort
        )
        self.sort_button.pack(side="left", padx=6)
        self.undo_button = ttk.Button(actions, text="Undo last run", command=self.undo_last)
        self.undo_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="STOP", command=self.stop_work, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Clear log", command=self.clear_log).pack(side="right")
        ttk.Button(actions, text="Save log\u2026", command=self.save_log).pack(side="right", padx=6)
        row += 1

        # ---- log ----------------------------------------------------------
        log_frame = ttk.LabelFrame(self, text=" Summary log ", padding=6)
        log_frame.grid(row=row, column=0, sticky="nsew", pady=4)
        self.rowconfigure(row, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame,
            height=12,
            width=40,
            wrap="none",
            font=("Consolas", 9),
            background="#101418",
            foreground="#d6e2ea",
            insertbackground="#d6e2ea",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.log.tag_configure("ERR", foreground="#ff8a80")
        self.log.tag_configure("WARN", foreground="#ffd479")
        self.log.tag_configure("OK", foreground="#8ee98a")
        self.log.tag_configure("INFO", foreground="#d6e2ea")
        self.log.tag_configure("----", foreground="#6ea8d8")
        self.log.configure(state="disabled")

        # ---- status -------------------------------------------------------
        status = ttk.Frame(self)
        status.grid(row=row + 1, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(status, mode="determinate", length=240)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self._log("----", f"{APP_TITLE} ready. Pick a folder, choose your rules, then press Preview.")

    # -- helpers -----------------------------------------------------------
    def _on_group_change(self) -> None:
        mode = self.group_var.get()
        needs_keywords = mode in (core.GROUP_KEYWORD, core.GROUP_COMBINED)
        needs_date = mode in (core.GROUP_DATE, core.GROUP_COMBINED)
        self.keywords_entry.configure(state="normal" if needs_keywords else "disabled")
        self.date_entry.configure(state="normal" if needs_date else "disabled")

    def _log(self, level: str, message: str) -> None:
        """Append one already-formatted-or-raw line to the text box."""
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", level if level in ("ERR", "WARN", "OK", "----") else "INFO")
        # keep memory bounded
        if int(self.log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _emit_line(self, line: str) -> None:
        """Route one engine log line to the right colour tag.

        Engine lines look like ``18:15:49  INFO message``; the level is the
        second token.  Separator rules are plain dashes.
        """
        stripped = line.strip()
        if stripped and set(stripped) <= {"-"}:
            self._log("----", line)
            return
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[1] in ("INFO", "WARN", "ERR", "OK"):
            self._log(parts[1], line)
        elif len(parts) >= 2 and parts[1] in ("scanning", "planning", "executing"):
            self._log("----", line)
        else:
            self._log("INFO", line)

    # -- user actions ------------------------------------------------------
    def browse_folder(self) -> None:
        initial = self.folder_var.get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Choose the folder to tidy", initialdir=initial)
        if chosen:
            self.folder_var.set(os.path.normpath(chosen))
            self._log("INFO", f"Selected folder: {chosen}")

    def open_folder(self) -> None:
        path = self._folder_path()
        if path is None or not path.is_dir():
            messagebox.showwarning(APP_TITLE, "Choose an existing folder first.")
            return
        folder = str(path)
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not open the folder:\n{exc}")

    def show_help(self) -> None:
        window = tk.Toplevel(self.master)
        window.title("Placeholders and rules")
        window.transient(self.master)
        window.geometry("760x520")
        text = tk.Text(window, wrap="word", font=("Consolas", 10), padx=10, pady=10)
        text.pack(fill="both", expand=True)
        text.insert("1.0", PLACEHOLDER_HELP)
        text.configure(state="disabled")
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=6)
        window.focus_set()

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def save_log(self) -> None:
        target = filedialog.asksaveasfilename(
            title="Save the summary log",
            defaultextension=".txt",
            initialfile=f"filesorter-log-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(self.log.get("1.0", "end-1c"))
            self._log("OK", f"Log saved to {target}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the log:\n{exc}")

    def stop_work(self) -> None:
        core.request_cancel()
        self.status_var.set("Stopping\u2026 (finishing the current file)")
        self._log("WARN", "Stop requested - no further files will be touched.")

    # -- config ------------------------------------------------------------
    def _folder_path(self) -> Path | None:
        """Normalise whatever is in the folder box (quotes, %VARS%, ~)."""
        raw = self.folder_var.get().strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1].strip()
        if not raw:
            return None
        try:
            return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        except (OSError, ValueError):
            return None

    def _build_config(self, dry_run: bool | None = None) -> core.SortConfig:
        """Collect the widgets into a SortConfig.

        ``dry_run`` forces the value; ``None`` means "use the checkbox", so the
        Safety switch really does govern every run.
        """
        path = self._folder_path()
        if path is None:
            raise ValueError("Please choose the folder you want to tidy.")
        if not path.exists():
            raise ValueError(f"That folder does not exist:\n{path}")
        if not path.is_dir():
            raise ValueError(f"That is a file, not a folder:\n{path}")
        root = path

        def number(text: str, label: str) -> float:
            text = text.strip()
            if not text:
                return 0.0
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError(f"{label} must be a number.") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be a normal, non-negative number.")
            return value

        min_size = number(self.min_size_var.get(), "Minimum size")
        max_size = number(self.max_size_var.get(), "Maximum size")
        if max_size and min_size > max_size:
            raise ValueError(
                f"The minimum size ({min_size:g} KB) is larger than the maximum "
                f"({max_size:g} KB), so no file could ever match."
            )

        keywords = [k.strip() for k in self.keywords_var.get().replace(";", ",").split(",")]
        extensions = [e.strip() for e in self.ext_filter_var.get().replace(";", ",").split(",")]
        return core.SortConfig(
            root=root,
            group_by=self.group_var.get(),
            keywords=keywords,
            rename_template=self.rename_tpl_var.get().strip(),
            folder_template=self.folder_tpl_var.get().strip(),
            date_format=self.date_fmt_var.get().strip(),
            recurse=self.recurse_var.get(),
            extension_filter=extensions,
            min_size_kb=min_size,
            max_size_kb=max_size,
            include_hidden=self.hidden_var.get(),
            dry_run=self.dry_run_var.get() if dry_run is None else bool(dry_run),
            conflict=self.conflict_var.get(),
            dedupe=self.dedupe_var.get(),
        )

    def _remember(self) -> None:
        save_settings(
            {
                "folder": self.folder_var.get(),
                "group_by": self.group_var.get(),
                "folder_template": self.folder_tpl_var.get(),
                "rename_template": self.rename_tpl_var.get(),
                "date_format": self.date_fmt_var.get(),
                "keywords": self.keywords_var.get(),
                "extensions": self.ext_filter_var.get(),
                "min_size": self.min_size_var.get(),
                "max_size": self.max_size_var.get(),
                "conflict": self.conflict_var.get(),
                "recurse": self.recurse_var.get(),
                "hidden": self.hidden_var.get(),
                "dedupe": self.dedupe_var.get(),
                "dry_run": self.dry_run_var.get(),
            }
        )

    def _load_settings(self) -> None:
        data = load_settings()
        if not data:
            return
        mapping = {
            "folder": self.folder_var,
            "group_by": self.group_var,
            "folder_template": self.folder_tpl_var,
            "rename_template": self.rename_tpl_var,
            "date_format": self.date_fmt_var,
            "keywords": self.keywords_var,
            "extensions": self.ext_filter_var,
            "min_size": self.min_size_var,
            "max_size": self.max_size_var,
            "conflict": self.conflict_var,
        }
        for key, var in mapping.items():
            value = data.get(key)
            if isinstance(value, str) and value:
                var.set(value)
        for key, var in (
            ("recurse", self.recurse_var),
            ("hidden", self.hidden_var),
            ("dedupe", self.dedupe_var),
            ("dry_run", self.dry_run_var),
        ):
            if isinstance(data.get(key), bool):
                var.set(data[key])
        self._log("INFO", "Restored your previous settings.")

    # -- running work ------------------------------------------------------
    def _busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.preview_button, self.sort_button, self.undo_button):
            widget.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def confirm_and_sort(self) -> None:
        try:
            config = self._build_config()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        if config.dry_run:
            proceed = messagebox.askyesno(
                core.APP_NAME,
                "The \u201cdry run only\u201d switch is still ticked, so nothing will be "
                "changed - the plan will only be listed.\n\n"
                "Do you want that simulation instead of a real sort?",
                icon="question",
            )
            if not proceed:
                return
            self._remember()
            self._start_worker(config, mode="preview")
            return
        confirm = messagebox.askyesno(
            core.APP_NAME,
            "Move and rename files in:\n"
            f"{config.root}\n\n"
            "Nothing is ever deleted, collisions can be renamed instead of overwritten, "
            "and the whole run can be undone afterwards.\n\nStart now?",
            icon="question",
        )
        if not confirm:
            return
        self._remember()
        self._start_worker(config, mode="sort")

    def start_sort(self, preview: bool = False) -> None:
        try:
            # The explicit Preview button always simulates; plain Sort honours
            # the Safety checkbox.
            config = self._build_config(dry_run=True if preview else None)
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._remember()
        self._start_worker(config, mode="preview" if (preview or config.dry_run) else "sort")

    def undo_last(self) -> None:
        # Use the same normalisation as a run, so a quoted / %VAR% path still
        # finds the journal this very session wrote.
        path = self._folder_path()
        journal = None
        if path is not None:
            try:
                journal = core.latest_journal(path)
            except OSError:
                journal = None
        if journal is None:
            chosen = filedialog.askopenfilename(
                title="Pick a journal file to undo",
                filetypes=[("Undo journals", "*.jsonl"), ("All files", "*.*")],
            )
            if not chosen:
                return
            journal = Path(chosen)
        if not messagebox.askyesno(
            core.APP_NAME,
            f"Undo every move recorded in\n{journal.name}?\n\n"
            "Files are put back where they came from, last action first. "
            "Nothing is overwritten: if a name is taken, the file is restored with a new suffix.",
        ):
            return
        self._start_worker(journal, mode="undo")

    def _start_worker(self, payload, mode: str) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "A job is already running. Press STOP first.")
            return
        core.reset_cancel()
        self._busy(True)
        self.progress.configure(value=0, maximum=100)
        self.worker = threading.Thread(
            target=self._worker_main, args=(payload, mode), name=f"filesorter-{mode}", daemon=True
        )
        self.worker.start()

    def _worker_main(self, payload, mode: str) -> None:
        """Runs on the worker thread; communicates only through the queue."""
        emit = self.msg_queue.put
        logger = core.Logger(sink=lambda line: emit(("log", line)))
        try:
            if mode == "undo":
                self._run_undo(Path(payload), logger, emit)
                return
            config: core.SortConfig = payload
            self._run_sort(config, logger, emit, preview=(mode == "preview"))
        except core.CancelledError:
            logger.warn("Cancelled before any file was touched.")
            emit(("done", "cancelled", None, None))
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            logger.error(str(exc))
            emit(("done", "error", str(exc), None))
        except BaseException as exc:  # last-resort net: the UI must never hang
            logger.error(f"Unexpected error: {type(exc).__name__}: {exc}")
            for line in traceback.format_exc().splitlines():
                logger.error("  " + line)
            emit(("done", "error", str(exc), None))
        finally:
            logger.close()

    def _run_sort(self, config: core.SortConfig, logger: core.Logger, emit, preview: bool) -> None:
        emit(("status", "Scanning the folder\u2026"))
        emit(("stage", "scan"))
        files = core.scan_files(config, logger, progress=lambda done, total: emit(("prog", done, total)))
        logger.info(f"Found {len(files):,} candidate file(s).")
        emit(("status", "Building the plan\u2026"))
        emit(("stage", "plan"))
        plan = core.plan_actions(config, files, logger, progress=lambda done, total: emit(("prog", done, total)))
        logger.info(f"Plan: {plan.summary()}")

        if preview:
            logger.rule("Preview only - nothing was changed")
            for action in plan.actions[:500]:
                logger.info("[would] " + action.describe())
            if len(plan.actions) > 500:
                logger.info(f"\u2026 and {len(plan.actions) - 500:,} more action(s).")
            emit(("done", "preview", plan, None))
            return

        if not plan.actions:
            emit(("done", "done", plan, core.ExecResult()))
            return

        emit(("status", "Moving files\u2026"))
        emit(("stage", "exec"))
        execution = core.execute_actions(
            plan.actions, config, logger, progress=lambda done, total: emit(("prog", done, total))
        )
        emit(("done", "cancelled" if execution.cancelled else "done", plan, execution))

    def _run_undo(self, journal: Path, logger: core.Logger, emit) -> None:
        emit(("status", "Restoring files\u2026"))
        emit(("stage", "exec"))
        result = core.undo_journal(journal, logger, progress=lambda done, total: emit(("prog", done, total)))
        emit(("done", "undone", None, result))

    # -- queue pump --------------------------------------------------------
    def _pump_queue(self) -> None:
        """Drain worker messages.  Never dies, even if a handler raises."""
        try:
            while True:
                message = self.msg_queue.get_nowait()
                kind = message[0]
                try:
                    if kind == "log":
                        self._emit_line(message[1])
                    elif kind == "status":
                        self.status_var.set(message[1])
                    elif kind == "stage":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(60)
                    elif kind == "prog":
                        done = int(message[1])
                        total = int(message[2] or 0)
                        if total <= 0:
                            # Scanning: the final count is not known yet.
                            continue
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=min(done, total))
                        self.status_var.set(f"{done:,} / {total:,}")
                    elif kind == "done":
                        self._finish(*message[1:])
                except Exception:  # one bad message must not kill the UI
                    traceback.print_exc()
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self._watchdog()
            self.after(80, self._pump_queue)

    def _watchdog(self) -> None:
        """Release the UI if the worker died without sending a 'done' message."""
        if self.worker is not None and not self.worker.is_alive() and self.stop_button["state"] == "normal":
            self._log("ERR", "The background job ended unexpectedly; controls were re-enabled.")
            self._finish("error", None, None)

    def _finish(self, outcome: str, plan, execution) -> None:
        self.worker = None  # the job is over; the watchdog must not fire again
        self._busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if outcome == "preview" and plan is not None:
            self.last_plan = plan
            self.status_var.set("Preview complete - nothing was changed.")
            messagebox.showinfo(
                "Preview complete",
                f"{plan.summary()}\n\nNothing was changed. "
                "Untick \u201cdry run only\u201d and press SORT FILES to apply it.",
            )
        elif outcome in ("done", "cancelled") and execution is not None:
            if execution.journal is not None and (execution.moved or execution.renamed):
                self.recent_journal = execution.journal
            done_something = bool(execution.moved or execution.renamed)
            if outcome == "cancelled":
                self.status_var.set(f"Cancelled: {execution.summary()}")
            else:
                self.status_var.set(f"Done: {execution.summary()}")
            if execution.journal_error and done_something:
                self._log("WARN", f"Undo is unavailable for this run: {execution.journal_error}")
                messagebox.showwarning(
                    "Sort finished without an undo file",
                    f"{execution.summary()}\n\n"
                    f"Warning: {execution.journal_error}, so this run cannot be undone "
                    "automatically. The files themselves are safe - nothing was deleted "
                    "or overwritten.",
                )
            elif outcome == "cancelled":
                messagebox.showinfo(
                    "Sort stopped",
                    f"{execution.summary()}\n\n"
                    + (
                        "Everything moved so far can be undone with \u201cUndo last run\u201d."
                        if done_something
                        else "Nothing had been moved yet."
                    ),
                )
            else:
                messagebox.showinfo("Sort finished", execution.summary())
        elif outcome == "undone" and execution is not None:
            if getattr(execution, "cancelled", False):
                self.status_var.set(f"Undo stopped early: {execution.summary()}")
            else:
                self.status_var.set(f"Undo: {execution.summary()}")
            messagebox.showinfo("Undo finished", execution.summary())
        elif outcome == "cancelled":
            self.status_var.set("Cancelled.")
        else:
            self.status_var.set("Stopped because of an error - see the log.")

    # -- window close ------------------------------------------------------
    def on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_TITLE, "A job is still running. Stop it and quit?"
            ):
                return
            self.status_var.set("Stopping after the current file\u2026")
            self._log("WARN", "Cancelling - waiting for the file being processed.")
            core.request_cancel()
            # A cross-volume move copies before deleting, so give it time to
            # finish instead of tearing the window down around it.
            for _ in range(40):  # up to ~20 seconds, UI stays responsive
                if not self.worker.is_alive():
                    break
                self.root_update()
                time.sleep(0.5)
            if self.worker.is_alive():
                if not messagebox.askyesno(
                    APP_TITLE,
                    "A file operation is still in progress (a large copy, for example).\n\n"
                    "Quitting now may leave a partially copied file next to its original "
                    "- nothing is deleted. Quit anyway?",
                ):
                    return
        self._remember()
        self.master.destroy()

    def root_update(self) -> None:
        """Process pending UI events (used while waiting for the worker)."""
        try:
            self.master.update()
        except tk.TclError:  # pragma: no cover - window already gone
            pass


def _take_settings_argument(argv: list[str]) -> int:
    """Handle ``--settings <file>`` and ``--settings=<file>``.

    Sets the module-level SETTINGS_NAME and removes the option from *argv*.
    Returns 2 when the option has no value, 0 otherwise.
    """
    global SETTINGS_NAME
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--settings":
            if index + 1 >= len(argv):
                print("--settings needs a file name", file=sys.stderr)
                return 2
            SETTINGS_NAME = argv[index + 1]
            del argv[index : index + 2]
            continue
        if argument.startswith("--settings="):
            value = argument.split("=", 1)[1].strip()
            if not value:
                print("--settings needs a file name", file=sys.stderr)
                return 2
            SETTINGS_NAME = value
            del argv[index]
            continue
        index += 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    status = _take_settings_argument(argv)
    if status != 0:
        return status
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(880, 620)
    root.geometry("1000x720")
    try:  # crisper text on high-DPI Windows screens
        if os.name == "nt":
            from ctypes import windll  # type: ignore

            windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")

    app = SorterApp(root)
    if argv and Path(argv[0]).is_dir():
        app.folder_var.set(os.path.normpath(argv[0]))
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
