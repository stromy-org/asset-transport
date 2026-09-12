# Asset Transport

Domain-agnostic content-addressed asset store and outbound delivery ladder shared by Stromy MCP servers

## Install

```bash
uv sync                          # Core deps
uv sync --extra all              # All optional extras
uv sync --extra dev              # Dev tools
```


## Public API

Two transport primitives every MCP that moves large binaries can share, plus a
caller-side tenant-scoping helper:

```python
from stromy_asset_transport import AssetStore, deliver_artifact, DeliveryResult, tenant_key

# Inbound: handles in, not inline base64.
store = AssetStore()
sha = store.put(font_bytes)          # -> "sha256…" (PUT-if-absent, content-addressed)
raw = store.fetch(sha)               # -> bytes (mem → disk → backend, digest-verified)

# Outbound: URLs/handles out, not raw blobs. One ladder, four modes.
result = deliver_artifact(
    deck_bytes,
    filename="strategy.pptx",
    inline_max=256 * 1024,           # ≤ this → inline base64; above → SharePoint/SAS
)
# result.mode ∈ {'inline','sharepoint','sas','pushed'}; result.inline_b64 / .download_url /
# .web_url / .warnings carry the channel-specific payload.

# Caller-side ACL key (the store's dedup key stays bare sha256).
key = tenant_key("rebecaelmudesi", sha)
```

Backends are selected by env (local filesystem for tests/dev, Azure Blob via
managed identity / connection string, SharePoint via Graph). The `azure` extra
installs the Azure SDKs — consumers that only use the local or SharePoint path
do not need it. See `stromy-org/infra-docs/ai/asset-store.md` for the full model.

### Per-space SharePoint targeting

The `RENDER_SHAREPOINT_*` env vars name **one** deployment-wide destination. To
deliver into a per-engagement collaboration space instead, pass an explicit
`SharePointTarget`:

```python
from stromy_asset_transport import SharePointTarget, deliver_artifact

result = deliver_artifact(
    pdf_bytes,
    filename="brief.pdf",
    inline_max=0,
    subfolder="KVGO/Social Media Campaign/03 Deliverables",
    sharepoint_target=SharePointTarget(
        site_id="stromy.sharepoint.com:/sites/ai4comms-collab",
        base_path="",             # None → env default ('Deliverables'); '' → drive root
        link_mode="webUrl",       # members hold direct permissions; mint no sharing link
    ),
)
```

A target **replaces** the env destination outright (it never merges with a
leftover `RENDER_SHAREPOINT_DRIVE_ID`), and is gated by
`RENDER_SHAREPOINT_ALLOWED_SITES` — a comma-separated allowlist of site refs in
the `host:/sites/name` form:

- **Deny-by-default.** An unset/empty allowlist refuses every explicit target,
  leaving only the env destination reachable.
- **A target must name a site.** A drive-id-only target is refused: a bare drive
  id would bypass the site boundary the allowlist enforces.
- **Refusal is loud, never silent.** An off-allowlist target raises
  `OutputStoreError`; `deliver_artifact` records it as a warning and drops to the
  SAS rung, so a mis-targeted artifact degrades rather than landing in the wrong
  tenant's space.

This library stays client-agnostic: it accepts a resolved destination and never
learns which client it belongs to. Mapping a client to its space is the caller's
job (in Stromy, `companies/<slug>/workspace.json` at the plugin layer).

### Workspace storage (read / list / create-only)

The delivery ladder is write-only. A durable, client-readable project record
needs three more shapes, added in 0.3.0 and gated by the same allowlist:

```python
from stromy_asset_transport import (
    SharePointFileRef, SharePointTarget,
    ensure_folder, create_file_once, get_file_metadata, read_file, list_children,
)

target = SharePointTarget(site_id="stromy.sharepoint.com:/sites/ai4comms-collab", base_path="")
ensure_folder(target, ["KVGO", "Social Media Campaign", "workspace-events"])

ref = SharePointFileRef.of(target, "KVGO", "Social Media Campaign",
                           "workspace-events", "sha256-abc123.json")
create_file_once(ref, body, digest=hashlib.sha256(body).hexdigest())  # replay-safe

items, cursor = list_children(target, ["KVGO", "Social Media Campaign", "workspace-events"],
                              limit=50)
```

Contract:

- **Create-only, never replace.** `create_file_once` writes with
  `conflictBehavior: fail`. On a conflict it reads the existing bytes back and
  compares the SHA-256: an equal digest is a *replay* and succeeds without a
  second write; a different digest raises `IdempotencyCollision` rather than
  overwriting a record a client can read. (Bytes are compared rather than a
  stored hash because SharePoint's Graph metadata exposes `quickXorHash`, not
  SHA-256.)
- **The ledger stays create-only.** There is deliberately no `update_file`: an
  immutable, content-addressed record makes a retry a no-op rather than a race.
  That is a design choice, not an API limit. An earlier version of this line said
  the platform offered no way to write conditionally; that was **false** — Graph
  honours `If-Match` here (see the co-edit guard below) — and it was the premise
  ORG-186 was mis-designed around for five weeks.
- **The co-edit guard is a real compare-and-swap.** `deliver_artifact` /
  `deliver_to_sharepoint` take `base_version` (the eTag you read before building),
  `base_sha256` and `force`. The content PUT carries `If-Match: <base_version>`;
  Graph answers `412` when the destination moved. Because SharePoint's web editors
  save a version on merely *opening* a file, a 412 alone is not evidence of an edit
  — the handler reads the current bytes and compares the digest: equal means a
  zero-change save (re-PUT once against the current eTag, `if_match_retried`),
  different means a collaborator really edited (refuse with a `StaleBaseConflict`
  naming the intervening versions and authors). No `base_sha256`, or an unreadable
  remote, **fails closed**. The happy path costs zero extra round-trips: the
  compare is bought only after a 412. `If-Match` on this endpoint is **measured,
  not documented** (probed 2026-09-08), so a 2xx that cannot be reconciled with the
  header having been honoured surfaces `failure_code: "graph_ignored_if_match"`
  rather than a clean push.
- **Folders are ensured, never renamed.** `ensure_folder` creates each missing
  level with `conflictBehavior: fail` and re-resolves on a 409 race. It never
  lets Graph mint `02 Working 1`, which would silently misfile an artifact.
- **Reads never export a capability.** `read_file` fetches metadata first and
  then the short-lived `@microsoft.graph.downloadUrl` **without** an
  Authorization header; that URL is never returned to a caller or logged.
- **Every path segment is validated.** `SharePointFileRef.of` and the folder /
  listing helpers reject `..`, embedded separators, absolute paths, control
  characters, and leading/trailing dots or spaces — refused before a URL is
  built, so no caller can escape its allowlisted target.
- **Listings are bounded.** `list_children` caps at `LIST_CHILDREN_MAX_LIMIT` and
  returns an opaque cursor; a cursor that does not point at `graph.microsoft.com`
  is refused.
- **Contention defers honestly.** A `423`/`429` uses the server's `Retry-After`
  or the same bounded 2/5/15s schedule the write path uses (four attempts max),
  then raises `SharePointLockedError` (which `deliver_artifact` already reports
  as `failure_code="sharepoint_locked"`) for a lock, or `GraphRequestError` for a
  pure throttle. Nothing is written after a defer.

## Tests

```bash
uv run pytest tests/unit
uv run pytest tests/contract
```

## Releases

This library is consumed by downstream repos via `[tool.uv.sources]` git+URL pins. To cut a release:

1. Bump `[project].version` in `pyproject.toml` on `main`.
2. `git tag vX.Y.Z && git push --tags`
3. CI builds + publishes a GitHub Release; `notify-parent.yml` fires a `submodule-bumped` event into stromy-org.

See `stromy-org/infra-docs/ai/internal-libs.md` for the full release pattern.

## Agent instructions

See `AGENTS.md` (canonical, cross-vendor). `CLAUDE.md` and `.github/copilot-instructions.md` are regenerated from it by `scripts/render-agent-md.py`.
