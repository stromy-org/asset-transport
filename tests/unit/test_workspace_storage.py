"""Workspace-storage primitives: read, list, create-only, folder ensure.

These back an IMMUTABLE, client-readable project ledger, so the properties under
test are mostly refusals: an off-allowlist target never reaches Graph, a
traversal segment is rejected before a URL is built, a create-only path is never
overwritten with different bytes, a listing cannot exceed its limit, and a
preauthenticated download URL is never handed our bearer token (nor returned to
a caller).
"""

from __future__ import annotations

import hashlib
import json
from urllib import parse as urllib_parse

import pytest

from stromy_asset_transport import delivery as O

SITE = "stromy.sharepoint.com:/sites/ai4comms-collab"
OFF_ALLOWLIST = "stromy.sharepoint.com:/sites/someone-else"


def _target(**kw: object) -> O.SharePointTarget:
    params: dict[str, object] = {
        "site_id": SITE,
        "drive_id": "drive-1",
        "base_path": "",
        "link_mode": "webUrl",
    }
    params.update(kw)
    return O.SharePointTarget(**params)  # pyright: ignore[reportArgumentType]


@pytest.fixture(autouse=True)
def _graph_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RENDER_SHAREPOINT_ALLOWED_SITES", SITE)
    monkeypatch.delenv("RENDER_SHAREPOINT_BASE_PATH", raising=False)
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    # The bounded contention retry must never actually sleep in a unit test.
    monkeypatch.setattr(O, "_sleep", lambda _seconds: None)


class FakeDrive:
    """An in-memory Graph drive that speaks the exact URL shapes we emit."""

    def __init__(self) -> None:
        # path (relative to drive root) -> {"id", "folder", "content"}
        self.items: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str]] = []  # (method, decoded-url)
        self.locked_paths: set[str] = set()
        self.lock_attempts: dict[str, int] = {}
        self._next_id = 0

    # -- seeding helpers -------------------------------------------------
    def add_folder(self, path: str) -> str:
        return self._add(path, folder=True, content=b"")

    def add_file(self, path: str, content: bytes) -> str:
        return self._add(path, folder=False, content=content)

    def _add(self, path: str, *, folder: bool, content: bytes) -> str:
        self._next_id += 1
        item_id = f"item-{self._next_id}"
        self.items[path] = {"id": item_id, "folder": folder, "content": content}
        return item_id

    def _payload(self, path: str) -> dict[str, object]:
        item = self.items[path]
        name = path.rsplit("/", 1)[-1] or "root"
        out: dict[str, object] = {
            "id": item["id"],
            "name": name,
            "eTag": f'"{item["id"]},1"',
            "webUrl": f"https://stromy.sharepoint.com/{urllib_parse.quote(path)}",
            "createdDateTime": "2026-07-26T09:00:00Z",
        }
        if item["folder"]:
            out["folder"] = {"childCount": 0}
        else:
            content = bytes(item["content"])  # pyright: ignore[reportArgumentType]
            out["file"] = {"mimeType": "application/json"}
            out["size"] = len(content)
            out["@microsoft.graph.downloadUrl"] = f"https://sp-download.example/{item['id']}?sig=SECRET"
        return out

    def _path_by_id(self, item_id: str) -> str:
        for path, item in self.items.items():
            if item["id"] == item_id:
                return path
        raise KeyError(item_id)

    # -- the _graph_call replacement -------------------------------------
    def __call__(
        self,
        url: str,
        *,
        token: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> O.GraphResponse:
        decoded = urllib_parse.unquote(url)
        self.calls.append((method, decoded))
        assert token, "every Graph call carries the app token"

        base, _, query = decoded.partition("?")
        prefix = f"{O._GRAPH_BASE}/drives/drive-1/"
        assert base.startswith(prefix), f"unexpected URL {base}"
        rest = base[len(prefix) :]

        # /items/{id}/children  → create under a resolved parent id
        if rest.startswith("items/"):
            parent_id, _, tail = rest[len("items/") :].partition("/")
            assert tail == "children"
            parent_path = self._path_by_id(parent_id)
            return self._create_child(parent_path, data)

        # /root/children, /root:/a/b, /root:/a/b:/children, /root:/a/b:/content
        if rest == "root/children":
            return self._create_child("", data) if method == "POST" else self._children("", query)
        assert rest.startswith("root:/"), f"unexpected URL {base}"
        addressed = rest[len("root:/") :]
        if addressed.endswith(":/children"):
            path = addressed[: -len(":/children")]
            return self._create_child(path, data) if method == "POST" else self._children(path, query)
        if addressed.endswith(":/content"):
            return self._put_content(addressed[: -len(":/content")], data, query)
        return self._metadata(addressed)

    # -- handlers ---------------------------------------------------------
    def _locked(self, path: str) -> O.GraphResponse | None:
        """Return a 423 while `path` is locked, decrementing a per-path budget."""
        if path not in self.locked_paths:
            return None
        remaining = self.lock_attempts.get(path)
        if remaining is not None:
            if remaining <= 0:
                self.locked_paths.discard(path)
                return None
            self.lock_attempts[path] = remaining - 1
        return O.GraphResponse(
            status=423,
            headers={"Retry-After": "0"},
            body=json.dumps({"error": {"code": "resourceLocked"}}).encode(),
        )

    def _metadata(self, path: str) -> O.GraphResponse:
        locked = self._locked(path)
        if locked is not None:
            return locked
        if path not in self.items:
            return O.GraphResponse(status=404, headers={}, body=b'{"error":{"code":"itemNotFound"}}')
        return O.GraphResponse(
            status=200, headers={}, body=json.dumps(self._payload(path)).encode()
        )

    def _children(self, path: str, query: str = "") -> O.GraphResponse:
        """Honour `$top` and `$skip` exactly as Graph does, including `@odata.nextLink`."""
        if path and path not in self.items:
            return O.GraphResponse(status=404, headers={}, body=b"{}")
        params = urllib_parse.parse_qs(query)
        top = int(params.get("$top", ["50"])[0])
        skip = int(params.get("$skip", ["0"])[0])
        prefix = f"{path}/" if path else ""
        names = [
            p
            for p in sorted(self.items)
            if p.startswith(prefix) and "/" not in p[len(prefix) :] and p != path
        ]
        page = names[skip : skip + top]
        payload: dict[str, object] = {"value": [self._payload(p) for p in page]}
        if skip + top < len(names):
            addressed = f"root:/{urllib_parse.quote(path)}:" if path else "root"
            payload["@odata.nextLink"] = (
                f"{O._GRAPH_BASE}/drives/drive-1/{addressed}/children?$top={top}&$skip={skip + top}"
            )
        return O.GraphResponse(status=200, headers={}, body=json.dumps(payload).encode())

    def _create_child(self, parent_path: str, data: bytes | None) -> O.GraphResponse:
        payload = json.loads(data or b"{}")
        assert payload.get("@microsoft.graph.conflictBehavior") == "fail", (
            "folder creation must never silently rename"
        )
        name = payload["name"]
        path = f"{parent_path}/{name}" if parent_path else name
        if path in self.items:
            return O.GraphResponse(status=409, headers={}, body=b'{"error":{"code":"nameAlreadyExists"}}')
        self.add_folder(path)
        return O.GraphResponse(
            status=201, headers={}, body=json.dumps(self._payload(path)).encode()
        )

    def _put_content(self, path: str, data: bytes | None, query: str) -> O.GraphResponse:
        assert "conflictBehavior=fail" in query, "create-only writes must not replace"
        locked = self._locked(path)
        if locked is not None:
            return locked
        if path in self.items:
            return O.GraphResponse(status=409, headers={}, body=b'{"error":{"code":"nameAlreadyExists"}}')
        self.add_file(path, data or b"")
        return O.GraphResponse(
            status=201, headers={}, body=json.dumps(self._payload(path)).encode()
        )


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    fake = FakeDrive()
    monkeypatch.setattr(O, "_graph_call", fake)
    return fake


@pytest.fixture
def download(monkeypatch: pytest.MonkeyPatch, drive: FakeDrive) -> list[dict[str, object]]:
    """Capture content fetches so the token-forwarding assertion is real."""
    fetches: list[dict[str, object]] = []

    class _Resp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, n: int = -1) -> bytes:
            return self._payload if n < 0 else self._payload[:n]

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def _urlopen(req, *_a: object, **_kw: object):
        fetches.append({"url": req.full_url, "headers": dict(req.headers)})
        item_id = req.full_url.rsplit("/", 1)[-1].split("?")[0]
        path = drive._path_by_id(item_id)
        return _Resp(bytes(drive.items[path]["content"]))  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(O.urllib_request, "urlopen", _urlopen)
    return fetches


# ── path safety ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "segment",
    ["..", ".", "a/b", "", " leading", "trailing ", "trailing.", "with:colon", "star*"],
)
def test_unsafe_segments_are_refused_before_any_url_is_built(segment: str) -> None:
    with pytest.raises(O.UnsafePath):
        O.SharePointFileRef.of(_target(), segment)


def test_ref_keeps_validated_segments() -> None:
    ref = O.SharePointFileRef.of(_target(), "KVGO", "02 Working", "event.json")
    assert ref.segments == ("KVGO", "02 Working", "event.json")
    assert ref.name == "event.json"


# ── allowlist: deny-by-default, before any Graph call ─────────────────────────


def test_off_allowlist_target_never_reaches_graph(drive: FakeDrive) -> None:
    ref = O.SharePointFileRef.of(_target(site_id=OFF_ALLOWLIST), "x.json")
    with pytest.raises(O.TargetNotAllowed):
        O.get_file_metadata(ref)
    with pytest.raises(O.TargetNotAllowed):
        O.list_children(_target(site_id=OFF_ALLOWLIST))
    with pytest.raises(O.TargetNotAllowed):
        O.ensure_folder(_target(site_id=OFF_ALLOWLIST), ["Proj"])
    with pytest.raises(O.TargetNotAllowed):
        O.create_file_once(ref, b"{}", digest=hashlib.sha256(b"{}").hexdigest())
    assert drive.calls == []


def test_unset_allowlist_denies_everything(monkeypatch: pytest.MonkeyPatch, drive: FakeDrive) -> None:
    monkeypatch.delenv("RENDER_SHAREPOINT_ALLOWED_SITES", raising=False)
    with pytest.raises(O.TargetNotAllowed):
        O.get_file_metadata(O.SharePointFileRef.of(_target(), "x.json"))
    assert drive.calls == []


# ── metadata + content read ──────────────────────────────────────────────────


def test_get_file_metadata_returns_etag_and_kind(drive: FakeDrive) -> None:
    drive.add_file("Proj/event.json", b'{"a":1}')
    info = O.get_file_metadata(O.SharePointFileRef.of(_target(), "Proj", "event.json"))
    assert info.name == "event.json"
    assert info.etag
    assert info.is_folder is False
    assert info.size == 7


def test_get_file_metadata_missing_is_typed(drive: FakeDrive) -> None:
    with pytest.raises(O.FileNotFound):
        O.get_file_metadata(O.SharePointFileRef.of(_target(), "nope.json"))


def test_read_file_uses_the_download_url_without_our_token(
    drive: FakeDrive, download: list[dict[str, object]]
) -> None:
    drive.add_file("Proj/event.json", b'{"kind":"milestone"}')
    data = O.read_file(O.SharePointFileRef.of(_target(), "Proj", "event.json"))
    assert data == b'{"kind":"milestone"}'
    assert len(download) == 1
    headers = {k.lower() for k in download[0]["headers"]}  # pyright: ignore[reportGeneralTypeIssues]
    assert "authorization" not in headers, "never forward the app token to the redirect target"


def test_read_file_refuses_an_over_ceiling_item(drive: FakeDrive) -> None:
    drive.add_file("Proj/big.json", b"x" * 5000)
    with pytest.raises(O.GraphRequestError, match="read ceiling"):
        O.read_file(O.SharePointFileRef.of(_target(), "Proj", "big.json"), max_bytes=100)


def test_read_file_missing_is_typed(drive: FakeDrive) -> None:
    with pytest.raises(O.FileNotFound):
        O.read_file(O.SharePointFileRef.of(_target(), "gone.json"))


# ── bounded listing ──────────────────────────────────────────────────────────


def test_list_children_is_bounded_by_the_requested_limit(drive: FakeDrive) -> None:
    drive.add_folder("Proj")
    for i in range(5):
        drive.add_file(f"Proj/e{i}.json", b"{}")
    items, cursor = O.list_children(_target(), ["Proj"], limit=3)
    assert len(items) == 3
    assert [c for c in drive.calls if "$top=3" in c[1]], (
        "the limit is pushed to Graph, not applied only client-side"
    )
    # The rest is reachable ONLY through the returned cursor — history is paged,
    # never returned unbounded in one call.
    assert cursor is not None
    rest, tail = O.list_children(_target(), ["Proj"], limit=3, cursor=cursor)
    assert len(rest) == 2
    assert tail is None
    assert {i.name for i in items} | {i.name for i in rest} == {f"e{i}.json" for i in range(5)}


def test_list_children_limit_is_capped_by_the_hard_ceiling(drive: FakeDrive) -> None:
    drive.add_folder("Proj")
    O.list_children(_target(), ["Proj"], limit=10_000)
    assert f"$top={O.LIST_CHILDREN_MAX_LIMIT}" in drive.calls[-1][1]


def test_list_children_rejects_a_foreign_cursor(drive: FakeDrive) -> None:
    with pytest.raises(O.UnsafePath):
        O.list_children(_target(), ["Proj"], cursor="https://evil.example/steal")
    assert drive.calls == []


def test_list_children_rejects_traversal(drive: FakeDrive) -> None:
    with pytest.raises(O.UnsafePath):
        O.list_children(_target(), ["Proj", ".."])
    assert drive.calls == []


# ── folder ensure (including the create race) ────────────────────────────────


def test_ensure_folder_creates_the_missing_chain(drive: FakeDrive) -> None:
    info = O.ensure_folder(_target(), ["KVGO", "Campaign", "02 Working"])
    assert info is not None and info.is_folder
    assert set(drive.items) == {"KVGO", "KVGO/Campaign", "KVGO/Campaign/02 Working"}


def test_ensure_folder_is_idempotent(drive: FakeDrive) -> None:
    O.ensure_folder(_target(), ["KVGO", "Campaign"])
    before = dict(drive.items)
    O.ensure_folder(_target(), ["KVGO", "Campaign"])
    assert drive.items == before


def test_ensure_folder_survives_a_concurrent_create(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDrive
) -> None:
    """A 409 means a sibling session won the race — re-resolve, never rename."""
    original_create = drive._create_child
    raced: dict[str, bool] = {}

    def _racing_create(parent_path: str, data: bytes | None) -> O.GraphResponse:
        name = json.loads(data or b"{}")["name"]
        path = f"{parent_path}/{name}" if parent_path else name
        if not raced.get(path):
            raced[path] = True
            drive.add_folder(path)  # the "other" session lands it first
        return original_create(parent_path, data)

    monkeypatch.setattr(drive, "_create_child", _racing_create)
    info = O.ensure_folder(_target(), ["KVGO"])
    assert info is not None and info.is_folder
    assert sorted(drive.items) == ["KVGO"], "no '-1' twin was created"


def test_ensure_folder_refuses_a_file_where_a_folder_belongs(drive: FakeDrive) -> None:
    drive.add_file("KVGO", b"not a folder")
    with pytest.raises(O.GraphRequestError, match="is a file"):
        O.ensure_folder(_target(), ["KVGO", "Campaign"])


def test_ensure_folder_rejects_traversal(drive: FakeDrive) -> None:
    with pytest.raises(O.UnsafePath):
        O.ensure_folder(_target(), ["KVGO", "../../etc"])
    assert drive.calls == []


# ── create-only writes ───────────────────────────────────────────────────────


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_create_file_once_creates(drive: FakeDrive) -> None:
    body = b'{"kind":"milestone"}'
    ref = O.SharePointFileRef.of(_target(), "Proj", "workspace-events", "e1.json")
    info = O.create_file_once(ref, body, digest=_digest(body))
    assert info.name == "e1.json"
    assert drive.items["Proj/workspace-events/e1.json"]["content"] == body


def test_identical_replay_creates_no_second_record(
    drive: FakeDrive, download: list[dict[str, object]]
) -> None:
    body = b'{"kind":"milestone"}'
    ref = O.SharePointFileRef.of(_target(), "Proj", "e1.json")
    first = O.create_file_once(ref, body, digest=_digest(body))
    writes_before = len([c for c in drive.calls if c[0] == "PUT"])
    again = O.create_file_once(ref, body, digest=_digest(body))
    assert again.id == first.id
    assert len(drive.items) == 1
    # The retry did attempt the create (and was refused), but wrote nothing new.
    assert len([c for c in drive.calls if c[0] == "PUT"]) == writes_before + 1


def test_different_content_at_the_same_key_collides(
    drive: FakeDrive, download: list[dict[str, object]]
) -> None:
    ref = O.SharePointFileRef.of(_target(), "Proj", "e1.json")
    O.create_file_once(ref, b'{"v":1}', digest=_digest(b'{"v":1}'))
    with pytest.raises(O.IdempotencyCollision):
        O.create_file_once(ref, b'{"v":2}', digest=_digest(b'{"v":2}'))
    assert drive.items["Proj/e1.json"]["content"] == b'{"v":1}', "the record was not overwritten"


def test_create_file_once_rejects_a_mismatched_digest(drive: FakeDrive) -> None:
    ref = O.SharePointFileRef.of(_target(), "Proj", "e1.json")
    with pytest.raises(ValueError, match="does not match"):
        O.create_file_once(ref, b'{"v":1}', digest=_digest(b"something-else"))
    assert drive.calls == []


# ── contention: bounded retry, then an honest defer ──────────────────────────


def test_a_transient_lock_is_retried_then_succeeds(drive: FakeDrive) -> None:
    drive.locked_paths.add("Proj/e1.json")
    drive.lock_attempts["Proj/e1.json"] = 2  # unlocks on the third attempt
    body = b"{}"
    ref = O.SharePointFileRef.of(_target(), "Proj", "e1.json")
    info = O.create_file_once(ref, body, digest=_digest(body))
    assert info.name == "e1.json"


def test_a_persistent_lock_defers_without_writing(drive: FakeDrive) -> None:
    drive.locked_paths.add("Proj/e1.json")
    body = b"{}"
    ref = O.SharePointFileRef.of(_target(), "Proj", "e1.json")
    with pytest.raises(O.SharePointLockedError) as exc:
        O.create_file_once(ref, body, digest=_digest(body))
    assert exc.value.attempts == 4
    assert exc.value.status_code == 423
    assert exc.value.error_code == "resourceLocked"
    assert drive.items == {}, "nothing is written after a deferred response"


# ── site→drive resolution + base path ────────────────────────────────────────


def test_base_path_is_prefixed_to_every_addressed_path(drive: FakeDrive) -> None:
    drive.add_file("Client Deliverables/Proj/e1.json", b"{}")
    info = O.get_file_metadata(
        O.SharePointFileRef.of(_target(base_path="Client Deliverables"), "Proj", "e1.json")
    )
    assert info.name == "e1.json"
