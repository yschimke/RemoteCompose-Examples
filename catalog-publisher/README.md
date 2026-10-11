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

Each run checks out the latest `main` as source and commits changed `.rc` bytes
**before** rendering. It then renders up to 40 missing or changed images per
batch, committing each batch before starting the next. A run stops after at most
40 batches or 60 minutes, and the next run resumes from the branch tip. The
output branch advances by an ordinary commit only when artifacts change. It retains the last successful
image for unchanged documents, records render failures, and moves past failed
documents on the next run. The player bundle's hash is part of the image cache
key, so a player update schedules new captures without rewriting unchanged
`.rc` files.

The workflow runs on pushes to the publisher branch. If a run reaches its time or
batch cap before completing the corpus, push an empty commit to resume:

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

## Player comparison

After a publish run, the `player-comparison` job renders every published document in each
Remote Compose player. It writes the results to the delivery branch root, where
preview.coo.ee's compare page shows the players side by side. The job runs only once
`publish-progress.json` reports `remaining: 0`, so a partial corpus is never compared.

It uses the same tooling as compose-ai-tools' `design-artifacts-reusable.yml`:
`rc-compare.mjs` from `@design-parity/export-driver`, installed from compose-ai-tools' locked
`package-lock.json` at a pinned commit. Its input is a zip that `rc_compare_bundle.py bundle`
builds from `documents/**.rc` and `images/**.png`. Each document's id is its served id: the image
path without `images/` and `.png`, with `/` replaced by `__`, for example
`advanced-samples__2-vtext`. The baked PNG from this publisher is the reference column
(`rc-baked/`). The job renders these lanes against it:

| Lane | Player | Output |
| --- | --- | --- |
| js | compose-ai-tools' vendored TypeScript player in Chromium | `rc/`, `rc-diff/` |
| embedded | rc-players' vendored Android player (Robolectric) | `rc-embedded/`, `rc-embedded-diff/` |
| androidx-embedded | AndroidX `remote-player-compose` (Robolectric) | `rc-androidx-embedded/`, `rc-androidx-embedded-diff/` |
| cmp-jvm | `rc-player-compose` on Compose Desktop / Skiko under xvfb | `rc-embedded-jvm/`, `rc-embedded-jvm-diff/` |

`rc-compare-summary.json` and `rc-compare.html` sit beside the lane directories. Each lane is
fail-soft on its own: if a harness cannot run, that lane's column is left out and the job
still succeeds. The step summary explains why the column is missing. Only the js lane is
required. Every run replaces the lane directories, so a dropped lane never leaves stale
images behind.

The **cmp-wasm** lane is off. About 15% of these documents use features the CMP player lacks,
and the reusable workflow's `--require-cmp-wasm` gate fails on them. To add the column
report-only, without the gate, dispatch the workflow with `rc-cmp-wasm-lane` enabled.

Comparing all 1,310 documents takes a long time, so the job is **cached by skipping**.
`rc-compare-key.txt` holds a sha256 over these inputs:

- every compared `.rc` and baked PNG, by served id
- the catalog's `renderer` string, which carries the baked player's hash
- the export-driver version, the rc-players tag and the compose-ai-tools commit
- which lanes are enabled, plus `COMPARE_VERSION` in `rc_compare_bundle.py`

If the key on the delivery branch matches, the job exits early with a notice. To force a
regeneration, delete `rc-compare-key.txt` from the delivery branch and re-run the workflow. To
force one from a code change instead, bump `COMPARE_VERSION`.

The comparison sizes each player to the baked PNG. A document larger than 640 pixels on its
longest side is therefore compared at its scaled-down size, not its declared size. The pins
`COMPOSE_AI_TOOLS_REF`, `RC_PLAYERS_PIN` and `EXPORT_DRIVER_VERSION` move together. The job
fails if the driver version disagrees with the compose-ai-tools lock. It skips the embedded
lanes if `rcplayers` there differs from `RC_PLAYERS_PIN`.
