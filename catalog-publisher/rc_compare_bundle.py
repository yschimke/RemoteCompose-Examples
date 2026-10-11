#!/usr/bin/env python3
"""Feed the published catalog to the Remote Compose player comparison and install its output.

The comparison itself is `rc-compare.mjs` from `@design-parity/export-driver`. This script owns
the three pieces specific to this delivery branch:

  key      decide whether the comparison is current (the cache key) and whether the corpus
           is complete enough to compare at all
  bundle   write the zip `rc-compare.mjs` reads: `ir/<id>.rc` + `previews/<id>.png`
  install  replace the comparison files at the delivery branch root with a fresh run's output

Every id is the served id: the catalog image path without `images/` and `.png`, with `/` as
`__` (`images/advanced-samples/2-vtext.png` -> `advanced-samples__2-vtext`). `rc-compare.mjs`
takes its row ids from the bundle's file names, so flattening here is what makes
`rc-compare-summary.json` and the lane directories line up with the ids the server serves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

# Bump when the bundle layout or the set of comparison inputs changes, to force one regeneration.
COMPARE_VERSION = 1
KEY_FILE = "rc-compare-key.txt"
SUMMARY_FILE = "rc-compare-summary.json"
PAGE_FILE = "rc-compare.html"
# Every directory rc-compare.mjs writes. The names are frozen: the server reads them.
LANE_DIRS = (
    "rc", "rc-baked", "rc-diff",
    "rc-embedded", "rc-embedded-diff",
    "rc-androidx-embedded", "rc-androidx-embedded-diff",
    "rc-embedded-jvm", "rc-embedded-jvm-diff",
    "rc-cmp-wasm", "rc-cmp-wasm-diff", "rc-cmp-wasm-errors",
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def served_id(image_path: str) -> str:
    path = PurePosixPath(image_path)
    if (path.is_absolute() or any(part in ("", ".", "..") for part in path.parts)
            or path.parts[0] != "images" or path.suffix != ".png" or len(path.parts) < 2):
        raise ValueError(f"unexpected catalog image path: {image_path!r}")
    return "__".join(path.with_suffix("").parts[1:])


def entries(output: Path) -> list[tuple[str, Path, Path]]:
    """(served id, .rc file, baked PNG) for every published document that has an image."""
    manifest = json.loads((output / "catalog.json").read_text())
    found: dict[str, tuple[Path, Path]] = {}
    for component in manifest["components"]:
        rc = PurePosixPath(component["rcDocument"])
        if rc.is_absolute() or ".." in rc.parts or rc.parts[0] != "documents":
            raise ValueError(f"unexpected .rc path: {rc}")
        for image in component["images"]:
            sid = served_id(image["path"])
            if sid in found:
                raise ValueError(f"served id collision: {sid}")
            png, doc = output / image["path"], output / rc
            if not doc.is_file() or not png.is_file() or not png.read_bytes().startswith(PNG_MAGIC):
                raise ValueError(f"published document or image is missing for {sid}")
            found[sid] = (doc, png)
    return [(sid, *found[sid]) for sid in sorted(found)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_key(output: Path, inputs: list[str]) -> str:
    """sha256 over every compared document and baked image, the baked player and the toolchain."""
    manifest = json.loads((output / "catalog.json").read_text())
    digest = hashlib.sha256()
    digest.update(f"compare-version {COMPARE_VERSION}\n".encode())
    digest.update(f"renderer {manifest.get('renderer', '')}\n".encode())
    for value in sorted(inputs):
        digest.update(f"input {value}\n".encode())
    for sid, doc, png in entries(output):
        digest.update(f"doc {sid} {sha256(doc)} {sha256(png)}\n".encode())
    return digest.hexdigest()


def cmd_key(args: argparse.Namespace) -> int:
    output = args.output
    key = compare_key(output, args.input)
    progress_path = output / "publish-progress.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    stored = (output / KEY_FILE).read_text().strip() if (output / KEY_FILE).exists() else ""
    if progress.get("remaining") != 0:
        run, reason = False, (f"the publish has not finished the corpus "
                              f"(remaining={progress.get('remaining')!r}); comparing a partial set "
                              f"would be overwritten by the next run")
    elif stored == key:
        run, reason = False, f"{KEY_FILE} already matches {key[:16]}; nothing changed"
    else:
        run, reason = True, f"comparison key {key[:16]} differs from {stored[:16] or 'none'}"
    print(json.dumps({"run": run, "key": key, "stored": stored, "reason": reason}))
    if args.github_output:
        with open(args.github_output, "a") as handle:
            handle.write(f"run={'true' if run else 'false'}\nkey={key}\n")
        if not run:
            print(f"::notice title=Player comparison skipped::{reason}")
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    items = entries(args.output)
    if args.limit:
        items = items[:args.limit]
    args.bundle.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for sid, doc, png in items:
            archive.write(doc, f"ir/{sid}.rc")
            # PNGs are already deflated; storing them avoids paying for a second pass.
            archive.write(png, f"previews/{sid}.png", compress_type=zipfile.ZIP_STORED)
    print(json.dumps({"bundle": str(args.bundle), "documents": len(items)}))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    source, output = args.compare, args.output
    summary = source / SUMMARY_FILE
    if not summary.is_file():
        print(f"no {SUMMARY_FILE} in {source}; nothing to install", file=sys.stderr)
        return 1
    rows = json.loads(summary.read_text()).get("rows", [])
    if not rows:
        print(f"{SUMMARY_FILE} has no rows; refusing to replace the published comparison",
              file=sys.stderr)
        return 1
    # Replace, never merge: a lane dropped this run must not leave last run's column behind.
    for name in LANE_DIRS:
        shutil.rmtree(output / name, ignore_errors=True)
        if (source / name).is_dir() and any((source / name).iterdir()):
            shutil.copytree(source / name, output / name)
    shutil.copy2(summary, output / SUMMARY_FILE)
    if (source / PAGE_FILE).is_file():
        shutil.copy2(source / PAGE_FILE, output / PAGE_FILE)
    (output / KEY_FILE).write_text(args.key + "\n")
    lanes = sorted(name for name in LANE_DIRS if (output / name).is_dir())
    print(json.dumps({"rows": len(rows), "lanes": lanes}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    key = commands.add_parser("key", help="compute the cache key and decide whether to run")
    key.add_argument("--output", type=Path, required=True, help="delivery branch checkout")
    key.add_argument("--input", action="append", default=[],
                     help="a toolchain input such as driver=1.9.0; repeatable")
    key.add_argument("--github-output", help="append run/key/reason to this $GITHUB_OUTPUT file")
    key.set_defaults(func=cmd_key)

    bundle = commands.add_parser("bundle", help="write the rc-compare.mjs input zip")
    bundle.add_argument("--output", type=Path, required=True, help="delivery branch checkout")
    bundle.add_argument("--bundle", type=Path, required=True, help="zip to write")
    bundle.add_argument("--limit", type=int, default=0, help="first N documents only (testing)")
    bundle.set_defaults(func=cmd_bundle)

    install = commands.add_parser("install", help="copy a comparison into the delivery branch")
    install.add_argument("--compare", type=Path, required=True, help="rc-compare.mjs --out dir")
    install.add_argument("--output", type=Path, required=True, help="delivery branch checkout")
    install.add_argument("--key", required=True, help="the key printed by `key`")
    install.set_defaults(func=cmd_install)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
