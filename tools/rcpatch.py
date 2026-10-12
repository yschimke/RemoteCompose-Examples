#!/usr/bin/env python3
"""rcpatch - reproducible fixes for documents whose generator is not in this repository.

    python3 tools/rcpatch.py              # apply every fix, refresh the catalog's byte fields
    python3 tools/rcpatch.py --check      # exit 1 if any document still needs a fix
    python3 tools/rcpatch.py --list       # the fixes and what they touch

`rcx add` runs the same fixes on every document it ingests, before it hashes or writes it, so
a re-import of a collection lands already fixed, and `rcx verify` fails while any document in
the corpus still needs one.

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
import fnmatch
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


# ── Operation walker ──────────────────────────────────────────────────────────
#
# Byte sizes of the operations pythonRcCreation emits, after the opcode byte, from each
# operation's read() in androidx remote-core 1.0.0-alpha19. Deliberately only that set: an
# opcode not listed here raises Skip, so a fix that needs to understand a document's structure
# can never act on a stream it has not parsed end to end.
FIXED_SIZE = {
    16: 8, 67: 8,                   # MODIFIER_WIDTH / HEIGHT: type, value
    42: 16, 46: 12, 47: 16,         # DRAW_RECT, DRAW_CIRCLE, DRAW_LINE
    51: 24, 56: 16, 152: 24,        # DRAW_ROUND_RECT, DRAW_OVAL, DRAW_ARC
    54: 16, 55: 36, 58: 16,         # MODIFIER_ROUNDED_CLIP_RECT, _BACKGROUND, _PADDING
    107: 44, 223: 4,                # MODIFIER_BORDER, MODIFIER_ZINDEX
    126: 16, 127: 8, 129: 12,       # MATRIX_SCALE, MATRIX_TRANSLATE, MATRIX_ROTATE
    130: 0, 131: 0,                 # MATRIX_SAVE, MATRIX_RESTORE
    133: 24, 135: 16, 150: 12,      # DRAW_TEXT_ANCHOR, TEXT_FROM_FLOAT, COMPONENT_VALUE
    200: 4, 201: 4, 202: 16,        # LAYOUT_ROOT, LAYOUT_CONTENT, LAYOUT_BOX
    203: 20, 204: 20, 205: 8,       # LAYOUT_ROW, LAYOUT_COLUMN, LAYOUT_CANVAS
    176: 16, 240: 28,               # LAYOUT_FIT_BOX, LAYOUT_FLOW
    214: 0, 215: 16,                # CONTAINER_END, LOOP_START
}
PAINT_VALUES, ANIMATED_FLOAT, DATA_TEXT, CORE_TEXT = 40, 81, 102, 239
COMPONENT_VALUE, CONTAINER_END = 150, 214
LAYOUT_CONTENT, LAYOUT_CANVAS, LOOP_START, LAYOUT_FLOW = 201, 205, 215, 240
# Operations closed by a CONTAINER_END.
CONTAINERS = {200, 201, 202, 203, 204, 205, 176, 240, 215, CORE_TEXT}
# CoreText parameters (TextStyle.PARAMETERS): id -> payload size, None for an array.
CORE_TEXT_PARAMS = {i: 4 for i in range(1, 27)}
CORE_TEXT_PARAMS.update({18: 1, 19: 1, 22: 1, 20: None, 21: None})


class Op:
    __slots__ = ("code", "start", "end", "parent")

    def __init__(self, code, start, end, parent):
        self.code, self.start, self.end, self.parent = code, start, end, parent


def field(data: bytes, op: Op, k: int) -> int:
    """The k-th four-byte int after the operation's opcode."""
    return struct.unpack_from(">i", data, op.start + 1 + 4 * k)[0]


def walk(data: bytes, flow_size: int = 28) -> list[Op]:
    """Every operation in the document, with the innermost container that holds it.

    Raises Skip unless the whole stream parses to the last byte with balanced containers.
    `flow_size` reads LAYOUT_FLOW in its older five-field form (20 bytes) instead.
    """
    end, _ = header_tags(data)
    ops = [Op(0, 0, end, None)]
    stack: list[Op] = []
    off = end
    while off < len(data):
        code = data[off]
        p = off + 1
        if code == LAYOUT_FLOW:
            p += flow_size
        elif code in FIXED_SIZE:
            p += FIXED_SIZE[code]
        elif code == PAINT_VALUES:
            (n,) = struct.unpack_from(">i", data, p)
            if not 0 <= n <= 1024:
                raise Skip(f"paint length {n} at {off}")
            p += 4 + 4 * n
        elif code == DATA_TEXT:
            (n,) = struct.unpack_from(">i", data, p + 4)
            p += 8 + n
        elif code == ANIMATED_FLOAT:
            (word,) = struct.unpack_from(">i", data, p + 4)
            p += 8 + 4 * ((word & 0xFFFF) + (word >> 16))
        elif code == CORE_TEXT:
            (count,) = struct.unpack_from(">H", data, p + 4)
            p += 6
            for _ in range(count):
                pid = data[p]
                if pid not in CORE_TEXT_PARAMS:
                    raise Skip(f"unknown CoreText parameter {pid} at {p}")
                size = CORE_TEXT_PARAMS[pid]
                if size is None:
                    (n,) = struct.unpack_from(">H", data, p + 1)
                    size = 2 + 4 * n
                p += 1 + size
        else:
            raise Skip(f"operation {code} at {off} is outside the walker's table")
        if p > len(data):
            raise Skip(f"operation {code} at {off} runs past the end")
        op = Op(code, off, p, stack[-1] if stack else None)
        ops.append(op)
        if code == CONTAINER_END:
            if not stack:
                raise Skip(f"unmatched CONTAINER_END at {off}")
            stack.pop()
        elif code in CONTAINERS:
            stack.append(op)
        off = p
    if stack:
        raise Skip(f"{len(stack)} unclosed containers")
    return ops


def nan_id(v: int) -> bytes:
    """A variable reference inside an expression (Utils.asNan)."""
    return struct.pack(">I", (v | 0xFF800000) & 0xFFFFFFFF)


# ── Fixes ─────────────────────────────────────────────────────────────────────

ID_COMPONENT_WIDTH, ID_COMPONENT_HEIGHT = 7, 8      # RemoteContext
CV_WIDTH, CV_HEIGHT = 0, 1                          # ComponentValue
MAX_STATE_DATA = 10000                              # Limits


def fix_python_component_size(data: bytes) -> bytes:
    """pythonRcCreation compiles COMPONENT_WIDTH / COMPONENT_HEIGHT to the system variables
    7 and 8 (RemoteContext.ID_COMPONENT_WIDTH / _HEIGHT). No player ever writes those - not
    AndroidX, not the TypeScript player - so they read 0, and every canvas sized from them
    collapses into its top-left corner (demo-pressure-gauge, rc-players#630). AndroidX's own
    writer gets a canvas's size by binding ComponentValue WIDTH/HEIGHT of the component to a
    fresh variable (RemoteComposeBuffer.addComponentWidthValue); this does the same. For each
    canvas content component whose expressions read 7 or 8, it inserts the ComponentValue
    operations as that component's first children and points those reads at the bound ids."""
    ops = walk(data)
    refs = {nan_id(ID_COMPONENT_WIDTH): CV_WIDTH, nan_id(ID_COMPONENT_HEIGHT): CV_HEIGHT}
    used: set[int] = set()
    reads: list[tuple[Op, int, int]] = []           # (content component, float offset, kind)
    for op in ops:
        if op.code != ANIMATED_FLOAT:
            continue
        used.add(field(data, op, 0))
        (word,) = struct.unpack_from(">i", data, op.start + 5)
        for k in range(word & 0xFFFF):              # the expression; not its animation spec
            at = op.start + 9 + 4 * k
            v = data[at:at + 4]
            if v[:2] == b"\xff\x80" or v[:2] == b"\xff\x81":
                used.add(struct.unpack(">I", v)[0] & 0x7FFFFF)
            if v in refs:
                owner = op.parent
                if owner is None or owner.code != LAYOUT_CONTENT or \
                        owner.parent is None or owner.parent.code != LAYOUT_CANVAS:
                    raise Skip(f"component size read at {at} is not inside a canvas")
                reads.append((owner, at, refs[v]))
    if not reads:
        raise Skip("reads no component size")
    for op in ops:
        if op.code in (DATA_TEXT, 135):
            used.add(field(data, op, 0))
        elif op.code == LOOP_START:
            used.add(field(data, op, 0))
        elif op.code == COMPONENT_VALUE:
            used.add(field(data, op, 2))
    next_id = max([i for i in used if i < MAX_STATE_DATA] + [41]) + 1

    binding: dict[tuple[int, int], int] = {}        # (component start, kind) -> variable id
    for owner, _, kind in reads:
        if (owner.start, kind) not in binding:
            binding[(owner.start, kind)] = next_id
            next_id += 1
    if next_id > MAX_STATE_DATA:
        raise Skip("no free variable ids")

    out = bytearray(data)
    for owner, at, kind in reads:
        out[at:at + 4] = nan_id(binding[(owner.start, kind)])
    # Insert back to front so earlier offsets stay valid.
    by_owner: dict[int, Op] = {owner.start: owner for owner, _, _ in reads}
    for start in sorted(by_owner, reverse=True):
        owner = by_owner[start]
        component = struct.unpack_from(">i", data, owner.start + 1)[0]
        ins = b"".join(
            struct.pack(">Biii", COMPONENT_VALUE, kind, component, binding[(start, kind)])
            for kind in (CV_WIDTH, CV_HEIGHT) if (start, kind) in binding)
        out[owner.end:owner.end] = ins
    return bytes(out)


INT_MAX = 0x7FFFFFFF


def fix_python_flow(data: bytes) -> bytes:
    """pythonRcCreation writes LAYOUT_FLOW (240) in an older five-field form - component id,
    animation id, horizontal and vertical positioning, spacedBy - the same bytes as
    androidx-demos/c-flow. remote-core alpha19 reads seven fields, adding maxItemsInEachRow and
    maxLines, so it reads the next operation's bytes as those two and loses the stream
    (`Invalid table size`, `Unknown operation encountered 255`), and the TypeScript player,
    which reads seven as well, draws nothing. This appends both fields with the values
    FlowLayout uses when they are not given, Integer.MAX_VALUE, which is what the five-field
    form meant: no limit. A document is changed only if it parses completely with the older
    form and not with the current one."""
    def parses(flow_size: int) -> list[Op] | None:
        try:
            return walk(data, flow_size)
        except (Skip, struct.error, IndexError):
            return None

    if parses(28) is not None:
        raise Skip("already parses with the current LAYOUT_FLOW")
    ops = parses(20)
    if ops is None:
        raise Skip("does not parse with the older LAYOUT_FLOW either")
    out = bytearray(data)
    for op in reversed([o for o in ops if o.code == LAYOUT_FLOW]):
        out[op.end:op.end] = struct.pack(">ii", INT_MAX, INT_MAX)
    walk(bytes(out))
    return bytes(out)


FIXES = {
    "python-profiles": ("python-samples/*", fix_python_profiles),
    "python-component-size": ("python-samples/*", fix_python_component_size),
    "python-flow": ("python-samples/*", fix_python_flow),
}


def patch_document(did: str, data: bytes,
                   fixes: dict | None = None) -> tuple[bytes, list[str]]:
    """Apply every fix (of `fixes`, default all) whose pattern matches the document id `did`.
    Returns the new bytes and the names of the fixes that changed them, in the order they ran.

    Fixes run in FIXES order, repeated until none changes anything, so a fix that can only
    parse a document once another has run still gets its turn whatever the order. Every fix is
    idempotent, so this always stops; the bound only turns a broken fix into an error rather
    than a hang.
    """
    fixes = FIXES if fixes is None else fixes
    applied: list[str] = []
    for _ in range(len(fixes) + 1):
        changed = False
        for name, (pattern, fn) in fixes.items():
            if not fnmatch.fnmatchcase(did, pattern):
                continue
            try:
                out = fn(data)
            except Skip:
                continue
            if out != data:
                data, changed = out, True
                applied.append(name)
        if not changed:
            return data, applied
    raise SystemExit(f"{did}: fixes did not settle: {', '.join(applied)}")


def pending() -> dict[str, list[str]]:
    """Document id -> the fixes it still needs, for every document in the corpus that needs any."""
    out = {}
    for rc in sorted(DOCS.glob("*/*/doc.rc")):
        did = f"{rc.parent.parent.name}/{rc.parent.name}"
        _, applied = patch_document(did, rc.read_bytes())
        if applied:
            out[did] = applied
    return out


# ── Catalog refresh ───────────────────────────────────────────────────────────

def _rewrite_json(path: Path, fn) -> None:
    obj = json.loads(path.read_text())
    if fn(obj):
        path.write_text(json.dumps(obj, indent=2) + "\n")


def op_counts(data: bytes) -> dict[int, int] | None:
    """opcode -> count, or None when the walker cannot parse the document."""
    try:
        ops = walk(data)
    except (Skip, struct.error, IndexError):
        return None
    out: dict[int, int] = {}
    for op in ops:
        out[op.code] = out.get(op.code, 0) + 1
    return out


def refresh_catalog(changed: dict[str, bytes], original: dict[str, bytes]) -> None:
    """Carry each changed document's new bytes into everything the catalog records about it.

    Always sha256 and size. When a fix adds or removes operations, also the derived histogram
    and operation counts, and catalog/by-op.json, which orders its examples by operation count.
    Operation names come from by-op.json's own opcode column, so this needs neither rc2json
    nor Operations.java. A fix that would change a document's `apis` or `flags` needs the
    facet index rebuilt as well, which only `rcx catalog` does, so that is refused.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    from rcx import api_tags

    by_op_path = CATALOG / "by-op.json"
    by_op = json.loads(by_op_path.read_text()) if by_op_path.exists() else {}
    names = {v["opcode"]: k for k, v in by_op.items() if v.get("opcode") is not None}
    ops_changed = False
    rows: dict[str, dict] = {}

    for did, data in changed.items():
        sha = hashlib.sha256(data).hexdigest()
        d = DOCS / did

        def entry(e):
            if "sha256" in e:
                e["sha256"] = sha
                return True
            return False

        def derived(o):
            nonlocal ops_changed
            o["sha256"], o["bytes"] = sha, len(data)
            before, after = op_counts(original[did]), op_counts(data)
            if o.get("decoded") and before is not None and after is not None and before != after:
                hist = dict(o["histogram"])
                for code in set(before) | set(after):
                    delta = after.get(code, 0) - before.get(code, 0)
                    if not delta:
                        continue
                    if code not in names:
                        raise SystemExit(f"{did}: no catalog name for opcode {code}")
                    hist[names[code]] = hist.get(names[code], 0) + delta
                    if hist[names[code]] <= 0:
                        del hist[names[code]]
                if sorted(api_tags(hist)) != sorted(set(api_tags(o["histogram"]))):
                    raise SystemExit(f"{did}: apis would change; run `rcx catalog` instead")
                o["histogram"] = hist
                o["ops"], o["distinctOps"] = sum(hist.values()), len(hist)
                ops_changed = True
            rows[did] = o
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
                if row["id"] in rows:
                    row["ops"] = rows[row["id"]]["ops"]
                line = json.dumps(row, separators=(",", ":"))
            lines.append(line)
        jl.write_text("\n".join(lines) + "\n")

    for shard in sorted(CATALOG.glob("docs-*.json")):
        shard_rows = json.loads(shard.read_text())
        hit = False
        for row in shard_rows:
            if row["id"] in changed:
                row["b"] = len(changed[row["id"]])
                if row["id"] in rows:
                    row["o"] = rows[row["id"]]["ops"]
                hit = True
        if hit:
            shard.write_text(json.dumps(shard_rows, separators=(",", ":")) + "\n")

    if ops_changed and by_op:
        # The same computation as rcx's write_op_index, over every derived.json.
        usage: dict[str, list[tuple[int, str]]] = {}
        for dp in sorted(DOCS.glob("*/*/derived.json")):
            o = json.loads(dp.read_text())
            if not o.get("decoded"):
                continue
            for op in o["histogram"]:
                usage.setdefault(op, []).append((o["ops"], o["id"]))
        out = {}
        for op, lst in sorted(usage.items()):
            lst.sort()
            out[op] = {"opcode": by_op.get(op, {}).get("opcode"), "documents": len(lst),
                       "examples": [i for _, i in lst[:25]]}
        by_op_path.write_text(json.dumps(out, separators=(",", ":")) + "\n")


# ── Driver ────────────────────────────────────────────────────────────────────

def run(names: list[str], check: bool) -> int:
    selected = {n: FIXES[n] for n in names}
    changed: dict[str, bytes] = {}
    counts = {n: 0 for n in names}
    for rc in sorted(DOCS.glob("*/*/doc.rc")):
        did = f"{rc.parent.parent.name}/{rc.parent.name}"
        if not any(fnmatch.fnmatchcase(did, p) for p, _ in selected.values()):
            continue
        data = rc.read_bytes()
        out, applied = patch_document(did, data, selected)
        for n in applied:
            counts[n] += 1
        if out != data:
            changed[did] = out
    verb = "need" if check else "fixed"
    for name in names:
        print(f"  {name}: {counts[name]} {verb}")
    if check:
        return 1 if changed else 0
    original = {did: (DOCS / did / "doc.rc").read_bytes() for did in changed}
    for did, data in changed.items():
        (DOCS / did / "doc.rc").write_bytes(data)
    refresh_catalog(changed, original)
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
