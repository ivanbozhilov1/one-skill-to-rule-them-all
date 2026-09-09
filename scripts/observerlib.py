"""Task Observer's portable storage primitives. No network or runtime activation."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys
import uuid


class ObserverError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObserverError(f"duplicate metadata key: {key}")
        result[key] = value
    return result


def json_read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs)


def atomic_write(path: Path, data: bytes):
    """Replace one owned mutable file, never exposing partial contents."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_write(path: Path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def lease(path: Path):
    """Exclusive lease. A crash leaves a visible lock, never silently steals it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ObserverError(f"busy or interrupted operation; inspect lock before recovery: {path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "token": str(uuid.uuid4())}))
        yield
    finally:
        path.unlink()


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise ObserverError("use a pinned absolute path")
    for ancestor in (value, *value.parents):
        if linked(ancestor):
            raise ObserverError(f"symlink/junction path not supported: {ancestor}")
    return value.resolve()


def scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith(('"', "'")):
        try:
            if value.startswith('"'):
                parsed = json.loads(value)
            else:
                match = re.fullmatch(r"'((?:[^']|'')*)'", value)
                if not match:
                    raise ValueError("invalid single-quoted YAML")
                parsed = match.group(1).replace("''", "'")
        except (ValueError, SyntaxError) as exc:
            raise ObserverError("invalid quoted metadata string") from exc
        if not isinstance(parsed, str):
            raise ObserverError("metadata scalar must be a string")
        return parsed
    if value.startswith("["):
        try:
            result = json.loads(value)
        except ValueError:
            if not value.endswith("]"):
                raise ObserverError("unterminated metadata list")
            inner = value[1:-1].strip()
            if any(c in inner for c in "[]{}\"'"):
                raise ObserverError("use JSON for complex metadata lists")
            result = [scalar(part) for part in inner.split(",")] if inner else []
        if not isinstance(result, list) or not all(isinstance(v, str) for v in result):
            raise ObserverError("legacy metadata lists must contain strings")
        return result
    if value in ("null", "~"):
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    if value in ("true", "false"):
        return value == "true"
    if any(c in value for c in "{}[]&*!|>") or ": " in value or " #" in value:
        raise ObserverError("unsupported YAML scalar; convert metadata to a JSON object")
    return value


def metadata(text: str):
    """JSON frontmatter plus a strict flat legacy YAML subset; never guess."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ObserverError("missing opening frontmatter marker")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ObserverError("missing closing frontmatter marker") from exc
    header = "\n".join(lines[1:end])
    if header.lstrip().startswith("{"):
        result = json.loads(header, object_pairs_hook=object_pairs)
        if not isinstance(result, dict):
            raise ObserverError("metadata must be an object")
    else:
        result, pending = {}, None
        index = 1
        while index < end:
            line = lines[index]
            index += 1
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            block = re.fullmatch(r" +-[ ]+(.*)", line)
            if block and pending:
                value = scalar(block.group(1))
                if not isinstance(value, str):
                    raise ObserverError("list entries must be strings")
                result[pending].append(value)
                continue
            field = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):[ ]*(.*)", line)
            if not field:
                raise ObserverError("unsupported YAML structure; use JSON-object frontmatter")
            key, value = field.groups()
            if key in result:
                raise ObserverError(f"duplicate metadata key: {key}")
            if value in (">", ">-", ">+", "|", "|-", "|+"):
                block_lines = []
                while index < end and (lines[index].startswith(" ") or not lines[index]):
                    block_lines.append(lines[index])
                    index += 1
                nonempty = [part for part in block_lines if part.strip()]
                indent = len(nonempty[0]) - len(nonempty[0].lstrip(" ")) if nonempty else 0
                if any(len(part) - len(part.lstrip(" ")) < indent or "\t" in part[:indent] for part in nonempty):
                    raise ObserverError("inconsistent block indentation")
                cooked = [part[indent:] if part.strip() else "" for part in block_lines]
                if value.startswith(">") and any(part.startswith(" ") for part in cooked if part):
                    raise ObserverError("more-indented folded content is unsupported")
                content = "\n".join(cooked) + ("\n" if cooked else "")
                if value.startswith(">"):
                    content = re.sub(r"(?<=[^\n])(\n+)(?=[^\n])", lambda m: " " if len(m[1]) == 1 else "\n" * (len(m[1]) - 1), content)
                if value.endswith("-"):
                    content = content.rstrip("\n")
                elif not value.endswith("+"):
                    content = content.rstrip("\n") + ("\n" if nonempty else "")
                result[key] = content
                pending = None
                continue
            pending = key if not value else None
            result[key] = [] if pending else scalar(value)
    return result, "\n".join(lines[end + 1:]).lstrip("\n")


def record_bytes(record, body=""):
    return ("---\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n---\n\n" + body.rstrip() + "\n").encode("utf-8")


STATUSES = {"open", "actioned", "declined", "superseded", "parked"}


def validate_record(record):
    identifier = record.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) or not re.fullmatch(r"[A-Za-z0-9-]+", str(identifier)):
        raise ObserverError("id must be an integer or stable alphanumeric identifier")
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        raise ObserverError("title must be a nonempty string")
    if not isinstance(record.get("status", "open"), str) or record.get("status", "open") not in STATUSES:
        raise ObserverError("unknown observation status")
    skills = record.get("skill")
    if not isinstance(skills, list) or not all(isinstance(s, str) and s.strip() for s in skills):
        raise ObserverError("skill must be a list of exact nonempty identifiers")
    if not skills and not record.get("proposes_skill"):
        raise ObserverError("provide skill identifiers or proposes_skill")
    if "installed_receipts" in record and (not isinstance(record["installed_receipts"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in record["installed_receipts"].items())):
        raise ObserverError("installed_receipts must map skill names to receipt IDs")


def records(directory: Path):
    found, ids, errors = [], set(), []
    # iterdir surfaces permission errors; glob/rglob may suppress them on new Python.
    for path in sorted(p for p in directory.iterdir() if p.suffix.lower() == ".md"):
        try:
            if linked(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ObserverError("invalid or oversized observation file")
            data = path.read_bytes()
            record, body = metadata(data.decode("utf-8"))
            validate_record(record)
            identifier = str(record["id"])
            if identifier in ids:
                raise ObserverError(f"duplicate observation id: {identifier}")
            ids.add(identifier)
            found.append({"record": record, "body": body, "path": str(path), "sha256": digest(data)})
        except (ValueError, UnicodeError, OSError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    if errors:
        raise ObserverError(json.dumps({"files": len(found) + len(errors), "parsed": len(found), "rejected": errors}))
    return found


class Snapshot(dict):
    def __init__(self):
        super().__init__()
        self.modes = {}


def tree(path: Path):
    if not path.is_dir() or linked(path):
        raise ObserverError(f"expected a real directory: {path}")
    result = Snapshot()
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            item = Path(directory) / name
            if linked(item):
                raise ObserverError(f"linked tree member not supported: {item}")
            relative = item.relative_to(path).as_posix()
            if name in (".git", "__pycache__") or item.suffix == ".pyc":
                raise ObserverError(f"non-runtime tree member: {relative}")
            if item.is_file():
                result[relative] = item.read_bytes()
                if os.name == "posix":
                    result.modes[relative] = item.stat().st_mode & 0o777
    if "SKILL.md" not in result:
        raise ObserverError("skill tree has no SKILL.md")
    return result


def tree_hash(files):
    return digest(json.dumps({"files": {name: digest(data) for name, data in sorted(files.items())}, "modes": getattr(files, "modes", {})}, sort_keys=True).encode())


def write_tree(path: Path, files):
    path.mkdir()
    for relative, data in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if relative in getattr(files, "modes", {}):
            target.chmod(files.modes[relative])


def merge_text(base: bytes, live: bytes, candidate: bytes, name: str):
    try:
        original = base.decode("utf-8").splitlines(keepends=True)
        sides = [live.decode("utf-8").splitlines(keepends=True), candidate.decode("utf-8").splitlines(keepends=True)]
    except UnicodeError as exc:
        raise ObserverError(f"binary conflict: {name}") from exc
    changes = []
    for side in sides:
        local = [(i, j, side[a:b]) for tag, i, j, a, b in difflib.SequenceMatcher(a=original, b=side, autojunk=False).get_opcodes() if tag != "equal"]
        for current in local:
            if current in changes:
                continue
            i, j, replacement = current
            for x, y, _ in changes:
                # Insertions at an edit boundary are conservatively conflicting.
                overlap = max(i, x) < min(j, y) or (i == j and x <= i <= y) or (x == y and i <= x <= j)
                if overlap:
                    raise ObserverError(f"overlapping edits require resolution: {name}")
            changes.append(current)
    merged = original[:]
    for i, j, replacement in sorted(changes, key=lambda c: (c[0], c[1]), reverse=True):
        merged[i:j] = replacement
    return "".join(merged).encode("utf-8")


def merge_trees(base, live, candidate):
    merged = Snapshot()
    for name in sorted(base.keys() | live.keys() | candidate.keys()):
        b, l, c = base.get(name), live.get(name), candidate.get(name)
        bmode, lmode, cmode = (getattr(side, "modes", {}).get(name) for side in (base, live, candidate))
        if b is not None and ((l is None and c is not None and cmode != bmode) or (c is None and l is not None and lmode != bmode)):
            raise ObserverError(f"delete/permission conflict: {name}")
        if c == b:
            value = l
        elif l == b or l == c:
            value = c
        elif b is None or l is None or c is None:
            raise ObserverError(f"add/delete conflict: {name}")
        else:
            value = merge_text(b, l, c, name)
        if value is not None:
            merged[name] = value
            if cmode == bmode:
                mode = lmode
            elif lmode == bmode or lmode == cmode:
                mode = cmode
            else:
                raise ObserverError(f"file permission conflict: {name}")
            if mode is not None:
                merged.modes[name] = mode
    return merged


def publish_directory(source: Path, destination: Path):
    """Atomic no-replace directory rename; fail closed where not supported."""
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename:
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
                return
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    raise ObserverError("atomic no-replace directory publication is unsupported on this platform")
