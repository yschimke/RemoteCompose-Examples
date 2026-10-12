#!/usr/bin/env python3
"""rcpatch - reproducible fixes for documents whose generator is not in this repository.

    python3 tools/rcpatch.py              # apply every fix, refresh the catalog's byte fields
    python3 tools/rcpatch.py --check      # exit 1 if any document still needs a fix
    python3 tools/rcpatch.py --list       # the fixes and what they touch

Some collections were imported as compiled `.rc` from a generator that lives elsewhere -
python-samples comes from Origami's pythonRcCreation, whose JSON dialect (`header`/`ops`)
nothing in this repository compiles. When such a generator writes a wrong document the fix
belongs upstream, but the corpus cannot wait for a re-import, and editing bytes by hand is
how a document silently becomes something nobody can reproduce. So each fix is a named,
idempotent function here: re-running is a no-op, `--check` says whether a re-import has
brought the defect back, and the bytes change only through code that can be reviewed.

A fix never guesses. It parses what it edits, and if a document does not have exactly the
shape the fix expects it is reported and left untouched.

After patching, the catalog fields that depend only on the bytes - sha256 and size, in
entry.json, derived.json, corpus.jsonl and the docs-NNN.json shards - are refreshed in place.
Nothing else in the catalog changes, so this does not need `rcx catalog` (and its rc2json and
rcj dependencies) to keep the catalog consistent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CATALOG = ROOT / "catalog"

HEADER_MAGIC = 0x048C0000
DATA_TYPE_INT = 0

# Header tags (androidx.compose.remote.core.operations.Header).
DOC_PROFILES = 14

# androidx.compose.remote.core.RcProfiles
PROFILE_EXPERIMENTAL = 1
PROFILE_ANDROIDX = 512


class Skip(Exception):
    """The document does not have the shape this fix expects; leave it alone."""


# ── Header ────────────────────────────────────────────────────────────────────

def header_tags(data: bytes) -> tuple[int, dict[int, tuple[int, int, int]]]:
    """(offset just past the tag table, {tag: (dataType, payload offset, size)}).

    Only the v7 header (MAGIC|MAJOR, MINOR, PATCH, tag count, tags) has a tag table. A legacy
    v1.0/1.1 header has none, and nothing here can add one without rewriting the document.
    """
    if len(data) < 17 or data[0] != 0:
        raise Skip("no header op")
    magic, _minor, _patch, count = struct.unpack_from(">iiii", data, 1)
    if (magic & 0xFFFF0000) != HEADER_MAGIC:
        raise Skip("legacy header without a tag table")
    off, tags = 17, {}
    for _ in range(count):
        if off + 4 > len(data):
            raise Skip("truncated header")
        raw, size = struct.unpack_from(">HH", data, off)
        off += 4
        if off + size > len(data):
            raise Skip("truncated header tag")
        tags[raw & 0x3FF] = (raw >> 10, off, size)
        off += size
    return off, tags


def add_int_tag(data: bytes, tag: int, value: int) -> bytes:
    """Append an INT tag to the header's table. Raises Skip if the tag is already present."""
    end, tags = header_tags(data)
    if tag in tags:
        raise Skip(f"header already carries tag {tag}")
    (count,) = struct.unpack_from(">i", data, 13)
    entry = struct.pack(">HHi", tag | (DATA_TYPE_INT << 10), 4, value)
    return data[:13] + struct.pack(">i", count + 1) + data[17:end] + entry + data[end:]


# ── Fixes ─────────────────────────────────────────────────────────────────────

def fix_python_profiles(data: bytes) -> bytes:
    """pythonRcCreation writes no DOC_PROFILES, so AndroidX loads its documents as the
    baseline profile. Its operation map there has no CORE_TEXT (239) or LAYOUT_FLOW (240) -
    CORE_TEXT is registered only for PROFILE_ANDROIDX / WIDGETS and LAYOUT_FLOW only with
    PROFILE_EXPERIMENTAL (Operations.createMapV7) - and 36 of these documents fail to inflate
    with `Unknown operation encountered 239/240`. ANDROIDX|EXPERIMENTAL (513) is what the
    rcJson generator writes for every document it produces."""
    return add_int_tag(data, DOC_PROFILES, PROFILE_ANDROIDX | PROFILE_EXPERIMENTAL)


FIXES = {
    "python-profiles": ("python-samples/*", fix_python_profiles),
}


# ── Catalog refresh ───────────────────────────────────────────────────────────

def _rewrite_json(path: Path, fn) -> None:
    obj = json.loads(path.read_text())
    if fn(obj):
        path.write_text(json.dumps(obj, indent=2) + "\n")


def refresh_catalog(changed: dict[str, bytes]) -> None:
    """Update the sha256 and size of each changed document wherever the catalog records it."""
    for did, data in changed.items():
        sha = hashlib.sha256(data).hexdigest()
        d = DOCS / did

        def entry(e):
            if "sha256" in e:
                e["sha256"] = sha
                return True
            return False

        def derived(o):
            o["sha256"], o["bytes"] = sha, len(data)
            return True

        if (d / "entry.json").exists():
            _rewrite_json(d / "entry.json", entry)
        if (d / "derived.json").exists():
            _rewrite_json(d / "derived.json", derived)

    jl = CATALOG / "corpus.jsonl"
    if jl.exists():
        lines = []
        for line in jl.read_text().splitlines():
            row = json.loads(line)
            if row["id"] in changed:
                row["bytes"] = len(changed[row["id"]])
                line = json.dumps(row, separators=(",", ":"))
            lines.append(line)
        jl.write_text("\n".join(lines) + "\n")

    for shard in sorted(CATALOG.glob("docs-*.json")):
        rows = json.loads(shard.read_text())
        hit = False
        for row in rows:
            if row["id"] in changed:
                row["b"] = len(changed[row["id"]])
                hit = True
        if hit:
            shard.write_text(json.dumps(rows, separators=(",", ":")) + "\n")


# ── Driver ────────────────────────────────────────────────────────────────────

def run(names: list[str], check: bool) -> int:
    changed: dict[str, bytes] = {}
    pending = 0
    for name in names:
        pattern, fn = FIXES[name]
        fixed = skipped = 0
        for rc in sorted(DOCS.glob(f"{pattern}/doc.rc")):
            did = f"{rc.parent.parent.name}/{rc.parent.name}"
            data = changed.get(did) or rc.read_bytes()
            try:
                out = fn(data)
            except Skip:
                skipped += 1
                continue
            if out == data:
                continue
            fixed += 1
            changed[did] = out
        verb = "need" if check else "fixed"
        print(f"  {name}: {fixed} {verb}, {skipped} already fixed or not applicable")
        pending += fixed
    if check:
        return 1 if pending else 0
    for did, data in changed.items():
        (DOCS / did / "doc.rc").write_bytes(data)
    refresh_catalog(changed)
    print(f"  {len(changed)} documents rewritten")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="rcpatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fixes", nargs="*", help="fixes to run (default: all)")
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    ap.add_argument("--list", action="store_true", help="list the fixes")
    args = ap.parse_args()
    if args.list:
        for name, (pattern, fn) in FIXES.items():
            print(f"{name}  docs/{pattern}\n    {' '.join(fn.__doc__.split())}\n")
        return 0
    unknown = [n for n in args.fixes if n not in FIXES]
    if unknown:
        ap.error(f"unknown fix: {', '.join(unknown)}")
    return run(args.fixes or list(FIXES), args.check)


if __name__ == "__main__":
    sys.exit(main())
