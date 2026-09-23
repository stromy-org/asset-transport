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
#: Ceiling on the bytes the co-edit guard will read back to decide "real edit or
#: zero-change editor save?" after a 412. Sized for a deliverable (decks and
#: handbook PDFs run to tens of MiB), not for the small ledger files
#: `DEFAULT_READ_MAX_BYTES` governs. Override with RENDER_COEDIT_COMPARE_MAX_BYTES.
DEFAULT_COMPARE_READ_MAX_BYTES = 32 * 1024 * 1024
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


def _empty_dict_list() -> list[dict[str, object]]:
    return []


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


#: Why a conditional write refused. `content_changed` is the real co-edit; the
#: other three are fail-closed outcomes where the guard could not *prove* the
#: remote is unchanged and refused rather than overwriting on an assumption.
CONFLICT_REASONS = (
    "content_changed",  # the remote bytes genuinely differ from the declared base
    "compare_unavailable",  # no base_sha256 supplied, so an edit is indistinguishable from an editor-open
    "remote_unreadable",  # the current version could not be read at 412 time
    "still_conflicting",  # the one retry after a zero-change save 412'd again
)


@dataclass(frozen=True)
class StaleBaseConflict:
    """A refused write: the destination moved away from the caller's declared base.

    Carries what a reconciliation actually needs — *which* versions intervened and
    *who* wrote them — rather than only "it moved". ``author`` is a display name
    and nothing else (see :func:`list_file_versions`): it is transient answer-shaped
    data for the overwrite decision, never something to persist into a
    client-readable record.

    ``differs`` is the measured content verdict and is ``False`` on the fail-closed
    reasons, where the guard refused *because* it could not measure. Read ``reason``
    before reading ``differs``.
    """

    base_version: str | None
    current_version: str | None
    differs: bool
    reason: str
    detail: str | None = None
    intervening: list[dict[str, object]] = field(default_factory=_empty_dict_list)

    def as_payload(self) -> dict[str, object]:
        """The JSON-safe shape a tool result carries to the agent."""
        return {
            "base_version": self.base_version,
            "current_version": self.current_version,
            "differs": self.differs,
            "reason": self.reason,
            "detail": self.detail,
            "intervening": list(self.intervening),
        }


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
    #: What this delivery overwrote at the destination, read immediately before
    #: the PUT. `replaced_existing` is True/False when known and None when the
    #: probe could not tell — "we did not look" must stay distinguishable from
    #: "nothing was there". Observation only: a populated `replaced_etag` records
    #: that a prior version was discarded, it does not mean anything refused.
    replaced_existing: bool | None = None
    replaced_etag: str | None = None
    replaced_last_modified_at: str | None = None
    #: The co-edit guard's verdict (ORG-186). `conflict` is set — and `mode` is
    #: 'conflict' — when the write was REFUSED because the destination moved away
    #: from the caller's declared base; nothing was written in that case.
    conflict: StaleBaseConflict | None = None
    #: True when the caller passed `force=True`, so the write went out
    #: unconditionally. Recorded because "this overwrote a collaborator on purpose"
    #: must be a fact in the result, not an inference from its absence.
    forced: bool = False
    #: True when no `base_version` was supplied, so this write was unguarded. The
    #: point is that an unguarded publish is now VISIBLE rather than silent.
    base_version_absent: bool = False
    #: True when a 412 turned out to be a zero-change editor save and the guard
    #: re-PUT once against the current eTag. This is also the signal that answers
    #: "does a web-editor open bump the eTag?" from live traffic.
    if_match_retried: bool = False
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
    base_version: str | None = None,
    base_sha256: str | None = None,
    force: bool = False,
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

    **Co-edit guard (ORG-186), SharePoint rung only.** Pass ``base_version`` (the
    eTag you read before building) and ``base_sha256`` (that version's digest) to
    turn the SharePoint PUT into a compare-and-swap that refuses to overwrite a
    collaborator. A refusal comes back as ``mode='conflict'`` with
    :attr:`DeliveryResult.conflict` populated and **nothing written** — it does not
    fall through to the SAS rung, because a conflict is a decision for the caller,
    not a delivery to reroute. ``force=True`` publishes anyway and records
    ``forced``. All three are keyword-only with behaviour-preserving defaults:
    an un-updated caller keeps working, unguarded, and now says so via
    :attr:`DeliveryResult.base_version_absent`.

    Only a caller that can actually reach a co-edited destination needs them. A
    consumer that never routes to SharePoint (``media-gen-mcp``'s serialization
    path, which passes no ``sharepoint_target``) has no file to protect and is
    intentionally left unguarded rather than threaded for symmetry.
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
            sp = deliver_to_sharepoint(
                raw,
                filename=filename,
                subfolder=subfolder,
                target=sharepoint_target,
                base_version=base_version,
                base_sha256=base_sha256,
                force=force,
            )
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
            conflict = sp.get("conflict")
            if isinstance(conflict, StaleBaseConflict):
                # A refusal is terminal, NOT a rung to fall off. Continuing down the
                # ladder would hand the caller a SAS link and a `mode` that reads like
                # a delivery, which is the silent-overwrite failure wearing a new hat.
                warnings.append(
                    "refused to overwrite the destination: it moved away from the "
                    f"`base_version` you declared ({conflict.reason}). Nothing was written."
                )
                return DeliveryResult(
                    mode="conflict",
                    sha256=sha,
                    size=size,
                    failure_code="stale_base",
                    retryable=True,
                    conflict=conflict,
                    replaced_etag=_as_str(sp.get("replaced_etag")),
                    replaced_last_modified_at=_as_str(sp.get("replaced_last_modified_at")),
                    replaced_existing=_as_bool(sp.get("replaced_existing")),
                    warnings=warnings,
                )
            guard_failure = _as_str(sp.get("guard_failure"))
            if guard_failure:
                warnings.append(
                    "the conditional write returned success but its result cannot be "
                    "reconciled with the `If-Match` having been honoured — Graph may have "
                    "stopped supporting it, in which case this publish overwrote blind. "
                    "Verify the destination before trusting this delivery."
                )
            warnings.extend(cast("list[str]", sp.get("notes") or []))
            return DeliveryResult(
                mode="sharepoint",
                sha256=sha,
                size=size,
                web_url=_as_str(sp.get("web_url")),
                destination_url=_as_str(sp.get("drive_item_web_url")),
                destination_item_id=_as_str(sp.get("item_id")),
                delivered_via=_as_str(sp.get("delivered_via")),
                failure_code=guard_failure,
                replaced_etag=_as_str(sp.get("replaced_etag")),
                replaced_last_modified_at=_as_str(sp.get("replaced_last_modified_at")),
                replaced_existing=_as_bool(sp.get("replaced_existing")),
                forced=force,
                base_version_absent=base_version is None,
                if_match_retried=bool(sp.get("if_match_retried")),
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
    request shapes and **no caller-supplied header dict**, so no caller can smuggle
    arbitrary headers through this layer. The one conditional header it does speak
    is a *named* parameter (``if_match``), added for the co-edit guard (ORG-186).

    That guard rests on measured behaviour, not on documentation: Graph **does**
    honour ``If-Match`` on ``PUT /drives/{id}/root:/{path}:/content``, returning
    ``412 notAllowed`` ("ETag does not match current item's value") on a stale eTag
    and ``200`` on the current one (probed live 2026-09-08, both request forms).
    Microsoft does not document it, so the guard treats it as revocable: see
    ``failure_code="graph_ignored_if_match"`` in :func:`deliver_to_sharepoint`,
    which is the standing detector for the behaviour being withdrawn.
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
    if_match: str | None = None,
) -> GraphResponse:
    """Issue one Graph request and return the full response, status included.

    This is the low-level primitive: an HTTP error status is **returned**, not
    raised, because the workspace layer treats several of them as ordinary
    control flow (404 probes for existence, 409 is the create-only conflict,
    412 is the co-edit guard's compare-and-swap miss, 423/429 drive the bounded
    contention retry). Only a transport failure raises.

    ``if_match`` is a **narrow, named** conditional header, never a generic header
    escape hatch (see :class:`GraphResponse`). Passing it turns a content PUT into
    a compare-and-swap; omitting it leaves every existing call byte-identical.

    :func:`_graph_request` is the raising wrapper the write path uses, so the
    established "any 4xx/5xx is an exception" contract is unchanged there.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    if if_match:
        headers["If-Match"] = if_match
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


def _put_conditional_with_lock_retries(
    url: str,
    *,
    token: str,
    data: bytes,
    content_type: str,
    if_match: str | None,
) -> GraphResponse:
    """The conditional twin of :func:`_put_with_lock_retries`.

    Returns the raw :class:`GraphResponse` instead of the parsed body, because a
    **412 is control flow here, not an error** — it is the compare-and-swap miss
    the co-edit guard exists to handle. Lock contention retries on the same
    bounded schedule; every other status (412 included) is returned to the caller
    to interpret. A genuine failure still raises via :func:`_graph_error_for`
    at the call site, never here.

    Deliberately a separate function rather than a refactor of
    :func:`_put_with_lock_retries`: that one goes through :func:`_graph_request`,
    which is the seam every existing test patches. Leaving it untouched keeps the
    unconditional path byte-identical.
    """
    attempts = 0
    while True:
        attempts += 1
        resp = _graph_call(url, token=token, method="PUT", data=data, content_type=content_type, if_match=if_match)
        if not _is_locked(resp):
            return resp
        if attempts > len(_SHAREPOINT_LOCK_RETRY_DELAYS):
            raise SharePointLockedError(
                f"Graph PUT {url} stayed locked after {attempts} attempts",
                status_code=resp.status,
                error_code=_graph_error_code(resp.body),
                attempts=attempts,
            )
        _sleep(_SHAREPOINT_LOCK_RETRY_DELAYS[attempts - 1])


def _etag_ordinal(etag: str | None) -> tuple[str, int] | None:
    """Split a SharePoint eTag ``"{GUID},N"`` into its identity and version ordinal.

    Returns ``None`` when the string is not in that shape — the caller must then
    treat "cannot tell" as exactly that, never as evidence either way. The whole
    eTag string including the ``,<n>`` suffix is the comparand for ``If-Match``;
    this split exists only for the *advance* check described in
    :func:`deliver_to_sharepoint`, never for building a header.
    """
    if not etag:
        return None
    body = etag.strip().strip('"')
    identity, _, ordinal = body.rpartition(",")
    if not identity or not ordinal.isdigit():
        return None
    return identity, int(ordinal)


def _list_versions_at_path(drive_id: str, encoded_path: str, *, token: str, limit: int = 10) -> list[FileVersionInfo]:
    """Version timeline for a path-addressed item, for the 412 conflict payload.

    The public :func:`list_file_versions` needs a :class:`SharePointFileRef`, and
    the delivery ladder has only a resolved ``drive_id`` + encoded path (it may be
    running off the env destination with no :class:`SharePointTarget` at all). So
    this issues the same ``/versions`` read against the path the PUT targeted and
    reuses :func:`_version_info` verbatim — the same display-name-only projection,
    so no email or UPN can reach a conflict payload through this door either.

    Never raises: a conflict that cannot also enumerate the intervening versions
    is still a conflict, and degrading to an empty timeline is strictly better
    than converting a refusal into an exception.
    """
    url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/versions?$top={limit}"
    try:
        resp = _graph_call(url, token=token)
    except (OutputStoreError, OSError):
        return []
    if resp.status >= 400:
        return []
    raw_values = resp.json.get("value")
    values = cast("list[object]", raw_values) if isinstance(raw_values, list) else []
    versions = [_version_info(cast("dict[str, object]", v)) for v in values if isinstance(v, dict)]
    versions.sort(key=lambda v: v.last_modified_at or "", reverse=True)
    return versions[:limit]


def _read_content_at_path(
    drive_id: str, encoded_path: str, *, token: str, max_bytes: int
) -> tuple[bytes | None, str | None, str | None]:
    """Read a path-addressed item's current bytes + eTag for the 412 compare.

    Returns ``(raw, etag, unavailable_reason)``. Exactly one of ``raw`` and
    ``unavailable_reason`` is set; ``etag`` is best-effort either way, because the
    current version is worth naming in a conflict even when the bytes are not
    readable.

    Mirrors :func:`read_file`'s credential discipline: metadata first, then the
    preauthenticated ``@microsoft.graph.downloadUrl`` fetched **without** an
    Authorization header, and that URL is never returned or logged.
    """
    url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}"
    try:
        resp = _graph_call(url, token=token)
    except (OutputStoreError, OSError):
        return None, None, "the current version could not be read (Graph unreachable)"
    if resp.status >= 400:
        return None, None, f"the current version could not be read (HTTP {resp.status})"
    payload = resp.json
    etag = _as_str(payload.get("eTag"))
    size = _as_int(payload.get("size"))
    if size is not None and size > max_bytes:
        return (
            None,
            etag,
            (
                f"the current version is {size} bytes, over the {max_bytes}-byte compare "
                "ceiling (raise RENDER_COEDIT_COMPARE_MAX_BYTES to compare files this large)"
            ),
        )
    download_url = _as_str(payload.get("@microsoft.graph.downloadUrl"))
    if not download_url:
        return None, etag, "the current version exposed no download URL"
    req = urllib_request.Request(download_url, method="GET")  # noqa: S310
    try:
        with urllib_request.urlopen(req) as content:  # noqa: S310
            data = content.read(max_bytes + 1)
    except (urllib_error.HTTPError, urllib_error.URLError, OSError):
        # Deliberately does not interpolate the URL — it is a live credential.
        return None, etag, "the current version's bytes could not be fetched"
    if len(data) > max_bytes:
        return None, etag, f"the current version exceeds the {max_bytes}-byte compare ceiling"
    return data, etag, None


def _compare_read_max_bytes() -> int:
    """Ceiling on the 412-path content read (env-overridable).

    Deliberately far above :data:`DEFAULT_READ_MAX_BYTES` (1 MiB, sized for small
    ledger files): the thing being compared here is a *deliverable* — a deck, a
    handbook PDF — and a ceiling below the artifacts in play would turn every real
    co-edit into ``compare_too_large`` instead of a usable answer. It is still a
    ceiling: this read only happens on the 412 branch, and an artifact over it
    fails **closed** (conflict) rather than proceeding blind.
    """
    raw = os.environ.get("RENDER_COEDIT_COMPARE_MAX_BYTES")
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_COMPARE_READ_MAX_BYTES
        if parsed > 0:
            return parsed
    return DEFAULT_COMPARE_READ_MAX_BYTES


def _safe_segment(name: str) -> str:
    """Sanitise a caller-supplied folder/name into a SharePoint-safe path segment."""
    cleaned = "".join("-" if c in '"*:<>?/\\|' else c for c in (name or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "Deliverables"


def _peek_existing(drive_id: str, encoded_path: str, *, token: str, path_for_message: str) -> dict[str, object]:
    """Read what is about to be replaced, so an overwrite is at least RECORDED.

    This is the OBSERVATION half, and it predates the guard. It makes an overwrite
    *countable*: one metadata read naming the version being replaced, so "how often
    does a publish land on a file that moved?" became a question the data could
    answer instead of one incidents answered. It is still the only thing that runs
    when a caller supplies no ``base_version`` — an unguarded publish is observed,
    not refused.

    The REFUSAL half is :func:`_conditional_sharepoint_write`, which a caller opts
    into with ``base_version``. Note the ordering: this probe runs *before* the PUT
    and the guard's compare runs only *after* a 412, so the guard costs nothing on
    the happy path.

    Deliberately OBSERVATION-ONLY. It never refuses, never compares against a
    caller-declared base, and never raises: a delivery that would have succeeded
    must still succeed, or this instrumentation becomes an outage. Every failure
    (including "the file is new") resolves to ``existed: False`` with no etag.
    """
    url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}"
    try:
        resp = _graph_call(url, token=token)
    except (OutputStoreError, OSError):
        return {"existed": None}
    if resp.status == 404:
        return {"existed": False}
    if resp.status >= 400:
        # An unreadable target is not a delivery failure. `None` (vs False) keeps
        # "we could not tell" distinct from "there was nothing there" in the data.
        return {"existed": None}
    payload = resp.json
    return {
        "existed": True,
        "etag": _as_str(payload.get("eTag")),
        "last_modified_at": _as_str(payload.get("lastModifiedDateTime")),
    }


def deliver_to_sharepoint(
    raw: bytes,
    *,
    filename: str,
    subfolder: str | None = None,
    content_type: str | None = None,
    target: SharePointTarget | None = None,
    base_version: str | None = None,
    base_sha256: str | None = None,
    force: bool = False,
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

    **The co-edit guard (ORG-186).** With ``base_version`` set the content PUT
    becomes a compare-and-swap: Graph is sent ``If-Match: <base_version>`` and
    answers ``412`` if the destination has moved. A 412 is not by itself evidence
    of an edit — SharePoint's web editors save a version on merely *opening* a
    file — so the 412 handler reads the current bytes and compares their digest
    against ``base_sha256``:

    * digest matches → a zero-change editor save. Re-PUT **once** against the
      current eTag and proceed silently (``if_match_retried``).
    * digest differs → a collaborator really edited. Refuse, and return a
      ``conflict`` naming the intervening versions and their authors.
    * no ``base_sha256``, or the current version is unreadable → refuse
      (**fail closed**). The guard never proceeds on an assumption.

    Without ``base_version`` the write is byte-identical to the original
    unconditional one; with ``force=True`` it is unconditional *and says so*.

    Returns ``{"delivered_via","web_url","drive_item_web_url","item_id"}`` on
    success, a ``{"conflict": StaleBaseConflict, ...}`` dict when the guard
    refused (nothing was written), or ``None`` when no SharePoint backend is
    configured (caller then continues down the download ladder). Raises
    ``OutputStoreError`` only when a *configured* backend genuinely fails, or when
    ``target`` is off-allowlist.
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
    replaced = _peek_existing(drive_id, encoded_path, token=token, path_for_message=item_path)
    upload_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/content"

    notes: list[str] = []
    if_match_retried = False
    guard_failure: str | None = None
    if not base_version or force:
        # Unguarded or deliberately forced: the original unconditional write, going
        # through the same `_graph_request` seam it always did.
        item = _put_with_lock_retries(upload_url, token=token, data=raw, content_type=ctype)
    elif replaced.get("existed") is False:
        # The base was deleted between the fetch and the publish. `If-Match` against
        # a vanished item can only 412 forever, and re-creating it loses nothing a
        # collaborator still has. Proceed, and say that the base is gone.
        item = _put_with_lock_retries(upload_url, token=token, data=raw, content_type=ctype)
        notes.append(
            "the file named by `base_version` no longer exists at this path; the publish "
            "created it fresh rather than replacing a version"
        )
    else:
        outcome = _conditional_sharepoint_write(
            upload_url,
            drive_id=drive_id,
            encoded_path=encoded_path,
            token=token,
            data=raw,
            content_type=ctype,
            base_version=base_version,
            base_sha256=base_sha256,
        )
        if outcome.conflict is not None:
            return {
                "delivered_via": "sharepoint-server",
                "conflict": outcome.conflict,
                "item_id": None,
                "web_url": None,
                "drive_item_web_url": None,
                "replaced_etag": replaced.get("etag"),
                "replaced_last_modified_at": replaced.get("last_modified_at"),
                "replaced_existing": replaced.get("existed"),
            }
        item = outcome.item
        if_match_retried = outcome.if_match_retried
        guard_failure = outcome.guard_failure
        notes.extend(outcome.notes)

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
        "replaced_etag": replaced.get("etag"),
        "replaced_last_modified_at": replaced.get("last_modified_at"),
        "replaced_existing": replaced.get("existed"),
        "if_match_retried": if_match_retried,
        "guard_failure": guard_failure,
        "notes": notes,
    }


@dataclass(frozen=True)
class _ConditionalWriteOutcome:
    """Internal result of one guarded content PUT."""

    item: dict[str, object]
    conflict: StaleBaseConflict | None = None
    if_match_retried: bool = False
    guard_failure: str | None = None
    notes: list[str] = field(default_factory=_empty_str_list)


def _conditional_sharepoint_write(
    upload_url: str,
    *,
    drive_id: str,
    encoded_path: str,
    token: str,
    data: bytes,
    content_type: str,
    base_version: str,
    base_sha256: str | None,
) -> _ConditionalWriteOutcome:
    """PUT with ``If-Match``, and decide what a 412 means. See ORG-186.

    Order matters: the PUT goes **first**, so the untouched-remote case — the
    overwhelming majority — costs the guard **zero** extra round-trips. The read
    that decides "real edit or editor-open?" is bought only when Graph says the
    remote moved.
    """
    resp = _put_conditional_with_lock_retries(
        upload_url, token=token, data=data, content_type=content_type, if_match=base_version
    )
    if resp.status == 412:
        return _resolve_stale_base(
            upload_url,
            drive_id=drive_id,
            encoded_path=encoded_path,
            token=token,
            data=data,
            content_type=content_type,
            base_version=base_version,
            base_sha256=base_sha256,
        )
    if resp.status >= 400:
        raise _graph_error_for(resp, method="PUT", url=upload_url)
    item = resp.json
    return _ConditionalWriteOutcome(
        item=item, guard_failure=_detect_ignored_if_match(item, base_version=base_version, written=data)
    )


def _detect_ignored_if_match(item: dict[str, object], *, base_version: str, written: bytes) -> str | None:
    """Catch Graph silently withdrawing the undocumented ``If-Match`` support.

    ``If-Match`` on the content endpoint is **measured, not documented** (probe in
    :class:`GraphResponse`). If Microsoft ever stops honouring it, a stale-base PUT
    stops returning 412 and starts returning ``200`` — and the transport is back to
    overwriting blind while reporting a clean push. That regression must be
    *detected*, not inherited.

    Two free comparands, both read off the response we already have:

    * **the eTag advance.** An honoured ``If-Match`` can only succeed from exactly
      ``base_version``, so the new eTag must be that same item at ordinal ``n+1``.
      A different identity, or a jump of anything but one, means the remote was
      somewhere else when we wrote — i.e. the header was ignored.
    * **the size.** The item we just wrote must report the size we sent.

    Returns a ``failure_code`` string when the 2xx **cannot** be reconciled with an
    honoured header, else ``None``. An unparseable eTag yields ``None``: "cannot
    tell" is not evidence, and a detector that fires on an eTag format change would
    be worse than none. ``quickXorHash`` would be a third comparand but requires
    implementing Microsoft's proprietary hash to verify — size plus the ordinal
    advance are the two that cost nothing.
    """
    size = _as_int(item.get("size"))
    if size is not None and size != len(written):
        return "graph_ignored_if_match"
    base = _etag_ordinal(base_version)
    current = _etag_ordinal(_as_str(item.get("eTag")))
    if base is None or current is None:
        return None
    if current[0] != base[0] or current[1] != base[1] + 1:
        return "graph_ignored_if_match"
    return None


def _resolve_stale_base(
    upload_url: str,
    *,
    drive_id: str,
    encoded_path: str,
    token: str,
    data: bytes,
    content_type: str,
    base_version: str,
    base_sha256: str | None,
) -> _ConditionalWriteOutcome:
    """The 412 handler: is this a real edit, or a zero-change editor save?"""
    if not base_sha256:
        # Fail CLOSED. Without the base digest there is no way to tell an edit from
        # an editor-open, and guessing in the permissive direction is exactly the
        # silent overwrite this guard exists to stop.
        return _ConditionalWriteOutcome(
            item={},
            conflict=StaleBaseConflict(
                base_version=base_version,
                current_version=None,
                differs=False,
                reason="compare_unavailable",
                detail=(
                    "the destination moved and no `base_sha256` was supplied, so a real edit "
                    "cannot be distinguished from a zero-change editor save. Re-fetch with "
                    "`include_content=True` and carry its `sha256` as `base_sha256`."
                ),
                intervening=_intervening_payload(drive_id, encoded_path, token=token),
            ),
        )

    current_raw, current_etag, unavailable = _read_content_at_path(
        drive_id, encoded_path, token=token, max_bytes=_compare_read_max_bytes()
    )
    if current_raw is None:
        return _ConditionalWriteOutcome(
            item={},
            conflict=StaleBaseConflict(
                base_version=base_version,
                current_version=current_etag,
                differs=False,
                reason="remote_unreadable",
                detail=unavailable,
                intervening=_intervening_payload(drive_id, encoded_path, token=token),
            ),
        )

    if hashlib.sha256(current_raw).hexdigest() != base_sha256:
        return _ConditionalWriteOutcome(
            item={},
            conflict=StaleBaseConflict(
                base_version=base_version,
                current_version=current_etag,
                differs=True,
                reason="content_changed",
                detail=(
                    "the destination's bytes differ from the base you built against; "
                    "reconcile the intervening versions into your source and republish, "
                    "or pass force/reconciled to overwrite deliberately."
                ),
                intervening=_intervening_payload(drive_id, encoded_path, token=token),
            ),
        )

    # Byte-identical: the version bump was a zero-change save (a web editor opening
    # the file). Retry ONCE against the version we just observed. Once, not a loop:
    # an unbounded retry against a moving target is how a guard becomes an overwrite.
    retry = _put_conditional_with_lock_retries(
        upload_url, token=token, data=data, content_type=content_type, if_match=current_etag
    )
    if retry.status == 412:
        return _ConditionalWriteOutcome(
            item={},
            conflict=StaleBaseConflict(
                base_version=base_version,
                current_version=current_etag,
                differs=False,
                reason="still_conflicting",
                detail=(
                    "the destination moved again between the content compare and the retry; "
                    "nothing was written. Re-fetch and republish."
                ),
                intervening=_intervening_payload(drive_id, encoded_path, token=token),
            ),
        )
    if retry.status >= 400:
        raise _graph_error_for(retry, method="PUT", url=upload_url)
    item = retry.json
    return _ConditionalWriteOutcome(
        item=item,
        if_match_retried=True,
        guard_failure=_detect_ignored_if_match(item, base_version=current_etag or base_version, written=data),
        notes=[
            "the destination's version had moved but its bytes were unchanged (a "
            "zero-change editor save); republished against the current version"
        ],
    )


def _intervening_payload(drive_id: str, encoded_path: str, *, token: str) -> list[dict[str, object]]:
    """The version timeline a reconciliation needs, JSON-shaped."""
    return [
        {
            "id": v.id,
            "author": v.author,
            "last_modified_at": v.last_modified_at,
            "size": v.size,
        }
        for v in _list_versions_at_path(drive_id, encoded_path, token=token)
    ]


# ── workspace storage: safe read / list / create-only Drive primitives ─────────
#
# The delivery ladder above is WRITE-ONLY: it PUTs bytes and returns a link. A
# durable, client-readable project record needs three more shapes — read one
# small file, list a bounded set of children, and create a file/folder that must
# never clobber an existing one. These primitives supply exactly those and
# nothing else.
#
# What is deliberately ABSENT:
#   * no `update_file`. The LEDGER stays immutable + content-addressed, which makes
#     a retry a no-op rather than a race — that is a property worth keeping, not a
#     workaround for a missing primitive.
#
#     This block used to say the reason was that Graph offers no conditional
#     content header. That was FALSE and it was load-bearing: it is the claim
#     ORG-186 was designed around for five weeks. Graph honours `If-Match` on
#     `PUT …root:/<path>:/content` (412 on a stale eTag, 200 on the current one —
#     probed live 2026-09-08, see GraphResponse), and `deliver_to_sharepoint` now
#     uses it as a real compare-and-swap. The ledger's immutability is a design
#     choice; it was never a constraint imposed by the API.
#   * no arbitrary-header escape hatch (see GraphResponse). `If-Match` is threaded
#     through a single named parameter, never a caller-supplied header dict.
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
#: Hard cap on one `list_file_versions` read. A co-edit reconciliation needs the
#: versions since a known base, not a file's whole life; a document co-edited in a
#: web editor accrues versions fast (an editor saves one on open), so an unbounded
#: read is both large and useless.
LIST_VERSIONS_MAX_LIMIT = 50

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


@dataclass(frozen=True)
class FileVersionInfo:
    """One entry in a drive item's version history.

    ``author`` is a display name only, by deliberate omission — see
    :func:`list_file_versions`. It is transient answer-shaped data, not something
    to persist: the workspace ledger refuses personal data by contract.
    """

    id: str
    last_modified_at: str | None
    author: str | None
    size: int | None = None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _version_info(payload: dict[str, object]) -> FileVersionInfo:
    modified_by = payload.get("lastModifiedBy")
    user: object = cast("dict[str, object]", modified_by).get("user") if isinstance(modified_by, dict) else None
    author = _as_str(cast("dict[str, object]", user).get("displayName")) if isinstance(user, dict) else None
    return FileVersionInfo(
        id=_as_str(payload.get("id")) or "",
        last_modified_at=_as_str(payload.get("lastModifiedDateTime")),
        author=author,
        size=_as_int(payload.get("size")),
    )


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
    return (
        f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded}:{suffix}"
        if suffix
        else (f"{_GRAPH_BASE}/drives/{drive_id}/root:/{encoded}")
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
    message = f"{describe} stayed locked/throttled after {attempts} attempts; deferring (nothing was written)"
    error_code = _graph_error_code(last.body)
    if _is_locked(last):
        raise SharePointLockedError(message, status_code=last.status, error_code=error_code, attempts=attempts)
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
        raise GraphRequestError(f"metadata read of {ref.path} → HTTP {resp.status}", status_code=resp.status)
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
        raise GraphRequestError(f"{ref.path} is {size} bytes, over the {max_bytes}-byte read ceiling")
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
        raise GraphRequestError(f"content read of {ref.path} → HTTP {exc.code}", status_code=exc.code) from exc
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
        raise GraphRequestError(f"listing returned {len(items)} children over the requested limit of {limit}")
    return items, _as_str(payload.get("@odata.nextLink"))


def list_file_versions(ref: SharePointFileRef, *, limit: int = LIST_VERSIONS_MAX_LIMIT) -> list[FileVersionInfo]:
    """Return a file's recent version timeline, newest first.

    This is the read a co-edit reconciliation needs and
    :func:`get_file_metadata` cannot serve: an eTag answers "did it move?", while
    reconciling a collaborator's edits needs *which* versions intervened and
    *who* wrote them.

    ``author`` is a display name and nothing else — Graph also offers the editor's
    email/UPN on ``lastModifiedBy.user`` and this deliberately drops it. A name is
    the minimum that lets an agent say "someone else edited this since your base";
    an address is a contact detail the co-edit decision never needs. Callers must
    treat ``author`` as transient: it belongs in the answer to "should I
    overwrite?", never in a persisted or client-readable record (the workspace
    ledger refuses personal data by contract).

    A version list is NOT proof of a content change: web editors save a version on
    open, so a newer version can be byte-identical to yours. Compare content
    before concluding a collaborator edited — see ``size`` as a first hint only.

    Raises :class:`TargetNotAllowed` before any Graph call for an off-allowlist
    target, and :class:`FileNotFound` when the item does not exist.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    limit = min(limit, LIST_VERSIONS_MAX_LIMIT)
    _require_allowed(ref.target)
    token = _graph_token()
    drive_id = _resolve_drive_id(ref.target, token)
    url = _path_url(drive_id, (*_base_segments(ref.target), *ref.segments), suffix="/versions")
    url = f"{url}?$top={limit}"
    resp = _call_with_contention_retry(f"version listing of {ref.path}", lambda: _graph_call(url, token=token))
    if resp.status == 404:
        raise FileNotFound(str(ref.path))
    if resp.status >= 400:
        raise GraphRequestError(f"version listing of {ref.path} → HTTP {resp.status}", status_code=resp.status)
    raw_values = resp.json.get("value")
    values = cast("list[object]", raw_values) if isinstance(raw_values, list) else []
    versions = [_version_info(cast("dict[str, object]", v)) for v in values if isinstance(v, dict)]
    # Graph returns versions newest-first, but that ordering is not contractual and
    # a reconciliation that walks the list backwards would silently fold the wrong
    # edits. Sort on the one field that is unambiguous.
    versions.sort(key=lambda v: v.last_modified_at or "", reverse=True)
    return versions[:limit]


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
                    f"{'/'.join(chain)} exists but is a file; refusing to file a deliverable under it"
                )
            parent_id = current.id
            continue
        if probe.status != 404:
            raise GraphRequestError(f"probing {'/'.join(chain)} → HTTP {probe.status}", status_code=probe.status)

        if parent_id:
            # Address the parent by the id the probe just returned.
            create_url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{parent_id}/children"
        else:
            # First level: the parent is the target's base folder (or the drive
            # root when the target declares no base path).
            create_url = _path_url(drive_id, base, suffix="/children")
        body = json.dumps({"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}).encode("utf-8")
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
                    f"folder {'/'.join(chain)} reported a conflict but could not be resolved (HTTP {reprobe.status})"
                )
            current = _item_info(reprobe.json)
            if not current.is_folder:
                raise GraphRequestError(f"{'/'.join(chain)} exists but is a file")
        elif created.status >= 400:
            raise GraphRequestError(f"creating {'/'.join(chain)} → HTTP {created.status}", status_code=created.status)
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
        raise ValueError(f"create_file_once: digest {digest!r} does not match the payload ({actual!r})")
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
            f"{ref.path} already exists with different content; refusing to overwrite a client-readable record"
        )
    if resp.status >= 400:
        raise GraphRequestError(f"create of {ref.path} → HTTP {resp.status}", status_code=resp.status)
    return _item_info(resp.json)
