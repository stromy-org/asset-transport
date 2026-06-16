"""Content-addressed asset store — the inbound transport primitive.

``put(bytes) -> sha256`` stores a blob keyed by its own digest; ``fetch(sha256)
-> bytes`` resolves it back. Large binaries (fonts, hero images, logos, rendered
artifacts) cross the plugin→MCP boundary *by reference* — a content-addressed
handle the server resolves here — never as inline base64 in a model-generated
tool call (the ~140 KB output-token ceiling). This is the domain-agnostic
extraction of ``stromy-format-mcp``'s ``asset_store.py`` (read) +
``scripts/asset_store_upload.py`` (write half), shared by every Stromy MCP.

Content-addressing, not tenant partitioning: the key is purely ``sha256(bytes)``.
The store never learns which client a hash belongs to, identical assets across
clients dedupe to one blob, and an asset update yields a new hash (automatic
cache invalidation). Tenant scoping, when needed, is enforced by the *caller*
(e.g. the asset-broker staging ledger), never baked into the store key — see
:func:`stromy_asset_transport.keys.tenant_key`.

**No silent downgrade.** A handle the store cannot resolve is a hard
``AssetStoreError``; callers translate it to their own error with a "repopulate
the store" hint. There is no fallback to a wrong asset.

Backends (selected by env, in priority order; same selection contract for read
and write):
  ASSET_STORE_LOCAL_DIR         -> filesystem (tests/dev; <dir>/<sha256>)
  ASSET_STORE_ACCOUNT           -> Azure Blob via DefaultAzureCredential
                                   (container ASSET_STORE_CONTAINER, default
                                   'brand-assets'); managed identity in ACA.
  ASSET_STORE_CONNECTION_STRING -> Azure Blob via connection string (local az)
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .exceptions import DependencyError

DEFAULT_CONTAINER = "brand-assets"
DISK_CACHE_DIR = Path(os.environ.get("ASSET_STORE_CACHE_DIR", "/tmp/stromy-asset-cache"))  # noqa: S108
# Bound the per-replica disk cache so a long-lived replica does not grow without
# limit. Hot assets stay; cold ones are evicted LRU. Tunable via env.
_CACHE_MAX_ENTRIES = int(os.environ.get("ASSET_STORE_CACHE_MAX_ENTRIES", "256"))
_MEM_CACHE_MAX_ENTRIES = int(os.environ.get("ASSET_STORE_MEM_CACHE_MAX_ENTRIES", "64"))


class AssetStoreError(RuntimeError):
    """Handle could not be resolved or stored (missing blob, no backend, corruption).

    There is no fallback — callers turn this into their own error that tells the
    operator to repopulate the store (e.g. ``scripts/sync.sh client-data``).
    """


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


class AssetStore:
    """Resolve and store content-addressed assets by sha256.

    One instance per call is cheap; the disk cache is process-wide and survives
    across calls within a replica's lifetime. Thread-safe.
    """

    def __init__(self) -> None:
        self._mem: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()
        # Lazily-built Azure container clients (import + auth deferred to first use).
        # Read and write clients are separate so the write path can create the
        # container while the read path stays create-free.
        self._read_container: Any = None
        self._read_checked = False
        self._write_container: Any = None
        self._write_checked = False

    # -- public API -----------------------------------------------------------

    def fetch(self, sha256: str) -> bytes:
        """Return bytes for ``sha256``, verifying the digest after every fetch.

        Resolution order: in-process cache → disk cache → backend. Raises
        ``AssetStoreError`` on miss, missing backend, or digest mismatch.
        """
        key = sha256.lower()
        if not _is_sha256(key):
            raise AssetStoreError(f"not a valid sha256 handle: {sha256!r}")

        cached = self._mem_get(key)
        if cached is not None:
            return cached

        disk = self._disk_get(key)
        if disk is not None:
            self._verify(key, disk)
            self._mem_put(key, disk)
            return disk

        raw = self._backend_fetch(key)
        self._verify(key, raw)
        self._disk_put(key, raw)
        self._mem_put(key, raw)
        return raw

    def put(self, data: bytes) -> str:
        """Store ``data`` keyed by its sha256 (PUT-if-absent); return the handle.

        Idempotent: storing the same bytes twice writes once. Populates the local
        caches so an immediate ``fetch`` of the returned handle hits in-process.
        Raises ``AssetStoreError`` on empty input or a missing/failed backend.
        """
        if not data:
            raise AssetStoreError("refusing to store empty bytes (no content-addressable handle)")
        key = hashlib.sha256(data).hexdigest()
        self._backend_put(key, data)
        # Warm the caches so a fetch in the same process resolves without a round-trip.
        self._disk_put(key, data)
        self._mem_put(key, data)
        return key

    # -- integrity ------------------------------------------------------------

    @staticmethod
    def _verify(sha256: str, raw: bytes) -> None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != sha256:
            raise AssetStoreError(
                f"asset integrity check failed: requested sha256:{sha256} but "
                f"resolved bytes hash to sha256:{actual} (corruption or poisoned cache)"
            )

    # -- in-process LRU -------------------------------------------------------

    def _mem_get(self, key: str) -> bytes | None:
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        return None

    def _mem_put(self, key: str, raw: bytes) -> None:
        with self._lock:
            self._mem[key] = raw
            self._mem.move_to_end(key)
            while len(self._mem) > _MEM_CACHE_MAX_ENTRIES:
                self._mem.popitem(last=False)

    # -- disk LRU (process-wide, by mtime) ------------------------------------

    @staticmethod
    def _disk_get(key: str) -> bytes | None:
        path = DISK_CACHE_DIR / key
        if not path.is_file():
            return None
        try:
            os.utime(path, None)  # refresh mtime for LRU
        except OSError:
            pass
        return path.read_bytes()

    def _disk_put(self, key: str, raw: bytes) -> None:
        try:
            DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DISK_CACHE_DIR / f".{key}.tmp"
            tmp.write_bytes(raw)
            tmp.replace(DISK_CACHE_DIR / key)
            self._evict_disk()
        except OSError:
            # The disk cache is best-effort; a write failure must not fail the
            # call — the bytes are already in memory.
            pass

    @staticmethod
    def _evict_disk() -> None:
        try:
            entries = [
                p for p in DISK_CACHE_DIR.iterdir()
                if p.is_file() and not p.name.startswith(".")
            ]
        except OSError:
            return
        if len(entries) <= _CACHE_MAX_ENTRIES:
            return
        entries.sort(key=lambda p: p.stat().st_mtime)
        for p in entries[: len(entries) - _CACHE_MAX_ENTRIES]:
            try:
                p.unlink()
            except OSError:
                pass

    # -- backends -------------------------------------------------------------

    def _backend_fetch(self, key: str) -> bytes:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / key
            if not path.is_file():
                raise AssetStoreError(
                    f"handle sha256:{key} not found in local store {local_dir}. "
                    f"Repopulate the asset store (e.g. `scripts/sync.sh client-data`)."
                )
            return path.read_bytes()

        container = self._read_azure_container()
        if container is None:
            raise AssetStoreError(
                "no asset-store backend configured. Set ASSET_STORE_LOCAL_DIR "
                "(tests/dev), ASSET_STORE_ACCOUNT (managed identity), or "
                "ASSET_STORE_CONNECTION_STRING."
            )
        try:
            blob = container.get_blob_client(key)
            return blob.download_blob().readall()
        except Exception as e:  # azure raises ResourceNotFoundError etc.
            raise AssetStoreError(
                f"handle sha256:{key} not resolvable from blob store: {e}. "
                f"Repopulate the asset store (e.g. `scripts/sync.sh client-data`)."
            ) from e

    def _backend_put(self, key: str, raw: bytes) -> None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            d = Path(local_dir)
            d.mkdir(parents=True, exist_ok=True)
            dest = d / key
            if not dest.exists():
                tmp = d / f".{key}.tmp"
                tmp.write_bytes(raw)
                tmp.replace(dest)
            return

        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError(
                "no asset-store backend configured for write. Set ASSET_STORE_LOCAL_DIR "
                "(tests/dev), ASSET_STORE_ACCOUNT (managed identity / az login), or "
                "ASSET_STORE_CONNECTION_STRING."
            )
        blob = container.get_blob_client(key)
        try:
            if blob.exists():
                return
        except Exception:  # noqa: BLE001, S110 - existence probe is best-effort
            pass
        try:
            blob.upload_blob(raw, overwrite=False)
        except Exception as e:  # noqa: BLE001
            # A concurrent writer may have created the blob between the probe and
            # the upload; content-addressing makes that a no-op, not an error.
            if _blob_already_exists(e):
                return
            raise AssetStoreError(f"failed to store sha256:{key} to blob backend: {e}") from e

    def _read_azure_container(self) -> Any:
        if self._read_checked:
            return self._read_container
        self._read_checked = True
        self._read_container = _build_azure_container(read_only=True)
        return self._read_container

    def _write_azure_container(self) -> Any:
        if self._write_checked:
            return self._write_container
        self._write_checked = True
        self._write_container = _build_azure_container(read_only=False)
        return self._write_container


def _blob_already_exists(exc: Exception) -> bool:
    """True if an Azure upload failed only because the blob already exists."""
    name = type(exc).__name__
    return "BlobAlreadyExists" in str(exc) or name in {"ResourceExistsError", "ResourceModifiedError"}


def _build_azure_container(*, read_only: bool) -> Any:
    """Build an Azure container client from env, or return None if unconfigured.

    Shared backend-selection contract: account+managed-identity first, then
    connection string. Azure SDKs are imported lazily so non-Azure paths pay
    nothing and the ``azure`` extra stays optional. The write path
    (``read_only=False``) ensures the container exists.
    """
    account = os.environ.get("ASSET_STORE_ACCOUNT")
    conn = os.environ.get("ASSET_STORE_CONNECTION_STRING")
    container_name = os.environ.get("ASSET_STORE_CONTAINER", DEFAULT_CONTAINER)
    if not account and not conn:
        return None

    try:
        from azure.storage.blob import BlobServiceClient  # lazy
    except ModuleNotFoundError as e:
        raise DependencyError("azure", "azure-storage-blob") from e

    if account:
        try:
            from azure.identity import DefaultAzureCredential  # lazy
        except ModuleNotFoundError as e:
            raise DependencyError("azure", "azure-identity") from e

        svc = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    elif conn:
        svc = BlobServiceClient.from_connection_string(conn)
    else:  # unreachable: guarded above (account or conn is set)
        return None

    container = svc.get_container_client(container_name)
    if not read_only:
        try:
            container.create_container()
        except Exception:  # noqa: BLE001, S110 — already exists is the common case
            pass
    return container
