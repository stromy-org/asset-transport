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

The SharePoint env vars name one deployment-wide destination. A caller that
needs a per-engagement destination passes an explicit :class:`SharePointTarget`,
gated by the ``RENDER_SHAREPOINT_ALLOWED_SITES`` deny-by-default allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .exceptions import DependencyError, StromyAssetTransportError

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
_SHAREPOINT_LOCK_RETRY_DELAYS = (2.0, 5.0, 15.0)
_sleep = time.sleep
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


class GraphRequestError(OutputStoreError):
    """Graph rejected a request, preserving machine-readable failure details."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class SharePointLockedError(GraphRequestError):
    """SharePoint refused an upload because the destination item is locked."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message, status_code=status_code, error_code=error_code)
        self.attempts = attempts


@dataclass(frozen=True)
class SharePointTarget:
    """An explicit, caller-supplied SharePoint destination.

    The env vars (``RENDER_SHAREPOINT_SITE_ID`` / ``_DRIVE_ID`` / ``_BASE_PATH``)
    describe **one** deployment-wide destination. A target overrides them per
    call, so a single server can deliver into per-engagement collaboration
    spaces. This layer stays client-agnostic: it takes a resolved destination as
    an argument and never learns *which client* it belongs to.

    Contract:

    ``site_id``
        Required. The allowlist is matched against this, so a target without it
        is refused (see :func:`_check_target_allowed`). Accepts either Graph
        form: a path ref (``stromy.sharepoint.com:/sites/foo``) or a composite
        id.
    ``drive_id``
        Optional performance shortcut that skips site→drive resolution. It MUST
        belong to ``site_id`` — the allowlist gates the *site*, so a drive id
        naming some other site is a caller bug, not something this layer can
        detect.
    ``base_path``
        Root folder under the drive. ``None`` falls back to the env default
        (``'Deliverables'``); ``''`` means *no* base folder, so ``subfolder``
        resolves straight off the drive root.
    ``link_mode``
        ``'createLink'`` mints a sharing link (correct for an outbox whose
        readers hold no direct permission). ``'webUrl'`` returns the item's own
        URL — correct for a collaboration space, where members already have
        access and a minted anonymous/org link would be both wrong and leaky.
    """

    site_id: str | None = None
    drive_id: str | None = None
    base_path: str | None = None
    link_mode: str = "createLink"


def _normalize_site_ref(ref: str) -> str:
    """Canonicalise a site reference for allowlist comparison."""
    return ref.strip().rstrip("/").lower()


def _allowed_sites() -> set[str]:
    """Parse ``RENDER_SHAREPOINT_ALLOWED_SITES`` into a comparable set.

    Comma-separated. Entries are expected in the path form
    (``host:/sites/name``) — the form the deployment already uses for
    ``RENDER_SHAREPOINT_SITE_ID``. A *composite* Graph site id embeds commas and
    therefore cannot be expressed here; that is deliberate rather than
    unfortunate, because a mangled entry simply fails the match and the target is
    refused (deny-by-default fails closed, never open).
    """
    raw = os.environ.get("RENDER_SHAREPOINT_ALLOWED_SITES") or ""
    return {_normalize_site_ref(part) for part in raw.split(",") if part.strip()}


def _check_target_allowed(target: SharePointTarget) -> None:
    """Gate an explicit target against the site allowlist. Raise if not allowed.

    Deny-by-default: an unset/empty allowlist refuses *every* explicit target and
    leaves only the env default reachable. Raising (rather than silently ignoring
    the target) is the point — ``deliver_artifact`` turns the raise into a warning
    and drops to the SAS rung, so a mis-targeted render is never silently
    delivered to the wrong tenant's space.
    """
    if not target.site_id:
        raise OutputStoreError(
            "SharePoint target must name a site (site_id); a drive-id-only target is "
            "refused because a bare drive id bypasses the site allowlist boundary"
        )
    allowed = _allowed_sites()
    if not allowed:
        raise OutputStoreError(
            "explicit SharePoint target refused: RENDER_SHAREPOINT_ALLOWED_SITES is unset "
            "or empty (deny-by-default; only the env-default destination is reachable)"
        )
    if _normalize_site_ref(target.site_id) not in allowed:
        raise OutputStoreError(
            f"SharePoint target site {target.site_id!r} is not in "
            "RENDER_SHAREPOINT_ALLOWED_SITES; refusing to deliver off-allowlist"
        )


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

    mode: str  # 'inline' | 'sharepoint' | 'sas' | 'pushed' | 'none'
    sha256: str
    size: int
    inline_b64: str | None = None
    download_url: str | None = None
    url_expires_at: str | None = None
    web_url: str | None = None
    destination_url: str | None = None
    destination_item_id: str | None = None
    delivered_via: str | None = None
    failure_code: str | None = None
    retryable: bool = False
    attempts: int | None = None
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
    dual_link: bool = True,
    subfolder: str | None = None,
    sharepoint_target: SharePointTarget | None = None,
) -> DeliveryResult:
    """Deliver ``raw`` via the best available channel; never raise on a backend miss.

    Ladder: a caller-brokered ``upload_url`` push (if supplied) → inline base64
    when ``size <= inline_max`` → SharePoint push (when ``prefer_sharepoint``) →
    Azure Blob + SAS URL. If every URL backend is unconfigured for an
    over-``inline_max`` artifact, the result is ``mode='none'`` (nothing delivered;
    the caller substitutes its own server-local path) — large bytes are **never**
    forced inline past ``inline_max``. Raises only ``ValueError`` on empty input.

    When ``dual_link`` and a pushed artifact also has a blob backend, a short-lived
    SAS ``download_url`` is minted alongside the push destination (best-effort), so
    the caller can offer a browser-openable fallback next to the push target.
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
        except OutputStoreError as e:
            warnings.append(f"caller-brokered push failed: {e}; falling back to the download ladder")
        else:
            result = DeliveryResult(
                mode="pushed",
                sha256=sha,
                size=size,
                web_url=_as_str(pushed.get("web_url")),
                destination_item_id=_as_str(pushed.get("item_id")),
                delivered_via=_as_str(pushed.get("delivered_via")),
                warnings=warnings,
            )
            # Dual link: also mint a SAS download fallback (best-effort) so a pushed
            # artifact still has a browser-openable URL next to its push destination.
            if dual_link:
                try:
                    dl = deliver(raw, sha=sha, filename=filename, ttl_seconds=ttl_seconds)
                except OutputStoreError as e:
                    warnings.append(
                        f"pushed to destination, but the fallback download link could not "
                        f"be minted ({e}); the push destination is fine"
                    )
                    dl = None
                if dl is not None:
                    result.download_url = _as_str(dl.get("download_url"))
                    result.url_expires_at = _as_str(dl.get("url_expires_at"))
            return result

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
            sp = deliver_to_sharepoint(raw, filename=filename, subfolder=subfolder, target=sharepoint_target)
        except SharePointLockedError as e:
            warnings.append(
                "SharePoint kept the destination locked after "
                f"{e.attempts} attempts; keep the local artifact and retry after the editor closes it"
            )
            return DeliveryResult(
                mode="none",
                sha256=sha,
                size=size,
                failure_code="sharepoint_locked",
                retryable=True,
                attempts=e.attempts,
                warnings=warnings,
            )
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
                delivered_via=_as_str(sp.get("delivered_via")),
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
            url_expires_at=_as_str(sas.get("url_expires_at")),
            warnings=warnings,
        )

    # 5. No URL backend produced a link for an over-ceiling artifact. Signal
    #    'none' — the caller substitutes its own server-local path. A large
    #    artifact is never forced inline (that is exactly the token-ceiling blow-up
    #    the handle design exists to prevent).
    warnings.append(
        f"no delivery backend configured for a {size}-byte artifact over "
        f"inline_max={inline_max}; not delivered (use a server-local path)"
    )
    return DeliveryResult(mode="none", sha256=sha, size=size, warnings=warnings)


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
        raise OutputStoreError(f"push_to_url size mismatch: payload is {len(raw)} bytes but total_size={size}")

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


def deliver(raw: bytes, *, sha: str, filename: str, ttl_seconds: int | None = None) -> dict[str, object] | None:
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

    return _azure_deliver(raw, blob_name=blob_name, filename=filename, ttl_seconds=ttl, account=account, conn=conn)


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
        blob.upload_blob(raw, overwrite=True, content_settings=ContentSettings(content_type=content_type))
    except Exception as e:  # noqa: BLE001
        raise OutputStoreError(f"failed to upload artifact to {container_name}/{blob_name}: {e}") from e

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
        raise OutputStoreError(f"uploaded {container_name}/{blob_name} but failed to mint a SAS URL: {e}") from e

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


@dataclass(frozen=True)
class GraphResponse:
    """One Graph HTTP response, retaining what the workspace primitives need.

    ``_graph_request`` (the original write path) only ever needed the parsed JSON
    body, so it discarded status, headers and raw bytes. The immutable workspace
    ledger needs all three: the **status** distinguishes "created" from
    "already exists" without guessing, the **headers** carry ``Retry-After`` on a
    lock/throttle, and the **bytes** are the file content itself.

    Deliberately NOT a generic escape hatch: ``_graph_call`` accepts a fixed set of
    request shapes and no caller-supplied header dict, so no caller can smuggle an
    ``If-Match`` (or any other) header through this layer. Conditional *content*
    replacement is not part of the supported Graph upload contract and is not
    offered here — the ledger is create-only instead.
    """

    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, object]:
        """The parsed JSON object body, or ``{}`` when empty/non-object."""
        if not self.body:
            return {}
        try:
            parsed: object = json.loads(self.body)
        except json.JSONDecodeError:
            return {}
        return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def _graph_call(
    url: str,
    *,
    token: str,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
) -> GraphResponse:
    """Issue one Graph request and return the full response, status included.

    This is the low-level primitive: an HTTP error status is **returned**, not
    raised, because the workspace layer treats several of them as ordinary
    control flow (404 probes for existence, 409 is the create-only conflict,
    423/429 drive the bounded contention retry). Only a transport failure raises.

    :func:`_graph_request` is the raising wrapper the write path uses, so the
    established "any 4xx/5xx is an exception" contract is unchanged there.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    if data is not None and method in ("POST", "PUT"):
        headers["Content-Length"] = str(len(data))
    req = urllib_request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib_request.urlopen(req) as resp:  # noqa: S310
            status = int(getattr(resp, "status", resp.getcode()) or 0)
            return GraphResponse(status=status, headers=dict(resp.headers.items()), body=resp.read())
    except urllib_error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        raw_headers = getattr(exc, "headers", None)
        exc_headers = dict(raw_headers.items()) if raw_headers is not None else {}
        return GraphResponse(status=exc.code, headers=exc_headers, body=body)
    except urllib_error.URLError as exc:
        raise OutputStoreError(f"Graph {method} {url} failed: {getattr(exc, 'reason', exc)}") from exc


def _graph_request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, object]:
    """Issue one Graph request and return the parsed JSON body (``{}`` if empty).

    The write path's helper, now a thin raising wrapper over :func:`_graph_call`.
    Its contract is unchanged — a lock still surfaces as
    :class:`SharePointLockedError` and every other error status as
    :class:`GraphRequestError`, both carrying the machine-readable status/code.
    """
    resp = _graph_call(url, token=token, method=method, data=data, content_type=content_type)
    if resp.status >= 400:
        raise _graph_error_for(resp, method=method, url=url)
    return resp.json


def _graph_error_for(resp: GraphResponse, *, method: str, url: str) -> GraphRequestError:
    """Build the typed error for an error-status Graph response."""
    detail = resp.body.decode("utf-8", "replace")[:500]
    error_code = _graph_error_code(resp.body)
    message = f"Graph {method} {url} → HTTP {resp.status}: {detail}"
    if _is_locked(resp):
        return SharePointLockedError(message, status_code=resp.status, error_code=error_code)
    return GraphRequestError(message, status_code=resp.status, error_code=error_code)


def _is_locked(resp: GraphResponse) -> bool:
    """True when the response is SharePoint lock contention (not mere throttling)."""
    return resp.status == 423 or (_graph_error_code(resp.body) or "").lower() == "resourcelocked"


def _graph_error_code(body: bytes) -> str | None:
    """Extract ``error.code`` from a Graph error body without masking the HTTP error."""
    try:
        parsed: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = cast("dict[str, object]", parsed).get("error")
    if not isinstance(error, dict):
        return None
    code = cast("dict[str, object]", error).get("code")
    return code if isinstance(code, str) else None


def _put_with_lock_retries(
    url: str,
    *,
    token: str,
    data: bytes,
    content_type: str,
) -> dict[str, object]:
    """Retry only SharePoint lock contention, using the bounded protocol delays."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return _graph_request(url, token=token, method="PUT", data=data, content_type=content_type)
        except SharePointLockedError as exc:
            if attempts > len(_SHAREPOINT_LOCK_RETRY_DELAYS):
                raise SharePointLockedError(
                    str(exc),
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    attempts=attempts,
                ) from exc
            _sleep(_SHAREPOINT_LOCK_RETRY_DELAYS[attempts - 1])


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
    target: SharePointTarget | None = None,
) -> dict[str, object] | None:
    """Push the artifact to a Stromy SharePoint library; return a durable share link.

    Backend selection (env, mirrors the blob backend's contract):
      RENDER_SHAREPOINT_DRIVE_ID   -> upload straight into this drive (document library)
      RENDER_SHAREPOINT_SITE_ID    -> resolve the site's default drive, then upload
    Optional:
      RENDER_SHAREPOINT_BASE_PATH  -> root folder under the drive (default 'Deliverables')
      RENDER_SHAREPOINT_LINK_SCOPE -> sharing-link scope: 'organization' (default) | 'anonymous'
      RENDER_SHAREPOINT_ALLOWED_SITES -> comma-separated sites an explicit ``target`` may name

    ``target`` (:class:`SharePointTarget`) overrides the env destination for this
    call and is gated by the allowlist. It **replaces** the env destination
    wholesale rather than merging with it: a target site combined with a leftover
    env ``RENDER_SHAREPOINT_DRIVE_ID`` would otherwise upload to the env drive
    while appearing to honour the target — a silent cross-client mis-delivery.
    With ``target=None`` the behavior is byte-identical to the env-only original.

    Returns ``{"delivered_via","web_url","drive_item_web_url","item_id"}`` on
    success, or ``None`` when no SharePoint backend is configured (caller then
    continues down the download ladder). Raises ``OutputStoreError`` only when a
    *configured* backend genuinely fails, or when ``target`` is off-allowlist.
    """
    if target is not None:
        # Gate first: an off-allowlist target must never reach Graph at all.
        _check_target_allowed(target)
        # A target owns the destination outright — never fall back to env here.
        drive_id = target.drive_id
        site_id = target.site_id
    else:
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

    # `None` -> env default; `''` -> deliberately no base folder (a collaboration
    # space is already scoped by its site, so its tree hangs off the drive root).
    # An *empty env var* stays falsy-defaulted to 'Deliverables' as it always was.
    if target is not None and target.base_path is not None:
        base_raw = target.base_path
    else:
        base_raw = os.environ.get("RENDER_SHAREPOINT_BASE_PATH") or "Deliverables"
    segments = [_safe_segment(base_raw)] if base_raw.strip() else []
    if subfolder:
        segments += [_safe_segment(p) for p in subfolder.split("/") if p.strip()]
    folder_path = "/".join(segments)
    item_path = f"{folder_path}/{_safe_segment(filename)}"

    ctype = content_type or _mime_for(filename)
    encoded_path = urllib_parse.quote(item_path)
    upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/content"
    item = _put_with_lock_retries(upload_url, token=token, data=raw, content_type=ctype)

    item_id = _as_str(item.get("id"))
    web_url = _as_str(item.get("webUrl"))

    # A driveItem.webUrl is only openable by someone who already has access. The
    # client is external to the Stromy tenant, so mint a sharing link. Best-effort:
    # a failed link mint still returns the (org-internal) webUrl rather than nothing.
    # `webUrl` mode opts out entirely: the readers are members of the target site,
    # so the item's own URL resolves for them and no link needs minting.
    share_url = web_url
    scope = os.environ.get("RENDER_SHAREPOINT_LINK_SCOPE") or "organization"
    link_mode = target.link_mode if target is not None else "createLink"
    if item_id and link_mode != "webUrl":
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
                _as_str(cast("dict[str, object]", link_obj).get("webUrl")) if isinstance(link_obj, dict) else None
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


# ── workspace storage: safe read / list / create-only Drive primitives ─────────
#
# The delivery ladder above is WRITE-ONLY: it PUTs bytes and returns a link. A
# durable, client-readable project record needs three more shapes — read one
# small file, list a bounded set of children, and create a file/folder that must
# never clobber an existing one. These primitives supply exactly those and
# nothing else.
#
# What is deliberately ABSENT:
#   * no `update_file` and no `If-Match` on content. Graph's supported small-file
#     upload endpoint documents create-or-REPLACE and no conditional content
#     header, so a "read-modify-write with a 412 retry" would be built on an
#     undocumented guarantee — and a lost update there would silently rewrite a
#     record a client can read. The ledger is immutable + content-addressed
#     instead, which makes a retry a no-op rather than a race.
#   * no arbitrary-header escape hatch (see GraphResponse).
#   * no client policy. This layer takes a resolved SharePointTarget and validated
#     path segments; it never learns which client it belongs to, never reads
#     client-data, and never decides where a workspace lives.


class WorkspaceStorageError(StromyAssetTransportError):
    """Base for the workspace-storage primitives' typed failures."""


class TargetNotAllowed(WorkspaceStorageError):
    """The target site is not on ``RENDER_SHAREPOINT_ALLOWED_SITES`` (deny-by-default)."""


class UnsafePath(WorkspaceStorageError, ValueError):
    """A path segment is not a single, safe, relative path component."""


class FileNotFound(WorkspaceStorageError):
    """The addressed drive item does not exist."""


class IdempotencyCollision(WorkspaceStorageError):
    """A file already exists at this create-only path with DIFFERENT content.

    The create-only contract is "the same key writes the same bytes exactly once".
    Same key + same digest is a replay and succeeds; same key + different digest
    means two callers derived different content for one identity, which is a
    caller bug that must surface rather than silently overwrite a client-readable
    record.
    """


#: Statuses that mean "someone else holds it / slow down", i.e. worth retrying.
#: The bounded policy itself (`_SHAREPOINT_LOCK_RETRY_DELAYS`: 2s, 5s, 15s across
#: at most four attempts) and the `_sleep` seam are shared with the write path —
#: one contention policy for the whole library, not a second dialect here.
_CONTENTION_STATUSES = frozenset({423, 429, 503})
#: Read ceiling for a ledger record. Events and the index are small by contract;
#: a larger file at one of those paths is a signal, not something to stream.
DEFAULT_READ_MAX_BYTES = 1024 * 1024
#: Hard cap on one `list_children` page, independent of what the caller asks for.
LIST_CHILDREN_MAX_LIMIT = 200

_MAX_SEGMENT_LEN = 255
_SEGMENT_ILLEGAL = set('\\/:*?"<>|')


def _validate_segment(segment: str) -> str:
    """Return ``segment`` if it is ONE safe relative path component, else raise.

    Strict by design (unlike :func:`_safe_segment`, which coerces): a caller that
    hands us ``..``, an absolute path, or an embedded slash has a bug, and
    silently rewriting it into a *different* valid path is how an artifact gets
    filed somewhere nobody asked for. Escaping the allowlisted target is refused
    here, before the allowlist is even consulted.
    """
    if not isinstance(segment, str) or not segment:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise UnsafePath("path segment must be a non-empty string")
    if len(segment) > _MAX_SEGMENT_LEN:
        raise UnsafePath(f"path segment is longer than {_MAX_SEGMENT_LEN} characters")
    if any(c in _SEGMENT_ILLEGAL for c in segment):
        raise UnsafePath(f"path segment {segment!r} contains a SharePoint-illegal character")
    if any(ord(c) < 0x20 for c in segment):
        raise UnsafePath("path segment contains a control character")
    if segment != segment.strip() or segment.startswith(".") or segment.endswith("."):
        raise UnsafePath(
            f"path segment {segment!r} starts or ends with whitespace or a dot "
            "(this also rejects the '.' and '..' traversal segments)"
        )
    return segment


def _validate_segments(segments: Iterable[str]) -> tuple[str, ...]:
    return tuple(_validate_segment(s) for s in segments)


@dataclass(frozen=True)
class SharePointFileRef:
    """One drive item addressed relative to a target's base path.

    ``path`` is a *relative* POSIX path whose every component was validated as a
    single safe segment at construction. Build it with :meth:`of` rather than
    assembling a string, so no unvalidated separator can sneak in.
    """

    target: SharePointTarget
    path: PurePosixPath

    @classmethod
    def of(cls, target: SharePointTarget, *segments: str) -> SharePointFileRef:
        """Build a ref from individually validated segments."""
        validated = _validate_segments(segments)
        if not validated:
            raise UnsafePath("a file ref needs at least one path segment")
        return cls(target=target, path=PurePosixPath(*validated))

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(self.path.parts)

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class DriveItemInfo:
    """The subset of a Graph driveItem the workspace layer needs.

    ``download_url`` is deliberately absent: the short-lived preauthenticated URL
    never leaves this module (see :func:`read_file`). Handing it to an MCP caller
    would export an unauthenticated, forwardable read capability on a client's
    document library.
    """

    id: str
    name: str
    etag: str | None
    web_url: str | None
    created_at: str | None
    is_folder: bool
    size: int | None = None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _item_info(payload: dict[str, object]) -> DriveItemInfo:
    return DriveItemInfo(
        id=_as_str(payload.get("id")) or "",
        name=_as_str(payload.get("name")) or "",
        etag=_as_str(payload.get("eTag")),
        web_url=_as_str(payload.get("webUrl")),
        created_at=_as_str(payload.get("createdDateTime")),
        is_folder="folder" in payload,
        size=_as_int(payload.get("size")),
    )


def _require_allowed(target: SharePointTarget) -> None:
    """Allowlist gate for every workspace primitive — always the FIRST thing run."""
    try:
        _check_target_allowed(target)
    except OutputStoreError as e:
        raise TargetNotAllowed(str(e)) from e


def _base_segments(target: SharePointTarget) -> tuple[str, ...]:
    """The target's root folder, split into validated segments (possibly empty).

    Same precedence as :func:`deliver_to_sharepoint`: an explicit ``base_path``
    wins (``''`` meaning *drive root*), otherwise the env default. Coerced with
    :func:`_safe_segment` rather than rejected, because this value is deployment
    configuration that already governs the existing write path — tightening it
    here would change where established deliveries land.
    """
    if target.base_path is not None:
        base_raw = target.base_path
    else:
        base_raw = os.environ.get("RENDER_SHAREPOINT_BASE_PATH") or "Deliverables"
    return tuple(_safe_segment(p) for p in base_raw.split("/") if p.strip())


def _resolve_drive_id(target: SharePointTarget, token: str) -> str:
    """Resolve the target's drive id, preferring the caller-supplied shortcut."""
    if target.drive_id:
        return target.drive_id
    site = _graph_request(f"{_GRAPH_BASE}/sites/{target.site_id}", token=token)
    resolved_site = site.get("id") or target.site_id
    drive = _graph_request(f"{_GRAPH_BASE}/sites/{resolved_site}/drive", token=token)
    drive_id = _as_str(drive.get("id"))
    if not drive_id:
        raise GraphRequestError(f"could not resolve a default drive for site {target.site_id}")
    return drive_id


def _path_url(drive_id: str, segments: Sequence[str], *, suffix: str = "") -> str:
    """Build a path-addressed drive URL: ``/drives/{id}/root:/a/b{suffix}``.

    With no segments the item IS the drive root, which has no ``root:/…:`` form.
    """
    if not segments:
        return f"{_GRAPH_BASE}/drives/{drive_id}/root{suffix}"
    encoded = urllib_parse.quote("/".join(segments))
    return f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded}:{suffix}" if suffix else (
        f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded}"
    )


def _retry_after_seconds(resp: GraphResponse) -> float | None:
    raw = resp.header("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None  # HTTP-date form: fall back to the fixed schedule


def _call_with_contention_retry(
    describe: str,
    call: Callable[[], GraphResponse],
) -> GraphResponse:
    """Run ``call``, retrying only genuine contention/throttle statuses.

    Honours a server-advertised ``Retry-After`` when present, else the same
    bounded 2/5/15s schedule the write path uses (four attempts max). When the
    budget is exhausted a lock raises :class:`SharePointLockedError` — the type
    ``deliver_artifact`` already maps to ``failure_code="sharepoint_locked"``,
    so contention has ONE meaning across the library — and a pure throttle
    raises :class:`GraphRequestError`. Either way nothing was written: an honest
    "not yet", never an unbounded wait and never a silent success.
    """
    last: GraphResponse | None = None
    for attempt in range(len(_SHAREPOINT_LOCK_RETRY_DELAYS) + 1):
        resp = call()
        if resp.status not in _CONTENTION_STATUSES:
            return resp
        last = resp
        if attempt < len(_SHAREPOINT_LOCK_RETRY_DELAYS):
            advertised = _retry_after_seconds(resp)
            _sleep(advertised if advertised is not None else _SHAREPOINT_LOCK_RETRY_DELAYS[attempt])
    attempts = len(_SHAREPOINT_LOCK_RETRY_DELAYS) + 1
    assert last is not None  # noqa: S101 - the loop only exits here after a response
    message = (
        f"{describe} stayed locked/throttled after {attempts} attempts; deferring "
        "(nothing was written)"
    )
    error_code = _graph_error_code(last.body)
    if _is_locked(last):
        raise SharePointLockedError(
            message, status_code=last.status, error_code=error_code, attempts=attempts
        )
    raise GraphRequestError(message, status_code=last.status, error_code=error_code)


def get_file_metadata(ref: SharePointFileRef) -> DriveItemInfo:
    """Return the drive item's metadata (including its eTag).

    Raises :class:`TargetNotAllowed` before any Graph call for an off-allowlist
    target, and :class:`FileNotFound` when the item does not exist.
    """
    _require_allowed(ref.target)
    token = _graph_token()
    drive_id = _resolve_drive_id(ref.target, token)
    url = _path_url(drive_id, (*_base_segments(ref.target), *ref.segments))
    resp = _call_with_contention_retry(f"metadata read of {ref.path}", lambda: _graph_call(url, token=token))
    if resp.status == 404:
        raise FileNotFound(str(ref.path))
    if resp.status >= 400:
        raise GraphRequestError(
            f"metadata read of {ref.path} → HTTP {resp.status}", status_code=resp.status
        )
    return _item_info(resp.json)


def read_file(ref: SharePointFileRef, *, max_bytes: int = DEFAULT_READ_MAX_BYTES) -> bytes:
    """Read a small file's bytes.

    Graph serves content by redirecting to a short-lived **preauthenticated** URL.
    That URL is a bearer capability in itself, so this function fetches metadata
    first, uses the returned ``@microsoft.graph.downloadUrl`` **without** an
    Authorization header (forwarding our token to a redirect target is how a
    credential leaks), and never returns or logs the URL.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    _require_allowed(ref.target)
    token = _graph_token()
    drive_id = _resolve_drive_id(ref.target, token)
    url = _path_url(drive_id, (*_base_segments(ref.target), *ref.segments))
    resp = _call_with_contention_retry(f"read of {ref.path}", lambda: _graph_call(url, token=token))
    if resp.status == 404:
        raise FileNotFound(str(ref.path))
    if resp.status >= 400:
        raise GraphRequestError(f"read of {ref.path} → HTTP {resp.status}", status_code=resp.status)

    payload = resp.json
    size = _as_int(payload.get("size"))
    if size is not None and size > max_bytes:
        raise GraphRequestError(
            f"{ref.path} is {size} bytes, over the {max_bytes}-byte read ceiling"
        )
    download_url = _as_str(payload.get("@microsoft.graph.downloadUrl"))
    if not download_url:
        raise GraphRequestError(f"{ref.path} exposed no download URL (is it a folder?)")

    # No Authorization header: the URL is already preauthenticated, and attaching
    # our app token to a non-Graph host would export it.
    req = urllib_request.Request(download_url, method="GET")  # noqa: S310
    try:
        with urllib_request.urlopen(req) as content:  # noqa: S310
            data = content.read(max_bytes + 1)
    except urllib_error.HTTPError as exc:
        raise GraphRequestError(
            f"content read of {ref.path} → HTTP {exc.code}", status_code=exc.code
        ) from exc
    except urllib_error.URLError as exc:
        # Deliberately does not interpolate the URL — it is a live credential.
        raise GraphRequestError(f"content read of {ref.path} failed") from exc
    if len(data) > max_bytes:
        raise GraphRequestError(f"{ref.path} exceeds the {max_bytes}-byte read ceiling")
    return data


def list_children(
    target: SharePointTarget,
    parent: Sequence[str] = (),
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[DriveItemInfo], str | None]:
    """List one bounded page of a folder's children.

    Returns ``(items, next_cursor)``. ``next_cursor`` is Graph's opaque
    ``@odata.nextLink``; pass it back to continue. A cursor pointing anywhere
    other than the Graph host is refused — a caller-supplied continuation URL is
    otherwise a request-forgery primitive.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    limit = min(limit, LIST_CHILDREN_MAX_LIMIT)
    _require_allowed(target)
    token = _graph_token()

    if cursor is not None:
        parsed = urllib_parse.urlparse(cursor)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
            raise UnsafePath("pagination cursor must be a Microsoft Graph https URL")
        url = cursor
    else:
        segments = (*_base_segments(target), *_validate_segments(parent))
        url = _path_url(_resolve_drive_id(target, token), segments, suffix="/children")
        url = f"{url}?$top={limit}"

    resp = _call_with_contention_retry("listing children", lambda: _graph_call(url, token=token))
    if resp.status == 404:
        raise FileNotFound("/".join(parent) or "<root>")
    if resp.status >= 400:
        raise GraphRequestError(f"listing children → HTTP {resp.status}", status_code=resp.status)

    payload = resp.json
    raw_values = payload.get("value")
    values = cast("list[object]", raw_values) if isinstance(raw_values, list) else []
    items = [_item_info(cast("dict[str, object]", v)) for v in values if isinstance(v, dict)]
    if len(items) > limit:
        # `$top` (carried into every nextLink) should make this impossible. If it
        # ever happens, fail loudly: silently slicing would drop items the cursor
        # can never return, which reads as "that is the whole history".
        raise GraphRequestError(
            f"listing returned {len(items)} children over the requested limit of {limit}"
        )
    return items, _as_str(payload.get("@odata.nextLink"))


def ensure_folder(target: SharePointTarget, segments: Sequence[str]) -> DriveItemInfo | None:
    """Create the folder chain under the target's base path if it is missing.

    Walks the chain one level at a time. A level that already exists is verified
    to be a **folder** (a file sitting where a folder belongs is a hard error, not
    something to route around). A missing level is created with
    ``conflictBehavior: fail``; a 409 means a concurrent caller won the race, so
    the level is re-resolved and verified rather than re-created.

    ``rename`` is never used: silently creating ``02 Working 1`` next to
    ``02 Working`` would misfile a client's artifact into a folder they will never
    look in. Returns the deepest folder's info, or ``None`` for an empty chain.
    """
    _require_allowed(target)
    validated = _validate_segments(segments)
    if not validated:
        return None
    token = _graph_token()
    drive_id = _resolve_drive_id(target, token)
    base = _base_segments(target)

    parent_id: str | None = None
    current: DriveItemInfo | None = None
    for depth, segment in enumerate(validated, start=1):
        chain = (*base, *validated[:depth])
        probe_url = _path_url(drive_id, chain)
        probe = _call_with_contention_retry(
            f"probing folder {segment!r}", lambda url=probe_url: _graph_call(url, token=token)
        )
        if probe.status == 200:
            current = _item_info(probe.json)
            if not current.is_folder:
                raise GraphRequestError(
                    f"{'/'.join(chain)} exists but is a file; refusing to file a "
                    "deliverable under it"
                )
            parent_id = current.id
            continue
        if probe.status != 404:
            raise GraphRequestError(
                f"probing {'/'.join(chain)} → HTTP {probe.status}", status_code=probe.status
            )

        if parent_id:
            # Address the parent by the id the probe just returned.
            create_url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{parent_id}/children"
        else:
            # First level: the parent is the target's base folder (or the drive
            # root when the target declares no base path).
            create_url = _path_url(drive_id, base, suffix="/children")
        body = json.dumps(
            {"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        ).encode("utf-8")
        created = _call_with_contention_retry(
            f"creating folder {segment!r}",
            lambda url=create_url, payload=body: _graph_call(
                url, token=token, method="POST", data=payload, content_type="application/json"
            ),
        )
        if created.status == 409:
            # Lost the race — re-resolve and verify it really is a folder.
            reprobe = _call_with_contention_retry(
                f"re-probing folder {segment!r}", lambda url=probe_url: _graph_call(url, token=token)
            )
            if reprobe.status != 200:
                raise GraphRequestError(
                    f"folder {'/'.join(chain)} reported a conflict but could not be resolved "
                    f"(HTTP {reprobe.status})"
                )
            current = _item_info(reprobe.json)
            if not current.is_folder:
                raise GraphRequestError(f"{'/'.join(chain)} exists but is a file")
        elif created.status >= 400:
            raise GraphRequestError(
                f"creating {'/'.join(chain)} → HTTP {created.status}", status_code=created.status
            )
        else:
            current = _item_info(created.json)
        parent_id = current.id
    return current


def create_file_once(
    ref: SharePointFileRef,
    data: bytes,
    *,
    content_type: str = "application/json",
    digest: str,
) -> DriveItemInfo:
    """Create ``ref`` exactly once; a replay of the same bytes is a success.

    ``digest`` is the SHA-256 of ``data`` and is the identity of this write. On a
    conflict the existing file is read back and hashed: an equal digest means this
    is a retry of a write that already landed (return the existing item, write
    nothing), while a different digest raises :class:`IdempotencyCollision`.

    The bytes are compared rather than a stored hash because SharePoint's Graph
    metadata exposes ``quickXorHash``, not SHA-256 — trusting a different
    algorithm's value here would be comparing two things that were never equal.
    Records on this path are small and bounded, so reading one back is cheap.
    """
    if not data:
        raise ValueError("create_file_once: refusing to create an empty file")
    actual = hashlib.sha256(data).hexdigest()
    if digest != actual:
        raise ValueError(
            f"create_file_once: digest {digest!r} does not match the payload ({actual!r})"
        )
    _require_allowed(ref.target)
    token = _graph_token()
    drive_id = _resolve_drive_id(ref.target, token)
    chain = (*_base_segments(ref.target), *ref.segments)
    url = _path_url(drive_id, chain, suffix="/content") + "?@microsoft.graph.conflictBehavior=fail"

    resp = _call_with_contention_retry(
        f"create of {ref.path}",
        lambda: _graph_call(url, token=token, method="PUT", data=data, content_type=content_type),
    )
    if resp.status == 409:
        existing = read_file(ref, max_bytes=max(len(data) * 2, 4096))
        if hashlib.sha256(existing).hexdigest() == digest:
            return get_file_metadata(ref)
        raise IdempotencyCollision(
            f"{ref.path} already exists with different content; refusing to overwrite a "
            "client-readable record"
        )
    if resp.status >= 400:
        raise GraphRequestError(f"create of {ref.path} → HTTP {resp.status}", status_code=resp.status)
    return _item_info(resp.json)
