"""Run-scoped artifact publication (ORG-PLAN-164 WS4).

Exercised against the filesystem backend, which is the same code path minus the
Azure client — key derivation, guards, idempotence and the no-URL contract all
live above the backend split.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stromy_asset_transport.publication import (
    PublishedArtifact,
    artifact_blob_key,
    mint_download_url,
    publish_artifact,
)
from stromy_asset_transport.store import AssetStoreError

RUN = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ASSET_STORE_LOCAL_DIR", str(tmp_path))
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("WORKFLOW_OUTPUT_CONTAINER", "workflow-outputs")
    return tmp_path


# --- key derivation ----------------------------------------------------------


def test_the_key_is_run_scoped_so_one_runs_outputs_can_be_enumerated() -> None:
    assert (
        artifact_blob_key(run_id=RUN, logical_name="report_pdf", filename="report.pdf")
        == f"{RUN}/report_pdf/report.pdf"
    )


@pytest.mark.parametrize(
    "logical_name",
    ["../../etc/passwd", "..", "report/../../escape"],
)
def test_a_traversal_in_the_logical_name_cannot_leave_the_run_prefix(
    logical_name: str,
) -> None:
    """A workflow's declaration is still authored text."""
    try:
        key = artifact_blob_key(
            run_id=RUN, logical_name=logical_name, filename="report.pdf"
        )
    except AssetStoreError:
        return  # refused outright, which is also correct
    assert key.startswith(f"{RUN}/")
    assert ".." not in key.split("/")


def test_a_traversal_in_the_filename_cannot_leave_the_run_prefix() -> None:
    key = artifact_blob_key(
        run_id=RUN, logical_name="report_pdf", filename="../../../evil.pdf"
    )
    assert key == f"{RUN}/report_pdf/evil.pdf"


def test_a_non_uuid_run_id_is_refused_rather_than_slugified() -> None:
    """Slugifying would bury a caller bug under a plausible-looking key."""
    with pytest.raises(AssetStoreError, match="not a UUID"):
        artifact_blob_key(run_id="../other", logical_name="x", filename="y.pdf")


# --- publication -------------------------------------------------------------


def test_publishing_returns_a_descriptor_and_never_a_url() -> None:
    """A stored URL expires; a completed run must not become unfetchable."""
    raw = b"%PDF-1.7 report"
    published = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=raw,
    )

    assert isinstance(published, PublishedArtifact)
    assert published.sha256 == hashlib.sha256(raw).hexdigest()
    assert published.size_bytes == len(raw)
    assert published.container == "workflow-outputs"
    assert published.blob_key == f"{RUN}/report_pdf/report.pdf"
    # The contract that matters: no URL anywhere in the descriptor.
    assert not any(
        isinstance(v, str) and "://" in v for v in published.as_json().values()
    )


def test_the_bytes_actually_land_where_the_descriptor_says(_local_backend: Path) -> None:
    raw = b"%PDF-1.7 report"
    published = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=raw,
    )
    assert (_local_backend / published.container / published.blob_key).read_bytes() == raw


def test_republishing_identical_bytes_is_idempotent(_local_backend: Path) -> None:
    """A retry after a failed registry write must adopt, not duplicate."""
    raw = b"%PDF-1.7 report"
    first = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=raw,
    )
    second = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=raw,
    )
    assert first == second
    container = _local_backend / first.container / RUN / "report_pdf"
    assert [p.name for p in container.iterdir()] == ["report.pdf"]


def test_republishing_changed_bytes_replaces_in_place(_local_backend: Path) -> None:
    """A retry that produced a better report overwrites, never accumulates."""
    publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=b"first",
    )
    second = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=b"second",
    )
    path = _local_backend / second.container / second.blob_key
    assert path.read_bytes() == b"second"
    assert second.sha256 == hashlib.sha256(b"second").hexdigest()


def test_publishing_empty_bytes_is_refused() -> None:
    """An empty artifact reports success while delivering nothing."""
    with pytest.raises(AssetStoreError, match="empty"):
        publish_artifact(
            run_id=RUN,
            logical_name="report_pdf",
            filename="report.pdf",
            media_type="application/pdf",
            raw=b"",
        )


def test_two_runs_never_collide_on_one_object() -> None:
    other = "99999999-8888-7777-6666-555555555555"
    a = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=b"a",
    )
    b = publish_artifact(
        run_id=other,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=b"b",
    )
    assert a.blob_key != b.blob_key


# --- URL minting -------------------------------------------------------------


def test_a_download_url_is_minted_per_read_not_stored(_local_backend: Path) -> None:
    published = publish_artifact(
        run_id=RUN,
        logical_name="report_pdf",
        filename="report.pdf",
        media_type="application/pdf",
        raw=b"%PDF-1.7",
    )
    url = mint_download_url(blob_key=published.blob_key, container=published.container)
    assert url.startswith("file://")
    assert published.blob_key in url
