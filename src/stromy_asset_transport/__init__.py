"""Domain-agnostic asset transport for Stromy MCP servers.

Two primitives shared by every MCP that moves large binaries:

- :class:`AssetStore` — content-addressed inbound store: ``put(bytes) -> sha256``,
  ``fetch(sha256) -> bytes`` (handles in, not inline base64).
- :func:`deliver_artifact` — outbound delivery ladder returning a
  :class:`DeliveryResult` (URLs/handles out, not raw blobs).

The library is domain-agnostic: it knows nothing about pptx/pdf keys, charters,
or clients. Callers map :class:`DeliveryResult` into their own result shapes and
enforce any tenant scoping themselves (see :func:`tenant_key`).
"""

from __future__ import annotations

from .delivery import (
    DeliveryResult,
    OutputStoreError,
    SharePointTarget,
    deliver,
    deliver_artifact,
    deliver_to_sharepoint,
    push_to_url,
)
from .exceptions import DependencyError, StromyAssetTransportError
from .keys import tenant_key
from .store import AssetStore, AssetStoreError

__version__ = "0.2.0"

__all__ = [
    "AssetStore",
    "AssetStoreError",
    "DeliveryResult",
    "DependencyError",
    "OutputStoreError",
    "SharePointTarget",
    "StromyAssetTransportError",
    "deliver",
    "deliver_artifact",
    "deliver_to_sharepoint",
    "push_to_url",
    "tenant_key",
    "__version__",
]
