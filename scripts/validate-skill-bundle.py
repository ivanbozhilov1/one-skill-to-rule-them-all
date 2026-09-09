#!/usr/bin/env python3
"""Validate a staged runtime tree and optionally pack/verify its .skill ZIP.

Usage: python validate-skill-bundle.py DIR [--pack OUT | --bundle EXISTING]
Only SKILL.md, LICENSE.txt, references/, scripts/ and agents/ are runtime
content. Repository documents, tests and build junk are never packaged.
No third-party dependencies. Frontmatter supports a strict, flat YAML string
mapping (plain/quoted scalars and |/> blocks); unsupported YAML is rejected.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import zipfile
import zlib

MAX_DESCRIPTION_CHARS = 1024
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PATH_RE = re.compile(r"\x60((?:references|scripts|assets|agents)/[^\x60\s*?]+\.[A-Za-z0-9]+)\x60")
ROOT_FILES = {"SKILL.md", "LICENSE.txt"}
RUNTIME_DIRS = {"references", "scripts", "agents"}
JUNK_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", "node_modules", "tests"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
RESIDUE_RES = [
    (re.compile(r"(?m)^\\[0-9]\s*$"), "literal regex backreference on its own line"),
    (re.compile(r"(?<![\w\x60])\\[1-9](?![\w])"), "literal regex backreference in prose"),
    (re.compile(r"(?m)^(<<<<<<<|=======|>>>>>>>)( |$)"), "merge conflict marker"),
    (re.compile(r"\{\{[A-Za-z_][A-Za-z0-9_ .-]*\}\}"), "unresolved template slot"),
    (re.compile(r"\b(TODO|FIXME|XXX)\b: ?(fill|replace|write)", re.I), "placeholder note left in"),
]
SECOND_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n\s*(---\n|name:|description:)", re.S)


def frontmatter(text):
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.S)
    return match.group(1) if match else None


def _scalar(raw):
    """Parse the deliberately limited string-only scalar subset, never guess."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except ValueError as exc:
            raise ValueError("unsupported or invalid double-quoted scalar") from exc
        if raw[end:].strip() and not raw[end:].lstrip().startswith("#"):
            raise ValueError("content after quoted scalar")
        return value
    if raw.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
        if not match:
            raise ValueError("invalid single-quoted scalar")
        return match.group(1).replace("''", "'")
    raw = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
    if (not raw or raw.startswith(("[", "{", "&", "*", "!", "|", ">", "%", "@", "\x60", "#"))
            or re.match(r"[-?:](?:\s|$)", raw) or ": " in raw
            or re.fullmatch(r"(?i:null|true|false|yes|no|on|off|~|\.nan|[+-]?\.inf)", raw)
            or re.match(r"^[+-]?(?:\d|\.\d)", raw)):
        raise ValueError("unsupported/non-string YAML scalar; quote string values")
    return raw


def parse_frontmatter(fm):
    """Flat YAML string mapping; reject nested/ambiguous/duplicate values.

    Folded blocks support uniform indentation. More-indented folded content
    and explicit indentation indicators are rejected instead of mismeasured.
    Literal blocks preserve embedded newlines and relative indentation.
    """
    lines = fm.split("\n")
    data = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)", line)
        if not match:
            raise ValueError("only a flat mapping with scalar values is supported")
        key, raw = match.groups()
        if key in data:
            raise ValueError("duplicate frontmatter key: " + key)
        block = re.fullmatch(r"([>|])([+-]?)(?:[ \t]+#.*)?", raw)
        if not block:
            data[key] = _scalar(raw)
            continue
        content = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
            content.append(lines[i])
            i += 1
        nonempty = [part for part in content if part.strip()]
        if any("\t" in part[:len(part) - len(part.lstrip())] for part in nonempty):
            raise ValueError("tabs in block indentation are unsupported")
        indent = len(nonempty[0]) - len(nonempty[0].lstrip(" ")) if nonempty else 0
        if any(len(part) - len(part.lstrip(" ")) < indent for part in nonempty):
            raise ValueError("inconsistent block indentation")
        cooked = [part[indent:] if part.strip() else "" for part in content]
        if block[1] == ">" and any(part.startswith(" ") for part in cooked if part):
            raise ValueError("more-indented folded block content is unsupported")
        value = "\n".join(cooked) + ("\n" if cooked else "")
        if block[1] == ">":
            value = re.sub(r"(?<=[^\n])(\n+)(?=[^\n])",
                           lambda m: " " if len(m[1]) == 1 else "\n" * (len(m[1]) - 1), value)
        if block[2] == "-":
            value = value.rstrip("\n")
        elif block[2] != "+":
            value = value.rstrip("\n") + ("\n" if nonempty else "")
        data[key] = value
    return data


def folded_description(fm):
    """Compatibility helper: return the parsed value, including literal blocks."""
    return parse_frontmatter(fm).get("description", "")


def _safe_member(name):
    if not name or "\\" in name or "\x00" in name or ":" in name or name.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _junk(path):
    return (path.name in JUNK_DIRS or path.name in JUNK_FILES
            or path.suffix.lower() in {".pyc", ".pyo"}
            or path.name.startswith(".~lock") or path.name.endswith("~"))


def runtime_files(root, fails):
    """Enumerate only the allowlisted runtime tree; never follow a symlink."""
    files = {}

    def visit(path):
        if path.is_symlink():
            fails.append("symlink in staged runtime tree: " + str(path.relative_to(root)))
            return
        if _junk(path):
            return
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            relative = path.relative_to(root).as_posix()
            if not _safe_member(relative):
                fails.append("unsafe staged runtime path: " + relative)
            else:
                files[relative] = path
        else:
            fails.append("unsupported runtime file type: " + str(path.relative_to(root)))

    for name in sorted(ROOT_FILES | RUNTIME_DIRS):
        path = root / name
        if path.exists() or path.is_symlink():
            if name in ROOT_FILES and not path.is_file() and not path.is_symlink():
                fails.append("runtime root file is not a regular file: " + name)
            elif name in RUNTIME_DIRS and not path.is_dir() and not path.is_symlink():
                fails.append("runtime directory is not a directory: " + name)
            else:
                visit(path)
    lowered = [name.casefold() for name in files]
    if len(lowered) != len(set(lowered)):
        fails.append("runtime paths collide on a case-insensitive filesystem")
    return files


def _digest(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(65536), b""):
        digest.update(chunk)
    return digest.hexdigest()


def check_dir(skill_dir, fails):
    source = pathlib.Path(skill_dir)
    if source.is_symlink():
        fails.append("staged directory must not be a symlink")
        return None
    root = source.resolve()
    if not root.is_dir():
        fails.append("staged directory missing")
        return None
    files = runtime_files(root, fails)
    if "SKILL.md" not in files:
        fails.append("SKILL.md missing")
        return None
    text = files["SKILL.md"].read_text(encoding="utf-8")
    fm = frontmatter(text)
    if fm is None:
        fails.append("frontmatter: no leading --- block")
        return None
    try:
        data = parse_frontmatter(fm)
    except ValueError as exc:
        fails.append("frontmatter: " + str(exc))
        return None
    name, desc = data.get("name"), data.get("description")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        fails.append("frontmatter: name must be a nonempty kebab-case string")
    if not isinstance(desc, str) or not desc.strip():
        fails.append("frontmatter: description must be a nonempty string")
    elif len(desc) > MAX_DESCRIPTION_CHARS:
        fails.append(f"description {len(desc)} chars > cap {MAX_DESCRIPTION_CHARS}")
    elif len(desc) > 900:
        print(f"warn: description {len(desc)} chars (cap {MAX_DESCRIPTION_CHARS}) — near the boundary")
    for relative in sorted(set(PATH_RE.findall(text))):
        if relative not in files:
            fails.append("cited path missing from staged runtime set: " + relative)
    if SECOND_FRONTMATTER_RE.match(text):
        fails.append("frontmatter: a second frontmatter block follows the first")
    for relative, path in files.items():
        if path.suffix.lower() in {".md", ".txt", ".yml", ".yaml", ".json"}:
            body = path.read_text(encoding="utf-8")
            prose = re.sub(r"(?ms)^\x60\x60\x60.*?^\x60\x60\x60[ \t]*$",
                           lambda m: re.sub(r"[^\n]", " ", m[0]), body)
            prose = re.sub(r"\x60[^\x60\n]*\x60", lambda m: " " * len(m[0]), prose)
            for pattern, why in RESIDUE_RES:
                match = pattern.search(prose)
                if match:
                    line = body.count("\n", 0, match.start()) + 1
                    fails.append(f"edit residue in {relative}:{line}: {why}")
    expected = {}
    for relative, path in files.items():
        with path.open("rb") as stream:
            expected[f"{name}/{relative}"] = (path.stat().st_size, _digest(stream))
    return {"root": root, "name": name, "files": files, "expected": expected}


def check_bundle(path, fails, skill_dir=None, *, context=None):
    """Require a real ZIP whose exact runtime member set and hashes match source."""
    if context is None:
        if skill_dir is None:
            fails.append("bundle: staged directory required for content verification")
            return
        context = check_dir(skill_dir, fails)
    if context is None:
        return
    try:
        with zipfile.ZipFile(path) as bundle:
            members = bundle.infolist()
            seen = set()
            portable_names = set()
            for info in members:
                name = info.orig_filename
                if not _safe_member(name) or name != info.filename:
                    fails.append("bundle: unsafe member path " + repr(name))
                if name in seen or name.casefold() in portable_names:
                    fails.append("bundle: duplicate member path " + repr(name))
                seen.add(name)
                portable_names.add(name.casefold())
                kind = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir() or kind not in (0, stat.S_IFREG):
                    fails.append("bundle: non-regular member (including symlink) " + repr(name))
                if info.flag_bits & 1:
                    fails.append("bundle: encrypted member " + repr(name))
            expected = context["expected"]
            for missing in sorted(set(expected) - seen):
                fails.append("bundle: missing runtime member " + missing)
            for extra in sorted(seen - set(expected)):
                fails.append("bundle: unexpected runtime member " + repr(extra))
            for info in members:
                if info.orig_filename not in expected:
                    continue
                size, digest = expected[info.orig_filename]
                if info.file_size != size:
                    fails.append("bundle: size differs from staged file " + info.orig_filename)
                    continue
                # Reading to EOF verifies local headers, decompression and CRC;
                # hashing also detects validly-repacked but changed payloads.
                with bundle.open(info) as stream:
                    actual = _digest(stream)
                if actual != digest:
                    fails.append("bundle: content hash differs from staged file " + info.orig_filename)
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile,
            zipfile.LargeZipFile, EOFError, zlib.error) as exc:
        fails.append("bundle: invalid or unreadable ZIP: " + str(exc))


def pack(src, out, *, context=None):
    """Pack the validated runtime set with a root from name, never cwd.name."""
    fails = []
    context = context or check_dir(src, fails)
    if fails or context is None:
        raise ValueError("; ".join(fails))
    destination = pathlib.Path(out)
    if destination.is_symlink():
        raise ValueError("bundle destination must not be a symlink")
    destination = destination.resolve()
    root = context["root"]
    if destination.is_relative_to(root):
        relative = destination.relative_to(root)
        if relative.parts and (relative.parts[0] in RUNTIME_DIRS or relative.as_posix() in ROOT_FILES):
            raise ValueError("bundle destination would include or overwrite runtime content")
    if destination.exists() and any(destination.samefile(p) for p in context["files"].values()):
        raise ValueError("bundle destination aliases a staged runtime file")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".skill-pack-", suffix=".tmp",
                                         dir=destination.parent, delete=False) as handle:
            temporary = pathlib.Path(handle.name)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
            for relative, path in sorted(context["files"].items()):
                if path.is_symlink():
                    raise ValueError("runtime file became a symlink: " + relative)
                bundle.write(path, arcname=f'{context["name"]}/{relative}')
        check_bundle(temporary, fails, context=context)
        if fails:
            raise ValueError("; ".join(fails))
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pack")
    mode.add_argument("--bundle")
    args = parser.parse_args(argv[1:])
    fails = []
    try:
        context = check_dir(args.skill_dir, fails)
        if args.pack and not fails:
            pack(args.skill_dir, args.pack, context=context)
            print("packed " + args.pack)
        elif args.bundle:
            check_bundle(args.bundle, fails, context=context, skill_dir=args.skill_dir)
    except (OSError, UnicodeError, ValueError) as exc:
        fails.append(str(exc))
    if fails:
        print("FAIL:")
        for failure in fails:
            print("  - " + failure)
        return 1
    print("OK: all gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
