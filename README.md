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
