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


# -- out-of-band upload session (large binaries, no inline base64) -----------------


def _staging_path(upload_url: str):
    from pathlib import Path as _Path
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    return _Path(url2pathname(urlparse(upload_url).path))


def test_upload_session_roundtrip(store):
    s, backend = store
    session = s.create_upload_url()
    assert session["upload_url"].startswith("file://")
    assert session["blob_key"] and session["expires_at"]
    # Simulate the user's browser PUT: write bytes to the pre-authorized URL.
    raw = b"large-brand-photo-\xff\x00" * 1000
    _staging_path(str(session["upload_url"])).write_bytes(raw)

    sha, size = s.finalize_upload(str(session["blob_key"]))
    assert sha == hashlib.sha256(raw).hexdigest()
    assert size == len(raw)
    # The finalized blob is content-addressed in the backend and resolves via fetch.
    assert (backend / sha).read_bytes() == raw
    assert s.fetch(sha) == raw


def test_finalize_upload_verifies_expected_digest(store):
    s, _ = store
    session = s.create_upload_url()
    _staging_path(str(session["upload_url"])).write_bytes(b"the-actual-bytes")
    wrong = hashlib.sha256(b"different").hexdigest()
    with pytest.raises(A.AssetStoreError, match="expected"):
        s.finalize_upload(str(session["blob_key"]), expected_sha256=wrong)


def test_finalize_upload_missing_blob_raises(store):
    s, _ = store
    session = s.create_upload_url()  # never uploaded
    with pytest.raises(A.AssetStoreError, match="missing or empty"):
        s.finalize_upload(str(session["blob_key"]))


def test_create_upload_url_no_backend_raises(tmp_path, monkeypatch):
    for var in ("ASSET_STORE_LOCAL_DIR", "ASSET_STORE_ACCOUNT", "ASSET_STORE_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")
    with pytest.raises(A.AssetStoreError, match="no asset-store backend"):
        A.AssetStore().create_upload_url()


# -- exists (pre-flight de-duplication) --------------------------------------------


def test_exists_true_for_stored_blob(store):
    s, _ = store
    sha = s.put(b"already-here")
    assert s.exists(sha) is True


def test_exists_false_for_absent_blob(store):
    s, _ = store
    absent = hashlib.sha256(b"never-stored").hexdigest()
    assert s.exists(absent) is False


def test_exists_never_downloads(store, monkeypatch):
    """A cache-cold hit must answer from the backend's existence probe, not a fetch."""
    s, backend = store
    raw = b"big-blob" * 1000
    sha = _put_raw(backend, raw)
    (A.DISK_CACHE_DIR / sha).unlink(missing_ok=True)
    monkeypatch.setattr(A.AssetStore, "_backend_fetch", lambda *_a, **_k: pytest.fail("exists() downloaded"))
    assert s.exists(sha) is True


def test_exists_invalid_handle_raises(store):
    s, _ = store
    with pytest.raises(ValueError, match="not a valid sha256"):
        s.exists("not-a-digest")


def test_exists_raises_on_backend_error(tmp_path, monkeypatch):
    """A store we cannot reach must NOT read as 'absent'.

    Absent means "ask the client to upload it again"; answering False on a
    transport failure is how a 13 MB re-upload gets requested for a blob we hold.
    """
    monkeypatch.delenv("ASSET_STORE_LOCAL_DIR", raising=False)
    monkeypatch.setenv("ASSET_STORE_ACCOUNT", "ststromybrandassets")
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")

    class _Boom:
        def exists(self) -> bool:
            raise RuntimeError("transient network failure")

    class _Container:
        def get_blob_client(self, _key: str) -> _Boom:
            return _Boom()

    s = A.AssetStore()
    monkeypatch.setattr(A.AssetStore, "_read_azure_container", lambda _self: _Container())
    sha = hashlib.sha256(b"whatever").hexdigest()
    with pytest.raises(A.AssetStoreError, match="Refusing to report it absent"):
        s.exists(sha)


# -- asset class (monotonic; blob index tag / local sidecar) -----------------------


def _class_of(backend, sha: str) -> str | None:
    p = backend / f"{sha}.class"
    return p.read_text().strip() if p.is_file() else None


def test_put_without_class_writes_no_class(store):
    s, backend = store
    sha = s.put(b"an-unclassified-render-artifact")
    assert _class_of(backend, sha) is None


def test_put_writes_class(store):
    s, backend = store
    sha = s.put(b"a-source-deck-screenshot", asset_class="deliverable")
    assert _class_of(backend, sha) == "deliverable"


def test_class_upgrade(store):
    """deliverable -> brand upgrades, ON A BLOB THE STORE ALREADY HOLDS.

    put() is PUT-if-absent, so the second call short-circuits the upload. If the
    tag rode the upload it would never be written, and Use Case 5 — the client's
    own logo, first seen inside their source deck — would keep `deliverable` and
    be deleted on day 90.
    """
    s, backend = store
    raw = b"the-clients-own-logo"
    sha = s.put(raw, asset_class="deliverable")
    assert _class_of(backend, sha) == "deliverable"
    assert s.put(raw, asset_class="brand") == sha
    assert _class_of(backend, sha) == "brand"


def test_class_no_downgrade(store):
    """brand -> deliverable is refused silently, on an existing blob."""
    s, backend = store
    raw = b"a-genuine-brand-asset"
    sha = s.put(raw, asset_class="brand")
    assert _class_of(backend, sha) == "brand"
    assert s.put(raw, asset_class="deliverable") == sha
    assert _class_of(backend, sha) == "brand"


def test_class_reclass_is_idempotent(store):
    s, backend = store
    raw = b"same-class-twice"
    sha = s.put(raw, asset_class="brand")
    s.put(raw, asset_class="brand")
    assert _class_of(backend, sha) == "brand"


def test_class_rejects_unknown(store):
    s, backend = store
    with pytest.raises(ValueError, match="unknown asset_class"):
        s.put(b"some-bytes", asset_class="whatever")
    # Refused before the write: nothing was stored under a bogus class.
    assert list(backend.glob("*.class")) == []


def test_finalize_upload_writes_class(store):
    s, backend = store
    session = s.create_upload_url()
    _staging_path(str(session["upload_url"])).write_bytes(b"uploaded-source-deck")
    sha, _size = s.finalize_upload(str(session["blob_key"]), asset_class="deliverable")
    assert _class_of(backend, sha) == "deliverable"


def test_finalize_upload_upgrades_class(store):
    s, backend = store
    raw = b"logo-first-seen-in-a-deck"
    sha = s.put(raw, asset_class="deliverable")
    session = s.create_upload_url()
    _staging_path(str(session["upload_url"])).write_bytes(raw)
    assert s.finalize_upload(str(session["blob_key"]), asset_class="brand")[0] == sha
    assert _class_of(backend, sha) == "brand"


def test_finalize_upload_rejects_unknown_class(store):
    s, _ = store
    session = s.create_upload_url()
    _staging_path(str(session["upload_url"])).write_bytes(b"bytes")
    with pytest.raises(ValueError, match="unknown asset_class"):
        s.finalize_upload(str(session["blob_key"]), asset_class="rubbish")
