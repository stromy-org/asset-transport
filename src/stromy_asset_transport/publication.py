"""Run-scoped artifact publication — the outbound half for hosted runs.

Why this is not ``AssetStore`` (ORG-PLAN-164 WS4)
--------------------------------------------------
The store is content-addressed and deliberately tenant-blind: its key is
``sha256(bytes)`` and nothing else, which is what lets identical assets across
clients dedupe to one blob. A *published run artifact* has the opposite
requirements — it must be listable per run, stable across a retry, and carry the
logical name the workflow declared ("report_pdf"), none of which a digest key can
express. Bolting run scoping onto the store would break the invariant its own
docstring states. So this is a sibling primitive with its own container.

Why publication and URL-minting are two functions
-------------------------------------------------
``deliver_artifact`` already exists and combines upload with a short-lived SAS.
That is right for a one-shot delivery and wrong here: a hosted run persists its
result and a client may fetch it days later, so a stored SAS would turn a
completed run into an unusable one the moment it expired. Publication therefore
returns a **descriptor with no URL**, and a fresh URL is minted per authorized
read. The split is the point, not an implementation detail.

Idempotence
-----------
Publication is keyed on ``(run_id, logical_name, filename)`` and skips the write
when an object with the same digest is already there. A retry after a failed
registry transaction must adopt the existing object rather than duplicate it —
the run's descriptors are what the client sees, and two objects for one logical
artifact means one of them is unreachable garbage.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .exceptions import DependencyError
from .store import AssetStoreError, build_azure_service

DEFAULT_OUTPUT_CONTAINER = "workflow-outputs"

#: Read URLs are short-lived by default. A client fetches results interactively,
#: and a long-lived URL is an unauthenticated capability sitting in a transcript.
DEFAULT_DOWNLOAD_TTL_SECONDS = int(os.environ.get("WORKFLOW_DOWNLOAD_URL_TTL_SECONDS", "900"))

_SAFE_SEGMENT_RE = re.compile(r"[^a-z0-9._-]+")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class PublishedArtifact:
    """A stable pointer to published bytes. Deliberately carries no URL."""

    run_id: str
    logical_name: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    container: str
    blob_key: str

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "logical_name": self.logical_name,
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "container": self.container,
            "blob_key": self.blob_key,
        }


def _safe_segment(value: str, *, what: str) -> str:
    """Reduce one path segment to a safe, lowercase slug.

    Applied to the logical name and filename because both originate in a
    workflow's own declaration, and a declaration is still authored text — a
    ``../`` in either would place the object outside the run's prefix and let one
    run's publication overwrite another's.
    """
    cleaned = _SAFE_SEGMENT_RE.sub("-", Path(value).name.lower()).strip("-.")
    if not cleaned:
        raise AssetStoreError(f"{what} {value!r} has no usable characters")
    return cleaned


def output_container(container: str | None = None) -> str:
    """Resolve the output container: explicit argument, then env, then default.

    Explicit-first because the two consumers name this account with their own
    server-owned env vars (the runner's ``STROMY_WORKFLOW_*``, the facade's
    ``WORKFLOW_*``). Neither should have to also export this library's names to
    make publication work — that duplication is how the two drift apart.
    """
    if container and container.strip():
        return container.strip()
    return os.environ.get("WORKFLOW_OUTPUT_CONTAINER", DEFAULT_OUTPUT_CONTAINER).strip()


def artifact_blob_key(*, run_id: str, logical_name: str, filename: str) -> str:
    """Stable, listable key for one published artifact.

    ``<run_id>/<logical_name>/<filename>``. Run id first so an operator (or the
    retention pass) can enumerate and delete exactly one run's outputs with a
    prefix query, which a digest-keyed layout cannot do.
    """
    if not _UUID_RE.fullmatch(run_id):
        # Guarded rather than slugified: a run id is server-minted, so a
        # non-UUID here means the caller passed something else entirely, and
        # slugifying it would bury that bug under a plausible-looking key.
        raise AssetStoreError(f"run_id {run_id!r} is not a UUID")
    return f"{run_id}/{_safe_segment(logical_name, what='logical name')}/{_safe_segment(filename, what='filename')}"


def _container_client(*, ensure: bool, account: str | None = None, container: str | None = None) -> tuple[Any, str]:
    svc, _account = build_azure_service(account)
    if svc is None:
        raise AssetStoreError(
            "no storage backend configured for artifact publication. Pass "
            "account=, or set ASSET_STORE_ACCOUNT (managed identity) or "
            "ASSET_STORE_CONNECTION_STRING."
        )
    name = output_container(container)
    client = svc.get_container_client(name)
    if ensure:
        try:
            client.create_container()
        except Exception:  # noqa: BLE001, S110 - already exists is the common case
            pass
    return client, name


def publish_artifact(
    *,
    run_id: str,
    logical_name: str,
    filename: str,
    media_type: str,
    raw: bytes,
    account: str | None = None,
    container: str | None = None,
) -> PublishedArtifact:
    """Publish one artifact and return its stable descriptor (never a URL).

    Idempotent: if the target object already holds these exact bytes, the upload
    is skipped and the same descriptor is returned.

    ``account``/``container`` let a caller name its own storage explicitly rather
    than inherit this library's env names — see ``build_azure_service``.
    """
    if not raw:
        raise AssetStoreError(
            f"refusing to publish empty bytes for {logical_name!r} — an empty "
            "artifact would report success while delivering nothing"
        )
    digest = hashlib.sha256(raw).hexdigest()
    blob_key = artifact_blob_key(run_id=run_id, logical_name=logical_name, filename=filename)

    resolved_container = output_container(container)

    local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
    if local_dir:
        dest = Path(local_dir) / resolved_container / blob_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == digest):
            dest.write_bytes(raw)
    else:
        client, _name = _container_client(ensure=True, account=account, container=resolved_container)
        blob = client.get_blob_client(blob_key)
        if not _already_published(blob, digest):
            try:
                from azure.storage.blob import ContentSettings  # noqa: PLC0415
            except ModuleNotFoundError as exc:
                raise DependencyError("azure", "azure-storage-blob") from exc
            blob.upload_blob(
                raw,
                overwrite=True,
                content_settings=ContentSettings(content_type=media_type),
                # Written on EVERY upload because it is what the idempotence
                # check reads back. Omitting it would make _already_published
                # always return False and silently re-upload on every retry.
                metadata={"sha256": digest, "logical_name": logical_name},
            )

    return PublishedArtifact(
        run_id=run_id,
        logical_name=logical_name,
        filename=filename,
        media_type=media_type,
        size_bytes=len(raw),
        sha256=digest,
        container=resolved_container,
        blob_key=blob_key,
    )


def _already_published(blob: Any, digest: str) -> bool:
    """True when the target already holds exactly these bytes.

    Compares the recorded digest metadata rather than downloading: a re-publish
    check that pulls the whole artifact back would cost more than the write it
    is trying to avoid.
    """
    try:
        props: Any = blob.get_blob_properties()
    except Exception:  # noqa: BLE001 - not found is the ordinary first-publish case
        return False
    metadata: dict[str, str] = dict(getattr(props, "metadata", None) or {})
    return metadata.get("sha256") == digest


def mint_download_url(
    *,
    blob_key: str,
    account: str | None = None,
    container: str | None = None,
    ttl_seconds: int = DEFAULT_DOWNLOAD_TTL_SECONDS,
) -> str:
    """Mint a fresh short-lived READ URL for a published artifact.

    Called per authorized read, never stored. Read permission only — a download
    capability must not also be a write or delete capability.

    ``account``/``container`` name the storage explicitly — see
    ``build_azure_service``.
    """
    svc, account_name = build_azure_service(account)
    if svc is None or account_name is None:
        local_dir = os.environ.get("ASSET_STORE_LOCAL_DIR")
        if local_dir:
            return (Path(local_dir) / output_container(container) / blob_key).as_uri()
        raise AssetStoreError("no storage backend configured to mint a download URL")

    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise DependencyError("azure", "azure-storage-blob") from exc

    container_name = output_container(container)
    blob = svc.get_container_client(container_name).get_blob_client(blob_key)
    start = datetime.now(UTC) - timedelta(minutes=5)
    expiry = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    permission = BlobSasPermissions(read=True)

    account_key = getattr(getattr(svc, "credential", None), "account_key", None)
    if account_key:
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_key,
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
            blob_name=blob_key,
            user_delegation_key=udk,
            permission=permission,
            expiry=expiry,
            start=start,
        )
    return f"{blob.url}?{sas}"
