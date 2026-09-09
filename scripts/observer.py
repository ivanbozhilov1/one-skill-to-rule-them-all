#!/usr/bin/env python3
"""Portable observation and staged review CLI. Writes require explicit commands.

No network, schedules, secret-store access or automatic skill activation.
All state uses a caller-pinned absolute --root. See references/storage.md.
"""
from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import uuid

sys.dont_write_bytecode = True

from observerlib import (ObserverError, absolute, digest, json_read, json_write, lease,
                         merge_trees, metadata, record_bytes, records, tree, tree_hash,
                         validate_record, write_tree, atomic_write, publish_directory)


def now():
    return datetime.now(timezone.utc).isoformat()


def workspace(value, create=False):
    root = absolute(value)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        with lease(root / ".init.lock"):
            config = root / "workspace.json"
            absolute(config)
            if config.exists() and json_read(config) != {"schema": 1, "root": str(root)}:
                raise ObserverError("existing workspace identity does not match")
            for folder in ("observation-log", "observation-log/archive", "skill-updates", "locks"):
                absolute(root / folder)
                (root / folder).mkdir(parents=True, exist_ok=True)
            if not config.exists():
                json_write(config, {"schema": 1, "root": str(root)})
    if not (root / "workspace.json").is_file():
        raise ObserverError("workspace missing or uninitialized; use init only for intended storage")
    if json_read(root / "workspace.json") != {"schema": 1, "root": str(root)}:
        raise ObserverError("workspace identity mismatch")
    for folder in ("observation-log", "observation-log/archive", "skill-updates", "locks"):
        absolute(root / folder)
        if not (root / folder).is_dir():
            raise ObserverError(f"workspace directory missing: {folder}")
    return root


def skill_name(files):
    fields, _ = metadata(files["SKILL.md"].decode("utf-8"))
    name = fields.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ObserverError("invalid skill name")
    return name


def manifest(root, identifier):
    try:
        identifier = str(uuid.UUID(identifier))
    except ValueError as exc:
        raise ObserverError("stage id must be a UUID") from exc
    stage = root / "skill-updates" / identifier
    absolute(stage)
    state = json_read(stage / "manifest.json")
    if state.get("id") != identifier or state.get("schema") != 1 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", state.get("skill", "")):
        raise ObserverError("stage identity mismatch")
    return stage, state


def validate_bundle(directory):
    validator = Path(__file__).with_name("validate-skill-bundle.py")
    spec = importlib.util.spec_from_file_location("observer_bundle_validator", validator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failures = []
    context = module.check_dir(directory, failures)
    if context and set(tree(directory)) != set(context["files"]):
        failures.append("installation tree contains files excluded by runtime packaging; reconcile extras explicitly")
    if failures or not context:
        raise ObserverError("candidate validation failed: " + "; ".join(failures))


def reviewed_records(log, state):
    current = {str(v["record"]["id"]): v for v in records(log)}
    for identifier, expected in state["observations"].items():
        item = current.get(identifier)
        if not item or item["sha256"] != expected or item["record"].get("status", "open") != "open":
            raise ObserverError("observation disposition changed; review a new stage")
    return current


def operation(args):
    root = workspace(args.root, args.command == "init")
    log = root / "observation-log"
    if args.command == "init":
        return {"root": str(root), "initialized": True}
    if args.command == "scan":
        values = records(log)
        selected = [v for v in values if (args.status == "all" or v["record"].get("status", "open") == args.status) and (not args.skill or args.skill in v["record"]["skill"]) and not (args.status == "open" and args.skill and args.skill in v["record"].get("installed_receipts", {}))]
        if not args.bodies:
            selected = [{k: v for k, v in item.items() if k != "body"} for item in selected]
        return {"kind": "untrusted_evidence", "files": len(values), "selected": len(selected), "observations": selected}
    if args.command == "add":
        record = json_read(absolute(args.record))
        if not isinstance(record, dict):
            raise ObserverError("record input must be a JSON object")
        body = record.pop("body", "")
        record.pop("installed_receipts", None)
        record.pop("installed_receipt", None)
        if not isinstance(body, str):
            raise ObserverError("body must be a string")
        record.update(id=str(uuid.uuid4()), status="open", captured_at=now(), capture_authority=args.authority)
        record.setdefault("scope", "internal")
        if not isinstance(record.get("evidence"), list) or not record["evidence"] or not all(isinstance(v, str) and v.strip() for v in record["evidence"]):
            raise ObserverError("provide nonempty evidence references")
        validate_record(record)
        target = log / (record["id"] + ".md")
        # The publication rename exposes a complete immutable record; UUID avoids allocation races.
        temporary = log / ("." + record["id"] + ".pending")
        with temporary.open("xb") as stream:
            stream.write(record_bytes(record, body))
            stream.flush()
            os.fsync(stream.fileno())
        with lease(root / "locks" / "observations.lock"):
            if target.exists():
                raise ObserverError("UUID collision; capture again")
            os.link(temporary, target)  # Atomic no-replace file publication.
            temporary.unlink()
        return {"id": record["id"], "path": str(target), "status": "open"}
    if args.command == "status":
        with lease(root / "locks" / "observations.lock"):
            matches = [v for v in records(log) if str(v["record"]["id"]) == args.id]
            if len(matches) != 1:
                raise ObserverError("observation not found")
            item = matches[0]
            if item["sha256"] != args.expected_hash:
                raise ObserverError("observation changed since review")
            item["record"].update(status=args.status, disposition=args.authority, updated_at=now())
            if args.status == "open":
                for field in ("installed_receipts", "installed_receipt", "resolved", "parked_until"):
                    item["record"].pop(field, None)
            if args.status == "parked":
                if not args.until:
                    raise ObserverError("parked status needs --until")
                item["record"]["parked_until"] = args.until
            atomic_write(Path(item["path"]), record_bytes(item["record"], item["body"]))
        return {"id": args.id, "status": args.status}
    if args.command == "stage":
        live = absolute(args.live)
        if root == live or root in live.parents or live in root.parents:
            raise ObserverError("live skill and observation workspace must be separate")
        files = tree(live)
        validate_bundle(live)
        name = skill_name(files)
        requested = set(args.observation)
        selected = [v for v in records(log) if str(v["record"]["id"]) in requested]
        if len(selected) != len(requested) or any(v["record"].get("status", "open") != "open" or name not in v["record"]["skill"] or name in v["record"].get("installed_receipts", {}) for v in selected):
            raise ObserverError("stage observations must be OPEN and match the exact target skill")
        identifier = str(uuid.uuid4())
        stage = root / "skill-updates" / identifier
        stage.mkdir()
        write_tree(stage / "base", files)
        write_tree(stage / "candidate", files)
        state = {"schema": 1, "id": identifier, "skill": name, "live": str(live), "base_hash": tree_hash(files), "state": "draft", "created": now(), "observations": {str(v["record"]["id"]): v["sha256"] for v in selected}}
        json_write(stage / "manifest.json", state)
        return {"id": identifier, "candidate": str(stage / "candidate"), "state": "draft"}
    if args.command == "pending":
        entries = []
        for directory in sorted((root / "skill-updates").iterdir()):
            if not directory.is_dir():
                continue
            if not (directory / "manifest.json").exists():
                entries.append({"id": directory.name, "state": "incomplete", "needs_recovery": True})
                continue
            _, state = manifest(root, directory.name)
            if state["state"] != "installed" or set(state["observations"]) != set(state.get("observations_resolved", [])):
                entries.append(state)
        return {"pending": entries}
    stage, state = manifest(root, args.id)
    live = absolute(state["live"])
    with lease(root / "locks" / (state["skill"] + ".lock")), lease(root / "locks" / "observations.lock"):
        stage, state = manifest(root, args.id)
        if args.command == "prepare":
            if state["state"] != "draft":
                raise ObserverError("candidate is immutable after preparation; start another stage")
            base, candidate, current = tree(stage / "base"), tree(stage / "candidate"), tree(live)
            if tree_hash(base) != state["base_hash"]:
                raise ObserverError("baseline changed; stage cannot be trusted")
            reviewed_records(log, state)
            merged = merge_trees(base, current, candidate)
            if skill_name(merged) != state["skill"]:
                raise ObserverError("candidate cannot rename the target skill")
            prepared = stage / "prepared"
            if prepared.exists():
                raise ObserverError("interrupted preparation; preserve it and create a new stage")
            write_tree(prepared, merged)
            validate_bundle(prepared)
            state.update(state="prepared", prepared_hash=tree_hash(merged), live_hash=tree_hash(current), authority=args.authority, prepared_at=now())
            json_write(stage / "manifest.json", state)
            return {"id": args.id, "state": "prepared", "path": str(prepared), "live_hash": state["live_hash"], "prepared_hash": state["prepared_hash"]}
        if state["state"] != "prepared":
            raise ObserverError("install requires a prepared candidate")
        reviewed_records(log, state)
        prepared = tree(stage / "prepared")
        if tree_hash(prepared) != state["prepared_hash"]:
            raise ObserverError("prepared candidate changed after review")
        if tree_hash(tree(live)) != state["live_hash"]:
            raise ObserverError("live changed after preparation; start a new stage and merge")
        validate_bundle(stage / "prepared")
        incoming = live.with_name(f".{live.name}.{state['id']}.incoming")
        backup = stage / "backup"
        if live.stat().st_dev != stage.stat().st_dev:
            raise ObserverError("install requires observation workspace and live skill on the same filesystem")
        if incoming.exists() or backup.exists():
            raise ObserverError("interrupted installation artifacts exist; inspect before recovery")
        write_tree(incoming, prepared)
        reviewed_records(log, state)
        state.update(state="installing", backup=str(backup), incoming=str(incoming), install_authority=args.authority)
        json_write(stage / "manifest.json", state)
        publish_directory(live, backup)
        try:
            if tree_hash(tree(backup)) != state["live_hash"]:
                raise ObserverError("live changed during installation")
            publish_directory(incoming, live)
            if tree_hash(tree(live)) != state["prepared_hash"]:
                raise ObserverError("installed tree verification failed")
        except Exception:
            # Preserve unexpected contents rather than replacing a concurrent writer.
            if not live.exists():
                publish_directory(backup, live)
            raise
        state.update(state="installed", installed_at=now(), installed_hash=state["prepared_hash"])
        json_write(stage / "manifest.json", state)
        resolved, unresolved = [], []
        # A verified install receipt is the prerequisite for resolving observations.
        # A concurrent disposition is preserved; installation remains honestly reported.
        try:
            current_records = {str(v["record"]["id"]): v for v in records(log)}
            for identifier, expected in state["observations"].items():
                item = current_records.get(identifier)
                if not item or item["sha256"] != expected:
                    unresolved.append(identifier)
                    continue
                receipts = item["record"].setdefault("installed_receipts", {})
                receipts[state["skill"]] = state["id"]
                if all(skill in receipts for skill in item["record"]["skill"]):
                    item["record"].update(status="actioned", resolved=now()[:10])
                atomic_write(Path(item["path"]), record_bytes(item["record"], item["body"]))
                resolved.append(identifier)
        except (OSError, ValueError):
            unresolved = [identifier for identifier in state["observations"] if identifier not in resolved]
        state.update(observations_resolved=resolved, observations_needing_reconciliation=unresolved)
        json_write(stage / "manifest.json", state)
        return {"id": args.id, "state": "installed", "live": str(live), "backup": str(backup), "sha256": state["installed_hash"], "observations_resolved": resolved, "observations_needing_reconciliation": unresolved}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, help="Pinned absolute observation workspace; never a skills discovery directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    scan = sub.add_parser("scan")
    scan.add_argument("--skill")
    scan.add_argument("--status", choices=["all", "open", "actioned", "declined", "superseded", "parked"], default="open")
    scan.add_argument("--bodies", action="store_true")
    add = sub.add_parser("add")
    add.add_argument("--record", required=True)
    add.add_argument("--authority", required=True, help="Existing user authorization to capture observations")
    status = sub.add_parser("status")
    status.add_argument("--id", required=True)
    status.add_argument("--status", required=True, choices=["open", "declined", "superseded", "parked"])
    status.add_argument("--expected-hash", required=True)
    status.add_argument("--authority", required=True)
    status.add_argument("--until")
    stage = sub.add_parser("stage")
    stage.add_argument("--live", required=True)
    stage.add_argument("--observation", action="append", default=[])
    sub.add_parser("pending")
    for name in ("prepare", "install"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--id", required=True)
        cmd.add_argument("--authority", required=True, help="Record existing review/install authorization; the flag does not grant it")
    return p


def main():
    try:
        result = operation(parser().parse_args())
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except (ObserverError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
