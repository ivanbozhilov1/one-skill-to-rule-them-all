"""Migration integrity regressions. All writes use disposable test directories."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-log.py"
spec = importlib.util.spec_from_file_location("migrate_log", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def legacy_entry(ident=2, title="Sample", body="Keep this body.", references=""):
    return (f"### Observation {ident}: {title}\n**Status:** OPEN\n"
            "**Date:** 2026-09-10\n**Skill:** sample\n"
            f"{references}\n**Issue:** {body}\n")


def record(ident=2, title="Sample"):
    entry = next(migration.parse_entries(legacy_entry(ident, title), "log.md"))
    return migration.to_record(entry, set())


def frontmatter(text):
    return json.loads(text.split("\n---\n", 1)[0].removeprefix("---\n"))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="migration-test-")
        self.root = Path(self.temp.name)
        self.log = self.write("log.md", legacy_entry())
        self.out = self.root / "output"
        self.lock = self.root / ".output.migration.lock"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def command(self, *args):
        return [sys.executable, "-B", "-S", str(SCRIPT), *map(str, args)]

    def cli(self, *args):
        return subprocess.run(self.command(*args), cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def stages(self):
        return list(self.root.glob(".output.migration-stage-*"))

    def assert_unpublished(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(self.lock.exists())
        self.assertEqual([], self.stages())

    def test_success_publishes_complete_output_and_preserves_source(self):
        self.log.write_text(legacy_entry(2) + "\n" + legacy_entry(3, "Another"), encoding="utf-8")
        before = self.log.read_bytes()
        result = self.cli("--convert", self.log, "--out", self.out)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["0002-sample.md", "0003-another.md", "archive"],
                         sorted(p.name for p in self.out.iterdir()))
        self.assertEqual(2, frontmatter((self.out / "0002-sample.md").read_text(encoding="utf-8"))["id"])
        self.assertEqual("3\n", (self.out / "archive/.id-floor").read_text())
        self.assertEqual(before, self.log.read_bytes())
        self.assertFalse(self.lock.exists())
        self.assertEqual([], self.stages())

    def test_occupied_output_is_never_merged_or_replaced(self):
        target = self.write("output/0002-sample.md", "Local edited output\n")
        floor = self.write("output/archive/.id-floor", "900\n")
        before = {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
        result = self.cli("--convert", self.log, "--out", self.out, "--id-floor-from", floor.parent)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Output already exists", result.stderr)
        self.assertEqual(before, {p.relative_to(self.out): p.read_bytes() for p in self.out.rglob("*") if p.is_file()})
        self.assertTrue(target.is_file())
        self.assertFalse(self.lock.exists())
        self.assertEqual([], self.stages())

    def test_empty_directory_and_file_destinations_are_refused(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            migration.convert([record()], self.out, [])
        self.assertEqual([], list(self.out.iterdir()))
        self.out.rmdir()
        self.out.write_text("existing file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            migration.convert([record()], self.out, [])
        self.assertEqual("existing file", self.out.read_text(encoding="utf-8"))

    def test_duplicate_ids_and_slug_collisions_fail_before_writing(self):
        for title in ("Sample", "Different title", "SAMPLE!!!"):
            with self.subTest(title=title):
                self.log.write_text(legacy_entry() + "\n" + legacy_entry(2, title), encoding="utf-8")
                result = self.cli("--convert", self.log, "--out", self.out)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Duplicate observation ID", result.stderr)
                self.assert_unpublished()

    def test_same_title_with_distinct_ids_is_valid(self):
        migration.convert([record(2), record(3)], self.out, [])
        self.assertTrue((self.out / "0002-sample.md").is_file())
        self.assertTrue((self.out / "0003-sample.md").is_file())

    def test_floor_file_survives_missing_historical_observations(self):
        floor = self.write("old-archive/.id-floor", "900\n")
        result = self.cli("--convert", self.log, "--out", self.out, "--id-floor-from", floor.parent)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("900\n", (self.out / "archive/.id-floor").read_text())
        self.assertEqual("900\n", floor.read_text())

    def test_floor_is_maximum_across_files_legacy_logs_and_multiple_archives(self):
        self.write("one/.id-floor", "900\n")
        self.write("one/nested/1100-old.md", "Archived body")
        self.write("two/log.md", legacy_entry(1200))
        self.write("two/nested/.id-floor", "1300\n")
        migration.convert([record()], self.out, [self.root / "one", self.root / "two"])
        self.assertEqual("1300\n", (self.out / "archive/.id-floor").read_text())

    def test_bad_or_missing_floor_input_fails_without_writes(self):
        for value in ("invalid", "-1", "", "3.5"):
            with self.subTest(value=value):
                floor = self.write("archive/.id-floor", value)
                with self.assertRaisesRegex(ValueError, "Invalid ID floor"):
                    migration.convert([record()], self.out, [floor.parent])
                self.assert_unpublished()
        with self.assertRaisesRegex(ValueError, "not a directory"):
            migration.convert([record()], self.out, [self.root / "missing"])
        self.assert_unpublished()

    def test_reference_aliases_and_repeated_labels_preserve_all_values(self):
        cases = [
            ("**Reference file:** one.md", "one.md"),
            ("**Reference files:** one.md, two.md", "one.md, two.md"),
            ("**Reference file:** one.md\n**Reference files:** two.md\n  continued.md", "one.md\ntwo.md continued.md"),
            ("**Reference files:** one.md\n**Reference files:** two.md", "one.md\ntwo.md"),
        ]
        for labels, expected in cases:
            with self.subTest(labels=labels):
                entry = next(migration.parse_entries(legacy_entry(references=labels), "log.md"))
                converted = migration.to_record(entry, set())
                rendered = migration.render(converted)
                self.assertEqual(expected, frontmatter(rendered)["reference"])
                self.assertTrue(rendered.endswith("**Issue:** Keep this body.\n"))

    def test_json_frontmatter_preserves_nested_metadata_and_resolution_hint(self):
        rec = record()
        rec.update(status="actioned", type="Improvement: parser", area="a: b",
                   session_context="Context\nwith newline", resolved_hint="2026-09-11",
                   resolution="Applied later", proposes_skill=["new-skill"],
                   skill_qualifiers={"sample": "scope", "_group": ["first", "second"]},
                   override_reason="Manual reconciliation")
        metadata = frontmatter(migration.render(rec))
        for key in ("status", "type", "area", "session_context", "resolution", "proposes_skill", "skill_qualifiers", "resolved_hint"):
            self.assertEqual(rec[key], metadata[key])
        self.assertIsNone(metadata["resolved"])
        self.assertEqual("Manual reconciliation", metadata["migration_override"])

    def test_check_only_and_default_mode_never_write(self):
        for mode in ([], ["--check"]):
            result = self.cli(*mode, self.log, "--out", self.out)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assert_unpublished()

    def test_check_and_convert_are_mutually_exclusive(self):
        result = self.cli("--check", "--convert", self.log, "--out", self.out)
        self.assertEqual(2, result.returncode)
        self.assertIn("not allowed with argument", result.stderr)
        self.assert_unpublished()

    def test_empty_input_is_not_published(self):
        self.log.write_text("Unrecognized log format\n", encoding="utf-8")
        result = self.cli("--convert", self.log, "--out", self.out)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("No observations parsed", result.stderr)
        self.assert_unpublished()

    def test_every_record_is_rendered_before_any_write(self):
        with mock.patch.object(migration, "render", side_effect=["first rendered", ValueError("invalid later record")]):
            with self.assertRaisesRegex(ValueError, "invalid later record"):
                migration.convert([record(2), record(3)], self.out, [])
        self.assert_unpublished()

    def test_existing_lock_is_not_removed_or_overwritten(self):
        self.lock.write_text("prior writer recovery state", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Migration lock exists"):
            migration.convert([record()], self.out, [])
        self.assertFalse(self.out.exists())
        self.assertEqual("prior writer recovery state", self.lock.read_text(encoding="utf-8"))
        self.assertEqual([], self.stages())

    def test_failed_staging_retains_recovery_without_partial_output(self):
        write_file = migration.write_staged_file
        count = 0

        def fail_second(path, content):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected write failure")
            write_file(path, content)

        with mock.patch.object(migration, "write_staged_file", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                migration.convert([record(2), record(3)], self.out, [])
        self.assertFalse(self.out.exists())
        self.assertTrue(self.lock.exists())
        self.assertEqual(1, len(self.stages()))
        self.assertTrue((self.stages()[0] / "0002-sample.md").is_file())

    def test_concurrent_destination_is_never_replaced(self):
        publish = migration.rename_new_directory

        def create_destination_then_publish(stage, out):
            out.mkdir()
            (out / "keep.txt").write_text("another writer", encoding="utf-8")
            publish(stage, out)

        with mock.patch.object(migration, "rename_new_directory", side_effect=create_destination_then_publish):
            with self.assertRaises(OSError):
                migration.convert([record()], self.out, [])
        self.assertEqual(["keep.txt"], [p.name for p in self.out.iterdir()])
        self.assertEqual("another writer", (self.out / "keep.txt").read_text(encoding="utf-8"))
        self.assertTrue(self.lock.exists())
        self.assertTrue((self.stages()[0] / "archive/.id-floor").is_file())

    def test_even_concurrent_empty_destination_is_not_replaced(self):
        publish = migration.rename_new_directory

        def occupy(stage, out):
            out.mkdir()
            publish(stage, out)

        with mock.patch.object(migration, "rename_new_directory", side_effect=occupy):
            with self.assertRaises(OSError):
                migration.convert([record()], self.out, [])
        self.assertEqual([], list(self.out.iterdir()))
        self.assertTrue(self.lock.exists())
        self.assertEqual(1, len(self.stages()))

    def test_competing_processes_publish_once(self):
        command = self.command("--convert", self.log, "--out", self.out)
        processes = [subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
        outcomes = [process.communicate(timeout=20) for process in processes]
        self.assertEqual([0, 1], sorted(p.returncode for p in processes), outcomes)
        self.assertTrue((self.out / "0002-sample.md").is_file())
        self.assertEqual("2\n", (self.out / "archive/.id-floor").read_text())
        self.assertFalse(self.lock.exists())
        self.assertEqual([], self.stages())

    def test_process_death_during_staging_leaves_no_partial_canonical_output(self):
        injected = '''import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("migration", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
entry = next(mod.parse_entries(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"), "log.md"))
original = mod.write_staged_file
def die_after_first_write(path, content):
    original(path, content)
    os._exit(73)
mod.write_staged_file = die_after_first_write
mod.convert([mod.to_record(entry, set())], sys.argv[3], [])
'''
        result = subprocess.run([sys.executable, "-B", "-S", "-c", injected,
                                 str(SCRIPT), str(self.log), str(self.out)], cwd=self.root,
                                capture_output=True, timeout=20)
        self.assertEqual(73, result.returncode, result.stderr)
        self.assertFalse(self.out.exists())
        self.assertTrue(self.lock.exists())
        self.assertEqual(1, len(self.stages()))
        self.assertTrue((self.stages()[0] / "0002-sample.md").is_file())


if __name__ == "__main__":
    unittest.main()
