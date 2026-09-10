#!/usr/bin/env python3
"""Optional Codex completion adapter and deterministic review queue. Offline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

sys.dont_write_bytecode = True
from observerlib import ObserverError, absolute, digest, json_read, json_write, lease

MARKER = "[task-observer:auto-review:v1]"
MAX_BYTES = 2 * 1024 * 1024


def review_prompt(prompt, automation_id=None):
    if not isinstance(prompt, str):
        return False
    if prompt.startswith(MARKER):
        return True
    # Desktop cron prepends this envelope before submitting native text input.
    match = re.match(r"\AAutomation: [^\r\n]+\r?\nAutomation ID: ([^\r\n]+)\r?\nAutomation memory: [^\r\n]+\r?\nLast run: [^\r\n]*\r?\n\r?\n", prompt)
    return bool(match and (automation_id is None or match[1] == automation_id) and prompt[match.end():].startswith(MARKER))


def utc():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", value):
        raise ObserverError("missing or invalid native session/turn identity")
    return value


def store(config):
    root = absolute(config["state_root"])
    # Init is deployment-owned. A hook cannot create another workspace implicitly.
    if not (root / "automatic" / "identity.json").is_file():
        raise ObserverError("automatic queue is not initialized")
    data = json_read(root / "automatic" / "identity.json")
    if data != {"schema": 1, "root": str(root)}:
        raise ObserverError("automatic workspace identity mismatch")
    for folder in ("turns", "receipts", "decisions", "batches", "reviews"):
        absolute(root / "automatic" / folder)
        if not (root / "automatic" / folder).is_dir():
            raise ObserverError("automatic workspace is incomplete")
    return root / "automatic"


def initialize(config):
    root = absolute(config["state_root"])
    if not (root / "workspace.json").is_file():
        raise ObserverError("initialize the observer workspace first")
    auto = root / "automatic"
    absolute(auto)
    auto.mkdir(exist_ok=True)
    with lease(auto / ".init.lock"):
        expected = {"schema": 1, "root": str(root)}
        if (auto / "identity.json").exists() and json_read(auto / "identity.json") != expected:
            raise ObserverError("existing automatic queue identity differs")
        for folder in ("turns", "receipts", "decisions", "batches", "reviews"):
            absolute(auto / folder).mkdir(exist_ok=True)
        json_write(auto / "identity.json", expected)
    return {"initialized": True}


def pending(config):
    auto = store(config)
    acknowledged = set()
    for path in (auto / "decisions").iterdir():
        if path.suffix == ".json":
            decision = json_read(absolute(path))
            if decision.get("schema") != 1 or not isinstance(decision.get("reviewed"), list):
                raise ObserverError("malformed review decision")
            report = absolute(decision["report"])
            if (auto / "reviews") not in report.parents or digest(report.read_bytes()) != decision["report_sha256"]:
                raise ObserverError("review decision evidence changed")
            acknowledged.update(decision["reviewed"])
    result = []
    for path in (auto / "receipts").iterdir():
        if path.suffix != ".json":
            continue
        receipt = json_read(absolute(path))
        if receipt.get("schema") != 1 or receipt.get("id") != path.stem:
            raise ObserverError("malformed captured receipt")
        if receipt["id"] not in acknowledged:
            result.append(receipt)
    return sorted(result, key=lambda item: (item["captured_at"], item["id"]))


def transcript(config, value):
    path = absolute(value)
    sessions = absolute(config["sessions_root"])
    if sessions not in path.parents or path.suffix != ".jsonl" or not path.is_file():
        raise ObserverError("transcript outside configured Codex sessions")
    return path


def read_span(path, start, end):
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or end > path.stat().st_size:
        raise ObserverError("transcript range changed")
    length = end - start
    # Bound the capture cost, retaining exact selected ranges for later verification.
    ranges = [(start, end)] if length <= MAX_BYTES else [(start, start + MAX_BYTES // 2), (end - MAX_BYTES // 2, end)]
    chunks, entries = [], []
    with path.open("rb") as stream:
        for left, right in ranges:
            stream.seek(left)
            data = stream.read(right - left)
            chunks.append(data)
            lines = data.splitlines()
            if left != start:
                lines = lines[1:]
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except (ValueError, UnicodeError):
                    continue  # Boundary fragments only; receipt labels sampled coverage.
    return entries, digest(b"\x00".join(chunks)), length <= MAX_BYTES


def hook(config, event):
    if event.get("agent_id"):
        return {}  # Subagents do not own the parent session's prompt boundary.
    auto = store(config)
    session = identity(event.get("session_id") or event.get("sessionId"))
    if session in config.get("excluded_sessions", []):
        return {}
    name = event.get("hook_event_name")
    if name not in ("UserPromptSubmit", "Stop"):
        return {}
    state_path = absolute(auto / "turns" / (session + ".json"))
    if name == "UserPromptSubmit":
        turn = identity(event.get("turn_id") or event.get("turnId"))
        prompt = event.get("prompt", "")
        if not isinstance(prompt, str):
            raise ObserverError("unsupported prompt shape")
        scheduled = review_prompt(prompt, config.get("automation_id"))
        if scheduled:
            # Gate does not depend on a transcript: first-turn logs may not exist yet.
            count = len(pending(config))
            json_write(auto / "last-gate.json", {"checked_at": utc(), "session": session, "turn": turn, "pending": count, "model_requested": bool(count)})
            json_write(state_path, {"session": session, "turn": turn, "self": True, "started_at": utc()})
            return {} if count else {"continue": False}
        if state_path.exists():
            previous = json_read(state_path)
            if previous.get("turn") == turn:
                return {}  # Steering preserves first boundary and the review's self marker.
        path = transcript(config, event.get("transcript_path", ""))
        state = {"session": session, "turn": turn, "transcript": str(path), "start": path.stat().st_size, "self": False, "started_at": utc()}
        json_write(state_path, state)
        return {}
    if not state_path.exists():
        return {}  # A task started before installation is outside capture coverage.
    state = json_read(state_path)
    if state["self"]:
        return {}
    if identity(event.get("turn_id") or event.get("turnId")) != state["turn"]:
        raise ObserverError("Stop turn does not match captured prompt boundary")
    path = transcript(config, state["transcript"])
    end = path.stat().st_size
    entries, fingerprint, complete = read_span(path, state["start"], end)
    turns = {r.get("payload", {}).get("turn_id") for r in entries if r.get("type") == "turn_context"}
    if turns and turns != {state["turn"]}:
        raise ObserverError("ambiguous transcript turn boundaries")
    calls = [r["payload"] for r in entries if r.get("type") == "response_item" and r.get("payload", {}).get("type") in ("function_call", "custom_tool_call")]
    if len(calls) < config.get("minimum_tool_calls", 2):
        return {}
    identifier = hashlib.sha256((session + ":" + state["turn"]).encode()).hexdigest()
    receipt = {"schema": 1, "id": identifier, "provider": "codex", "session_id": session, "turn_id": state["turn"], "transcript": str(path), "start": state["start"], "end": end, "span_sha256": fingerprint, "coverage_complete": complete, "sampled_tool_calls": len(calls), "tools": sorted(set(str(v.get("name", "unknown")) for v in calls)), "captured_at": utc(), "trust": "evidence_only"}
    target = auto / "receipts" / (identifier + ".json")
    with lease(auto / ".capture.lock"):
        if not target.exists():
            json_write(target, receipt)
    return {}


def redact(text):
    # Deliberately conservative: never duplicate raw tool output or credential lines.
    safe = []
    for line in text.splitlines():
        if re.search(r"(?i)password|secret|api.?key|access.?key|token\s*[=:]|authorization|bearer |cookie|private.key|service.account|client|customer|patient|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", line):
            safe.append("[sensitive line omitted]")
            continue
        line = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[email]", line)
        line = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}\b", "[credential]", line)
        line = re.sub(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)", "[number]", line)
        safe.append(line)
    return "\n".join(safe)[:4000]


def evidence(config, identifier):
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ObserverError("invalid receipt id")
    receipt = json_read(absolute(store(config) / "receipts" / (identifier + ".json")))
    path = transcript(config, receipt["transcript"])
    entries, fingerprint, complete = read_span(path, receipt["start"], receipt["end"])
    if fingerprint != receipt["span_sha256"]:
        raise ObserverError("captured transcript evidence changed")
    messages = []
    for row in entries:
        value = row.get("payload", {})
        if row.get("type") == "event_msg" and value.get("type") in ("user_message", "agent_message") and isinstance(value.get("message"), str):
            messages.append({"role": value["type"], "text": redact(value["message"])})
        elif row.get("type") == "event_msg" and value.get("type") == "task_complete" and isinstance(value.get("last_agent_message"), str):
            messages.append({"role": "assistant_final", "text": redact(value["last_agent_message"])})
        elif row.get("type") == "response_item" and value.get("type") in ("agent_message", "message"):
            role = value.get("role") or value.get("author")
            if isinstance(role, dict):
                role = role.get("role")
            content = value.get("content", [])
            if role in ("user", "assistant") and isinstance(content, list):
                text = "\n".join(v["text"] for v in content if isinstance(v, dict) and isinstance(v.get("text"), str))
                if text:
                    messages.append({"role": role, "text": redact(text)})
    return {"kind": "untrusted_redacted_evidence", "receipt": receipt, "messages": messages[-6:], "coverage_complete": complete}


def batch(config, limit):
    auto = store(config)
    entries = pending(config)[:limit]
    if not entries:
        return {"pending": 0, "batch": None}
    identifier = str(uuid.uuid4())
    value = {"schema": 1, "id": identifier, "created_at": utc(), "receipts": [r["id"] for r in entries]}
    json_write(auto / "batches" / (identifier + ".json"), value)
    return value


def finish(config, batch_id, report):
    auto = store(config)
    identifier = str(uuid.UUID(batch_id))
    selected = json_read(absolute(auto / "batches" / (identifier + ".json")))
    report = absolute(report)
    if (auto / "reviews") not in report.parents:
        raise ObserverError("review receipt must be saved beneath automatic/reviews")
    summary = json_read(report)
    reviewed = summary.get("reviewed", [])
    if summary.get("batch_id") != identifier or not isinstance(reviewed, list) or not reviewed:
        raise ObserverError("missing review coverage")
    if any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in reviewed):
        raise ObserverError("malformed reviewed item")
    ids = [r.get("id") for r in reviewed]
    if len(set(ids)) != len(ids) or not set(ids) <= set(selected["receipts"]):
        raise ObserverError("review coverage exceeds selected batch")
    for row in reviewed:
        if row.get("disposition") not in ("observed", "no_action", "needs_evidence") or not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ObserverError("each reviewed item needs a disposition and reason")
    ids = [r["id"] for r in reviewed if r["disposition"] != "needs_evidence"]
    target = auto / "decisions" / (identifier + ".json")
    decision = {"schema": 1, "batch_id": identifier, "reviewed": ids, "report": str(report), "report_sha256": digest(report.read_bytes())}
    with lease(auto / ".review.lock"):
        if target.exists() and json_read(target) != decision:
            raise ObserverError("review acknowledgement already differs")
        if not target.exists():
            json_write(target, decision)
    return {"acknowledged": len(ids), "remaining": len(pending(config))}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("init", "hook", "queue"):
        sub.add_parser(name)
    create = sub.add_parser("batch")
    create.add_argument("--limit", type=int, choices=range(1, 21), default=10)
    show = sub.add_parser("evidence")
    show.add_argument("--id", required=True)
    done = sub.add_parser("finish")
    done.add_argument("--batch", required=True)
    done.add_argument("--report", required=True)
    args = p.parse_args()
    event, config = {}, {}
    try:
        if args.command == "hook":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ObserverError("hook payload exceeds bound")
            event = json.loads(raw)
        config = json_read(absolute(args.config))
        if args.command == "hook":
            result = hook(config, event)
        elif args.command == "init":
            result = initialize(config)
        elif args.command == "queue":
            result = {"pending": pending(config)}
        elif args.command == "batch":
            result = batch(config, args.limit)
        elif args.command == "evidence":
            result = evidence(config, args.id)
        else:
            result = finish(config, args.batch, args.report)
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=None if args.command == "hook" else 2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if args.command == "hook":
            # Ordinary tasks proceed; matched reviews fail closed before model use.
            try:
                json_write(absolute(config["state_root"]) / "automatic" / "last-error.json", {"at": utc(), "error": type(exc).__name__, "message": str(exc)[:300]})
            except Exception:
                pass
            if isinstance(event, dict) and review_prompt(event.get("prompt", "")):
                print(json.dumps({"continue": False, "stopReason": "Task Observer gate failed. Inspect automatic/last-error.json before retrying."}))
            return 0
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
