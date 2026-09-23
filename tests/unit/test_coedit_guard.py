"""The co-edit guard: a content-aware compare-and-swap on the SharePoint PUT (ORG-186).

Every test here drives a *simulator* of Graph's measured behaviour rather than a
per-test canned response ladder, because the thing under test is a protocol, not a
sequence of return values. ``_FakeGraph`` enforces the one rule the guard rests on —
a content PUT carrying ``If-Match`` succeeds only when the header matches the
destination's current eTag, and returns **412** otherwise (probed live 2026-09-08:
``412 notAllowed``, "ETag does not match current item's value"). A test that wants to
model Microsoft *withdrawing* that undocumented behaviour flips ``ignores_if_match``,
which is exactly the regression AC14 stands guard over.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from stromy_asset_transport import delivery as O

_GUID = "{FE220D24-4EA6-440A-A2BF-F41EF050BFA7}"


def _etag(n: int) -> str:
    return f'"{_GUID},{n}"'


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class _FakeGraph:
    """A minimal, stateful stand-in for the Graph endpoints the guard touches.

    Models the destination as (bytes, eTag ordinal, exists) and serves the four
    shapes the delivery path uses: the metadata read, the content download, the
    version listing, and the conditional content PUT.
    """

    def __init__(
        self,
        *,
        content: bytes = b"base-bytes",
        ordinal: int = 1,
        exists: bool = True,
        ignores_if_match: bool = False,
        metadata_status: int = 200,
        versions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.content = content
        self.ordinal = ordinal
        self.exists = exists
        self.ignores_if_match = ignores_if_match
        self.metadata_status = metadata_status
        self.versions = versions if versions is not None else []
        self.calls: list[dict[str, Any]] = []

    # ── the seams ────────────────────────────────────────────────────────────
    @property
    def etag(self) -> str:
        return _etag(self.ordinal)

    def graph_call(
        self,
        url: str,
        *,
        token: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        if_match: str | None = None,
    ) -> O.GraphResponse:
        self.calls.append({"url": url, "method": method, "if_match": if_match})
        if method == "PUT" and url.endswith(":/content"):
            return self._put(data or b"", if_match)
        if method == "GET" and ":/versions" in url:
            return _resp(200, {"value": self.versions})
        if method == "GET":
            return self._metadata()
        return _resp(200, {})

    def graph_request(
        self,
        url: str,
        *,
        token: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """The raising wrapper: only the unconditional PUT and createLink use it."""
        self.calls.append({"url": url, "method": method, "if_match": None})
        if method == "PUT" and url.endswith(":/content"):
            resp = self._put(data or b"", None)
            if resp.status >= 400:
                raise O._graph_error_for(resp, method=method, url=url)
            return resp.json
        if method == "POST" and url.endswith("/createLink"):
            return {"link": {"webUrl": "https://stromy.sharepoint.com/share/abc"}}
        return {}

    def urlopen(self, request: Any) -> Any:
        """Serves the preauthenticated content download only."""
        return _ContentResp(self.content)

    # ── behaviour ────────────────────────────────────────────────────────────
    def _metadata(self) -> O.GraphResponse:
        if not self.exists:
            return _resp(404, {"error": {"code": "itemNotFound"}})
        if self.metadata_status >= 400:
            return _resp(self.metadata_status, {"error": {"code": "serviceUnavailable"}})
        return _resp(
            200,
            {
                "id": "item-1",
                "eTag": self.etag,
                "size": len(self.content),
                "lastModifiedDateTime": "2026-09-07T18:00:00Z",
                "@microsoft.graph.downloadUrl": "https://dl.example.invalid/preauth",
            },
        )

    def _put(self, data: bytes, if_match: str | None) -> O.GraphResponse:
        honoured = if_match is None or self.ignores_if_match or if_match == self.etag
        if not honoured:
            return _resp(412, {"error": {"code": "notAllowed", "message": "ETag does not match"}})
        self.content = data
        self.ordinal += 1
        self.exists = True
        return _resp(
            200,
            {
                "id": "item-1",
                "eTag": self.etag,
                "size": len(data),
                "webUrl": "https://stromy.sharepoint.com/item-1",
            },
        )

    # ── assertions helpers ───────────────────────────────────────────────────
    def puts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "PUT"]

    def metadata_reads(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "GET" and ":/versions" not in c["url"]]


def _resp(status: int, payload: dict[str, Any]) -> O.GraphResponse:
    return O.GraphResponse(status=status, headers={}, body=json.dumps(payload).encode())


class _ContentResp:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self, _n: int | None = None) -> bytes:
        return self._raw

    def __enter__(self) -> _ContentResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> _FakeGraph:
    fake = _FakeGraph()
    monkeypatch.setenv("RENDER_SHAREPOINT_DRIVE_ID", "drive-xyz")
    monkeypatch.delenv("RENDER_SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setenv("RENDER_SHAREPOINT_BASE_PATH", "Deliverables")
    monkeypatch.setattr(O, "_graph_token", lambda: "fake-token")
    monkeypatch.setattr(O, "_graph_call", fake.graph_call)
    monkeypatch.setattr(O, "_graph_request", fake.graph_request)
    monkeypatch.setattr(O.urllib_request, "urlopen", fake.urlopen)
    return fake


def _publish(graph: _FakeGraph, raw: bytes = b"rebuilt-deck", **kwargs: Any) -> O.DeliveryResult:
    return O.deliver_artifact(raw, filename="d.pptx", inline_max=0, prefer_sharepoint=True, **kwargs)


# ── the happy path: the guard must cost nothing ──────────────────────────────


def test_a_clean_republish_costs_no_extra_reads(graph: _FakeGraph) -> None:
    """AC-happy-path: the untouched-remote case buys ZERO extra round-trips.

    One metadata read happens either way — `_peek_existing`, which predates the
    guard and runs whether or not a base_version is supplied. The guard's own
    compare is bought only after a 412, so a clean republish must show exactly
    that one read and exactly one PUT.
    """
    res = _publish(graph, base_version=graph.etag, base_sha256=_sha(graph.content))

    assert res.mode == "sharepoint"
    assert res.conflict is None
    assert res.if_match_retried is False
    assert res.base_version_absent is False
    assert len(graph.puts()) == 1
    assert len(graph.metadata_reads()) == 1, "the guard added a read to the happy path"
    assert graph.puts()[0]["if_match"] == _etag(1)


def test_an_unguarded_publish_says_so_and_sends_no_condition(graph: _FakeGraph) -> None:
    """AC7's transport half: no base_version → unguarded, and VISIBLY so."""
    res = _publish(graph)

    assert res.mode == "sharepoint"
    assert res.base_version_absent is True
    assert res.conflict is None
    assert graph.puts()[0]["if_match"] is None


# ── AC3: the negative control — a real collaborator edit refuses ─────────────


def test_a_real_collaborator_edit_is_refused_with_the_intervening_versions(
    graph: _FakeGraph,
) -> None:
    """AC3. Publish A, a collaborator overwrites with different bytes, republish
    against A's base → conflict naming who intervened, and NOTHING written."""
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.content = b"COLLABORATOR EDIT - do not lose me"
    graph.ordinal = 3
    graph.versions = [
        {
            "id": "3.0",
            "lastModifiedDateTime": "2026-09-07T19:00:00Z",
            "size": len(graph.content),
            "lastModifiedBy": {"user": {"displayName": "William Masquelier"}},
        },
        {
            "id": "2.0",
            "lastModifiedDateTime": "2026-09-07T18:00:00Z",
            "size": 12,
            "lastModifiedBy": {"user": {"displayName": "Application SharePoint"}},
        },
    ]
    survivor = graph.content

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.mode == "conflict"
    assert res.failure_code == "stale_base"
    assert res.conflict is not None
    assert res.conflict.reason == "content_changed"
    assert res.conflict.differs is True
    assert res.conflict.base_version == base_version
    assert res.conflict.current_version == _etag(3)
    payload = res.conflict.as_payload()
    assert len(payload["intervening"]) >= 1
    assert payload["intervening"][0]["author"] == "William Masquelier"
    assert graph.content == survivor, "the collaborator's bytes must survive a refusal"


def test_a_refusal_never_carries_an_email_address(graph: _FakeGraph) -> None:
    """The conflict payload is answer-shaped, not a contact record.

    Graph returns `lastModifiedBy.user.email`/`userPrincipalName` alongside the
    display name; the projection must drop them, or a co-edit refusal quietly
    widens the PII surface the ledger refuses by contract.
    """
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.content = b"different"
    graph.ordinal = 2
    graph.versions = [
        {
            "id": "2.0",
            "lastModifiedDateTime": "2026-09-07T19:00:00Z",
            "lastModifiedBy": {
                "user": {
                    "displayName": "William Masquelier",
                    "email": "william.masquelier@stromy.com.au",
                    "userPrincipalName": "william.masquelier@stromy.com.au",
                }
            },
        }
    ]

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.conflict is not None
    assert "@" not in json.dumps(res.conflict.as_payload())


# ── AC4: the inverse negative control — an editor-open must NOT refuse ───────


def test_a_zero_change_editor_save_retries_instead_of_refusing(graph: _FakeGraph) -> None:
    """AC4. The eTag moved but the bytes did not — a web editor opened the file.

    This is the case that decides the whole design: an eTag-only guard would
    refuse here, and a guard that fires on non-edits is one people disable.
    """
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.ordinal = 2  # the editor's zero-change save; bytes unchanged

    res = _publish(graph, b"rebuilt-deck", base_version=base_version, base_sha256=base_sha)

    assert res.mode == "sharepoint"
    assert res.conflict is None
    assert res.if_match_retried is True
    puts = graph.puts()
    assert len(puts) == 2, "exactly one retry, never a loop"
    assert puts[0]["if_match"] == _etag(1)
    assert puts[1]["if_match"] == _etag(2), "the retry conditions on the version it just observed"
    assert graph.content == b"rebuilt-deck"


def test_the_retry_is_bounded_to_one_attempt(graph: _FakeGraph) -> None:
    """A destination that keeps moving conflicts; it never becomes an overwrite loop."""
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.ordinal = 2

    original_put = graph._put

    def _moving_target(data: bytes, if_match: str | None) -> O.GraphResponse:
        if if_match is not None:
            graph.ordinal += 1  # someone else lands a version between our read and our write
            return _resp(412, {"error": {"code": "notAllowed"}})
        return original_put(data, if_match)

    graph._put = _moving_target  # type: ignore[method-assign]

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.mode == "conflict"
    assert res.conflict is not None
    assert res.conflict.reason == "still_conflicting"
    assert len(graph.puts()) == 2, "one conditional PUT plus one retry, then stop"


# ── fail-closed paths ────────────────────────────────────────────────────────


def test_a_missing_base_digest_fails_closed(graph: _FakeGraph) -> None:
    """base_version without base_sha256: an edit is indistinguishable from an
    editor-open, so the guard refuses rather than guessing permissively."""
    base_version = graph.etag
    graph.ordinal = 4

    res = _publish(graph, base_version=base_version)

    assert res.mode == "conflict"
    assert res.conflict is not None
    assert res.conflict.reason == "compare_unavailable"
    assert res.conflict.differs is False
    assert "include_content=True" in (res.conflict.detail or "")


def test_an_unreadable_remote_at_conflict_time_fails_closed(graph: _FakeGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the current version cannot be read, the guard refuses — never proceeds."""
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.ordinal = 5

    calls = {"n": 0}
    real_metadata = graph._metadata

    def _fails_after_the_peek() -> O.GraphResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_metadata()  # the pre-PUT probe still works
        return _resp(503, {"error": {"code": "serviceUnavailable"}})

    graph._metadata = _fails_after_the_peek  # type: ignore[method-assign]

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.mode == "conflict"
    assert res.conflict is not None
    assert res.conflict.reason == "remote_unreadable"


def test_a_remote_over_the_compare_ceiling_fails_closed(graph: _FakeGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    """An artifact too large to compare refuses rather than publishing blind."""
    monkeypatch.setenv("RENDER_COEDIT_COMPARE_MAX_BYTES", "4")
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.ordinal = 2

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.mode == "conflict"
    assert res.conflict is not None
    assert res.conflict.reason == "remote_unreadable"
    assert "compare ceiling" in (res.conflict.detail or "")


def test_a_conflict_does_not_fall_through_to_the_sas_rung(graph: _FakeGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is terminal. Rerouting the bytes to a download link would dress a
    refused write up as a delivery — the original failure wearing a new hat."""
    monkeypatch.setattr(O, "deliver", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SAS rung consulted")))
    base_version, base_sha = graph.etag, _sha(graph.content)
    graph.content = b"changed"
    graph.ordinal = 2

    res = _publish(graph, base_version=base_version, base_sha256=base_sha)

    assert res.mode == "conflict"
    assert res.download_url is None


# ── AC5: force ───────────────────────────────────────────────────────────────


def test_force_overwrites_and_records_that_it_did(graph: _FakeGraph) -> None:
    """AC5. `reconciled: true` must get past a real conflict — and leave a trace."""
    graph.content = b"COLLABORATOR EDIT"
    graph.ordinal = 3

    res = _publish(graph, b"reconciled-build", base_version=_etag(1), base_sha256=_sha(b"x"), force=True)

    assert res.mode == "sharepoint"
    assert res.forced is True
    assert res.conflict is None
    assert graph.puts()[0]["if_match"] is None, "force sends no condition"
    assert graph.content == b"reconciled-build"


# ── AC14: the guard on the guard ─────────────────────────────────────────────


def test_graph_withdrawing_if_match_is_detected_not_inherited(graph: _FakeGraph) -> None:
    """AC14. `If-Match` here is measured, not documented, so it can regress.

    If Graph stops honouring it, a stale-base PUT stops returning 412 and starts
    returning 200 — and the transport is silently back to overwriting blind. The
    2xx must be rejected as unreconcilable, never reported as a clean push.
    """
    graph.ignores_if_match = True
    graph.ordinal = 7  # the remote is far past our base

    res = _publish(graph, base_version=_etag(1), base_sha256=_sha(b"base-bytes"))

    assert res.failure_code == "graph_ignored_if_match"
    assert any("stopped supporting it" in w for w in res.warnings)


def test_a_short_write_is_also_unreconcilable(graph: _FakeGraph) -> None:
    """The second free comparand: the item we wrote must report the size we sent."""
    base_version = graph.etag
    original_put = graph._put

    def _lies_about_size(data: bytes, if_match: str | None) -> O.GraphResponse:
        resp = original_put(data, if_match)
        payload = dict(resp.json)
        payload["size"] = len(data) - 1
        return _resp(resp.status, payload)

    graph._put = _lies_about_size  # type: ignore[method-assign]

    res = _publish(graph, base_version=base_version, base_sha256=_sha(b"base-bytes"))

    assert res.failure_code == "graph_ignored_if_match"


def test_an_unparseable_etag_is_not_treated_as_evidence(graph: _FakeGraph) -> None:
    """ "Cannot tell" must never read as "regression detected" — a detector that
    fires on an eTag format change would be worse than no detector."""
    original_put = graph._put

    def _opaque_etag(data: bytes, if_match: str | None) -> O.GraphResponse:
        resp = original_put(data, if_match)
        payload = dict(resp.json)
        payload["eTag"] = "opaque-token"
        return _resp(resp.status, payload)

    graph._put = _opaque_etag  # type: ignore[method-assign]

    res = _publish(graph, base_version=graph.etag, base_sha256=_sha(b"base-bytes"))

    assert res.mode == "sharepoint"
    assert res.failure_code is None


# ── edge cases ───────────────────────────────────────────────────────────────


def test_a_base_that_no_longer_exists_publishes_fresh_with_a_note(graph: _FakeGraph) -> None:
    """The file was deleted between the fetch and the publish. `If-Match` against a
    vanished item can only 412 forever, and re-creating it loses nothing."""
    graph.exists = False

    res = _publish(graph, base_version=_etag(1), base_sha256=_sha(b"base-bytes"))

    assert res.mode == "sharepoint"
    assert res.conflict is None
    assert any("no longer exists" in w for w in res.warnings)


def test_the_etag_ordinal_split_only_accepts_the_real_shape() -> None:
    assert O._etag_ordinal('"{GUID},3"') == ("{GUID}", 3)
    assert O._etag_ordinal("{GUID},3") == ("{GUID}", 3)
    assert O._etag_ordinal('"{GUID}"') is None
    assert O._etag_ordinal('"{GUID},x"') is None
    assert O._etag_ordinal(None) is None


def test_the_whole_etag_including_its_ordinal_is_what_conditions_the_put(
    graph: _FakeGraph,
) -> None:
    """The probe settled this: the GUID alone is NOT the comparand."""
    _publish(graph, base_version=graph.etag, base_sha256=_sha(graph.content))
    assert graph.puts()[0]["if_match"] == f'"{_GUID},1"'
