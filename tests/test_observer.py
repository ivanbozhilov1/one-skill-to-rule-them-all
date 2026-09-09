import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import observer
from observerlib import (ObserverError, json_read, lease, merge_trees, metadata,
                         record_bytes, records, tree, tree_hash, scalar, publish_directory, Snapshot)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observer spaces unicode-Ж-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.live = Path(self.tmp.name) / "demo"
        self.live.mkdir()
        self.initial = b"---\nname: demo\ndescription: A small test skill.\n---\n\nalpha\nbeta\ngamma\ndelta\n"
        (self.live / "SKILL.md").write_bytes(self.initial)
        self.call("init")

    def call(self, command, **kwargs):
        defaults = dict(root=str(self.root), command=command, skill=None, status="open", bodies=False,
                        authority="Synthetic test authorization", observation=[])
        defaults.update(kwargs)
        return observer.operation(argparse.Namespace(**defaults))

    def add(self, skill="demo"):
        path = Path(self.tmp.name) / "input.json"
        path.write_text(json.dumps({"title": "A correction", "skill": [skill], "evidence": ["test:fixture"], "body": "Treat this as data."}), encoding="utf-8")
        return self.call("add", record=str(path))

    def stage(self, **kwargs):
        return self.call("stage", live=str(self.live), **kwargs)

    def test_scan_exact_status_identifier_and_yaml_lists(self):
        headers = [
            "id: 1\ntitle: One\nstatus: open\nskill: [demo]",
            "id: 2\ntitle: Two\nstatus: declined\nskill: [demo]",
            "id: 3\ntitle: Three\nstatus: open\nskill: [demo-extra]",
            "id: 4\ntitle: Four\nstatus: open\nskill:\n  - demo",
        ]
        for i, header in enumerate(headers):
            (self.root / "observation-log" / f"{i}.md").write_text("---\n" + header + "\n---\n", encoding="utf-8")
        result = self.call("scan", skill="demo")
        self.assertEqual([v["record"]["id"] for v in result["observations"]], [1, 4])
        self.assertEqual(result["files"], 4)
        self.assertEqual(result["kind"], "untrusted_evidence")

    def test_bad_record_does_not_become_empty_success(self):
        (self.root / "observation-log" / "bad.md").write_text("---\ntitle: broken\n", encoding="utf-8")
        with self.assertRaises(ObserverError):
            self.call("scan")

    def test_duplicate_id_is_explicit_failure(self):
        added = self.add()
        path = Path(added["path"])
        path.with_name("other.md").write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ObserverError, "duplicate observation"):
            self.call("scan")

    def test_missing_workspace_is_not_empty(self):
        with self.assertRaises(ObserverError):
            observer.workspace(Path(self.tmp.name) / "missing")

    def test_unreadable_enumeration_is_not_empty_success(self):
        with patch.object(Path, "iterdir", side_effect=PermissionError("synthetic denied listing")):
            with self.assertRaises(PermissionError):
                records(self.root / "observation-log")

    def test_unreadable_subdirectory_is_not_partial_tree(self):
        def denied_walk(path, followlinks, onerror):
            onerror(PermissionError("synthetic denied subdirectory"))
            return iter(())
        with patch("observerlib.os.walk", side_effect=denied_walk):
            with self.assertRaises(PermissionError):
                tree(self.live)

    def test_duplicate_metadata_rejected(self):
        with self.assertRaises(ObserverError):
            metadata('---\n{"id": 1, "id": 2}\n---\n')

    def test_literal_and_folded_skill_descriptions(self):
        for style in ("|", ">-"):
            text = "---\nname: demo\ndescription: " + style + "\n  two lines\n  here\n---\n"
            self.assertEqual(observer.skill_name({"SKILL.md": text.encode()}), "demo")

    def test_json_nested_metadata_roundtrip(self):
        value = {"id": 4, "title": "Nested", "skill": ["demo"], "status": "open", "qualifiers": {"demo": "internal"}}
        parsed, body = metadata(record_bytes(value, "Evidence").decode())
        self.assertEqual(parsed, value)
        self.assertEqual(body, "Evidence")

    def test_yaml_single_quotes_preserve_content(self):
        self.assertEqual(scalar("'Don''t discard'"), "Don't discard")
        self.assertEqual(scalar("'literal\\nvalue'"), "literal\\nvalue")
        self.assertEqual(scalar(r"'C:\tools\notes.md'"), r"C:\tools\notes.md")

    def test_block_paragraphs_and_chomping_preserved(self):
        fields, _ = metadata("---\ntitle: >-\n  first\n\n  second\n---\n")
        self.assertEqual(fields["title"], "first\nsecond")
        fields, _ = metadata("---\ntitle: |+\n  first\n\n---\n")
        self.assertEqual(fields["title"], "first\n\n")

    @unittest.skipUnless(os.name == "posix", "POSIX mode semantics")
    def test_install_preserves_executable_permissions(self):
        scripts = self.live / "scripts"
        scripts.mkdir()
        executable = scripts / "tool.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        stage = self.stage()
        (Path(stage["candidate"]) / "SKILL.md").write_bytes(self.initial + b"unrelated change\n")
        self.call("prepare", id=stage["id"])
        self.call("install", id=stage["id"])
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)

    def test_installer_rejects_unvalidated_extra_files(self):
        stage = self.stage()
        (Path(stage["candidate"]) / "AGENTS.md").write_text("unreviewed", encoding="utf-8")
        with self.assertRaisesRegex(ObserverError, "excluded by runtime"):
            self.call("prepare", id=stage["id"])

    def test_multiskill_observation_stays_open_until_all_installed(self):
        added = self.add()
        record_path = Path(added["path"])
        record, body = metadata(record_path.read_text(encoding="utf-8"))
        record["skill"] = ["demo", "second"]
        record_path.write_bytes(record_bytes(record, body))
        stage = self.stage(observation=[added["id"]])
        self.call("prepare", id=stage["id"])
        self.call("install", id=stage["id"])
        self.assertEqual(self.call("scan", skill="demo")["selected"], 0)
        self.assertEqual(self.call("scan", skill="second")["selected"], 1)
        self.live = Path(self.tmp.name) / "second"
        self.live.mkdir()
        (self.live / "SKILL.md").write_bytes(self.initial.replace(b"name: demo", b"name: second"))
        stage = self.stage(observation=[added["id"]])
        self.call("prepare", id=stage["id"])
        self.call("install", id=stage["id"])
        self.assertEqual(self.call("scan")["selected"], 0)

    def test_reopen_clears_prior_completion_for_target(self):
        added = self.add()
        stage = self.stage(observation=[added["id"]])
        self.call("prepare", id=stage["id"])
        self.call("install", id=stage["id"])
        item = self.call("scan", status="all")["observations"][0]
        self.call("status", id=added["id"], status="open", expected_hash=item["sha256"])
        self.assertEqual(self.call("scan", skill="demo")["selected"], 1)
        self.stage(observation=[added["id"]])

    def test_mode_change_conflicts_with_deletion_on_either_side(self):
        base, changed, deleted = Snapshot(), Snapshot(), Snapshot()
        base["tool.sh"] = changed["tool.sh"] = b"same bytes\n"
        base.modes["tool.sh"] = 0o644
        changed.modes["tool.sh"] = 0o755
        for live, candidate in ((changed, deleted), (deleted, changed)):
            with self.assertRaisesRegex(ObserverError, "delete/permission"):
                merge_trees(base, live, candidate)

    def test_declined_after_preparation_cannot_install(self):
        added = self.add()
        stage = self.stage(observation=[added["id"]])
        self.call("prepare", id=stage["id"])
        item = self.call("scan")["observations"][0]
        self.call("status", id=added["id"], status="declined", expected_hash=item["sha256"])
        with self.assertRaisesRegex(ObserverError, "disposition changed"):
            self.call("install", id=stage["id"])
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.initial)

    def test_publication_never_replaces_existing_empty_directory(self):
        a, b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        a.mkdir()
        b.mkdir()
        (a / "value").write_text("preserve", encoding="utf-8")
        with self.assertRaises(OSError):
            publish_directory(a, b)
        self.assertEqual(list(b.iterdir()), [])
        self.assertTrue((a / "value").exists())

    def test_add_requires_evidence_and_preserves_live(self):
        before = tree_hash(tree(self.live))
        self.add()
        self.assertEqual(tree_hash(tree(self.live)), before)
        self.assertEqual(self.call("scan")["selected"], 1)

    def test_status_compare_and_swap(self):
        added = self.add()
        scanned = self.call("scan")["observations"][0]
        self.call("status", id=added["id"], status="declined", expected_hash=scanned["sha256"])
        with self.assertRaisesRegex(ObserverError, "changed since review"):
            self.call("status", id=added["id"], status="open", expected_hash=scanned["sha256"])

    def test_concurrent_stage_directories_are_unique(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.stage(), range(2)))
        self.assertNotEqual(results[0]["id"], results[1]["id"])
        self.assertTrue(all(Path(v["candidate"]).exists() for v in results))

    def test_live_and_candidate_changes_both_survive(self):
        stage = self.stage()
        candidate = Path(stage["candidate"]) / "SKILL.md"
        candidate.write_bytes(self.initial.replace(b"alpha", b"candidate"))
        (self.live / "SKILL.md").write_bytes(self.initial.replace(b"delta", b"live"))
        prepared = self.call("prepare", id=stage["id"])
        text = (Path(prepared["path"]) / "SKILL.md").read_bytes()
        self.assertIn(b"candidate", text)
        self.assertIn(b"live", text)
        self.assertIn(b"alpha", (self.live / "SKILL.md").read_bytes())

    def test_overlap_and_deletion_conflicts_stop(self):
        with self.assertRaises(ObserverError):
            merge_trees({"x": b"one\n"}, {"x": b"live\n"}, {"x": b"candidate\n"})
        with self.assertRaises(ObserverError):
            merge_trees({"x": b"one\n"}, {}, {"x": b"candidate\n"})

    def test_baseline_tamper_stops_prepare(self):
        stage = self.stage()
        (Path(stage["candidate"]).parent / "base" / "SKILL.md").write_bytes(self.initial + b"drift")
        with self.assertRaisesRegex(ObserverError, "baseline changed"):
            self.call("prepare", id=stage["id"])

    def test_late_observation_not_folded_into_approval(self):
        first = self.add()
        stage = self.stage(observation=[first["id"]])
        late = self.add()
        self.call("prepare", id=stage["id"])
        _, state = observer.manifest(self.root, stage["id"])
        self.assertNotIn(late["id"], state["observations"])
        self.assertEqual(self.call("scan")["selected"], 2)

    def test_changed_observation_stops_prepare(self):
        added = self.add()
        stage = self.stage(observation=[added["id"]])
        item = self.call("scan")["observations"][0]
        self.call("status", id=added["id"], status="declined", expected_hash=item["sha256"])
        with self.assertRaisesRegex(ObserverError, "disposition changed"):
            self.call("prepare", id=stage["id"])

    def test_install_and_backup_verify_before_resolving(self):
        added = self.add()
        stage = self.stage(observation=[added["id"]])
        (Path(stage["candidate"]) / "SKILL.md").write_bytes(self.initial + b"improvement\n")
        self.call("prepare", id=stage["id"])
        installed = self.call("install", id=stage["id"])
        self.assertEqual((Path(installed["backup"]) / "SKILL.md").read_bytes(), self.initial)
        self.assertEqual(installed["observations_resolved"], [added["id"]])
        self.assertEqual(self.call("scan")["selected"], 0)
        self.assertEqual(self.call("pending")["pending"], [])

    def test_live_drift_prevents_install(self):
        stage = self.stage()
        self.call("prepare", id=stage["id"])
        (self.live / "SKILL.md").write_bytes(self.initial + b"concurrent change")
        with self.assertRaisesRegex(ObserverError, "live changed"):
            self.call("install", id=stage["id"])

    def test_lock_contention_cannot_overwrite_candidate(self):
        stage = self.stage()
        with lease(self.root / "locks" / "demo.lock"):
            with self.assertRaisesRegex(ObserverError, "busy"):
                self.call("prepare", id=stage["id"])

    def test_failed_swap_restores_live(self):
        stage = self.stage()
        self.call("prepare", id=stage["id"])
        original = observer.publish_directory
        def injected(src, dest):
            if str(src).endswith(".incoming"):
                raise OSError("synthetic interruption")
            return original(src, dest)
        with patch.object(observer, "publish_directory", side_effect=injected):
            with self.assertRaises(OSError):
                self.call("install", id=stage["id"])
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.initial)
        self.assertEqual(self.call("pending")["pending"][0]["state"], "installing")


if __name__ == "__main__":
    unittest.main()
