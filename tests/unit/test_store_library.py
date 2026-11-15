"""ORG-PLAN-592 A2: the durable `library` class, monotonic + concurrent-safe tags, streaming I/O.

Local-backend cases exercise the sidecar path; a small in-memory fake of the Azure
container (with real lease semantics) exercises the index-tag path, which the
original suite could not reach.
"""

from __future__ import annotations

import hashlib
import io
import random
import threading
import tracemalloc
import uuid

import pytest

from stromy_asset_transport import store as A

# --------------------------------------------------------------------- fixtures


@pytest.fixture
def local(tmp_path, monkeypatch):
    backend = tmp_path / "store"
    backend.mkdir()
    monkeypatch.setenv("ASSET_STORE_LOCAL_DIR", str(backend))
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")
    return A.AssetStore(), backend


class LeaseConflict(Exception):
    def __init__(self) -> None:
        super().__init__("LeaseAlreadyPresent: There is already a lease present.")


class LeaseMissing(Exception):
    pass


class _Lease:
    def __init__(self, blob: FakeBlob, lid: str) -> None:
        self.blob, self.id = blob, lid

    def release(self) -> None:
        with self.blob.c.lock:
            if self.blob.c.leases.get(self.blob.key) == self.id:
                del self.blob.c.leases[self.blob.key]


class FakeBlob:
    def __init__(self, c: FakeContainer, key: str) -> None:
        self.c, self.key = c, key

    def exists(self) -> bool:
        return self.key in self.c.data

    def upload_blob(self, data, overwrite=False, length=None, max_concurrency=1):
        raw = data.read() if hasattr(data, "read") else bytes(data)
        with self.c.lock:
            if self.key in self.c.data and not overwrite:
                raise Exception("BlobAlreadyExists")
            self.c.data[self.key] = raw

    def download_blob(self):
        raw = self.c.data[self.key]

        class _D:
            def readall(self_inner):
                return raw

            def readinto(self_inner, fh):
                for i in range(0, len(raw), 1024):
                    fh.write(raw[i : i + 1024])
                return len(raw)

        return _D()

    def get_blob_properties(self):
        return type("P", (), {"size": len(self.c.data[self.key])})()

    def get_blob_tags(self):
        return dict(self.c.tags.get(self.key, {}))

    def acquire_lease(self, lease_duration=15):
        with self.c.lock:
            if self.c.fail_leases > 0:
                self.c.fail_leases -= 1
                raise LeaseConflict()
            if self.key in self.c.leases:
                raise LeaseConflict()
            lid = uuid.uuid4().hex
            self.c.leases[self.key] = lid
            return _Lease(self, lid)

    def set_blob_tags(self, tags, lease=None):
        with self.c.lock:
            held = self.c.leases.get(self.key)
            if held and (lease is None or lease.id != held):
                raise LeaseMissing("LeaseIdMissing")
            self.c.tags[self.key] = dict(tags)
            self.c.tag_writes += 1

    def delete_blob(self):
        self.c.data.pop(self.key, None)


class FakeContainer:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.leases: dict[str, str] = {}
        self.lock = threading.RLock()
        self.fail_leases = 0
        self.tag_writes = 0

    def get_blob_client(self, key: str) -> FakeBlob:
        return FakeBlob(self, key)


@pytest.fixture
def azure(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSET_STORE_LOCAL_DIR", raising=False)
    monkeypatch.setattr(A, "DISK_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(A, "_TAG_BACKOFF_SECONDS", 0.0)
    fake = FakeContainer()

    def make() -> A.AssetStore:
        s = A.AssetStore()
        s._write_container, s._write_checked = fake, True
        s._read_container, s._read_checked = fake, True
        return s

    return make, fake


def _class_local(backend, sha):
    p = backend / f"{sha}.class"
    return p.read_text().strip() if p.exists() else None


# --------------------------------------------------------------------- precedence


def test_library_is_a_known_class_and_ranks_highest():
    assert "library" in A.ASSET_CLASSES
    assert A.CLASS_RANK["library"] > A.CLASS_RANK["brand"] > A.CLASS_RANK["deliverable"]


@pytest.mark.parametrize(
    "new,existing,expected",
    [
        ("library", None, True),
        ("library", "brand", True),
        ("library", "deliverable", True),
        ("brand", "library", False),
        ("deliverable", "library", False),
        ("library", "library", False),
        ("brand", "archival-v2", False),  # a class from a newer release is never overwritten
        (None, "library", False),
    ],
)
def test_class_outranks(new, existing, expected):
    assert A.class_outranks(new, existing) is expected


def test_local_library_tag_written_and_never_downgraded(local):
    s, backend = local
    raw = b"a-library-original"
    sha = s.put(raw, asset_class="library")
    assert _class_local(backend, sha) == "library"
    s.put(raw, asset_class="brand")
    s.put(raw, asset_class="deliverable")
    assert _class_local(backend, sha) == "library"


def test_local_finalize_cannot_downgrade_library(local):
    s, backend = local
    raw = b"a-library-original-via-upload"
    sha = s.put(raw, asset_class="library")
    session = s.create_upload_url()
    (backend / A._STAGING_PREFIX / str(session["blob_key"])).write_bytes(raw)
    assert s.finalize_upload(str(session["blob_key"]), asset_class="deliverable")[0] == sha
    assert _class_local(backend, sha) == "library"


def test_local_upgrade_chain_reaches_library(local):
    s, backend = local
    raw = b"first-seen-in-a-source-deck"
    sha = s.put(raw, asset_class="deliverable")
    s.put(raw, asset_class="brand")
    assert _class_local(backend, sha) == "brand"
    s.put(raw, asset_class="library")
    assert _class_local(backend, sha) == "library"


def test_local_concurrent_writers_cannot_downgrade(local):
    s, backend = local
    raw = b"contended-bytes"
    sha = hashlib.sha256(raw).hexdigest()
    order = ["deliverable", "brand", "library"] * 20
    random.Random(7).shuffle(order)  # noqa: S311 - test ordering, not crypto
    errors: list[BaseException] = []

    def worker(cls: str) -> None:
        try:
            A.AssetStore().put(raw, asset_class=cls)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(c,)) for c in order]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert _class_local(backend, sha) == "library"


# --------------------------------------------------------------------- azure path


def test_azure_library_tag_and_unrelated_tags_preserved(azure):
    make, fake = azure
    s = make()
    raw = b"azure-library"
    sha = hashlib.sha256(raw).hexdigest()
    fake.data[sha] = raw
    fake.tags[sha] = {"owner": "ops", "asset_class": "brand"}
    s.put(raw, asset_class="library")
    assert fake.tags[sha] == {"owner": "ops", "asset_class": "library"}
    assert not fake.leases  # lease released


@pytest.mark.parametrize("lower", ["brand", "deliverable"])
def test_azure_put_and_finalize_refuse_downgrade(azure, lower):
    make, fake = azure
    s = make()
    raw = b"azure-promoted"
    sha = s.put(raw, asset_class="library")
    writes = fake.tag_writes
    s.put(raw, asset_class=lower)
    fake.data[f"{A._STAGING_PREFIX}/k1"] = raw
    s.finalize_upload("k1", asset_class=lower)
    assert fake.tags[sha]["asset_class"] == "library"
    assert fake.tag_writes == writes  # a refused downgrade writes nothing


def test_azure_concurrent_writers_cannot_downgrade(azure, monkeypatch):
    make, fake = azure
    # 45 simultaneous writers on one blob is far past real contention: give the
    # lease loop a realistic backoff and enough attempts to drain the pile-up.
    monkeypatch.setattr(A, "_TAG_BACKOFF_SECONDS", 0.002)
    monkeypatch.setattr(A, "_TAG_RETRIES", 200)
    raw = b"azure-contended"
    sha = hashlib.sha256(raw).hexdigest()
    fake.data[sha] = raw
    order = ["deliverable", "brand", "library"] * 15
    random.Random(11).shuffle(order)  # noqa: S311 - test ordering, not crypto
    errors: list[BaseException] = []

    def worker(cls: str) -> None:
        try:
            make().put(raw, asset_class=cls)
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(c,)) for c in order]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:1]
    assert fake.tags[sha]["asset_class"] == "library"


def test_azure_lease_conflict_retries_then_succeeds(azure):
    make, fake = azure
    raw = b"briefly-leased"
    sha = hashlib.sha256(raw).hexdigest()
    fake.data[sha] = raw
    fake.fail_leases = A._TAG_RETRIES - 1
    make().put(raw, asset_class="library")
    assert fake.tags[sha]["asset_class"] == "library"


def test_azure_lease_conflict_exhausts_and_raises(azure):
    make, fake = azure
    raw = b"held-by-someone-else"
    fake.data[hashlib.sha256(raw).hexdigest()] = raw
    fake.fail_leases = A._TAG_RETRIES + 5
    with pytest.raises(A.AssetStoreError, match="stayed leased"):
        make().put(raw, asset_class="library")


def test_azure_unknown_newer_class_left_alone(azure):
    make, fake = azure
    raw = b"tagged-by-a-newer-release"
    sha = hashlib.sha256(raw).hexdigest()
    fake.data[sha] = raw
    fake.tags[sha] = {"asset_class": "archival-v2"}
    make().put(raw, asset_class="library")
    assert fake.tags[sha]["asset_class"] == "archival-v2"


# --------------------------------------------------------------------- streaming


def _big_file(tmp_path, mib: int):
    p = tmp_path / "big.bin"
    block = hashlib.sha256(b"seed").digest() * 32768  # 1 MiB
    with p.open("wb") as fh:
        for _ in range(mib):
            fh.write(block)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return p, h.hexdigest()


def test_put_file_streams_with_bounded_memory(local, tmp_path, monkeypatch):
    s, backend = local
    monkeypatch.setattr(A, "_STREAM_CHUNK", 1 << 20)
    monkeypatch.setattr(A, "_CACHE_INLINE_MAX", 1 << 20)
    path, digest = _big_file(tmp_path, 24)
    tracemalloc.start()
    try:
        sha, size = s.put_file(path, asset_class="library")
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (sha, size) == (digest, 24 << 20)
    assert peak < 8 << 20, f"peak {peak} bytes: put_file must not hold the file in memory"
    assert (backend / sha).stat().st_size == size
    assert _class_local(backend, sha) == "library"
    assert s._mem_get(sha) is None  # above the inline-cache ceiling


def test_put_file_refuses_changed_bytes(local, tmp_path):
    s, _ = local
    p = tmp_path / "f.bin"
    p.write_bytes(b"now")
    with pytest.raises(A.AssetStoreError, match="changed after it was inventoried"):
        s.put_file(p, expected_sha256="0" * 64)


def test_put_file_refuses_empty(local, tmp_path):
    s, _ = local
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    with pytest.raises(A.AssetStoreError, match="empty"):
        s.put_file(p)


def test_azure_put_file_and_finalize_stream(azure, tmp_path, monkeypatch):
    make, fake = azure
    monkeypatch.setattr(A, "_CACHE_INLINE_MAX", 1024)
    path, digest = _big_file(tmp_path, 3)
    s = make()
    assert s.put_file(path, asset_class="library") == (digest, 3 << 20)
    assert fake.tags[digest]["asset_class"] == "library"
    fake.data[f"{A._STAGING_PREFIX}/k2"] = path.read_bytes()
    assert s.finalize_upload("k2", expected_sha256=digest, asset_class="brand") == (digest, 3 << 20)
    assert f"{A._STAGING_PREFIX}/k2" not in fake.data
    assert fake.tags[digest]["asset_class"] == "library"


def test_finalize_streams_large_upload_with_bounded_memory(local, tmp_path, monkeypatch):
    s, backend = local
    monkeypatch.setattr(A, "_STREAM_CHUNK", 1 << 20)
    monkeypatch.setattr(A, "_CACHE_INLINE_MAX", 1 << 20)
    path, digest = _big_file(tmp_path, 16)
    session = s.create_upload_url()
    staged = backend / A._STAGING_PREFIX / str(session["blob_key"])
    path.replace(staged)
    tracemalloc.start()
    try:
        sha, size = s.finalize_upload(str(session["blob_key"]), expected_sha256=digest, asset_class="library")
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (sha, size) == (digest, 16 << 20)
    assert peak < 8 << 20
    assert not staged.exists()


# --------------------------------------------------------------------- describe / fetch_to_file


def test_describe_reports_size_and_class(local):
    s, _ = local
    sha = s.put(b"describe-me", asset_class="library")
    assert s.describe(sha) == {"size": 11, "asset_class": "library"}
    assert s.describe("f" * 64) is None
    with pytest.raises(ValueError):
        s.describe("nope")


def test_azure_describe(azure):
    make, fake = azure
    s = make()
    sha = s.put(b"azure-describe", asset_class="library")
    assert s.describe(sha) == {"size": 14, "asset_class": "library"}
    assert s.describe("e" * 64) is None


def test_fetch_to_file_verifies_and_replaces_atomically(local, tmp_path):
    s, backend = local
    sha = s.put(b"recover-me")
    dest = tmp_path / "out" / "copy.bin"
    assert s.fetch_to_file(sha, dest) == 10
    assert dest.read_bytes() == b"recover-me"
    # Corrupt the stored blob: the fetch refuses and leaves the previous copy intact.
    (backend / sha).write_bytes(b"tampered!!")
    with pytest.raises(A.AssetStoreError, match="integrity"):
        s.fetch_to_file(sha, dest)
    assert dest.read_bytes() == b"recover-me"
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".fetch-")]


def test_fetch_to_file_streams_from_azure(azure, tmp_path):
    make, fake = azure
    s = make()
    sha = s.put(b"x" * 5000, asset_class="library")
    dest = tmp_path / "d.bin"
    assert s.fetch_to_file(sha, dest) == 5000
    assert io.BytesIO(dest.read_bytes()).getbuffer().nbytes == 5000


@pytest.mark.parametrize("locked", [True, False])
def test_lock_is_what_prevents_the_interleaved_downgrade(local, monkeypatch, locked):
    """Negative control: widen the read-decide-write window. Without the per-blob
    lock a slow `brand` writer that read "untagged" lands after `library`."""
    import contextlib
    import time as _time

    s, backend = local
    raw = b"interleaved"
    sha = s.put(raw)  # stored, untagged
    real = A.class_outranks
    brand_read = threading.Event()

    def slow(new, existing):
        if new == "brand":
            brand_read.set()
            _time.sleep(0.2)
        return real(new, existing)

    monkeypatch.setattr(A, "class_outranks", slow)
    if not locked:
        monkeypatch.setattr(A, "_local_class_lock", lambda *_a: contextlib.nullcontext())
    t = threading.Thread(target=lambda: A.AssetStore().put(raw, asset_class="brand"))
    t.start()
    brand_read.wait(2)
    A.AssetStore().put(raw, asset_class="library")
    t.join()
    assert _class_local(backend, sha) == ("library" if locked else "brand")
