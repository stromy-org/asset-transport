"""Delivery unit tests — no Azure needed.

The Azure SAS path is exercised against real Blob storage out-of-band; these
CI-safe tests cover backend selection, the local-dir (file://) delivery
contract, the SharePoint push, and the deliver_artifact ladder. Ported from
stromy-format-mcp's test_output_store.py + new deliver_artifact coverage.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from stromy_asset_transport import delivery as O


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ── deliver() : Azure Blob / local-dir backend ───────────────────────────────


def test_no_backend_returns_none(monkeypatch):
    """No outputs backend configured → None, so the caller falls back."""
    for var in ("RENDER_OUTPUT_LOCAL_DIR", "ASSET_STORE_ACCOUNT", "ASSET_STORE_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    assert O.deliver(b"deck-bytes", sha=_sha(b"deck-bytes"), filename="x.pptx") is None


def test_local_dir_backend_writes_file_and_returns_file_url(tmp_path, monkeypatch):
    """Local-dir backend writes <sha>.pptx and returns a fetchable file:// URL."""
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    out = tmp_path / "outputs"
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(out))
    raw = b"%PPTX-bytes-stand-in%"
    sha = _sha(raw)

    res = O.deliver(raw, sha=sha, filename="stromy-deck.pptx")
    assert res is not None
    assert res["blob"] == f"{sha}.pptx"
    assert res["url_expires_at"]
    parsed = urlparse(cast(str, res["download_url"]))
    assert parsed.scheme == "file"
    fetched = Path(url2pathname(parsed.path))
    assert fetched.read_bytes() == raw
    assert fetched == out / f"{sha}.pptx"


def test_empty_ttl_env_var_falls_back_to_default(tmp_path, monkeypatch):
    """A present-but-EMPTY RENDER_URL_TTL_SECONDS must fall back to default, not crash."""
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("RENDER_URL_TTL_SECONDS", "")  # empty, not absent
    raw = b"deck"
    res = O.deliver(raw, sha=_sha(raw), filename="x.pptx")  # must not raise
    assert res is not None
    assert res["url_expires_at"]


def test_local_dir_backend_preserves_extension(tmp_path, monkeypatch):
    """The blob name carries the artifact's real extension (pptx/docx/pdf/mp4)."""
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    raw = b"clip"
    res = O.deliver(raw, sha=_sha(raw), filename="promo.mp4")
    assert res is not None
    assert cast(str, res["blob"]).endswith(".mp4")


# ── push_to_url() : caller-brokered PUT ──────────────────────────────────────


class _PutRecorder(BaseHTTPRequestHandler):
    request_data: dict[str, object] = {}
    response_code = 201
    response_body = {"webUrl": "https://contoso.sharepoint.com/doc", "id": "drive-item-123"}

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.request_data = {
            "headers": dict(self.headers.items()),
            "body": self.rfile.read(length),
        }
        body = json.dumps(self.response_body).encode("utf-8")
        self.send_response(self.response_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


def _serve(handler_class):
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_push_to_url_graph_upload_session_uses_content_range_and_no_auth():
    _PutRecorder.request_data = {}
    _PutRecorder.response_code = 201
    server, thread = _serve(_PutRecorder)
    try:
        url = f"http://127.0.0.1:{server.server_port}/upload"
        raw = b"deck-bytes"
        res = O.push_to_url(raw, upload_url=url, kind="graph-upload-session")
    finally:
        server.shutdown()
        thread.join()

    headers = cast("dict[str, str]", _PutRecorder.request_data["headers"])
    assert headers["Content-Range"] == f"bytes 0-{len(raw) - 1}/{len(raw)}"
    assert "Authorization" not in headers
    assert _PutRecorder.request_data["body"] == raw
    assert res["delivered_via"] == "graph-upload-session"
    assert res["status_code"] == 201
    assert res["web_url"] == "https://contoso.sharepoint.com/doc"
    assert res["item_id"] == "drive-item-123"


# ── deliver_to_sharepoint() ──────────────────────────────────────────────────


def _fake_graph(calls: list[dict]):
    def _impl(url, *, token, method="GET", data=None, content_type=None):
        calls.append({"url": url, "method": method, "data": data})
        if method == "GET" and url.endswith("/drive"):
            return {"id": "resolved-drive-id"}
        if method == "GET" and "/sites/" in url:
            return {"id": "resolved-site-id"}
        if method == "PUT" and url.endswith(":/content"):
            return {"id": "item-1", "webUrl": "https://stromy.sharepoint.com/item-1"}
        if method == "POST" and url.endswith("/createLink"):
            return {"link": {"webUrl": "https://stromy.sharepoint.com/share/abc"}}
        return {}

    return _impl


def test_sharepoint_no_backend_returns_none(monkeypatch):
    monkeypatch.delenv("RENDER_SHAREPOINT_DRIVE_ID", raising=False)
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    assert O.deliver_to_sharepoint(b"deck", filename="x.pptx") is None


def test_sharepoint_uploads_to_per_client_path_and_returns_share_link(monkeypatch):
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setenv("RENDER_SHAREPOINT_BASE_PATH", "Deliverables")
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    res = O.deliver_to_sharepoint(b"deck-bytes", filename="strategy.pptx", subfolder="Stichting UPV Textiel/2026-06")
    assert res is not None
    assert res["delivered_via"] == "sharepoint-server"
    assert res["web_url"] == "https://stromy.sharepoint.com/share/abc"
    assert res["item_id"] == "item-1"
    put = next(c for c in calls if c["method"] == "PUT")
    assert "Deliverables/Stichting%20UPV%20Textiel/2026-06/strategy.pptx" in put["url"]
    assert put["data"] == b"deck-bytes"
    assert any(c["method"] == "POST" and c["url"].endswith("/createLink") for c in calls)


def test_sharepoint_resolves_drive_from_site_id(monkeypatch):
    monkeypatch.delenv("RENDER_SHAREPOINT_DRIVE_ID", raising=False)
    monkeypatch.setenv("RENDER_SHAREPOINT_SITE_ID", "site-abc")
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    res = O.deliver_to_sharepoint(b"deck", filename="d.pptx")
    assert res is not None
    assert calls[0]["method"] == "GET" and calls[0]["url"].endswith("/sites/site-abc")
    assert calls[1]["method"] == "GET" and calls[1]["url"].endswith("/sites/resolved-site-id/drive")
    put = next(c for c in calls if c["method"] == "PUT")
    assert "/drives/resolved-drive-id/root:/" in put["url"]


def test_sharepoint_falls_back_to_item_weburl_when_link_mint_fails(monkeypatch):
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")

    def _impl(url, *, token, method="GET", data=None, content_type=None):
        if method == "PUT":
            return {"id": "item-1", "webUrl": "https://stromy.sharepoint.com/item-1"}
        if method == "POST":
            raise O.OutputStoreError("createLink denied")
        return {}

    monkeypatch.setattr(O, "_graph_request", _impl)
    res = O.deliver_to_sharepoint(b"deck", filename="d.pptx")
    assert res is not None
    assert res["web_url"] == "https://stromy.sharepoint.com/item-1"


def _http_error(status: int, body: dict[str, object]) -> urllib_error.HTTPError:
    return urllib_error.HTTPError(
        "https://graph.microsoft.com/upload",
        status,
        "Graph error",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def test_graph_recognizes_sharepoint_lock_by_423_status(monkeypatch):
    monkeypatch.setattr(
        O.urllib_request,
        "urlopen",
        lambda _request: (_ for _ in ()).throw(_http_error(423, {"error": {"code": "unknown"}})),
    )

    with pytest.raises(O.SharePointLockedError) as caught:
        O._graph_request("https://graph.microsoft.com/upload", token=str(), method="PUT", data=b"x")

    assert caught.value.status_code == 423
    assert caught.value.error_code == "unknown"


def test_graph_recognizes_sharepoint_resource_locked_by_body(monkeypatch):
    monkeypatch.setattr(
        O.urllib_request,
        "urlopen",
        lambda _request: (_ for _ in ()).throw(
            _http_error(409, {"error": {"code": "ResourceLocked", "message": "open elsewhere"}})
        ),
    )

    with pytest.raises(O.SharePointLockedError) as caught:
        O._graph_request("https://graph.microsoft.com/upload", token=str(), method="PUT", data=b"x")

    assert caught.value.status_code == 409
    assert caught.value.error_code == "ResourceLocked"


def test_sharepoint_lock_backoff_uses_exact_delays_then_succeeds(monkeypatch):
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    sleeps: list[float] = []
    calls: list[str] = []

    def _impl(url, *, token, method="GET", data=None, content_type=None):
        calls.append(method)
        if method == "PUT" and calls.count("PUT") < 4:
            raise O.SharePointLockedError("locked", status_code=423, error_code="resourceLocked")
        if method == "PUT":
            return {"id": "item-1", "webUrl": "https://stromy.sharepoint.com/item-1"}
        if method == "POST":
            return {"link": {"webUrl": "https://stromy.sharepoint.com/share/abc"}}
        return {}

    monkeypatch.setattr(O, "_graph_request", _impl)
    monkeypatch.setattr(O.time, "sleep", sleeps.append)

    result = O.deliver_to_sharepoint(b"deck", filename="d.pptx")

    assert result is not None
    assert calls.count("PUT") == 4
    assert sleeps == [2.0, 5.0, 15.0]


def test_exhausted_sharepoint_lock_returns_retryable_metadata_without_sas_or_delete(monkeypatch):
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    methods: list[str] = []
    sleeps: list[float] = []

    def _impl(url, *, token, method="GET", data=None, content_type=None):
        methods.append(method)
        raise O.SharePointLockedError("locked", status_code=423, error_code="resourceLocked")

    monkeypatch.setattr(O, "_graph_request", _impl)
    monkeypatch.setattr(O.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        O,
        "deliver",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SAS fallback must not run")),
    )

    result = O.deliver_artifact(b"large", filename="d.pptx", inline_max=0)

    assert result.mode == "none"
    assert result.failure_code == "sharepoint_locked"
    assert result.retryable is True
    assert result.attempts == 4
    assert methods == ["PUT", "PUT", "PUT", "PUT"]
    assert "DELETE" not in methods
    assert sleeps == [2.0, 5.0, 15.0]
    assert any("retry after the editor closes it" in warning for warning in result.warnings)


def test_non_lock_graph_failure_still_falls_back_to_sas(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    monkeypatch.setattr(
        O,
        "_graph_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            O.GraphRequestError("precondition failed", status_code=412, error_code="preconditionFailed")
        ),
    )
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))

    result = O.deliver_artifact(b"large", filename="d.pptx", inline_max=0)

    assert result.mode == "sas"
    assert result.failure_code is None
    assert result.download_url is not None
    assert any("precondition failed" in warning for warning in result.warnings)


# ── deliver_to_sharepoint(target=…) : per-space targeting + allowlist ────────


def _target_env(monkeypatch, allowed: str | None = "stromy.sharepoint.com:/sites/ai4comms-collab"):
    """Env for explicit-target tests: a legacy env destination + an allowlist.

    The env destination is deliberately left set so each test also proves the
    target *overrides* it rather than merging with it.
    """
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "legacy-env-drive")
    monkeypatch.setenv("RENDER_SHAREPOINT_SITE_ID", "legacy-env-site")
    monkeypatch.delenv("RENDER_SHAREPOINT_BASE_PATH", raising=False)
    if allowed is None:
        monkeypatch.delenv("RENDER_SHAREPOINT_ALLOWED_SITES", raising=False)
    else:
        monkeypatch.setenv("RENDER_SHAREPOINT_ALLOWED_SITES", allowed)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")


def test_sharepoint_target_overrides_env_destination(monkeypatch):
    """An allowlisted target wins over the env drive, and base_path='' drops the base."""
    _target_env(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    res = O.deliver_to_sharepoint(
        b"deck",
        filename="brief.pdf",
        subfolder="KVGO/Social Media Campaign/03 Deliverables",
        target=O.SharePointTarget(
            site_id="stromy.sharepoint.com:/sites/ai4comms-collab",
            base_path="",
        ),
    )
    assert res is not None
    put = next(c for c in calls if c["method"] == "PUT")
    # Resolved via the *target* site, never the env drive.
    assert "legacy-env-drive" not in put["url"]
    assert any("/sites/stromy.sharepoint.com:/sites/ai4comms-collab" in c["url"] for c in calls)
    # base_path='' => no 'Deliverables/' prefix; tree hangs off the drive root.
    assert "root:/KVGO/Social%20Media%20Campaign/03%20Deliverables/brief.pdf" in put["url"]
    assert "Deliverables/KVGO" not in urllib_parse.unquote(put["url"])


def test_sharepoint_target_weburl_mode_skips_createlink(monkeypatch):
    _target_env(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    res = O.deliver_to_sharepoint(
        b"deck",
        filename="brief.pdf",
        target=O.SharePointTarget(
            site_id="stromy.sharepoint.com:/sites/ai4comms-collab",
            link_mode="webUrl",
        ),
    )
    assert res is not None
    # No sharing link minted — members already hold direct permissions.
    assert not any(c["url"].endswith("/createLink") for c in calls)
    assert res["web_url"] == "https://stromy.sharepoint.com/item-1"
    assert res["drive_item_web_url"] == "https://stromy.sharepoint.com/item-1"


def test_sharepoint_target_off_allowlist_site_is_refused(monkeypatch):
    _target_env(monkeypatch, allowed="stromy.sharepoint.com:/sites/duke-collab")
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    with pytest.raises(O.OutputStoreError, match="not in RENDER_SHAREPOINT_ALLOWED_SITES"):
        O.deliver_to_sharepoint(
            b"deck",
            filename="brief.pdf",
            target=O.SharePointTarget(site_id="stromy.sharepoint.com:/sites/other-client"),
        )
    assert calls == []  # refused before any Graph call


def test_sharepoint_target_drive_id_only_is_refused(monkeypatch):
    """A bare drive id would bypass the site boundary the allowlist enforces."""
    _target_env(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    with pytest.raises(O.OutputStoreError, match="must name a site"):
        O.deliver_to_sharepoint(b"deck", filename="brief.pdf", target=O.SharePointTarget(drive_id="some-drive"))
    assert calls == []


def test_sharepoint_target_refused_when_allowlist_unset(monkeypatch):
    """Deny-by-default: no allowlist => explicit targets are unreachable."""
    _target_env(monkeypatch, allowed=None)
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    with pytest.raises(O.OutputStoreError, match="unset or empty"):
        O.deliver_to_sharepoint(
            b"deck",
            filename="brief.pdf",
            target=O.SharePointTarget(site_id="stromy.sharepoint.com:/sites/ai4comms-collab"),
        )
    assert calls == []


def test_sharepoint_allowlist_ignored_without_explicit_target(monkeypatch):
    """Backward-compat: the env-default path is untouched by the allowlist."""
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.delenv("RENDER_SHAREPOINT_ALLOWED_SITES", raising=False)
    monkeypatch.delenv("RENDER_SHAREPOINT_BASE_PATH", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    calls: list[dict] = []
    monkeypatch.setattr(O, "_graph_request", _fake_graph(calls))

    res = O.deliver_to_sharepoint(b"deck", filename="d.pptx")
    assert res is not None
    put = next(c for c in calls if c["method"] == "PUT")
    assert "/drives/drive-xyz/root:/Deliverables/d.pptx" in urllib_parse.unquote(put["url"])
    assert res["web_url"] == "https://stromy.sharepoint.com/share/abc"  # createLink still minted


def test_deliver_artifact_off_allowlist_target_warns_and_falls_to_sas(tmp_path, monkeypatch):
    """The top risk: a mis-targeted render must degrade loudly, never mis-deliver."""
    _target_env(monkeypatch, allowed="stromy.sharepoint.com:/sites/duke-collab")
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    monkeypatch.setattr(O, "_graph_request", _fake_graph([]))

    res = O.deliver_artifact(
        b"a-large-enough-payload",
        filename="brief.pdf",
        inline_max=0,
        sharepoint_target=O.SharePointTarget(site_id="stromy.sharepoint.com:/sites/other-client"),
    )
    assert res.mode != "sharepoint"  # never silently delivered to the wrong space
    assert any("other-client" in w for w in res.warnings)
    assert any("sharepoint push failed" in w for w in res.warnings)


# ── deliver_artifact() : the ladder ──────────────────────────────────────────


def test_deliver_artifact_inline_when_small():
    raw = b"tiny"
    res = O.deliver_artifact(raw, filename="x.pdf", inline_max=1024)
    assert res.mode == "inline"
    assert res.inline_b64 == base64.b64encode(raw).decode("ascii")
    assert res.sha256 == _sha(raw)
    assert res.size == len(raw)
    assert res.download_url is None and res.web_url is None


def test_deliver_artifact_empty_raises():
    try:
        O.deliver_artifact(b"", filename="x.pdf", inline_max=10)
    except ValueError as e:
        assert "empty" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on empty payload")


def test_deliver_artifact_threads_subfolder_to_sharepoint(monkeypatch):
    raw = b"x" * 4096
    seen: dict[str, object] = {}

    def _sp(raw, *, filename, subfolder=None, target=None):
        seen["subfolder"] = subfolder
        seen["target"] = target
        return {"delivered_via": "sharepoint-server", "web_url": "https://sp/s", "item_id": "i"}

    monkeypatch.setattr(O, "deliver_to_sharepoint", _sp)
    res = O.deliver_artifact(raw, filename="d.pptx", inline_max=1024, subfolder="Rebeca/2026-06")
    assert res.mode == "sharepoint"
    assert seen["subfolder"] == "Rebeca/2026-06"
    assert seen["target"] is None  # no target supplied => env-default path


def test_deliver_artifact_threads_sharepoint_target(monkeypatch):
    """deliver_artifact passes an explicit target straight through to the push."""
    raw = b"x" * 4096
    seen: dict[str, object] = {}

    def _sp(raw, *, filename, subfolder=None, target=None):
        seen["target"] = target
        return {"delivered_via": "sharepoint-server", "web_url": "https://sp/s", "item_id": "i"}

    monkeypatch.setattr(O, "deliver_to_sharepoint", _sp)
    tgt = O.SharePointTarget(site_id="stromy.sharepoint.com:/sites/ai4comms-collab", base_path="")
    res = O.deliver_artifact(raw, filename="d.pptx", inline_max=1024, sharepoint_target=tgt)
    assert res.mode == "sharepoint"
    assert seen["target"] is tgt


def test_deliver_artifact_prefers_sharepoint_for_large(monkeypatch):
    raw = b"x" * 4096
    monkeypatch.setattr(
        O,
        "deliver_to_sharepoint",
        lambda raw, *, filename, subfolder=None, target=None: {
            "delivered_via": "sharepoint-server",
            "web_url": "https://stromy.sharepoint.com/share/zzz",
            "drive_item_web_url": "https://stromy.sharepoint.com/item",
            "item_id": "it-9",
        },
    )
    # deliver() must not be consulted once SharePoint succeeds.
    monkeypatch.setattr(O, "deliver", lambda *a, **k: (_ for _ in ()).throw(AssertionError("deliver called")))
    res = O.deliver_artifact(raw, filename="big.pptx", inline_max=1024)
    assert res.mode == "sharepoint"
    assert res.web_url == "https://stromy.sharepoint.com/share/zzz"
    assert res.destination_item_id == "it-9"


def test_deliver_artifact_falls_through_to_sas(monkeypatch, tmp_path):
    raw = b"y" * 4096
    monkeypatch.setattr(O, "deliver_to_sharepoint", lambda raw, *, filename, subfolder=None, target=None: None)
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    res = O.deliver_artifact(raw, filename="big.pdf", inline_max=1024)
    assert res.mode == "sas"
    assert res.download_url is not None and res.download_url.startswith("file://")
    assert res.url_expires_at is not None


def test_deliver_artifact_pushed_when_upload_url_with_dual_link(monkeypatch, tmp_path):
    raw = b"z" * 100
    monkeypatch.setattr(
        O,
        "push_to_url",
        lambda raw, *, upload_url, kind, total_size: {
            "delivered_via": kind,
            "web_url": "https://sp/x",
            "item_id": "id-1",
        },
    )
    # A blob backend is present, so the pushed artifact also gets a SAS dual-link.
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    res = O.deliver_artifact(raw, filename="d.pptx", inline_max=10, upload_url="https://upload")
    assert res.mode == "pushed"
    assert res.web_url == "https://sp/x"
    assert res.destination_item_id == "id-1"
    assert res.delivered_via == "graph-upload-session"
    # Dual link minted alongside the push destination.
    assert res.download_url is not None and res.download_url.startswith("file://")
    assert res.url_expires_at is not None


def test_deliver_artifact_pushed_no_dual_link_when_disabled(monkeypatch, tmp_path):
    raw = b"z" * 100
    monkeypatch.setattr(
        O,
        "push_to_url",
        lambda raw, *, upload_url, kind, total_size: {"delivered_via": kind, "web_url": "https://sp/x"},
    )
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    res = O.deliver_artifact(raw, filename="d.pptx", inline_max=10, upload_url="https://upload", dual_link=False)
    assert res.mode == "pushed"
    assert res.download_url is None and res.url_expires_at is None


def test_deliver_artifact_none_when_no_backend_for_large(monkeypatch):
    """A large artifact with no URL backend is NOT inlined — mode 'none'."""
    raw = b"q" * 4096
    monkeypatch.setattr(O, "deliver_to_sharepoint", lambda raw, *, filename, subfolder=None, target=None: None)
    for var in ("RENDER_OUTPUT_LOCAL_DIR", "ASSET_STORE_ACCOUNT", "ASSET_STORE_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    res = O.deliver_artifact(raw, filename="big.pdf", inline_max=1024)
    assert res.mode == "none"
    assert res.inline_b64 is None
    assert any("not delivered" in w for w in res.warnings)
