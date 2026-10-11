"""
filesorter.core
===============

Engine for the Safe File Sorter.

The module is deliberately GUI-free so that it can be unit tested, scripted and
called from the command line.  It provides four stages:

1. :func:`scan_files`  - walk a folder, never following symlinks or leaving it.
2. :func:`plan_actions` - pure function: list of actions, nothing is touched.
3. :func:`execute_actions` - performs the moves, records an undo journal.
4. :func:`undo_journal` - rolls a journal back (last action first).

Safety model
------------
* Every destination is computed **relative to the chosen root**; anything that
  would escape the root (``..``, an absolute path smuggled in through a
  template) is refused.
* Nothing is ever deleted.  Collisions are either skipped or renamed
  (``name (2).txt``); identical duplicates can be routed to a duplicate folder.
* ``dry_run=True`` computes and reports everything but touches no file.
* Cross-volume moves fall back to copy + verify + delete.
* An append-only JSON Lines journal is written *before* the GUI can claim
  success, so any run can be undone afterwards.

Requires Python 3.10+ (uses PEP 604 unions and modern dataclasses).
"""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

APP_NAME = "Safe File Sorter"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Grouping modes
# ---------------------------------------------------------------------------
GROUP_EXTENSION = "extension"
GROUP_CATEGORY = "category"
GROUP_DATE = "date"
GROUP_KEYWORD = "keyword"
GROUP_COMBINED = "combined"
GROUP_FLAT = "flat"

GROUPING_MODES: tuple[str, ...] = (
    GROUP_EXTENSION,
    GROUP_CATEGORY,
    GROUP_DATE,
    GROUP_KEYWORD,
    GROUP_COMBINED,
    GROUP_FLAT,
)

GROUPING_LABELS: dict[str, str] = {
    GROUP_EXTENSION: "By file extension  (.pdf, .jpg, ...)",
    GROUP_CATEGORY: "By file category   (Images, Documents, ...)",
    GROUP_DATE: "By date modified    (2024 / 03-March / ...)",
    GROUP_KEYWORD: "By keyword in the filename",
    GROUP_COMBINED: "Combined            (category / keyword / date)",
    GROUP_FLAT: "No subfolders       (only rename / de-duplicate)",
}

CONFLICT_RENAME = "rename"
CONFLICT_SKIP = "skip"
CONFLICT_MODES: tuple[str, ...] = (CONFLICT_RENAME, CONFLICT_SKIP)

UNSORTED_LABEL = "_Unsorted"
DUPLICATES_LABEL = "_Duplicates"
NO_EXTENSION_LABEL = "_NoExtension"

#: Best-effort extension -> category table.  Unknown extensions land in "Other".
CATEGORY_MAP: dict[str, str] = {
    # images
    "jpg": "Images", "jpeg": "Images", "png": "Images", "gif": "Images",
    "bmp": "Images", "tif": "Images", "tiff": "Images", "webp": "Images",
    "svg": "Images", "ico": "Images", "heic": "Images", "raw": "Images",
    "psd": "Images", "ai": "Images",
    # documents
    "pdf": "Documents", "doc": "Documents", "docx": "Documents",
    "odt": "Documents", "rtf": "Documents", "txt": "Documents",
    "md": "Documents", "tex": "Documents", "pages": "Documents",
    "epub": "Documents", "mobi": "Documents",
    # spreadsheets & slides
    "xls": "Spreadsheets", "xlsx": "Spreadsheets", "xlsm": "Spreadsheets",
    "ods": "Spreadsheets", "csv": "Spreadsheets", "tsv": "Spreadsheets",
    "ppt": "Presentations", "pptx": "Presentations", "odp": "Presentations",
    # video
    "mp4": "Video", "mkv": "Video", "avi": "Video", "mov": "Video",
    "wmv": "Video", "flv": "Video", "webm": "Video", "m4v": "Video",
    "mpg": "Video", "mpeg": "Video",
    # audio
    "mp3": "Audio", "wav": "Audio", "flac": "Audio", "aac": "Audio",
    "ogg": "Audio", "oga": "Audio", "m4a": "Audio", "wma": "Audio",
    "opus": "Audio", "aiff": "Audio",
    # archives
    "zip": "Archives", "rar": "Archives", "7z": "Archives", "tar": "Archives",
    "gz": "Archives", "tgz": "Archives", "bz2": "Archives", "xz": "Archives",
    "iso": "Archives", "cab": "Archives",
    # code & data
    "py": "Code", "pyw": "Code", "js": "Code", "mjs": "Code", "cjs": "Code",
    "ts": "Code", "tsx": "Code", "jsx": "Code", "java": "Code", "c": "Code",
    "h": "Code", "cpp": "Code", "hpp": "Code", "cs": "Code", "go": "Code",
    "rs": "Code", "rb": "Code", "php": "Code", "sh": "Code", "ps1": "Code",
    "bat": "Code", "cmd": "Code", "sql": "Code", "html": "Code",
    "htm": "Code", "css": "Code", "scss": "Code", "json": "Code",
    "xml": "Code", "yml": "Code", "yaml": "Code", "toml": "Code",
    "ini": "Code", "cfg": "Code", "ipynb": "Code",
    # installers / binaries
    "exe": "Installers", "msi": "Installers", "dmg": "Installers",
    "pkg": "Installers", "deb": "Installers", "rpm": "Installers",
    "apk": "Installers", "appx": "Installers", "jar": "Installers",
    # fonts / misc binary
    "ttf": "Fonts", "otf": "Fonts", "woff": "Fonts", "woff2": "Fonts",
}

#: Placeholders accepted in folder / rename templates.
TEMPLATE_TOKENS: tuple[str, ...] = (
    "ext", "EXT", "category", "keyword", "date", "year", "month", "monthname",
    "day", "weekday", "hour", "minute", "second", "name", "stem",
)

_INVALID_CHARS = '<>:"/\\|?*'
#: FILE_ATTRIBUTE_REPARSE_POINT - junctions and symlinks share it on Windows.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}
_WINDOWS = os.name == "nt"

#: Kill switch honoured by long running loops (set from the GUI's Stop button).
CANCEL_FLAG = False


class CancelledError(RuntimeError):
    """Raised internally when the user aborts a running sort."""


# ---------------------------------------------------------------------------
# Public API: configuration / result objects
# ---------------------------------------------------------------------------
@dataclass
class SortConfig:
    """Everything the engine needs to know about one sort run."""

    root: Path
    group_by: str = GROUP_EXTENSION
    keywords: Sequence[str] = ()
    rename_template: str = ""
    folder_template: str = ""
    date_format: str = ""
    recurse: bool = False
    extension_filter: Sequence[str] = ()
    min_size_kb: float = 0.0
    max_size_kb: float = 0.0  # 0 == no upper bound
    include_hidden: bool = False
    dry_run: bool = False
    conflict: str = CONFLICT_RENAME
    dedupe: bool = False
    unique_names: bool = True
    log_dir_name: str = ".filesorter"
    max_files: int = 200_000

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.group_by not in GROUPING_MODES:
            raise ValueError(f"Unknown grouping mode: {self.group_by!r}")
        if self.conflict not in CONFLICT_MODES:
            raise ValueError(f"Unknown conflict mode: {self.conflict!r}")
        self.keywords = [k.strip() for k in self.keywords if str(k).strip()]
        self.extension_filter = [normalise_ext(e) for e in self.extension_filter]
        self.extension_filter = [e for e in self.extension_filter if e]

    @property
    def journal_dir(self) -> Path:
        return self.root / self.log_dir_name

    @property
    def effective_date_format(self) -> str:
        if self.date_format.strip():
            return self.date_format.strip()
        if self.group_by in (GROUP_DATE, GROUP_COMBINED):
            return "%Y/%m-%B"
        return "%Y/%m"

    def as_dict(self) -> dict[str, Any]:
        """JSON friendly snapshot for the journal index."""
        return {
            "root": str(self.root),
            "group_by": self.group_by,
            "keywords": list(self.keywords),
            "rename_template": self.rename_template,
            "folder_template": self.folder_template,
            "date_format": self.effective_date_format,
            "recurse": self.recurse,
            "extension_filter": list(self.extension_filter),
            "min_size_kb": self.min_size_kb,
            "max_size_kb": self.max_size_kb,
            "include_hidden": self.include_hidden,
            "dry_run": self.dry_run,
            "conflict": self.conflict,
            "dedupe": self.dedupe,
        }


@dataclass
class Action:
    """A single planned or executed file operation."""

    src: Path
    dst: Path
    group: str = ""
    reason: str = ""
    duplicate: bool = False

    @property
    def is_move(self) -> bool:
        return parent_key(self.src) != parent_key(self.dst)

    @property
    def is_rename(self) -> bool:
        return parent_key(self.src) == parent_key(self.dst) and self.src.name != self.dst.name

    def describe(self) -> str:
        kind = "move+rename" if (self.is_move and self.src.name != self.dst.name) else (
            "rename" if not self.is_move else "move"
        )
        return f"[{kind}] {self.src.name}  ->  {rel_display(self.src, self.dst)}"


@dataclass
class PlanResult:
    """Outcome of planning: what would happen and what was deliberately left out."""

    actions: list[Action] = field(default_factory=list)
    skipped_unchanged: int = 0
    skipped_filtered: int = 0
    skipped_conflict: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    scanned: int = 0
    duration_s: float = 0.0

    @property
    def moves(self) -> int:
        return sum(1 for a in self.actions if a.is_move)

    @property
    def renames(self) -> int:
        return sum(1 for a in self.actions if not a.is_move and a.src.name != a.dst.name)

    def summary(self) -> str:
        return (
            f"{len(self.actions)} action(s): {self.moves} move(s), {self.renames} rename(s) | "
            f"scanned {self.scanned} file(s), {self.skipped_unchanged} already in place, "
            f"{self.skipped_filtered} filtered out, {self.skipped_conflict} conflict(s) skipped, "
            f"{self.duplicates} duplicate(s)"
        )


@dataclass
class ExecResult:
    """Outcome of executing a plan."""

    moved: int = 0
    renamed: int = 0
    skipped: int = 0
    failed: int = 0
    journal: Path | None = None
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    duration_s: float = 0.0
    #: True when the numbers describe what a dry run *would* have done.
    simulated: bool = False
    #: Set when files were moved but the undo journal could not be written.
    journal_error: str = ""

    def summary(self) -> str:
        parts = [f"{self.moved} moved", f"{self.renamed} renamed"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.cancelled:
            parts.append("CANCELLED by user")
        text = ", ".join(parts)
        if self.simulated:
            text = "dry run - " + text
        if self.journal is not None:
            text += f" | undo file: {self.journal.name}"
        elif self.journal_error:
            text += " | WARNING: no undo file"
        return text


@dataclass
class UndoResult:
    """Outcome of rolling a journal back."""

    restored: int = 0
    failed: int = 0
    journal: Path | None = None
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    def summary(self) -> str:
        text = f"{self.restored} item(s) restored"
        if self.failed:
            text += f", {self.failed} failed"
        if self.cancelled:
            text += " (stopped early on request)"
        return text


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
class Logger:
    """Collects human readable lines and mirrors them to ``.filesorter/logs``."""

    def __init__(
        self,
        sink: Callable[[str], None] | None = None,
        log_file: Path | None = None,
    ) -> None:
        self.sink = sink
        self.log_file = log_file
        self.lines: list[str] = []
        self._handle = None
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(log_file, "w", encoding="utf-8", newline="\n")
            except OSError as exc:  # pragma: no cover - depends on the filesystem
                self._handle = None
                self.error(f"Could not open log file {log_file}: {exc}")

    # -- levels ------------------------------------------------------------
    def _write(self, level: str, message: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        # Format: "HH:MM:SS LEVEL message" - the level is always the second
        # token so that the CLI can filter on it and the GUI can colour it.
        line = f"{stamp} {level} {message}" if level != "----" else message
        self.lines.append(line)
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except OSError:
                pass
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:  # never let the UI break the engine
                pass

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERR", message)

    def ok(self, message: str) -> None:
        self._write("OK", message)

    def rule(self, title: str = "") -> None:
        bar = "-" * 68
        self._write("----", f"{bar}\n{title}\n{bar}" if title else bar)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def parent_key(path: Path) -> str:
    """Case-insensitive parent folder key (Windows-safe comparisons)."""
    text = str(path.parent)
    return text.lower() if _WINDOWS else text


def same_path(a: Path, b: Path) -> bool:
    return parent_key(a) == parent_key(b) and a.name.lower() == b.name.lower()


def _key(path: Path) -> str:
    """Comparison / set key for a path (case-insensitive on Windows)."""
    text = str(path)
    return text.lower() if _WINDOWS else text


def normalise_ext(ext: str) -> str:
    """``".PDF" -> "pdf"``, ``"*"`` stays ``"*"`` for "everything"."""
    ext = (ext or "").strip().lower()
    if ext in ("*", "all"):
        return "*"
    return ext.lstrip(".")


def extension_of(path: Path) -> str:
    """Lower-case extension without the dot; ``.tar.gz`` becomes ``gz``'s parent."""
    return path.suffix.lower().lstrip(".")


def category_of(path: Path) -> str:
    ext = extension_of(path)
    if not ext:
        return "Other"
    return CATEGORY_MAP.get(ext, "Other")


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0 or unit == "TB":
            return f"{num_bytes:,.0f} {unit}" if unit == "B" else f"{num_bytes:,.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def safe_component(name: str, fallback: str = "unnamed") -> str:
    """Make one *folder* name safe on Windows *and* POSIX."""
    cleaned = _sanitise_characters(name)
    if not cleaned:
        cleaned = _sanitise_characters(fallback)
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:150]


def safe_file_name(name: str, fallback: str = "unnamed") -> str:
    """Like :func:`safe_component` but keeps a file's leading dot and suffix."""
    prefix = "." if str(name).startswith(".") else ""
    cleaned = _sanitise_characters(name) or _sanitise_characters(fallback) or "unnamed"
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    elif not stem:
        stem, suffix = cleaned, ""
    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem
    return (prefix + stem + (dot + suffix if dot and suffix else ""))[:255]


def _sanitise_characters(name: str) -> str:
    cleaned = "".join("_" if (ch in _INVALID_CHARS or ord(ch) < 32) else ch for ch in str(name))
    cleaned = cleaned.strip().strip(".")
    return re.sub(r"\s+", " ", cleaned)


def safe_parts(text: str, fallback: str = "unsorted") -> list[str]:
    """Split a template result into safe, non-empty path components.

    A segment that renders to nothing (a stray ``//``) is dropped; a segment
    that renders to the fallback - an unmatched ``{keyword}``, or a literal
    ``_Unsorted`` in the template - is kept, so the folder structure stays
    predictable and visible.
    """
    raw = str(text).replace("\\", "/")
    parts = [safe_component(p, fallback) for p in raw.split("/")]
    return [p for p in parts if p and p not in (".", "..")]


def rel_display(src: Path, dst: Path) -> str:
    try:
        return str(dst.relative_to(_common_root(src, dst)))
    except (ValueError, OSError):
        return str(dst)


def _common_root(a: Path, b: Path) -> Path:
    import os.path as _op

    try:
        return Path(_op.commonpath([str(a), str(b)]))
    except ValueError:
        return Path(a.anchor or ".")


def inside_root(root: Path, target: Path) -> bool:
    """True when *target* physically lives inside *root*.

    Both paths are resolved through reparse points (Windows junctions, symlinks)
    before comparing, and the comparison is repeated against the canonical root:
    otherwise a junction *inside* the root that points somewhere else would make
    a destination look contained when it is not.
    """
    import os.path as _op

    def canonical(path: Path) -> str:
        try:
            return _op.normcase(_op.abspath(str(path)))
        except (OSError, ValueError):
            return str(path)

    root_c = canonical(root)
    target_c = canonical(target)
    candidates = [(root_c, target_c)]
    try:
        root_real = _op.normcase(_op.realpath(str(root)))
        target_real = _op.normcase(_op.realpath(str(target)))
        if (root_real, target_real) != (root_c, target_c):
            candidates.append((root_real, target_real))
    except (OSError, ValueError):
        pass

    # Contained only when *every* view agrees; if the lexical path looks fine
    # but the resolved path escapes through a junction, it is not contained.
    for root_candidate, target_candidate in candidates:
        try:
            contained = _op.commonpath([root_candidate, target_candidate]) == root_candidate
        except ValueError:
            contained = False
        if not contained:
            return False
    return True


def _is_reparse_point(path: Path) -> bool:
    """Symlinks *and* NTFS junctions/mount points (Windows reparse points)."""
    if _WINDOWS:
        try:
            attrs = path.lstat().st_file_attributes  # type: ignore[attr-defined]
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except (AttributeError, OSError):
            pass
    try:
        return path.is_symlink()
    except OSError:
        return False


def ensure_unique(target: Path, taken: set[str] | None = None) -> Path:
    """``report.pdf`` -> ``report (2).pdf`` when the name is already used.

    ``taken`` holds comparison keys produced by :func:`_key` (full paths,
    case-folded on Windows), so callers can reserve names that are not on disk
    yet - planned destinations, for instance.
    """
    taken = taken if taken is not None else set()

    def seen(path: Path) -> bool:
        return _key(path) in taken or path.exists()

    if not seen(target):
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(2, 100_000):
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if not seen(candidate):
            return candidate
    raise OSError(f"Could not find a free name for {target.name}")


def format_with_date(template: str, moment: _dt.datetime, fallback: str = "Undated") -> str:
    """Apply ``strftime`` to every ``/``-separated segment of a template."""
    segments = [seg for seg in str(template).split("/")]
    out: list[str] = []
    for seg in segments:
        try:
            rendered = moment.strftime(seg)
        except (ValueError, TypeError):
            rendered = seg
        out.append(rendered)
    return "/".join(out)


#: ``{token}`` occurrences in rename / folder templates.
_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_tokens(
    template: str,
    src: Path,
    moment: _dt.datetime,
    date_fmt: str,
    keyword: str,
    category: str,
) -> str:
    """Substitute every ``{token}`` in one single pass.

    A single pass matters: values taken from the file name may themselves look
    like placeholders (a file literally called ``a{ext}b.txt``), and a
    sequential ``str.replace`` per token would substitute inside those values
    and silently produce the wrong name.  Unknown placeholders are removed.
    """
    ext = extension_of(src)
    literal_suffix = src.suffix  # keeps the original case: ".JPG" stays ".JPG"
    mapping = {
        "ext": literal_suffix,
        "EXT": literal_suffix.upper(),
        "category": category,
        "keyword": keyword,
        "date": format_with_date(date_fmt, moment, "Undated"),
        "year": moment.strftime("%Y"),
        "month": moment.strftime("%m"),
        "monthname": moment.strftime("%B"),
        "day": moment.strftime("%d"),
        "weekday": moment.strftime("%A"),
        "hour": moment.strftime("%H"),
        "minute": moment.strftime("%M"),
        "second": moment.strftime("%S"),
        "name": src.name,
        "stem": src.stem,
    }
    return _TOKEN_RE.sub(lambda match: str(mapping.get(match.group(1), "")), str(template))


def match_keyword(stem: str, keywords: Sequence[str]) -> str:
    """First configured keyword found in the filename (case-insensitive)."""
    haystack = stem.lower()
    for word in keywords:
        if word.lower() in haystack:
            return word
    return ""


# ---------------------------------------------------------------------------
# Stage 1: scanning
# ---------------------------------------------------------------------------
def _cancelled() -> bool:
    return bool(CANCEL_FLAG)


def reset_cancel() -> None:
    global CANCEL_FLAG
    CANCEL_FLAG = False


def request_cancel() -> None:
    global CANCEL_FLAG
    CANCEL_FLAG = True


def scan_files(
    config: SortConfig,
    logger: Logger | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """Return the files eligible for sorting.

    Symlinks *and* Windows reparse points (junctions, mount points) are never
    entered or collected: a junction inside the chosen folder can point to a
    completely different tree, and moving files out of it would reach outside
    the folder the user selected.  The tool's own ``.filesorter`` folder is
    skipped at every depth.
    """
    root = config.root
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")

    log = logger or Logger()
    found: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        if _cancelled():
            raise CancelledError("scan cancelled")
        current = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d != config.log_dir_name and not _is_reparse_point(current / d)
        ]
        if not config.recurse and current != root:
            dirnames[:] = []
            continue
        for name in filenames:
            if _cancelled():
                raise CancelledError("scan cancelled")
            path = current / name
            if _is_reparse_point(path):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if len(found) >= config.max_files:
                truncated = True
                break
            found.append(path)
            if len(found) % 500 == 0 and progress is not None:
                progress(len(found), 0)
        if truncated:
            break

    found.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    if truncated:
        log.warn(f"Stopped scanning at max_files={config.max_files:,}.")
    if progress is not None:
        progress(len(found), len(found))
    return found


# ---------------------------------------------------------------------------
# Stage 2: planning
# ---------------------------------------------------------------------------
def _passes_filter(path: Path, config: SortConfig, stat: os.stat_result) -> bool:
    if not config.include_hidden:
        if path.name.startswith("."):
            return False
        if _WINDOWS:
            try:
                attrs = getattr(stat, "st_file_attributes", 0)
                if attrs & 0x2:  # FILE_ATTRIBUTE_HIDDEN
                    return False
                if attrs & 0x4:  # FILE_ATTRIBUTE_SYSTEM
                    return False
            except (AttributeError, OSError):
                pass
    wanted = config.extension_filter
    if wanted and "*" not in wanted:
        ext = extension_of(path)
        if ext not in wanted:
            return False
    size_kb = stat.st_size / 1024.0
    if config.min_size_kb and size_kb < config.min_size_kb:
        return False
    if config.max_size_kb and size_kb > config.max_size_kb:
        return False
    return True


def _folder_parts(
    config: SortConfig,
    src: Path,
    moment: _dt.datetime,
    keyword: str,
    category: str,
) -> list[str]:
    date_fmt = config.effective_date_format
    mode = config.group_by

    if config.folder_template.strip():
        rendered = _resolve_tokens(
            config.folder_template, src, moment, date_fmt, keyword or UNSORTED_LABEL, category
        )
        return safe_parts(rendered, UNSORTED_LABEL) or [safe_component(UNSORTED_LABEL)]

    if mode == GROUP_FLAT:
        return []
    if mode == GROUP_EXTENSION:
        ext = extension_of(src)
        return [safe_component(ext.upper() if ext else NO_EXTENSION_LABEL)]
    if mode == GROUP_CATEGORY:
        return [safe_component(category)]
    if mode == GROUP_DATE:
        return safe_parts(format_with_date(date_fmt, moment), "Undated") or ["Undated"]
    if mode == GROUP_KEYWORD:
        if not config.keywords:
            return safe_parts(format_with_date(date_fmt, moment), "Undated") or ["Undated"]
        return [safe_component(keyword or UNSORTED_LABEL)]
    if mode == GROUP_COMBINED:
        parts = [safe_component(category)]
        if config.keywords:
            parts.append(safe_component(keyword or UNSORTED_LABEL))
        parts.extend(safe_parts(format_with_date(date_fmt, moment), "Undated") or ["Undated"])
        return parts
    return [safe_component(category)]


def _filename_for(
    config: SortConfig,
    src: Path,
    moment: _dt.datetime,
    keyword: str,
    category: str,
) -> str:
    """Final file name (with extension) for *src*."""
    ext = extension_of(src)
    template = config.rename_template.strip()
    if not template:
        return safe_file_name(src.name, src.name)

    rendered = _resolve_tokens(
        template, src, moment, config.effective_date_format, keyword, category
    )
    base = safe_file_name(rendered.replace("\\", "/").split("/")[-1], src.stem)
    if not base or base == ".":
        return safe_file_name(src.name, src.name)

    # Append the original extension unless the template already produced one.
    # This covers ``{ext}``/``{EXT}`` placeholders *and* a literal extension in
    # the template (``photo_{year}.jpg``, ``{stem}.txt``), so it is never
    # doubled; a template with no extension at all (``{stem}``) still gets one.
    tail = base.rsplit(".", 1)[-1] if "." in base else ""
    if tail and len(tail) <= 8 and re.fullmatch(r"\w+", tail) and not base.startswith("."):
        return base
    if ext and tail.lower() == ext:
        return base
    return safe_file_name(base + src.suffix, src.name)


def plan_actions(
    config: SortConfig,
    files: Iterable[Path] | None = None,
    logger: Logger | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> PlanResult:
    """Compute the full list of actions without touching a single file."""
    started = _dt.datetime.now()
    log = logger or Logger()
    result = PlanResult()

    if files is None:
        files = scan_files(config, log, progress)
    files = list(files)
    if not files:
        result.duration_s = (_dt.datetime.now() - started).total_seconds()
        return result

    root = config.root
    reserved: set[str] = set()
    hash_cache: dict[str, str] = {}
    content_index: dict[tuple[int, str], Path] = {}

    total = len(files)
    for index, src in enumerate(files, start=1):
        if _cancelled():
            raise CancelledError("planning cancelled")
        if progress is not None and (index % 50 == 0 or index == total):
            progress(index, total)
        result.scanned += 1
        try:
            stat = src.stat()
            if not _passes_filter(src, config, stat):
                result.skipped_filtered += 1
                continue

            moment = _dt.datetime.fromtimestamp(stat.st_mtime)
            category = category_of(src)
            keyword = match_keyword(src.stem, config.keywords)
            parts = _folder_parts(config, src, moment, keyword, category)
            name = _filename_for(config, src, moment, keyword, category)

            target = root.joinpath(*parts, name) if parts else root / name

            if not inside_root(root, target):
                result.errors.append(f"{src.name}: target escapes the chosen folder - skipped")
                continue
            if len(str(target)) > 250 and _WINDOWS:
                result.errors.append(f"{src.name}: resulting path is too long - skipped")
                continue

            key = _key(target)
            duplicate = False
            location_clash = False

            if target.exists() and not same_path(src, target):
                if _is_duplicate(src, target, stat, hash_cache):
                    duplicate = True
                    result.duplicates += 1
                    target = root / config.log_dir_name / DUPLICATES_LABEL / name
                    key = _key(target)
                else:
                    location_clash = True

            if same_path(src, target) or (
                parent_key(src) == parent_key(target) and src.name == target.name
            ):
                result.skipped_unchanged += 1
                continue

            if key in reserved or location_clash:
                if config.conflict == CONFLICT_SKIP:
                    result.skipped_conflict += 1
                    continue
                target = ensure_unique(target, reserved)
                key = _key(target)
                if not inside_root(root, target):
                    result.errors.append(f"{src.name}: no safe free name found - skipped")
                    continue

            if config.dedupe and not duplicate:
                clash = _index_duplicate(src, stat, content_index, hash_cache)
                if clash is not None:
                    result.duplicates += 1
                    duplicate = True
                    target = root / config.log_dir_name / DUPLICATES_LABEL / name
                    key = _key(target)
                    # Another duplicate may already own this name - it has to
                    # stay unique even when the file itself does not exist yet.
                    if key in reserved or target.exists():
                        target = ensure_unique(target, reserved)
                        key = _key(target)
            elif config.dedupe:
                _index_duplicate(src, stat, content_index, hash_cache)
        except CancelledError:
            raise
        except (OSError, ValueError) as exc:
            # A file can vanish, be locked or lose its permissions between any
            # two calls above; report it and carry on with the rest.
            result.errors.append(f"{src.name}: skipped ({exc})")
            continue

        reserved.add(key)

        result.actions.append(
            Action(src=src, dst=target, group="/".join(parts), reason="dry-run", duplicate=duplicate)
        )

    _resolve_late_conflicts(result, config, root)
    result.duration_s = (_dt.datetime.now() - started).total_seconds()
    return result


def _resolve_late_conflicts(result: PlanResult, config: SortConfig, root: Path) -> None:
    """Files that stay put are only discovered at the end of the first pass.

    A file that is already exactly where it belongs is never selected for
    moving, so it could not reserve its own name.  Without this second pass an
    incoming file would silently overwrite it (or be renamed for no reason).
    """
    moving: set[str] = set()
    occupied: set[str] = set()
    for source in result.actions:
        moving.add(_key(source.src))

    for action in result.actions:
        target = action.dst
        # Only a file that will *stay* in that spot can block the destination.
        if (
            _key(target) not in moving
            and target.exists()
            and not same_path(action.src, target)
        ):
            occupied.add(_key(target))

    if not occupied:
        return

    for action in result.actions:
        key = _key(action.dst)
        if key not in occupied:
            continue
        if config.conflict == CONFLICT_SKIP:
            result.skipped_conflict += 1
            action.reason = "skipped: name already used"
            action.dst = action.src  # marker: filtered out below
            continue
        fresh = ensure_unique(action.dst, occupied)
        if not inside_root(root, fresh):
            action.reason = "skipped: no safe name"
            action.dst = action.src
            continue
        action.reason = f"renamed to avoid {action.dst.name}"
        action.dst = fresh
        occupied.add(_key(fresh))

    result.actions = [a for a in result.actions if not same_path(a.src, a.dst)]


def _hash_file(path: Path, cache: dict[str, str], chunk: int = 1024 * 256) -> str | None:
    """Blake2b of a file, cached by path (returns None when unreadable)."""
    key = _key(path)
    if key in cache:
        return cache[key]
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    cache[key] = digest.hexdigest()
    return cache[key]


def _is_duplicate(src: Path, target: Path, stat: os.stat_result, cache: dict[str, str]) -> bool:
    try:
        if target.stat().st_size != stat.st_size:
            return False
    except OSError:
        return False
    left = _hash_file(src, cache)
    right = _hash_file(target, cache)
    return left is not None and left == right


def _index_duplicate(
    src: Path,
    stat: os.stat_result,
    seen: dict[tuple[int, str], Path],
    cache: dict[str, str],
) -> Path | None:
    """O(1) duplicate lookup: same byte size plus the same content hash.

    ``seen`` maps ``(size, blake2b)`` to the first file that had that content,
    so a folder full of identically sized files stays linear instead of being
    compared pairwise.
    """
    signature = _hash_file(src, cache)
    if signature is None:
        return None
    key = (stat.st_size, signature)
    existing = seen.get(key)
    if existing is not None:
        return existing
    seen[key] = src
    return None


# ---------------------------------------------------------------------------
# Stage 3: execution
# ---------------------------------------------------------------------------
@dataclass
class _JournalRecord:
    ts: str
    action: str
    src: str
    dst: str
    result: str
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "action": self.action,
                "src": self.src,
                "dst": self.dst,
                "result": self.result,
                "note": self.note,
            },
            ensure_ascii=False,
        )


class JournalWriter:
    """Append-only JSON Lines journal; flushed after every record."""

    def __init__(self, directory: Path, config: SortConfig, logger: Logger) -> None:
        self.logger = logger
        self.path: Path | None = None
        self.records = 0
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"journal-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._handle = None
            self.path = None
            logger.error(f"Could not create the undo journal in {directory}: {exc}")
            return
        meta = {
            "kind": "meta",
            "app": APP_NAME,
            "version": APP_VERSION,
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "root": str(config.root),
            "config": config.as_dict(),
        }
        self._handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
        self._handle.flush()

    def record(self, action: Action, outcome: str, note: str = "") -> None:
        if self._handle is None:
            return
        rec = _JournalRecord(
            ts=_dt.datetime.now().isoformat(timespec="seconds"),
            action="move",
            src=str(action.src),
            dst=str(action.dst),
            result=outcome,
            note=note,
        )
        try:
            self._handle.write(rec.to_json() + "\n")
            self._handle.flush()
            self.records += 1
        except OSError as exc:  # pragma: no cover
            self.logger.error(f"Could not write to the journal: {exc}")

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def _move_file(src: Path, dst: Path) -> str:
    """Move a file, returning a note describing any fallback that was needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return ""
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
            raise
    # Cross-device (or restricted) fallback: copy, verify size, then delete.
    shutil.copy2(src, dst)
    if dst.stat().st_size != src.stat().st_size:
        try:
            dst.unlink()
        except OSError:
            pass
        raise OSError(f"copy verification failed for {src.name}")
    try:
        src.unlink()
    except OSError as exc:
        raise OSError(f"copied to {dst.name} but could not remove the original: {exc}") from exc
    return "copied across volumes"


def _prepare_target(
    action: Action,
    config: SortConfig,
    logger: Logger,
    planned: set[str] | None = None,
) -> Path | None:
    """Re-validate a destination at execution time; None means 'skip this one'.

    ``planned`` carries every destination the plan intends to use (plus the
    ones already claimed during this run), so a fallback rename can never steal
    a name that another queued action is about to move into.
    """
    dst = action.dst
    if dst.exists() and not same_path(action.src, dst):
        if _is_duplicate(action.src, dst, action.src.stat(), {}):
            logger.warn(f"{action.src.name}: identical copy already at destination - skipped")
            return None
        if config.conflict == CONFLICT_SKIP:
            logger.warn(f"{action.src.name}: destination exists - skipped")
            return None
        fresh = ensure_unique(dst, planned)
        logger.warn(f"{action.src.name}: destination appeared meanwhile, using {fresh.name}")
        dst = fresh
    if not inside_root(config.root, dst):
        logger.error(f"{action.src.name}: unsafe destination refused")
        return None
    return dst


def execute_actions(
    actions: Sequence[Action],
    config: SortConfig,
    logger: Logger | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ExecResult:
    """Carry out the plan.  With ``config.dry_run`` nothing is modified."""
    started = _dt.datetime.now()
    log = logger or Logger()
    result = ExecResult()

    if not actions:
        log.info("Nothing to move - no journal was written.")
        result.duration_s = (_dt.datetime.now() - started).total_seconds()
        return result

    if config.dry_run:
        result.simulated = True
        for index, action in enumerate(actions, start=1):
            if progress is not None:
                progress(index, len(actions))
            if action.is_move:
                result.moved += 1
            else:
                result.renamed += 1
            log.info(f"[dry-run] {action.describe()}")
        result.duration_s = (_dt.datetime.now() - started).total_seconds()
        log.ok("Dry run complete - no file was touched.")
        return result

    writer = JournalWriter(config.journal_dir / "logs", config, log)
    result.journal = writer.path
    if writer.path is None:
        result.journal_error = "the undo journal could not be created"
        log.warn("Undo will NOT be possible for this run - see the error above.")

    # Every destination this plan intends to use, plus the ones already taken
    # during this run.  A fallback rename must never steal one of these names.
    claimed: set[str] = set()
    reserved = {_key(action.dst) for action in actions} | claimed

    total = len(actions)
    try:
        for index, action in enumerate(actions, start=1):
            if _cancelled():
                result.cancelled = True
                log.warn("Cancelled by the user - stopping before the next file.")
                break
            if progress is not None:
                progress(index, total)
            src = action.src
            if not src.exists():
                result.skipped += 1
                log.warn(f"{src.name}: vanished before it could be moved - skipped")
                writer.record(action, "skipped", "source missing")
                continue
            try:
                dst = _prepare_target(action, config, log, reserved | claimed)
            except OSError as exc:
                result.failed += 1
                result.errors.append(f"{src.name}: {exc}")
                log.error(f"{src.name}: cannot verify the destination ({exc})")
                writer.record(action, "failed", str(exc))
                continue
            if dst is None:
                result.skipped += 1
                writer.record(Action(src=src, dst=action.dst), "skipped", "conflict")
                continue
            try:
                note = _move_file(src, dst)
            except OSError as exc:
                result.failed += 1
                result.errors.append(f"{src.name}: {exc}")
                log.error(f"{src.name}: move failed ({exc})")
                writer.record(Action(src=src, dst=dst), "failed", str(exc))
                continue
            claimed.add(_key(dst))
            if action.is_move:
                result.moved += 1
            else:
                result.renamed += 1
            writer.record(Action(src=src, dst=dst), "ok", note)
            log.info(f"{Action(src=src, dst=dst).describe()}" + (f"  ({note})" if note else ""))
    except CancelledError:
        result.cancelled = True
        log.warn("Cancelled by the user.")
    finally:
        writer.close()

    result.duration_s = (_dt.datetime.now() - started).total_seconds()
    log.rule()
    log.ok(f"Finished: {result.summary()}")
    if writer.path is not None and (result.moved or result.renamed):
        log.info(f"Undo journal: {writer.path}")
    return result


# ---------------------------------------------------------------------------
# Stage 4: undo
# ---------------------------------------------------------------------------
@dataclass
class JournalEntry:
    ts: str
    action: str
    src: str
    dst: str
    result: str
    note: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result == "ok"


def list_journals(root: Path, log_dir_name: str = ".filesorter") -> list[Path]:
    """All journals for *root*, newest first."""
    directory = Path(root) / log_dir_name / "logs"
    if not directory.is_dir():
        return []

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    items = [p for p in directory.glob("journal-*.jsonl") if p.is_file()]
    return sorted(items, key=modified, reverse=True)


def latest_journal(root: Path, log_dir_name: str = ".filesorter") -> Path | None:
    items = list_journals(root, log_dir_name)
    return items[0] if items else None


def read_journal(path: Path) -> tuple[dict[str, Any], list[JournalEntry]]:
    """Parse a journal; tolerates a truncated or corrupt line anywhere.

    The file is decoded line by line so that a torn multi-byte UTF-8 sequence
    (a crash or a full disk in the middle of a write) degrades to "skip that
    line" instead of raising UnicodeDecodeError.
    """
    meta: dict[str, Any] = {}
    entries: list[JournalEntry] = []
    with open(path, "rb") as handle:
        for raw in handle:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue  # torn write - cannot be a complete record
            text = text.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue  # torn write - ignore the broken line
            if not isinstance(data, dict):
                continue
            if data.get("kind") == "meta":
                meta = data
                continue
            entries.append(
                JournalEntry(
                    ts=str(data.get("ts", "")),
                    action=str(data.get("action", "move")),
                    src=str(data.get("src", "")),
                    dst=str(data.get("dst", "")),
                    result=str(data.get("result", "")),
                    note=str(data.get("note", "")),
                )
            )
    return meta, entries


def undo_journal(
    journal: Path,
    logger: Logger | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> UndoResult:
    """Reverse a journal, last action first.  Never overwrites anything."""
    log = logger or Logger()
    result = UndoResult(journal=Path(journal))
    if not Path(journal).is_file():
        log.error(f"Journal not found: {journal}")
        result.errors.append("journal missing")
        return result

    meta, entries = read_journal(Path(journal))
    root_text = meta.get("root", "")
    log.info(f"Journal: {Path(journal).name}  ({len(entries)} recorded action(s))")
    if root_text:
        log.info(f"Originally applied to: {root_text}")

    done = [e for e in entries if e.succeeded]
    if not done:
        log.info("This journal has no successfully applied action - nothing to restore.")
        return result
    for index, entry in enumerate(reversed(done), start=1):
        if _cancelled():
            remaining = len(done) - index + 1
            log.warn(f"Undo stopped on request - {remaining} item(s) left where they are.")
            result.cancelled = True
            break
        if progress is not None:
            progress(index, len(done))
        src = Path(entry.src)
        dst = Path(entry.dst)
        if not dst.exists():
            result.failed += 1
            result.errors.append(f"{dst.name}: no longer at {dst}")
            log.warn(f"{dst.name}: not found at its sorted location - cannot restore")
            continue
        if src.exists():
            if _is_duplicate(dst, src, src.stat(), {}):
                log.warn(f"{src.name}: the original is already back in place")
                continue
            fresh = ensure_unique(src)
            log.warn(f"{src.name}: an original already exists, restoring as {fresh.name}")
            src = fresh
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            note = _move_file(dst, src)
        except OSError as exc:
            result.failed += 1
            result.errors.append(f"{dst.name}: {exc}")
            log.error(f"{dst.name}: could not restore ({exc})")
            continue
        result.restored += 1
        log.info(f"restored {dst.name} -> {src.parent}" + (f"  ({note})" if note else ""))

    # Mark the journal as consumed so the GUI does not offer it twice - unless
    # the user stopped early, in which case the rest is still pending.
    if result.restored and not result.cancelled:
        try:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            target = Path(journal).with_name(Path(journal).name + f".undone-{stamp}")
            Path(journal).rename(target)
        except OSError:
            pass

    log.rule()
    log.ok(f"Undo finished: {result.summary()}")
    return result


# ---------------------------------------------------------------------------
# Convenience: the whole pipeline in one call
# ---------------------------------------------------------------------------
def run_sort(
    config: SortConfig,
    logger: Logger | None = None,
    progress: Callable[[int, int], None] | None = None,
    stage: Callable[[str], None] | None = None,
) -> tuple[PlanResult, ExecResult]:
    """Scan -> plan -> execute.  Returns the plan and the execution result."""
    log = logger or Logger()
    if stage is not None:
        stage("scanning")
    log.rule(f"{APP_NAME} {APP_VERSION}")
    log.info(f"Folder : {config.root}")
    log.info(f"Mode   : {config.group_by}" + (f"  keywords={list(config.keywords)}" if config.keywords else ""))
    if config.dry_run:
        log.info("DRY RUN - files will be listed but not touched.")
    log.rule()

    if not config.root.exists():
        raise FileNotFoundError(f"Folder not found: {config.root}")
    if not config.root.is_dir():
        raise NotADirectoryError(f"Not a folder: {config.root}")

    if stage is not None:
        stage("planning")
    plan = plan_actions(config, logger=log, progress=progress)
    log.info(f"Planned: {plan.summary()}")
    for problem in plan.errors:
        log.warn(problem)
    if not plan.actions:
        log.info("Nothing to do - every file is already where it belongs.")
        return plan, ExecResult()

    # A dry run must not create anything, not even our bookkeeping folder.
    if not config.dry_run:
        try:
            config.journal_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warn(f"Cannot create {config.journal_dir} ({exc}); undo will not be available.")

    if stage is not None:
        stage("executing")
    execution = execute_actions(plan.actions, config, logger=log, progress=progress)
    return plan, execution
