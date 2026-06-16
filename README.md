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
