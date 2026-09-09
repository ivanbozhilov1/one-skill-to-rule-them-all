"""Adversarial bundle contract tests. All writes stay in temporary fixtures."""

import contextlib
import importlib.util
import io
import os
import pathlib
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "validate-skill-bundle.py"
SPEC = importlib.util.spec_from_file_location("bundle_validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.root = self.base / "repository checkout"
        self.root.mkdir()
        self.bundle = self.base / "task-observer.skill"
        self.write_skill()
        (self.root / "LICENSE.txt").write_text("Example license\n", encoding="utf-8")
        for folder in ("references", "scripts", "agents"):
            (self.root / folder).mkdir()
        (self.root / "references" / "guide.md").write_text("Reference guide.\n", encoding="utf-8")
        (self.root / "scripts" / "helper.py").write_text("print('example')\n", encoding="utf-8")
        (self.root / "agents" / "config.yaml").write_text("label: Example\n", encoding="utf-8")

    def write_skill(self, description="A useful test skill.", name="task-observer"):
        (self.root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# Example\n",
            encoding="utf-8")

    def gate(self, *options):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = validator.main(["validator", str(self.root), *map(str, options)])
        return result, output.getvalue()

    def expected_bytes(self):
        return {f"task-observer/{name}": path.read_bytes()
                for name, path in validator.runtime_files(self.root, []).items()}

    def write_zip(self, members=None):
        members = members if members is not None else self.expected_bytes().items()
        with zipfile.ZipFile(self.bundle, "w", zipfile.ZIP_STORED) as archive:
            for name, content in members:
                archive.writestr(name, content)

    def assert_bad_bundle(self, needle=None):
        result, output = self.gate("--bundle", self.bundle)
        self.assertEqual(result, 1, output)
        if needle:
            self.assertIn(needle, output)

    def test_positive_roundtrip_and_runtime_allowlist(self):
        for relative in ("README.md", "USER-GUIDE.md", "CONTRIBUTING.md", ".git/config",
                         "tests/test_fake.py", "scripts/__pycache__/junk.pyc",
                         "scripts/.DS_Store", "references/.~lock.notes"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("excluded\n", encoding="utf-8")
        result, output = self.gate("--pack", self.bundle)
        self.assertEqual(result, 0, output)
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertEqual(set(archive.namelist()), set(self.expected_bytes()))
            self.assertEqual(len(archive.namelist()), 5)
            self.assertTrue(all("\\" not in name for name in archive.namelist()))
        self.assertEqual(self.gate("--bundle", self.bundle)[0], 0)

    def test_dot_packing_uses_skill_name_not_checkout_name(self):
        previous = pathlib.Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = validator.main(["validator", ".", "--pack", "output.skill"])
            self.assertEqual(result, 0, output.getvalue())
        finally:
            os.chdir(previous)
        with zipfile.ZipFile(self.root / "output.skill") as archive:
            self.assertIn("task-observer/SKILL.md", archive.namelist())
            self.assertNotIn("task-observer/output.skill", archive.namelist())

    def test_invalid_zip_with_forged_central_signature(self):
        self.bundle.write_bytes(b"not a zip PK\x01\x02" + bytes(100))
        self.assert_bad_bundle("invalid or unreadable ZIP")

    def test_traversal_only_zip_is_not_a_skill(self):
        self.write_zip([("../escape.txt", b"escape")])
        self.assert_bad_bundle("unsafe member")
        self.assertFalse((self.base / "escape.txt").exists())

    def test_unsafe_member_names_are_rejected(self):
        for name in ("/absolute.txt", "C:/drive.txt", "C:drive.txt",
                     "task-observer/../escape.txt",
                     "task-observer/./SKILL.md", "task-observer//SKILL.md",
                     "//server/share.txt", "task-observer/script.py:stream"):
            with self.subTest(name=name):
                members = list(self.expected_bytes().items()) + [(name, b"unsafe")]
                self.write_zip(members)
                self.assert_bad_bundle("unsafe member")

    def test_raw_backslash_member_is_rejected_on_windows(self):
        # ZipInfo normalizes names while writing on Windows; modify both raw
        # headers so the fixture actually contains an installer-unsafe path.
        self.write_zip()
        original = b"task-observer/scripts/helper.py"
        changed = b"task-observer\\scripts/helper.py"
        content = self.bundle.read_bytes()
        self.assertEqual(content.count(original), 2)
        self.bundle.write_bytes(content.replace(original, changed))
        self.assert_bad_bundle("unsafe member")

    def test_unexpected_repository_docs_rejected(self):
        self.write_zip(list(self.expected_bytes().items()) +
                       [("task-observer/README.md", b"not runtime")])
        self.assert_bad_bundle("unexpected runtime member")

    def test_missing_member_rejected(self):
        members = self.expected_bytes()
        del members["task-observer/references/guide.md"]
        self.write_zip(members.items())
        self.assert_bad_bundle("missing runtime member")

    def test_wrong_root_and_missing_root_skill_rejected(self):
        self.write_zip([(name.replace("task-observer/", "other-root/"), data)
                        for name, data in self.expected_bytes().items()])
        self.assert_bad_bundle("missing runtime member task-observer/SKILL.md")

    def test_changed_same_length_payload_rejected_by_hash(self):
        members = self.expected_bytes()
        original = members["task-observer/references/guide.md"]
        members["task-observer/references/guide.md"] = b"X" + original[1:]
        self.write_zip(members.items())
        self.assert_bad_bundle("content hash differs")

    def test_duplicate_member_rejected(self):
        members = list(self.expected_bytes().items())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            self.write_zip(members + [members[0]])
        self.assert_bad_bundle("duplicate member")

    def test_corrupt_payload_crc_rejected(self):
        self.write_zip()
        with zipfile.ZipFile(self.bundle) as archive:
            info = archive.getinfo("task-observer/references/guide.md")
        content = bytearray(self.bundle.read_bytes())
        filename_length, extra_length = struct.unpack_from("<HH", content, info.header_offset + 26)
        payload = info.header_offset + 30 + filename_length + extra_length
        content[payload] ^= 0x01
        self.bundle.write_bytes(content)
        self.assert_bad_bundle("invalid or unreadable ZIP")

    def test_truncated_zip_rejected(self):
        self.write_zip()
        self.bundle.write_bytes(self.bundle.read_bytes()[:-12])
        self.assert_bad_bundle("invalid or unreadable ZIP")

    def test_zip_symlink_rejected(self):
        members = self.expected_bytes()
        self.write_zip(members.items())
        with zipfile.ZipFile(self.bundle, "a") as archive:
            info = zipfile.ZipInfo("task-observer/scripts/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../SKILL.md")
        self.assert_bad_bundle("non-regular member")

    def test_runtime_symlink_rejected(self):
        link = self.root / "scripts" / "linked.py"
        try:
            link.symlink_to(self.root / "scripts" / "helper.py")
        except OSError as exc:
            self.skipTest("symlink creation unavailable: " + str(exc))
        result, output = self.gate("--pack", self.bundle)
        self.assertEqual(result, 1, output)
        self.assertIn("symlink", output)
        self.assertFalse(self.bundle.exists())

    def test_self_including_destination_rejected(self):
        target = self.root / "scripts" / "bundle.skill"
        result, output = self.gate("--pack", target)
        self.assertEqual(result, 1, output)
        self.assertIn("include or overwrite runtime", output)
        self.assertFalse(target.exists())

    def test_overwriting_skill_with_bundle_rejected(self):
        target = self.root / "SKILL.md"
        before = target.read_bytes()
        result, output = self.gate("--pack", target)
        self.assertEqual(result, 1, output)
        self.assertEqual(target.read_bytes(), before)

    def test_block_description_values_are_parsed(self):
        cases = {
            "|\n  first\n  second": "first\nsecond\n",
            "|-\n  first\n  second": "first\nsecond",
            "|+\n  first\n\n": "first\n\n\n",
            ">\n  first\n  second": "first second\n",
            ">-\n  first\n\n  second": "first\nsecond",
            '"true"': "true",
            "'it''s quoted'": "it's quoted",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(validator.folded_description("description: " + source), expected)

    def test_description_limits_include_literal_newlines(self):
        self.write_skill("|-\n  " + "A" * 512 + "\n  " + "B" * 512)
        result, output = self.gate()
        self.assertEqual(result, 1, output)
        self.assertIn("description 1025 chars", output)
        self.write_skill("|-\n  " + "A" * 511 + "\n  " + "B" * 512)
        self.assertEqual(self.gate()[0], 0)

    def test_folded_description_limits_include_chomping(self):
        self.write_skill(">\n  " + "A" * 1024)
        result, output = self.gate()
        self.assertEqual(result, 1, output)
        self.assertIn("description 1025 chars", output)
        self.write_skill(">-\n  " + "A" * 1024)
        self.assertEqual(self.gate()[0], 0)

    def test_description_non_string_types_are_rejected(self):
        for value in ("true", "12", "null", "[]", "{}", "[one, two]", ""):
            with self.subTest(value=value):
                self.write_skill(value)
                result, output = self.gate()
                self.assertEqual(result, 1, output)
                self.assertIn("non-string YAML scalar", output)

    def test_unsupported_yaml_and_duplicate_keys_rejected(self):
        for source in ("description: >2\n  indented", "description: &anchor text",
                       "description: test\ndescription: duplicate",
                       "description: >\n  first\n    more-indented",
                       "description:\n  nested: value"):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    validator.parse_frontmatter(source)

    def test_current_repository_runtime_tree_is_valid(self):
        failures = []
        with contextlib.redirect_stdout(io.StringIO()):
            context = validator.check_dir(SCRIPT.parents[1], failures)
        self.assertIsNotNone(context)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
