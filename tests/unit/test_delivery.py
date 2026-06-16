"""Delivery unit tests — no Azure needed.

The Azure SAS path is exercised against real Blob storage out-of-band; these
CI-safe tests cover backend selection, the local-dir (file://) delivery
contract, the SharePoint push, and the deliver_artifact ladder. Ported from
stromy-format-mcp's test_output_store.py + new deliver_artifact coverage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from urllib.request import url2pathname

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

    res = O.deliver_to_sharepoint(
        b"deck-bytes", filename="strategy.pptx", subfolder="Stichting UPV Textiel/2026-06"
    )
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


def test_deliver_artifact_prefers_sharepoint_for_large(monkeypatch):
    raw = b"x" * 4096
    monkeypatch.setattr(
        O, "deliver_to_sharepoint",
        lambda raw, *, filename: {
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
    monkeypatch.setattr(O, "deliver_to_sharepoint", lambda raw, *, filename: None)
    monkeypatch.delenv("ASSET_STORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ASSET_STORE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    res = O.deliver_artifact(raw, filename="big.pdf", inline_max=1024)
    assert res.mode == "sas"
    assert res.download_url is not None and res.download_url.startswith("file://")


def test_deliver_artifact_pushed_when_upload_url(monkeypatch):
    raw = b"z" * 100
    monkeypatch.setattr(
        O, "push_to_url",
        lambda raw, *, upload_url, kind, total_size: {
            "delivered_via": kind, "web_url": "https://sp/x", "item_id": "id-1",
        },
    )
    res = O.deliver_artifact(raw, filename="d.pptx", inline_max=10, upload_url="https://upload")
    assert res.mode == "pushed"
    assert res.web_url == "https://sp/x"
    assert res.destination_item_id == "id-1"


def test_deliver_artifact_inline_last_resort_when_no_backend(monkeypatch):
    raw = b"q" * 4096
    monkeypatch.setattr(O, "deliver_to_sharepoint", lambda raw, *, filename: None)
    for var in ("RENDER_OUTPUT_LOCAL_DIR", "ASSET_STORE_ACCOUNT", "ASSET_STORE_CONNECTION_STRING"):
        monkeypatch.delenv(var, raising=False)
    res = O.deliver_artifact(raw, filename="big.pdf", inline_max=1024)
    assert res.mode == "inline"
    assert res.inline_b64 == base64.b64encode(raw).decode("ascii")
    assert any("no delivery backend" in w for w in res.warnings)
