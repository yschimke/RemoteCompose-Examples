#!/usr/bin/env python3
"""Incrementally publish the static Remote Compose catalog from main's corpus."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

RENDER_VERSION = 1
SYSTEM = "remote-compose-examples"
CAPTURE_PAGE = b"""<!doctype html><html><head><script src='/assets/player.js'></script></head><body></body></html>"""
CAPTURE_SCRIPT = r"""
async ({id, width, height, maxDimension}) => {
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:-10000px;top:0';
  document.body.appendChild(host);
  let handle;
  try {
    const response = await fetch(`/docs/${id}/doc.rc`);
    if (!response.ok) throw new Error(`document fetch returned ${response.status}`);
    handle = RC.createPlayer(host, {width, height});
    await handle.loadFromArrayBuffer(await response.arrayBuffer());
    handle.player.repaint();
    await new Promise(resolve => setTimeout(resolve, 40));
    handle.player.repaint();
    const scale = Math.min(1, maxDimension / Math.max(width, height));
    const image = document.createElement('canvas');
    image.width = Math.max(1, Math.round(width * scale));
    image.height = Math.max(1, Math.round(height * scale));
    image.getContext('2d').drawImage(handle.canvas, 0, 0, image.width, image.height);
    return {png: image.toDataURL('image/png'), width: image.width, height: image.height};
  } finally {
    if (handle) handle.destroy();
    host.remove();
  }
}
"""


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def document_id(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError(f"unsafe document id: {value!r}")
    if path.as_posix() != value:
        raise ValueError(f"non-canonical document id: {value!r}")
    return value


def load_corpus(source: Path) -> list[dict]:
    rows = [json.loads(line) for line in (source / "catalog/corpus.jsonl").read_text().splitlines()]
    ids = [document_id(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate document id in corpus")
    for row in rows:
        expected = f"docs/{row['id']}/doc.rc"
        if row["rc"] != expected or not (source / expected).is_file():
            raise ValueError(f"missing or unexpected .rc path for {row['id']}")
    return rows


class CaptureHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/__capture":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(CAPTURE_PAGE)))
            self.end_headers()
            self.wfile.write(CAPTURE_PAGE)
            return
        super().do_GET()

    def log_message(self, *args: object) -> None:
        pass


def capture_images(source: Path, rows: list[dict], output: Path, state: dict,
                   browser_executable: str | None, max_dimension: int) -> tuple[int, int]:
    from playwright.sync_api import sync_playwright

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(CaptureHandler, directory=str(source))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rendered = failed = 0
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": [
                "--no-sandbox", "--disable-dev-shm-usage", "--use-gl=angle",
                "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            ]}
            if browser_executable:
                launch["executable_path"] = browser_executable
            browser = playwright.chromium.launch(**launch)
            try:
                page = browser.new_page(device_scale_factor=1)
                url = f"http://127.0.0.1:{server.server_port}/__capture"
                page.goto(url)
                for row in rows:
                    item = state[row["id"]]
                    key = f"{item['sha256']}:{item['renderVersion']}"
                    try:
                        width = int(row.get("width") or 400)
                        height = int(row.get("height") or 400)
                        if width < 1 or height < 1 or width * height > 10_000_000:
                            raise ValueError("invalid or oversized document dimensions")
                        result = page.evaluate(CAPTURE_SCRIPT, {
                            "id": row["id"], "width": width, "height": height,
                            "maxDimension": max_dimension,
                        })
                        image = base64.b64decode(result["png"].split(",", 1)[1])
                        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("player did not return a PNG")
                        atomic_write(output / f"images/{row['id']}.png", image)
                        item.update(renderKey=key, imageWidth=result["width"],
                                    imageHeight=result["height"])
                        item.pop("error", None)
                        rendered += 1
                    except Exception as exc:
                        item.update(renderKey=key, error=str(exc)[:500])
                        (output / f"images/{row['id']}.png").unlink(missing_ok=True)
                        failed += 1
                        page.goto(url)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return rendered, failed


def publish(source: Path, output: Path, revision: str, batch_size: int,
            browser_executable: str | None, max_dimension: int, retry_failed: bool) -> dict:
    rows = load_corpus(source)
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "publish-state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    state: dict[str, dict] = {}
    player_hash = hashlib.sha256((source / "assets/player.js").read_bytes()).hexdigest()[:16]
    render_version = f"{RENDER_VERSION}:{player_hash}"
    copied = 0
    for row in rows:
        source_file = source / row["rc"]
        data = source_file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if row.get("sha256") and row["sha256"] != digest:
            raise ValueError(f"corpus hash differs from .rc bytes for {row['id']}")
        dest = output / f"documents/{row['id']}.rc"
        if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
            atomic_write(dest, data)
            copied += 1
        old = previous.get(row["id"], {})
        current = {"sha256": digest, "renderVersion": render_version}
        if old.get("sha256") == digest and old.get("renderVersion") == render_version:
            current.update({k: old[k] for k in ("renderKey", "imageWidth", "imageHeight", "error")
                            if k in old})
        else:
            (output / f"images/{row['id']}.png").unlink(missing_ok=True)
        state[row["id"]] = current

    active = set(state)
    for old_id in set(previous) - active:
        (output / f"documents/{old_id}.rc").unlink(missing_ok=True)
        (output / f"images/{old_id}.png").unlink(missing_ok=True)

    def pending(row: dict) -> bool:
        item = state[row["id"]]
        key = f"{item['sha256']}:{render_version}"
        if item.get("renderKey") != key:
            return True
        if retry_failed and item.get("error"):
            return True
        return not item.get("error") and not (output / f"images/{row['id']}.png").exists()

    # Featured samples form the first useful catalog; every later run advances the backlog.
    queue = sorted((row for row in rows if pending(row)),
                   key=lambda row: ("featured" not in row.get("tags", []), row["id"]))
    batch = queue[:batch_size]
    rendered = failed = 0
    if batch:
        rendered, failed = capture_images(
            source, batch, output, state, browser_executable, max_dimension)

    components = []
    for row in rows:
        item = state[row["id"]]
        image = output / f"images/{row['id']}.png"
        if not image.exists() or item.get("error"):
            continue
        components.append({
            "componentId": row["id"],
            "section": row["source"],
            "group": row["id"].split("/", 1)[0],
            "caption": row.get("description") or row["title"],
            "sourceFile": row["rc"],
            "rcDocument": f"documents/{row['id']}.rc",
            "images": [{
                "variant": "ideal", "state": "default",
                "path": f"images/{row['id']}.png",
                "width": item["imageWidth"], "height": item["imageHeight"],
            }],
        })
    source_date = subprocess.check_output(
        ["git", "-C", str(source), "show", "-s", "--format=%cI", revision], text=True
    ).strip()
    manifest = {
        "schema": "design-parity-catalog/v1",
        "system": SYSTEM,
        "title": "Remote Compose Examples",
        "library": ["Remote Compose"],
        "renderer": f"Remote Compose web player {render_version}",
        "generatedAt": source_date,
        "source": {"repo": "yschimke/RemoteCompose-Examples", "ref": "main", "commit": revision},
        "components": components,
    }
    documents = [dict(row, rc=f"documents/{row['id']}.rc", sourceRc=row["rc"])
                 for row in rows]
    atomic_write(output / "catalog.json", json_bytes(manifest))
    atomic_write(output / "documents.jsonl", b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for row in documents
    ))
    atomic_write(state_path, json_bytes(state))
    atomic_write(output / "render-failures.json", json_bytes({
        row["id"]: state[row["id"]]["error"] for row in rows if state[row["id"]].get("error")
    }))
    progress = {"source": revision, "documents": len(rows), "images": len(components),
                "renderFailures": sum(bool(item.get("error")) for item in state.values()),
                "remaining": len(queue) - len(batch)}
    atomic_write(output / "publish-progress.json", json_bytes(progress))
    validate_output(output, rows, components, state)
    if batch and rendered == 0 and len(components) == 0:
        raise RuntimeError("no document rendered; inspect render-failures.json")
    return dict(progress, copiedRc=copied, rendered=rendered, failed=failed)


def validate_output(output: Path, rows: list[dict], components: list[dict],
                    state: dict[str, dict]) -> None:
    """Catch partial copies and dangling image paths before committing a delivery tree."""
    for row in rows:
        path = output / f"documents/{row['id']}.rc"
        if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()
                != state[row["id"]]["sha256"]):
            raise ValueError(f"published .rc is missing or changed: {row['id']}")
    for component in components:
        for image in component["images"]:
            path = output / image["path"]
            if not path.is_file() or not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError(f"published image is missing or invalid: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", help="source branch commit; defaults to source HEAD")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--max-dimension", type=int, default=640)
    parser.add_argument("--browser-executable", help="Chromium or Google Chrome path")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 0 or args.max_dimension < 1:
        parser.error("batch-size must be non-negative and max-dimension must be positive")
    source = args.source.resolve()
    revision = args.revision or subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    print(json.dumps(publish(source, args.output.resolve(), revision, args.batch_size,
                             args.browser_executable, args.max_dimension, args.retry_failed)))


if __name__ == "__main__":
    main()
