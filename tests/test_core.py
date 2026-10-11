"""
Self-contained test suite for the Safe File Sorter engine.

Run it with::

    python tests/test_core.py          # standalone, no pytest required
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filesorter import core  # noqa: E402

TS_2024_03_15 = 1710460800  # 2024-03-15 09:20 UTC-ish, fixed for reproducible tests
TS_2023_11_02 = 1698883200  # 2023-11-02

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _pick_writable_temp() -> Path:
    """System temp folder, falling back to ``<project>/.tmp``.

    The fallback keeps the suite usable inside sandboxes and on locked-down
    machines where the system temp directory cannot be written to.
    """
    for candidate in (Path(tempfile.gettempdir()), PROJECT_DIR / ".tmp"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe-{os.getpid()}.tmp"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return PROJECT_DIR


TEMP_ROOT = _pick_writable_temp()
tempfile.tempdir = str(TEMP_ROOT)


def make_temp_dir(prefix: str = "filesorter-test-") -> Path:
    """Like ``tempfile.mkdtemp`` but without its ``chmod 0o700``.

    On Windows that chmod can leave a directory the current token is not
    allowed to create files in (sandboxed or otherwise restricted sessions),
    so tests create their scratch folders with a plain ``mkdir`` instead.
    """
    for _ in range(100):
        candidate = TEMP_ROOT / f"{prefix}{os.urandom(4).hex()}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"could not create a scratch folder under {TEMP_ROOT}")


def force_rmtree(path: Path) -> None:
    """Delete a scratch folder even when a test left files read-only."""
    import stat as _stat

    def on_error(func, target, _exc):  # pragma: no cover - Windows only
        try:
            os.chmod(target, _stat.S_IWRITE | _stat.S_IREAD)
            func(target)
        except OSError:
            pass

    try:
        if not path.exists():
            return
        for root_dir, dirs, files in os.walk(path):
            for name in list(dirs) + list(files):
                try:
                    os.chmod(os.path.join(root_dir, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(path, onerror=on_error)
    except OSError:
        pass


def touch(path: Path, text: str = "x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class SorterTestCase(unittest.TestCase):
    """Creates a throw-away messy folder for every test."""

    def setUp(self) -> None:
        core.reset_cancel()
        self.root = make_temp_dir()
        self.logger = core.Logger()
        self.addCleanup(force_rmtree, self.root)
        self.addCleanup(core.reset_cancel)

    # -- helpers -----------------------------------------------------------
    def make_messy_folder(self) -> None:
        touch(self.root / "holiday.jpg", "jpeg-bytes")
        touch(self.root / "holiday (2).jpg", "jpeg-bytes")
        touch(self.root / "notes.txt", "notes")
        touch(self.root / "budget.xlsx", "xlsx")
        touch(self.root / "Invoice-2024.pdf", "pdf")
        touch(self.root / "receipt-march.pdf", "pdf", TS_2024_03_15)
        touch(self.root / "movie.mp4", "video")
        touch(self.root / "song.mp3", "audio")
        touch(self.root / "archive.zip", "zip")
        touch(self.root / "script.py", "print()")
        touch(self.root / "noextension", "misc")
        touch(self.root / "report.PDF", "pdf", TS_2023_11_02)
        touch(self.root / "sub" / "deep.png", "png")

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root)).replace("\\", "/")

    def plan(self, **kwargs) -> core.PlanResult:
        config = core.SortConfig(root=self.root, **kwargs)
        return core.plan_actions(config, logger=self.logger)

    def destinations(self, plan: core.PlanResult) -> set[str]:
        return {self.rel(a.dst) for a in plan.actions}

    # -- scanning ----------------------------------------------------------
    def test_scan_flat_ignores_subfolders(self) -> None:
        self.make_messy_folder()
        files = core.scan_files(core.SortConfig(root=self.root))
        names = {f.name for f in files}
        self.assertIn("holiday.jpg", names)
        self.assertNotIn("deep.png", names)

    def test_scan_recursive_finds_nested_files(self) -> None:
        self.make_messy_folder()
        files = core.scan_files(core.SortConfig(root=self.root, recurse=True))
        self.assertIn("deep.png", {f.name for f in files})

    def test_scan_skips_own_bookkeeping_folder(self) -> None:
        self.make_messy_folder()
        touch(self.root / ".filesorter" / "logs" / "journal-x.jsonl", "{}")
        files = core.scan_files(core.SortConfig(root=self.root, recurse=True))
        self.assertNotIn("journal-x.jsonl", {f.name for f in files})

    def test_scan_rejects_missing_folder(self) -> None:
        with self.assertRaises(NotADirectoryError):
            core.scan_files(core.SortConfig(root=self.root / "nope"))

    def test_scan_never_follows_symlinks(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation needs privileges on Windows")
        self.make_messy_folder()
        outside = self.root.parent / "outside-target"
        outside.mkdir(exist_ok=True)
        (outside / "outside.txt").write_text("x", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not supported here")
        files = core.scan_files(core.SortConfig(root=self.root, recurse=True))
        self.assertNotIn("outside.txt", {f.name for f in files})

    # -- grouping ----------------------------------------------------------
    def test_group_by_extension(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        destinations = self.destinations(plan)
        self.assertIn("JPG/holiday.jpg", destinations)
        self.assertIn("PDF/report.PDF", destinations)
        self.assertIn(f"{core.NO_EXTENSION_LABEL}/noextension", destinations)

    def test_group_by_category(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_CATEGORY)
        destinations = self.destinations(plan)
        self.assertIn("Images/holiday.jpg", destinations)
        self.assertIn("Documents/Invoice-2024.pdf", destinations)
        self.assertIn("Spreadsheets/budget.xlsx", destinations)
        self.assertIn("Code/script.py", destinations)

    def test_group_by_date_uses_mtime(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_DATE, date_format="%Y/%m")
        destinations = self.destinations(plan)
        self.assertIn("2024/03/receipt-march.pdf", destinations)
        self.assertIn("2023/11/report.PDF", destinations)

    def test_group_by_keyword_with_fallback(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_KEYWORD, keywords=["invoice", "receipt"])
        destinations = self.destinations(plan)
        self.assertIn("invoice/Invoice-2024.pdf", destinations)
        self.assertIn("receipt/receipt-march.pdf", destinations)
        self.assertIn(f"{core.UNSORTED_LABEL}/holiday.jpg", destinations)

    def test_group_flat_moves_nothing_but_renames(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_FLAT)
        self.assertEqual([], plan.actions)

    def test_folder_template_tokens(self) -> None:
        touch(self.root / "photo.PNG", "png", TS_2024_03_15)
        plan = self.plan(folder_template="{category}/{year}")
        self.assertIn("Images/2024/photo.PNG", self.destinations(plan))

    def test_folder_template_unknown_placeholder_is_stripped(self) -> None:
        touch(self.root / "a.txt", "a")
        plan = self.plan(folder_template="Sort/{nonsense}")
        self.assertEqual(["Sort/_Unsorted/a.txt"], [self.rel(a.dst) for a in plan.actions])

    def test_combined_mode_stacks_parts(self) -> None:
        touch(self.root / "invoice-2024.pdf", "pdf", TS_2024_03_15)
        plan = self.plan(group_by=core.GROUP_COMBINED, keywords=["invoice"], date_format="%Y/%m")
        self.assertIn("Documents/invoice/2024/03/invoice-2024.pdf", self.destinations(plan))

    # -- renaming ----------------------------------------------------------
    def test_rename_template_keeps_extension(self) -> None:
        touch(self.root / "IMG_2043.jpg", "j", TS_2024_03_15)
        plan = self.plan(rename_template="{year}{month}{day}_{stem}")
        self.assertIn("JPG/20240315_IMG_2043.jpg", self.destinations(plan))

    def test_rename_template_with_explicit_extension(self) -> None:
        touch(self.root / "IMG_2043.jpg", "j", TS_2024_03_15)
        plan = self.plan(rename_template="{year}-{month}-{day}-{stem}{ext}")
        self.assertIn("JPG/2024-03-15-IMG_2043.jpg", self.destinations(plan))

    def test_rename_produces_safe_names(self) -> None:
        touch(self.root / "we:ird?name.txt", "x")
        plan = self.plan(rename_template='bad<name>|{stem}')
        name = next(a.dst.name for a in plan.actions)
        self.assertFalse(set(name) & set('<>:"/\\|?*'))

    def test_rename_into_same_folder_is_a_pure_rename(self) -> None:
        touch(self.root / "before.txt", "x")
        plan = self.plan(group_by=core.GROUP_FLAT, rename_template="after_{stem}")
        self.assertEqual(1, len(plan.actions))
        action = plan.actions[0]
        self.assertFalse(action.is_move)
        self.assertEqual("after_before.txt", action.dst.name)

    # -- collisions & duplicates -------------------------------------------
    def test_collision_renames_instead_of_overwriting(self) -> None:
        touch(self.root / "JPG" / "holiday.jpg", "existing")
        touch(self.root / "holiday.jpg", "new")
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        self.assertEqual(["JPG/holiday (2).jpg"], [self.rel(a.dst) for a in plan.actions])

    def test_collision_skip_mode(self) -> None:
        touch(self.root / "JPG" / "holiday.jpg", "existing")
        touch(self.root / "holiday.jpg", "new")
        plan = self.plan(group_by=core.GROUP_EXTENSION, conflict=core.CONFLICT_SKIP)
        self.assertEqual([], plan.actions)
        self.assertEqual(1, plan.skipped_conflict)

    def test_identical_duplicate_detected_and_routed(self) -> None:
        touch(self.root / "JPG" / "photo.jpg", "same-bytes")
        touch(self.root / "photo.jpg", "same-bytes")
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        self.assertEqual(1, plan.duplicates)
        target = [self.rel(a.dst) for a in plan.actions if a.duplicate]
        self.assertEqual([".filesorter/_Duplicates/photo.jpg"], target)

    def test_different_content_is_not_a_duplicate(self) -> None:
        touch(self.root / "JPG" / "photo.jpg", "aaa")
        touch(self.root / "photo.jpg", "bbb")
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        self.assertEqual(0, plan.duplicates)
        self.assertEqual(["JPG/photo (2).jpg"], [self.rel(a.dst) for a in plan.actions])

    def test_same_name_taken_by_a_file_that_stays_put(self) -> None:
        # "keep.txt" is already in TXT/ and is not part of the move set, so the
        # incoming root file must be renamed rather than overwrite it.
        touch(self.root / "TXT" / "keep.txt", "already sorted")
        touch(self.root / "keep.txt", "newcomer")
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        self.assertEqual(["TXT/keep (2).txt"], [self.rel(a.dst) for a in plan.actions])

    def test_dedupe_flag_spots_duplicates_inside_the_batch(self) -> None:
        touch(self.root / "one.txt", "identical")
        touch(self.root / "two.txt", "identical")
        plan = self.plan(group_by=core.GROUP_EXTENSION, dedupe=True)
        self.assertEqual(1, plan.duplicates)
        self.assertIn(".filesorter/_Duplicates/two.txt", self.destinations(plan))

    def test_dedupe_keeps_one_canonical_copy_of_many_twins(self) -> None:
        for name in ("aaa.txt", "bbb.txt", "ccc.txt", "ddd.txt"):
            touch(self.root / name, "exactly the same bytes")
        plan = self.plan(group_by=core.GROUP_EXTENSION, dedupe=True)
        self.assertEqual(3, plan.duplicates)
        self.assertEqual(
            {"TXT/aaa.txt"}, {self.rel(a.dst) for a in plan.actions if not a.duplicate}
        )
        routed = sorted(self.rel(a.dst) for a in plan.actions if a.duplicate)
        self.assertEqual(
            [".filesorter/_Duplicates/bbb.txt",
             ".filesorter/_Duplicates/ccc.txt",
             ".filesorter/_Duplicates/ddd.txt"],
            routed,
        )

    def test_two_files_never_get_the_same_target(self) -> None:
        touch(self.root / "a" / "same.txt", "a")
        touch(self.root / "b" / "same.txt", "b", mtime=TS_2024_03_15)
        plan = self.plan(group_by=core.GROUP_EXTENSION, recurse=True)
        targets = [self.rel(a.dst) for a in plan.actions]
        self.assertEqual(len(targets), len(set(targets)))

    # -- filters -----------------------------------------------------------
    def test_extension_filter(self) -> None:
        self.make_messy_folder()
        plan = self.plan(group_by=core.GROUP_EXTENSION, extension_filter=["pdf"])
        self.assertEqual({"PDF/Invoice-2024.pdf", "PDF/receipt-march.pdf", "PDF/report.PDF"},
                         self.destinations(plan))

    def test_size_filter(self) -> None:
        touch(self.root / "small.txt", "x")
        touch(self.root / "big.txt", "y" * 5000)
        plan = self.plan(group_by=core.GROUP_EXTENSION, min_size_kb=1)
        self.assertEqual({"TXT/big.txt"}, self.destinations(plan))

    def test_hidden_files_skipped_by_default(self) -> None:
        touch(self.root / ".secret.txt", "s")
        touch(self.root / "visible.txt", "v")
        plan = self.plan(group_by=core.GROUP_EXTENSION)
        self.assertEqual({"TXT/visible.txt"}, self.destinations(plan))

    def test_hidden_files_can_be_included(self) -> None:
        touch(self.root / ".secret.txt", "s")
        plan = self.plan(group_by=core.GROUP_EXTENSION, include_hidden=True)
        self.assertIn("TXT/.secret.txt", self.destinations(plan))

    # -- safety ------------------------------------------------------------
    def test_already_sorted_folder_produces_no_actions(self) -> None:
        touch(self.root / "TXT" / "a.txt", "a")
        touch(self.root / "JPG" / "b.jpg", "b")
        plan = self.plan(group_by=core.GROUP_EXTENSION, recurse=True)
        self.assertEqual([], plan.actions)
        self.assertEqual(2, plan.skipped_unchanged)

    def test_absolute_template_is_sanitised_instead_of_escaping(self) -> None:
        touch(self.root / "a.txt", "a")
        plan = self.plan(folder_template="C:/Windows/Temp")
        self.assertTrue(plan.actions)
        for action in plan.actions:
            self.assertTrue(core.inside_root(self.root, action.dst))
            self.assertNotIn(":", str(action.dst.relative_to(self.root)))

    def test_traversal_template_cannot_escape_root(self) -> None:
        touch(self.root / "a.txt", "a")
        plan = self.plan(folder_template="../../escape")
        for action in plan.actions:
            self.assertTrue(core.inside_root(self.root, action.dst))

    def test_safe_name_blocks_reserved_windows_names(self) -> None:
        self.assertEqual("_CON", core.safe_component("CON"))
        self.assertEqual("_NUL.txt", core.safe_file_name("NUL.txt"))
        self.assertEqual(".secret.txt", core.safe_file_name(".secret.txt"))
        self.assertEqual("a_b_.txt", core.safe_file_name('a:b?.txt'))

    def test_dry_run_touches_nothing(self) -> None:
        self.make_messy_folder()
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION, dry_run=True)
        plan, execution = core.run_sort(config, self.logger)
        self.assertTrue(plan.actions)
        self.assertTrue(execution.simulated)
        self.assertIsNone(execution.journal)
        before = sorted(f.name for f in self.root.iterdir() if f.is_file())
        self.assertIn("holiday.jpg", before)
        self.assertFalse((self.root / "JPG").exists())
        self.assertFalse((self.root / "TXT").exists())

    # -- execution & undo --------------------------------------------------
    def test_execute_moves_files(self) -> None:
        self.make_messy_folder()
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan, execution = core.run_sort(config, self.logger)
        self.assertGreater(execution.moved, 0)
        self.assertTrue((self.root / "JPG" / "holiday.jpg").is_file())
        self.assertTrue((self.root / "PDF" / "Invoice-2024.pdf").is_file())
        self.assertFalse((self.root / "holiday.jpg").exists())
        self.assertIsNotNone(execution.journal)
        self.assertTrue(execution.journal.is_file())

    def test_execute_never_deletes_and_keeps_every_file(self) -> None:
        self.make_messy_folder()
        before = {f.name for f in self.root.rglob("*") if f.is_file()}
        config = core.SortConfig(root=self.root, group_by=core.GROUP_CATEGORY, recurse=True)
        core.run_sort(config, self.logger)
        after = {
            f.name
            for f in self.root.rglob("*")
            if f.is_file() and ".filesorter" not in f.parts
        }
        self.assertEqual(before, after)

    def test_undo_restores_everything(self) -> None:
        self.make_messy_folder()
        before = {self.rel(f) for f in self.root.rglob("*") if f.is_file()}
        config = core.SortConfig(root=self.root, group_by=core.GROUP_CATEGORY, recurse=True)
        _, execution = core.run_sort(config, self.logger)
        result = core.undo_journal(execution.journal, self.logger)
        self.assertEqual(0, result.failed)
        after = {
            self.rel(f)
            for f in self.root.rglob("*")
            if f.is_file() and ".filesorter" not in f.parts
        }
        self.assertEqual(before, after)

    def test_undo_of_renames_only(self) -> None:
        touch(self.root / "a.txt", "a")
        touch(self.root / "b.txt", "b")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_FLAT, rename_template="x_{stem}")
        _, execution = core.run_sort(config, self.logger)
        self.assertTrue((self.root / "x_a.txt").is_file())
        core.undo_journal(execution.journal, self.logger)
        self.assertTrue((self.root / "a.txt").is_file())
        self.assertFalse((self.root / "x_a.txt").exists())

    def test_undo_does_not_overwrite_a_new_file(self) -> None:
        touch(self.root / "a.txt", "original")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_FLAT, rename_template="sorted_{stem}")
        _, execution = core.run_sort(config, self.logger)
        touch(self.root / "a.txt", "somebody else created this")
        result = core.undo_journal(execution.journal, self.logger)
        self.assertEqual(1, result.restored)
        self.assertEqual("somebody else created this", (self.root / "a.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "a (2).txt").is_file())

    def test_undo_marks_the_journal_as_consumed(self) -> None:
        touch(self.root / "a.txt", "a")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        _, execution = core.run_sort(config, self.logger)
        core.undo_journal(execution.journal, self.logger)
        self.assertIsNone(core.latest_journal(self.root))

    def test_journal_records_config_and_is_readable(self) -> None:
        touch(self.root / "a.txt", "a")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        _, execution = core.run_sort(config, self.logger)
        meta, entries = core.read_journal(execution.journal)
        self.assertEqual(str(self.root), meta["root"])
        self.assertEqual(core.GROUP_EXTENSION, meta["config"]["group_by"])
        self.assertEqual(1, len(entries))
        self.assertTrue(entries[0].succeeded)

    def test_read_journal_tolerates_a_torn_last_line(self) -> None:
        journal = self.root / "journal-torn.jsonl"
        journal.write_text('{"kind": "meta", "root": "x"}\n{"ts": "now", "src": "a", "ds',
                           encoding="utf-8")
        meta, entries = core.read_journal(journal)
        self.assertEqual("x", meta["root"])
        self.assertEqual([], entries)

    def test_second_run_is_a_no_op(self) -> None:
        self.make_messy_folder()
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        core.run_sort(config, self.logger)
        plan, execution = core.run_sort(config, self.logger)
        self.assertEqual([], plan.actions)
        self.assertEqual(0, execution.moved)

    def test_cancel_flag_stops_execution(self) -> None:
        self.make_messy_folder()
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan = core.plan_actions(config, logger=self.logger)
        core.request_cancel()
        result = core.execute_actions(plan.actions, config, self.logger)
        self.assertTrue(result.cancelled)
        self.assertEqual(0, result.moved)
        self.assertTrue((self.root / "holiday.jpg").is_file())

    def test_progress_callbacks_are_invoked(self) -> None:
        self.make_messy_folder()
        seen: list[tuple[int, int]] = []
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        core.plan_actions(config, logger=self.logger, progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)

    def test_plan_reports_instead_of_crashing_on_missing_source(self) -> None:
        # A file that disappears between planning and execution must be
        # reported as skipped, never as a crash.
        touch(self.root / "gone.txt", "x")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan = core.plan_actions(config, logger=self.logger)
        self.assertEqual(1, len(plan.actions))
        (self.root / "gone.txt").unlink()
        execution = core.execute_actions(plan.actions, config, self.logger)
        self.assertEqual(1, execution.skipped)
        self.assertEqual(0, execution.failed)
        self.assertEqual(0, execution.moved)

    # -- regressions -------------------------------------------------------
    def test_template_values_are_not_rescanned(self) -> None:
        """A file called ``a{ext}b.txt`` must not have its own braces expanded.

        The substitution has to happen in a single pass: resolving ``{stem}``
        must not then re-scan the braces that came out of the file name.
        """
        touch(self.root / "a{ext}b.txt", "x")
        touch(self.root / "c{year}.pdf", "x")
        plan = self.plan(rename_template="x_{stem}{ext}")
        names = sorted(a.dst.name for a in plan.actions)
        self.assertEqual(["x_a{ext}b.txt", "x_c{year}.pdf"], names)
        for action in plan.actions:
            self.assertTrue(action.dst.name.startswith("x_"))
            self.assertIn(".", action.dst.name)

    def test_folder_template_values_are_not_rescanned(self) -> None:
        touch(self.root / "weird{category}.txt", "x")
        plan = self.plan(folder_template="{category}")
        self.assertEqual(["Documents/weird{category}.txt"], [self.rel(a.dst) for a in plan.actions])

    def test_planned_name_is_not_stolen_by_a_fallback_rename(self) -> None:
        """A later action's destination must survive an earlier fallback rename."""
        touch(self.root / "TXT" / "x.txt", "PRE-EXISTING, MUST SURVIVE")
        touch(self.root / "x.txt", "incoming one")
        touch(self.root / "X (2).txt", "incoming two")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan = core.plan_actions(config, logger=self.logger)
        self.assertEqual(2, len(plan.actions))
        core.execute_actions(plan.actions, config, self.logger)

        self.assertEqual(
            "PRE-EXISTING, MUST SURVIVE", (self.root / "TXT" / "x.txt").read_text(encoding="utf-8")
        )
        survivors = sorted(p.name for p in (self.root / "TXT").iterdir())
        self.assertEqual(3, len(survivors))
        bodies = {
            (self.root / "TXT" / name).read_text(encoding="utf-8") for name in survivors
        }
        self.assertEqual({"PRE-EXISTING, MUST SURVIVE", "incoming one", "incoming two"}, bodies)

    def test_failures_are_journalled_and_do_not_stop_the_run(self) -> None:
        for index in range(4):
            touch(self.root / f"f{index}.txt", f"body {index}")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan = core.plan_actions(config, logger=self.logger)
        real_move = core._move_file

        def flaky_move(src: Path, dst: Path) -> str:
            if src.name in ("f1.txt", "f2.txt"):
                raise PermissionError(13, "simulated lock")
            return real_move(src, dst)

        core._move_file = flaky_move  # type: ignore[assignment]
        try:
            result = core.execute_actions(plan.actions, config, self.logger)
        finally:
            core._move_file = real_move  # type: ignore[assignment]

        self.assertEqual(2, result.moved)
        self.assertEqual(2, result.failed)
        self.assertEqual(2, len(result.errors))
        self.assertTrue((self.root / "f1.txt").is_file())
        self.assertTrue((self.root / "f2.txt").is_file())

        _meta, entries = core.read_journal(result.journal)
        self.assertEqual(["ok", "failed", "failed", "ok"], [e.result for e in entries])

        undo = core.undo_journal(result.journal, self.logger)
        self.assertEqual(2, undo.restored)
        self.assertEqual(
            ["f0.txt", "f1.txt", "f2.txt", "f3.txt"],
            sorted(p.name for p in self.root.iterdir() if p.is_file()),
        )

    def test_moves_still_happen_when_the_journal_cannot_be_written(self) -> None:
        """No undo journal (e.g. a file squatting on .filesorter) must not stop
        the sort - but the loss of undo has to be reported to the caller."""
        touch(self.root / "a.txt", "a")
        touch(self.root / "b.jpg", "b")
        (self.root / ".filesorter").write_text("not a folder", encoding="utf-8")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan, result = core.run_sort(config, self.logger)
        self.assertEqual(2, result.moved)
        self.assertIsNone(result.journal)
        self.assertTrue(result.journal_error)
        self.assertIn("no undo file", result.summary())
        self.assertTrue((self.root / "TXT" / "a.txt").is_file())
        self.assertTrue((self.root / "JPG" / "b.jpg").is_file())

    # -- regressions found by audit ---------------------------------------
    def test_torn_multibyte_journal_line_is_survivable(self) -> None:
        """A crash mid-write can leave a half-written UTF-8 sequence behind."""
        journal = self.root / "journal-torn-utf8.jsonl"
        record = json.dumps(
            {"ts": "t1", "action": "move", "src": "C:\\x\\café.txt",
             "dst": "C:\\x\\TXT\\café.txt", "result": "ok"},
            ensure_ascii=False,
        ).encode("utf-8")
        journal.write_bytes(
            b'{"kind": "meta", "root": "C:\\\\root"}\n'
            + record
            + b"\n"
            + '{"ts": "t2", "src": "C:\\\\x\\\\caf'.encode("utf-8")
            + b"\xc3"  # truncated multi-byte character
        )
        meta, entries = core.read_journal(journal)  # must not raise
        self.assertEqual("C:\\root", meta["root"])
        self.assertEqual(1, len(entries))
        self.assertEqual("café.txt", Path(entries[0].src).name)
        result = core.undo_journal(journal, self.logger)  # reports, does not crash
        self.assertEqual(1, result.failed)

    def test_duplicates_never_share_a_destination(self) -> None:
        """Three identical files whose own destinations differ must each get a
        unique name inside _Duplicates, so the plan matches what happens."""
        touch(self.root / "a" / "notes.txt", "identical bytes", TS_2023_11_02)
        touch(self.root / "b" / "notes.txt", "identical bytes", TS_2024_03_15)
        touch(self.root / "c" / "notes.txt", "identical bytes", 1710460800 + 86400)
        plan = self.plan(group_by=core.GROUP_DATE, date_format="%Y/%m", recurse=True, dedupe=True)
        targets = [self.rel(a.dst) for a in plan.actions]
        self.assertEqual(len(targets), len(set(targets)), targets)
        self.assertEqual(2, plan.duplicates)
        config = core.SortConfig(
            root=self.root, group_by=core.GROUP_DATE, date_format="%Y/%m",
            recurse=True, dedupe=True,
        )
        execution = core.execute_actions(plan.actions, config, self.logger)
        self.assertEqual(0, execution.failed)
        self.assertEqual(len(plan.actions), execution.moved + execution.renamed)

    def test_rename_template_extension_is_not_doubled(self) -> None:
        touch(self.root / "photo.JPG", "j", TS_2024_03_15)
        cases = {
            "copy_{name}": "copy_photo.JPG",
            "photo_{year}.jpg": "photo_2024.jpg",
            "{stem}.txt": "photo.txt",
            "{stem}_{year}": "photo_2024.JPG",
            "{stem}_{year}{ext}": "photo_2024.JPG",
        }
        for template, expected in cases.items():
            plan = self.plan(group_by=core.GROUP_FLAT, rename_template=template)
            self.assertEqual(1, len(plan.actions), template)
            self.assertEqual(expected, plan.actions[0].dst.name, template)
        # Templates that reproduce the original name are a no-op, never a
        # double suffix: {name}, {stem}, {stem}{ext} all leave it alone.
        for identity in ("{name}", "{stem}", "{stem}{ext}", "photo"):
            plan = self.plan(group_by=core.GROUP_FLAT, rename_template=identity)
            self.assertEqual([], plan.actions, identity)

    def test_folder_template_keyword_segment_is_kept(self) -> None:
        touch(self.root / "invoice-2024.pdf", "p")
        touch(self.root / "photo.jpg", "j")
        plan = self.plan(
            folder_template="{category}/{keyword}", group_by=core.GROUP_FLAT, keywords=["invoice"]
        )
        destinations = self.destinations(plan)
        self.assertIn("Documents/invoice/invoice-2024.pdf", destinations)
        self.assertIn("Images/_Unsorted/photo.jpg", destinations)

    def test_scan_max_files_is_exact_and_sorted(self) -> None:
        for index in range(5):
            touch(self.root / f"f{index}.txt", "x")
        files = core.scan_files(core.SortConfig(root=self.root, max_files=2))
        self.assertEqual(2, len(files))
        self.assertEqual(sorted(p.name for p in files), [p.name for p in files])
        self.assertEqual(0, len(core.scan_files(core.SortConfig(root=self.root, max_files=0))))

    def test_dry_run_creates_nothing_at_all(self) -> None:
        touch(self.root / "a.txt", "a")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION, dry_run=True)
        core.run_sort(config, self.logger)
        self.assertFalse((self.root / ".filesorter").exists())
        self.assertEqual(["a.txt"], sorted(p.name for p in self.root.iterdir()))

    def test_undo_stops_when_cancelled_and_keeps_the_journal(self) -> None:
        for index in range(5):
            touch(self.root / f"u{index}.txt", f"body {index}")
        config = core.SortConfig(root=self.root, group_by=core.GROUP_EXTENSION)
        plan = core.plan_actions(config, logger=self.logger)
        execution = core.execute_actions(plan.actions, config, self.logger)
        self.assertEqual(5, execution.moved)

        def stop_after_first(done: int, total: int) -> None:
            if done >= 1:
                core.request_cancel()

        result = core.undo_journal(execution.journal, self.logger, progress=stop_after_first)
        core.reset_cancel()
        self.assertTrue(result.cancelled)
        self.assertEqual(1, result.restored)
        self.assertTrue(execution.journal.is_file(), "a partial undo must keep its journal")
        # And the remaining items can still be restored afterwards.
        final = core.undo_journal(execution.journal, self.logger)
        self.assertEqual(4, final.restored)

    def test_helpers(self) -> None:
        self.assertEqual("pdf", core.extension_of(Path("a.PDF")))
        self.assertEqual("Other", core.category_of(Path("weird.qqq")))
        self.assertEqual("Images", core.category_of(Path("a.jpeg")))
        self.assertEqual("KB", core.human_size(2048).split()[1])
        self.assertEqual("NUL_y", core.safe_component("NUL_y"))
        self.assertTrue(core.inside_root(self.root, self.root / "a" / "b"))
        self.assertFalse(core.inside_root(self.root, self.root.parent / "b"))
        unique = core.ensure_unique(self.root / "taken.txt", {core._key(self.root / "taken.txt")})
        self.assertEqual("taken (2).txt", unique.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
