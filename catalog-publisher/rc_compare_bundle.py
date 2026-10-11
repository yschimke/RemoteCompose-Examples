#!/usr/bin/env python3
"""Feed the published catalog to the Remote Compose player comparison and install its output.

The comparison itself is `rc-compare.mjs` from `@design-parity/export-driver`. This script owns
the three pieces specific to this delivery branch:

  key      decide whether the comparison is current (the cache key) and whether the corpus
           is complete enough to compare at all
  bundle   write the zip `rc-compare.mjs` reads: `ir/<id>.rc` + `previews/<id>.png`
  install  replace the comparison files at the delivery branch root with a fresh run's output
  failures record every document a player failed on (and renders that look grossly wrong) in
           `rc-compare-failures.json`, and summarise them for the job's step summary

Every id is the served id: the catalog image path without `images/` and `.png`, with `/` as
`__` (`images/advanced-samples/2-vtext.png` -> `advanced-samples__2-vtext`). `rc-compare.mjs`
takes its row ids from the bundle's file names, so flattening here is what makes
`rc-compare-summary.json` and the lane directories line up with the ids the server serves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# Bump when the bundle layout or the set of comparison inputs changes, to force one regeneration.
# 2: the first run with rc-compare-failures.json, so a current comparison regenerates once.
COMPARE_VERSION = 2
KEY_FILE = "rc-compare-key.txt"
SUMMARY_FILE = "rc-compare-summary.json"
PAGE_FILE = "rc-compare.html"
FAILURES_FILE = "rc-compare-failures.json"
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
    # `failures` rewrites it from this run; until then no record of last run's failures may stand
    # beside this run's comparison.
    (output / FAILURES_FILE).unlink(missing_ok=True)
    if (source / PAGE_FILE).is_file():
        shutil.copy2(source / PAGE_FILE, output / PAGE_FILE)
    # A lane that dropped its column this run (a harness crash, a flaky runner) must be retried, so
    # an incomplete comparison is published without the key: the next run sees no match and redoes
    # it. Any older key goes too, since it would name a complete comparison that is no longer there.
    dropped = []
    if args.dropped_lanes and args.dropped_lanes.is_file():
        dropped = [line for line in args.dropped_lanes.read_text().splitlines() if line.strip()]
    if dropped:
        (output / KEY_FILE).unlink(missing_ok=True)
        print(f"::notice::lanes dropped ({', '.join(dropped)}); not recording the cache key so "
              "the next run retries them")
    else:
        (output / KEY_FILE).write_text(args.key + "\n")
    lanes = sorted(name for name in LANE_DIRS if (output / name).is_dir())
    print(json.dumps({"rows": len(rows), "lanes": lanes}))
    return 0


# Every lane the comparison can run: the name used in rc-compare-failures.json, the field prefix
# rc-compare-summary.json uses for it ("" is the js lane's top-level fields), the summary section
# that is non-null when the lane ran, the harness output directory under $WORK holding its
# `<id>.error` files, and the label rc_compare_lanes.sh writes to `dropped-lanes`.
LANES = (
    ("js", "", None, None, None),
    ("embedded", "embedded", "embedded", "rc-embedded-out", "vendored embedded-player"),
    ("androidx-embedded", "androidxEmbedded", "androidxEmbedded", "rc-androidx-embedded-out",
     "upstream embedded-player"),
    ("cmp-jvm", "embeddedJvm", "embeddedJvm", "rc-embedded-jvm-out", "cmp-jvm player"),
    ("cmp-wasm", "cmpWasm", "cmpWasm", None, "cmp-wasm player"),
)
REASON_LIMIT = 300
# A number right after one of these words names *what* failed (an opcode the player lacks), so it
# is kept when grouping; every other number is a position, an index or a value and is dropped.
_KEPT_NUMBER = re.compile(r"(?i)(?:\bopcode|\bencountered|\bcommand)\s*[=:]?\s*$")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
# Suspect thresholds, in percentage points over the js lane on the same document. The js lane is
# the control: a lane is only suspect where it disagrees with the baked PNG grossly *and* the js
# player, which baked that PNG, does not. Tuned on the 1,309-document corpus, where both gaps are
# bimodal (almost nothing sits between 20 and 40 points of coverage).
SUSPECT_COVERAGE_POINTS = 40.0
SUSPECT_CONTENT_PCT = 90.0
SUSPECT_CONTENT_POINTS = 50.0


def first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= REASON_LIMIT else line[:REASON_LIMIT - 1] + "…"


def normalise_reason(reason: str, sid: str = "") -> str:
    """The grouping key for a failure: the same failure on two documents reads the same."""
    text = reason.replace(sid, "<id>") if sid else reason
    text = re.sub(r"\b0x[0-9a-fA-F]+\b", "0xN", text)
    text = re.sub(r"\b[0-9a-f]{12,}\b", "<hash>", text)

    def number(match: re.Match) -> str:
        return match.group(0) if _KEPT_NUMBER.search(text[:match.start()]) else "N"

    text = _NUMBER.sub(number, text)
    return re.sub(r"\s+", " ", text).strip()


def harness_errors(directory: Path | None) -> dict[str, str]:
    """A harness's full per-document reasons: `errors.txt` (id TAB reason) and `<id>.error`."""
    found: dict[str, str] = {}
    if not directory or not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.error")):
        found[path.name[:-len(".error")]] = path.read_text(errors="replace")
    # rc-compare.mjs prefers errors.txt over `<id>.error`; so does this.
    listing = directory / "errors.txt"
    if listing.is_file():
        for line in listing.read_text(errors="replace").splitlines():
            sid, _, reason = line.partition("\t")
            if sid and reason:
                found[sid] = reason
    return found


def suspect_reason(row: dict, prefix: str) -> tuple[str, str] | None:
    """(kind, reason) when a lane's render is grossly unlike the baked PNG and the js lane's."""
    if prefix == "" or row.get("referenceBlank") or not row.get("rendered"):
        return None
    coverage, content = row.get(f"{prefix}CoverageDeltaPct"), row.get(f"{prefix}ContentMismatchPct")
    js_coverage, js_content = row.get("coverageDeltaPct"), row.get("contentMismatchPct")
    if js_content is not None and content is None and coverage is not None:
        # Usually a blank canvas; sometimes a render so magnified or offset that nothing lands.
        return "blank", (f"nothing it painted overlaps the baked PNG's content "
                         f"({coverage:.1f}% of the canvas painted on one side only)")
    if coverage is not None and js_coverage is not None \
            and coverage - js_coverage >= SUSPECT_COVERAGE_POINTS:
        return "coverage", (f"painted area disagrees with the baked PNG on {coverage:.1f}% of the "
                            f"canvas (js lane {js_coverage:.1f}%); likely magnified, cropped or "
                            f"offset")
    if content is not None and js_content is not None and content >= SUSPECT_CONTENT_PCT \
            and content - js_content >= SUSPECT_CONTENT_POINTS:
        return "content", (f"colour disagrees on {content:.1f}% of the pixels both painted (js "
                           f"lane {js_content:.1f}%); likely magnified or drawn in the wrong "
                           f"colours")
    return None


def group_reasons(failures: list[dict]) -> list[dict]:
    groups: dict[str, list[str]] = {}
    for failure in failures:
        groups.setdefault(normalise_reason(failure["reason"], failure["id"]), []).append(
            failure["id"])
    return [{"reason": reason, "count": len(ids), "ids": sorted(ids)}
            for reason, ids in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))]


def collect_failures(summary: dict, work: Path | None, dropped: list[str], key: str,
                     key_recorded: bool, output: Path | None) -> dict:
    rows = summary.get("rows", [])
    lanes: dict[str, dict] = {}
    for name, prefix, section, harness_dir, label in LANES:
        lane_dropped = label is not None and label in dropped
        if section is not None and summary.get(section) is None and not lane_dropped:
            continue  # not requested this run
        errors = harness_errors(work / harness_dir if work and harness_dir else None)
        rendered_field = f"{prefix}Rendered" if prefix else "rendered"
        note_field = f"{prefix}Note" if prefix else "note"
        failures, suspects = [], []
        if lane_dropped:
            # The lane never reached rc-compare.mjs, so the summary has nothing for it: its
            # harness's own error files are the only record.
            failures = [{"id": sid, "reason": first_line(text)}
                        for sid, text in sorted(errors.items())]
            rendered = 0
        else:
            for row in rows:
                sid = row["id"]
                if row.get(rendered_field) is False:
                    # The summary note is cut at 200 characters; the harness file has it all.
                    raw = errors.get(sid) or row.get(note_field) or "no render and no reason given"
                    failures.append({"id": sid, "reason": first_line(raw)})
                elif row.get(rendered_field):
                    suspect = suspect_reason(row, prefix)
                    if suspect:
                        suspects.append({"id": sid, "kind": suspect[0], "reason": suspect[1]})
            rendered = (summary.get("rendered") if section is None
                        else summary[section].get("rendered"))
            if rendered is None:
                rendered = sum(1 for row in rows if row.get(rendered_field))
        entry = {"dropped": lane_dropped, "rendered": rendered, "failed": len(failures)}
        if section is not None:
            entry["suspect"] = len(suspects)
        entry["reasons"] = group_reasons(failures)
        entry["failures"] = failures
        if section is not None:
            kinds: dict[str, int] = {}
            for suspect in suspects:
                kinds[suspect["kind"]] = kinds.get(suspect["kind"], 0) + 1
            entry["suspectKinds"] = dict(sorted(kinds.items()))
            entry["suspects"] = suspects
        lanes[name] = entry
    for label in dropped:  # a label this script does not know still deserves a line
        if label not in {lane[4] for lane in LANES}:
            lanes[label] = {"dropped": True, "rendered": 0, "failed": 0, "reasons": [],
                            "failures": []}

    publish: dict = {}
    if output is not None:
        progress_path, failures_path = output / "publish-progress.json", output / "render-failures.json"
        if progress_path.is_file():
            progress = json.loads(progress_path.read_text())
            publish.update(source=progress.get("source"), documents=progress.get("documents"),
                           images=progress.get("images"))
        if failures_path.is_file():
            recorded = json.loads(failures_path.read_text())
            # Catalog ids (`group/name`); a document that failed here has no image, so it is in
            # no lane above.
            publish_failures = [{"id": sid, "reason": first_line(str(text))}
                                for sid, text in sorted(recorded.items())]
            publish["renderFailures"] = publish_failures
            publish["reasons"] = group_reasons(publish_failures)
    return {
        "schema": "rc-compare-failures/v1",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "key": key,
        "keyRecorded": key_recorded,
        "documents": summary.get("total", len(rows)),
        "lanes": lanes,
        "publish": publish,
    }


def _cell(text: str) -> str:
    return "`" + text.replace("`", "'").replace("|", "\\|") + "`"


def failures_markdown(report: dict, top: int = 5) -> str:
    lines = ["### Player comparison failures", "",
             f"{report['documents']} documents. Full list in `{FAILURES_FILE}` on the delivery "
             "branch.", "",
             "| Lane | Rendered | Failed | Suspect |", "| --- | ---: | ---: | ---: |"]
    for name, lane in report["lanes"].items():
        label = f"{name} (dropped)" if lane.get("dropped") else name
        lines.append(f"| {label} | {lane['rendered']} | {lane['failed']} | "
                     f"{lane.get('suspect', '–')} |")
    publish_failures = report["publish"].get("renderFailures")
    if publish_failures is not None:
        lines.append(f"| publish (baked PNG) | {report['publish'].get('images', '–')} | "
                     f"{len(publish_failures)} | – |")
    sections = [(name, lane["reasons"], lane.get("suspectKinds", {}))
                for name, lane in report["lanes"].items()]
    if publish_failures:
        sections.append(("publish (baked PNG)", report["publish"]["reasons"], {}))
    for name, reasons, kinds in sections:
        if not reasons and not kinds:
            continue
        lines += ["", f"**{name}**", ""]
        if reasons:
            lines += ["| Count | Reason |", "| ---: | --- |"]
            lines += [f"| {group['count']} | {_cell(group['reason'])} |" for group in reasons[:top]]
            if len(reasons) > top:
                rest = sum(group["count"] for group in reasons[top:])
                lines.append(f"| {rest} | {len(reasons) - top} more reason(s) |")
        if kinds:
            lines += ["", "Suspect renders: " + ", ".join(f"{count} {kind}"
                                                           for kind, count in kinds.items())]
    return "\n".join(lines) + "\n"


def cmd_failures(args: argparse.Namespace) -> int:
    summary_path = args.compare / SUMMARY_FILE
    if not summary_path.is_file():
        print(f"no {SUMMARY_FILE} in {args.compare}; nothing to record", file=sys.stderr)
        return 1
    dropped_path = args.work / "dropped-lanes" if args.work else None
    dropped = []
    if dropped_path and dropped_path.is_file():
        dropped = [line.strip() for line in dropped_path.read_text().splitlines() if line.strip()]
    report = collect_failures(json.loads(summary_path.read_text()), args.work, dropped, args.key,
                              key_recorded=not dropped, output=args.output)
    target = args.output / FAILURES_FILE
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    temporary.replace(target)
    markdown = failures_markdown(report)
    step_summary = args.step_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as handle:
            handle.write(markdown + "\n")
    else:
        print(markdown)
    print(json.dumps({name: {k: lane[k] for k in ("rendered", "failed", "suspect") if k in lane}
                      for name, lane in report["lanes"].items()}))
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
    install.add_argument("--dropped-lanes", type=Path,
                         help="rc_compare_lanes.sh's list of lanes that dropped their column")
    install.set_defaults(func=cmd_install)

    failures = commands.add_parser(
        "failures", help=f"write {FAILURES_FILE} and a step-summary table for a comparison")
    failures.add_argument("--compare", type=Path, required=True, help="rc-compare.mjs --out dir")
    failures.add_argument("--work", type=Path,
                          help="rc_compare_lanes.sh's $WORK (harness .error files, dropped-lanes)")
    failures.add_argument("--output", type=Path, required=True, help="delivery branch checkout")
    failures.add_argument("--key", required=True, help="the key printed by `key`")
    failures.add_argument("--step-summary",
                          help="append the markdown here (default $GITHUB_STEP_SUMMARY, else stdout)")
    failures.set_defaults(func=cmd_failures)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
