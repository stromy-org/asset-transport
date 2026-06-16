"""Outbound artifact delivery — the write twin of :mod:`store`.

A server renders a large artifact (deck, PDF, video) the remote client cannot
read off the server filesystem, and a multi-MB blob cannot ride home as base64
in the tool-result JSON (the same token ceiling that forced the inbound handle
design). :func:`deliver_artifact` runs one delivery ladder and returns a
:class:`DeliveryResult` describing how the bytes left the server:

    caller-brokered push (``upload_url``)  ->  mode 'pushed'
    inline base64 (size <= ``inline_max``) ->  mode 'inline'
    SharePoint server push (Graph)         ->  mode 'sharepoint'
    Azure Blob + user-delegation SAS URL   ->  mode 'sas'

SharePoint is preferred over SAS for large artifacts because the claude.ai /
Cowork sandbox blocks egress to ``*.blob.core.windows.net`` — a SAS URL is only
reachable from the *user's* browser, while a SharePoint sharing link is durable
and openable anywhere. This is the domain-agnostic extraction of
``stromy-format-mcp``'s ``output_store.py``; the underlying ``push_to_url`` /
``deliver`` / ``deliver_to_sharepoint`` functions are lifted verbatim so the
proven behavior is preserved.

Backends (env, same selection contract as :mod:`store`):
  RENDER_OUTPUT_LOCAL_DIR        -> filesystem (tests/dev); SAS path returns file://
  ASSET_STORE_ACCOUNT            -> Azure Blob via DefaultAzureCredential (upload + SAS)
  ASSET_STORE_CONNECTION_STRING  -> Azure Blob via connection string (account-key SAS)
  RENDER_SHAREPOINT_DRIVE_ID / _SITE_ID -> SharePoint push via managed identity
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .exceptions import DependencyError

DEFAULT_OUTPUT_CONTAINER = "brand-outputs"
# 24h: the SAS link is opened by a *human* (the sandbox can't egress to blob
# storage — only the user's browser can), so the TTL must outlive a working
# session, not a single tool round-trip. Capped well under the 7-day
# user-delegation-key ceiling. Override with RENDER_URL_TTL_SECONDS.
DEFAULT_TTL_SECONDS = 24 * 3600
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Simple Graph PUT to .../content uploads up to 250 MiB; a single PUT is enough
# for any artifact under that (no upload session needed for the server push).
_GRAPH_SIMPLE_PUT_LIMIT = 250 * 1024 * 1024
_MIME_BY_EXT = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
_DEFAULT_MIME = "application/octet-stream"


def _empty_str_list() -> list[str]:
    """Typed default factory for DeliveryResult.warnings (pyright-strict clean)."""
    return []


class OutputStoreError(RuntimeError):
    """Upload or SAS generation failed (no silent success over a missing file)."""


def _mime_for(filename: str) -> str:
    return _MIME_BY_EXT.get(Path(filename).suffix.lower(), _DEFAULT_MIME)


@dataclass
class DeliveryResult:
    """How a delivered artifact left the server.

    Exactly one delivery channel succeeds per call; ``mode`` names it and the
    relevant URL/field is populated. ``warnings`` records any ladder rung that
    was tried and fell through (e.g. a SharePoint push that failed before SAS
    succeeded), so the caller can surface a degraded delivery without failing.
    """

    mode: str  # 'inline' | 'sharepoint' | 'sas' | 'pushed'
    sha256: str
    size: int
    inline_b64: str | None = None
    download_url: str | None = None
    web_url: str | None = None
    destination_url: str | None = None
    destination_item_id: str | None = None
    warnings: list[str] = field(default_factory=_empty_str_list)


def deliver_artifact(
    raw: bytes,
    *,
    filename: str,
    inline_max: int,
    prefer_sharepoint: bool = True,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    upload_url: str | None = None,
    upload_kind: str = "graph-upload-session",
    total_size: int | None = None,
) -> DeliveryResult:
    """Deliver ``raw`` via the best available channel; never raise on a backend miss.

    Ladder: a caller-brokered ``upload_url`` push (if supplied) → inline base64
    when ``size <= inline_max`` → SharePoint push (when ``prefer_sharepoint``) →
    Azure Blob + SAS URL. If every URL backend is unconfigured for an
    over-``inline_max`` artifact, the bytes are returned inline as a last resort
    with a warning (the caller still gets the artifact and can fall back to a
    server-local path). Raises only ``ValueError`` on empty input.
    """
    if not raw:
        raise ValueError("deliver_artifact: refusing to deliver empty bytes")
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    warnings: list[str] = []

    # 1. Caller-brokered push to a pre-authorized URL (e.g. a Graph upload session
    #    minted by the client). The bytes never touch our credentials.
    if upload_url:
        try:
            pushed = push_to_url(raw, upload_url=upload_url, kind=upload_kind, total_size=total_size)
            return DeliveryResult(
                mode="pushed",
                sha256=sha,
                size=size,
                web_url=_as_str(pushed.get("web_url")),
                destination_item_id=_as_str(pushed.get("item_id")),
                warnings=warnings,
            )
        except OutputStoreError as e:
            warnings.append(f"caller-brokered push failed: {e}; falling back to the download ladder")

    # 2. Inline small payloads — cheapest, no backend needed.
    if size <= inline_max:
        return DeliveryResult(
            mode="inline",
            sha256=sha,
            size=size,
            inline_b64=base64.b64encode(raw).decode("ascii"),
            warnings=warnings,
        )

    # 3. SharePoint server push — preferred for large artifacts (durable link,
    #    reachable from a sandbox that blocks blob egress).
    if prefer_sharepoint:
        try:
            sp = deliver_to_sharepoint(raw, filename=filename)
        except OutputStoreError as e:
            warnings.append(f"sharepoint push failed: {e}")
            sp = None
        if sp is not None:
            return DeliveryResult(
                mode="sharepoint",
                sha256=sha,
                size=size,
                web_url=_as_str(sp.get("web_url")),
                destination_url=_as_str(sp.get("drive_item_web_url")),
                destination_item_id=_as_str(sp.get("item_id")),
                warnings=warnings,
            )

    # 4. Azure Blob upload + short-lived SAS download URL.
    try:
        sas = deliver(raw, sha=sha, filename=filename, ttl_seconds=ttl_seconds)
    except OutputStoreError as e:
        warnings.append(f"blob/SAS delivery failed: {e}")
        sas = None
    if sas is not None:
        return DeliveryResult(
            mode="sas",
            sha256=sha,
            size=size,
            download_url=_as_str(sas.get("download_url")),
            warnings=warnings,
        )

    # 5. No URL backend produced a link for an over-ceiling artifact. Return the
    #    bytes inline as a last resort with a loud warning — the caller always
    #    gets the artifact and can substitute a server-local path if it has one.
    warnings.append(
        f"no delivery backend configured; returning {size} bytes inline despite "
        f"exceeding inline_max={inline_max}"
    )
    return DeliveryResult(
        mode="inline",
        sha256=sha,
        size=size,
        inline_b64=base64.b64encode(raw).decode("ascii"),
        warnings=warnings,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ── caller-brokered push ──────────────────────────────────────────────────────


def push_to_url(
    raw: bytes,
    *,
    upload_url: str,
    kind: str = "graph-upload-session",
    total_size: int | None = None,
) -> dict[str, object]:
    """PUT bytes to a pre-authorized upload URL without our own credentials."""
    size = total_size if total_size is not None else len(raw)
    if size != len(raw):
        raise OutputStoreError(
            f"push_to_url size mismatch: payload is {len(raw)} bytes but total_size={size}"
        )

    parsed = urllib_parse.urlparse(upload_url)
    host = parsed.netloc or "unknown-host"

    if kind == "graph-upload-session":
        single_put_limit = 60 * 1024 * 1024
        if size > single_put_limit:
            raise OutputStoreError(
                "graph-upload-session single PUT limit exceeded; chunked upload is required "
                f"for files over {single_put_limit // (1024 * 1024)} MiB"
            )
        headers = {
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }
    elif kind == "presigned-put":
        headers = {"Content-Length": str(size)}
        if host.endswith(".blob.core.windows.net"):
            headers["x-ms-blob-type"] = "BlockBlob"
    else:
        raise OutputStoreError(f"unsupported deliver_to kind '{kind}'")

    req = urllib_request.Request(upload_url, data=raw, headers=headers, method="PUT")  # noqa: S310
    try:
        with urllib_request.urlopen(req) as resp:  # noqa: S310
            status = getattr(resp, "status", resp.getcode())
            body = resp.read()
    except urllib_error.HTTPError as exc:
        raise OutputStoreError(f"{kind} PUT to {host} returned HTTP {exc.code}") from exc
    except urllib_error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise OutputStoreError(f"{kind} PUT to {host} failed: {reason}") from exc

    result: dict[str, object] = {"delivered_via": kind, "status_code": status}
    if body:
        try:
            payload: object = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            payload_d = cast("dict[str, object]", payload)
            web_url = payload_d.get("webUrl")
            item_id = payload_d.get("id")
            if web_url:
                result["web_url"] = web_url
            if item_id:
                result["item_id"] = item_id
    return result


# ── Azure Blob upload + SAS download URL ──────────────────────────────────────


def deliver(
    raw: bytes, *, sha: str, filename: str, ttl_seconds: int | None = None
) -> dict[str, object] | None:
    """Upload bytes to the outputs container; return a download descriptor, or ``None``.

    Returns ``{"download_url", "url_expires_at", "blob"}`` on success. Returns
    ``None`` when no output backend is configured (the caller then falls back to
    a server-local path, dev only). Raises ``OutputStoreError`` only when a
    configured backend genuinely fails (upload/SAS error).
    """
    # `... or DEFAULT` (not get(key, DEFAULT)) so a present-but-EMPTY env var —
    # how a deploy workflow writes an unset GitHub repo var (FOO=) — falls back
    # to the default instead of crashing int("").
    ttl = int(ttl_seconds or os.environ.get("RENDER_URL_TTL_SECONDS") or DEFAULT_TTL_SECONDS)
    ext = Path(filename).suffix or ".bin"
    blob_name = f"{sha}{ext}"

    local_dir = os.environ.get("RENDER_OUTPUT_LOCAL_DIR")
    if local_dir:
        path = Path(local_dir)
        path.mkdir(parents=True, exist_ok=True)
        dest = path / blob_name
        dest.write_bytes(raw)
        expiry = datetime.now(UTC) + timedelta(seconds=ttl)
        return {
            "download_url": dest.as_uri(),
            "url_expires_at": expiry.isoformat(),
            "blob": blob_name,
        }

    account = os.environ.get("ASSET_STORE_ACCOUNT")
    conn = os.environ.get("ASSET_STORE_CONNECTION_STRING")
    if not account and not conn:
        return None

    return _azure_deliver(
        raw, blob_name=blob_name, filename=filename, ttl_seconds=ttl, account=account, conn=conn
    )


def _azure_deliver(
    raw: bytes,
    *,
    blob_name: str,
    filename: str,
    ttl_seconds: int,
    account: str | None,
    conn: str | None,
) -> dict[str, object]:
    """Upload to the outputs container and mint a read-only SAS URL.

    Managed-identity backend → **user-delegation SAS** (no account key on disk);
    connection-string backend → account-key SAS. Azure SDKs imported lazily.
    """
    try:
        from azure.storage.blob import (  # lazy
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ModuleNotFoundError as e:
        raise DependencyError("azure", "azure-storage-blob") from e

    container_name = os.environ.get("RENDER_OUTPUT_CONTAINER", DEFAULT_OUTPUT_CONTAINER)
    content_type = _mime_for(filename)

    if account:
        try:
            from azure.identity import DefaultAzureCredential  # lazy
        except ModuleNotFoundError as e:
            raise DependencyError("azure", "azure-identity") from e

        svc = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
        account_name = account
    elif conn:
        svc = BlobServiceClient.from_connection_string(conn)
        account_name = cast(str, svc.account_name)
    else:  # unreachable: deliver() guards the no-backend case before calling
        raise OutputStoreError("no Azure backend configured for SAS delivery")

    container = svc.get_container_client(container_name)
    try:
        container.create_container()
    except Exception:  # noqa: BLE001, S110 - already exists is the common case
        pass

    blob = container.get_blob_client(blob_name)
    try:
        blob.upload_blob(
            raw, overwrite=True, content_settings=ContentSettings(content_type=content_type)
        )
    except Exception as e:  # noqa: BLE001
        raise OutputStoreError(
            f"failed to upload artifact to {container_name}/{blob_name}: {e}"
        ) from e

    # 5-minute backdated start tolerates clock skew between server and storage.
    start = datetime.now(UTC) - timedelta(minutes=5)
    expiry = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    permission = BlobSasPermissions(read=True)

    try:
        if account:
            # Managed identity: mint a user-delegation key, then a SAS signed by it.
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
        else:
            account_key = svc.credential.account_key  # type: ignore[union-attr]
            sas = generate_blob_sas(
                account_name=account_name,
                container_name=container_name,
                blob_name=blob_name,
                account_key=account_key,
                permission=permission,
                expiry=expiry,
                start=start,
            )
    except Exception as e:  # noqa: BLE001
        raise OutputStoreError(
            f"uploaded {container_name}/{blob_name} but failed to mint a SAS URL: {e}"
        ) from e

    return {
        "download_url": f"{blob.url}?{sas}",
        "url_expires_at": expiry.isoformat(),
        "blob": blob_name,
    }


# ── SharePoint server push (preferred for sandbox-unreachable blob egress) ─────


def _graph_token() -> str:
    """Acquire a Graph application token via the app's managed identity."""
    try:
        from azure.identity import DefaultAzureCredential  # lazy
    except ModuleNotFoundError as e:
        raise DependencyError("azure", "azure-identity") from e

    cred = DefaultAzureCredential()
    return cred.get_token("https://graph.microsoft.com/.default").token


def _graph_request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, object]:
    """Issue one Graph request and return the parsed JSON body (``{}`` if empty)."""
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    if data is not None and method in ("POST", "PUT"):
        headers["Content-Length"] = str(len(data))
    req = urllib_request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib_request.urlopen(req) as resp:  # noqa: S310
            body = resp.read()
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500] if hasattr(exc, "read") else ""
        raise OutputStoreError(f"Graph {method} {url} → HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise OutputStoreError(
            f"Graph {method} {url} failed: {getattr(exc, 'reason', exc)}"
        ) from exc
    if not body:
        return {}
    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _safe_segment(name: str) -> str:
    """Sanitise a caller-supplied folder/name into a SharePoint-safe path segment."""
    cleaned = "".join("-" if c in '"*:<>?/\\|' else c for c in (name or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "Deliverables"


def deliver_to_sharepoint(
    raw: bytes,
    *,
    filename: str,
    subfolder: str | None = None,
    content_type: str | None = None,
) -> dict[str, object] | None:
    """Push the artifact to a Stromy SharePoint library; return a durable share link.

    Backend selection (env, mirrors the blob backend's contract):
      RENDER_SHAREPOINT_DRIVE_ID   -> upload straight into this drive (document library)
      RENDER_SHAREPOINT_SITE_ID    -> resolve the site's default drive, then upload
    Optional:
      RENDER_SHAREPOINT_BASE_PATH  -> root folder under the drive (default 'Deliverables')
      RENDER_SHAREPOINT_LINK_SCOPE -> sharing-link scope: 'organization' (default) | 'anonymous'

    Returns ``{"delivered_via","web_url","drive_item_web_url","item_id"}`` on
    success, or ``None`` when no SharePoint backend is configured (caller then
    continues down the download ladder). Raises ``OutputStoreError`` only when a
    *configured* backend genuinely fails.
    """
    drive_id = os.environ.get("RENDER_SHAREPOINT_DRIVE_ID")
    site_id = os.environ.get("RENDER_SHAREPOINT_SITE_ID")
    if not drive_id and not site_id:
        return None

    if len(raw) > _GRAPH_SIMPLE_PUT_LIMIT:
        limit_mib = _GRAPH_SIMPLE_PUT_LIMIT // (1024 * 1024)
        raise OutputStoreError(
            f"artifact is {len(raw) // (1024 * 1024)} MiB; exceeds the {limit_mib} MiB single-PUT "
            "limit for SharePoint server push (upload-session chunking not implemented)"
        )

    token = _graph_token()

    if not drive_id:
        site = _graph_request(f"{_GRAPH_BASE}/sites/{site_id}", token=token)
        resolved_site = site.get("id") or site_id
        drive = _graph_request(f"{_GRAPH_BASE}/sites/{resolved_site}/drive", token=token)
        drive_id = _as_str(drive.get("id"))
        if not drive_id:
            raise OutputStoreError(f"could not resolve a default drive for site {site_id}")

    base = _safe_segment(os.environ.get("RENDER_SHAREPOINT_BASE_PATH") or "Deliverables")
    segments = [base]
    if subfolder:
        segments += [_safe_segment(p) for p in subfolder.split("/") if p.strip()]
    folder_path = "/".join(segments)
    item_path = f"{folder_path}/{_safe_segment(filename)}"

    ctype = content_type or _mime_for(filename)
    encoded_path = urllib_parse.quote(item_path)
    upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/content"
    item = _graph_request(upload_url, token=token, method="PUT", data=raw, content_type=ctype)

    item_id = _as_str(item.get("id"))
    web_url = _as_str(item.get("webUrl"))

    # A driveItem.webUrl is only openable by someone who already has access. The
    # client is external to the Stromy tenant, so mint a sharing link. Best-effort:
    # a failed link mint still returns the (org-internal) webUrl rather than nothing.
    share_url = web_url
    scope = os.environ.get("RENDER_SHAREPOINT_LINK_SCOPE") or "organization"
    if item_id:
        try:
            link = _graph_request(
                f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/createLink",
                token=token,
                method="POST",
                data=json.dumps({"type": "view", "scope": scope}).encode("utf-8"),
                content_type="application/json",
            )
            link_obj = link.get("link")
            link_web = (
                _as_str(cast("dict[str, object]", link_obj).get("webUrl"))
                if isinstance(link_obj, dict)
                else None
            )
            if link_web:
                share_url = link_web
        except OutputStoreError:
            pass  # fall back to the bare driveItem webUrl

    return {
        "delivered_via": "sharepoint-server",
        "web_url": share_url,
        "drive_item_web_url": web_url,
        "item_id": item_id,
    }
