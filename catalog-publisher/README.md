# Remote Compose preview catalog publisher

This code lives on `agent/preview-catalog-publisher`. The fork's `main` remains a
direct copy of upstream. Generated files live on the separate orphan branch
`design-artifacts/remote-compose-examples`, which is the branch to register as
`remote-compose-examples` on `preview.coo.ee` **after it exists**.

The delivery branch contains `catalog.json` (`design-parity-catalog/v1`), PNGs
under `images/`, and **all** 1,310 compiled documents under `documents/`. The
`documents.jsonl` index maps each original sample to its downloadable `.rc`.
No executable live bundle is published: this is a static catalog, so the server
will show baked images and the `.rc` payloads. The original GitHub Pages site
still provides the interactive browser player.

## Incremental updates

Each run checks out the latest `main` as source, copies only changed `.rc` bytes,
and renders at most 40 missing or changed images. The output branch advances by
an ordinary commit only when artifacts change. It retains the last successful
image for unchanged documents, records render failures, and moves past failed
documents on the next run. The player bundle's hash is part of the image cache
key, so a player update schedules new captures without rewriting unchanged
`.rc` files.

The workflow runs on pushes to the publisher branch. To advance another batch
without changing publisher code, push an empty commit to that branch:

```sh
git switch agent/preview-catalog-publisher
git commit --allow-empty -m 'chore: publish next preview batch'
git push origin agent/preview-catalog-publisher
```

When upstream adds samples, repeat this after `main` is updated; the workflow
always checks out the current `main`. GitHub schedules only workflows present
on the default branch, so a cron on this isolated publisher branch would not
run. Keeping the workflow off `main` makes the manual trigger explicit.

For a local update, check out the output branch separately and run:

```sh
python3 -m pip install playwright==1.55.0
python3 -m playwright install chromium
python3 catalog-publisher/publish.py \
  --source . --output ../remote-compose-catalog-output \
  --revision "$(git rev-parse main)" --batch-size 40
```

The publisher renders each document at its declared size (400 × 400 when the
header leaves it unspecified), then scales the captured frame to at most 640
pixels on its longest side. It never shrinks the player's viewport, which would
change layout. Each player is destroyed after capture to release its WebGL
context. Failed captures are listed in `render-failures.json` and can be retried
with `--retry-failed`.
