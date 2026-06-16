"""AssetStore unit tests (local-fs backend) — fetch + put.

The local-fs backend exercises the full resolution + integrity + cache + write
path without Azure. Azure is behind the same interface and not unit-tested here.
Ported from stromy-format-mcp's test_asset_store.py + new put() coverage.
"""

from __future__ import annotations

import hashlib

import pytest

from stromy_asset_transport import store as A


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An AssetStore wired to a fresh local backend + isolated disk cache."""
    backend = tmp_path / "store"
    backend.mkdir()
    cache = tmp_path / "cache"
    monkeypatch.setenv("ASSET_STORE_LOCAL_DIR", str(backend))
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", cache)
    return A.AssetStore(), backend


def _put_raw(backend, raw: bytes) -> str:
    sha = hashlib.sha256(raw).hexdigest()
    (backend / sha).write_bytes(raw)
    return sha


# -- fetch ---------------------------------------------------------------------


def test_roundtrip(store):
    s, backend = store
    raw = b"font-bytes-\x00\x01\x02" * 100
    sha = _put_raw(backend, raw)
    assert s.fetch(sha) == raw
    # Second fetch hits the in-process cache and is still identical.
    assert s.fetch(sha) == raw


def test_disk_cache_survives_new_instance(store):
    s, backend = store
    raw = b"cached-bytes"
    sha = _put_raw(backend, raw)
    assert s.fetch(sha) == raw  # populates disk cache
    # Remove the backend file; a fresh store must still resolve from disk cache.
    (backend / sha).unlink()
    s2 = A.AssetStore()
    assert s2.fetch(sha) == raw


def test_tampered_blob_raises(store):
    s, backend = store
    raw = b"the-real-bytes"
    sha = hashlib.sha256(raw).hexdigest()
    # Write WRONG bytes under the correct key — integrity check must catch it.
    (backend / sha).write_bytes(b"poisoned")
    with pytest.raises(A.AssetStoreError, match="integrity"):
        s.fetch(sha)


def test_missing_handle_raises(store):
    s, _ = store
    sha = hashlib.sha256(b"never-stored").hexdigest()
    with pytest.raises(A.AssetStoreError, match="sync.sh client-data"):
        s.fetch(sha)


def test_invalid_handle_raises(store):
    s, _ = store
    with pytest.raises(A.AssetStoreError, match="valid sha256"):
        s.fetch("not-a-hash")


def test_no_backend_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSET_STORE_LOCAL_DIR", raising=False)
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")
    s = A.AssetStore()
    sha = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(A.AssetStoreError, match="no asset-store backend"):
        s.fetch(sha)


# -- put -----------------------------------------------------------------------


def test_put_returns_sha_and_writes_backend(store):
    s, backend = store
    raw = b"logo-bytes-\xff\xfe" * 50
    sha = s.put(raw)
    assert sha == hashlib.sha256(raw).hexdigest()
    assert (backend / sha).read_bytes() == raw


def test_put_then_fetch_roundtrips_across_instances(store):
    s, backend = store
    raw = b"reusable-brand-photo"
    sha = s.put(raw)
    # Drop the disk-cache copy so the fresh instance must resolve from the backend.
    (A.DISK_CACHE_DIR / sha).unlink(missing_ok=True)
    s2 = A.AssetStore()
    assert s2.fetch(sha) == raw


def test_put_is_idempotent(store):
    s, backend = store
    raw = b"same-bytes"
    sha1 = s.put(raw)
    sha2 = s.put(raw)
    assert sha1 == sha2
    assert (backend / sha1).read_bytes() == raw


def test_put_empty_raises(store):
    s, _ = store
    with pytest.raises(A.AssetStoreError, match="empty"):
        s.put(b"")


def test_put_no_backend_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSET_STORE_LOCAL_DIR", raising=False)
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")
    s = A.AssetStore()
    with pytest.raises(A.AssetStoreError, match="no asset-store backend"):
        s.put(b"bytes")
