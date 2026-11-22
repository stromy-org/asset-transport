"""Content-addressed asset store — the inbound transport primitive.

``put(bytes) -> sha256`` stores a blob keyed by its own digest; ``fetch(sha256)
-> bytes`` resolves it back. Large binaries (fonts, hero images, logos, rendered
artifacts) cross the plugin→MCP boundary *by reference* — a content-addressed
handle the server resolves here — never as inline base64 in a model-generated
tool call (the ~140 KB output-token ceiling). This is the domain-agnostic
extraction of ``stromy-format-mcp``'s ``asset_store.py`` (read) +
``scripts/asset_store_upload.py`` (write half), shared by every Stromy MCP.

The content key is ``sha256(bytes)``, so identical assets share one blob. A
service may bind bytes it has verified to an authenticated scope at upload time.
Separate durable markers record those bindings. Each service chooses the scope
from verified identity or workspace authority and checks it before resolving a
handle from a request.

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

import contextlib
import hashlib
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from .exceptions import DependencyError

DEFAULT_CONTAINER = "brand-assets"
# Out-of-band uploads land under this prefix/subdir before finalize moves them to
# their content-addressed key. Kept separate so a staged-but-unfinalized blob is
# never mistaken for a resolvable handle.
_STAGING_PREFIX = ".staging"
_ACCESS_PREFIX = "asset-access/v1"
DISK_CACHE_DIR = Path(os.environ.get("ASSET_STORE_CACHE_DIR", "/tmp/stromy-asset-cache"))  # noqa: S108
# Bound the per-replica disk cache so a long-lived replica does not grow without
# limit. Hot assets stay; cold ones are evicted LRU. Tunable via env.
_CACHE_MAX_ENTRIES = int(os.environ.get("ASSET_STORE_CACHE_MAX_ENTRIES", "256"))
_MEM_CACHE_MAX_ENTRIES = int(os.environ.get("ASSET_STORE_MEM_CACHE_MAX_ENTRIES", "64"))

# -- asset class ---------------------------------------------------------------
# A blob is `deliverable` (a client's source material staged for a render, expired
# after 90 days), `brand` (a client's own brand material, kept and proposable) or
# `library` (ORG-PLAN-592: a brand's heavy original or derivative whose meaning
# lives in client-data's library.json; kept, never expires, and the store is its
# canonical copy). The class rides the blob as an Azure **index tag**, not
# metadata, because only index tags can be filtered on by a storage lifecycle
# rule (`match_blob_index_tag`) — which is what lets the platform do the expiring
# instead of a scheduled job of ours. The local-dir backend has no tag concept,
# so there the class is a `<key>.class` sidecar written under the identical
# monotonic rule: the whole contract is exercisable in tests with no Azure at all.
_CLASS_TAG = "asset_class"
_CLASS_BRAND = "brand"
_CLASS_DELIVERABLE = "deliverable"
_CLASS_LIBRARY = "library"
_CLASS_SIDECAR_SUFFIX = ".class"
#: Retention precedence. Reconciliation only ever moves UP this order:
#: untagged < deliverable < brand < library. A tag value not in this map (a class
#: introduced by a newer release) is never overwritten by this one.
CLASS_RANK: dict[str, int] = {_CLASS_DELIVERABLE: 1, _CLASS_BRAND: 2, _CLASS_LIBRARY: 3}
#: The classes the store recognises. See :meth:`AssetStore.put` for the monotonic
#: reconciliation rule.
ASSET_CLASSES = frozenset(CLASS_RANK)

# Streaming I/O (ORG-PLAN-592 A2): files are hashed, uploaded and finalized in
# bounded chunks so a gigabyte source never sits in memory. Blobs above the
# inline-cache ceiling are not copied into the process caches either.
_STREAM_CHUNK = 8 * 1024 * 1024
_CACHE_INLINE_MAX = int(os.environ.get("ASSET_STORE_CACHE_INLINE_MAX", str(32 * 1024 * 1024)))
# Tag reconciliation is serialised per blob (Azure: a short lease; local: a file
# lock) with bounded retry, so two concurrent writers cannot interleave a stale
# read with a downgrade write.
_TAG_LEASE_SECONDS = 15
_TAG_RETRIES = 6
_TAG_BACKOFF_SECONDS = 0.5


def class_outranks(new: str | None, existing: str | None) -> bool:
    """True when writing ``new`` over ``existing`` moves UP the retention order.

    ``None`` never outranks anything (an unclassified call changes nothing). An
    existing value this release does not know is treated as outranking every class
    it does know: an older writer must never overwrite a newer class.
    """
    if new is None:
        return False
    if existing is None:
        return True
    if existing not in CLASS_RANK:
        return False
    return CLASS_RANK[new] > CLASS_RANK[existing]


class AssetStoreError(RuntimeError):
    """Handle could not be resolved or stored (missing blob, no backend, corruption).

    There is no fallback — callers turn this into their own error that tells the
    operator to repopulate the store (e.g. ``scripts/sync.sh client-data``).
    """


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def _access_key(scope: str, sha256: str) -> str:
    if not scope or scope != scope.strip() or len(scope) > 200:
        raise ValueError("scope must be a non-empty canonical identifier of at most 200 characters")
    if not _is_sha256(sha256):
        raise ValueError("an asset binding requires a valid sha256 handle")
    scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return f"{_ACCESS_PREFIX}/{scope_digest}/{sha256.lower()}"


def _validate_asset_class(asset_class: str | None) -> None:
    """Refuse a class the store does not define. The store does not invent classes."""
    if asset_class is None or asset_class in ASSET_CLASSES:
        return
    raise ValueError(
        f"unknown asset_class {asset_class!r} — expected one of "
        f"{sorted(ASSET_CLASSES)} (or None for an unclassified blob)"
    )


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

    def exists(self, sha256: str) -> bool:
        """True when the content-addressed blob is present. Never downloads.

        A cache hit, or a HEAD against the backend. This is the pre-flight
        de-duplication primitive: a caller holding a candidate's bytes can ask
        whether the store already has them *without moving any*, which turns "the
        client re-uploads a 13 MB deck" into "nothing happens".

        A backend error is NOT swallowed into ``False``. A store we cannot reach
        must not read as "absent", because absent means "ask the client to upload
        it again" — and asking for an upload we already hold is the exact cost
        this method exists to avoid.
        """
        key = sha256.lower()
        if not _is_sha256(key):
            # ValueError, not AssetStoreError: a malformed argument is the caller's
            # bug, not a store failure, and it is refused before any network call.
            raise ValueError(f"not a valid sha256 handle: {sha256!r}")

        if self._mem_get(key) is not None or (DISK_CACHE_DIR / key).is_file():
            return True
        return self._backend_exists(key)

    def has_access(self, sha256: str, *, scope: str) -> bool:
        """Check a durable scope binding without accessing the content blob.

        The service derives ``scope`` from trusted identity or workspace data.
        An absent binding returns False. An unavailable backend raises.
        """
        return self._backend_exists(_access_key(scope, sha256))

    def put(
        self, data: bytes, *, asset_class: str | None = None, scope: str | None = None
    ) -> str:
        """Store ``data`` keyed by its sha256 (PUT-if-absent); return the handle.

        Idempotent: storing the same bytes twice writes once. Populates the local
        caches so an immediate ``fetch`` of the returned handle hits in-process.
        Raises ``AssetStoreError`` on empty input or a missing/failed backend.
        Pass a verified ``scope`` to bind the supplied bytes to it after the
        content write and class check have succeeded.

        ``asset_class`` (``"deliverable"`` | ``"brand"`` | ``"library"``) is written as a blob
        **index tag** so an Azure lifecycle rule can filter on it — metadata
        cannot be filtered on, tags can. OPTIONAL by design: several live call
        sites store unclassified render artifacts, and an untagged blob is never
        expired, by construction.

        CLASS IS MONOTONIC, AND THAT IS WHY THIS IS NOT A ONE-LINER. ``put``
        short-circuits when the blob already exists, so tagging cannot ride the
        upload. After the PUT-if-absent step this ALWAYS reconciles the tag:

          * no existing tag      -> write ``asset_class``
          * existing == new      -> no-op
          * a move UP ``deliverable < brand < library`` -> UPGRADE (set the tag)
          * any move down        -> REFUSE SILENTLY (keep the higher class)
          * an existing class this release does not know -> leave it alone

        Unrelated tags on the blob are preserved, and the read-decide-write runs
        under a per-blob lease (Azure) or file lock (local) with bounded retry, so
        concurrent writers cannot interleave into a downgrade.

        Without the upgrade branch, a picture first seen inside a client's source
        deck and later re-staged as a genuine brand asset keeps ``deliverable``
        and is deleted by the lifecycle rule on day 90.
        """
        if not data:
            raise AssetStoreError("refusing to store empty bytes (no content-addressable handle)")
        _validate_asset_class(asset_class)
        key = hashlib.sha256(data).hexdigest()
        access_key = _access_key(scope, key) if scope is not None else None
        self._backend_put(key, data)
        self._reconcile_class(key, asset_class)
        if access_key is not None:
            self._write_access_marker(access_key)
        # Warm the caches so a fetch in the same process resolves without a round-trip.
        self._disk_put(key, data)
        self._mem_put(key, data)
        return key

    def put_file(
        self,
        path: str | Path,
        *,
        asset_class: str | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[str, int]:
        """Stream a file into the store (PUT-if-absent); return ``(sha256, size)``.

        The bounded-memory twin of :meth:`put` (ORG-PLAN-592 A2): the file is hashed
        and uploaded in chunks, never read whole, and blobs above the inline-cache
        ceiling are not copied into the process caches. Same monotonic class rule.
        ``expected_sha256`` refuses a file whose bytes changed since it was hashed.
        """
        _validate_asset_class(asset_class)
        src = Path(path)
        key, size = _hash_file(src)
        if size == 0:
            raise AssetStoreError(f"refusing to store empty file {src} (no content-addressable handle)")
        if expected_sha256 is not None and key != expected_sha256.lower():
            raise AssetStoreError(
                f"{src} hashes to sha256:{key} but expected sha256:{expected_sha256.lower()} "
                f"(the file changed after it was inventoried)"
            )
        self._backend_put_file(key, src, size)
        self._reconcile_class(key, asset_class)
        if size <= _CACHE_INLINE_MAX:
            raw = src.read_bytes()
            self._disk_put(key, raw)
            self._mem_put(key, raw)
        return key, size

    def describe(self, sha256: str) -> dict[str, Any] | None:
        """``{"size": int, "asset_class": str | None}`` for a stored blob, else ``None``.

        A HEAD plus a tag read: never downloads. The verify primitive for a manifest
        that records sizes (ORG-PLAN-592 A3). A backend error RAISES, exactly as
        :meth:`exists` does: unreachable is never reported as absent.
        """
        key = sha256.lower()
        if not _is_sha256(key):
            raise ValueError(f"not a valid sha256 handle: {sha256!r}")
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / key
            if not path.is_file():
                return None
            return {"size": path.stat().st_size, "asset_class": self._read_class(key)}
        container = self._read_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for a describe")
        blob = container.get_blob_client(key)
        try:
            if not blob.exists():
                return None
            props = blob.get_blob_properties()
            tags: dict[str, Any] = dict(blob.get_blob_tags() or {})
        except Exception as e:  # noqa: BLE001 — unreachable is not absent
            raise AssetStoreError(f"could not describe sha256:{key}: {e}") from e
        value: Any = tags.get(_CLASS_TAG)
        return {"size": int(getattr(props, "size", 0) or 0), "asset_class": str(value) if value else None}

    def fetch_to_file(self, sha256: str, dest: str | Path) -> int:
        """Stream a blob to ``dest`` (atomic replace), verifying its digest; return size.

        Bounded memory: the recovery-cache primitive (ORG-PLAN-592 A2R). A digest
        mismatch leaves ``dest`` untouched and raises.
        """
        key = sha256.lower()
        if not _is_sha256(key):
            raise AssetStoreError(f"not a valid sha256 handle: {sha256!r}")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                self._backend_stream_into(key, fh)
            actual, size = _hash_file(tmp)
            if actual != key:
                raise AssetStoreError(
                    f"asset integrity check failed: requested sha256:{key} but streamed bytes "
                    f"hash to sha256:{actual} (corruption)"
                )
            tmp.replace(target)
            return size
        finally:
            tmp.unlink(missing_ok=True)

    def create_upload_url(self, *, ttl_seconds: int = 3600, content_type: str | None = None) -> dict[str, object]:
        """Mint a pre-authorized WRITE URL for an out-of-band upload to a staging blob.

        Large inbound binaries can't ride the MCP tool-argument channel (the ~140 KB
        ceiling), and the sandboxed agent can't egress to blob storage — so the
        *user's browser* PUTs the bytes to ``upload_url`` directly, then
        :meth:`finalize_upload` hashes them, moves them into the content-addressed
        store at key = sha256, and deletes the staging blob. Returns
        ``{upload_url, blob_key, expires_at}``; ``blob_key`` is the opaque staging
        handle to pass back to :meth:`finalize_upload`.
        """
        blob_key = secrets.token_hex(16)
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            staging = Path(local_dir) / _STAGING_PREFIX
            staging.mkdir(parents=True, exist_ok=True)
            dest = staging / blob_key
            return {"upload_url": dest.as_uri(), "blob_key": blob_key, "expires_at": expires_at}

        svc, account_name = build_azure_service()
        if svc is None:
            raise AssetStoreError(
                "no asset-store backend configured for an upload session. Set "
                "ASSET_STORE_LOCAL_DIR (tests/dev), ASSET_STORE_ACCOUNT (managed "
                "identity), or ASSET_STORE_CONNECTION_STRING."
            )
        # content_type is recorded by the caller (broker); the write SAS itself does
        # not pin it — the uploader sets Content-Type on its PUT.
        _ = content_type
        upload_url = _azure_write_sas(svc, account_name, blob_key, ttl_seconds=ttl_seconds)
        return {"upload_url": upload_url, "blob_key": blob_key, "expires_at": expires_at}

    def finalize_upload(
        self,
        blob_key: str,
        *,
        expected_sha256: str | None = None,
        asset_class: str | None = None,
        scope: str | None = None,
    ) -> tuple[str, int]:
        """Move a staged out-of-band upload into the content-addressed store.

        Reads the staging blob at ``blob_key``, hashes it → sha256, stores it under
        that key (PUT-if-absent), deletes the staging blob, and returns
        ``(sha256, size)``. Raises ``AssetStoreError`` if the staged blob is missing,
        empty, or (when ``expected_sha256`` is given) does not match the digest.

        ``asset_class`` is reconciled exactly as :meth:`put` reconciles it — same
        monotonic rule, same helper. The browser-upload path never calls ``put``,
        so without this keyword every handle minted through an upload link would
        reach the store untagged and outside the lifecycle rule's reach.
        With a verified ``scope``, persist the binding before deleting the upload
        so a failed binding write can be retried against the same staged bytes.
        """
        if expected_sha256 is not None and not _is_sha256(expected_sha256.lower()):
            raise AssetStoreError(f"not a valid sha256 handle: {expected_sha256!r}")
        _validate_asset_class(asset_class)
        # Stream the staged blob to a temp file (bounded memory, ORG-PLAN-592 A2):
        # finalize used to read the whole upload into memory.
        with self._staging_spool(blob_key) as spooled:
            if spooled is None:
                raise AssetStoreError(f"staged upload {blob_key!r} is missing or empty — was the blob uploaded?")
            key, size = _hash_file(spooled)
            if size == 0:
                raise AssetStoreError(f"staged upload {blob_key!r} is missing or empty — was the blob uploaded?")
            if expected_sha256 is not None and key != expected_sha256.lower():
                raise AssetStoreError(
                    f"staged upload {blob_key!r} hashes to sha256:{key} but expected "
                    f"sha256:{expected_sha256.lower()} (corrupt or wrong upload)"
                )
            access_key = _access_key(scope, key) if scope is not None else None
            self._backend_put_file(key, spooled, size)
            self._reconcile_class(key, asset_class)
            if access_key is not None:
                self._write_access_marker(access_key)
            if size <= _CACHE_INLINE_MAX:
                raw = spooled.read_bytes()
                self._disk_put(key, raw)
                self._mem_put(key, raw)
        self._staging_delete(blob_key)
        return key, size

    @contextlib.contextmanager
    def _staging_spool(self, blob_key: str) -> Generator[Path | None, None, None]:
        """Yield a local file holding the staged upload (streamed), or ``None``."""
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / _STAGING_PREFIX / blob_key
            yield path if path.is_file() else None
            return
        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for upload finalize")
        fd, tmp_name = tempfile.mkstemp(prefix=".staging-")
        tmp = Path(tmp_name)
        try:
            try:
                with os.fdopen(fd, "wb") as fh:
                    container.get_blob_client(f"{_STAGING_PREFIX}/{blob_key}").download_blob().readinto(fh)
            except Exception:  # noqa: BLE001 — missing staged blob → None (finalize raises)
                yield None
                return
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)

    def _staging_delete(self, blob_key: str) -> None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / _STAGING_PREFIX / blob_key
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        container = self._write_azure_container()
        if container is None:
            return
        try:
            container.get_blob_client(f"{_STAGING_PREFIX}/{blob_key}").delete_blob()
        except Exception:  # noqa: BLE001, S110 — best-effort staging cleanup
            pass

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
            tmp = DISK_CACHE_DIR / f".{key}.{secrets.token_hex(4)}.tmp"
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
            entries = [p for p in DISK_CACHE_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
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

    def _write_access_marker(self, key: str) -> None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / key
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            except OSError as exc:
                raise AssetStoreError("could not persist the asset access binding") from exc
            return
        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for the access binding")
        try:
            container.get_blob_client(key).upload_blob(b"", overwrite=False)
        except Exception as exc:  # noqa: BLE001 - only an existing immutable marker is a success
            if not _blob_already_exists(exc):
                raise AssetStoreError("could not persist the asset access binding") from exc

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
                tmp = d / f".{key}.{secrets.token_hex(4)}.tmp"
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

    def _backend_put_file(self, key: str, src: Path, size: int) -> None:
        """PUT-if-absent from a file, streamed in chunks (never read whole)."""
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            d = Path(local_dir)
            d.mkdir(parents=True, exist_ok=True)
            dest = d / key
            if not dest.exists():
                tmp = d / f".{key}.{secrets.token_hex(4)}.tmp"
                with src.open("rb") as fin, tmp.open("wb") as fout:
                    shutil.copyfileobj(fin, fout, _STREAM_CHUNK)
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
            with src.open("rb") as fh:
                blob.upload_blob(fh, length=size, overwrite=False, max_concurrency=1)
        except Exception as e:  # noqa: BLE001
            if _blob_already_exists(e):
                return
            raise AssetStoreError(f"failed to store sha256:{key} to blob backend: {e}") from e

    def _backend_stream_into(self, key: str, fh: BinaryIO) -> None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            path = Path(local_dir) / key
            if not path.is_file():
                raise AssetStoreError(f"handle sha256:{key} not found in local store {local_dir}.")
            with path.open("rb") as fin:
                shutil.copyfileobj(fin, fh, _STREAM_CHUNK)
            return
        container = self._read_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for a streamed fetch")
        try:
            container.get_blob_client(key).download_blob().readinto(fh)
        except Exception as e:  # noqa: BLE001
            raise AssetStoreError(f"handle sha256:{key} not resolvable from blob store: {e}") from e

    def _backend_exists(self, key: str) -> bool:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            return (Path(local_dir) / key).is_file()

        container = self._read_azure_container()
        if container is None:
            raise AssetStoreError(
                "no asset-store backend configured. Set ASSET_STORE_LOCAL_DIR "
                "(tests/dev), ASSET_STORE_ACCOUNT (managed identity), or "
                "ASSET_STORE_CONNECTION_STRING."
            )
        try:
            return bool(container.get_blob_client(key).exists())
        except Exception as e:  # noqa: BLE001 — a store we cannot reach is NOT "absent"
            raise AssetStoreError(
                f"could not determine whether sha256:{key} is present in the blob "
                f"store: {e}. Refusing to report it absent — absent means 'ask the "
                f"client to upload it again'."
            ) from e

    # -- asset class (blob index tags) ----------------------------------------

    def _reconcile_class(self, key: str, asset_class: str | None) -> None:
        """Apply the monotonic class rule to the blob at ``key``. See :meth:`put`.

        Runs AFTER the PUT-if-absent step, on every call, because ``_backend_put``
        short-circuits on an existing blob — so a tag written only at upload time
        would never reclass a blob the store already holds.

        The read-decide-write is serialised per blob: a file lock on the local
        backend, a short blob lease on Azure (``set_blob_tags`` under that lease),
        retried a bounded number of times. Without it two writers can both read
        "untagged", and the lower class lands last.
        """
        if asset_class is None:
            return
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            with _local_class_lock(Path(local_dir), key):
                existing = self._read_class(key)
                if class_outranks(asset_class, existing):
                    self._write_class(key, asset_class)
            return
        self._reconcile_class_azure(key, asset_class)

    def _reconcile_class_azure(self, key: str, asset_class: str) -> None:
        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for a class write")
        blob = container.get_blob_client(key)
        last: Exception | None = None
        for attempt in range(_TAG_RETRIES):
            try:
                lease = blob.acquire_lease(lease_duration=_TAG_LEASE_SECONDS)
            except Exception as e:  # noqa: BLE001
                if _lease_conflict(e):
                    last = e
                    # Linear backoff with jitter so contending writers do not retry in lockstep.
                    time.sleep(_TAG_BACKOFF_SECONDS * (attempt + 1) * (0.5 + secrets.randbelow(1000) / 1000))
                    continue
                raise AssetStoreError(f"could not lease sha256:{key} to reconcile its class: {e}") from e
            try:
                tags: dict[str, str] = {str(k): str(v) for k, v in dict(blob.get_blob_tags() or {}).items()}
                existing = tags.get(_CLASS_TAG) or None
                if not class_outranks(asset_class, existing):
                    return
                tags[_CLASS_TAG] = asset_class  # merge: unrelated tags are preserved
                try:
                    blob.set_blob_tags(tags, lease=lease)
                except Exception as e:  # noqa: BLE001
                    raise AssetStoreError(
                        f"stored sha256:{key} but failed to write its {_CLASS_TAG}="
                        f"{asset_class!r} index tag: {e}. The blob keeps its previous class "
                        f"({existing!r}) — this must not pass silently."
                    ) from e
                return
            except AssetStoreError:
                raise
            except Exception as e:  # noqa: BLE001
                raise AssetStoreError(f"failed to read the class tag of sha256:{key}: {e}") from e
            finally:
                with contextlib.suppress(Exception):
                    lease.release()
        raise AssetStoreError(
            f"could not reconcile the class of sha256:{key}: the blob stayed leased by another "
            f"writer through {_TAG_RETRIES} attempts ({last})"
        )

    def _read_class(self, key: str) -> str | None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            sidecar = Path(local_dir) / f"{key}{_CLASS_SIDECAR_SUFFIX}"
            if not sidecar.is_file():
                return None
            value = sidecar.read_text(encoding="utf-8").strip()
            return value or None

        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for a class read")
        try:
            tags: dict[str, Any] = dict(container.get_blob_client(key).get_blob_tags() or {})
        except Exception as e:  # noqa: BLE001
            raise AssetStoreError(f"failed to read the class tag of sha256:{key}: {e}") from e
        value: Any = tags.get(_CLASS_TAG)
        return str(value) if value else None

    def _write_class(self, key: str, asset_class: str) -> None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            d = Path(local_dir)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{key}{_CLASS_SIDECAR_SUFFIX}.{secrets.token_hex(4)}.tmp"
            tmp.write_text(asset_class, encoding="utf-8")
            tmp.replace(d / f"{key}{_CLASS_SIDECAR_SUFFIX}")
            return

        container = self._write_azure_container()
        if container is None:
            raise AssetStoreError("no asset-store backend configured for a class write")
        try:
            container.get_blob_client(key).set_blob_tags({_CLASS_TAG: asset_class})
        except Exception as e:  # noqa: BLE001
            # Deliberately NOT best-effort. A blob stored without its class is an
            # un-expirable orphan, and silence here is how the tag drifts from the
            # ledger that believes it was written.
            raise AssetStoreError(
                f"stored sha256:{key} but failed to write its {_CLASS_TAG}="
                f"{asset_class!r} index tag: {e}. The blob is now untagged and "
                f"outside the lifecycle rule — this must not pass silently."
            ) from e

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


def _hash_file(path: Path) -> tuple[str, int]:
    """Stream ``path`` through sha256 in bounded chunks; return ``(hexdigest, size)``."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_STREAM_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


@contextlib.contextmanager
def _local_class_lock(root: Path, key: str) -> Generator[None, None, None]:
    """Exclusive per-blob lock for the local backend's class sidecar."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{key}{_CLASS_SIDECAR_SUFFIX}.lock"
    with lock_path.open("a+") as fh:
        try:
            import fcntl  # POSIX; the local backend is a dev/test backend

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX dev box
            pass
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _lease_conflict(exc: Exception) -> bool:
    """True when a lease could not be taken because another writer holds one."""
    text = f"{type(exc).__name__} {exc}"
    return "LeaseAlreadyPresent" in text or "There is already a lease present" in text


def _blob_already_exists(exc: Exception) -> bool:
    """True if an Azure upload failed only because the blob already exists."""
    name = type(exc).__name__
    return "BlobAlreadyExists" in str(exc) or name in {"ResourceExistsError", "ResourceModifiedError"}


def build_azure_service(account: str | None = None) -> tuple[Any, str | None]:
    """Build ``(BlobServiceClient, account_name)`` from env, or ``(None, None)``.

    Module-public because the sibling ``publication`` primitive shares the exact
    same backend-selection contract; duplicating the env precedence there would
    be two places to change when a backend is added.

    Shared backend-selection contract: account+managed-identity first, then
    connection string. Azure SDKs are imported lazily so non-Azure paths pay
    nothing and the ``azure`` extra stays optional.

    ``account`` overrides ``ASSET_STORE_ACCOUNT`` for callers that own a
    *different* storage account and must not be steered by this library's env
    names. A hosted workflow runner is the motivating case: its outputs live on
    the workflow data-plane account, while ``ASSET_STORE_ACCOUNT`` names the
    content-addressed brand-asset account. Reading the env var there would send
    published run artifacts to the wrong account — or, worse, silently succeed
    today and break whenever the same process also reads a brand asset.
    """
    account = account or os.environ.get("ASSET_STORE_ACCOUNT")
    conn = os.environ.get("ASSET_STORE_CONNECTION_STRING")
    if not account and not conn:
        return None, None

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
        return svc, account
    if conn:
        svc = BlobServiceClient.from_connection_string(conn)
        return svc, svc.account_name
    return None, None  # unreachable: guarded above


def _build_azure_container(*, read_only: bool) -> Any:
    """Build an Azure container client from env, or return None if unconfigured.

    The write path (``read_only=False``) ensures the container exists.
    """
    svc, _ = build_azure_service()
    if svc is None:
        return None
    container_name = os.environ.get("ASSET_STORE_CONTAINER", DEFAULT_CONTAINER)
    container = svc.get_container_client(container_name)
    if not read_only:
        try:
            container.create_container()
        except Exception:  # noqa: BLE001, S110 — already exists is the common case
            pass
    return container


def _azure_write_sas(svc: Any, account_name: str | None, blob_key: str, *, ttl_seconds: int) -> str:
    """Mint a short-lived WRITE SAS URL to a staging blob.

    User-delegation SAS for the managed-identity backend (no account key on disk),
    account-key SAS for a connection string — mirroring the outbound delivery SAS.
    The user's browser PUTs to it; ``finalize_upload`` then content-addresses the
    bytes.
    """
    if account_name is None:
        raise AssetStoreError("could not resolve the storage account name for an upload SAS")
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas  # lazy

    container_name = os.environ.get("ASSET_STORE_CONTAINER", DEFAULT_CONTAINER)
    blob_name = f"{_STAGING_PREFIX}/{blob_key}"
    container = svc.get_container_client(container_name)
    try:
        container.create_container()
    except Exception:  # noqa: BLE001, S110 — already exists is the common case
        pass
    blob = container.get_blob_client(blob_name)

    start = datetime.now(UTC) - timedelta(minutes=5)
    expiry = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    permission = BlobSasPermissions(write=True, create=True)

    account_key = getattr(getattr(svc, "credential", None), "account_key", None)
    if account_key:
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=permission,
            expiry=expiry,
            start=start,
        )
    else:
        udk = svc.get_user_delegation_key(start, expiry)
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            user_delegation_key=udk,
            permission=permission,
            expiry=expiry,
            start=start,
        )
    return f"{blob.url}?{sas}"
