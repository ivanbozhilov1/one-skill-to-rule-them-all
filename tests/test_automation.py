"""Synthetic native events: never reads real Codex history."""
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import automation as a
from observerlib import ObserverError, json_write


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observer auto spaces ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        json_write(self.state / "workspace.json", {"schema": 1})
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.transcript = self.sessions / "session.jsonl"
        self.transcript.touch()
        self.config = {"state_root": str(self.state), "sessions_root": str(self.sessions)}
        a.initialize(self.config)
        self.auto = self.state / "automatic"
        self.event = {"session_id": "session-12345678", "turn_id": "turn-12345678", "transcript_path": str(self.transcript)}

    def invoke(self, name, **kw):
        return a.hook(self.config, dict(self.event, hook_event_name=name, **kw))

    def append(self, rows):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def capture(self, count=2):
        self.invoke("UserPromptSubmit", prompt="Synthetic task")
        self.append([{"type": "turn_context", "payload": {"turn_id": self.event["turn_id"]}},
                     {"type": "event_msg", "payload": {"type": "user_message", "message": "Synthetic task email alice@example.test"}},
                     *[{"type": "response_item", "payload": {"type": "function_call", "name": "fixture", "arguments": "NEVER_COPY_TOOL_ARGUMENT"}} for _ in range(count)],
                     {"type": "response_item", "payload": {"type": "function_call_output", "output": "NEVER_COPY_TOOL_OUTPUT"}},
                     {"type": "event_msg", "payload": {"type": "agent_message", "message": "Corrected the synthetic case"}}])
        self.invoke("Stop")
        return a.pending(self.config)

    def test_empty_gate_stops_before_model(self):
        self.assertEqual(self.invoke("UserPromptSubmit", prompt=a.MARKER + " review"), {"continue": False})
        self.assertEqual(a.pending(self.config), [])
        self.assertEqual(a.batch(self.config, 10), {"pending": 0, "batch": None})

    def test_ordinary_prompt_is_unchanged(self):
        self.assertEqual(self.invoke("UserPromptSubmit", prompt="Hello"), {})

    def test_desktop_wrapped_review_is_gated(self):
        prompt = "Automation: Daily review\nAutomation ID: fixture-review\nAutomation memory: $CODEX_HOME/automations/fixture-review/memory.md\nLast run: Never\n\n" + a.MARKER
        self.config["automation_id"] = "fixture-review"
        self.assertEqual(self.invoke("UserPromptSubmit", prompt=prompt), {"continue": False})
        self.assertFalse(a.review_prompt(prompt, "other-review"))
        self.assertFalse(a.review_prompt("Quoted example: " + a.MARKER))

    def test_empty_gate_needs_no_transcript(self):
        self.event["transcript_path"] = None
        self.assertEqual(self.invoke("UserPromptSubmit", prompt=a.MARKER), {"continue": False})

    def test_subagent_does_not_overwrite_parent_state(self):
        self.invoke("UserPromptSubmit", prompt=a.MARKER)
        before = (self.auto / "turns" / (self.event["session_id"] + ".json")).read_bytes()
        self.assertEqual(self.invoke("UserPromptSubmit", prompt="Child", agent_id="child-12345678"), {})
        self.assertEqual((self.auto / "turns" / (self.event["session_id"] + ".json")).read_bytes(), before)

    def test_steering_preserves_first_boundary(self):
        self.invoke("UserPromptSubmit", prompt="Original")
        before = (self.auto / "turns" / (self.event["session_id"] + ".json")).read_bytes()
        self.append([{"type": "response_item", "payload": {"type": "function_call", "name": "first"}}])
        self.invoke("UserPromptSubmit", prompt="Steering")
        self.assertEqual((self.auto / "turns" / (self.event["session_id"] + ".json")).read_bytes(), before)

    def test_broken_gate_fails_closed_but_normal_task_proceeds(self):
        missing = self.root / "missing-config.json"
        command = [sys.executable, "-B", str(Path(a.__file__)), "--config", str(missing), "hook"]
        for prompt, stopped in ((a.MARKER, True), ("Hello", False)):
            result = subprocess.run(command, input=json.dumps(dict(self.event, hook_event_name="UserPromptSubmit", prompt=prompt)), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            if stopped:
                self.assertIs(json.loads(result.stdout)["continue"], False)
            else:
                self.assertEqual(result.stdout, "")

    def test_capture_is_metadata_only_and_deduplicated(self):
        records = self.capture()
        self.assertEqual(len(records), 1)
        self.invoke("Stop")
        self.assertEqual(a.pending(self.config), records)
        data = json.dumps(records)
        for secret in ("NEVER_COPY", "alice@", "Synthetic task"):
            self.assertNotIn(secret, data)

    def test_trivial_turn_has_no_receipt(self):
        self.assertEqual(self.capture(1), [])

    def test_no_capture_for_preinstall_turn(self):
        self.assertEqual(self.invoke("Stop"), {})

    def test_review_turn_never_captures_itself(self):
        records = self.capture()
        self.event["turn_id"] = "review-12345678"
        self.assertEqual(self.invoke("UserPromptSubmit", prompt=a.MARKER), {})
        self.append([{"type": "response_item", "payload": {"type": "function_call", "name": "fixture"}}] * 3)
        self.invoke("Stop")
        self.assertEqual(a.pending(self.config), records)

    def test_excluded_session(self):
        self.config["excluded_sessions"] = [self.event["session_id"]]
        self.assertEqual(self.capture(), [])

    def test_missing_native_turn_identity_is_error(self):
        del self.event["turn_id"]
        with self.assertRaises(ObserverError):
            self.invoke("UserPromptSubmit", prompt="Hello")

    def test_mismatched_stop_is_error(self):
        self.invoke("UserPromptSubmit", prompt="Hello")
        self.event["turn_id"] = "other-12345678"
        with self.assertRaises(ObserverError):
            self.invoke("Stop")

    def test_cross_turn_transcript_is_rejected(self):
        self.invoke("UserPromptSubmit", prompt="Hello")
        self.append([{"type": "turn_context", "payload": {"turn_id": "other-12345678"}}])
        with self.assertRaises(ObserverError):
            self.invoke("Stop")

    def test_evidence_is_redacted_and_excludes_tool_output(self):
        receipt = self.capture()[0]
        result = json.dumps(a.evidence(self.config, receipt["id"]))
        self.assertIn("[email]", result)
        self.assertIn("Corrected the synthetic case", result)
        self.assertNotIn("alice@", result)
        self.assertNotIn("NEVER_COPY", result)

    def test_changed_evidence_fails(self):
        receipt = self.capture()[0]
        data = self.transcript.read_bytes().replace(b"fixture", b"changed")
        self.transcript.write_bytes(data)
        with self.assertRaisesRegex(ObserverError, "evidence changed"):
            a.evidence(self.config, receipt["id"])

    def test_transcript_outside_sessions_is_rejected(self):
        outside = self.root / "outside.jsonl"
        outside.touch()
        self.event["transcript_path"] = str(outside)
        with self.assertRaises(ObserverError):
            self.invoke("UserPromptSubmit", prompt="Hello")

    def test_malformed_queue_fails_instead_of_empty(self):
        json_write(self.auto / "receipts" / "bad.json", {"schema": 8})
        with self.assertRaises(ObserverError):
            self.invoke("UserPromptSubmit", prompt=a.MARKER)

    def test_acknowledgement_requires_review_and_is_idempotent(self):
        receipt = self.capture()[0]
        batch = a.batch(self.config, 10)
        report = self.auto / "reviews" / "result.json"
        json_write(report, {"batch_id": batch["id"], "reviewed": [{"id": receipt["id"], "disposition": "no_action", "reason": "Synthetic case adds no learning."}]})
        self.assertEqual(a.finish(self.config, batch["id"], str(report)), {"acknowledged": 1, "remaining": 0})
        self.assertEqual(a.finish(self.config, batch["id"], str(report))["remaining"], 0)
        self.assertEqual(self.invoke("UserPromptSubmit", prompt=a.MARKER), {"continue": False})

    def test_forged_batch_coverage_is_rejected(self):
        self.capture()
        batch = a.batch(self.config, 10)
        report = self.auto / "reviews" / "result.json"
        json_write(report, {"batch_id": batch["id"], "reviewed": [{"id": "f" * 64, "disposition": "no_action", "reason": "Bad id"}]})
        with self.assertRaises(ObserverError):
            a.finish(self.config, batch["id"], str(report))
        self.assertEqual(len(a.pending(self.config)), 1)

    def test_needs_evidence_remains_pending(self):
        receipt = self.capture()[0]
        batch = a.batch(self.config, 10)
        report = self.auto / "reviews" / "result.json"
        json_write(report, {"batch_id": batch["id"], "reviewed": [{"id": receipt["id"], "disposition": "needs_evidence", "reason": "Need independent evidence"}]})
        self.assertEqual(a.finish(self.config, batch["id"], str(report)), {"acknowledged": 0, "remaining": 1})

    def test_changed_review_report_does_not_silently_skip_work(self):
        self.test_acknowledgement_requires_review_and_is_idempotent()
        (self.auto / "reviews" / "result.json").write_text("{}")
        with self.assertRaisesRegex(ObserverError, "evidence changed"):
            a.pending(self.config)

    def test_redaction(self):
        value = a.redact("password=hunter-fixture\nalice@example.test +63 917 123 4567\nghp_012345678901234567890123456789\nACCESS_TOKEN=synthetic-token-value\nAWS_ACCESS_KEY_ID=AKIAEXAMPLEONLYTEST00")
        self.assertNotIn("hunter", value)
        self.assertNotIn("alice", value)
        self.assertNotIn("917", value)
        self.assertNotIn("ghp_", value)
        self.assertNotIn("synthetic-token-value", value)
        self.assertNotIn("AKIA", value)


if __name__ == "__main__":
    unittest.main()
