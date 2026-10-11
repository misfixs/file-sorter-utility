"""
filesorter.cli
==============

Head-less entry point - handy for scripting, testing and scheduled runs.

    python -m filesorter.cli --folder "D:\\Downloads" --group extension --dry-run
    python -m filesorter.cli --folder "D:\\Downloads" --group date
    python -m filesorter.cli --folder "D:\\Downloads" --undo --last
    python -m filesorter.cli --list-journals --folder "D:\\Downloads"

Exit codes: 0 = success, 1 = nothing to do or an error, 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

try:
    from . import core
except ImportError:  # pragma: no cover - direct execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from filesorter import core


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filesorter",
        description=f"{core.APP_NAME} {core.APP_VERSION} - tidy a folder safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing is ever deleted; every run writes an undo journal into <folder>/.filesorter/logs.",
    )
    parser.add_argument("--folder", "-f", required=False, help="folder to tidy")
    parser.add_argument(
        "--group",
        "-g",
        choices=list(core.GROUPING_MODES),
        default=core.GROUP_EXTENSION,
        help="sorting rule (default: extension)",
    )
    parser.add_argument("--keywords", "-k", help="comma separated keywords for keyword mode")
    parser.add_argument("--rename", default="", help="rename template, e.g. '{year}{month}{day}_{stem}{ext}'")
    parser.add_argument("--folder-template", default="", help="target sub-folder template, e.g. '{category}/{year}'")
    parser.add_argument("--date-format", default="", help="strftime pattern, e.g. %%Y/%%m-%%B")
    parser.add_argument("--extensions", "-e", help="only handle these extensions (comma separated, * for all)")
    parser.add_argument("--min-kb", type=float, default=0.0, help="ignore files smaller than this")
    parser.add_argument("--max-kb", type=float, default=0.0, help="ignore files larger than this (0 = no limit)")
    parser.add_argument("--recurse", action="store_true", help="include sub-folders")
    parser.add_argument("--hidden", action="store_true", help="include hidden / system files")
    parser.add_argument("--dedupe", action="store_true", help="move exact duplicates to _Duplicates")
    parser.add_argument(
        "--conflict",
        choices=list(core.CONFLICT_MODES),
        default=core.CONFLICT_RENAME,
        help="what to do when a name already exists (default: rename)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--undo", action="store_true", help="undo a previous run")
    parser.add_argument("--last", action="store_true", help="with --undo: use the newest journal")
    parser.add_argument("--journal", help="with --undo: path of a specific journal file")
    parser.add_argument("--list-journals", action="store_true", help="show the undo journals of a folder")
    parser.add_argument("--quiet", "-q", action="store_true", help="only print warnings and errors")
    parser.add_argument("--no-logfile", action="store_true", help="do not write a log file next to the journal")
    return parser


def log_level(line: str) -> str:
    """Extract the level token from a logger line ("HH:MM:SS LEVEL message")."""
    parts = line.split(maxsplit=2)
    if len(parts) >= 2 and parts[1] in ("INFO", "WARN", "ERR", "OK"):
        return parts[1]
    return "" if line.lstrip().startswith("-") or not parts else "INFO"


def safe_print(text: str, stream=None) -> None:
    """Print without dying on a legacy console code page (cp1252, cp850...)."""
    stream = stream if stream is not None else sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --undo with an explicit journal needs no --folder at all: the journal
    # records the root it was applied to.
    explicit_journal = Path(args.journal).expanduser() if args.journal else None
    if args.undo and explicit_journal is not None:
        if not explicit_journal.is_file():
            safe_print(f"error: journal not found: {explicit_journal}", sys.stderr)
            return 1
        logger = core.Logger(
            sink=lambda line: (None if args.quiet and log_level(line) in ("INFO", "OK") else safe_print(line))
        )
        try:
            result = core.undo_journal(explicit_journal, logger)
            return 0 if result.restored else 1
        except (OSError, ValueError) as exc:
            logger.error(str(exc))
            return 1
        finally:
            logger.close()

    if not args.folder:
        parser.error("--folder is required")

    root = Path(args.folder).expanduser().resolve()
    if not root.exists():
        safe_print(f"error: folder not found: {root}", sys.stderr)
        return 1

    if args.list_journals:
        journals = core.list_journals(root)
        if not journals:
            safe_print(f"No undo journals found under {root}.")
            return 1
        for path in journals:
            meta, entries = core.read_journal(path)
            ok = sum(1 for e in entries if e.succeeded)
            safe_print(f"{path.name}   {len(entries):>5} action(s) ({ok} applied)   root={meta.get('root', '?')}")
        return 0

    def printer(line: str) -> None:
        if args.quiet and log_level(line) in ("INFO", "OK", ""):
            return
        safe_print(line)

    log_file = None
    if not args.no_logfile:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = root / ".filesorter" / "logs" / f"run-{stamp}.log"
    logger = core.Logger(sink=printer, log_file=log_file)

    try:
        if args.undo:
            journal = Path(args.journal) if args.journal else (core.latest_journal(root) if args.last else None)
            if journal is None:
                safe_print("error: no journal found; pass --journal or --last", sys.stderr)
                return 1
            result = core.undo_journal(journal, logger)
            return 0 if result.restored else 1

        config = core.SortConfig(
            root=root,
            group_by=args.group,
            keywords=_parse_csv(args.keywords),
            rename_template=args.rename,
            folder_template=args.folder_template,
            date_format=args.date_format,
            recurse=args.recurse,
            extension_filter=_parse_csv(args.extensions),
            min_size_kb=args.min_kb,
            max_size_kb=args.max_kb,
            include_hidden=args.hidden,
            dry_run=args.dry_run,
            conflict=args.conflict,
            dedupe=args.dedupe,
        )
        plan, execution = core.run_sort(config, logger, stage=lambda stage: safe_print(f"[{stage}]"))
        if args.dry_run:
            return 0
        return 0 if (execution.moved or execution.renamed) or plan.skipped_unchanged else 1
    except core.CancelledError:
        safe_print("cancelled", sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        safe_print("interrupted", sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        logger.error(str(exc))
        return 1
    finally:
        logger.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
