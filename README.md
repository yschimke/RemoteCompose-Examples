# Remote Compose Examples delivery catalog

Generated from [`yschimke/RemoteCompose-Examples`](https://github.com/yschimke/RemoteCompose-Examples)
by the publisher on `agent/preview-catalog-publisher`.

- `catalog.json` is the `design-parity-catalog/v1` manifest for `preview.coo.ee`.
- `images/` contains rendered preview PNGs, added incrementally.
- `documents/` contains every compiled `.rc` sample, whether or not its image has
  been captured yet. `documents.jsonl` is the complete metadata and download index.
- `publish-progress.json` and `render-failures.json` show image coverage and
  any captures the browser player could not complete.

This branch is append-only. Each update preserves unchanged documents and images
and adds one commit for changed artifacts. It has no executable live bundle.
