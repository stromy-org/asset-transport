"""Upload-session cleanup: target validation, cancellation and commit proof.

A caller-brokered Graph upload session reserves its filename until it is filled,
cancelled or expires. These tests pin the three pieces that keep a failed render
from leaving that reservation behind: ``validate_upload_target`` (one predicate
for PUT and DELETE), ``cancel_upload_session`` (best-effort, never raises) and
the receipt ``deliver_artifact`` carries into every fallback result.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stromy_asset_transport import delivery as O

SENTINEL = "SENTINEL-TEMPAUTH-7f3a"


class _Recorder(BaseHTTPRequestHandler):
    calls: list[dict[str, object]] = []
    status = 204
    put_status = 201
    put_body: dict[str, object] = {"id": "drive-item-1", "webUrl": "https://example.test/doc"}
    location: str | None = None

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        self.__class__.calls.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": self.rfile.read(length) if length else b"",
            }
        )

    def do_DELETE(self):  # noqa: N802
        self._record()
        self.send_response(self.__class__.status)
        if self.__class__.location:
            self.send_header("Location", self.__class__.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):  # noqa: N802
        self._record()
        body = json.dumps(self.__class__.put_body).encode()
        self.send_response(self.__class__.put_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


@pytest.fixture
def server():
    _Recorder.calls = []
    _Recorder.status = 204
    _Recorder.put_status = 201
    _Recorder.put_body = {"id": "drive-item-1", "webUrl": "https://example.test/doc"}
    _Recorder.location = None
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/up?tempauth={SENTINEL}"
    finally:
        srv.shutdown()
        thread.join()


# ── validate_upload_target ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://tenant-my.sharepoint.com/personal/x/_api/v2.0/uploadSession?guid=1",
        "https://my.microsoftpersonalcontent.com/rup/abc",
        "https://upload",
        "http://127.0.0.1:8080/upload",
        "http://localhost/upload",
        "http://[::1]:9000/upload",
    ],
)
def test_validate_accepts_https_and_loopback_http(url):
    assert O.validate_upload_target(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "http://example.com/upload",
        "https://user:pw@example.com/upload",
        "https:///nohost",
        "not a url",
        "file:///etc/passwd",
    ],
)
def test_validate_rejects_unsafe_targets_without_echoing_them(url):
    reason = O.validate_upload_target(url)
    assert reason is not None
    if url:
        assert url not in reason


# ── cancel_upload_session ────────────────────────────────────────────────────


@pytest.mark.parametrize(("status", "expected"), [(204, True), (404, True), (500, False), (403, False)])
def test_cancel_maps_status(server, status, expected):
    _Recorder.status = status
    assert O.cancel_upload_session(server) is expected
    assert len(_Recorder.calls) == 1
    call = _Recorder.calls[0]
    assert call["method"] == "DELETE"
    assert "Authorization" not in call["headers"]  # type: ignore[operator]
    assert call["body"] == b""


def test_cancel_refuses_redirects(server):
    _Recorder.status = 307
    _Recorder.location = "http://127.0.0.1:1/elsewhere"
    assert O.cancel_upload_session(server) is False
    assert len(_Recorder.calls) == 1  # the redirect target was never contacted


@pytest.mark.parametrize("url", ["http://example.com/up", "https://u:p@example.com/up", "ftp://x/y", ""])
def test_cancel_makes_no_request_for_invalid_targets(monkeypatch, url):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("no request may be made for an invalid target")

    monkeypatch.setattr(O.urllib_request, "build_opener", _boom)
    assert O.cancel_upload_session(url) is False


def test_cancel_never_raises_on_network_failure_and_logs_no_url(monkeypatch, caplog):
    class _Opener:
        def open(self, *_a, **_k):
            raise TimeoutError(f"timed out talking to {SENTINEL}")

    monkeypatch.setattr(O.urllib_request, "build_opener", lambda *_a: _Opener())
    with caplog.at_level(logging.DEBUG):
        assert O.cancel_upload_session(f"https://example.test/up?tempauth={SENTINEL}", timeout=0.1) is False
    assert SENTINEL not in caplog.text


# ── push_to_url commit proof ─────────────────────────────────────────────────


def test_push_graph_202_is_incomplete_not_pushed(server):
    _Recorder.put_status = 202
    _Recorder.put_body = {"nextExpectedRanges": ["5-"]}
    with pytest.raises(O.OutputStoreError) as caught:
        O.push_to_url(b"bytes", upload_url=server, kind="graph-upload-session")
    assert "incomplete" in str(caught.value)
    assert SENTINEL not in str(caught.value)


def test_push_graph_success_without_item_id_is_not_pushed(server):
    _Recorder.put_body = {"webUrl": "https://example.test/doc"}
    with pytest.raises(O.OutputStoreError):
        O.push_to_url(b"bytes", upload_url=server, kind="graph-upload-session")


def test_push_presigned_keeps_plain_2xx_semantics(server):
    _Recorder.put_status = 201
    _Recorder.put_body = {}
    res = O.push_to_url(b"bytes", upload_url=server, kind="presigned-put")
    assert res["status_code"] == 201


def test_push_refuses_invalid_target_before_any_request(monkeypatch):
    monkeypatch.setattr(
        O.urllib_request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no request"))
    )
    with pytest.raises(O.OutputStoreError) as caught:
        O.push_to_url(b"bytes", upload_url=f"http://example.com/up?tempauth={SENTINEL}")
    assert SENTINEL not in str(caught.value)


# ── deliver_artifact receipt ─────────────────────────────────────────────────


def _failing_push(*_a, **_k):
    raise O.OutputStoreError("graph-upload-session PUT to host returned HTTP 500")


@pytest.fixture
def no_backends(monkeypatch, tmp_path):
    for var in ("ASSET_STORE_ACCOUNT", "ASSET_STORE_CONNECTION_STRING", "RENDER_OUTPUT_LOCAL_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(O, "deliver_to_sharepoint", lambda *_a, **_k: None)


@pytest.mark.parametrize(("cancel_ok", "status"), [(True, "cancelled"), (False, "cancel_failed")])
def test_push_fail_cancels_once_and_receipt_survives_inline_fallback(monkeypatch, no_backends, cancel_ok, status):
    cancels: list[str] = []
    monkeypatch.setattr(O, "push_to_url", _failing_push)
    monkeypatch.setattr(O, "cancel_upload_session", lambda url, **_k: cancels.append(url) or cancel_ok)
    res = O.deliver_artifact(b"small", filename="x.pdf", inline_max=1024, upload_url="https://up/s")
    assert res.mode == "inline"
    assert res.upload_session_status == status
    assert cancels == ["https://up/s"]


def test_push_fail_receipt_survives_sas_and_none_fallbacks(monkeypatch, no_backends, tmp_path):
    monkeypatch.setattr(O, "push_to_url", _failing_push)
    monkeypatch.setattr(O, "cancel_upload_session", lambda *_a, **_k: True)
    none = O.deliver_artifact(b"x" * 4096, filename="x.pdf", inline_max=10, upload_url="https://up/s")
    assert none.mode == "none" and none.upload_session_status == "cancelled"
    monkeypatch.setenv("RENDER_OUTPUT_LOCAL_DIR", str(tmp_path / "o"))
    sas = O.deliver_artifact(b"x" * 4096, filename="x.pdf", inline_max=10, upload_url="https://up/s")
    assert sas.mode == "sas" and sas.upload_session_status == "cancelled"


def test_presigned_push_failure_never_cancels(monkeypatch, no_backends):
    monkeypatch.setattr(O, "push_to_url", _failing_push)
    monkeypatch.setattr(
        O, "cancel_upload_session", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no cancel"))
    )
    res = O.deliver_artifact(
        b"small", filename="x.pdf", inline_max=1024, upload_url="https://up/s", upload_kind="presigned-put"
    )
    assert res.mode == "inline"
    assert res.upload_session_status is None


def test_committed_push_never_cancels(monkeypatch, no_backends):
    monkeypatch.setattr(
        O, "push_to_url", lambda *_a, **kind: {"delivered_via": "graph-upload-session", "item_id": "id-1"}
    )
    monkeypatch.setattr(
        O, "cancel_upload_session", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no cancel"))
    )
    res = O.deliver_artifact(b"small", filename="x.pdf", inline_max=1024, upload_url="https://up/s")
    assert res.mode == "pushed"
    assert res.upload_session_status is None
