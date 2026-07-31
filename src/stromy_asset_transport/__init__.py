"""Domain-agnostic asset transport for Stromy MCP servers.

Three primitives shared by every MCP that moves large binaries or maintains a
durable record beside them:

- :class:`AssetStore` — content-addressed inbound store: ``put(bytes) -> sha256``,
  ``fetch(sha256) -> bytes`` (handles in, not inline base64).
- :func:`deliver_artifact` — outbound delivery ladder returning a
  :class:`DeliveryResult` (URLs/handles out, not raw blobs).
- the workspace-storage primitives (:func:`ensure_folder`, :func:`create_file_once`,
  :func:`get_file_metadata`, :func:`read_file`, :func:`list_children`) — safe
  read / list / create-only Drive operations for an immutable, client-readable
  project ledger. Create-only by design: no conditional content replacement.

The library is domain-agnostic: it knows nothing about pptx/pdf keys, charters,
or clients. Callers map :class:`DeliveryResult` into their own result shapes and
enforce any tenant scoping themselves (see :func:`tenant_key`).
"""

from __future__ import annotations

from .delivery import (
    DEFAULT_READ_MAX_BYTES,
    LIST_CHILDREN_MAX_LIMIT,
    LIST_VERSIONS_MAX_LIMIT,
    DeliveryResult,
    DriveItemInfo,
    FileNotFound,
    FileVersionInfo,
    GraphRequestError,
    IdempotencyCollision,
    OutputStoreError,
    SharePointFileRef,
    SharePointLockedError,
    SharePointTarget,
    TargetNotAllowed,
    UnsafePath,
    WorkspaceStorageError,
    create_file_once,
    deliver,
    deliver_artifact,
    deliver_to_sharepoint,
    ensure_folder,
    get_file_metadata,
    list_children,
    list_file_versions,
    push_to_url,
    read_file,
)
from .exceptions import DependencyError, StromyAssetTransportError
from .keys import tenant_key
from .store import AssetStore, AssetStoreError

__version__ = "0.4.0"

__all__ = [
    "DEFAULT_READ_MAX_BYTES",
    "LIST_CHILDREN_MAX_LIMIT",
    "LIST_VERSIONS_MAX_LIMIT",
    "AssetStore",
    "AssetStoreError",
    "DeliveryResult",
    "DependencyError",
    "DriveItemInfo",
    "FileNotFound",
    "FileVersionInfo",
    "GraphRequestError",
    "IdempotencyCollision",
    "OutputStoreError",
    "SharePointFileRef",
    "SharePointLockedError",
    "SharePointTarget",
    "StromyAssetTransportError",
    "TargetNotAllowed",
    "UnsafePath",
    "WorkspaceStorageError",
    "create_file_once",
    "deliver",
    "deliver_artifact",
    "deliver_to_sharepoint",
    "ensure_folder",
    "get_file_metadata",
    "list_children",
    "list_file_versions",
    "push_to_url",
    "read_file",
    "tenant_key",
    "__version__",
]
